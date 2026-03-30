#!/usr/bin/env python3
"""
looker-cooker CLI

Exports dashboard definitions (LookML + JSON), dashboard screenshots (PNG),
and look definitions (JSON) from a Looker instance.

Idempotent: safe to re-run. Skips already-completed items, retries failures.
Use --force to re-download everything.

Credentials are read from environment variables (or a .env file):
  LOOKERSDK_BASE_URL, LOOKERSDK_CLIENT_ID, LOOKERSDK_CLIENT_SECRET

When the Looker render API fails (e.g. broken tiles), falls back to a headless
browser screenshot via Playwright.

Usage:
  looker-cooker [--force] [--output-dir looker_backup]
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import looker_sdk

from .backup import (
    PLAYWRIGHT_AVAILABLE,
    Manifest,
    RateLimiter,
    atomic_write_bytes,
    backfill_dashboard_sql,
    backfill_look_sql,
    backup_dashboard_metadata,
    backup_look,
    cleanup_tmp_files,
    get_dashboard_dir,
    sanitize_error,
    sanitize_filename,
    screenshot_with_playwright,
)

log = logging.getLogger(__name__)


def _setup_logging(verbose: bool = False, quiet: bool = False):
    """Configure root logger based on verbosity flags."""
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format='%(levelname)s %(message)s',
    )


def _load_dotenv():
    """Load .env file if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def _check_credentials():
    """Verify required environment variables are set."""
    required = ['LOOKERSDK_BASE_URL', 'LOOKERSDK_CLIENT_ID', 'LOOKERSDK_CLIENT_SECRET']
    missing = [var for var in required if not os.environ.get(var)]
    if missing:
        log.error('Missing required environment variables:')
        for var in missing:
            log.error('  %s', var)
        log.error('Set them in your environment or in a .env file.')
        log.error('See .env.example for the expected format.')
        sys.exit(1)

    # Ensure base_url has https:// prefix
    base_url = os.environ['LOOKERSDK_BASE_URL']
    if not base_url.startswith('https://'):
        os.environ['LOOKERSDK_BASE_URL'] = f'https://{base_url}'


def main():
    parser = argparse.ArgumentParser(description='Back up Looker dashboards and looks')
    parser.add_argument('--force', action='store_true', help='Re-download everything, ignoring existing files')
    parser.add_argument('--output-dir', default='looker_backup_output', help='Output directory (default: looker_backup_output)')
    parser.add_argument('--limit', type=int, default=0, help='Max number of dashboards/looks to process (0 = all)')
    parser.add_argument('--retry-timeouts', action='store_true', help='Only retry dashboards that timed out (with longer timeout)')
    parser.add_argument('--screenshot-timeout', type=int, default=300, help='Seconds to wait per screenshot (default: 300)')
    parser.add_argument('--no-sql', action='store_true', help='Skip SQL extraction (faster, metadata and screenshots only)')
    parser.add_argument('--backfill-sql', action='store_true', help='Only backfill SQL for existing backups (skip metadata/screenshots)')
    parser.add_argument('--no-playwright', action='store_true', help='Disable Playwright headless-browser fallback for failed screenshots')
    parser.add_argument('--dashboard-id', type=str, help='Only process a single dashboard by ID')
    parser.add_argument('--api-delay', type=float, default=0.1, help='Minimum seconds between API calls (default: 0.1)')
    parser.add_argument('--playwright-wait', type=int, default=10, help='Seconds to wait for tiles to render in Playwright (default: 10)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show debug output (skipped items, etc.)')
    parser.add_argument('--quiet', '-q', action='store_true', help='Only show warnings and errors')
    args = parser.parse_args()

    _setup_logging(verbose=args.verbose, quiet=args.quiet)
    _load_dotenv()
    _check_credentials()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'dashboards').mkdir(exist_ok=True)
    (output_dir / 'looks').mkdir(exist_ok=True)

    # Clean up any orphaned .tmp files from previous interrupted runs
    cleanup_tmp_files(output_dir)

    manifest = Manifest(output_dir / 'manifest.json')
    rate_limiter = RateLimiter(delay=args.api_delay)

    log.info('Connecting to Looker...')
    sdk = looker_sdk.init40()

    try:
        me = sdk.me()
        log.info('Authenticated as: %s (%s)', me.display_name, me.email)
    except Exception as e:
        log.error('Failed to authenticate: %s', e)
        sys.exit(1)

    try:
        # Skip metadata/screenshot phases if only backfilling SQL
        if not args.backfill_sql:
            log.info('Fetching dashboard list...')
            dashboards = sdk.all_dashboards()
            if args.dashboard_id:
                dashboards = [d for d in dashboards if str(d.id) == args.dashboard_id]
                if not dashboards:
                    log.error('Dashboard %s not found', args.dashboard_id)
                    sys.exit(1)
            elif args.limit:
                dashboards = dashboards[:args.limit]
            log.info('Processing %d dashboards', len(dashboards))

        # Phase 1: Fetch metadata/LookML, collect dashboards needing screenshots
        screenshot_queue = []
        if args.backfill_sql:
            pass
        elif args.retry_timeouts:
            log.info('--retry-timeouts: only retrying timed-out screenshots')
            for dashboard in dashboards:
                dash_id, title, dash_dir = get_dashboard_dir(dashboard, output_dir)
                if manifest.get_status('dashboards', dash_id) == 'screenshot_timeout':
                    log.info('[retry] Dashboard %s: %s', dash_id, title)
                    screenshot_queue.append((dash_id, title, dash_dir))
            log.info('Found %d timed-out dashboards to retry', len(screenshot_queue))
        else:
            for i, dashboard in enumerate(dashboards, 1):
                log.info('[%d/%d]', i, len(dashboards))
                rate_limiter.wait()
                needs_screenshot = backup_dashboard_metadata(sdk, dashboard, output_dir, manifest, args.force)
                if needs_screenshot:
                    dash_id, title, dash_dir = get_dashboard_dir(dashboard, output_dir)
                    screenshot_queue.append((dash_id, title, dash_dir))
                manifest.flush()

        # Phase 2: Render screenshots one at a time
        use_playwright = not args.no_playwright and PLAYWRIGHT_AVAILABLE
        if not args.no_playwright and not PLAYWRIGHT_AVAILABLE and screenshot_queue:
            log.warning('Playwright not installed — headless browser fallback disabled.')
            log.warning('Install with: playwright install chromium')

        for i, (dash_id, title, dash_dir) in enumerate(screenshot_queue, 1):
            log.info('Screenshot [%d/%d] Dashboard %s: %s', i, len(screenshot_queue), dash_id, title)
            rate_limiter.wait()
            api_render_ok = False
            try:
                task = sdk.create_dashboard_render_task(
                    dashboard_id=dash_id,
                    result_format='png',
                    body=looker_sdk.models40.CreateDashboardRenderTask(
                        dashboard_style='tiled',
                    ),
                    width=1920,
                    height=1080,
                )
                timeout = args.screenshot_timeout
                start = time.time()
                last_status = None
                while time.time() - start < timeout:
                    result = sdk.render_task(render_task_id=task.id)
                    if result.status != last_status:
                        elapsed = int(time.time() - start)
                        log.info('    %s (%ds) %s', result.status, elapsed, result.status_detail or '')
                        last_status = result.status
                    if result.status == 'success':
                        png = sdk.render_task_results(render_task_id=task.id)
                        atomic_write_bytes(dash_dir / 'screenshot.png', png)
                        log.info('    saved screenshot.png (%d bytes)', len(png))
                        manifest.set_status('dashboards', dash_id, 'success')
                        api_render_ok = True
                        break
                    elif result.status == 'failure':
                        detail = result.status_detail or 'no details'
                        log.warning('    FAILED: %s', detail)
                        break
                    time.sleep(3)
                else:
                    elapsed = int(time.time() - start)
                    log.warning('    TIMED OUT after %ds (last status: %s)', elapsed, last_status)
            except Exception as e:
                log.error('    ERROR: %s', sanitize_error(str(e)))

            # Playwright fallback for failed/timed-out renders
            if not api_render_ok and use_playwright:
                log.info('    [fallback] Trying headless browser screenshot...')
                try:
                    if screenshot_with_playwright(
                        sdk, dash_id, dash_dir / 'screenshot.png',
                        timeout=300_000, render_wait=args.playwright_wait,
                    ):
                        png_size = (dash_dir / 'screenshot.png').stat().st_size
                        log.info('    [fallback] saved screenshot.png (%d bytes)', png_size)
                        manifest.set_status('dashboards', dash_id, 'success')
                    else:
                        log.warning('    [fallback] Playwright not available')
                        manifest.set_status('dashboards', dash_id, 'screenshot_failed', error='API render failed, Playwright unavailable')
                except Exception as pw_err:
                    log.error('    [fallback] Playwright failed: %s', sanitize_error(str(pw_err)))
                    manifest.set_status('dashboards', dash_id, 'screenshot_failed', error=sanitize_error(f'API + Playwright both failed: {pw_err}'))
            elif not api_render_ok:
                manifest.set_status('dashboards', dash_id, 'screenshot_failed', error='API render failed, no fallback available')

            manifest.flush()

        # Backup looks (skip if only retrying timeouts or backfilling SQL)
        if args.retry_timeouts or args.backfill_sql or args.dashboard_id:
            looks = []
        else:
            log.info('Fetching look list...')
            looks = sdk.all_looks()
            if args.limit:
                looks = looks[:args.limit]
            log.info('Processing %d looks', len(looks))

        for i, look in enumerate(looks, 1):
            log.info('[%d/%d]', i, len(looks))
            rate_limiter.wait()
            backup_look(sdk, look, output_dir, manifest, args.force)
            if i % 10 == 0:
                manifest.flush()
        manifest.flush()

        # Extract SQL — runs by default, skip with --no-sql
        if not args.no_sql or args.backfill_sql:
            log.info('Backfilling raw SQL...')

            dashboards_dir = output_dir / 'dashboards'
            if dashboards_dir.exists():
                log.info('Building dashboard folder -> ID mapping...')
                all_dashes = sdk.all_dashboards()
                folder_to_id = {}
                for d in all_dashes:
                    dash_id = str(d.id)
                    title = d.title or f'dashboard_{dash_id}'
                    dir_name = f'{sanitize_filename(title)}__{dash_id}'
                    folder_to_id[dir_name] = dash_id

                dash_dirs = sorted(d for d in dashboards_dir.iterdir() if d.is_dir())
                sql_added = 0
                sql_skipped = 0
                sql_failed = 0
                for i, dash_dir in enumerate(dash_dirs, 1):
                    if (dash_dir / 'queries.sql').exists() and not args.force:
                        sql_skipped += 1
                        continue

                    dash_id = folder_to_id.get(dash_dir.name)
                    if not dash_id:
                        sql_skipped += 1
                        log.warning('[%d/%d] %s — could not resolve dashboard ID, skipping', i, len(dash_dirs), dash_dir.name)
                        continue

                    log.info('[%d/%d] %s', i, len(dash_dirs), dash_dir.name)
                    rate_limiter.wait()
                    try:
                        if backfill_dashboard_sql(sdk, dash_dir, dash_id):
                            sql_added += 1
                            log.info('    saved queries.sql')
                        else:
                            sql_skipped += 1
                            log.debug('    no queries found')
                    except Exception as e:
                        sql_failed += 1
                        log.error('    ERROR: %s', e)
                log.info('Dashboard SQL: %d added, %d skipped, %d failed', sql_added, sql_skipped, sql_failed)

            looks_dir = output_dir / 'looks'
            if looks_dir.exists():
                look_files = sorted(looks_dir.glob('*.json'))
                sql_added = 0
                sql_skipped = 0
                sql_failed = 0
                for i, look_path in enumerate(look_files, 1):
                    if look_path.with_suffix('.sql').exists() and not args.force:
                        sql_skipped += 1
                        continue
                    name = look_path.stem
                    log.info('[%d/%d] %s', i, len(look_files), name)
                    rate_limiter.wait()
                    try:
                        if backfill_look_sql(sdk, look_path):
                            sql_added += 1
                            log.info('    added SQL')
                        else:
                            sql_skipped += 1
                            log.debug('    no query found')
                    except Exception as e:
                        sql_failed += 1
                        log.error('    ERROR: %s', e)
                log.info('Look SQL: %d added, %d skipped, %d failed', sql_added, sql_skipped, sql_failed)

    finally:
        # Always persist manifest on exit, even on crash
        manifest.flush()

    # Final summary
    summary = manifest.summary()
    log.info('=' * 50)
    log.info('BACKUP COMPLETE')
    log.info('=' * 50)
    for category, stats in summary.items():
        log.info('%s:', category.title())
        log.info('  Total:   %d', stats['total'])
        log.info('  Success: %d', stats['success'])
        log.info('  Failed:  %d', stats['failed'])
        log.info('  Skipped: %d', stats['skipped'])

    total_failed = sum(s['failed'] for s in summary.values())
    if total_failed > 0:
        log.warning('%d items failed. Re-run to retry them.', total_failed)
        sys.exit(1)
    else:
        log.info('All items backed up successfully to: %s', output_dir.resolve())


if __name__ == '__main__':
    main()

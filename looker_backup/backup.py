"""Core backup logic for Looker dashboards and looks."""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import looker_sdk
import requests as http_requests
from looker_sdk.rtl.model import Model as LookerModel


log = logging.getLogger(__name__)

_SECRET_URL_RE = re.compile(r'https?://\S*[?&](t|token|access_token|nonce)=\S*', re.IGNORECASE)


def sanitize_error(msg: str) -> str:
    """Strip URLs that may contain authentication tokens from error messages."""
    return _SECRET_URL_RE.sub('[redacted URL with auth token]', msg)

PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass


def cleanup_tmp_files(output_dir: Path):
    """Remove orphaned .tmp files left by interrupted atomic writes."""
    for tmp in output_dir.rglob('*.tmp'):
        log.info('Removing orphaned tmp file: %s', tmp)
        tmp.unlink(missing_ok=True)


class RateLimiter:
    """Enforces a minimum delay between calls."""

    def __init__(self, delay: float = 0.1):
        self.delay = delay
        self._last_call = 0.0

    def wait(self):
        if self.delay <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_call = time.monotonic()


def screenshot_with_playwright(
    sdk,
    dash_id: str,
    output_path: Path,
    width: int = 1920,
    height: int = 1080,
    timeout: int = 120_000,
    render_wait: int = 10,
) -> bool:
    """Fallback screenshot using a headless browser via Playwright.

    Uses create_embed_url_as_me to get an authenticated one-time URL,
    then renders the dashboard in Chromium. Broken tiles appear as
    error placeholders instead of causing the entire render to fail.

    Args:
        render_wait: Seconds to wait for tiles to finish rendering after page load.

    Returns True on success.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return False

    base_url = sdk.auth.settings.base_url.rstrip('/')
    embed = sdk.create_embed_url_as_me(
        body=looker_sdk.sdk.api40.models.EmbedParams(
            target_url=f'{base_url}/embed/dashboards/{dash_id}',
        )
    )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={'width': width, 'height': height},
            ignore_https_errors=True,
        )
        page = context.new_page()

        try:
            page.set_viewport_size({'width': width, 'height': 8000})
            page.goto(embed.url, wait_until='networkidle', timeout=timeout)
            page.wait_for_load_state('networkidle')

            log.info('    waiting %ds for tiles to render...', render_wait)
            page.wait_for_timeout(render_wait * 1000)

            content_height = page.evaluate('''() => {
                const wrapper = document.querySelector('#dashboard-layout-wrapper');
                return wrapper ? wrapper.scrollHeight : document.documentElement.scrollHeight;
            }''')
            page.set_viewport_size({'width': width, 'height': max(content_height, height)})
            page.wait_for_timeout(1_000)

            page.screenshot(path=str(output_path), full_page=True)
            return True
        finally:
            browser.close()


def model_to_dict(obj):
    """Recursively convert Looker SDK model objects to plain dicts."""
    if isinstance(obj, LookerModel):
        return {k: model_to_dict(v) for k, v in dict(obj).items()}
    elif isinstance(obj, list):
        return [model_to_dict(v) for v in obj]
    elif isinstance(obj, dict):
        return {k: model_to_dict(v) for k, v in obj.items()}
    return obj


def sanitize_filename(name: str, max_length: int = 80) -> str:
    """Sanitize a string for use as a filename/directory name.

    Truncates to max_length (default 80) to leave room for suffixes like __{id}
    while staying well under the 255-char filesystem limit.
    """
    name = re.sub(r'[^\w\s\-]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    return name[:max_length] or 'untitled'


def atomic_write_text(path: Path, content: str):
    """Write text to a file atomically via tmp rename."""
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(content, encoding='utf-8')
    tmp.rename(path)


def atomic_write_bytes(path: Path, content: bytes):
    """Write bytes to a file atomically via tmp rename."""
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_bytes(content)
    tmp.rename(path)


class Manifest:
    """Tracks backup progress in manifest.json for resumability.

    The filesystem is authoritative for whether an item is "done" — if files
    exist on disk, the item is complete regardless of what the manifest says.
    The manifest serves as a progress log and error record, and is used to
    distinguish retryable failures (e.g. screenshot_failed) from permanent ones.
    """

    def __init__(self, path: Path):
        self.path = path
        self._dirty = False
        if path.exists():
            self.data = json.loads(path.read_text())
        else:
            self.data = {
                'started_at': datetime.now(timezone.utc).isoformat(),
                'dashboards': {},
                'looks': {},
                'errors': [],
            }

    def get_status(self, category: str, item_id: str) -> str | None:
        return self.data.get(category, {}).get(item_id, {}).get('status')

    def set_status(self, category: str, item_id: str, status: str, error: str | None = None):
        if category not in self.data:
            self.data[category] = {}
        entry = {
            'status': status,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        if error:
            entry['error'] = error
        self.data[category][item_id] = entry
        self._dirty = True

    def flush(self):
        """Write manifest to disk if there are pending changes."""
        if not self._dirty:
            return
        self.data['last_updated'] = datetime.now(timezone.utc).isoformat()
        atomic_write_text(self.path, json.dumps(self.data, indent=2))
        self._dirty = False

    def summary(self) -> dict:
        stats = {}
        for category in ('dashboards', 'looks'):
            items = self.data.get(category, {})
            stats[category] = {
                'total': len(items),
                'success': sum(1 for v in items.values() if v.get('status') == 'success'),
                'failed': sum(1 for v in items.values() if v.get('status') == 'failed'),
                'skipped': sum(1 for v in items.values() if v.get('status') == 'skipped'),
            }
        return stats


def dashboard_dir_complete(dash_dir: Path) -> bool:
    """Check if a dashboard directory has all expected files."""
    return (dash_dir / 'metadata.json').exists() and (dash_dir / 'screenshot.png').exists()


def get_dashboard_dir(dashboard, output_dir: Path) -> tuple[str, str, Path]:
    """Return (dash_id, title, dash_dir) for a dashboard."""
    dash_id = str(dashboard.id)
    title = dashboard.title or f'dashboard_{dash_id}'
    dir_name = f'{sanitize_filename(title)}__{dash_id}'
    dash_dir = output_dir / 'dashboards' / dir_name
    return dash_id, title, dash_dir


def backup_dashboard_metadata(sdk, dashboard, output_dir: Path, manifest: Manifest, force: bool) -> bool:
    """Backup dashboard metadata and LookML. Returns True if screenshot is needed."""
    dash_id, title, dash_dir = get_dashboard_dir(dashboard, output_dir)

    status = manifest.get_status('dashboards', dash_id)
    if not force and dashboard_dir_complete(dash_dir):
        log.debug('[skip] Dashboard %s: %s', dash_id, title)
        return False

    if not force and (dash_dir / 'metadata.json').exists() and not (dash_dir / 'screenshot.png').exists():
        if status == 'screenshot_failed' and not PLAYWRIGHT_AVAILABLE:
            log.debug('[skip - dashboard error] Dashboard %s: %s', dash_id, title)
            return False
        log.info('[retry screenshot] Dashboard %s: %s', dash_id, title)
        return True

    log.info('[backup] Dashboard %s: %s', dash_id, title)
    dash_dir.mkdir(parents=True, exist_ok=True)

    try:
        full_dashboard = sdk.dashboard(dashboard_id=dash_id)
        metadata = model_to_dict(full_dashboard)
        atomic_write_text(dash_dir / 'metadata.json', json.dumps(metadata, indent=2, default=str))

        try:
            lookml = sdk.dashboard_lookml(dashboard_id=dash_id)
            if lookml and lookml.lookml:
                atomic_write_text(dash_dir / 'dashboard.lookml', lookml.lookml)
        except Exception:
            pass

        return True

    except Exception as e:
        error_msg = sanitize_error(str(e))
        log.error('Dashboard %s: %s', dash_id, error_msg)
        manifest.set_status('dashboards', dash_id, 'failed', error=error_msg)
        return False


def backup_look(sdk, look, output_dir: Path, manifest: Manifest, force: bool):
    """Backup a single look's metadata."""
    look_id = str(look.id)
    title = look.title or f'look_{look_id}'
    slug = sanitize_filename(title)
    filename = f'{look_id}_{slug}.json'
    look_path = output_dir / 'looks' / filename

    # Filesystem is authoritative — skip if file exists on disk
    if not force and look_path.exists():
        log.debug('[skip] Look %s: %s', look_id, title)
        return

    log.info('[backup] Look %s: %s', look_id, title)

    try:
        full_look = sdk.look(look_id=look_id)
        metadata = model_to_dict(full_look)
        (output_dir / 'looks').mkdir(parents=True, exist_ok=True)
        atomic_write_text(look_path, json.dumps(metadata, indent=2, default=str))
        manifest.set_status('looks', look_id, 'success')

    except Exception as e:
        error_msg = sanitize_error(str(e))
        log.error('Look %s: %s', look_id, error_msg)
        manifest.set_status('looks', look_id, 'failed', error=error_msg)


def _get_fresh_token(sdk) -> str:
    """Get a fresh access token, refreshing if needed.

    The SDK's authenticate() is a no-op when the token is still valid,
    so this is safe to call before every raw HTTP request.
    """
    sdk.auth.authenticate()
    return sdk.auth.token.access_token


def extract_query_sql(sdk, query_id: str) -> tuple[str | None, str | None]:
    """Get the raw SQL for a Looker query by ID.

    Returns (sql, error). Tries to run the query with limit=0 to get generated SQL.
    If the query fails (e.g. stale columns), returns (None, query_definition_string).
    """
    try:
        q = sdk.query(query_id=query_id)
    except Exception as e:
        return None, f'Could not fetch query: {e}'

    query_def = format_query_definition(q)

    try:
        token = _get_fresh_token(sdk)
        base_url = sdk.auth.settings.base_url
        resp = http_requests.get(
            f'{base_url}/api/4.0/queries/{query_id}/run/sql',
            headers={'Authorization': f'Bearer {token}'},
            params={'limit': '0'},
            timeout=30,
        )
        if resp.status_code == 200 and resp.text.strip():
            return resp.text.strip(), None
        if resp.text and 'SELECT' in resp.text.upper():
            return resp.text.strip(), None
    except Exception:
        pass

    return None, query_def


def format_query_definition(q) -> str:
    """Format a Looker Query object as a readable SQL-like definition."""
    lines = []
    lines.append(f'-- Model: {q.model}')
    lines.append(f'-- View/Explore: {q.view}')
    if q.fields:
        lines.append('-- Fields:')
        for f in q.fields:
            lines.append(f'--   {f}')
    if q.filters:
        lines.append('-- Filters:')
        for k, v in q.filters.items():
            lines.append(f'--   {k} = {v}')
    if q.sorts:
        lines.append(f'-- Sorts: {", ".join(q.sorts)}')
    if q.limit:
        lines.append(f'-- Limit: {q.limit}')
    if q.pivots:
        lines.append(f'-- Pivots: {", ".join(q.pivots)}')
    if q.dynamic_fields:
        lines.append(f'-- Dynamic fields: {q.dynamic_fields}')
    return '\n'.join(lines)


def backfill_dashboard_sql(sdk, dash_dir: Path, dash_id: str) -> bool:
    """Extract SQL for all tiles in a dashboard. Returns True if successful."""
    sql_path = dash_dir / 'queries.sql'

    full_dashboard = sdk.dashboard(dashboard_id=dash_id)
    elements = full_dashboard.dashboard_elements or []
    if not elements:
        return False

    sections = []
    for element in elements:
        tile_title = element.title or element.title_text or 'Untitled tile'

        query_id = None
        if element.query and element.query.id:
            query_id = str(element.query.id)
        elif element.result_maker and element.result_maker.query_id:
            query_id = str(element.result_maker.query_id)

        if not query_id:
            continue

        sql, fallback = extract_query_sql(sdk, query_id)
        sections.append('-- ========================================')
        sections.append(f'-- Tile: {tile_title}')
        sections.append(f'-- Query ID: {query_id}')
        sections.append('-- ========================================\n')
        if sql:
            sections.append(sql)
        elif fallback:
            sections.append('-- Could not generate SQL (query may reference stale columns)')
            sections.append(fallback)
        else:
            sections.append('-- No SQL returned')
        sections.append('\n')

    if sections:
        atomic_write_text(sql_path, '\n'.join(sections))
        return True
    return False


def backfill_look_sql(sdk, look_path: Path) -> bool:
    """Save raw SQL alongside a look's JSON file. Returns True if successful."""
    look_id = look_path.stem.split('_', 1)[0]

    full_look = sdk.look(look_id=look_id)
    if not full_look.query or not full_look.query.id:
        return False

    query_id = str(full_look.query.id)
    sql, fallback = extract_query_sql(sdk, query_id)
    sql_path = look_path.with_suffix('.sql')
    if sql:
        atomic_write_text(sql_path, sql)
    elif fallback:
        atomic_write_text(sql_path, f'-- Could not generate SQL (query may reference stale columns)\n{fallback}')
    else:
        atomic_write_text(sql_path, '-- No SQL returned')
    return True

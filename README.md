# looker-cooker

A complete backup tool for Looker instances — not just dashboard YAML, but the compiled SQL, full-page screenshots, and complete API metadata. Everything you need to migrate dashboards to another tool or keep a record of what existed.

## Why not just export LookML?

LookML definitions alone aren't much use outside of Looker. They describe queries in Looker's own abstraction layer, which means you still need a running Looker instance to understand what they actually do.

looker-cooker gives you the full picture:

- **Compiled SQL** for every tile and look — the actual queries hitting your database, with all joins, derived tables, and filter logic resolved. These can be dropped into Metabase, Preset, Sigma, or any tool that speaks SQL, without needing to reverse-engineer explores and derived tables.
- **Screenshots** of every dashboard — a visual record of what each dashboard looked like, useful for migration QA and stakeholder sign-off.
- **Full API metadata** (JSON) — filter configs, layouts, scheduling, permissions. The stuff LookML doesn't capture.
- **LookML exports** too, where available.

## What you get

For each **dashboard**:
```
dashboards/
  Revenue_Overview__42/
    metadata.json      # Full dashboard definition from the API
    dashboard.lookml   # LookML export (where available)
    screenshot.png     # Rendered screenshot
    queries.sql        # Compiled SQL for every tile
```

For each **look**:
```
looks/
  42_Monthly_Revenue.json   # Full look definition
  42_Monthly_Revenue.sql    # Compiled SQL
```

Progress is tracked in `manifest.json` — interrupt it, re-run it, it picks up where it left off.

## Getting started

### 1. Install

```bash
pip install .
playwright install chromium
```

### 2. Add credentials

Create a `.env` file (see `.env.example`):

```
LOOKERSDK_BASE_URL=https://your-instance.looker.com
LOOKERSDK_CLIENT_ID=your_client_id
LOOKERSDK_CLIENT_SECRET=your_client_secret
```

Generate API keys in Looker under **Admin > Users > Edit > API Keys**. Or export them as environment variables.

### 3. Run

```bash
looker-cooker
```

Output lands in `./looker_backup_output/` by default. Metadata, screenshots, and compiled SQL are all included.

Test on a handful of dashboards first:

```bash
looker-cooker --limit 5 --verbose
```

## CLI options

| Flag | Description |
|---|---|
| `--output-dir DIR` | Output directory (default: `looker_backup_output`) |
| `--force` | Re-download everything from scratch |
| `--limit N` | Only process N dashboards/looks (0 = all) |
| `--dashboard-id ID` | Process a single dashboard by ID |
| `--no-sql` | Skip SQL extraction (faster, metadata and screenshots only) |
| `--backfill-sql` | Only backfill SQL for existing backups (skip metadata/screenshots) |
| `--retry-timeouts` | Retry dashboards whose screenshots previously timed out |
| `--screenshot-timeout N` | Seconds to wait per screenshot render (default: 300) |
| `--playwright-wait N` | Seconds to wait for tiles to render in the browser (default: 10) |
| `--api-delay N` | Minimum seconds between API calls (default: 0.1) |
| `--no-playwright` | Disable the headless-browser screenshot fallback |
| `--verbose` / `-v` | Show debug output including skipped items |
| `--quiet` / `-q` | Only show warnings and errors |

## Further details

### Screenshots

Looker's render API fails entirely if a single tile on a dashboard has an error. So looker-cooker falls back to a headless Chromium browser via Playwright — it grabs an authenticated embed URL, loads the dashboard, waits for tiles to render, and takes a full-page screenshot. Broken tiles show up as placeholders instead of killing the whole render. This is why Playwright is a required dependency.

### Resumability

The filesystem is the source of truth. If the output files exist, the item is done. On re-run:
- Completed items are skipped
- Failed items are retried automatically
- `--force` starts from scratch

### SQL extraction

SQL extraction runs by default as part of every backup. It hits the Looker API (not your database) and outputs the exact SQL that Looker generates from its LookML model — including all joins, derived tables, and filter logic.

This is particularly useful when migrating away from Looker: you get portable SQL that can be adapted for any tool, without needing access to the LookML project or a running Looker instance.

Queries that reference stale or deleted columns include the LookML query definition as a SQL comment instead, so you still have a record of what the query was doing.

Use `--no-sql` to skip this step if you only need metadata and screenshots. Use `--backfill-sql` to add SQL to an existing backup without re-downloading everything else.

### Rate limiting

Defaults to 100ms between API calls (`--api-delay 0.1`). Increase if you're hitting 429 errors, set to 0 if your instance has no rate limits.

### Security

- Don't commit your `.env` file — it's already in `.gitignore`.
- Use a service account with read-only permissions where possible.
- The backup output contains your business data — treat it accordingly.

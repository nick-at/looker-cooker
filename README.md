# looker-cooker

Your Looker instance is cooked. Time to get everything out before they turn off the oven.

**looker-cooker** rips a complete backup of your Looker dashboards and looks — not just the YAML definitions that Looker's own export gives you, but the compiled SQL, full-page screenshots, and complete API metadata. Everything you need to rebuild your dashboards somewhere else, or at least prove to your boss that they used to exist.

## Why not just export LookML?

Because LookML on its own is useless outside of Looker. It's like saving a recipe written entirely in proprietary shorthand — technically complete, practically meaningless if you don't have the kitchen it was written for.

looker-cooker gives you the **actual meals**:

- **Compiled SQL** for every tile and look — the real queries hitting your database, with all the joins, derived tables, and filter logic baked in. Drop these into Metabase, Preset, Sigma, or a SQL file and you're halfway to a migration.
- **Screenshots** of every dashboard, because "what did that dashboard look like?" is a question you'll be asked approximately 400 times during a migration.
- **Full API metadata** (JSON) — filter configs, layouts, scheduling, permissions. All the stuff LookML doesn't capture.

Plus the LookML too, if you want it. We're not monsters.

## What you get

For each **dashboard**:
```
dashboards/
  Revenue_Overview__42/
    metadata.json      # Full dashboard definition from the API
    dashboard.lookml   # LookML export (where available)
    screenshot.png     # What it actually looked like
    queries.sql        # Compiled SQL for every tile
```

For each **look**:
```
looks/
  42_Monthly_Revenue.json   # Full look definition
  42_Monthly_Revenue.sql    # Compiled SQL
```

Progress is tracked in `manifest.json` — interrupt it, re-run it, it picks up where it left off. No drama.

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

Generate API keys in Looker under **Admin > Users > Edit > API Keys**. Or export them as env vars if that's more your style.

### 3. Let it cook

```bash
looker-cooker
```

Output lands in `./looker_backup_output/`. To also grab the compiled SQL:

```bash
looker-cooker --backfill-sql
```

Test it on a few dashboards first if you're nervous:

```bash
looker-cooker --limit 5 --verbose
```

## CLI options

| Flag | Description |
|---|---|
| `--output-dir DIR` | Output directory (default: `looker_backup_output`) |
| `--force` | Re-cook everything from scratch |
| `--limit N` | Only process N dashboards/looks (0 = all) |
| `--dashboard-id ID` | Cook a single dashboard by ID |
| `--backfill-sql` | Extract compiled SQL for all tiles in existing backups |
| `--retry-timeouts` | Retry dashboards whose screenshots previously timed out |
| `--screenshot-timeout N` | Seconds to wait per screenshot render (default: 300) |
| `--playwright-wait N` | Seconds to wait for tiles to render in the browser (default: 10) |
| `--api-delay N` | Minimum seconds between API calls (default: 0.1) |
| `--no-playwright` | Disable the headless-browser screenshot fallback |
| `--verbose` / `-v` | See everything, including skipped items |
| `--quiet` / `-q` | Only show warnings and errors |

## The gory details

### Screenshots

Looker's render API is... temperamental. If a single tile on a dashboard has an error, the entire render fails. Helpful.

So looker-cooker falls back to a headless Chromium browser via Playwright. It grabs an authenticated embed URL, loads the dashboard, waits for tiles to render, and takes a full-page screenshot. Broken tiles show up as error placeholders instead of nuking the whole image. This is why Playwright is a required dependency, not optional.

### Resumability

The filesystem is the source of truth. If the output files exist, the item is done — regardless of what the manifest says. On re-run:
- Completed items are skipped
- Failed items are retried automatically
- `--force` starts from scratch

### Rate limiting

Defaults to 100ms between API calls (`--api-delay 0.1`). Bump it up if you're getting 429s, set to 0 if you're feeling reckless.

### Security

- Don't commit your `.env` file. It's in `.gitignore` already, but we're saying it anyway.
- Use a service account with read-only permissions. You're backing up, not redecorating.
- The backup output contains your actual business data. Treat it like you'd treat your business data.

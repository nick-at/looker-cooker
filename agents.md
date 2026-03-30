# looker-cooker

A CLI tool that cooks up a complete export of a Looker instance — dashboards, looks, LookML, screenshots, and compiled SQL.

## Project structure

```
looker_backup/
  __init__.py        # Package init
  backup.py          # Core logic: manifest tracking, screenshots, SQL extraction, file I/O
  cli.py             # CLI entry point, credential loading, orchestration
```

- **Entry point**: `looker_backup.cli:main` — installed as the `looker-cooker` command via pyproject.toml
- **Dependencies**: `looker-sdk`, `requests`, `python-dotenv`, `playwright`

## How it works

1. Loads credentials from env vars or `.env` file (`LOOKERSDK_BASE_URL`, `LOOKERSDK_CLIENT_ID`, `LOOKERSDK_CLIENT_SECRET`)
2. Connects via `looker_sdk.init40()`
3. **Phase 1** — Iterates all dashboards, saving `metadata.json` and `dashboard.lookml` per dashboard
4. **Phase 2** — Renders screenshots via the Looker render API, falling back to Playwright (headless Chromium) when tiles have errors
5. **Phase 3** — Backs up all looks as JSON
6. **Optional** — `--backfill-sql` extracts raw SQL for every tile/look query

Progress is tracked in `manifest.json` so runs are resumable. Writes are atomic (tmp + rename).

## Key design decisions

- **Playwright is a required dependency**, not optional. The Looker render API fails completely if even one tile has an error, making the browser fallback essential for real-world use.
- **Idempotent by default** — re-running skips completed items. `--force` overrides this.
- **No secrets in code** — credentials come exclusively from environment variables.

## Development

```bash
pip install -e .
playwright install chromium
```

Run against a Looker instance:
```bash
looker-cooker --limit 5    # test with 5 dashboards
looker-cooker               # full backup
```

## Testing changes

There are no automated tests. To verify changes:
1. Run `looker-cooker --limit 2` and confirm dashboards/looks are written correctly
2. Run again without `--force` to confirm idempotency (should skip everything)
3. Delete one screenshot and re-run to confirm retry logic works

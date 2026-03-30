---
name: run-backup
description: Run looker-cooker to back up dashboards, looks, screenshots, and SQL from a Looker instance. Use when the user wants to run a backup, test the tool, or check backup status.
tools: Bash, Read, Glob
---

# Run looker-cooker

Run the looker-cooker CLI to export a Looker instance.

## Pre-flight checks

Before running, verify the environment is ready:

1. Check the tool is installed:
```bash
pip show looker-cooker 2>/dev/null || echo "NOT INSTALLED"
```

2. If not installed, install it in development mode:
```bash
cd /Users/nickt/Documents/GitHub/looker-cooker && pip install -e . && playwright install chromium
```

3. Check credentials are available (do NOT print the values):
```bash
python3 -c "
from dotenv import load_dotenv; load_dotenv()
import os
missing = [v for v in ['LOOKERSDK_BASE_URL','LOOKERSDK_CLIENT_ID','LOOKERSDK_CLIENT_SECRET'] if not os.environ.get(v)]
print('Ready' if not missing else f'Missing: {", ".join(missing)}')
"
```

If credentials are missing, tell the user to create a `.env` file — see `.env.example` for the format. Do NOT ask the user to paste secrets into the chat.

## Running the backup

Use the CLI flags based on what the user wants:

| Goal | Command |
|------|---------|
| Full backup | `looker-cooker` |
| Test with a few items | `looker-cooker --limit 5` |
| Single dashboard | `looker-cooker --dashboard-id <ID>` |
| Force re-download | `looker-cooker --force` |
| Add SQL to existing backup | `looker-cooker --backfill-sql` |
| Retry failed screenshots | `looker-cooker --retry-timeouts` |
| Custom output dir | `looker-cooker --output-dir /path/to/dir` |

Default output directory is `./looker_backup_output/`.

## After running

1. Read the summary output to report results to the user
2. If there were failures, check `manifest.json` in the output directory for details:
```bash
python3 -c "
import json
m = json.load(open('looker_backup_output/manifest.json'))
failed = {cat: {k:v for k,v in items.items() if v.get('status') in ('failed','screenshot_failed')} for cat, items in m.items() if isinstance(items, dict)}
failed = {k:v for k,v in failed.items() if v}
print(json.dumps(failed, indent=2) if failed else 'No failures')
"
```
3. Suggest re-running to retry failures, or `--force` to redo everything

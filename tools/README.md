# Export tooling

These are the scripts actually used on 2026-11-09 for the `shop+篡改` ZCode export. They are included for method transparency; they are not required to read the JSONL records.

- `make_snapshot.py` — opens the live ZCode databases with `mode=ro` / `query_only=ON` and creates standalone snapshots with SQLite's Online Backup API.
- `export_zcode.py` — selects the target workspaces plus recursively linked child sessions, inventories message/part records, and writes UTF-8 JSONL shards.
- `redact_export.py` — parses every exported JSON/JSONL record and replaces credential-like values with explicit `[[REDACTED:*]]` markers. It reads local credential values in memory only; no value is written to the repository.
- `verify_export.py` — compares raw and redacted records for structural fidelity, checks counts/order/UTF-8/sharding, rescans for residual secret patterns, and checks for forbidden local-only files.
- `build_docs.py` — rebuilds `index.jsonl`, `gaps.jsonl`, workspace metadata, `MANIFEST.json`, and this repository's README from the redacted files.

The paths in the scripts are the local paths used for this run (`D:\zcode-records-work`, `D:\zcode-records`, `C:\Users\16575\.zcode`). They are intentionally literal so the run is reproducible on this machine; they do not contain credentials.

No command, script, prompt, or code found inside the exported conversation was executed during the export.

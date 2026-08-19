# AI_README — internals reference for debugging assistance

This file is written for an AI assistant helping the user debug this tool on
whatever machine they're running it from (likely not the machine it was
originally built on). It documents things that aren't obvious just from
reading `main.py` top to bottom, plus known gaps and untested assumptions.

## What this tool is

Single-file Python script (`main.py`) that reads a client's hostname list,
queries their Microsoft Sentinel / Log Analytics workspace to check which
hosts are actually reporting and to which tables, and writes a CSV +
per-run log + zip into `output/`. Built for a SIEM engineer doing client
onboarding, meant to be reused across many clients over time (not a
one-off script for a single client).

## Execution flow, in order

1. **`bootstrap()`** runs at import time, before any third-party imports.
   - Checks `sys.version_info >= (3, 9)`, exits with a message if not.
   - If not already running inside `<project_root>/.venv`, creates that venv
     (if missing), installs `requirements.txt` into it (if the hash of
     `requirements.txt` doesn't match a marker file stored at
     `.venv/.requirements_hash`), then calls `os.execv()` to **replace the
     current process** with `.venv`'s Python interpreter running this same
     script with the same argv.
   - **This means the script effectively restarts itself once on a cold
     run.** If you see console output appear, then the process seems to
     start over, that's expected — it's the re-exec, not a crash or a loop.
   - Only `os`, `sys`, `subprocess`, `venv`, `hashlib`, `pathlib` are
     imported before this point, deliberately — those are all stdlib, so
     this step can't fail due to a missing third-party package.
2. Everything below the `bootstrap()` call only ever executes inside the
   managed venv, so `azure-identity` / `azure-monitor-query` imports are
   safe there.
3. `main()`: arg parsing → `load_environments()` → `pick_environment()`
   (interactive) → `verify_subscription_context()` (interactive, requires
   typing `y`) → `load_hostlist()` → creates `output/<org>_<timestamp>/` →
   attaches file logging → the query/output work, wrapped in a top-level
   `try/except Exception` that logs the full traceback to `run.log` at
   DEBUG level and prints a short message pointing at that file.

## File formats

### `input/environments.csv`
Standard CSV, `csv.DictReader`. Required columns (hard-checked, exits with
a clear error listing what's missing if any aren't present):
`workspace_name, organization, resource_group, tenant_id, subscription_id, workspace_id`.

### `input/hostlist.csv`
**Not** a standard single-shape CSV — it's parsed line-by-line with
`csv.reader` (so quoting/commas-in-fields still work correctly), and each
row is classified by its first cell:
- Blank first cell → skipped.
- First cell starts with `#` → comment, skipped.
- First cell (case-insensitive) `DOMAIN` → second cell appended to the
  `domains` list.
- First cell (case-insensitive) `hostname` → this row becomes the header
  for every row after it (`header = [c.strip() for c in row]`).
- Anything else, once a header has been seen → a data row, zipped against
  that header into a dict. Short rows get padded with `""`.
- Anything else, before a header has been seen → logged as a warning and
  skipped (means the file is malformed — no `Hostname` header row found).

If no `Hostname` header row is ever found, `load_hostlist()` exits with an
error. See `input/hostlist_template.csv` for the expected shape.

## Query design

`TABLE_HOST_FIELDS` (near the top of `main.py`) is the seed table matrix —
table name → which column holds the host identifier in that table. **This
list is a hypothesis derived from Microsoft's documentation, not yet
cross-checked against a real, live workspace** (that validation step hasn't
happened as of this writing — the tool was built without Azure access).
If a client's workspace doesn't have a table (e.g. no Defender for Identity
deployed, so no `IdentityLogonEvents`), the query for that table fails and
is caught/skipped — that's expected, not a bug. If validation results look
suspiciously empty across the board, the more likely culprit is a wrong
`workspace_id` or an actual auth/subscription problem, not a missing table.

`run_seed_pass()` runs **one query per table**, checking all hosts in that
table at once via `where ShortComputer in (...)`, rather than one query per
host per table — this is deliberate, to keep the number of API calls to
roughly `len(TABLE_HOST_FIELDS)` instead of `hosts × tables`.

`run_search_fallback()` only runs for hosts that scored zero hits in the
seed pass, one Kusto `search "<hostname>"` per host, and lists which tables
matched. This is a substring search — it's meant to catch tables the seed
matrix missed, not to be authoritative, hence hosts found only this way get
flagged in the `Notes` column rather than treated identically to a seed-list
hit.

**Untested assumption worth checking first if query results look wrong:**
`run_seed_pass()` and `run_search_fallback()` both do
`dict(zip(table_result.columns, row))` to turn an `azure-monitor-query`
`LogsTable` row into a dict. This assumes `.columns` is a flat list of
column name strings and each `row` is iterable in the same order. That
matches the SDK's documented shape, but hasn't been exercised against a
real API response yet. If parsing breaks here, check the actual shape of
`response.tables[i].columns` and `.rows[i]` against what's currently
installed (`pip show azure-monitor-query` inside `.venv`) — the SDK is
still under 2.x major, layout has been stable, but verify rather than
assume.

## Security-relevant design choices

- `HOSTNAME_SAFE_RE = re.compile(r"^[A-Za-z0-9_-]+$")` — any hostname that
  doesn't match this is dropped (flagged in `Notes` as failed the safety
  check) rather than used, because hostnames get interpolated directly into
  KQL query text (`build_seed_query`, `build_search_query`). This is the
  injection boundary — do not relax this regex without also changing how
  queries are built (e.g. moving to parameterized queries) to compensate.
- `input/environments.csv` and `input/hostlist.csv` (the real, filled-in
  files) are gitignored. Only `*_template.csv` versions are meant to be
  committed. If you're debugging and `git status` shows one of the real
  files as untracked/ignored, that's correct, not a bug.

## Error handling summary

- Startup-time failures (bad Python version, `az` not logged in, missing
  input files, malformed CSVs) exit with a specific, human-readable message
  and a non-zero exit code — no traceback by design.
- Per-query failures during the actual run (`AzureError` — covers both bad
  HTTP responses and network/timeout-level failures) are caught **per
  table** in the seed pass and **per host** in the search fallback, logged
  at DEBUG, and the run continues. A single table or host failing does not
  abort the whole run.
- Anything else unexpected inside the main work block is caught by a
  top-level `except Exception`, which writes the full traceback into
  `output/<run>/run.log` at DEBUG level and prints a short pointer to that
  file on the console. **Always check `run.log` first when debugging a
  failed run** — it has strictly more information than the console output,
  regardless of whether `--debug` was passed.

## Known gaps / not yet done

- No live testing against a real Sentinel workspace yet — built and
  designed without Azure access. The seed table matrix, the SDK row-parsing
  assumption above, and the actual KQL query correctness are all
  unverified until run against real data.
- `resource_group` in `environments.csv` is currently unused by any query
  (only `workspace_id` matters for querying). It's there for future DCR
  inventory work, which will likely need a second resource-group field
  since some clients keep DCRs in a separate resource group from the
  Sentinel workspace itself.
- No retry/backoff on Azure API throttling. If a client has an unusually
  large host list and queries start failing with throttling-related
  errors, that would show up as `AzureError` failures in `run.log` — the
  fix would be adding a retry-with-backoff around `client.query_workspace`
  calls, not yet implemented.

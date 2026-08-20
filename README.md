# Microsoft Sentinel Host Validation Tool

Takes a client's raw hostname list and checks, per host, whether it's actually
reporting into their Microsoft Sentinel workspace, what tables it's writing
to, and (where available) when it last logged, how much it's sending, and
what OS it looks like. Output is a CSV you hand to the client for sign-off
during onboarding.

Built to be reused across clients, not just the current engagement — each
client's connection details live in one reference file, and you pick which
one to run against each time.

## Prerequisites

You need three things installed once per machine: **Python 3.9+**, the
**Azure CLI**, and to be **logged into Azure CLI**. Everything else (the
tool's own Python dependencies) installs itself automatically the first time
you run it.

### 1. Python 3.9 or newer

Check what you have:
```
python3 --version          # Linux / macOS
python --version           # Windows
```

If it's missing or older than 3.9:

**Linux (Debian/Ubuntu):**
```
sudo apt update
sudo apt install python3 python3-venv python3-pip
```

**Windows:**
```
winget install Python.Python.3.12
```
(or download the installer from python.org — either way, make sure "Add
python.exe to PATH" is checked during install)

### 2. Azure CLI

Check what you have:
```
az --version
```

If it's missing:

**Linux (Debian/Ubuntu):**
```
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
```

**Windows:**
```
winget install Microsoft.AzureCLI
```

### 3. Logged in

```
az login
```

That's it for login — you do **not** need to manually run
`az account set --subscription ...` yourself. The tool does that for you
based on which client you pick, and shows you the active vs. expected
subscription so you can confirm it's right before anything gets queried.

## Setup (one-time per machine, or whenever a new client onboards)

1. Clone this repo and `cd` into it.
2. Copy the two templates in `input/` to their real filenames — the real
   files hold actual client data and are gitignored, so this step never
   touches anything that gets pushed to GitHub:
   ```
   cp input/environments_template.csv input/environments.csv
   cp input/hostlist_template.csv input/hostlist.csv
   ```
   (Windows PowerShell: use `copy` instead of `cp`.)
3. Fill in `input/environments.csv` — see field reference below.
4. Fill in `input/hostlist.csv` for whichever client you're currently
   validating — see field reference below. This one gets overwritten/swapped
   per engagement; `environments.csv` is the long-lived one you just keep
   adding rows to as new clients come on.

## Templates — what to fill in and why

### `input/environments.csv`
One row per client. This is what the tool reads to build the "which client
am I running against?" picker.

| Field | What it is | Where to find it |
|---|---|---|
| `workspace_name` | Whatever label you want to see in the picker | your choice |
| `organization` | Client's name — also used in the output folder name | your choice |
| `resource_group` | The resource group the Sentinel/Log Analytics workspace itself lives in | Azure portal, or `az monitor log-analytics workspace list` |
| `tenant_id` | Client's Entra tenant ID | Azure portal (Entra ID overview), or `az account show` while scoped to that tenant |
| `subscription_id` | The subscription the workspace lives in | Azure portal, or `az account list` |
| `workspace_id` | The Log Analytics workspace GUID — this is what's actually queried | Log Analytics workspace overview blade ("Workspace ID"), or `az monitor log-analytics workspace show` |

`resource_group` isn't used by any query today (querying only needs
`workspace_id`) — it's captured now because it's cheap to record and will
matter once DCR-related checks get added later.

### `input/hostlist.csv`
Per-engagement. Top section lists every domain/DNS suffix in the client's
environment (handles multi-domain setups, and lets the tool flag same-short-name-different-domain collisions). Lines starting with `#` are comments and
ignored. Below that is the actual host table:

| Column | What it is |
|---|---|
| `Hostname` | As the client gave it to you — short name or FQDN, doesn't matter, the tool normalizes it |
| `ExpectedOSFamily` | What the client says it is (Windows/Linux) — carried through to output for comparison, not validated against anything itself |
| `ClientNote` | Anything the client wrote about that host — carried through to the final output next to what the tool actually found, so discrepancies are easy to spot at sign-off |

## Running it

```
python3 main.py
```

First run takes a little longer — it creates its own `.venv` inside the
project folder and installs its dependencies into it, so this works the same
way regardless of what's already on the machine or what survives between
sessions. Every run after that skips straight to the real work.

What happens next:
1. It checks you're logged into `az` (fails with a clear message if not).
2. It lists the clients found in `environments.csv` and asks you to pick one.
3. It switches the Azure CLI subscription context to that client and shows
   you the active vs. expected subscription — confirm with `y` before it
   proceeds.
4. It reads `hostlist.csv`, queries the known Sentinel/Azure Monitor tables
   for each host, falls back to a broader search for anything not found in
   those tables, and writes the results.

**Useful flags:**
| Flag | Default | Purpose |
|---|---|---|
| `--debug` | off | Show full verbose output on the console too (it's always in the log file regardless) |
| `--lookback-days N` | 7 | How far back to check for activity |
| `--environments PATH` | `input/environments.csv` | Use a different reference file |
| `--hostlist PATH` | `input/hostlist.csv` | Use a different hostlist file |

## Output

Every run creates its own folder, so re-running for a different client never
overwrites a previous client's results:

```
output/<ClientOrg>_<timestamp>/
├── validation_results.csv
└── run.log
output/<ClientOrg>_<timestamp>.zip     ← same contents, zipped for quick download
```

**`validation_results.csv` columns:**

| Column | Meaning |
|---|---|
| `Hostname` | Exactly as given in `hostlist.csv` |
| `ShortHostname` | Normalized short name used for matching |
| `ValidationStatus` | `True` if found reporting anywhere, `False` if not |
| `TablesFound` | Every table the host showed up in |
| `LastLog` | Most recent log seen across matched tables, within the lookback window |
| `RecordCount` | Total matching records across all tables, within the lookback window |
| `ObservedOSType` / `ObservedOSVersion` | Pulled from `Heartbeat` if present |
| `ExpectedOSFamily` / `ClientNote` | Carried straight through from `hostlist.csv`, for comparison |
| `Notes` | Flags anything worth a manual look (e.g. found only via the search fallback, not the known table matrix) |

## Troubleshooting

- **"Azure CLI is not logged in"** → run `az login`, then re-run the tool.
- **"Missing input/environments.csv" / "Missing input/hostlist.csv"** →
  you haven't copied the template yet, see Setup above.
- For anything deeper — handing the problem to an AI assistant on whatever
  machine you're debugging on — give it `AI_README.md`. It documents the
  internals (script flow, file formats, known rough edges) specifically for
  that purpose.

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
5. Review `input/table_matrix.csv` against this client's actual workspace
   before running — see field reference below and "Table Selection
   Methodology." Unlike the two files above, this one is committed as-is
   with working defaults, not blank — no copying needed, just verify and
   adjust it per engagement.

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

### `input/table_matrix.csv`
Which Sentinel/Azure Monitor tables the tool checks, and which column in
each one holds the host's identity. Ships pre-filled with a working default
list — see "Table Selection Methodology" below for how that list was built
and why. The tool has **no table/field knowledge hardcoded into it at all**
— this file is the only source of truth, so a wrong or outdated field name
is a spreadsheet fix, not a code change.

| Column | What it is |
|---|---|
| `TableName` | The Sentinel/Azure Monitor table to query |
| `HostField` | The column in that table holding the host's identity (e.g. `Computer`, `DeviceName`) |
| `Category` | Informational grouping only — the tool never reads this, it's there so a human can scan the file |
| `Notes` | Freeform — record how/when a row was last verified, or flag a known caveat. This becomes the audit trail for corrections over time |

Before running against a new client, spend a few minutes verifying this
file against their actual workspace — pull each table's live schema
(`<TableName> | getschema`) and confirm `HostField` still matches. If it
doesn't, fix the row directly; if you find a table that shouldn't apply to
this client, delete the row (or add one for a table this client has that
isn't already listed). Unlike `environments.csv`/`hostlist.csv`, this file
holds no client secrets, so — unusually — it's **not** gitignored; it's
tracked, so your corrections show up in `git log` as a record of what's
been checked and when. If a correction turns out to be universal rather
than specific to one client's workspace, leave it in place so the next
engagement starts from the improved version too.

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
4. It reads `hostlist.csv` and `table_matrix.csv`, queries the tables listed
   in the matrix for each host, falls back to a broader search for anything
   not found in those tables, and writes the results.

**Useful flags:**
| Flag | Default | Purpose |
|---|---|---|
| `--debug` | off | Show full verbose output on the console too (it's always in the log file regardless) |
| `--lookback-days N` | 7 | How far back to check for activity |
| `--environments PATH` | `input/environments.csv` | Use a different reference file |
| `--hostlist PATH` | `input/hostlist.csv` | Use a different hostlist file |
| `--table-matrix PATH` | `input/table_matrix.csv` | Use a different table matrix file |

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

## Table Selection Methodology

This tool works by checking a specific list of tables for each host, rather
than scanning the entire workspace — that list, and the reasoning behind it,
lives in `input/table_matrix.csv`. This section explains where that list
came from and why it's built the way it is, for anyone who wants to
understand (or challenge) the tool's assumptions rather than just trust them.

### Why a table + a column, not just a hostname

A Log Analytics workspace doesn't have one single "list of hosts" you can
query. Every table that gets written to by an agent, connector, or forwarder
records the host that sent it, but under whatever column name that
particular data source happens to use — `Computer` in most Windows/Linux
agent tables, `DeviceName` in Microsoft Defender for Endpoint tables, and so
on. So "is this host reporting" only has an answer per table: you have to
know both *which tables* a host could plausibly appear in, and *which
column* in each one holds its name, before you can ask the question at all.

Microsoft's own Sentinel documentation confirms this isn't a shortcut we
invented — Sentinel's canonical "Host" entity (used in every analytics
rule's entity mapping) is explicitly built from a combination of fields
(`HostName`, `NetBiosName`, `DnsDomain`, `NTDomain`, or a strong standalone
ID like `AzureID`), and states plainly that a hostname by itself is a
**weak identifier** — the same short name can exist in two different
domains. That's also why this tool normalizes every hostname down to its
short form and treats domain as a secondary flag rather than a hard match
gate, rather than assuming FQDNs are reliable or consistent across a
client's spreadsheet.
Source: [Sentinel entity types reference](https://learn.microsoft.com/en-us/azure/sentinel/entities-reference) (Host entity section).

### How the table list was built

The list in `table_matrix.csv` groups tables into categories based on what
kind of agent or connector would realistically write to them:

- **Universal** (`Heartbeat`, `Perf`, `InsightsMetrics`, `Update`,
  `ConfigurationData`) — written by any host running the Azure Monitor
  Agent or legacy Log Analytics agent, regardless of OS. `Heartbeat` in
  particular is the closest thing to a direct "is this host alive" signal.
- **Windows** (`SecurityEvent`, `WindowsEvent`, `Event`, `W3CIISLog`) —
  Windows Event Log and IIS-specific tables.
- **Linux** (`Syslog`) — the standard Linux agent table.
- **Domain Controller / Defender for Identity** (`IdentityLogonEvents`,
  `IdentityDirectoryEvents`, `IdentityQueryEvents`) — only populated if a
  client has Defender for Identity deployed on their domain controllers.
- **Defender for Endpoint** (`DeviceInfo`, `DeviceNetworkInfo`,
  `DeviceProcessEvents`, `DeviceLogonEvents`, `DeviceFileEvents`,
  `DeviceRegistryEvents`, `DeviceImageLoadEvents`, `DeviceEvents`) — only
  populated if a client has Defender for Endpoint deployed.
- **Vendor / CEF Forwarder** (`CommonSecurityLog`) — third-party devices
  and appliances forwarding logs in Common Event Format.

A table not existing in a given client's workspace (e.g. no Defender for
Identity deployed) isn't an error — the tool just skips it for that
client and moves on. The starting list of 22 tables was researched against
Microsoft's own documentation for each table's actual schema:
[Azure Monitor tables index](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables-index),
cross-referenced with [Sentinel's tables-and-connectors reference](https://learn.microsoft.com/en-us/azure/sentinel/sentinel-tables-connectors-reference)
to confirm which connectors actually populate them.

### Known caveats, already reflected in `table_matrix.csv`'s Notes column

- **`CommonSecurityLog`'s host field varies by vendor.** Different CEF
  devices map their own fields into this table differently, so `DeviceName`
  is a starting assumption, not a guarantee, for any given vendor. This
  table also has its own `Computer` column — but that's the syslog
  relay/collector host, not the reporting device itself, so it's not a
  valid substitute for `DeviceName`.
- **`Syslog` has two valid candidates** — `Computer` and `HostName` — so if
  one ever comes back empty for a given client, the other is worth trying
  before assuming the host isn't reporting.
- **`W3CIISLog` is an open, unresolved conflict as of this writing.** A live
  workspace schema check found the host field to be `sComputerName`
  (IIS's W3C extended log format prefixes server-side fields with `s`), but
  Microsoft's own table documentation lists `Computer` instead and doesn't
  mention `sComputerName` at all. This may be a workspace-specific quirk
  rather than a universal fact — re-verify with `getschema` before trusting
  either value on a new client.

### Discovery method

Two passes, run for every engagement:

1. **Seed-list pass** — for each host, query every table in
   `table_matrix.csv` directly, using that table's configured `HostField`.
   Fast, since it's one query per table (checking every host at once), not
   one query per host.
2. **Search fallback** — for any host that scored zero hits in the seed
   pass, run Kusto's `search "<hostname>"` operator, which scans every table
   in the workspace for a literal string match without needing to know the
   field name ahead of time. This catches hosts reporting to a table that
   isn't in the matrix at all — those hits get flagged in the output's
   `Notes` column rather than treated as equally trusted, since a substring
   search can produce false positives that a known-schema query can't.

The Notes column also flags anything found via search-fallback in a table
that's *already* in `table_matrix.csv` — that's a stronger signal than an
unknown-table hit, since it usually means that table's `HostField` is
wrong for this particular client, not that the table is unrelated to host
identity. Worth checking `table_matrix.csv` first when that happens.

### Other sources consulted

- [a6n.co.uk — Finding servers that have gone silent with Heartbeat monitoring](https://www.a6n.co.uk/2026/03/sentinel-finding-servers-that-have-gone.html) — an existing solution to the same underlying problem (silent host detection), confirms hostname-format inconsistency is a known, previously-solved issue, not something unique to this project.
- [Common tasks with KQL for Microsoft Sentinel](https://learn.microsoft.com/en-us/kusto/query/tutorials/common-tasks-microsoft-sentinel) — general reference for the `search`/`summarize` KQL patterns used throughout the tool.
- [column_ifexists() function](https://learn.microsoft.com/en-us/kusto/query/column-ifexists-function) — a native KQL graceful-fallback mechanism, considered as a possible future improvement for handling schema drift inline within a query itself.

## Troubleshooting

- **"Azure CLI is not logged in"** → run `az login`, then re-run the tool.
- **"Missing input/environments.csv" / "Missing input/hostlist.csv"** →
  you haven't copied the template yet, see Setup above.
- **"Missing input/table_matrix.csv"** → this file should have come with
  the repo — restore it from git rather than recreating it by hand.
- For anything deeper — handing the problem to an AI assistant on whatever
  machine you're debugging on — give it `AI_README.md`. It documents the
  internals (script flow, file formats, known rough edges) specifically for
  that purpose.

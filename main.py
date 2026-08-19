#!/usr/bin/env python3
"""Sentinel onboarding host validation tool — see README.md."""

import os
import sys
import subprocess
import venv
import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
MIN_PYTHON = (3, 9)


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _in_target_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV_DIR.resolve()


def _requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def bootstrap() -> None:
    """Self-heals the run environment so this script works the same way regardless
    of whether it's run from a persistent machine or a wiped-clean session."""
    if sys.version_info < MIN_PYTHON:
        sys.exit(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required, found "
            f"{sys.version_info.major}.{sys.version_info.minor}. "
            "See README.md 'Prerequisites' for install instructions."
        )

    if _in_target_venv():
        return

    if not _venv_python().exists():
        print("First run: creating local virtual environment (.venv)...")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)

    marker = VENV_DIR / ".requirements_hash"
    current_hash = _requirements_hash()
    if not marker.exists() or marker.read_text().strip() != current_hash:
        print("Installing/updating dependencies into .venv...")
        subprocess.run(
            [str(_venv_python()), "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS)],
            check=True,
        )
        marker.write_text(current_hash)

    # Re-launch inside the managed venv so every import below resolves against
    # packages we control, regardless of what's on the system Python's path.
    python = str(_venv_python())
    os.execv(python, [python, str(Path(__file__).resolve())] + sys.argv[1:])


bootstrap()

# --- everything below only ever runs inside .venv ---

import argparse
import csv
import json
import logging
import re
import traceback
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Optional

from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential
from azure.monitor.query import LogsQueryClient

DEFAULT_LOOKBACK_DAYS = 7

# Seed hypothesis from Sentinel/Azure Monitor docs research — not yet cross-checked
# against a live workspace. Validate/correct against real schema before trusting fully.
TABLE_HOST_FIELDS = {
    "Heartbeat": "Computer",
    "Perf": "Computer",
    "InsightsMetrics": "Computer",
    "Update": "Computer",
    "ConfigurationData": "Computer",
    "SecurityEvent": "Computer",
    "WindowsEvent": "Computer",
    "Event": "Computer",
    "W3CIISLog": "Computer",
    "Syslog": "Computer",
    "IdentityLogonEvents": "DeviceName",
    "IdentityDirectoryEvents": "DeviceName",
    "IdentityQueryEvents": "DeviceName",
    "DeviceInfo": "DeviceName",
    "DeviceNetworkInfo": "DeviceName",
    "DeviceProcessEvents": "DeviceName",
    "DeviceLogonEvents": "DeviceName",
    "DeviceFileEvents": "DeviceName",
    "DeviceRegistryEvents": "DeviceName",
    "DeviceImageLoadEvents": "DeviceName",
    "DeviceEvents": "DeviceName",
    "CommonSecurityLog": "DeviceName",
}

EXTRA_TABLE_COLUMNS = {
    "Heartbeat": ["OSType", "OSMajorVersion", "OSMinorVersion", "ComputerEnvironment"],
}

OUTPUT_FIELDS = [
    "Hostname", "ShortHostname", "ValidationStatus", "TablesFound",
    "LastLog", "FirstLog", "RecordCount", "ObservedOSType", "ObservedOSVersion",
    "ExpectedOSFamily", "ClientNote", "Notes",
]

# Hostnames drive raw KQL string interpolation below, so anything outside this
# charset is dropped rather than risking malformed/injected query syntax.
HOSTNAME_SAFE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def setup_console_logging(debug: bool) -> logging.Logger:
    logger = logging.getLogger("host_validation")
    logger.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)
    return logger


def attach_file_logging(logger: logging.Logger, log_path: Path) -> None:
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)


def run_az(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["az", *args], capture_output=True, text=True)


def check_az_login(logger: logging.Logger) -> dict:
    result = run_az("account", "show", "--output", "json")
    if result.returncode != 0:
        logger.error("Azure CLI is not logged in (or 'az' isn't installed).")
        logger.error("Run 'az login' first, then re-run this tool.")
        sys.exit(1)
    return json.loads(result.stdout)


def load_environments(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(
            f"Missing {path}. Copy input/environments_template.csv to "
            f"input/environments.csv and fill in your client details."
        )
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    required = {"workspace_name", "organization", "resource_group",
                "tenant_id", "subscription_id", "workspace_id"}
    for row in rows:
        missing = required - row.keys()
        if missing:
            sys.exit(f"{path} is missing columns: {sorted(missing)}")
    return rows


def pick_environment(envs: list[dict], logger: logging.Logger) -> dict:
    logger.info("Client environments found in input/environments.csv:")
    for i, env in enumerate(envs, start=1):
        logger.info(f"  [{i}] {env['organization']} — {env['workspace_name']}")
    while True:
        choice = input(f"Pick a client [1-{len(envs)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(envs):
            return envs[int(choice) - 1]
        print("Invalid choice, try again.")


def verify_subscription_context(env: dict, logger: logging.Logger) -> None:
    active = check_az_login(logger)
    expected_sub = env["subscription_id"]

    if active.get("id") != expected_sub:
        logger.info(f"Switching to expected subscription for {env['organization']}...")
        result = run_az("account", "set", "--subscription", expected_sub)
        if result.returncode != 0:
            logger.error(f"Could not switch subscription: {result.stderr.strip()}")
            sys.exit(1)
        active = check_az_login(logger)

    logger.info("")
    logger.info(f"  Expected : {env['organization']} — {expected_sub}")
    logger.info(f"  Active   : {active.get('name')} — {active.get('id')}")
    logger.info("")
    if active.get("id") != expected_sub:
        logger.error("Active subscription still doesn't match the expected client. Aborting.")
        sys.exit(1)

    confirm = input("Confirm this is the correct client before querying [y/N]: ").strip().lower()
    if confirm != "y":
        sys.exit("Aborted by user.")


def load_hostlist(path: Path, logger: logging.Logger) -> tuple[list[str], list[dict]]:
    if not path.exists():
        sys.exit(
            f"Missing {path}. Copy input/hostlist_template.csv to "
            f"input/hostlist.csv and fill in this engagement's hosts."
        )
    domains: list[str] = []
    hosts: list[dict] = []
    header: Optional[list[str]] = None

    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip():
                continue
            first = row[0].strip()
            if first.startswith("#"):
                continue
            if first.upper() == "DOMAIN":
                if len(row) > 1 and row[1].strip():
                    domains.append(row[1].strip().lower())
                continue
            if first.lower() == "hostname":
                header = [c.strip() for c in row]
                continue
            if header is None:
                logger.warning(f"Skipping line before Hostname header: {row}")
                continue
            values = row + [""] * (len(header) - len(row))
            hosts.append(dict(zip(header, values)))

    if header is None:
        sys.exit(f"{path} has no 'Hostname' header row — check it against the template.")
    return domains, hosts


def normalize_short_name(hostname: str) -> Optional[str]:
    short = hostname.strip().split(".")[0].lower()
    if not short or not HOSTNAME_SAFE_RE.match(short):
        return None
    return short


def build_seed_query(table: str, field: str, short_names: list[str], lookback_days: int) -> str:
    name_list = ", ".join(f'"{n}"' for n in short_names)
    extra = EXTRA_TABLE_COLUMNS.get(table, [])
    extra_agg = "".join(f", {c}=take_any({c})" for c in extra)
    return (
        f"{table}\n"
        f"| where TimeGenerated > ago({lookback_days}d)\n"
        f'| extend ShortComputer = tolower(tostring(split({field}, ".")[0]))\n'
        f"| where ShortComputer in ({name_list})\n"
        f"| summarize LastLog=max(TimeGenerated), FirstLog=min(TimeGenerated), "
        f"Count=count(){extra_agg} by ShortComputer"
    )


def build_search_query(hostname: str, lookback_days: int) -> str:
    escaped = hostname.replace('"', "")
    return f'search "{escaped}"\n| where TimeGenerated > ago({lookback_days}d)\n| distinct $table'


def run_seed_pass(client, workspace_id, short_names, lookback_days, logger) -> dict:
    results = {name: {"tables": {}, "found": False} for name in short_names}
    if not short_names:
        return results
    timespan = timedelta(days=lookback_days)

    for table, field in TABLE_HOST_FIELDS.items():
        query = build_seed_query(table, field, short_names, lookback_days)
        logger.debug(f"Querying {table}...")
        try:
            response = client.query_workspace(workspace_id, query, timespan=timespan)
        except AzureError as exc:
            logger.debug(f"  {table}: query failed, skipping ({exc.message.splitlines()[0]})")
            continue

        for table_result in response.tables:
            columns = table_result.columns
            for row in table_result.rows:
                record = dict(zip(columns, row))
                short = record.get("ShortComputer")
                if short not in results:
                    continue
                results[short]["found"] = True
                results[short]["tables"][table] = record

    return results


def run_search_fallback(client, workspace_id, missing_short_names, hostname_by_short, lookback_days, logger) -> dict:
    extra_tables: dict[str, set] = {name: set() for name in missing_short_names}
    timespan = timedelta(days=lookback_days)

    for short in missing_short_names:
        original = hostname_by_short[short]
        query = build_search_query(original, lookback_days)
        try:
            response = client.query_workspace(workspace_id, query, timespan=timespan)
        except AzureError as exc:
            logger.debug(f"search fallback failed for {original}: {exc.message.splitlines()[0]}")
            continue
        for table_result in response.tables:
            for row in table_result.rows:
                extra_tables[short].add(str(row[0]))

    return extra_tables


def build_output_rows(hosts, seed_results, search_results) -> list[dict]:
    rows = []
    for host in hosts:
        raw_hostname = host.get("Hostname", "").strip()
        short = normalize_short_name(raw_hostname)
        row = {
            "Hostname": raw_hostname,
            "ShortHostname": short or "",
            "ExpectedOSFamily": host.get("ExpectedOSFamily", ""),
            "ClientNote": host.get("ClientNote", ""),
            "ValidationStatus": False,
            "TablesFound": "",
            "LastLog": "",
            "FirstLog": "",
            "RecordCount": "",
            "ObservedOSType": "",
            "ObservedOSVersion": "",
            "Notes": "",
        }
        if short is None:
            row["Notes"] = "Hostname failed normalization/safety check — skipped"
            rows.append(row)
            continue

        seed = seed_results.get(short, {"found": False, "tables": {}})
        tables_found = dict(seed["tables"])
        via_search_only = list(search_results.get(short, [])) if not seed["found"] else []

        if tables_found or via_search_only:
            row["ValidationStatus"] = True

        row["TablesFound"] = ", ".join(sorted(set(tables_found.keys()) | set(via_search_only)))

        if tables_found:
            last_logs = [r["LastLog"] for r in tables_found.values() if r.get("LastLog")]
            first_logs = [r["FirstLog"] for r in tables_found.values() if r.get("FirstLog")]
            counts = [r.get("Count", 0) for r in tables_found.values()]
            row["LastLog"] = max(last_logs) if last_logs else ""
            row["FirstLog"] = min(first_logs) if first_logs else ""
            row["RecordCount"] = sum(counts) if counts else ""
            heartbeat = tables_found.get("Heartbeat")
            if heartbeat:
                row["ObservedOSType"] = heartbeat.get("OSType", "")
                major = heartbeat.get("OSMajorVersion", "")
                minor = heartbeat.get("OSMinorVersion", "")
                row["ObservedOSVersion"] = f"{major}.{minor}".strip(".")
        elif via_search_only:
            row["Notes"] = "Found via search fallback only — not in the known seed-table matrix, worth a manual look"

        rows.append(row)
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def zip_run_folder(run_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in run_dir.rglob("*"):
            zf.write(file, arcname=file.relative_to(run_dir.parent))


def sanitize_for_path(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "client"


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel onboarding host validation tool")
    parser.add_argument("--debug", action="store_true", help="verbose console output")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--environments", type=Path, default=PROJECT_ROOT / "input" / "environments.csv")
    parser.add_argument("--hostlist", type=Path, default=PROJECT_ROOT / "input" / "hostlist.csv")
    args = parser.parse_args()

    logger = setup_console_logging(args.debug)

    envs = load_environments(args.environments)
    env = pick_environment(envs, logger)
    verify_subscription_context(env, logger)

    domains, hosts = load_hostlist(args.hostlist, logger)
    if domains:
        logger.info(f"Domains declared for this environment: {', '.join(domains)}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_name = f"{sanitize_for_path(env['organization'])}_{timestamp}"
    run_dir = PROJECT_ROOT / "output" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    attach_file_logging(logger, run_dir / "run.log")

    logger.info(f"Starting validation for {env['organization']} — {len(hosts)} hosts")

    # Everything below can fail on things outside our control (auth, network,
    # a permissions gap). Log full detail to the run's log file always, but
    # keep the console message short and point at that file instead of a raw
    # traceback — the whole point of per-run logging is to make that safe to do.
    try:
        credential = AzureCliCredential()
        client = LogsQueryClient(credential)

        short_names: list[str] = []
        hostname_by_short: dict[str, str] = {}
        for host in hosts:
            raw = host.get("Hostname", "").strip()
            short = normalize_short_name(raw)
            if short:
                short_names.append(short)
                hostname_by_short[short] = raw
            else:
                logger.warning(f"Skipping unsafe/empty hostname: {raw!r}")
        short_names = sorted(set(short_names))

        logger.info(f"Seed-list pass across {len(TABLE_HOST_FIELDS)} known tables...")
        seed_results = run_seed_pass(client, env["workspace_id"], short_names, args.lookback_days, logger)

        missing = [n for n, r in seed_results.items() if not r["found"]]
        search_results: dict = {}
        if missing:
            logger.info(f"{len(missing)} host(s) not found in seed tables — running search fallback...")
            search_results = run_search_fallback(
                client, env["workspace_id"], missing, hostname_by_short, args.lookback_days, logger
            )

        rows = build_output_rows(hosts, seed_results, search_results)

        csv_path = run_dir / "validation_results.csv"
        write_csv(rows, csv_path)

        zip_path = PROJECT_ROOT / "output" / f"{run_name}.zip"
        zip_run_folder(run_dir, zip_path)
    except Exception:
        logger.debug("Unhandled exception:\n" + traceback.format_exc())
        logger.error("")
        logger.error(f"Run failed unexpectedly. Full details in: {run_dir / 'run.log'}")
        sys.exit(1)

    validated = sum(1 for r in rows if r["ValidationStatus"])
    logger.info("")
    logger.info(f"Done: {validated}/{len(rows)} hosts validated.")
    logger.info(f"Results: {csv_path}")
    logger.info(f"Zip:     {zip_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAborted.")

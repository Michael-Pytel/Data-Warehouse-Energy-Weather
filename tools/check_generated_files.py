"""
List the files in the project that are not part of the repository (generated output, local files)
and say where each one comes from.

    python tools/check_generated_files.py                  # all generated / local files
    python tools/check_generated_files.py --since 2h       # only files written in the last 2 hours
                                                           # (e.g. right after an SSIS run)
    python tools/check_generated_files.py --delete-obsolete --yes   # delete leftovers of retired code

Every file is matched against the rules below:
    expected  - output of the current ETL scripts (run logs, Eurostat exports)
    obsolete  - leftovers of retired or changed code; safe to delete
    local     - local settings, notes, IDE/build state and the Power BI report (never deleted)
    UNKNOWN   - no rule matches: a script writes something new. Add a rule here or change the script.
Exit code 1 when UNKNOWN files are found.
"""

import argparse
import fnmatch
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (pattern relative to the project root, category, where it comes from); first match wins
RULES = [
    ("etl/log/*.log", "expected", "run log of an ETL script (etl_log.start_run_log), last 30 per script kept"),
    ("etl/eurostat_export/eurostat_raw_population.csv", "expected", "eurostat_client.py: raw data per indicator"),
    ("etl/eurostat_export/eurostat_raw_gdp_per_capita.csv", "expected", "eurostat_client.py: raw data per indicator"),
    ("etl/eurostat_export/eurostat_raw_electricity_prices.csv", "expected", "eurostat_client.py: raw data per indicator"),
    ("etl/eurostat_export/eurostat_raw_energy_intensity.csv", "expected", "eurostat_client.py: raw data per indicator"),
    ("etl/eurostat_export/eurostat_raw_unemployment_rate.csv", "expected", "eurostat_client.py: raw data per indicator"),
    ("etl/eurostat_export/eurostat_integrated.csv", "expected", "eurostat_client.py: integrated table (backlog 3.1)"),
    ("etl/eurostat_extraction_summary.json", "expected", "eurostat_client.py: extraction summary"),

    ("etl/eurostat_export/eurostat_raw_*.csv", "obsolete", "Eurostat indicator dropped in step 2.1"),
    ("etl/staging_*_eu_*.csv", "obsolete", "timestamped CSV backups of the old weather/climate script"),
    ("etl/staging_climate_data*.csv", "obsolete", "climate API data (no longer used)"),
    ("etl/staging_entso_*.csv", "obsolete", "CSV backups of the old entsoe_client.py (removed in 2.4)"),
    ("etl/staging_weather_data.csv", "obsolete", "CSV backup of open_meteo_client.py (removed in 2.4)"),
    ("etl/*.pkl", "obsolete", "intermediate results of the retired fact_processor.py"),
    ("etl/failed_xml_parse.xml", "obsolete", "debug dump of the old ENTSO-E parser (removed in 2.4)"),
    ("etl/*.log", "obsolete", "log of a retired script, or written outside etl/log/"),

    (".env", "local", "local environment settings (git-ignored)"),
    ("CLAUDE.md", "local", "local working notes (git-ignored)"),
    ("docs/PLAN.md", "local", "local project plan (git-ignored)"),
    ("powerbi/*.pbix", "local", "Power BI report (git-ignored, too large for GitHub)"),
    ("powerbi/*", "local", "Power BI working files"),
    (".vs/*", "local", "Visual Studio state"),
    ("*/.vs/*", "local", "Visual Studio state"),
    ("ssis/bin/*", "local", "SSIS build output"),
    ("ssis/obj/*", "local", "SSIS build output"),
    ("ssis/*.user", "local", "Visual Studio user settings"),
    ("*/__pycache__/*", "local", "Python bytecode cache"),
    ("*.lnk", "local", "Windows shortcut"),
]
SKIP_DIRS = {".git"}


def tracked_files() -> set:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return {p for p in out.decode("utf-8").split("\0") if p}


def classify(rel: str):
    for pattern, category, source in RULES:
        if fnmatch.fnmatch(rel, pattern):
            return category, source
    return "UNKNOWN", "no rule matches - which script writes this?"


def parse_since(text: str) -> datetime:
    units = {"m": "minutes", "h": "hours", "d": "days"}
    if text[-1] in units and text[:-1].isdigit():
        return datetime.now() - timedelta(**{units[text[-1]]: int(text[:-1])})
    return datetime.fromisoformat(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="only files written since then: 30m, 2h, 1d or an ISO date/time")
    parser.add_argument("--delete-obsolete", action="store_true", help="delete the files marked obsolete")
    parser.add_argument("--yes", action="store_true", help="do not ask before deleting")
    args = parser.parse_args()
    since = parse_since(args.since) if args.since else None

    tracked = tracked_files()
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in tracked:
            continue
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime)
        if since and modified < since:
            continue
        found.append((rel, *classify(rel), stat.st_size, modified))

    order = {"UNKNOWN": 0, "obsolete": 1, "expected": 2, "local": 3}
    found.sort(key=lambda f: (order[f[1]], f[0]))
    title = f"Files not in the repository{f' written since {since:%Y-%m-%d %H:%M}' if since else ''}"
    print(f"{title} ({ROOT})\n")
    for category in order:
        group = [f for f in found if f[1] == category]
        if not group:
            continue
        if category == "local":
            # IDE / cache files can be many: summarise per source
            print(f"local ({len(group)} files, {sum(f[3] for f in group) / 1e6:.1f} MB)")
            for source in sorted({f[2] for f in group}):
                files = [f for f in group if f[2] == source]
                print(f"  {len(files):>5} files  {sum(f[3] for f in files) / 1e6:8.1f} MB  {source}")
            print()
            continue
        print(f"{category} ({len(group)} files, {sum(f[3] for f in group) / 1e6:.1f} MB)")
        width = max(len(f[0]) for f in group)
        for rel, _, source, size, modified in group:
            print(f"  {rel:{width}} {size / 1e3:7.0f} kB  {modified:%Y-%m-%d %H:%M}  {source}")
        print()

    unknown = [f for f in found if f[1] == "UNKNOWN"]
    obsolete = [f for f in found if f[1] == "obsolete"]
    if args.delete_obsolete and obsolete:
        if not args.yes:
            answer = input(f"Delete {len(obsolete)} obsolete files? [y/N] ")
            if answer.strip().lower() != "y":
                print("Nothing deleted.")
                return 1 if unknown else 0
        for rel, *_ in obsolete:
            (ROOT / rel).unlink()
        print(f"Deleted {len(obsolete)} obsolete files.")
    elif obsolete:
        print(f"{len(obsolete)} obsolete files can be deleted with --delete-obsolete.")
    if unknown:
        print(f"{len(unknown)} UNKNOWN files: add a rule in tools/check_generated_files.py or change the script that writes them.")
    return 1 if unknown else 0


if __name__ == "__main__":
    sys.exit(main())

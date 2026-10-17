"""PV forecast, internally built (our own models): every command in one place (see README.md).

    python run.py hourly [--dry-run] [--no-wait]       forecast the current hour (and fill missed hours), all families
    python run.py weather [FIRST [LAST]] [--models M]  store new weather forecast runs (backfill with UTC days)
    python run.py status                               active models, their age, saved candidates
    python run.py train FAMILY [--end DAY]             fit on all data up to DAY (default today): a CANDIDATE
    python run.py compare FAMILY [--days 14]           does retraining help? report + REPLACE / KEEP suggestion
    python run.py promote FAMILY VERSION               make a saved version active (the hourly job uses it)
    python run.py rollback FAMILY                      go back to the version active before
    python run.py backfill FAMILY START END [--write]  walk-forward replay of past local days: CSV, then --write
FAMILY is h1 (next hour, sources 100-103) or h48 (next 48 hours, sources 110-112).
Exit code 0 = done, 1 = something failed (see logs/run_<YYYY-MM>.log).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

import pandas as pd

from pv_forecast import jobs, registry, compare
from pv_forecast.families import FAMILIES
from pv_forecast.log import log


def utc_day(text: str) -> pd.Timestamp:
    return pd.Timestamp(date.fromisoformat(text), tz="UTC")


def status() -> int:
    now = pd.Timestamp.now(tz="UTC")
    for name in FAMILIES:
        act = registry.active(name)
        print(f"{name}: active {act or 'NONE'}")
        for v in registry.versions(name):
            m = registry.meta(name, v)
            end = pd.Timestamp(m["trained_to_utc"])
            days = ", ".join(f"{f} {c['days']} days" for f, c in m.get("data_check", {}).items())
            print(f"  {'*' if v == act else ' '} {v}  data before {compare.local_day(end)} local "
                  f"({(now - end) / pd.Timedelta(days=1):.1f} days ago){'; ' + days if days else ''}, "
                  f"code {m['code_version']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("hourly")
    p.add_argument("--dry-run", action="store_true", help="compute and save a CSV in reports/, write nothing")
    p.add_argument("--no-wait", action="store_true", help="do not wait for late plant data")
    p = sub.add_parser("weather")
    p.add_argument("first", nargs="?", type=utc_day, help="backfill: first UTC day")
    p.add_argument("last", nargs="?", type=utc_day, help="backfill: last UTC day, inclusive")
    p.add_argument("--models", nargs="+", choices=["ecmwf_ifs", "icon_eu"])
    sub.add_parser("status")
    p = sub.add_parser("train")
    p.add_argument("family", choices=FAMILIES)
    p.add_argument("--end", help="first local day NOT used (default today)")
    p = sub.add_parser("compare")
    p.add_argument("family", choices=FAMILIES)
    p.add_argument("--days", type=int, default=14, help="test window length (default 14)")
    p = sub.add_parser("promote")
    p.add_argument("family", choices=FAMILIES)
    p.add_argument("version")
    p = sub.add_parser("rollback")
    p.add_argument("family", choices=FAMILIES)
    p = sub.add_parser("backfill")
    p.add_argument("family", choices=FAMILIES)
    p.add_argument("start", help="first local day")
    p.add_argument("end", help="first local day NOT included")
    p.add_argument("--write", action="store_true", help="write the CSV of the earlier dry run")
    a = ap.parse_args()

    if a.cmd == "hourly":
        return jobs.hourly(a.dry_run, a.no_wait)
    if a.cmd == "weather":
        from pv_forecast import weather_collector
        if a.last is not None and a.last < a.first:
            raise SystemExit("weather: last day is before first day")
        return weather_collector.collect(a.first, a.last, a.models)
    if a.cmd == "status":
        return status()
    fam = FAMILIES[a.family]
    if a.cmd == "train":
        jobs.train(fam, a.end)
    elif a.cmd == "compare":
        compare.run(fam, a.days)
    elif a.cmd == "promote":
        registry.promote(fam.NAME, a.version)
        log(f"{fam.NAME}: {a.version} is now active")
    elif a.cmd == "rollback":
        log(f"{fam.NAME}: rolled back, {registry.rollback(fam.NAME)} is now active")
    elif a.cmd == "backfill":
        if a.write:
            jobs.backfill_write(fam, a.start, a.end)
        else:
            jobs.backfill(fam, a.start, a.end)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as exc:
        if isinstance(exc.code, str):
            log(f"STOPPED: {exc.code}", "ERROR")
            sys.exit(1)
        raise
    except Exception as exc:                                    # noqa: BLE001 - logged with its cause
        import traceback
        log(f"CRASH {type(exc).__name__}: {exc} | {traceback.format_exc()[-2000:]}", "ERROR")
        sys.exit(1)

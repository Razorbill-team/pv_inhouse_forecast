"""The commands of run.py: hourly forecast, train, backfill (compare is in compare.py).

hourly    every family with an active model (h48 first, then h1): forecast every issue hour from the one after the
          newest written up to the current hour (at most CATCHUP_MAX_H; so missed hours fill themselves), PPA and
          FREE, one write per family. A 1-hour source without a value for a daylight hour (plant data missing) gets
          the 48-hour forecast of that hour (H1_FALLBACK_IDS), logged as FALLBACK. Never trains. Warns when the
          active model is older than MODEL_MAX_AGE_DAYS.
train     fit a family on all data up to a local midnight and save it as a CANDIDATE (not used until promoted).
backfill  walk-forward replay over past days, models refitted every RETRAIN_DAYS (from BLOCK_ANCHOR) on the data
          before each block only: a CSV of rows to check, then --write. For a new or empty table.
"""

from __future__ import annotations

import time
import traceback

import pandas as pd
import sqlalchemy as sa

from . import compare, db, inputs, registry, writer
from . import settings as S
from .families import FAMILIES
from .log import PROBLEMS, log

HOUR = pd.Timedelta(hours=1)


def utc(day) -> pd.Timestamp:
    """Local midnight of a day (text or Timestamp) in UTC."""
    return pd.Timestamp(day).normalize().tz_localize(S.LOCAL_TZ).tz_convert("UTC")


def today_utc() -> pd.Timestamp:
    """Today's local midnight in UTC."""
    return pd.Timestamp.now(tz=S.LOCAL_TZ).normalize().tz_convert("UTC")


# ------------------------------------------------------------------ hourly

def _issues(fam, write_eng, hour: pd.Timestamp, trained_to: pd.Timestamp) -> pd.DatetimeIndex:
    """Issue hours to make now: after the newest written one, at most CATCHUP_MAX_H back, not before the model's
    training end (that would score it on its own training data)."""
    ids = [i for _, i in fam.SOURCES.values()]
    try:
        last = writer.newest_issue(write_eng, ids)
    except Exception as exc:                                # noqa: BLE001 - say plainly what failed
        raise RuntimeError(f"cannot read the forecast table: {db.describe(write_eng, exc)}") from None
    first = hour - (S.CATCHUP_MAX_H - 1) * HOUR
    if last is not None and last + HOUR < first:
        log(f"{fam.NAME}: newest written issue {last:%Y-%m-%d %H}Z is more than {S.CATCHUP_MAX_H} h ago; "
            f"hours before {first:%Y-%m-%d %H}Z stay empty: fill them with run.py backfill", "WARN")
    if last is not None:
        first = max(first, last + HOUR)
    if trained_to > hour:
        raise ValueError(f"active model trained on data up to {trained_to}, after the current hour; promote another")
    if first < trained_to:
        start = trained_to.ceil("h")
        log(f"{fam.NAME}: issue hours {first:%m-%d %H}Z .. {start - HOUR:%m-%d %H}Z "
            f"({int((start - first) / HOUR)}) are before the active model's training end: left empty, "
            "fill them with run.py backfill", "WARN")
        first = start
    return pd.date_range(first, hour, freq="h")


def hourly(dry_run: bool = False, no_wait: bool = False) -> int:
    """Exit code 0 = every family written or up to date; 1 = a family failed (see the log)."""
    now = pd.Timestamp.now(tz="UTC")
    hour = now.floor("h")
    log(f"hourly run for {hour:%Y-%m-%d %H:%M}Z ({hour.tz_convert(S.LOCAL_TZ):%H:%M} local)"
        + (" DRY RUN" if dry_run else "") + f"; code {registry.code_version()}")
    read_eng = db.engine(S.READ_DB_ENV)
    write_eng = db.write_engine(S.WRITE_DB_ENV, S.FORECAST_TABLE_ENV)
    waited = False
    failed = []
    for fam in (FAMILIES["h48"], FAMILIES["h1"]):          # h48 first: h1 falls back on it
        try:
            version = registry.active(fam.NAME)
            if version is None:
                log(f"{fam.NAME}: no active model (run.py train, then run.py promote)", "ERROR")
                failed.append(fam.NAME)
                continue
            fitted, meta = registry.load(fam, version)
            trained_to = pd.Timestamp(meta["trained_to_utc"])
            age = (now - trained_to) / pd.Timedelta(days=1)
            if age > S.MODEL_MAX_AGE_DAYS:
                log(f"{fam.NAME}: active model {version} was trained on data up to {age:.0f} days ago; "
                    "run.py compare to see if retraining helps", "WARN")
            issues = _issues(fam, write_eng, hour, trained_to)
            if issues.empty:
                log(f"{fam.NAME}: up to date")
                continue
            if hour in issues and not (no_wait or waited):  # plant data up to the hour start, once per run
                deadline = time.monotonic() + 60 * S.DATA_WAIT_MIN
                for farm in S.FARMS:
                    if inputs.sun_up(farm, hour):
                        late = inputs.wait_for_data(read_eng, farm, hour, deadline)
                        if late:
                            log(f"{farm.name}: SACU {late} have not reported up to {hour:%H:%M}Z after the wait; "
                                "going on with what is there", "WARN")
                waited = True
            log(f"{fam.NAME}: model {version}; issues {issues[0]:%m-%d %H}Z .. {issues[-1]:%m-%d %H}Z ({len(issues)})")
            data = fam.read(read_eng, issues[0] - fam.HISTORY, issues[-1] + HOUR)
            detail = fam.predict(data, fitted, issues)
            stamp = pd.Timestamp.now(tz="UTC")
            if fam.NAME == "h1":
                detail = fallback_h1(write_eng, detail, fam.SOURCES, hour, stamp)
            rows = writer.rows(detail, fam.SOURCES, hour, stamp)
            _log_inputs(fam, data, detail, hour)
            if dry_run:
                S.REPORT_DIR.mkdir(exist_ok=True)
                path = S.REPORT_DIR / f"dry_run_{fam.NAME}_{hour:%Y%m%dT%H}Z.csv"
                rows.to_csv(path, index=False)
                log(f"{fam.NAME}: DRY RUN, {len(rows)} rows to {path}")
            elif writer.write(write_eng, rows) is None:
                failed.append(fam.NAME)
        except (Exception, SystemExit) as exc:              # one family failing must not stop the other
            log(f"{fam.NAME}: FAILED {type(exc).__name__}: {exc} | {traceback.format_exc()[-1500:]}", "ERROR")
            failed.append(fam.NAME)
    log(f"done; failed: {failed or 'none'}; warnings and errors: {len(PROBLEMS)}")
    return 1 if failed else 0


FALLBACK_SQL = """
select farmid, id_source, slot_ts, last_forecast_datetime as run, forecasted_power_mw as mw from {table}
where farmid = any(:farms) and id_source = any(:ids) and slot_ts >= :t0 and slot_ts < :t1
  and last_forecast_datetime <= :t_max
"""


def fallback_h1(eng, detail: pd.DataFrame, sources: dict, live_issue: pd.Timestamp,
                now: pd.Timestamp) -> pd.DataFrame:
    """Fill each 1-hour source that has no value for an issue hour (all 4 quarters missing; night is 0, never
    missing) with the 48-hour forecast of the same slots: per id of H1_FALLBACK_IDS in turn, the newest run in the
    table issued no later than the 1-hour row would be stamped, used only if it has all 4 quarters. Every fill is
    logged as FALLBACK (WARN); rows keep the source's own id and name."""
    farms = {f.farmid: f.name for f in S.FARMS}
    gaps = [(farm, issue, col) for col in sources for (farm, issue), v in detail.groupby(["farm", "issue_utc"])[col]
            if v.isna().all()]
    if not gaps:
        return detail
    stamp = {i: (min(now, i + pd.Timedelta(minutes=59)) if i == live_issue
                 else i + pd.Timedelta(minutes=S.ISSUE_DELAY_MIN)) for i in {g[1] for g in gaps}}
    with eng.connect() as c:
        t = pd.DataFrame(c.execute(sa.text(FALLBACK_SQL.format(table=db.table(S.FORECAST_TABLE_ENV))), {
            "farms": list(farms), "ids": list(S.H1_FALLBACK_IDS), "t0": min(stamp).floor("h"),
            "t1": max(stamp).floor("h") + HOUR, "t_max": max(stamp)}).fetchall(),
            columns=["farmid", "id_source", "slot_ts", "run", "mw"])
    for col in ("slot_ts", "run"):
        t[col] = pd.to_datetime(t[col], utc=True)
    t["farm"], t["mw"] = t["farmid"].map(farms), t["mw"].astype(float)
    detail = detail.copy()
    for farm, issue, col in gaps:
        slots = pd.date_range(issue, issue + HOUR, freq="15min", inclusive="left")
        for id_source in S.H1_FALLBACK_IDS:
            c = t[(t["farm"] == farm) & (t["id_source"] == id_source) & t["slot_ts"].isin(slots)
                  & (t["run"] <= stamp[issue])]
            c = c.sort_values("run").drop_duplicates("slot_ts", keep="last").set_index("slot_ts")
            if len(c) == 4:
                m = (detail["farm"] == farm) & (detail["issue_utc"] == issue)
                detail.loc[m, col] = c["mw"].reindex(detail.loc[m, "slot_utc"]).to_numpy()
                log(f"FALLBACK {farm} {issue:%m-%d %H}Z: {sources[col][1]} <- {id_source} (run "
                    f"{c['run'].max():%m-%d %H:%M}Z); no 1-hour value (plant data or weather missing)", "WARN")
                break
        else:
            log(f"{farm} {issue:%m-%d %H}Z: {sources[col][1]} has no value and no 48-hour fallback "
                f"{S.H1_FALLBACK_IDS} in the table: no rows", "WARN")
    return detail


def _log_inputs(fam, data: dict, detail: pd.DataFrame, hour: pd.Timestamp) -> None:
    """Per farm: input counts (plant and weather quality), the current issue's mean MW per source; WARN for issues
    with no rows from a source, or weather runs older than WEATHER_OLD_H at the issue."""
    for farm, g in detail.groupby("farm"):
        log(f"{fam.NAME} {farm}: inputs {data[farm]['counts']}")
        now = g[g["issue_utc"] == hour]
        if len(now):
            log(f"{fam.NAME} {farm} {hour:%H}Z: " + ", ".join(
                f"{i} {now[c].mean():.2f}" if now[c].notna().any() else f"{i} -"
                for c, (_, i) in fam.SOURCES.items()) + f" MW (mean over its {len(now)} slots)")
        for c, (name, i) in fam.SOURCES.items():
            none = g.groupby("issue_utc")[c].apply(lambda v: v.isna().all())
            if none.any():
                log(f"{fam.NAME} {farm}: no rows from {i} {name} for {int(none.sum())} of {len(none)} issues "
                    f"(newest {none[none].index.max():%m-%d %H}Z): missing plant data or weather", "WARN")
        for col in ("weather_run_utc", "icon_run_utc"):
            age = (g["issue_utc"] - g[col]).max() / pd.Timedelta(hours=1)
            if not age <= S.WEATHER_OLD_H:
                log(f"{fam.NAME} {farm}: {col} up to {age:.0f} h older than the issue, or missing "
                    "(is run.py weather running?)", "WARN")


# ------------------------------------------------------------------ train

def data_check(data: dict, end: pd.Timestamp) -> tuple[dict, list[str]]:
    """Per farm, how much plant data training has before `end`: days with data (>= half the day's slots present),
    of them in the last 7 days, first and last such day, share of missing slots. Returns (summary, problems):
    a problem is fewer than MIN_TRAIN_DAYS days, or fewer than MIN_RECENT_DAYS in the last 7."""
    summary, problems = {}, []
    for farm, d in data.items():
        cf = d["cf"][d["cf"].index < end]
        cf = cf[cf.first_valid_index():] if cf.notna().any() else cf
        per_day = cf.notna().groupby(cf.index.tz_convert(S.LOCAL_TZ).date).sum()
        days = per_day[per_day >= 48].index
        recent = [x for x in days if x >= (end - pd.Timedelta(days=7)).tz_convert(S.LOCAL_TZ).date()]
        summary[farm] = {"days": len(days), "first": str(min(days, default="-")), "last": str(max(days, default="-")),
                         "last_7_days": len(recent), "missing_pct": round(100 * float(cf.isna().mean()), 1)}
        log(f"{farm}: {len(days)} days with data ({summary[farm]['first']} .. {summary[farm]['last']}), "
            f"{len(recent)}/7 last days, missing slots {summary[farm]['missing_pct']} %")
        if len(days) < S.MIN_TRAIN_DAYS:
            problems.append(f"{farm} has {len(days)} days of data (need {S.MIN_TRAIN_DAYS})")
        if len(recent) < S.MIN_RECENT_DAYS:
            problems.append(f"{farm} has {len(recent)} of the last 7 days (need {S.MIN_RECENT_DAYS})")
    return summary, problems


def train(fam, end_day: str | None = None) -> str:
    """Fit on TRAIN_START .. end_day (default today, local) and save a candidate. Returns the version."""
    end = utc(end_day) if end_day else today_utc()
    if end > today_utc():
        raise SystemExit(f"--end {end_day} is in the future: the model would block forecasts until then")
    log(f"{fam.NAME}: training on {S.TRAIN_START} .. {end.tz_convert(S.LOCAL_TZ):%Y-%m-%d} (local days)")
    data = fam.read(db.engine(S.READ_DB_ENV), utc(S.TRAIN_START), end)
    checked, bad = data_check(data, end)
    if bad:
        raise SystemExit(f"train {fam.NAME}: NOT saved: {bad}; the active model stays as it is")
    fitted = fam.fit(data, end)
    bad = fam.problems(fitted)
    if bad:
        raise SystemExit(f"train {fam.NAME}: NOT saved: {bad}; the active model stays as it is")
    version = registry.save(fam, fitted, end, {"data_check": checked,
                                               "data_counts": {k: v["counts"] for k, v in data.items()}})
    log(f"{fam.NAME}: saved candidate {version} (not active). Next: run.py compare {fam.NAME}; "
        f"run.py promote {fam.NAME} {version}")
    return version


# ------------------------------------------------------------------ backfill

def backfill_paths(fam, start_day: str, end_day: str):
    base = S.REPORT_DIR / f"backfill_{fam.NAME}_{pd.Timestamp(start_day):%Y-%m-%d}_{pd.Timestamp(end_day):%Y-%m-%d}"
    return base.with_suffix(".csv"), base.with_name(base.name + "_detail.csv")


def backfill(fam, start_day: str, end_day: str) -> pd.DataFrame:
    """Dry run: rows of every issue hour in the local days [start_day, end_day) (cut at the current hour), each
    block forecast by models fitted on the data before the block. Saves the rows CSV and a detail CSV; prints
    scores. Nothing is written to a database."""
    start = utc(start_day)
    end = min(utc(end_day), pd.Timestamp.now(tz="UTC").floor("h") + HOUR)
    if start < utc(S.BLOCK_ANCHOR) or start >= end:
        raise SystemExit(f"start must be on or after {S.BLOCK_ANCHOR} and before end")
    data = fam.read(db.engine(S.READ_DB_ENV), utc(S.TRAIN_START), end)
    starts = [utc(d) for d in pd.date_range(S.BLOCK_ANCHOR, end.tz_convert(S.LOCAL_TZ).tz_localize(None),
                                            freq=f"{S.RETRAIN_DAYS}D")]
    out = []
    for b0, b1 in zip(starts, [*starts[1:], end]):
        lo, hi = max(b0, start), min(b1, end)
        if lo >= hi:                                  # date_range(a, a, inclusive="left") is [a], not empty
            continue
        issues = pd.date_range(lo, hi, freq="h", inclusive="left")
        t = time.monotonic()
        fitted = fam.fit(data, b0)
        bad = fam.problems(fitted)
        if bad:
            log(f"{fam.NAME} block {b0:%Y-%m-%d}: {bad} (those forecasts are missing)", "WARN")
        out.append(fam.predict(data, fitted, issues).assign(trained_to_utc=b0))
        log(f"{fam.NAME} block {b0:%Y-%m-%d}: {len(issues)} issues in {time.monotonic() - t:.0f} s")
    detail = pd.concat(out, ignore_index=True)
    rows = writer.rows(detail, fam.SOURCES, None, pd.Timestamp.now(tz="UTC"))
    rows_path, detail_path = backfill_paths(fam, start_day, end_day)
    S.REPORT_DIR.mkdir(exist_ok=True)
    rows.to_csv(rows_path, index=False)
    detail.to_csv(detail_path, index=False)
    log(f"{fam.NAME}: {len(rows)} rows ({rows.groupby(['farmid', 'id_source']).size().to_dict()}) -> {rows_path}")
    print(compare.scores(detail, fam.SOURCES).to_string(index=False))
    return rows


def backfill_write(fam, start_day: str, end_day: str) -> None:
    """Write the rows CSV of an earlier backfill dry run (only runs newer than the table's are written)."""
    rows_path, _ = backfill_paths(fam, start_day, end_day)
    if not rows_path.exists():
        raise SystemExit(f"{rows_path} not found: run the backfill without --write first")
    st = writer.write(db.write_engine(S.WRITE_DB_ENV, S.FORECAST_TABLE_ENV), writer.from_csv(rows_path))
    if st is None:
        raise SystemExit("write failed")

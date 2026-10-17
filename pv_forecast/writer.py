"""The one way forecasts reach the forecast table (FORECAST_TABLE; on production only integration_schema.forecast_internal).

Rows: one per (run, farm, source, 15-min slot). Every run is kept: the newest run of a slot has is_current = 1, older
runs 0. FREE rows are made here from the PPA rows (settings.FreeFarm) and written in the same transaction.

Idempotent: a run (farm, source, issue hour) already in the table is never written again, so writing the same rows
twice adds nothing (an issue hour has one run, whether stamped on time or + ISSUE_DELAY_MIN by a catch-up). Older runs can still be added (a backfill fills a gap): a new row becomes current only if no
later run of its slot is in the table. In ONE transaction: read which runs and current rows exist, keep the new
runs, set is_current = 0 on the table's current rows they replace, insert them (every row must be inserted, else
everything is rolled back), check no slot has two current rows.
A failed write is retried; if it still fails, nothing is written and the next hourly run recomputes the missed hours.

Row layout: last_forecast_datetime = when the run was issued (the current time for an on-time run; the issue hour +
ISSUE_DELAY_MIN for a run made late, which also has is_backfill = 1); slot_ts = 15-min slot start (UTC);
data_forecast / ora_forecast = its local date and hour; interval_minutes = minute of the hour (0/15/30/45);
forecast_point_id = farm + source + UTC slot start, e.g. '30_110_20261025T0015Z'.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import sqlalchemy as sa

from . import db
from . import settings as S
from .log import log

COLUMNS = ["last_forecast_datetime", "slot_ts", "data_forecast", "ora_forecast", "interval_minutes", "farmid",
           "siteid", "id_source", "source_name", "forecasted_power_mw", "forecast_point_id", "is_current",
           "is_active_checkbox", "is_backfill"]
PAGE_ROWS = 1000

INSERT = """
insert into {table} (""" + ", ".join(COLUMNS) + """)
values %s
on conflict (slot_ts, siteid, farmid, id_source) where is_current = 1 do nothing
returning 1
"""
NEWEST = """
select farmid, id_source, max(last_forecast_datetime) as run from {table}
where farmid = any(:farms) and id_source = any(:ids) group by 1, 2
"""
# Runs issued in [r0, r1) already in the table, and the current rows of the slots [t0, t1)
RUNS = """
select distinct farmid, id_source, last_forecast_datetime as run from {table}
where farmid = any(:farms) and id_source = any(:ids) and last_forecast_datetime >= :r0 and last_forecast_datetime < :r1
"""
CURRENT = """
select farmid, id_source, slot_ts, last_forecast_datetime as run from {table}
where farmid = any(:farms) and id_source = any(:ids) and is_current = 1 and slot_ts >= :t0 and slot_ts < :t1
"""
DEMOTE = """
update {table} set is_current = 0
where is_current = 1 and farmid = :farmid and id_source = :id_source and slot_ts = any(:slots)
"""
DOUBLE = """
select count(*) from (select 1 from {table}
                      where farmid = any(:farms) and id_source = any(:ids) and is_current = 1
                        and slot_ts >= :t0 and slot_ts < :t1
                      group by slot_ts, farmid, id_source having count(*) > 1) d
"""


def all_farmids() -> list[int]:
    return [f.farmid for f in S.FARMS] + [f.farmid for f in S.FREE_FARMS]


# ------------------------------------------------------------------ rows

def _layout(d: pd.DataFrame) -> pd.DataFrame:
    local = d["slot_ts"].dt.tz_convert(S.LOCAL_TZ)
    d["data_forecast"] = local.dt.date
    d["ora_forecast"] = local.dt.hour
    d["interval_minutes"] = d["slot_ts"].dt.minute
    d["forecast_point_id"] = (d["farmid"].astype(str) + "_" + d["id_source"].astype(str) + "_"
                              + d["slot_ts"].dt.strftime("%Y%m%dT%H%MZ"))
    d["is_current"] = 0                                      # set among the rows written (write())
    d["is_active_checkbox"] = 1
    return d[COLUMNS]


def rows(detail: pd.DataFrame, sources: dict, live_issue: pd.Timestamp | None,
         now: pd.Timestamp) -> pd.DataFrame:
    """Table rows (PPA and FREE) from a family's predict() output: every (farm, issue, slot) with a value per source.
    The issue `live_issue` is stamped `now` (on time; at most its hour + 59 min, so a slow run never looks like the
    next hour's); every other issue issue + ISSUE_DELAY_MIN, is_backfill = 1.
    Raises on a negative or non-finite value (predict floors at 0; anything else is a bug)."""
    farms = {f.name: f for f in S.FARMS}
    late = detail["issue_utc"] != live_issue if live_issue is not None else pd.Series(True, index=detail.index)
    issued = detail["issue_utc"].where(~late, detail["issue_utc"] + pd.Timedelta(minutes=S.ISSUE_DELAY_MIN))
    issued = issued.where(late, min(now, live_issue + pd.Timedelta(minutes=59)) if live_issue is not None else now)
    parts = []
    for col, (name, id_source) in sources.items():
        ok = detail[col].notna()
        v = detail.loc[ok, col]
        if not np.isfinite(v).all() or (v < 0).any():
            raise ValueError(f"{col}: {int((v < 0).sum())} negative or non-finite forecasts")
        parts.append(pd.DataFrame({
            "last_forecast_datetime": issued[ok], "slot_ts": detail.loc[ok, "slot_utc"],
            "farmid": detail.loc[ok, "farm"].map(lambda n: farms[n].farmid),
            "siteid": detail.loc[ok, "farm"].map(lambda n: farms[n].siteid),
            "id_source": id_source, "source_name": name, "forecasted_power_mw": v.round(3),
            "is_backfill": late[ok].astype(int)}))
    ppa = pd.concat(parts, ignore_index=True)
    free = []
    for f in S.FREE_FARMS:                                   # FREE = PPA rescaled, same run, slot and source
        d = ppa[ppa["farmid"] == f.ppa_farmid].copy()
        d["farmid"], d["siteid"] = f.farmid, f.siteid
        d["forecasted_power_mw"] = (d["forecasted_power_mw"] * f.multiplier).round(3)
        free.append(d)
    return _layout(pd.concat([ppa, *free], ignore_index=True))


# ------------------------------------------------------------------ write

def newest_runs(c, ids: list[int]) -> dict[tuple[int, int], pd.Timestamp]:
    """(farmid, id_source) -> newest run in the table, our farms; `c` a connection."""
    q = sa.text(NEWEST.format(table=db.table(S.FORECAST_TABLE_ENV)))
    return {(r.farmid, r.id_source): pd.Timestamp(r.run).tz_convert("UTC")
            for r in c.execute(q, {"farms": all_farmids(), "ids": ids})}


def to_write(df: pd.DataFrame, written: set, current: dict) -> pd.DataFrame:
    """The rows of runs not yet in the table, with is_current set: 1 for the newest run of a slot among them if the
    table holds no later run of that slot. written: {(farmid, id_source, issue hour)} in the table; current:
    (farmid, id_source, slot) -> run of the table's current row. Raises if a (farm, source, run, slot) repeats."""
    key = ["farmid", "id_source", "last_forecast_datetime", "slot_ts"]
    dup = df.duplicated(key)
    if dup.any():
        raise ValueError(f"{int(dup.sum())} rows repeat a (farm, source, run, slot); nothing written")
    there = pd.Series([(int(f), int(i), t) in written for f, i, t in
                       zip(df["farmid"], df["id_source"], df["last_forecast_datetime"].dt.floor("h"))],
                      index=df.index)
    d = df[~there].copy()
    top = d.groupby(["farmid", "id_source", "slot_ts"])["last_forecast_datetime"].transform("max")
    old = pd.Series([current.get((int(f), int(i), s)) for f, i, s in zip(d["farmid"], d["id_source"], d["slot_ts"])],
                    index=d.index, dtype="datetime64[ns, UTC]")
    d["is_current"] = ((d["last_forecast_datetime"] == top) & ~(old > top)).astype(int)
    return d.reset_index(drop=True)


def _write_once(eng: sa.Engine, df: pd.DataFrame) -> dict:
    from psycopg2.extras import execute_values

    table = db.table(S.FORECAST_TABLE_ENV)
    ids = sorted(int(i) for i in df["id_source"].unique())
    p = {"farms": all_farmids(), "ids": ids, "t0": df["slot_ts"].min(),
         "t1": df["slot_ts"].max() + pd.Timedelta(minutes=15)}
    with eng.begin() as c:                                    # one transaction: all or nothing
        c.execute(sa.text("set local statement_timeout = '900s'"))
        c.execute(sa.text("set local lock_timeout = '10s'"))
        c.execute(sa.text("set local idle_in_transaction_session_timeout = '60s'"))
        r0 = df["last_forecast_datetime"].min().floor("h")
        r1 = df["last_forecast_datetime"].max().floor("h") + pd.Timedelta(hours=1)
        written = {(r.farmid, r.id_source, pd.Timestamp(r.run).tz_convert("UTC").floor("h")) for r in c.execute(
            sa.text(RUNS.format(table=table)), {**p, "r0": r0, "r1": r1})}
        current = {(r.farmid, r.id_source, pd.Timestamp(r.slot_ts).tz_convert("UTC")):
                   pd.Timestamp(r.run).tz_convert("UTC") for r in c.execute(sa.text(CURRENT.format(table=table)), p)}
        new = to_write(df, written, current)
        if new.empty:
            return {"rows": len(df), "new": 0, "demoted": 0}
        demoted = 0
        for (farmid, id_source), g in new[new["is_current"] == 1].groupby(["farmid", "id_source"]):
            demoted += c.execute(sa.text(DEMOTE.format(table=table)), {
                "farmid": int(farmid), "id_source": int(id_source),
                "slots": [t.to_pydatetime() for t in g["slot_ts"]]}).rowcount
        values = [tuple(r[col] for col in COLUMNS) for r in new.to_dict("records")]
        with c.connection.cursor() as cur:
            inserted = execute_values(cur, INSERT.format(table=table), values, page_size=PAGE_ROWS, fetch=True)
        if len(inserted) != len(new):
            raise RuntimeError(f"inserted {len(inserted)} of {len(new)} rows; rolled back, nothing written")
        double = c.execute(sa.text(DOUBLE.format(table=table)), p).scalar()
        if double:
            raise RuntimeError(f"{double} slots would have 2 current rows; rolled back, nothing written")
    return {"rows": len(df), "new": len(new), "demoted": demoted,
            "by_farm_source": new.groupby(["farmid", "id_source"]).size().to_dict()}


def write(eng: sa.Engine, df: pd.DataFrame) -> dict | None:
    """Write `df` (rows() output) as described in the module docstring, with retries. Returns the counts, or None if
    every attempt failed (nothing written)."""
    if df.empty:
        return {"rows": 0, "new": 0, "demoted": 0}
    for attempt in range(1, S.WRITE_RETRIES + 1):
        try:
            st = _write_once(eng, df)
        except Exception as exc:                              # network, timeout, lock: all retried the same way
            wait = S.WRITE_RETRY_BASE_S * 2 ** (attempt - 1)
            more = f"; retry in {wait} s" if attempt < S.WRITE_RETRIES else "; giving up"
            log(f"write attempt {attempt}/{S.WRITE_RETRIES} failed: {db.describe(eng, exc)}{more}", "WARN")
            if attempt < S.WRITE_RETRIES:
                time.sleep(wait)
            continue
        log(f"wrote {st['new']} new rows of {st['rows']} ({st['rows'] - st['new']} of runs already in the table), "
            f"{st['demoted']} older rows set not current; by (farm, source) {st.get('by_farm_source', {})}")
        return st
    log("write FAILED: nothing written; the next hourly run recomputes these hours", "ERROR")
    return None


def newest_issue(eng: sa.Engine, ids: list[int]) -> pd.Timestamp | None:
    """The newest issue hour written for any PPA farm and source of `ids` (None if none)."""
    with eng.connect() as c:
        runs = [t for (farmid, _), t in newest_runs(c, ids).items() if farmid in {f.farmid for f in S.FARMS}]
    return max(runs).floor("h") if runs else None


def from_csv(path) -> pd.DataFrame:
    """Rows saved with DataFrame.to_csv(index=False), with the column types write() needs. Raises on wrong columns
    or missing values."""
    df = pd.read_csv(path)
    if list(df.columns) != COLUMNS or df.isna().any().any():
        raise SystemExit(f"{path}: columns are not {COLUMNS} or values are missing")
    for col in ("last_forecast_datetime", "slot_ts"):
        df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
    df["data_forecast"] = pd.to_datetime(df["data_forecast"]).dt.date
    return df

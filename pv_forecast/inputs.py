"""Model inputs, read from production (read only) or computed: plant capacity factor, weather forecasts, sun geometry.

capacity_factor()  ACTIVE power of a farm's PPA SACUs / their nameplate per 15-min slot, gap-free UTC index. A slot is
                   missing (never filled) when any SACU has no reading, its readings span less than MIN_COVER_MIN, a
                   reading is frozen (same non-zero mean as the slot before) or above OVER_RATED x nameplate.
wait_for_data()    waits until every SACU has reported up to the hour start (the plant table may lag).
weather_newest()   per 15-min slot, the weather of its hour from the newest run usable at the hour start (1-hour models).
weather_runs()     every run of one weather model for a site, as issued (48-hour models).
sun()              clear-sky irradiance and sun / panel angles at each slot middle (pvlib, no network).
"""

from __future__ import annotations

import time

import pandas as pd
import sqlalchemy as sa

from . import db
from . import settings as S

SLOT = pd.Timedelta(minutes=15)
PLANT_TABLES = ("hist_production_rawdata.raw_realtime_sacu_pv_data",    # archive
                "integration_schema.raw_realtime_sacu_pv_data")         # live tail (~2 weeks)
CHUNK_DAYS = 7

# Sums and counts per (slot, SACU), so a slot split between the archive and the live table adds up exactly.
# t_stamp is naive local time; bounds are naive local text cast to timestamp.
SLOT_SQL = """
select date_trunc('hour', t_stamp) + floor(extract(minute from t_stamp) / 15) * interval '15 min' as slot_ts,
       sacu_no,
       min(t_stamp)    as first_ts,
       max(t_stamp)    as last_ts,
       sum({value})    as v_sum,
       count({value})  as v_n
from {table}
where farmid = :farmid and sacu_no = any(:sacus)
  and t_stamp >= cast(:t0 as timestamp) and t_stamp < cast(:t1 as timestamp)
group by 1, 2
"""
LATEST_SQL = """
select sacu_no, max(t_stamp) as last_ts
from integration_schema.raw_realtime_sacu_pv_data
where farmid = :farmid and sacu_no = any(:sacus)
  and t_stamp >= cast(:t0 as timestamp) and t_stamp < cast(:t1 as timestamp)
group by 1
"""
# Weather rows of the hours [t0, t1) from every run usable at the hour start (publication delay and age limit)
NEWEST_SQL = """
select run_utc, slot_start_utc, lead_h, {cols}
from {table}
where site = :site and model = :model
  and slot_start_utc >= :t0 and slot_start_utc < :t1
  and run_utc <= slot_start_utc - make_interval(hours => :delay_h)
  and run_utc >= slot_start_utc - make_interval(hours => :age_h)
"""
RUNS_SQL = """
select run_utc, slot_start_utc, lead_h, panel_tilt, panel_azimuth, {cols}
from {table}
where site = :site and model = :model and slot_start_utc >= :t0 and slot_start_utc < :t1
"""


def to_local(t: pd.Timestamp) -> pd.Timestamp:
    """tz-aware -> naive local, as stored in the plant tables."""
    return t.tz_convert(S.LOCAL_TZ).tz_localize(None)


# ------------------------------------------------------------------ plant data

def wait_for_data(eng: sa.Engine, farm: S.Farm, hour_utc: pd.Timestamp, deadline: float, late_s: int = 60,
                  poll_s: int = 30) -> list[int]:
    """Poll until every SACU has a reading at or after hour start - late_s, or until `deadline` (time.monotonic()).
    Returns the SACUs still late ([] = all data arrived)."""
    lo, hi = to_local(hour_utc - pd.Timedelta(hours=1)), to_local(hour_utc + pd.Timedelta(hours=1))
    need = to_local(hour_utc - pd.Timedelta(seconds=late_s))
    while True:
        last = db.read_sql(eng, LATEST_SQL, farmid=farm.rawdata_farmid, sacus=list(farm.sacus),
                           t0=str(lo), t1=str(hi)).set_index("sacu_no")["last_ts"]
        late = [n for n in farm.sacus if not (n in last.index and last[n] >= need)]
        if not late or time.monotonic() >= deadline:
            return late
        time.sleep(poll_s)


def capacity_factor(eng: sa.Engine, farm: S.Farm, start_utc: pd.Timestamp,
                    end_utc: pd.Timestamp) -> tuple[pd.Series, dict]:
    """Capacity factor of the farm's SACUs per 15-min slot in [start_utc, end_utc): (series on a gap-free UTC index,
    counts for the log). Slots of the repeated / skipped DST hour cannot be placed in UTC from naive local time: they
    stay missing. Raises if the archive and the live table hold the same readings (would double count)."""
    lo, hi = to_local(start_utc), to_local(end_utc)
    bounds = [*pd.date_range(lo, hi, freq=f"{CHUNK_DAYS}D"), hi]
    parts = [db.read_sql(eng, SLOT_SQL.format(table=t, value=S.PLANT_COLUMN), farmid=farm.rawdata_farmid,
                         sacus=list(farm.sacus), t0=str(a), t1=str(b))
             for t in PLANT_TABLES for a, b in zip(bounds[:-1], bounds[1:]) if a < b]
    parts = [p for p in parts if not p.empty]
    if not parts:
        raise ValueError(f"{farm.name}: no plant rows for SACUs {list(farm.sacus)} in [{lo}, {hi}) local")
    raw = pd.concat(parts, ignore_index=True)
    raw = raw.astype({"v_sum": "float64", "v_n": "int64"}).sort_values(["slot_ts", "sacu_no", "first_ts"])
    if (raw["first_ts"] <= raw.groupby(["slot_ts", "sacu_no"])["last_ts"].shift()).any():
        raise ValueError(f"{farm.name}: archive and live table overlap")
    s = raw.groupby(["slot_ts", "sacu_no"]).agg(v_sum=("v_sum", "sum"), v_n=("v_n", "sum"),
                                                first_ts=("first_ts", "min"), last_ts=("last_ts", "max")).reset_index()

    # naive local -> UTC; the ambiguous (October) and non-existent (March) hour become NaT and are dropped
    s["slot_utc"] = (s["slot_ts"].dt.tz_localize(S.LOCAL_TZ, ambiguous="NaT", nonexistent="NaT")
                     .dt.tz_convert("UTC"))
    n_dst = int(s["slot_utc"].isna().sum())
    s = s.dropna(subset=["slot_utc"])
    covered = (s["last_ts"] - s["first_ts"]) >= pd.Timedelta(minutes=S.MIN_COVER_MIN)
    s["v"] = (s["v_sum"] / s["v_n"]).where((s["v_n"] > 0) & covered)   # kW, slot mean
    few = int(((s["v_n"] > 0) & ~covered).sum())

    idx = pd.date_range(start_utc, end_utc, freq=SLOT, inclusive="left", name="slot_start_utc")
    wide = s.pivot(index="slot_utc", columns="sacu_no", values="v").reindex(idx)
    wide = wide.reindex(columns=list(farm.sacus))                    # a SACU that never reported: all missing
    plate = pd.Series(farm.sacus)
    frozen = (wide != 0) & (wide == wide.shift())
    over = wide > S.OVER_RATED * plate
    wide = wide.mask(frozen | over)
    cf = (wide.sum(axis=1, min_count=1) / plate.sum()).where(wide.notna().all(axis=1)).rename("cf")
    counts = {"slots": len(cf), "missing": int(cf.isna().sum()), "partial_slot": few,
              "frozen": int(frozen.sum().sum()), "over_rated": int(over.sum().sum()), "dst_dropped": n_dst}
    return cf, counts


# ------------------------------------------------------------------ weather

def _read_weather(eng: sa.Engine, template: str, cols: list[str], **params) -> pd.DataFrame:
    query = template.format(table=db.table(S.WEATHER_TABLE_ENV), cols=", ".join(cols))
    d = db.read_sql(eng, query, **params)
    for col in ("run_utc", "slot_start_utc"):
        d[col] = pd.to_datetime(d[col], utc=True)
    if d.duplicated(["run_utc", "slot_start_utc"]).any():
        raise ValueError(f"weather table repeats (run, hour) rows for {params}")
    return d


def weather_newest(eng: sa.Engine, farm: S.Farm, start_utc: pd.Timestamp, end_utc: pd.Timestamp, model: str,
                   cols: dict[str, str], delay_h: int, max_age_h: int) -> tuple[pd.DataFrame, dict]:
    """Weather forecast of `model` per 15-min slot of [start_utc, end_utc): for each hour H the row of the newest run
    with run_utc + delay_h <= H, H - run_utc <= max_age_h and every `cols` value present, unchanged for its 4
    quarters. Columns: `cols` renamed (table column -> model name), run_utc, lead_h. A slot whose hour has no such
    run is missing (never filled). Returns (frame, counts: hours, missing hours, hours from an older run)."""
    d = _read_weather(eng, NEWEST_SQL, list(cols), site=farm.site, model=model, t0=start_utc.floor("h"),
                      t1=end_utc.ceil("h"), delay_h=delay_h, age_h=max_age_h)
    d = d.sort_values(["slot_start_utc", "run_utc"])
    newest = d.drop_duplicates("slot_start_utc", keep="last").set_index("slot_start_utc")["run_utc"]
    usable = d.dropna(subset=list(cols)).drop_duplicates("slot_start_utc", keep="last")
    usable = usable.set_index("slot_start_utc").rename(columns=cols)
    hours = pd.date_range(start_utc.floor("h"), end_utc.ceil("h"), freq="h", inclusive="left")
    usable = usable.reindex(hours)
    older = int((usable["run_utc"].notna() & (usable["run_utc"] != newest.reindex(hours))).sum())
    slots = pd.date_range(start_utc, end_utc, freq=SLOT, inclusive="left", name="slot_start_utc")
    out = usable[[*cols.values(), "run_utc", "lead_h"]].reindex(slots.floor("h")).set_axis(slots)
    counts = {"hours": len(hours), "missing_hours": int(usable["run_utc"].isna().sum()), "older_run_hours": older}
    return out, counts


def weather_runs(eng: sa.Engine, farm: S.Farm, t0: pd.Timestamp, t1: pd.Timestamp, model: str,
                 cols: dict[str, str], required: bool = True) -> pd.DataFrame:
    """Every run's rows of the hours [t0, t1) for the farm's site and `model`: run_utc, slot_start_utc, lead_h and
    `cols` renamed, sorted by run and hour. No rows: raises if `required`, else an empty frame. Raises if a row's
    panel plane is not the farm's (Open-Meteo azimuth 0 = south = our 180)."""
    d = _read_weather(eng, RUNS_SQL, list(cols), site=farm.site, model=model, t0=t0, t1=t1)
    if d.empty:
        if required:
            raise ValueError(f"{farm.name}: no {model} rows for {farm.site} in [{t0}, {t1})")
        return pd.DataFrame(columns=["run_utc", "slot_start_utc", "lead_h", *cols.values()])
    if not ((d["panel_tilt"] == farm.tilt) & (d["panel_azimuth"] == farm.azimuth - 180)).all():
        raise ValueError(f"{farm.name}: weather panel plane is not tilt {farm.tilt}, azimuth {farm.azimuth}: "
                         f"{d[['panel_tilt', 'panel_azimuth']].drop_duplicates().to_dict('records')}")
    d = d.drop(columns=["panel_tilt", "panel_azimuth"]).rename(columns=cols)
    return d.sort_values(["run_utc", "slot_start_utc"]).reset_index(drop=True)


# ------------------------------------------------------------------ sun

def sun(farm: S.Farm, index: pd.DatetimeIndex) -> pd.DataFrame:
    """At the middle of each 15-min slot of `index` (UTC): clear-sky GHI (Ineichen, W/m2), apparent zenith and
    azimuth (deg), clear-sky irradiance on the panel plane (isotropic) and the angle of incidence on it."""
    import pvlib

    loc = pvlib.location.Location(farm.latitude, farm.longitude, altitude=None)    # altitude from pvlib's map
    mid = index + SLOT / 2
    sp = loc.get_solarposition(mid)
    cs = loc.get_clearsky(mid, model="ineichen", solar_position=sp)
    poa = pvlib.irradiance.get_total_irradiance(farm.tilt, farm.azimuth, sp["apparent_zenith"], sp["azimuth"],
                                                cs["dni"], cs["ghi"], cs["dhi"])["poa_global"]
    aoi = pvlib.irradiance.aoi(farm.tilt, farm.azimuth, sp["apparent_zenith"], sp["azimuth"])
    return pd.DataFrame({"cs_ghi": cs["ghi"].to_numpy(), "zenith": sp["apparent_zenith"].to_numpy(),
                         "azimuth": sp["azimuth"].to_numpy(), "poa_clearsky": poa.to_numpy(),
                         "aoi": aoi.to_numpy()}, index=index)


def sun_up(farm: S.Farm, hour_utc: pd.Timestamp) -> bool:
    """True if the sun is above the horizon at any minute of the hour (checked every 5 min)."""
    import pvlib

    t = pd.date_range(hour_utc, hour_utc + pd.Timedelta(hours=1), freq="5min")
    el = pvlib.location.Location(farm.latitude, farm.longitude, altitude=None).get_solarposition(t)["elevation"]
    return bool((el > 0).any())

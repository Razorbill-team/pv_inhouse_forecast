"""Weather forecast collector: Open-Meteo runs of ECMWF IFS and DWD ICON-EU for our sites into WEATHER_TABLE.

One row per (site, model, run, forecast hour); every run kept (past runs train the models, the newest makes the
forecast). Rows are only inserted, never changed or deleted; an existing key is skipped, so reruns are safe.
  ecmwf_ifs  runs 00/06/12/18 UTC, published ~6.5 h later
  icon_eu    runs every 3 h, but only 00/06/12/18 UTC reach 72 h, so only those are stored; published ~3-4 h later;
             fewer variables (the others stay NULL)
Without days (hourly): each model's newest long run and the LOOKBACK runs before it (24 h, so an outage fills
itself). With FIRST [LAST] UTC days: every run started in those days (Open-Meteo keeps runs from 2024-03-14).

Time convention: Open-Meteo stamps an hourly value with the END of its hour; radiation is the mean over that hour.
A row is labelled by its start: slot_start_utc = stamp - 1 h. global_tilted_irradiance is on each site's panel
plane (Open-Meteo azimuth 0 = south). Missing values stay NULL. Licence: the free tier is for non-commercial use.
"""

from __future__ import annotations

import json
import time

import pandas as pd
import requests
import sqlalchemy as sa

from . import db
from . import settings as S
from .log import log

FORECAST_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
RUN_EVERY = pd.Timedelta(hours=6)
HOURS = 72
HOUR = pd.Timedelta(hours=1)
LOOKBACK = 4                        # hourly mode also checks the 4 runs before the newest (24 h of outage)
PUBLISH_GRACE = pd.Timedelta(hours=2)
RETRIES, RETRY_BASE_S = 4, 15
TIMEOUT_S = (10, 90)

# site -> latitude, longitude, panel tilt and azimuth (Open-Meteo convention: 0 = south)
SITES = {
    "negureni": dict(latitude=47.6332891, longitude=28.5502817, tilt=34, azimuth=0),
    "radeni": dict(latitude=47.1861165, longitude=28.6455931, tilt=34, azimuth=0),
    "valea_mare": dict(latitude=47.118036, longitude=27.880384, tilt=25, azimuth=0),
}
VARIABLES = {
    "shortwave_radiation": "ghi", "direct_normal_irradiance": "dni", "diffuse_radiation": "dhi",
    "direct_radiation": "direct_radiation", "global_tilted_irradiance": "global_tilted_irradiance",
    "terrestrial_radiation": "terrestrial_radiation",
    "cloud_cover": "cloud_cover", "cloud_cover_low": "cloud_cover_low", "cloud_cover_mid": "cloud_cover_mid",
    "cloud_cover_high": "cloud_cover_high", "sunshine_duration": "sunshine_duration", "is_day": "is_day",
    "temperature_2m": "temperature_2m", "dew_point_2m": "dew_point_2m", "relative_humidity_2m": "relative_humidity_2m",
    "total_column_integrated_water_vapour": "total_column_integrated_water_vapour",
    "surface_pressure": "surface_pressure", "precipitation": "precipitation",
    "et0_fao_evapotranspiration": "et0_fao_evapotranspiration",
    "wind_speed_10m": "wind_speed_10m", "wind_direction_10m": "wind_direction_10m", "wind_gusts_10m": "wind_gusts_10m",
    "wind_speed_80m": "wind_speed_80m", "wind_speed_100m": "wind_speed_100m", "wind_speed_120m": "wind_speed_120m",
    "wind_direction_100m": "wind_direction_100m", "boundary_layer_height": "boundary_layer_height", "cape": "cape",
    "snowfall": "snowfall",
}
VALUES = list(VARIABLES.values())
MODELS = {
    "ecmwf_ifs": {"meta": "https://api.open-meteo.com/data/ecmwf_ifs/static/meta.json", "variables": list(VARIABLES)},
    "icon_eu": {"meta": "https://api.open-meteo.com/data/dwd_icon_eu/static/meta.json", "variables": [
        "shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation", "direct_radiation",
        "global_tilted_irradiance", "cloud_cover", "temperature_2m", "dew_point_2m", "relative_humidity_2m",
        "surface_pressure", "precipitation", "snowfall", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
        "wind_speed_80m", "wind_speed_100m", "wind_speed_120m", "wind_direction_100m"]},
}
KEY = ["site", "model", "run_utc", "slot_start_utc"]
COLUMNS = [*KEY, "period_end_utc", "lead_h", "latitude", "longitude", "panel_tilt", "panel_azimuth", *VALUES]


class RunNotArchived(Exception):
    """Open-Meteo answered that it does not hold this model run."""


class ApiLimit(Exception):
    """Open-Meteo's request limit is used up: every further request would fail."""


# ------------------------------------------------------------------ API

def is_json(r: requests.Response) -> bool:
    try:
        return isinstance(json.loads(r.content), dict)
    except ValueError:
        return False


def get(url: str, params: dict | None = None) -> requests.Response:
    """GET with retries on network errors, 429, 5xx and a 200 whose body is not valid JSON (Open-Meteo sometimes
    answers 200 with a cut-off body). Raises ApiLimit at once when the hourly or daily limit is used up."""
    for attempt in range(RETRIES):
        wait = RETRY_BASE_S * (attempt + 1)
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            if attempt == RETRIES - 1:
                raise
            log(f"  {type(exc).__name__}: {str(exc)[:200]}; retrying in {wait} s", "WARN")
        else:
            if r.status_code == 429 and ("Hourly" in r.text or "Daily" in r.text):
                raise ApiLimit(f"Open-Meteo request limit used up: {r.text[:200]}; run again later")
            broken = r.status_code == 200 and not is_json(r)
            if (r.status_code not in (429, 500, 502, 503, 504) and not broken) or attempt == RETRIES - 1:
                return r
            log(f"  HTTP {r.status_code}{' non-JSON body' if broken else ''}; retrying in {wait} s", "WARN")
        time.sleep(wait)
    raise AssertionError("unreachable")


def newest_run(model: str) -> tuple[pd.Timestamp, pd.Timestamp | None]:
    """(newest stored-kind run start, its publication time or None), from the model's meta.json. For ICON-EU a newer
    short run (03/09/15/21 UTC) gives the long run before it, with publication time None (not fresh)."""
    r = get(MODELS[model]["meta"])
    r.raise_for_status()
    meta = r.json()
    start = pd.Timestamp(meta["last_run_initialisation_time"], unit="s", tz="UTC")
    start = pd.Timestamp(f"{start:%Y-%m-%d %H:%M}", tz="UTC")
    published = pd.Timestamp(meta["last_run_availability_time"], unit="s", tz="UTC")
    return (start, published) if start == start.floor(RUN_EVERY) else (start.floor(RUN_EVERY), None)


def fetch_run(site: str, run: pd.Timestamp, model: str) -> pd.DataFrame:
    """One run of `model` at one site in the table layout (HOURS - 1 rows, lead 0 .. HOURS - 2 h)."""
    s = SITES[site]
    r = get(FORECAST_URL, {"latitude": s["latitude"], "longitude": s["longitude"], "models": model,
                           "hourly": ",".join(MODELS[model]["variables"]), "timezone": "UTC",
                           "wind_speed_unit": "ms", "tilt": s["tilt"], "azimuth": s["azimuth"],
                           "run": f"{run:%Y-%m-%dT%H:%M}", "forecast_hours": HOURS})
    if r.status_code == 400 and "model run is not available" in r.text:
        raise RunNotArchived(r.text[:200])
    if r.status_code != 200 or not is_json(r):
        raise RuntimeError(f"Open-Meteo HTTP {r.status_code}: {r.text[:300]}")
    what = f"{site} {model} run {run:%Y-%m-%d %H:%M}Z"
    answer = json.loads(r.content)
    if not isinstance(answer.get("hourly"), dict):
        raise ValueError(f"{what}: no 'hourly' block in the answer")
    df = pd.DataFrame(answer["hourly"])
    absent = {"time", *MODELS[model]["variables"]} - set(df.columns)
    if absent:
        raise ValueError(f"{what}: variables missing from the answer: {sorted(absent)}")
    stamp = pd.to_datetime(df.pop("time"), utc=True)
    if len(stamp) != HOURS or not (stamp.values == pd.date_range(run, periods=HOURS, freq=HOUR).values).all():
        raise ValueError(f"{what}: stamps are not the {HOURS} hours from the run start")
    df = df[MODELS[model]["variables"]].rename(columns=VARIABLES).reindex(columns=VALUES)
    keep = (stamp > run).to_numpy()                         # the stamp at the run start is the hour before it
    df, stamp = df[keep].reset_index(drop=True), stamp[keep].reset_index(drop=True)
    df = df.assign(site=site, model=model, run_utc=run, slot_start_utc=stamp - HOUR, period_end_utc=stamp,
                   latitude=s["latitude"], longitude=s["longitude"], panel_tilt=s["tilt"], panel_azimuth=s["azimuth"])
    df["lead_h"] = ((df["slot_start_utc"] - run) / HOUR).astype(int)
    return df[COLUMNS]


# ------------------------------------------------------------------ database

def insert(eng: sa.Engine, df: pd.DataFrame) -> int:
    """Insert in one transaction, one statement per 1,000 rows; an existing key is skipped. Returns rows inserted."""
    from psycopg2.extras import execute_values

    q = (f"insert into {db.table(S.WEATHER_TABLE_ENV)} ({', '.join(COLUMNS)}) values %s "
         f"on conflict ({', '.join(KEY)}) do nothing returning 1")
    values = [tuple(None if pd.isna(v) else v.to_pydatetime() if isinstance(v, pd.Timestamp) else v
                    for v in r.values()) for r in df[COLUMNS].to_dict("records")]    # Python types for psycopg2
    with eng.begin() as c, c.connection.cursor() as cur:
        return len(execute_values(cur, q, values, page_size=1000, fetch=True))


def collect_runs(eng: sa.Engine, model: str, runs: list[pd.Timestamp], fresh: pd.Timestamp | None,
                 backfill: bool) -> int:
    """Download and store every run of `runs` the table does not hold yet, every site. Returns failures. The run
    `fresh` (published < PUBLISH_GRACE ago) not served yet or incomplete is a warning, retried next hour. In a
    backfill, a past run Open-Meteo never archived is a warning."""
    failures = 0
    asked = [VARIABLES[v] for v in MODELS[model]["variables"]]
    for site in SITES:
        with eng.connect() as c:
            have = {pd.Timestamp(t).tz_convert("UTC") for t in c.execute(sa.text(
                f"select distinct run_utc from {db.table(S.WEATHER_TABLE_ENV)} "
                "where site = :s and model = :m and run_utc >= :r"),
                {"s": site, "m": model, "r": min(runs).to_pydatetime()}).scalars()}
        todo = [r for r in runs if r not in have]
        log(f"weather {site} {model}: {len(runs) - len(todo)} of {len(runs)} runs stored, {len(todo)} to download")
        for run in todo:
            name = f"{site} {model} run {run:%Y-%m-%d %H:%M}Z"
            try:
                df = fetch_run(site, run, model)
                nulls = df[asked].isna().sum()
                if run == fresh and nulls.any():
                    log(f"  {name}: just published and incomplete {nulls[nulls > 0].to_dict()}; next hour", "WARN")
                    continue
                n = insert(eng, df)
                log(f"  {name}: {n} rows stored" + (f", missing values (NULL) {nulls[nulls > 0].to_dict()}"
                                                    if nulls.any() else ""), "WARN" if nulls.any() else "INFO")
            except RunNotArchived as exc:
                if run == fresh:
                    log(f"  {name}: not served yet (just published), next hour", "WARN")
                elif backfill:
                    log(f"  {name}: not in Open-Meteo's archive, skipped", "WARN")
                else:
                    failures += 1
                    log(f"  {name} FAILED: Open-Meteo does not serve it: {exc}", "ERROR")
            except ApiLimit:
                raise
            except Exception as exc:                        # noqa: BLE001 - one bad run must not stop the rest
                failures += 1
                log(f"  {name} FAILED: {type(exc).__name__}: {str(exc)[:300]}", "ERROR")
    return failures


def collect(first: pd.Timestamp | None = None, last: pd.Timestamp | None = None,
            models: list[str] | None = None) -> int:
    """Hourly (first None) or backfill of the UTC days [first, last]. Returns the exit code (0 ok, 1 failures)."""
    try:
        return _collect(first, last, models)
    except ApiLimit as exc:                                 # every further request would fail: stop cleanly
        log(f"weather: STOPPED {exc}", "ERROR")
        return 1


def _collect(first, last, models) -> int:
    eng = db.write_engine(S.WEATHER_DB_ENV, S.WEATHER_TABLE_ENV)
    failures = 0
    for model in models or list(MODELS):
        try:
            run, published = newest_run(model)
        except ApiLimit:
            raise
        except Exception as exc:                            # noqa: BLE001
            log(f"weather {model}: meta.json not readable: {type(exc).__name__}: {str(exc)[:300]}", "ERROR")
            failures += 1
            continue
        fresh = run if published is not None and pd.Timestamp.now(tz="UTC") - published < PUBLISH_GRACE else None
        if first is None:
            runs = [run - k * RUN_EVERY for k in range(LOOKBACK, -1, -1)]
        else:
            end = last + pd.Timedelta(days=1) if last is not None else run + RUN_EVERY
            runs = [r for r in pd.date_range(first, end, freq=RUN_EVERY, inclusive="left") if r <= run]
            if not runs:
                log(f"weather {model}: no published run from {first:%Y-%m-%d} on (newest {run})", "ERROR")
                failures += 1
                continue
        log(f"weather {model}: newest long run {run:%Y-%m-%d %H:%M}Z; checking {len(runs)} runs")
        failures += collect_runs(eng, model, runs, fresh, backfill=first is not None)
    log(f"weather: done, {failures} failure(s)", "ERROR" if failures else "INFO")
    return 1 if failures else 0

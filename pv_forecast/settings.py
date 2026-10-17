"""All settings of the PV forecast in one place: farms, database settings, sources, job limits.

What is forecast: the power (MW) of each PPA farm, per 15-min slot, from plant data and weather forecasts.
FREE farms at the same site get the PPA forecast rescaled (see FreeFarm). Models live in families/:
  h1   next hour, sources 100-103 (families/h1.py)
  h48  next 48 hours, sources 110-112 (families/h48.py)
Times are UTC everywhere except the plant tables, which store naive local time (LOCAL_TZ).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # the repository folder
MODELS_DIR = ROOT / "saved_models"              # <family>/<version>/ + ACTIVE + history.jsonl (registry.py)
LOG_DIR = ROOT / "logs"                         # run_<YYYY-MM>.log
REPORT_DIR = ROOT / "reports"                   # compare reports, backfill and dry-run CSVs

LOCAL_TZ = "Europe/Chisinau"

# ---- databases (URLs and table names come from environment variables or .env, see .env.example)
READ_DB_ENV = "POSTDB_URL"                      # production, read only: plant data and the weather table
WEATHER_TABLE_ENV = "WEATHER_TABLE"             # weather table in POSTDB_URL (filled by `run.py weather`)
WEATHER_DB_ENV = "WEATHER_DB_URL"               # database the weather collector writes to (same table)
WRITE_DB_ENV = "FORECAST_DB_URL"                # database the forecast is written to
FORECAST_TABLE_ENV = "FORECAST_TABLE"           # table in it, schema.table
PROD_HOSTS = ("10.50.0.1",)                     # production
PROD_TABLES = {FORECAST_TABLE_ENV: "integration_schema.forecast_internal",   # the only tables written there
               WEATHER_TABLE_ENV: "integration_schema.open_meteo_forecast_hourly"}


# ---- farms
@dataclass(frozen=True)
class Farm:
    """A PPA farm: what the models forecast."""
    name: str
    site: str                       # site key of the weather table and the panel plane
    farmid: int                     # business farm id (integration_schema.farms)
    siteid: int
    rawdata_farmid: int             # telemetry farm id (raw_realtime_sacu_pv_data.farmid)
    sacus: dict[int, float]         # SACU -> nameplate kW (raw_realtime_sacu_pv_data.rated_power)
    latitude: float
    longitude: float
    tilt: float                     # panel plane (deg); the weather table's global_tilted_irradiance is on it
    azimuth: float                  # deg, 180 = south

    @property
    def nameplate_mw(self) -> float:
        return sum(self.sacus.values()) / 1000


@dataclass(frozen=True)
class FreeFarm:
    """A FREE farm: its forecast is the PPA forecast at the same site, rescaled (research 2026-10-13: on uncurtailed
    slots FREE and PPA capacity factors agree within ~1 %; FREE's own power is held back by dispatch ~60 % of the
    time). FREE MW = PPA MW / ppa_mw x free_mw x site_factor."""
    name: str
    farmid: int
    siteid: int
    ppa_farmid: int                 # the PPA farm whose forecast is rescaled
    ppa_mw: float                   # PPA nameplate
    free_mw: float                  # FREE nameplate (sum of its SACUs)
    site_factor: float              # median FREE / PPA capacity-factor ratio, uncurtailed slots before 2026-08-12

    @property
    def multiplier(self) -> float:
        return self.free_mw / self.ppa_mw * self.site_factor


# PPA farms (integration_schema.farms market_status 1, checked 2026-10-13). Panel planes estimated from telemetry
# 2026-10-06. Target = ACTIVE power of all PPA SACUs (Radeni SACU 1 has a wrong available-power signal only).
FARMS = [
    Farm("radeni_ppa", "radeni", 30, 2, 503, {1: 6600.0, 2: 6600.0, 3: 6600.0, 4: 6600.0},
         47.1861165, 28.6455931, 34, 180),
    Farm("negureni_ppa", "negureni", 29, 1, 502, {3: 7260.0, 4: 7260.0}, 47.6332891, 28.5502817, 34, 180),
]
# FREE SACU nameplates (kW): Radeni 5, 6 = 6600, 7 = 7590, 8 = 7805; Negureni 1 = 7590, 2, 5, 6 = 7260.
# Valea Mare has no PPA part: not covered.
FREE_FARMS = [
    FreeFarm("radeni_free", 2, 2, 30, 26.4, 28.595, 1.0127),
    FreeFarm("negureni_free", 1, 1, 29, 14.52, 29.37, 1.0054),
]

# ---- plant data
PLANT_COLUMN = "active_power"
OVER_RATED = 1.05                   # a SACU reading above 1.05 x nameplate is treated as missing
MIN_COVER_MIN = 13.5                # a SACU slot needs readings spanning >= 13.5 of its 15 min, else missing
TRAIN_START = "2026-06-10"          # first local day used for training (the weather table starts 2026-06-08 00Z)

# ---- jobs
ISSUE_DELAY_MIN = 2                 # a forecast made late (catch-up, backfill) is stamped issue hour + 2 min
CATCHUP_MAX_H = 48                  # the hourly job fills at most the last 48 missed issue hours
DATA_WAIT_MIN = 10                  # the hourly job waits up to 10 min for plant data up to the hour start
MODEL_MAX_AGE_DAYS = 14             # the hourly job warns when the active model was trained longer ago
H1_FALLBACK_IDS = (112, 110, 111)  # 1-hour source without a value (plant data missing): the 48-hour forecast of
                                    # the same hour, first of these with all 4 quarters; logged, same id and name
WEATHER_OLD_H = 20                  # ... and when a weather run used is older than this at the issue (normal: < 14 h)
MIN_TRAIN_DAYS = 28                 # train refuses to save a farm with fewer days of plant data (>= half a day each)
MIN_RECENT_DAYS = 5                 # ... or fewer of them in the last 7 days before the training end
BLOCK_ANCHOR = "2026-08-12"         # backfill: models refitted every RETRAIN_DAYS from this local day
RETRAIN_DAYS = 7
DAYLIGHT_MW = 0.5                   # scoring: hours with actual >= 0.5 MW

# ---- writing
WRITE_RETRIES = 5                   # attempts per write; waits 15, 30, 60, 120 s between them
WRITE_RETRY_BASE_S = 15

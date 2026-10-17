# pv_inhouse_forecast

**Internally built PV forecasts**: our own models, trained on our plant data and public weather forecasts
(Open-Meteo).

PV power forecast for the PPA farms Radeni (farm 30) and Negureni (farm 29), and the FREE farms at the same sites
(farms 2 and 1, the PPA forecast rescaled). Every hour it writes 15-min forecasts in MW to
`integration_schema.forecast_internal`:

| family | id_source | source_name | what |
|---|---|---|---|
| h1 | 100 | `pv_1h_no_weather` | next hour: average of 4 models, no weather |
| h1 | 101 | `pv_1h_smart_persistence` | next hour: smart persistence |
| h1 | 102 | `pv_1h_open_meteo` | next hour: average of 4 models with ECMWF weather |
| h1 | 103 | `pv_1h_open_meteo_ecmwf_icon_eu` | as 102, weather = mean of ECMWF and ICON-EU |
| h48 | 110 | `pv_48h_open_meteo_ecmwf_ifs_arimax` | next 48 h: linear on ECMWF weather + ARMA error (< 6 h) |
| h48 | 111 | `pv_48h_open_meteo_ecmwf_ifs_xgboost` | next 48 h: same linear + XGBoost error |
| h48 | 112 | `pv_48h_open_meteo_ecmwf_icon_eu_arimax` | as 110, weather = mean of ECMWF and ICON-EU |

Every run is kept: the newest run of a slot has `is_current = 1`, older runs 0. Forecasts below 0 MW are set to 0.
If a 1-hour source has no value for an hour (plant data missing), the 48-hour forecast of that hour is written under
its id instead (112, else 110, else 111); the log says FALLBACK. The weather forecasts come from Open-Meteo; this
repo also collects them (`run.py weather`) into `integration_schema.open_meteo_forecast_hourly`.

## Install (Linux)

Python 3.12 or newer (Ubuntu 22.04's `python3` is 3.10: install 3.12), git, access to the production database and
to `*.open-meteo.com`. Install as the user that will run cron.

```bash
git clone https://github.com/Razorbill-team/pv_inhouse_forecast.git /opt/pv_inhouse_forecast
cd /opt/pv_inhouse_forecast
python3.12 -m venv .venv                 # Debian/Ubuntu: apt install python3.12-venv
.venv/bin/pip install -r requirements.txt
mkdir -p logs                            # cron writes logs/cron.log, so the folder must exist
cp .env.example .env && nano .env && chmod 600 .env    # fill every field (comments in .env.example)
.venv/bin/python run.py status
```

## First start

```bash
.venv/bin/python run.py weather          # weather runs of the last 7 days; the log must end "0 failure(s)"
.venv/bin/python run.py train h1 --end 2026-10-17     # --end = first local day NOT used (see below)
.venv/bin/python run.py train h48 --end 2026-10-17
.venv/bin/python run.py status           # shows the candidate versions
.venv/bin/python run.py promote h1 <version>
.venv/bin/python run.py promote h48 <version>
.venv/bin/python run.py hourly --dry-run # computes, saves a CSV in reports/, writes nothing (reads FORECAST_DB_URL)
```

`--end`: a model is never used for hours before its training end, so it must not be later than the newest hour
already in `forecast_internal` (the first deployment: 2026-10-17). If the log warns "issue hours ... before the
active model's training end", fill them: `run.py backfill FAMILY START END`, check the CSV, then add `--write`.

## Schedule (crontab -e, same user as the install)

```
50 * * * * cd /opt/pv_inhouse_forecast && flock -n /tmp/pv_weather.lock .venv/bin/python run.py weather > /dev/null 2>> logs/cron.log
2  * * * * cd /opt/pv_inhouse_forecast && flock -n /tmp/pv_hourly.lock  .venv/bin/python run.py hourly  > /dev/null 2>> logs/cron.log
```

The weather job runs at minute 50, so each weather run is stored before the hour the models may first use it.
The hourly job fills missed hours by itself (up to 48 h back), so a short outage needs nothing. A longer one:
`run.py backfill FAMILY START END`, check the CSV in `reports/`, then the same command with `--write` (only runs
not yet in the table are written; a gap between written hours is filled). A backfill refuses to run while the
weather of its days is incomplete: run `run.py weather FIRST LAST` first (the message says which days).

## Retraining (never automatic; a person decides)

```bash
.venv/bin/python run.py compare h1       # report in reports/: REPLACE / KEEP / NO CLEAR DIFFERENCE
.venv/bin/python run.py train h1         # if REPLACE: a new candidate on all data up to today
.venv/bin/python run.py promote h1 <version>
.venv/bin/python run.py rollback h1      # if needed: back to the version active before
```

`train` saves nothing if a farm has fewer than 28 days of plant data, or fewer than 5 of the last 7 days, or if any
training hour has no weather (the log says why; the active model stays). `status` shows how many days each version
learned from.

`compare` trains a test model on the data up to 14 days ago and scores it against the active model on the last
14 days (same hours, data neither model was trained on). The hourly job warns in the log when the active model is
older than 14 days.

## Open-Meteo licence

Without a key the free tier is used, which is for non-commercial use only: production needs the paid plan. Put its
key in `.env` as `OPEN_METEO_API_KEY`; the next `run.py weather` uses the customer address (its first log line says
which). The plan must include the Single Runs API (API Professional).

## Logs

`logs/run_YYYY-MM.log`, one line per step. `ERROR` = a family wrote nothing this hour (exit code 1). `WARN` = written,
but something needs a look (late plant data, old weather, FALLBACK, a source without rows, an old model).
`logs/cron.log` only gets what the log cannot catch (a crash before logging starts): it should stay empty. An hour
skipped because the previous run still held the lock leaves no line; the next hourly run fills it.

## Code

- `run.py` — every command
- `pv_forecast/settings.py` — farms, ids, limits: everything one may need to change
- `pv_forecast/families/h1.py`, `h48.py` — the models (one module per family; a new model = a new id_source)
- `pv_forecast/inputs.py` — plant data, weather and sun inputs
- `pv_forecast/writer.py` — the only code that writes forecasts (idempotent, PPA and FREE in one transaction)
- `pv_forecast/jobs.py`, `compare.py`, `registry.py` — hourly job, training, backfill, compare, saved models
- `pv_forecast/weather_collector.py` — Open-Meteo weather collector

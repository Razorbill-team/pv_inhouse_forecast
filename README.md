# pv_forecast

**Internally built PV forecasts**: our own models, trained on our plant data and public weather forecasts
(Open-Meteo). No vendor forecasts here; vendor forecasts (e.g. Meteologica, id 120) are loaded separately.

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
The weather forecasts come from Open-Meteo; this repo also collects them (`run.py weather`) into
`integration_schema.open_meteo_forecast_hourly`.

## Install (Linux)

Python 3.12 or newer, access to the production database and to open-meteo.com.

```bash
cd /opt/pv_forecast                      # this repository
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env && chmod 600 .env
.venv/bin/python run.py status
```

## First start

```bash
.venv/bin/python run.py train h1         # fit on all data up to today: saved as a candidate
.venv/bin/python run.py train h48
.venv/bin/python run.py status           # shows the candidate versions
.venv/bin/python run.py promote h1 <version>
.venv/bin/python run.py promote h48 <version>
.venv/bin/python run.py hourly --dry-run # computes, saves a CSV in reports/, writes nothing
```

## Schedule (crontab -e)

```
50 * * * * cd /opt/pv_forecast && flock -n /tmp/pv_weather.lock .venv/bin/python run.py weather > /dev/null 2>> logs/cron.log
2  * * * * cd /opt/pv_forecast && flock -n /tmp/pv_hourly.lock  .venv/bin/python run.py hourly  > /dev/null 2>> logs/cron.log
```

The weather job runs at minute 50, so each weather run is stored before the hour the models may first use it.
The hourly job fills missed hours by itself (up to 48 h back), so a short outage needs nothing. A longer one:
`run.py backfill FAMILY START END`, check the CSV in `reports/`, then the same command with `--write` (only runs
not yet in the table are written; a gap between written hours is filled).

## Retraining (never automatic; a person decides)

```bash
.venv/bin/python run.py compare h1       # report in reports/: REPLACE / KEEP / NO CLEAR DIFFERENCE
.venv/bin/python run.py train h1         # if REPLACE: a new candidate on all data up to today
.venv/bin/python run.py promote h1 <version>
.venv/bin/python run.py rollback h1      # if needed: back to the version active before
```

`train` saves nothing if a farm has fewer than 28 days of plant data, or fewer than 5 of the last 7 days
(the log says why; the active model stays). `status` shows how many days each version learned from.

`compare` trains a test model on the data up to 14 days ago and scores it against the active model on the last
14 days (same hours, data neither model was trained on). The hourly job warns in the log when the active model is
older than 14 days.

## Open-Meteo licence

Without a key the free tier is used (non-commercial only). With a paid plan, put the key in `.env` as
`OPEN_METEO_API_KEY`; the next `run.py weather` uses the customer address (its first log line says which). The plan
must include the Single Runs API (API Professional). The server needs access to `*.open-meteo.com`.

## Logs

`logs/run_YYYY-MM.log`, one line per step. `ERROR` = a family wrote nothing this hour (exit code 1). `WARN` = written,
but something needs a look (late plant data, old weather, a source without rows, an old model).

## Code

- `run.py` — every command
- `pv_forecast/settings.py` — farms, ids, limits: everything one may need to change
- `pv_forecast/families/h1.py`, `h48.py` — the models (one module per family; a new model = a new id_source)
- `pv_forecast/inputs.py` — plant data, weather and sun inputs
- `pv_forecast/writer.py` — the only code that writes forecasts (idempotent, PPA and FREE in one transaction)
- `pv_forecast/jobs.py`, `compare.py`, `registry.py` — hourly job, training, backfill, compare, saved models
- `pv_forecast/weather_collector.py` — Open-Meteo weather collector

# How the in-house PV forecast works

This page explains what the repository does and why, in plain words. Install steps, the cron lines and the
command list are in the [README](../README.md). Day-to-day operation is in [OPERATIONS.md](OPERATIONS.md).

Words used on this page:

- **Slot**: a 15-minute period, named by its start time (for example 12:15 = 12:15 to 12:30).
- **Issue hour**: the hour at which a forecast is made. The job runs once an hour.
- **Run**: one forecast made at one issue hour, for one farm and one source.
- **Source**: one forecasting method, with a fixed number (`id_source`). One number always means one method.
- **Capacity factor**: power divided by nameplate (0 = nothing, 1 = full power). The models work in capacity
  factor and convert to MW at the end.
- **Family**: a group of sources that are trained and run together. `h1` = next hour, `h48` = next 48 hours.

---

## 1. What is forecast

Power in **MW**, per 15-minute slot, for the **PPA** part of two farms: Radeni (farm 30, 26.4 MW) and Negureni
(farm 29, 14.52 MW). The **FREE** farms at the same sites (Radeni FREE = farm 2, Negureni FREE = farm 1) get the
PPA forecast rescaled (section 4). Farms, nameplates and panel angles are in `pv_forecast/settings.py`.

Results go to one table, `integration_schema.forecast_internal` (section 5).

---

## 2. The sources

### 1-hour family `h1` (sources 100-103, `pv_forecast/families/h1.py`)

At each issue hour H it forecasts the four quarters of H itself (H:00, H:15, H:30, H:45). It only uses plant data
from slots that started before H.

- **101 `pv_1h_smart_persistence`**: "the next 15 minutes look like the last 15 minutes, adjusted for the sun".
  It takes the latest measured capacity factor and scales it by how much the clear-sky sunshine changes between
  then and the target slot. This is the simplest reasonable forecast and a yardstick for the others.
- **100 `pv_1h_no_weather`**: the average of four members that use no weather forecast: smart persistence; a
  "clear-day curve" (the farm's own typical sunny-day shape, scaled to how the last 45 minutes compared to it);
  an ARIMAX time-series model on clear-sky sunshine; and an XGBoost model on sun position and recent plant output.
- **102 `pv_1h_open_meteo`**: the same four-member average, but the ARIMAX and XGBoost members also get the ECMWF
  weather forecast (irradiance: GHI, DHI and irradiance on the panel plane).
- **103 `pv_1h_open_meteo_ecmwf_icon_eu`**: as 102, but each weather value is the average of ECMWF and ICON-EU.
  If either weather model is missing for that hour, 103 has no value.

An average is only made when all four members have a value. A source writes rows for an hour only when all four
quarters have a value; otherwise it writes nothing for that hour (or uses the fallback, section 7).

### 48-hour family `h48` (sources 110-112, `pv_forecast/families/h48.py`)

At each issue hour H it forecasts every slot from H to H + 48 h (192 slots).

- **110 `pv_48h_open_meteo_ecmwf_ifs_arimax`**: a straight-line formula per farm on two ECMWF weather values:
  irradiance on the panel plane (GTI) and direct sunlight times cloud cover. For the first 6 hours it adds a
  correction learned from the formula's recent errors (an ARMA model on the last 2 days of errors).
- **111 `pv_48h_open_meteo_ecmwf_ifs_xgboost`**: the same straight-line formula, plus an XGBoost model that
  predicts the formula's error from weather and sun position. One XGBoost model serves both farms. Used at all
  lead times.
- **112 `pv_48h_open_meteo_ecmwf_icon_eu_arimax`**: as 110, but GTI and direct-times-cloud are each the average of
  ECMWF and ICON-EU. If no ICON-EU run is usable, 112 has no rows (110 and 111 are not affected).

If no ECMWF run is usable for an issue hour, that issue hour gets no `h48` rows at all; the other issue hours are
still written. Days without plant data do not stop `h48` (only the first-6-hours correction is then missing),
unless the plant tables have no rows at all in the 3 days it reads.

The exact formulas are in the appendix.

---

## 3. Inputs

### Plant data (`pv_forecast/inputs.py`, `capacity_factor()`)

- Column `active_power` of the PPA SACUs (inverter groups) of each farm, from
  `hist_production_rawdata.raw_realtime_sacu_pv_data` (archive) and `integration_schema.raw_realtime_sacu_pv_data`
  (live, about the last two weeks). Read only.
- Readings (about every 10 s) are averaged into 15-minute slots per SACU. The farm value is the sum over its SACUs
  divided by the farm nameplate.
- A SACU slot is **missing** when:
  - its readings do not span at least 13.5 of the 15 minutes (`MIN_COVER_MIN`), or it has no readings;
  - it is **frozen**: exactly the same non-zero average as the slot before;
  - it is **above nameplate**: more than 1.05 x the SACU nameplate (`OVER_RATED`);
  - it falls in the **DST hour**: the plant tables store local time without a time zone, so the repeated hour in
    October and the skipped hour in March cannot be placed in UTC. Both happen at night (about 03:00 local).
- If **any** SACU of a farm is missing in a slot, the whole farm slot is missing. Nothing is filled in.
- If the archive and the live table hold the same readings, the run stops instead of counting them twice.

### Weather (Open-Meteo, `pv_forecast/weather_collector.py`)

- Two weather models, both via Open-Meteo: **ECMWF IFS** and **DWD ICON-EU**. Runs start at 00, 06, 12 and
  18 UTC. Only these four ICON-EU runs a day are stored, because only they reach 72 hours.
- `run.py weather` stores every run, hourly values for 72 hours, for each site, in
  `integration_schema.open_meteo_forecast_hourly`. Rows are only added, never changed. Panel-plane irradiance is
  requested for each site's panel tilt and direction.
- **Which run is used at issue hour H** (no peeking at runs not yet published):
  - a run counts as published at **run start + 8 h** (ECMWF) or **run start + 5 h** (ICON-EU);
  - the models use the newest published run;
  - it must not be too old: at most **25 h** before H for `h1`, **30 h** for `h48`. Normal age is 8 to 14 h.
  - `h1` takes, per hour, the newest such run whose needed values are all present. `h48` uses a run only if at
    least 99 % of its irradiance values are present.
- Both families check that the stored panel plane (tilt, direction) is the farm's in `settings.py`; if not,
  the family stops for that hour.
- Weather values are hourly. All four quarters of an hour get the same weather value.

### Sun geometry (`inputs.sun()`, `inputs.sun_up()`)

Computed locally with the `pvlib` library, no network: sun position, clear-sky irradiance (what a cloudless sky
would give) and clear-sky irradiance on the panel plane, at the middle of each slot.

---

## 4. FREE farms

The FREE farms' own measured power is often held back by dispatch instructions, so it does not show what the sun
allows and is not a good training target. On slots where FREE was not held back, FREE and PPA at the same site
produced almost the same capacity factor. So the FREE forecast is the PPA forecast, rescaled:

FREE MW = PPA MW x (FREE nameplate / PPA nameplate) x site factor

The numbers are in `FREE_FARMS` in `settings.py` (Radeni: multiplier about 1.097; Negureni: about 2.034). FREE rows
use the same run, slot and source as the PPA row they come from, and are written in the same transaction
(`writer.rows()`). This applies to fallback rows too. Valea Mare has no PPA part and is not covered.

---

## 5. The table rows (`pv_forecast/writer.py`)

One row per (run, farm, source, slot). Columns:

| column | meaning |
|---|---|
| `last_forecast_datetime` | when the run was issued (UTC). On time: the clock time of the write, at most H:59. Made late: H + 2 min. |
| `slot_ts` | slot start, UTC |
| `data_forecast`, `ora_forecast` | the slot's local date and local hour (Europe/Chisinau); on the October clock-change night one local hour appears twice |
| `interval_minutes` | minute of the hour of the slot: 0, 15, 30 or 45 |
| `farmid`, `siteid` | business farm and site id |
| `id_source`, `source_name` | the source (section 2) |
| `forecasted_power_mw` | the forecast in MW, 3 decimals, never below 0 |
| `forecast_point_id` | farm + source + UTC slot start, e.g. `30_110_20261025T0015Z` |
| `is_current` | 1 = the newest run for this slot, 0 = an older run |
| `is_active_checkbox` | always 1 |
| `is_backfill` | 1 = the run was made late (by the hourly catch-up or by `run.py backfill`), 0 = made on time |

---

## 6. Runs and `is_current`

Every run is kept. Nothing already in the table is rewritten, except that `is_current` changes from 1 to 0 when a
newer run of the same slot arrives (that row's `updated_at` is then set to the time of the change). For each
(farm, source, slot) exactly one row has `is_current = 1`.

**Worked example**, Radeni PPA, source 110, slot 2026-10-20 12:00 UTC. A 48-hour run covers this slot if it was
issued between 2026-10-18 13:00 and 2026-10-20 12:00 UTC, so it is forecast 48 times:

| issue hour | row written | `is_current` after the write |
|---|---|---|
| 10-18 13:00 | yes | 1 |
| 10-18 14:00 | yes | 1 (the 13:00 row becomes 0) |
| ... | ... | ... |
| 10-20 12:00 | yes | 1 (the 11:00 row becomes 0) |
| 10-20 13:00 | no (this run starts at 13:00) | the 12:00 row stays 1 for good |

So a past slot keeps the last forecast made for it. A 1-hour source forecasts each slot only once, so its rows
are normally current from the start.

A late (backfill) run is added only if that run is not in the table yet. It becomes current only if the table
has no later run for that slot.

---

## 7. The hourly job, step by step (`run.py hourly`, `pv_forecast/jobs.py`)

Cron starts it at minute 2 of every hour (see README). For each family, **`h48` first, then `h1`** (h1 may fall
back on h48):

1. **Load the active model** (`saved_models/<family>/ACTIVE`). No active model: ERROR for that family. The
   other family still runs. If the model's training data ended more than 14 days ago, a WARN.
2. **Choose the issue hours**: from the hour after the newest issue hour already in the table, up to the current
   hour. At most 48 hours back (`CATCHUP_MAX_H`); older missed hours are left empty with a WARN. Never an issue
   hour before the model's training end (a WARN says which hours were skipped; both families also refuse such
   hours themselves). If the table has no rows of the family yet: only the last 48 hours, with a WARN. If none:
   "up to date".
3. **Wait for plant data** (only if the current hour is included, and only once per run): for each farm where
   the sun is up, wait up to 10 minutes in total until every SACU has reported up to the start of the hour. Still
   late: a WARN, then go on with what is there.
4. **Read inputs and forecast.** Night is 0 MW:
   - `h1`: the sun is below the horizon for the whole hour (checked every 5 minutes) gives 0 MW for all quarters;
   - `h48`: a slot with no clear-sky sunshine on the panel at its middle gives 0 MW.
   Negative forecasts are set to 0 MW.
5. **1-hour fallback** (`h1` only): a 1-hour source with no value at all for an issue hour (plant data or weather
   missing) gets the 48-hour forecast of the same four slots. It tries **112, then 110, then 111**, and uses the
   newest run in the table issued no later than the 1-hour run, only if it has all four quarters. The rows keep
   the 1-hour source's own id and name, so in the table they look like normal rows; only the log's `FALLBACK`
   line shows it (a deliberate choice). Because h48 runs first, the 48-hour run of the same hour is normally
   already in the table.
   If a family has no rows at all for its issue hours: ERROR `no rows for issues ...`, nothing written.
6. **Write** (one transaction per family, PPA and FREE together): only runs not yet in the table are
   inserted; older current rows of the same slots get `is_current = 0`; a check that no slot has two current rows.
   Anything wrong rolls the whole write back. A run (farm, source, issue hour) is never written twice.
   A failed write is tried 5 times (waits 15, 30, 60, 120 s). If all fail: ERROR, nothing written, and the next
   hourly run makes those hours again (catch-up).
7. **Log** per farm: input quality counts, the mean MW per source for the current hour, and WARN lines for
   problems (issue hours with no forecast at all, a source without rows, weather missing or over 20 h old).
   Last line: `done; failed: ...`. Exit code 1 if a family wrote nothing because of an error.

`--dry-run` does steps 1-5 and saves a CSV in `reports/` instead of writing. `--no-wait` skips step 3.

---

## 8. Saved models and retraining (`registry.py`, `jobs.train`, `compare.py`)

Models are trained once and saved; the hourly job never trains. A saved model is a folder
`saved_models/<family>/<version>/` with model files and `meta.json`. The version name is
`<first local day NOT used>_<save time UTC>`.

- **train** (`run.py train FAMILY [--end DAY]`): fits on all data from 2026-06-10 (`TRAIN_START`) to the local
  midnight starting DAY (default: today) and saves a **candidate**. The hourly job does not use it yet. Nothing is
  saved when:
  - a farm has fewer than 28 days with data (a day counts if at least half its slots are present);
  - a farm has fewer than 5 such days in the last 7 days before the end;
  - a time-series model did not converge;
  - weather is missing in the training data (both families): `h1` checks every hour with plant data, `h48`
    every issue hour, for ECMWF and ICON-EU. The message names the hours and says to run `run.py weather`.

  A model never forecasts issue hours before its training end: both families refuse them.
- **compare** (`run.py compare FAMILY [--days 14]`): an honest test of whether retraining helps.
  - It trains a *test* model on the data before the last 14 days (the *test window*).
  - The active model and the test model both forecast the test window. Neither was trained on it.
  - Score: hourly error (RMSE) in % of nameplate, daylight hours only (actual at least 0.5 MW), only hours where
    both have a value, per farm and source.
  - A difference is **clear** when it is at least **0.2 points** of nameplate and its 90 % uncertainty range
    (resampling the test days) does not include 0.
  - **REPLACE**: clearly better for some farm and source, clearly worse for none. **KEEP**: clearly worse
    anywhere. **NO CLEAR DIFFERENCE**: otherwise (retraining is harmless but not needed).
  - If the active model's training already reaches into the test window, it is too early to judge: KEEP.
  - The report goes to `reports/compare_<family>_<time>.md`.
- **promote** (`run.py promote FAMILY VERSION`): makes a saved version active. The switch is atomic and recorded
  in `saved_models/<family>/history.jsonl`. **rollback** goes back to the version active before; repeated
  rollbacks walk further back.

**Nothing is automatic.** A person runs compare, train and promote.

---

## 9. What this code does NOT do

- **It does not fill missing data.** A missing plant or weather value stays missing; the forecast for it is
  missing too (or the 1-hour fallback is used). There is no interpolation and no "use yesterday".
- **It does not retrain by itself.** It only warns when a model is older than 14 days.
- **It does not send alerts.** It writes log lines and returns exit code 1. Someone, or a monitoring tool, has to
  read them.
- It does not rewrite past forecasts. A slot that was forecast late or with a fallback keeps that forecast.
- The hourly catch-up does not go back more than 48 hours, and it does not fill a gap that lies before the newest
  written issue hour. `run.py backfill` does that (see OPERATIONS.md).

---

## Appendix: formulas

Notation: cf = capacity factor; T = target slot; h = slots between the newest plant slot used and T
(quarter q of hour H uses h = q + 1, so only slots before H are used); CS = clear-sky GHI; POA_cs = clear-sky
irradiance on the panel plane. All results are multiplied by the farm nameplate to get MW.

**101 smart persistence**: cf(T) = cf(T - h) x CS(T) / CS(T - h). When CS(T - h) < 50 W/m2: cf(T) = cf(T - h).

**Clear-day curve (member of 100/102/103)**: e(T) = 0.6 quantile of cf at the same time of day (+-1 slot) over
the last 14 days. Forecast = e(T) x [3 cf(T-h) + 2 cf(T-h-1) + cf(T-h-2)] / [3 e(T-h) + 2 e(T-h-1) + e(T-h-2)].
When e(T - h) < 0.1 (sunrise, sunset): the mean cf of the same slot over the last 7 days (at least 4 present).

**ARIMAX (member)**: cf = b x CS / max(CS) + ARMA(1,1) error, no constant. The weather version adds DHI, GTI and
GHI (each divided by its largest training value). Each hour: a 4-step forecast from the data before H. Needs at
least one day of gap-free inputs before H.

**XGBoost (member)**: one model per h = 1..4, target cf. Inputs: sun geometry at T (CS, zenith, azimuth, POA_cs,
angle of incidence); cf at T-h, T-h-1, T-h-3 and one day earlier; mean cf of the last hour; clear-sky index at
T-h; for 102/103 also the weather of T's hour. Stopping point chosen on the last 7 training days, then refit on all.

**100 / 102 / 103** = mean of smart persistence, clear-day curve, ARIMAX and XGBoost (no weather / ECMWF /
mean of ECMWF and ICON-EU).

**110**: MW(T) = b1 x GTI + b2 x DNI x cloud / 100, fitted per farm by least squares, no constant. For leads under
6 h, plus an ARMA(2,0,2) forecast of the error, using only the errors of the 2 days before H.

**111**: the 110 formula (without ARMA) + nameplate x XGBoost(error / nameplate). XGBoost inputs: GTI / POA_cs,
GHI / CS, DHI / CS (only when the clear-sky value is at least 20 W/m2), cloud cover, temperature, wind speed,
humidity, zenith, angle of incidence, a Radeni yes/no flag (to become a real farm id before a third farm). Recent data weigh more (half-life 60 days). Trained
on runs issued every 3rd hour.

**112**: as 110, with GTI and DNI x cloud each the mean of ECMWF and ICON-EU.

**FREE**: MW_FREE = MW_PPA x free_mw / ppa_mw x site_factor (`settings.FREE_FARMS`).

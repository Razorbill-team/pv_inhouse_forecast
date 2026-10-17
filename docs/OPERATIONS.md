# Operations runbook

For people who keep the in-house PV forecast running. How it works: [HOW_IT_WORKS.md](HOW_IT_WORKS.md). Install,
cron lines and first start: [README](../README.md).

All commands below run in the install folder (README: `/opt/pv_inhouse_forecast`) as the cron user:

```bash
cd /opt/pv_inhouse_forecast
PY=.venv/bin/python
```

Times in the log are **UTC**. Local time (Europe/Chisinau) is UTC + 3 h in summer, + 2 h in winter.

---

## 1. Daily check (5 minutes)

```bash
LOG=logs/run_$(date -u +%Y-%m).log
grep "done; failed" $LOG | tail -24            # one line per hour; normal: "failed: none"
grep "weather: done" $LOG | tail -24           # one line per hour; normal: "0 failure(s)"
grep -E " (WARN|ERROR) " $LOG | tail -50       # anything to look at
cat logs/cron.log                              # should be empty
$PY run.py status                              # active models and their age
```

Every hour should have exactly one `hourly run for ...` line followed by a `done` line. A missing hour is usually
fine (the next run catches it up), but several missing hours in a row means cron is not running.

**What a normal hour looks like** (taken from a real run; numbers vary):

```
06:02:36Z INFO  hourly run for 2026-10-17 06:00Z (09:00 local); code 503994e
06:02:40Z INFO  h48: model 2026-10-16_20261017T054221Z; issues 10-17 06Z .. 10-17 06Z (1)
06:02:49Z INFO  h48 radeni_ppa: inputs {'plant': {'slots': ..., 'missing': 0, 'partial_slot': 0, 'frozen': 0, 'over_rated': 0, 'dst_dropped': 0}, 'pairs': {...}}
06:02:49Z INFO  h48 radeni_ppa 06Z: 110 1.51, 111 1.57, 112 1.23 MW (mean over its 192 slots)
06:02:55Z INFO  wrote 2304 new rows of 2304 (0 of runs already in the table), 2256 older rows set not current; by (farm, source) {...}
06:02:56Z INFO  h1: model 2026-10-16_20261017T054210Z; issues 10-17 06Z .. 10-17 06Z (1)
06:03:31Z INFO  h1 radeni_ppa 06Z: 100 2.64, 101 1.74, 102 2.45, 103 2.38 MW (mean over its 4 slots)
06:03:33Z INFO  wrote 64 new rows of 64 (0 of runs already in the table), 0 older rows set not current; by (farm, source) {...}
06:03:33Z INFO  done; failed: none; warnings and errors: 0
06:50:02Z INFO  weather: Open-Meteo free tier (no API key)
06:50:03Z INFO  weather icon_eu: newest long run 2026-10-17 00:00Z; checking 29 runs
06:50:03Z INFO  weather radeni icon_eu: 28 of 29 runs stored, 1 to download
06:50:04Z INFO    radeni icon_eu run 2026-10-17 00:00Z: 71 rows stored
06:50:08Z INFO  weather: done, 0 failure(s)
```

Most weather hours download nothing (`29 of 29 runs stored, 0 to download`): a new run appears every 6 hours.

Normal numbers for one issue hour: `h48` writes 192 slots x 3 sources x 4 farms (2 PPA + 2 FREE) = **2,304 rows**;
`h1` writes 4 slots x 4 sources x 4 farms = **64 rows**. Night hours are written too (0 MW). In the inputs line,
`missing`, `frozen`, `over_rated` should be 0 or small; `dst_dropped` is above 0 only after the October clock change
(the repeated night hour). On that October night the table has the same local date + hour (`data_forecast`,
`ora_forecast`) twice; `slot_ts` and `forecast_point_id` stay unique. This is correct, not a duplicate.

`FALLBACK` rows look like normal rows in the table (same source id); only the log shows `FALLBACK`. This was
decided on purpose.

---

## 2. Log messages and what to do

`ERROR` = a family (or the weather job) wrote nothing this time; exit code 1. `WARN` = written, but look at it.
In the table, `<fam>` is `h1` or `h48`, `<farm>` is `radeni_ppa` or `negureni_ppa`.

### Hourly forecast and backfill (`run.py hourly` / `backfill`, `jobs.py`, `writer.py`)

| log message | meaning | what to do |
|---|---|---|
| `<fam>: no active model (run.py train, then run.py promote)` ERROR | no model is promoted for this family | train and promote (section 3.5) |
| `<fam>: no rows of its sources in the table yet: making the last 48 h only` WARN | first run on an empty table | earlier days, if wanted: backfill (section 3.1) |
| `<fam>: active model ... was trained on data up to N days ago` WARN | the model is older than 14 days | section 3.7; repeats every hour until a newer model is promoted |
| `<fam>: newest written issue ... is more than 48 h ago; hours before ... stay empty` WARN | outage longer than 48 h | backfill the gap (section 3.1) |
| `<fam>: issue hours ... are before the active model's training end: left empty` WARN | a model was promoted whose training data reaches past the newest written hour | backfill those hours (section 3.1) |
| `<farm>: SACU [...] have not reported up to HH:MMZ after the wait` WARN | plant data late after 10 min of waiting | once: nothing. Every hour: section 3.3 |
| `FALLBACK <farm> MM-DD HHZ: 10x <- 11x (run ...)` WARN | a 1-hour source had no value; the 48-hour forecast was written under its id | once in a while: nothing. Many: section 3.3 / 3.2 |
| `<farm> MM-DD HHZ: 10x has no value and no 48-hour fallback ... no rows` WARN | no 1-hour value and no complete 48-hour run in the table | that hour stays empty for that source; check plant data and weather |
| `<fam> <farm>: N of M issues have no forecast at all (newest ...): no usable weather run` WARN | some issue hours got no rows from any source (mostly `h48`: no usable ECMWF run) | section 3.2 |
| `<fam> <farm>: no rows from <id> <name> for N of M issues ...` WARN | a source wrote nothing for some issue hours | plant data or weather missing: sections 3.2, 3.3 |
| `<fam> <farm>: weather_run_utc (or icon_run_utc) missing for some issues (is run.py weather running?)` WARN | no usable weather run for some issue hours | check the weather job (section 3.2) |
| `<fam> <farm>: weather_run_utc (or icon_run_utc) up to N h older than the issue (...)` WARN | the weather used is over 20 h old (normal: under 14 h) | section 3.2. Right after a catch-up over a weather gap it is expected once |
| `<fam>: FAILED RuntimeError: no rows for issues ... (no usable weather run or plant data; ...)` ERROR | nothing could be made for any issue hour; nothing written | read the WARN lines above it; sections 3.2, 3.3 |
| `<fam>: FAILED RuntimeError: cannot read the forecast table: server ... NOT REACHABLE` ERROR | the forecast database cannot be reached | section 3.4 |
| `<fam>: FAILED reading production: server ... NOT REACHABLE (...)` or `... database error on ...` ERROR | the plant / weather database cannot be reached, or a query failed (e.g. timed out) | section 3.4 |
| `<fam>: FAILED ... active model trained on data up to ..., after the current hour; promote another` ERROR | the active model was trained with a future `--end` | promote an older version, or `rollback` |
| `<fam>: FAILED ... no plant rows for SACUs ...` ERROR | no plant data at all in the read window | section 3.3 |
| `<fam>: FAILED ... archive and live table overlap` ERROR | the same plant readings are in both plant tables | tell the data owner; do not work around it |
| `<fam>: FAILED ... weather table repeats (run, hour) rows` ERROR | duplicate rows in the weather table | tell the data owner / fix the table; the collector never writes duplicates |
| `h48: FAILED ... no ecmwf_ifs rows for ...` or `no ECMWF run published by any issue` ERROR | no usable ECMWF weather at all in the hours read | section 3.2 |
| `<fam>: FAILED ... weather panel plane is not tilt ..., azimuth ...` ERROR | weather rows were collected for different panel angles than `settings.py` (both families check) | `settings.py` and `weather_collector.SITES` disagree: fix the code, re-collect |
| `<fam>: FAILED ... forecasting issues before the training end` ERROR | the model was asked for hours before its training end | should not happen (step 2 skips them); report it |
| `<fam>: FAILED ... data do not cover ...`, `sun / weather join changed the slots`, `sun geometry does not cover every slot`, `negative or non-finite forecasts` or `rows repeat a (farm, source, run, slot)` ERROR | a code bug | report it with the log line; the next hour retries |
| `<fam> block <day>: [...] (those forecasts are missing)` WARN | backfill: a model of that week did not converge | check the CSV before `--write` |
| `write attempt k/5 failed: ...; retry in N s` (or `; giving up`) WARN | a write failed, being retried; the text may be a database error or `rolled back, nothing written` | nothing if a later attempt says `wrote` |
| `write FAILED: nothing written; the next hourly run recomputes these hours` ERROR | all 5 write attempts failed | section 3.4 |
| `STOPPED: ...` ERROR | the command refused to run; the text says why | read the text; see below |
| `CRASH <Type>: ...` ERROR | an unexpected error | report it with the log line |

### Weather job (`run.py weather`, `weather_collector.py`)

| log message | meaning | what to do |
|---|---|---|
| `  <Error>: ...; retrying in N s` / `  HTTP 5xx ...; retrying in N s` WARN | Open-Meteo or network hiccup, being retried | nothing if the run is then stored |
| `  <site> <model> run ...: just published and incomplete {...}; next hour` WARN | the newest run is not complete yet | nothing; the next hour stores it |
| `  <site> <model> run ...: not served yet (just published), next hour` WARN | same | nothing |
| `  <site> <model> run ...: N rows stored, missing values (NULL) {...}` WARN | a run was stored with some empty values | nothing if rare; the models skip incomplete runs |
| `  <site> <model> run ...: not in Open-Meteo's archive, skipped` WARN | a backfill asked for a run Open-Meteo does not have | nothing; that run cannot be had |
| `  <site> <model> run ... FAILED: Open-Meteo does not serve it` ERROR | a recent run is not served (hourly mode) | retried next hour; if it lasts for hours, check Open-Meteo status |
| `  <site> <model> run ... FAILED: <Type>: ...` ERROR | download or insert failed for one run | retried next hour (the job looks back 7 days) |
| `weather <model>: meta.json not readable` ERROR | cannot ask Open-Meteo for its newest run | check network to `*.open-meteo.com` |
| `weather <model>: no published run from <day> on` ERROR | a backfill asked for days in the future | fix the dates |
| `weather: STOPPED Open-Meteo request limit used up` ERROR | free-tier or plan request limit reached | wait; production needs the paid key (section 3.6) |
| `weather: STOPPED, weather database: server ... NOT REACHABLE` (or `database error on ...`) ERROR | the weather table cannot be read or written | section 3.4 |
| `weather: done, N failure(s)` ERROR | summary: N runs failed | see the lines above it |

### `STOPPED:` messages you may see

| text contains | what to do |
|---|---|
| `environment variable X is not set` / `expected schema.table` / `has no host; refusing to write` | fix `.env` (see `.env.example`) |
| `points at production host` / `is the production database: only ... there` | the write guard: on production only `forecast_internal` and `open_meteo_forecast_hourly` may be written. Fix `FORECAST_TABLE` / `WEATHER_TABLE` |
| `train <fam>: NOT saved: [...]` | too little plant data, a model did not converge, or `... without weather ...; fill the weather table: run.py weather FIRST LAST`; the active model stays. Wait for data, or collect the weather days named, then train again |
| `--end ... is in the future` | use today or an earlier day |
| `no saved version` / `already active` / `no previous version to roll back to` / `no active model to compare against` | check `run.py status` |
| `--days must be at least ...` / `weather: last day is before first day` | fix the command arguments |
| `backfill <fam>: N issue hours have no usable weather run ...; run.py weather A B` | run the `run.py weather` command it prints, then the backfill again |
| `start must be on or after 2026-08-12` | backfill cannot start earlier (`BLOCK_ANCHOR`) |
| `... not found: run the backfill without --write first` | run the backfill without `--write` first, with the same days |
| `...: columns are not [...] or values are missing` / `write failed` | the backfill CSV was edited or the write failed: rerun the dry run, then `--write` |

---

## 3. Common situations

### 3.1 The server was down for 3 days

1. Restart cron / the server. The weather job looks back 7 days by itself, so its next run fills the weather.
   Check: `grep "weather: done" logs/run_*.log | tail -3`.
2. The next hourly run fills the last 48 hours by itself and logs `newest written issue ... more than 48 h ago`.
3. Fill the rest with a backfill, using **local days** (END = first day NOT included):

```bash
$PY run.py backfill h48 2026-10-14 2026-10-16      # dry run: CSV in reports/ and scores on screen
$PY run.py backfill h1  2026-10-14 2026-10-16
# look at reports/backfill_<fam>_<start>_<end>.csv, then:
$PY run.py backfill h48 2026-10-14 2026-10-16 --write
$PY run.py backfill h1  2026-10-14 2026-10-16 --write
```

Runs already in the table are skipped, so overlapping days are safe. Backfill rows have `is_backfill = 1`. Note:
a backfill refits the models week by week on the data before each week; it does not use the active model, and it
does not apply the 1-hour fallback.

### 3.2 The weather collector is broken

Signs: `weather: done, N failure(s)` or `weather: STOPPED`, `weather_run_utc ... older than the issue` or
`... missing for some issues` warnings, then 102/103/112 missing, and after 30 h `h48: FAILED ... no rows for
issues` (the 1-hour 100/101 are still written).

```bash
$PY run.py weather                          # run it by hand and read the lines it prints
$PY run.py weather 2026-10-14 2026-10-16    # store every run of these UTC days (if more than 7 days were lost)
```

Check network access to `*.open-meteo.com` and the request limit. When the weather is back, the hourly job
catches up the last 48 hours. But it only starts after the newest written issue hour, so a source missing behind
it (e.g. 102/103 while the weather was down and no 48-hour fallback existed, or 112) is filled **only** by
`run.py backfill` for those days (section 3.1; it writes only the runs still missing, and refuses until the
weather of those days is complete).

### 3.3 Plant data missing

Signs: `SACU [...] have not reported` every hour, `no rows from 100/101/...`, many `FALLBACK` lines.

- Check the plant table (read only):

```sql
select farmid, sacu_no, max(t_stamp) as last_reading     -- t_stamp is local time
from integration_schema.raw_realtime_sacu_pv_data
where farmid in (502, 503) and t_stamp > now()::timestamp - interval '1 day'
group by 1, 2 order by 1, 2;
```

  (502 = Negureni, 503 = Radeni: telemetry ids. PPA SACUs: Radeni 1-4, Negureni 3-4.)
- Tell the data owner. Nothing to do in this repo: the 1-hour sources fall back on the 48-hour forecast, and
  the 48-hour forecast keeps running on weather (only its first 6 h correction needs recent plant data). Only if
  the plant tables have no rows at all in the days read (`h48`: 3, `h1`: 16) does the family stop with
  `no plant rows for SACUs`.
- Missing data is never filled in. Past hours keep what was written.
- If the outage lasts, `train` will refuse to save a new model (fewer than 5 of the last 7 days).

### 3.4 Database unreachable

Signs: `NOT REACHABLE`, `database error on`, `write attempt k/5 failed`, `FAILED reading production`,
`weather: STOPPED, weather database`. The message names the host:port/database.

- Check from the server, e.g. `pg_isready -h 10.50.0.1 -p 5432` if installed, or `psql` with the URL in `.env`.
- Outage under 48 hours: nothing to do; the next successful hourly run catches up.
- Longer: section 3.1.

### 3.5 A model looks bad: compare, retrain, roll back

```bash
$PY run.py compare h1          # report in reports/compare_h1_<time>.md: REPLACE / KEEP / NO CLEAR DIFFERENCE
$PY run.py train h1            # only if REPLACE: a new candidate on all data up to today
$PY run.py status              # shows the new version name
$PY run.py promote h1 <version>
$PY run.py rollback h1         # if the new one is worse: back to the version active before
```

Promote and rollback take effect at the next hourly run. Forecasts already written are not changed.

### 3.6 The Open-Meteo key arrives

1. Put it in `.env`: `OPEN_METEO_API_KEY=...` (the plan must include the Single Runs API).
2. Run `$PY run.py weather`. Its first line must say `weather: Open-Meteo paid plan (API key set)` and the last
   `weather: done, 0 failure(s)`. The key never appears in the log.

### 3.7 Model older than 14 days

The hourly WARN `trained on data up to N days ago` appears. Run `compare` (section 3.5). REPLACE: train and
promote. NO CLEAR DIFFERENCE: retraining is harmless; train and promote if you want the warning gone. KEEP: keep
the model; the warning continues every hour until a newer model is promoted.

### 3.8 Adding a new farm

Research is needed first if the farm is not like Radeni/Negureni PPA: trackers instead of fixed panels; its
measured power is often held back by dispatch (like the FREE farms); a FREE farm (needs a measured `site_factor`);
or a farm with less than about a month of clean plant data. The 48-hour XGBoost has only a Radeni yes/no flag
(`FARM_FLAG_SITE` in `pv_forecast/families/h48.py`): before a third farm is added to `h48`, that flag must become a
real farm id (a code change), otherwise the new farm is treated like Negureni.

For a plain fixed-panel PPA farm (code changes are made in the repository, then pulled on the server):

1. `pv_forecast/settings.py`: add a `Farm(...)` to `FARMS` (name, weather site key, business farm and site id,
   telemetry farm id, SACU numbers with nameplate kW, latitude, longitude, tilt, azimuth with 180 = south).
2. `pv_forecast/weather_collector.py`: add the site to `SITES` if it is new (same tilt; azimuth there uses
   0 = south).
3. Right after pulling the change: the saved models have no files for the new farm, so the hourly job fails for
   each family until new models are trained and promoted. So at once: collect the new site's weather from the
   start of training (`$PY run.py weather 2026-06-08 <today>`; `train` refuses while hours lack weather), then
   `$PY run.py train h1 --end <day>`, `$PY run.py train h48 --end <day>`, and promote both. `<day>` = the local
   day of the newest forecast already in the table (a model never makes hours before its training end, so a later
   day would leave those hours empty).
4. Check the next hourly run's log and the SQL below. The new farm starts from the next hour; earlier hours need
   a backfill.

---

## 4. Checking the table (read only)

```sql
-- 1. Newest run per source (should be within the last hour)
select id_source, source_name, max(last_forecast_datetime) as newest_run
from integration_schema.forecast_internal
where id_source in (100, 101, 102, 103, 110, 111, 112)
  and last_forecast_datetime > now() - interval '3 days'
group by 1, 2 order by 1;

-- 2. Exactly one current row per slot (should return no rows)
select farmid, id_source, slot_ts, count(*)
from integration_schema.forecast_internal
where id_source in (100, 101, 102, 103, 110, 111, 112)
  and is_current = 1 and slot_ts > now() - interval '7 days'
group by 1, 2, 3 having count(*) > 1;

-- 3. Rows per issue hour and source (normal: 16 per h1 source, 768 per h48 source, i.e. 4 farms each)
select date_trunc('hour', last_forecast_datetime) as issue_hour, id_source, count(*) as n_rows,
       max(is_backfill) as late
from integration_schema.forecast_internal
where id_source in (100, 101, 102, 103, 110, 111, 112)
  and last_forecast_datetime > now() - interval '1 day'
group by 1, 2 order by 1 desc, 2;
```

---

## 5. Where things are

| path | what |
|---|---|
| `saved_models/<family>/<version>/` | one saved model: model files + `meta.json` (training end, data checks, code version) |
| `saved_models/<family>/ACTIVE` | the version the hourly job uses (change it only with promote / rollback) |
| `saved_models/<family>/history.jsonl` | every promote and rollback |
| `logs/run_YYYY-MM.log` | one line per step of every command, UTC; a new file each month, never deleted |
| `logs/cron.log` | only crashes before logging starts; should be empty |
| `reports/dry_run_<fam>_<hour>.csv` | output of `hourly --dry-run` |
| `reports/backfill_<fam>_<start>_<end>.csv` (+ `_detail.csv`) | backfill dry run; `--write` writes this file |
| `reports/compare_<fam>_<time>.md` | compare reports |
| `.env` | database URLs, table names, Open-Meteo key (never commit) |
| `pv_forecast/settings.py` | farms, ids and all limits |

`reports/` and `logs/` are never cleaned up by the code; delete old files by hand if space runs low.

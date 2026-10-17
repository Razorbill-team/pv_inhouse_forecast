# Verification and simulation report

What was checked before this code went to production (2026-10-17), how, and the result. Every check was run by
an independent reviewer (a separate agent that did not write the code), with its own scripts. Production was only
read, except the one approved catch-up write in section 3. Tests that write ran against a local PostgreSQL copy of
the production forecast table.

Summary: the forecasts are the same, number for number, as the earlier tested code; no forecast uses information
from the future; the table stays consistent under failures. The bugs found on the way are fixed and re-tested
(section 8).

## 1. Same numbers as the earlier tested code ("golden" check)

The models were rewritten into this repository from earlier, separately tested packages. Both were run on the same
days with the same data and their output rows compared.

| Family | Window | Rows compared | Largest difference |
|---|---|---|---|
| 1-hour (100-103), PPA and FREE | issues 2026-10-06 21Z .. 10-16 15Z | 15,040 | 0.000 MW (after the floor at 0 MW that the old output did not yet have) |
| 48-hour (110-112), PPA | issues 2026-10-06 21Z .. 10-16 14Z | 134,784 | 0.000 MW, also for leads < 6 h |
| 48-hour (110-112), FREE | same | 145,152 | 0.000 MW |

All row columns (dates, hours, interval, ids, names, forecast_point_id, is_backfill) were identical. The 48-hour
error model is now saved as parameters and rebuilt at forecast time; on real data this gives exactly (0.0) the
same forecasts as the old way, also with gaps in the data.

## 2. The weather collector

Two days of Open-Meteo runs were collected into a local table and compared with the production weather table:
all 4,260 shared rows identical in every column (missing values included); a second run inserts nothing; inserts
are about 20-30x faster than before.

## 3. The production catch-up write (2026-10-17 06:35 UTC)

The first production write of this code filled the gap since 2026-10-16 15Z: 37,824 rows (48-hour: 16 issue hours;
1-hour: 15 issue hours; PPA and FREE). Checked afterwards by two reviewers who recomputed the rows with the old,
separately tested code:

- 1-hour: 960 of 960 rows identical (0.000 MW), all columns.
- 48-hour: 36,864 of 36,864 rows identical (0.000 MW), all columns, both lead groups.
- Old rows: none deleted, no value changed; only is_current 1 -> 0 on the 2,256 rows that newer runs replaced.
- Whole table: every slot has exactly one current row and it is the newest run; one run per (farm, source, issue
  hour); no negative values; FREE = PPA x multiplier exactly.
- Weather used: every run was published (by the rule run + 8 h ECMWF / run + 5 h ICON-EU) before its issue hour.
- A rerun right after wrote nothing.

## 4. No information from the future ("time travel" test)

For 15 issue hours of every kind (sunrise, midday, evening, night, the two cloudiest days, the hour just before
and just after a new weather run becomes usable) both families were run three ways:

- A: normal, with all data in the database today (including plant data after the hour);
- B: with everything that did not exist at that hour removed (plant data from the hour on; weather runs not yet
  published);
- C: with that future data replaced by random noise.

A, B and C gave identical forecasts (difference 0.0) for every hour, source and farm. A control test that also
changed one slot from just BEFORE the hour moved the forecasts by up to 18 MW, so the test does detect a leak.
Training was tested the same way: replacing everything after the training end with noise left every model
parameter and every prediction unchanged (0.0). The retraining check (`compare`) fits its test model only on data
before its test window, and the active model must be older.

## 5. Failure simulations (local database)

| Situation | Result |
|---|---|
| Missed hours (server off), simulated at several times | caught up, stamped issue + 2 min, is_backfill = 1; the on-time hour stamped with the real time |
| Plant data missing: last slot, 3 hours, 1 day | 1-hour falls back to the 48-hour forecast (FALLBACK in the log); 48-hour writes everything |
| Plant data missing 2-3 days | crashed the 48-hour job -> fixed, re-tested (section 8) |
| Weather 20 h / 26 h old | normal / old-weather warning, 1-hour weather sources fall back |
| Weather 31 h old | crashed the 48-hour job -> fixed, re-tested (section 8) |
| Weather 80 h old, then back | no 48-hour rows (ERROR, exit 1); forecasting resumes when the weather returns |
| Forecast database unreachable at start | clear "NOT REACHABLE", nothing written, next run catches up |
| Connection killed in the middle of a write | retried; nothing half-written (row count, current flags, ids unchanged) |
| Table locked by another session | retried, nothing written, next run catches up |
| Two hourly runs at the same time | no duplicates, no slot with two current rows |
| Clock change 2026-10-25 (simulated) | no hour skipped or doubled; all row columns correct (see note) |
| Empty table (new install) | last 48 h made; `backfill` then fills earlier days and skips what exists |

Note on the clock change: on that night the local hour 03:00 occurs twice, so two slots share the same local date,
hour and interval; `slot_ts` (UTC) and `forecast_point_id` stay unique.

## 6. Settings and identifiers

Farm and site ids, SACU lists, nameplates (26.40 / 14.52 MW PPA; 28.595 / 29.37 MW FREE), coordinates, panel
planes, source ids and source names, model settings and the row layout were checked against the production
registry tables, the telemetry ratings, the rows already in the table and the research code: all match.

## 7. Install from scratch

A fresh clone was installed in a new Python 3.12 environment from `requirements.txt` (all packages install; the
same versions exist for Linux), every command was run as the README says, and training ran on real data. The gaps
found in the README (create `logs/` before cron, `--end` for the first training, Python 3.12 on Ubuntu, collect
weather first) are fixed.

## 8. Bugs found and fixed

| Found by | Problem | Fixed |
|---|---|---|
| golden check | a backfill starting on a block boundary forecast one hour twice | yes |
| code review | a backfill could not fill a gap older than the newest run; a late live run could hide the next hour; future `--end` blocked forecasting; rollback could swing between two versions | yes |
| write rehearsal | a backfill could add a second run for an hour that already had a live run | yes (one run per farm, source and issue hour) |
| rehearsal | the 1-hour fallback took the previous hour's 48-hour run instead of the same hour's | yes |
| failure simulation | 48-hour crashed after 2+ days without plant data, and with only 31 h old weather | yes |
| documentation review | training with a weather gap stopped with a crash message (1-hour) or not at all (48-hour) | yes: clear "NOT saved" for both |

Re-test of the fixed code: see the appendix below.

## 9. The shipped models and how long they stay good

The models in `saved_models/` (trained on data before 2026-10-17) were rebuilt by the earlier, separately tested
code: every parameter and every XGBoost model is identical (difference 0.0; the XGBoost files byte for byte), and
both give identical forecasts for 2026-10-17.

How long can a model run without retraining? The same test days were forecast by models trained 0, 7, 14, 21, 28
and 42 days before them (two test windows: 2026-09-23 .. 10-02 and 2026-10-07 .. 10-16):

| Model age | Change in error (points of nameplate), sources 100, 102, 103, 110, 112 |
|---|---|
| 1 week | -0.1 .. +0.14 (no real difference) |
| 2 weeks | -0.2 .. +0.17 (no real difference) |
| 3-6 weeks | 1-hour sources: +0.3 .. +1.1 in one window, none in the other; 48-hour 110 / 112: at most +0.2 |

So a model runs safely for 1-2 weeks; the log reminds after 14 days (`MODEL_MAX_AGE_DAYS`). Through November,
retrain every 1-2 weeks so that the lower sun of late autumn enters the training data. Source 111 (XGBoost)
changes by up to 0.8 points from one training to the next whatever its age; 112 remains the better 48-hour
source. Snow, frost or fog on panels never occurred in the training data.

## Appendix: re-test of the final code

The failure simulations and a real-data check were run again on the fixed code:

| Check | Result |
|---|---|
| Plant data missing 15 min / 3 h / 1 day | all 48-hour rows written; 1-hour FALLBACK lines; exit 0 |
| Plant data missing 2-3 days (one or both farms) | no crash; 111 writes all 192 slots, 110 / 112 all but the first 6 h; 1-hour falls back; exit 0 |
| Weather 20 h / 26 h old | normal / old-weather warning; 1-hour weather sources fall back; exit 0 |
| Weather 31 h and 80 h old | no 48-hour rows: a clear ERROR ("no rows ..."), exit 1; 1-hour continues |
| Weather returns | missed hours caught up, no warnings |
| Empty table | WARN "no rows ... in the table yet"; last 48 h made |
| Catch-up; two runs at once | pass; the second run writes nothing |
| Table rules after every scenario | one current row per slot, current = newest run, one run per issue hour, no negatives, FREE exact; rows set not current carry `updated_at` |
| Real data, 2026-10-14 (both families) | byte-identical to the rows made before the fixes (55,296 + 1,536 rows) |
| Training with the new data and weather checks | passes on real data; saves the candidate |

Known limit (not an error): when 110 / 112 have no first-6-hours correction (plant data missing for 2 days),
the log does not say so separately; the rows for those hours are simply not written.

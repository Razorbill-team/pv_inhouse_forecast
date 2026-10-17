"""Model families. Each family is one module with the same interface, so the jobs (jobs.py) treat them alike:

    NAME        "h1" / "h48": folder name under saved_models/ and the name on the command line
    SOURCES     output column -> (source_name, id_source) written to the forecast table
    HORIZON     slots [issue, issue + HORIZON) are forecast at each hourly issue
    HISTORY     data needed before the first issue

    read(eng, start, end)            -> data: every input of every PPA farm, plant data from `start`, enough for
                                        issues in [start + HISTORY, end) (and their actuals as far as they exist);
                                        data[farm]["cf"] = plant capacity factor per slot (data checks);
                                        data[farm]["counts"] = input quality counts for the log
    fit(data, fit_end)               -> fitted: trained on what was known before fit_end only
    weather_holes(data)              -> list of words: training hours without the weather the models need
    problems(fitted)                 -> list of words: a fit that must not be used (e.g. not converged)
    save(fitted, folder) / load(folder)
    summary(fitted)                  -> dict for meta.json and the log
    predict(data, fitted, issues)    -> one row per (farm, issue, slot): farm, issue_utc, slot_utc, actual_mw,
                                        nameplate_mw, the SOURCES columns in MW (missing = no row is written; a value
                                        below 0 is set to 0 and the value before kept as <column>_raw), and extras

Adding a model: give it a new id_source in a family's SOURCES (or a new family module listed below). Every id means
one method forever; never reuse an id.
"""

from . import h1, h48

FAMILIES = {f.NAME: f for f in (h1, h48)}

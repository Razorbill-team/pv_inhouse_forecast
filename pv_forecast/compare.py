"""Scores, and `compare`: does retraining help? An honest test, advisory only (promote stays a manual step).

compare(family, days=14):
  W = today - days (local midnight). The active model must be trained on data before W (else: too early, KEEP).
  A TEST candidate is fitted on the data before W only, so it has (W - active training end) more days of data.
  Both forecast the same issue hours [W, today) from the same data: neither is scored on its own training data.
  Score: hourly RMSE in % of nameplate, daylight hours (actual >= DAYLIGHT_MW), only hours where both have a value,
  per farm and source. Uncertainty: the difference is bootstrapped over the days forecast (2,000 resamples, 90 %).
  A difference is CLEAR when its interval excludes 0 and it is at least CLEAR_PP points of nameplate.
  Suggestion: REPLACE if the candidate is clearly better for some farm / source and clearly worse for none; KEEP if
  it is clearly worse for any; otherwise NO CLEAR DIFFERENCE (retraining is harmless but not needed).
If REPLACE: `run.py train <family>` makes the real candidate (all data to today), then `run.py promote`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import db, registry
from . import settings as S
from .log import log

MIN_DAYS = 3                        # a shorter test window says nothing
CLEAR_PP = 0.2                      # smaller differences (points of nameplate) do not matter in practice
BOOT = 2000
LEADS = [("next hour", 0, 1), ("0-6 h", 0, 6), ("6-24 h", 6, 24), ("24-48 h", 24, 48)]


def local_day(t: pd.Timestamp) -> str:
    return f"{t.tz_convert(S.LOCAL_TZ):%Y-%m-%d}"


def hourly(detail: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Hourly means of `cols` and actual_mw per (farm, issue, hour) where all 4 quarters of every column have a
    value, daylight hours only; with lead_h (hour - issue) and the local day forecast."""
    d = detail.assign(hour_utc=detail["slot_utc"].dt.floor("h"))
    g = d.groupby(["farm", "nameplate_mw", "issue_utc", "hour_utc"])
    use = ["actual_mw", *cols]
    h = g[use].mean().where(g[use].count() == 4).reset_index()
    h = h[h[use].notna().all(axis=1) & (h["actual_mw"] >= S.DAYLIGHT_MW)].copy()
    h["lead_h"] = (h["hour_utc"] - h["issue_utc"]) / pd.Timedelta(hours=1)
    h["day"] = h["hour_utc"].dt.tz_convert(S.LOCAL_TZ).dt.date
    return h


def rmse_pct(err: pd.Series, plate: float) -> float:
    return 100 * float(np.sqrt((err ** 2).mean())) / plate


def _leads(h: pd.DataFrame) -> list[tuple[str, pd.Series]]:
    out = [(name, (h["lead_h"] >= a) & (h["lead_h"] < b)) for name, a, b in LEADS]
    return [("all leads", h["lead_h"] >= 0)] + [(n, m) for n, m in out if m.any() and not m.all()]


def scores(detail: pd.DataFrame, sources: dict) -> pd.DataFrame:
    """Hourly daylight RMSE, % of nameplate, per farm and lead; every source on the same hours."""
    cols = [c for c in sources if detail[c].notna().any()]
    h = hourly(detail, cols)
    res = []
    for (farm, plate), d in h.groupby(["farm", "nameplate_mw"]):
        for lead, m in _leads(d):
            x = d[m]
            res.append({"farm": farm, "lead": lead, "hours scored": len(x),
                        **{f"{sources[c][1]} {c}": round(rmse_pct(x[c] - x["actual_mw"], plate), 2) for c in cols}})
    return pd.DataFrame(res)


def _boot(x: pd.DataFrame, plate: float, rng) -> tuple[float, float]:
    """90 % interval of RMSE(candidate) - RMSE(active), resampling whole days forecast."""
    sq = x.assign(a=(x["active"] - x["actual_mw"]) ** 2, c=(x["candidate"] - x["actual_mw"]) ** 2)
    per_day = sq.groupby("day")[["a", "c"]].agg(["sum", "count"])
    a_sum, c_sum, n = (per_day[("a", "sum")].to_numpy(), per_day[("c", "sum")].to_numpy(),
                       per_day[("a", "count")].to_numpy())
    pick = rng.integers(0, len(per_day), size=(BOOT, len(per_day)))
    diff = 100 * (np.sqrt(c_sum[pick].sum(1) / n[pick].sum(1)) - np.sqrt(a_sum[pick].sum(1) / n[pick].sum(1))) / plate
    return float(np.quantile(diff, 0.05)), float(np.quantile(diff, 0.95))


def _verdict(lo: float, hi: float, diff: float) -> str:
    """Clear: the interval excludes 0 and the difference is at least CLEAR_PP."""
    if hi < 0 and diff <= -CLEAR_PP:
        return "candidate better"
    if lo > 0 and diff >= CLEAR_PP:
        return "active better"
    return "no clear difference"


def run(fam, days: int) -> str:
    """The compare command; returns the suggestion. Writes reports/compare_<family>_<time>.md."""
    from .jobs import today_utc, utc

    if days < MIN_DAYS:
        raise SystemExit(f"--days must be at least {MIN_DAYS}")
    version = registry.active(fam.NAME)
    if version is None:
        raise SystemExit(f"{fam.NAME}: no active model to compare against")
    fitted_a, meta_a = registry.load(fam, version)
    a_end = pd.Timestamp(meta_a["trained_to_utc"])
    today = today_utc()
    w = today - pd.Timedelta(days=days)
    if a_end >= w:
        log(f"{fam.NAME}: active model {version} uses data before {local_day(a_end)}, inside the last {days} days: "
            "too early to judge, a test model would be the same model. Suggestion: KEEP")
        return "KEEP"
    extra = (w - a_end) / pd.Timedelta(days=1)
    log(f"{fam.NAME}: test days {local_day(w)} .. {local_day(today - pd.Timedelta(days=1))}; active {version} "
        f"(data before {local_day(a_end)}); test model with {extra:.0f} more days of data")
    data = fam.read(db.engine(S.READ_DB_ENV), utc(S.TRAIN_START), today)
    fitted_c = fam.fit(data, w)
    issues = pd.date_range(w, today, freq="h", inclusive="left")
    det_a, det_c = fam.predict(data, fitted_a, issues), fam.predict(data, fitted_c, issues)
    key = ["farm", "issue_utc", "slot_utc"]
    if not det_a[key].equals(det_c[key]):
        raise ValueError("active and candidate forecasts are not on the same slots")

    rng = np.random.default_rng(0)
    notes, verdicts, table = [], [], []
    for col, (name, id_source) in fam.SOURCES.items():
        d = det_a[[*key, "actual_mw", "nameplate_mw"]].assign(active=det_a[col], candidate=det_c[col])
        h = hourly(d, ["active", "candidate"])
        only = int((det_a[col].notna() != det_c[col].notna()).sum())
        for (farm, plate), x in h.groupby(["farm", "nameplate_mw"]):
            for lead, m in _leads(x):
                y = x[m]
                ra, rc = rmse_pct(y["active"] - y["actual_mw"], plate), rmse_pct(y["candidate"] - y["actual_mw"], plate)
                lo, hi = _boot(y, plate, rng) if y["day"].nunique() >= 2 else (np.nan, np.nan)
                v = _verdict(lo, hi, rc - ra)
                main = lead == "all leads"
                table.append({"source": f"{id_source} {name}", "farm": farm,
                              "lead": lead if main else f"{lead} (info)", "hours scored": len(y),
                              "active %": round(ra, 2), "test model %": round(rc, 2), "diff": round(rc - ra, 2),
                              "90% interval": f"{lo:+.2f} .. {hi:+.2f}", "verdict": v if main else f"({v})"})
                if main:
                    verdicts.append(v)
        if only:
            notes.append(f"- {id_source}: {only} slots have a value from one model only (left out of the score)")
    if "active better" in verdicts:
        suggestion, plain = "KEEP", "the retrained test model was clearly worse somewhere: keep the active model."
    elif "candidate better" in verdicts:
        suggestion, plain = "REPLACE", "the retrained test model was clearly better and nowhere clearly worse."
    else:
        suggestion = "NO CLEAR DIFFERENCE"
        plain = f"retraining changed the error by less than {CLEAR_PP} points of nameplate: no action needed."
    t = pd.DataFrame(table)
    worst = t.loc[t["lead"] == "all leads", "diff"].abs().max()
    report = "\n".join([
        f"# compare {fam.NAME}, {pd.Timestamp.now(tz=S.LOCAL_TZ):%Y-%m-%d %H:%M} local", "",
        f"**Suggestion: {suggestion}**: {plain} (largest change on all leads: {worst:.2f} points)", "",
        f"- Active model: {version}, data before {local_day(a_end)}.",
        f"- Test model: same code, data before {local_day(w)} ({extra:.0f} more days than the active model).",
        f"- Test days: {local_day(w)} .. {local_day(today - pd.Timedelta(days=1))}, daylight hours, both models on "
        "the same hours; neither was trained on them.",
        "- Error = hourly RMSE in % of nameplate; diff = test model - active (negative = test model better).",
        f"- Clear = the 90 % interval excludes 0 and the diff is at least {CLEAR_PP} points. The suggestion uses the "
        "'all leads' rows; lead rows marked (info) are for information only.", "",
        "```", t.to_string(index=False), "```", "", *notes,
        *(f"- test model fit problem: {b}" for b in fam.problems(fitted_c))])
    if suggestion == "REPLACE":
        report += (f"\n\nNext: `python run.py train {fam.NAME}`, then `python run.py promote {fam.NAME} <version>`. "
                   "The decision stays with a person.")
    S.REPORT_DIR.mkdir(exist_ok=True)
    path = S.REPORT_DIR / f"compare_{fam.NAME}_{pd.Timestamp.now(tz='UTC'):%Y%m%dT%H%MZ}.md"
    path.write_text(report, encoding="utf-8")
    print(report)
    log(f"{fam.NAME}: compare suggestion {suggestion}; report {path}")
    return suggestion

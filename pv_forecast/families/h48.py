"""48-hour-ahead family: at each hour H, every 15-min slot of [H, H + 48 h) for every PPA farm (sources 110-112).

As researched in 3_train_test_model/h48_ahead (ladder.py, re-selection 2026-10-13; weather_models.py 2026-10-15):
  110  linear GTI + DNI x cloud (no intercept, per farm), plus an ARMA(2,0,2) forecast of its error for leads < 6 h
  111  the same linear model, plus an XGBoost forecast of its error (one model for both farms)
  112  as 110, but GTI and DNI x cloud are each the mean of ECMWF IFS and DWD ICON-EU
Weather: the newest ECMWF run published by H (run + 8 h) and the newest ICON-EU long run (run + 5 h), each at most
MAX_WEATHER_AGE_H old. No ECMWF run: no rows for that issue; no ICON-EU run: no 112 rows (110 / 111 not affected). Sun down at the slot middle: 0 MW.

pairs()  one row per (issue H, slot T): lead, the weather of T's hour from the runs above, sun geometry at T, the
         actual MW (for fitting and scoring only) and the model inputs.
fit()    on the pairs known before b0 (issued before b0 and slot ended by b0), sun up, actual and inputs present.
         The ARMA is fitted on the linear model's error per slot (the run newest at the slot's own hour).
predict  the ARMA error forecast at H is filtered on the errors of [H - 2 d, H) only, parameters fixed.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .. import inputs
from .. import settings as S

NAME = "h48"
SLOT = pd.Timedelta(minutes=15)
HORIZON = pd.Timedelta(hours=48)
HISTORY = pd.Timedelta(days=3)      # pairs start 1 day after the data; the ARMA needs the 2 days before H
SOURCES = {"arimax": ("pv_48h_open_meteo_ecmwf_ifs_arimax", 110),
           "xgboost": ("pv_48h_open_meteo_ecmwf_ifs_xgboost", 111),
           "arimax_ecmwf_icon": ("pv_48h_open_meteo_ecmwf_icon_eu_arimax", 112)}

# ---- weather (table column -> model name)
WEATHER_COLS = {"global_tilted_irradiance": "om_gti", "ghi": "om_ghi", "dhi": "om_dhi", "dni": "om_dni",
                "cloud_cover": "om_cloud_cover", "temperature_2m": "om_temperature_2m",
                "wind_speed_10m": "om_wind_speed_10m", "relative_humidity_2m": "om_relative_humidity_2m"}
RUN_CHECK_COLS = ["om_gti", "om_ghi", "om_dhi"]   # a run is usable if >= MIN_COMPLETE of these values are present
MIN_COMPLETE = 0.99
PUBLISH_DELAY_H = 8
ICON_COLS = {"global_tilted_irradiance": "ic_gti", "ghi": "ic_ghi", "dhi": "ic_dhi", "dni": "ic_dni",
             "cloud_cover": "ic_cloud_cover"}
ICON_RUN_CHECK_COLS = ["ic_gti", "ic_ghi", "ic_dhi"]
ICON_PUBLISH_DELAY_H = 5            # long runs published ~3-4 h after their start, + 1 h margin
MAX_WEATHER_AGE_H = 30              # a run older than this at the issue is not used (normal age: up to 14 h)

# ---- models
FARM_FLAG_SITE = "radeni"           # XGBoost farm flag: 1 for radeni, 0 for negureni
LINEAR_FEATURES = {"arimax": ["om_gti", "dni_x_cloud"],                  # dni_x_cloud = om_dni x om_cloud_cover / 100
                   "arimax_ecmwf_icon": ["mean_gti", "mean_dni_x_cloud"]}  # ECMWF and ICON-EU averaged
ARMA_ORDER = (2, 0, 2)
ARMA_ERR_DAYS = 2
ARMA_MAX_LEAD_H = 6
XGB_FEATURES = ["gti_r", "ghi_r", "dhi_r", "om_cloud_cover", "om_temperature_2m", "om_wind_speed_10m",
                "om_relative_humidity_2m", "zenith", "aoi", "farm_id"]
RATIO_MIN = 20                      # gti_r = om_gti / poa_clearsky etc., missing below 20 W/m2
XGB_PARAMS = dict(n_estimators=50, learning_rate=0.0360, max_depth=3, min_child_weight=1994, subsample=0.80,
                  colsample_bytree=0.80, reg_lambda=27.8, objective="reg:squarederror", tree_method="hist",
                  random_state=42, n_jobs=8)
XGB_MONOTONE = {"gti_r": 1}
XGB_HALF_LIFE_D = 60                # sample weight 0.5 ** (age in days / 60)
XGB_TRAIN_EVERY_H = 3               # training rows: runs issued every 3rd hour


# ------------------------------------------------------------------ data

def usable_runs(wx: pd.DataFrame, cols: list[str], check: list[str]) -> tuple[pd.Series, dict, list]:
    """(run times sorted, run -> hours x `cols` frame, runs left out): a run is usable if >= MIN_COMPLETE of its
    `check` values are present."""
    frames, bad = {}, []
    for run, g in wx.groupby("run_utc"):
        if g[check].notna().to_numpy().mean() >= MIN_COMPLETE:
            frames[run] = g.set_index("slot_start_utc")[cols]
        else:
            bad.append(run)
    return pd.Series(sorted(frames), dtype="datetime64[ns, UTC]"), frames, bad


def pairs(farm: S.Farm, mw: pd.Series, geo: pd.DataFrame, wx: pd.DataFrame, wx_ic: pd.DataFrame,
          issues: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    """Pairs of `issues` (see the module docstring). Returns (pairs, counts)."""
    cols, ic_cols = list(WEATHER_COLS.values()), list(ICON_COLS.values())
    run_times, frames, bad = usable_runs(wx, cols, RUN_CHECK_COLS)
    published = run_times + pd.Timedelta(hours=PUBLISH_DELAY_H)
    if wx_ic.empty:
        ic_times, ic_frames, ic_bad = pd.Series([], dtype="datetime64[ns, UTC]"), {}, []
    else:
        ic_times, ic_frames, ic_bad = usable_runs(wx_ic, ic_cols, ICON_RUN_CHECK_COLS)
    ic_published = ic_times + pd.Timedelta(hours=ICON_PUBLISH_DELAY_H)
    out, no_run, no_icon = [], 0, 0
    for h in issues:
        i = published.searchsorted(h, side="right") - 1          # newest run published by h, not too old
        if i < 0 or h - run_times.iloc[i] > pd.Timedelta(hours=MAX_WEATHER_AGE_H):
            no_run += 1
            continue
        run = run_times.iloc[i]
        slots = pd.date_range(h, h + HORIZON, freq=SLOT, inclusive="left")
        f = pd.DataFrame(frames[run].reindex(slots.floor("h")).to_numpy(), columns=cols)
        f.insert(0, "slot_utc", slots)
        f.insert(0, "issue_utc", h)
        f["run_utc"] = run
        j = ic_published.searchsorted(h, side="right") - 1
        if j < 0 or h - ic_times.iloc[j] > pd.Timedelta(hours=MAX_WEATHER_AGE_H):
            no_icon += 1
            f[ic_cols], f["ic_run_utc"] = np.nan, pd.NaT
        else:
            f[ic_cols] = ic_frames[ic_times.iloc[j]].reindex(slots.floor("h")).to_numpy()
            f["ic_run_utc"] = ic_times.iloc[j]
        out.append(f)
    if not out:
        raise ValueError(f"{farm.name}: no ECMWF run published by any issue {issues[0]} .. {issues[-1]}")
    p = pd.concat(out, ignore_index=True)
    p["ic_run_utc"] = pd.to_datetime(p["ic_run_utc"], utc=True)
    p["lead_h"] = (p["slot_utc"] - p["issue_utc"]) / pd.Timedelta(hours=1)
    g = geo.reindex(p["slot_utc"])
    if g["poa_clearsky"].isna().any():
        raise ValueError(f"{farm.name}: sun geometry does not cover every slot")
    for c in geo.columns:
        p[c] = g[c].to_numpy()
    p["actual_mw"] = mw.reindex(p["slot_utc"]).to_numpy()
    p["farm"], p["nameplate_mw"] = farm.name, farm.nameplate_mw
    p["farm_id"] = float(farm.site == FARM_FLAG_SITE)
    p["dni_x_cloud"] = p["om_dni"] * p["om_cloud_cover"] / 100
    p["mean_gti"] = (p["om_gti"] + p["ic_gti"]) / 2
    p["mean_dni_x_cloud"] = (p["dni_x_cloud"] + p["ic_dni"] * p["ic_cloud_cover"] / 100) / 2
    p["gti_r"] = (p["om_gti"] / p["poa_clearsky"]).where(p["poa_clearsky"] >= RATIO_MIN)
    p["ghi_r"] = (p["om_ghi"] / p["cs_ghi"]).where(p["cs_ghi"] >= RATIO_MIN)
    p["dhi_r"] = (p["om_dhi"] / p["cs_ghi"]).where(p["cs_ghi"] >= RATIO_MIN)
    counts = {"issues": len(issues), "issues_without_run": no_run, "runs_usable": len(run_times),
              "runs_left_out": [str(b) for b in bad], "icon_issues_without_run": no_icon,
              "icon_runs_usable": len(ic_times), "icon_runs_left_out": [str(b) for b in ic_bad],
              "pairs": len(p), "pairs_without_weather": int(p[LINEAR_FEATURES["arimax"]].isna().any(axis=1).sum()),
              "pairs_without_2wx": int(p[LINEAR_FEATURES["arimax_ecmwf_icon"]].isna().any(axis=1).sum())}
    return p, counts


def read(eng, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """Per farm: the pairs of every issue hour in [start + 1 day, end), and counts. Actuals exist only up to now."""
    now = pd.Timestamp.now(tz="UTC").floor("15min")
    issues = pd.date_range(start + pd.Timedelta(days=1), end, freq="h", inclusive="left")
    out = {}
    for farm in S.FARMS:
        cf, counts = inputs.capacity_factor(eng, farm, start, min(end + HORIZON, now))
        geo = inputs.sun(farm, pd.date_range(start, end + HORIZON, freq=SLOT, inclusive="left"))
        wx = inputs.weather_runs(eng, farm, start, end + HORIZON, "ecmwf_ifs", WEATHER_COLS)
        wx_ic = inputs.weather_runs(eng, farm, start, end + HORIZON, "icon_eu", ICON_COLS, required=False)
        p, pc = pairs(farm, cf * farm.nameplate_mw, geo, wx, wx_ic, issues)
        out[farm.name] = {"cf": cf, "pairs": p, "counts": {"plant": counts, "pairs": pc}}
    return out


# ------------------------------------------------------------------ models

def known_before(p: pd.DataFrame, b0: pd.Timestamp) -> pd.Series:
    """Pairs usable for fitting a model used from b0 on: issued before b0 and the target slot ended by b0."""
    return (p["issue_utc"] < b0) & (p["slot_utc"] + SLOT <= b0)


def errors(p: pd.DataFrame, beta: np.ndarray, feats: list[str]) -> pd.Series:
    """Linear model error per 15-min slot (actual - linear), with the weather of the run newest at the slot's own
    hour. Gap-free 15-min index; missing where the actual or an input is."""
    own = p[p["issue_utc"] == p["slot_utc"].dt.floor("h")]
    u = pd.Series(own["actual_mw"].to_numpy() - own[feats].to_numpy() @ beta,
                  index=pd.DatetimeIndex(own["slot_utc"]))
    return u.asfreq("15min")


def fit_arimax(p: pd.DataFrame, b0: pd.Timestamp, feats: list[str]) -> dict:
    """Linear model on `feats` and the ARMA on its errors before b0: {"beta", "arma" (params) or None, "info"}.
    beta None when no row can be fitted (e.g. no ICON-EU run before b0)."""
    from statsmodels.tsa.arima.model import ARIMA

    m = known_before(p, b0) & (p["poa_clearsky"] > 0) & p["actual_mw"].notna() & p[feats].notna().all(axis=1)
    if not m.any():
        return {"beta": None, "arma": None, "info": {"linear_rows": 0, "arma_converged": False}}
    x, y = p.loc[m, feats].to_numpy(), p.loc[m, "actual_mw"].to_numpy()
    beta = np.linalg.solve(x.T @ x, x.T @ y)
    u = errors(p, beta, feats)
    info = {"linear_rows": int(m.sum()), "beta": dict(zip(feats, beta.round(6).tolist()))}
    try:
        res = ARIMA(u[u.index < b0], order=ARMA_ORDER, trend="n").fit()
    except Exception as exc:                    # noqa: BLE001 - a failed ARMA must not stop the other models
        return {"beta": beta.tolist(), "arma": None,
                "info": {**info, "arma_error": f"{type(exc).__name__}: {str(exc)[:200]}", "arma_converged": False}}
    ok = bool(res.mle_retvals["converged"])
    params = {k: float(v) for k, v in res.params.items()}
    info |= {"arma": {k: round(v, 4) for k, v in params.items()}, "arma_converged": ok}
    return {"beta": beta.tolist(), "arma": params if ok else None, "info": info}


def fit(data: dict, fit_end: pd.Timestamp) -> dict:
    """Every model on what is known before fit_end: per farm and ARIMAX model {beta, arma, info}; one XGBoost."""
    import xgboost as xgb

    out = {"trained_to_utc": str(fit_end), "farms": {}}
    rows = []
    for name, d in data.items():
        p = d["pairs"]
        out["farms"][name] = {m: fit_arimax(p, fit_end, feats) for m, feats in LINEAR_FEATURES.items()}
        beta = np.array(out["farms"][name]["arimax"]["beta"])            # XGBoost corrects the 110 linear model
        yn = (p["actual_mw"] - p[LINEAR_FEATURES["arimax"]].to_numpy() @ beta) / p["nameplate_mw"]
        r = (known_before(p, fit_end) & (p["poa_clearsky"] > 0) & yn.notna()
             & (p["issue_utc"].dt.hour % XGB_TRAIN_EVERY_H == 0))
        w = 0.5 ** (((fit_end - p.loc[r, "slot_utc"]) / pd.Timedelta(days=1)) / XGB_HALF_LIFE_D)
        rows.append((p.loc[r, XGB_FEATURES], yn[r], w))
    xs = pd.concat([r[0] for r in rows], ignore_index=True)
    ys = pd.concat([r[1] for r in rows], ignore_index=True)
    ws = pd.concat([r[2] for r in rows], ignore_index=True)
    mono = tuple(XGB_MONOTONE.get(c, 0) for c in XGB_FEATURES)
    out["xgboost"] = xgb.XGBRegressor(**XGB_PARAMS, monotone_constraints=mono).fit(xs, ys, sample_weight=ws)
    out["xgboost_rows"] = len(xs)
    return out


def problems(fitted: dict) -> list[str]:
    return [f"{farm} {m} ARMA did not converge or had nothing to fit" for farm, f in fitted["farms"].items()
            for m in LINEAR_FEATURES if not f[m]["info"]["arma_converged"]]


def summary(fitted: dict) -> dict:
    return {"trained_to_utc": fitted["trained_to_utc"], "xgboost_rows": fitted["xgboost_rows"],
            **{farm: {m: v["info"] for m, v in f.items()} for farm, f in fitted["farms"].items()}}


def save(fitted: dict, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "linear_arma.json").write_text(json.dumps(
        {"trained_to_utc": fitted["trained_to_utc"], "xgboost_rows": fitted["xgboost_rows"],
         "farms": fitted["farms"]}, indent=1))
    fitted["xgboost"].save_model(folder / "xgboost.json")


def load(folder: Path) -> dict:
    import xgboost as xgb

    out = json.loads((folder / "linear_arma.json").read_text())
    out["xgboost"] = xgb.XGBRegressor()
    out["xgboost"].load_model(folder / "xgboost.json")
    return out


# ------------------------------------------------------------------ forecast

def arma_errors(params: dict, u: pd.Series, issues: pd.DatetimeIndex) -> pd.DataFrame:
    """Per issue H: the ARMA forecast of the error for [H, H + ARMA_MAX_LEAD_H), filtered on the errors of
    [H - ARMA_ERR_DAYS, H) only, parameters fixed. An issue with no error in its window has no rows."""
    from statsmodels.tsa.arima.model import ARIMA

    steps = int(pd.Timedelta(hours=ARMA_MAX_LEAD_H) / SLOT)
    out = []
    for h in issues:
        w = u[(u.index >= h - pd.Timedelta(days=ARMA_ERR_DAYS)) & (u.index < h)]
        if w.notna().sum() == 0:
            continue
        mod = ARIMA(w.to_numpy(), order=ARMA_ORDER, trend="n")
        f = mod.filter(np.array([params[n] for n in mod.param_names])).forecast(steps)
        out.append(pd.DataFrame({"issue_utc": h, "slot_utc": pd.date_range(h, periods=steps, freq=SLOT),
                                 "err": np.asarray(f)}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["issue_utc", "slot_utc", "err"])


def _predict_farm(p_all: pd.DataFrame, f: dict, xgb_model, issues: pd.DatetimeIndex) -> pd.DataFrame:
    p = p_all[p_all["issue_utc"].isin(issues)].reset_index(drop=True)
    key = pd.MultiIndex.from_frame(p[["issue_utc", "slot_utc"]])
    short = p["lead_h"] < ARMA_MAX_LEAD_H
    out = p[["farm", "issue_utc", "slot_utc", "actual_mw", "nameplate_mw", "lead_h", "run_utc", "ic_run_utc"]].rename(
        columns={"run_utc": "weather_run_utc", "ic_run_utc": "icon_run_utc"})
    for model, feats in LINEAR_FEATURES.items():
        sfx = "" if model == "arimax" else "_2wx"
        beta, params = f[model]["beta"], f[model]["arma"]
        if beta is None:                                        # nothing was fitted (no ICON-EU before training end)
            out[f"linear{sfx}"], out[f"arma_err{sfx}"], out[model] = np.nan, np.nan, np.nan
            continue
        beta = np.array(beta)
        lin = pd.Series(p[feats].to_numpy() @ beta, index=p.index)
        u = errors(p_all, beta, feats)                          # errors before each issue, from the data read
        e = arma_errors(params, u, issues if params is not None else issues[:0])   # ARMA not converged: missing
        err = pd.Series(e.set_index(["issue_utc", "slot_utc"])["err"].reindex(key).to_numpy(), index=p.index)
        out[f"linear{sfx}"], out[f"arma_err{sfx}"] = lin, err.where(short)
        out[model] = lin + out[f"arma_err{sfx}"].where(short, 0.0)
    out["xgboost"] = out["linear"] + xgb_model.predict(p[XGB_FEATURES]) * p["nameplate_mw"]
    night = p["poa_clearsky"] <= 0
    out.loc[night, ["linear", "arimax", "xgboost"]] = 0.0
    if f["arimax_ecmwf_icon"]["beta"] is not None:              # 112 at night only where an ICON-EU run exists
        out.loc[night & p["ic_run_utc"].notna(), ["linear_2wx", "arimax_ecmwf_icon"]] = 0.0
    for m in SOURCES:                                           # power cannot be negative (user 2026-10-13)
        out[f"{m}_raw"] = out[m]
        out[m] = out[m].where(~(out[m] < 0), 0.0)
    return out


def predict(data: dict, fitted: dict, issues: pd.DatetimeIndex) -> pd.DataFrame:
    """Every slot of [H, H + 48 h) of every issue H and farm, in MW. Issues before the training end are refused."""
    if (issues < pd.Timestamp(fitted["trained_to_utc"])).any():
        raise ValueError("forecasting issues before the training end")
    out = []
    for farm in S.FARMS:
        p = data[farm.name]["pairs"]                   # an issue without an ECMWF run has no pairs: no rows
        out.append(_predict_farm(p, fitted["farms"][farm.name], fitted["xgboost"], issues))
    cols = ["farm", "issue_utc", "slot_utc", "actual_mw", "nameplate_mw", *SOURCES]
    df = pd.concat(out, ignore_index=True)
    return df[[*cols, *[c for c in df.columns if c not in cols]]]

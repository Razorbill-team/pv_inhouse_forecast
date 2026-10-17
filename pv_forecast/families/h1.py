"""1-hour-ahead family: at each hour H, the 4 quarter hours of H for every PPA farm (sources 100-103).

As researched in 3_train_test_model/h1_ahead (PV_MARKET=ppa) and run in production since 2026-10-13:
  101  smart persistence             cf(T-h) x clear-sky(T) / clear-sky(T-h)
  100  average of smart persistence, clear-day curve, ARIMAX (clear sky) and XGBoost (no weather)
  102  average of smart persistence, clear-day curve, ARIMAX + weather and XGBoost + weather (ECMWF IFS)
  103  as 102, each weather input the mean of ECMWF IFS and DWD ICON-EU (missing if either is missing)
Quarter q of H comes from horizon h = q + 1 slots, so plant data only from slots that start before H are used.
An average needs all 4 members; a source gets rows for H only if all 4 quarters have a value. Night (sun below the
horizon all hour): 0 MW. Weather of hour H: the newest run with run + delay <= H and H - run <= 25 h.

  member              formula / model
  smart_persistence   plain persistence where clear-sky(T-h) < CSI_MIN_CLEARSKY
  clear_day_curve     persistence on the farm's own clear-day shape: 0.6 quantile of the same slot (+-1) over 14
                      days, weighted ratio of the last 3 slots; climatology at sunrise / sunset
  arimax              ARMA(1,1) errors around beta x clear-sky GHI (scaled by its training maximum), no constant;
                      each hour a 4-step forecast from data up to H with the parameters fixed
  arimax_weather*     the same on (clear-sky GHI, dhi, gti, ghi); missing with < 1 day of gap-free weather before H
  xgboost*            one model per horizon: sun and panel geometry at T, plant lags up to T-h [, weather of T's hour]
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .. import inputs
from .. import settings as S

NAME = "h1"
HORIZON = pd.Timedelta(hours=1)
HISTORY_DAYS = 16                   # clear-day curve needs 14 days + 1 slot, climatology 7
HISTORY = pd.Timedelta(days=HISTORY_DAYS)
SOURCES = {"average": ("pv_1h_no_weather", 100),
           "smart_persistence": ("pv_1h_smart_persistence", 101),
           "average_weather": ("pv_1h_open_meteo", 102),
           "average_weather_icon": ("pv_1h_open_meteo_ecmwf_icon_eu", 103)}

# ---- weather (table column -> model name); ECMWF om_*, ICON-EU ic_*, their means mx_*
WEATHER_COLS = {"dhi": "om_dhi", "global_tilted_irradiance": "om_gti", "ghi": "om_ghi"}   # W/m2, hourly means
ICON_COLS = {k: "ic_" + v.removeprefix("om_") for k, v in WEATHER_COLS.items()}
ECMWF_DELAY_H, ICON_DELAY_H = 8, 5  # a run is usable for hour H when run + delay <= H
MAX_WEATHER_AGE_H = 25
ARIMAX_MIN_SLOTS = 96               # ARIMAX + weather needs >= 1 day of gap-free weather before H

# ---- models
HORIZONS = [1, 2, 3, 4]
DAY = 96
CSI_MIN_CLEARSKY = 50               # W/m2: below it the clear-sky ratio is unstable -> plain persistence
ENVELOPE = dict(q=0.6, n=14, s=1)
ENVELOPE_SP = dict(k=3, low=0.1)
CLIM_DAYS, CLIM_MIN = 7, 4
ARIMAX_ORDER = (1, 0, 1)
XGB_PARAMS = dict(
    n_estimators=3000, learning_rate=0.0668, max_depth=3, min_child_weight=1.4885, subsample=0.9167,
    colsample_bytree=0.4312, reg_lambda=19.5409, gamma=0.0, objective="reg:squarederror", tree_method="hist",
    random_state=42, n_jobs=1,
)
XGB_EARLY_STOP = 100
VAL_DAYS = 7                        # early stopping on the last 7 training days, then refit on all

WCOLS = list(WEATHER_COLS.values())
GEOMETRY = ["cs_ghi", "zenith", "azimuth", "poa_clearsky", "aoi"]
WEATHER_SETS = {"weather": WCOLS, "weather_icon": ["mx_" + c.removeprefix("om_") for c in WCOLS]}
MEMBERS = {"average": ["smart_persistence", "clear_day_curve", "arimax", "xgboost"],
           "average_weather": ["smart_persistence", "clear_day_curve", "arimax_weather", "xgboost_weather"],
           "average_weather_icon": ["smart_persistence", "clear_day_curve", "arimax_weather_icon",
                                    "xgboost_weather_icon"]}
ARIMAX_NAMES = ["arimax", *(f"arimax_{s}" for s in WEATHER_SETS)]
XGB_NAMES = ["xgboost", *(f"xgboost_{s}" for s in WEATHER_SETS)]
MODELS = list(dict.fromkeys(m for ms in MEMBERS.values() for m in ms))


# ------------------------------------------------------------------ data

def read(eng, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """Per farm: cf (capacity factor) and geo (sun + weather) on one 15-min UTC index [start, end + 1 h), and counts.
    cf exists only up to now; slots after it are missing."""
    stop = end + HORIZON
    now = pd.Timestamp.now(tz="UTC").floor("15min")
    out = {}
    for farm in S.FARMS:
        cf, counts = inputs.capacity_factor(eng, farm, start, min(stop, now))
        idx = pd.date_range(start, stop, freq="15min", inclusive="left")
        cf = cf.reindex(idx)
        wx, wc = inputs.weather_newest(eng, farm, start, stop, "ecmwf_ifs", WEATHER_COLS, ECMWF_DELAY_H,
                                       MAX_WEATHER_AGE_H)
        ic, icc = inputs.weather_newest(eng, farm, start, stop, "icon_eu", ICON_COLS, ICON_DELAY_H,
                                        MAX_WEATHER_AGE_H)
        ic = ic.rename(columns={"run_utc": "ic_run_utc", "lead_h": "ic_lead_h"})
        for c in WCOLS:
            n = c.removeprefix("om_")
            wx["mx_" + n] = (wx[c] + ic["ic_" + n]) / 2              # missing if either is
        geo = inputs.sun(farm, idx).join(wx).join(ic)
        if not geo.index.equals(idx):
            raise ValueError(f"{farm.name}: sun / weather join changed the slots")
        out[farm.name] = {"cf": cf, "geo": geo, "counts": {"plant": counts, "ecmwf": wc, "icon_eu": icc}}
    return out


# ------------------------------------------------------------------ formulas (no training)

def smart_persistence(cf: pd.Series, cs: pd.Series, h: int) -> pd.Series:
    prev, cs_prev = cf.shift(h), cs.shift(h)
    return (prev * cs / cs_prev).where(cs_prev >= CSI_MIN_CLEARSKY, prev)


def climatology(cf: pd.Series) -> pd.Series:
    past = pd.concat([cf.shift(DAY * d) for d in range(1, CLIM_DAYS + 1)], axis=1)
    return past.mean(axis=1).where(past.count(axis=1) >= CLIM_MIN)


def envelope(cf: pd.Series) -> pd.Series:
    """Clear-day shape: q-quantile of cf at the same UTC slot +-s over the last n days; missing below half present."""
    q, n, s = ENVELOPE["q"], ENVELOPE["n"], ENVELOPE["s"]
    cols = [cf.shift(DAY * d - j) for d, j in itertools.product(range(1, n + 1), range(-s, s + 1))]
    past = pd.concat(cols, axis=1)
    return past.quantile(q, axis=1).where(past.count(axis=1) >= len(cols) / 2)


def clear_day_curve(cf: pd.Series, e: pd.Series, clim: pd.Series, h: int) -> pd.Series:
    k, low = ENVELOPE_SP["k"], ENVELOPE_SP["low"]
    w = range(k, 0, -1)                                           # newest slot weighs most
    ys = sum(wi * cf.shift(h + i) for i, wi in enumerate(w))
    es = sum(wi * e.shift(h + i) for i, wi in enumerate(w))
    out = (ys / es * e).where(e.shift(h) >= low, clim)
    return out.where(e.notna() & e.shift(h).notna())


# ------------------------------------------------------------------ XGBoost

def xgb_features(cf: pd.Series, geo: pd.DataFrame, h: int, wcols: list[str]) -> pd.DataFrame:
    """Research's column order: [weather of T's hour,] sun and panel geometry at T, plant values up to T-h."""
    df = geo[[*wcols, *GEOMETRY]].copy()
    for k in (h, h + 1, h + 3, DAY):
        df[f"lag_{k}"] = cf.shift(k)
    df["mean_last_1h"] = cf.shift(h).rolling(4, min_periods=4).mean()
    cs_prev, poa_prev = geo["cs_ghi"].shift(h), geo["poa_clearsky"].shift(h)
    df["csi_newest"] = (cf.shift(h) / cs_prev).where(cs_prev >= CSI_MIN_CLEARSKY)
    df["csi_newest_poa"] = (cf.shift(h) / poa_prev).where(poa_prev >= CSI_MIN_CLEARSKY)
    return df.rename(columns={f"lag_{h}": "lag_h", f"lag_{h + 1}": "lag_h1", f"lag_{h + 3}": "lag_h3"})


def fit_xgb(cf: pd.Series, geo: pd.DataFrame, h: int, fit_end: pd.Timestamp, wcols: list[str]):
    """Early stopping on the VAL_DAYS days before fit_end, then refit on every labelled slot before fit_end."""
    import xgboost as xgb

    x = xgb_features(cf, geo, h, wcols)
    ok = cf.notna() & (cf.index < fit_end)
    if wcols:
        ok &= geo[wcols].notna().all(axis=1)
    val = ok & (cf.index >= fit_end - pd.Timedelta(days=VAL_DAYS))
    train = ok & ~val
    probe = xgb.XGBRegressor(**XGB_PARAMS, early_stopping_rounds=XGB_EARLY_STOP, eval_metric="rmse")
    probe.fit(x[train], cf[train], eval_set=[(x[val], cf[val])], verbose=False)
    model = xgb.XGBRegressor(**{**XGB_PARAMS, "n_estimators": probe.best_iteration + 1})
    model.fit(x[ok], cf[ok], verbose=False)
    return model


# ------------------------------------------------------------------ ARIMAX

def fit_arimax(cf: pd.Series, x: pd.DataFrame, fit_end: pd.Timestamp) -> dict:
    """Fit on every slot from the first labelled one to fit_end; each input divided by its largest absolute value.
    Raises if an input is missing in that range or all zero. Not converged: kept with converged = False (its
    forecasts are then missing and train refuses to save it)."""
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    fit = (cf.index >= cf.first_valid_index()) & (cf.index < fit_end)
    xf = x.loc[fit]
    if xf.isna().any().any():
        gap = xf.index[xf.isna().any(axis=1)]
        raise ValueError(f"ARIMAX {list(x.columns)}: {len(gap)} training slots without input (first {gap[0]}, last "
                         f"{gap[-1]}); the weather table must cover the training window")
    scale = xf.abs().max()
    if (scale == 0).any():
        raise ValueError(f"ARIMAX input all zero in training: {list(scale[scale == 0].index)}")
    res = SARIMAX(cf[fit], exog=xf / scale, order=ARIMAX_ORDER, trend="n").fit(disp=False, maxiter=1000)
    return {"params": res.params.to_dict(), "scale": scale.to_dict(), "fit_from": str(cf.index[fit][0]),
            "converged": bool(res.mle_retvals["converged"])}


def arimax(cf: pd.Series, x: pd.DataFrame, fitted: dict, hours: pd.DatetimeIndex) -> pd.Series:
    """Each hour H: steps 1..4 from data up to H (dynamic), parameters fixed; the filter runs over the gap-free
    stretch of inputs ending at H's end. Fewer than ARIMAX_MIN_SLOTS of it before H, or a fit that did not converge:
    missing. cf may hold the actuals of the hours forecast: the dynamic prediction from H never uses them."""
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    cf = cf.asfreq("15min")
    slots = pd.DatetimeIndex([h + pd.Timedelta(minutes=15 * q) for h in hours for q in range(4)])
    if not fitted["converged"]:
        return pd.Series(np.nan, index=slots)
    cols = list(fitted["scale"])
    xs = (x[cols] / pd.Series(fitted["scale"])[cols]).reindex(cf.index)
    gap = xs.isna().any(axis=1).to_numpy()
    pos = {}
    for hour in hours:
        i = cf.index.get_loc(hour)
        bad = np.flatnonzero(gap[:i + 4])
        pos[hour] = (i, bad[-1] + 1 if len(bad) else 0)
    out = {}
    for hour, (i, j) in pos.items():
        if i - j < ARIMAX_MIN_SLOTS:
            out[hour] = pd.Series(np.nan, index=cf.index[i:i + 4])
    for j in sorted({j for h, (_, j) in pos.items() if h not in out}):
        group = [h for h, (_, jj) in pos.items() if jj == j and h not in out]
        end = max(pos[h][0] for h in group) + 4
        mod = SARIMAX(cf.iloc[j:end], exog=xs.iloc[j:end], order=ARIMAX_ORDER, trend="n")
        res = mod.filter(np.array([fitted["params"][n] for n in mod.param_names]))
        for hour in group:
            i = pos[hour][0]
            pred = res.get_prediction(start=i - j, end=i - j + 3, dynamic=True).predicted_mean
            out[hour] = pd.Series(np.asarray(pred), index=cf.index[i:i + 4])
    return pd.concat([out[h] for h in hours])


# ------------------------------------------------------------------ fit / save / load

def _fit_farm(cf: pd.Series, geo: pd.DataFrame, fit_end: pd.Timestamp) -> dict:
    for s, wcols in WEATHER_SETS.items():
        no_wx = cf.notna() & (cf.index < fit_end) & geo[wcols].isna().any(axis=1)
        if no_wx.any():
            raise ValueError(f"{int(no_wx.sum())} labelled training slots without {s} {wcols} (first "
                             f"{cf.index[no_wx][0]}, last {cf.index[no_wx][-1]})")
    out = {"arimax": fit_arimax(cf, geo[["cs_ghi"]], fit_end),
           "xgboost": {h: fit_xgb(cf, geo, h, fit_end, []) for h in HORIZONS}}
    for s, wcols in WEATHER_SETS.items():
        out[f"arimax_{s}"] = fit_arimax(cf, geo[["cs_ghi", *wcols]], fit_end)
        out[f"xgboost_{s}"] = {h: fit_xgb(cf, geo, h, fit_end, wcols) for h in HORIZONS}
    out["info"] = {"trained_to_utc": str(fit_end), "data_from": str(cf.first_valid_index()),
                   "labelled_slots": int((cf.notna() & (cf.index < fit_end)).sum())}
    return out


def fit(data: dict, fit_end: pd.Timestamp) -> dict:
    """Every trained member of every farm on the slots before fit_end."""
    return {name: _fit_farm(d["cf"], d["geo"], fit_end) for name, d in data.items()}


def problems(fitted: dict) -> list[str]:
    return [f"{farm} {n} did not converge" for farm, f in fitted.items() for n in ARIMAX_NAMES
            if not f[n]["converged"]]


def summary(fitted: dict) -> dict:
    return {farm: {**f["info"], **{n: {k: round(v, 4) for k, v in f[n]["params"].items()} for n in ARIMAX_NAMES},
                   **{f"{n}_trees": {h: int(m.n_estimators) for h, m in f[n].items()} for n in XGB_NAMES}}
            for farm, f in fitted.items()}


def save(fitted: dict, folder: Path) -> None:
    for farm, f in fitted.items():
        d = folder / farm
        d.mkdir(parents=True, exist_ok=True)
        for name in ARIMAX_NAMES:
            (d / f"{name}.json").write_text(json.dumps(f[name], indent=1))
        for name in XGB_NAMES:
            for h, m in f[name].items():
                m.save_model(d / f"{name}_h{h}.json")
        (d / "info.json").write_text(json.dumps(f["info"], indent=1))


def load(folder: Path) -> dict:
    import xgboost as xgb

    out = {}
    for farm in S.FARMS:
        d = folder / farm.name
        f = {name: json.loads((d / f"{name}.json").read_text()) for name in ARIMAX_NAMES}
        for name in XGB_NAMES:
            f[name] = {}
            for h in HORIZONS:
                f[name][h] = xgb.XGBRegressor()
                f[name][h].load_model(d / f"{name}_h{h}.json")
        f["info"] = json.loads((d / "info.json").read_text())
        out[farm.name] = f
    return out


# ------------------------------------------------------------------ forecast

def _forecast_farm(cf: pd.Series, geo: pd.DataFrame, fitted: dict, hours: pd.DatetimeIndex) -> pd.DataFrame:
    """Every member and average (capacity factor) for the 4 quarters of each hour; cf and geo on one gap-free index
    from HISTORY before the first hour to the last quarter."""
    if not cf.index.equals(geo.index) or not (cf.index.to_series().diff().dropna() == pd.Timedelta("15min")).all():
        raise ValueError("cf and geo must share one gap-free 15-min index")
    cs, e, clim = geo["cs_ghi"], envelope(cf), climatology(cf)
    slots = pd.DatetimeIndex([h + pd.Timedelta(minutes=15 * q) for h in hours for q in range(4)])
    parts = []
    for q, h in enumerate(HORIZONS):
        f = pd.DataFrame({"smart_persistence": smart_persistence(cf, cs, h),
                          "clear_day_curve": clear_day_curve(cf, e, clim, h)})
        f["xgboost"] = fitted["xgboost"][h].predict(xgb_features(cf, geo, h, []))
        for s, wcols in WEATHER_SETS.items():
            xw = xgb_features(cf, geo, h, wcols)
            pred = pd.Series(fitted[f"xgboost_{s}"][h].predict(xw), index=xw.index)
            f[f"xgboost_{s}"] = pred.where(geo[wcols].notna().all(axis=1))      # no weather -> no value
        parts.append(f[f.index.minute == 15 * q])
    out = pd.concat(parts).sort_index().reindex(slots)
    out["arimax"] = arimax(cf, geo[["cs_ghi"]], fitted["arimax"], hours).reindex(slots)
    for s, wcols in WEATHER_SETS.items():
        out[f"arimax_{s}"] = arimax(cf, geo[["cs_ghi", *wcols]], fitted[f"arimax_{s}"], hours).reindex(slots)
    out = out[MODELS]
    for avg, members in MEMBERS.items():
        out[avg] = out[members].mean(axis=1, skipna=False)            # all 4 members or missing
    return out


def predict(data: dict, fitted: dict, issues: pd.DatetimeIndex) -> pd.DataFrame:
    """The 4 quarters of every issue hour, every farm, in MW (see the module docstring)."""
    out = []
    for farm in S.FARMS:
        d, f = data[farm.name], fitted[farm.name]
        idx = pd.date_range(issues[0] - HISTORY, issues[-1] + HORIZON, freq="15min", inclusive="left")
        if idx[0] < d["cf"].index[0] or idx[-1] > d["cf"].index[-1]:
            raise ValueError(f"{farm.name}: data do not cover {idx[0]} .. {idx[-1]}")
        q = _forecast_farm(d["cf"].reindex(idx), d["geo"].reindex(idx), f, issues) * farm.nameplate_mw
        q["issue_utc"] = q.index.floor("h")
        q["night"] = q["issue_utc"].map({h: not inputs.sun_up(farm, h) for h in issues})
        q.loc[q["night"], [*MODELS, *MEMBERS]] = 0.0                   # night: 0 MW for every source
        for col in SOURCES:
            # Power cannot be negative (user 2026-10-16): below 0 -> 0, the value before kept as <col>_raw
            q[f"{col}_raw"] = q[col]
            q[col] = q[col].where(~(q[col] < 0), 0.0)
            # A source gets rows for an hour only if all 4 quarters have a value
            complete = q[col].notna().groupby(q["issue_utc"]).transform("all")
            q[col] = q[col].where(complete)
        q["actual_mw"] = d["cf"].reindex(q.index) * farm.nameplate_mw
        q["weather_run_utc"] = d["geo"]["run_utc"].reindex(q.index)
        q["icon_run_utc"] = d["geo"]["ic_run_utc"].reindex(q.index)
        q["farm"], q["nameplate_mw"] = farm.name, farm.nameplate_mw
        out.append(q.rename_axis("slot_utc").reset_index())
    cols = ["farm", "issue_utc", "slot_utc", "actual_mw", "nameplate_mw", *SOURCES]
    df = pd.concat(out, ignore_index=True)
    return df[[*cols, *[c for c in df.columns if c not in cols]]]

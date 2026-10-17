"""Saved models: saved_models/<family>/<version>/ (model files + meta.json), ACTIVE (the version the hourly job uses)
and history.jsonl (every promote and rollback).

Retraining is never automatic (a person checks first): `train` saves a CANDIDATE, `compare` says whether retraining
helps, `promote` makes a version active (atomic switch, the previous one is kept), `rollback` goes back one step.
"""

from __future__ import annotations

import json
import os
import subprocess

import pandas as pd

from . import settings as S


def family_dir(family: str):
    return S.MODELS_DIR / family


def versions(family: str) -> list[str]:
    d = family_dir(family)
    return sorted(p.name for p in d.iterdir()
                  if (p / "meta.json").exists() and not p.name.endswith(".tmp")) if d.exists() else []


def meta(family: str, version: str) -> dict:
    return json.loads((family_dir(family) / version / "meta.json").read_text())


def active(family: str) -> str | None:
    p = family_dir(family) / "ACTIVE"
    return p.read_text().strip() if p.exists() else None


def code_version() -> str:
    """git commit of the code (with '+changes' if the working tree differs), or 'unknown'."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=S.ROOT, capture_output=True, text=True,
                             timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "pv_forecast"], cwd=S.ROOT,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return (sha or "unknown") + ("+changes" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def save(family, fitted: dict, trained_to: pd.Timestamp, extra: dict) -> str:
    """Save a candidate (not active). Written to a temporary folder first, so a version folder is complete or absent.
    Returns the version name: <training end local day>_<save time UTC>."""
    now = pd.Timestamp.now(tz="UTC")
    version = f"{trained_to.tz_convert(S.LOCAL_TZ):%Y-%m-%d}_{now:%Y%m%dT%H%M%SZ}"
    final = family_dir(family.NAME) / version
    tmp = final.with_name(version + ".tmp")
    family.save(fitted, tmp)
    m = {"family": family.NAME, "version": version, "trained_to_utc": str(trained_to), "saved_utc": str(now.floor("s")),
         "code_version": code_version(), "summary": family.summary(fitted), **extra}
    (tmp / "meta.json").write_text(json.dumps(m, indent=1, default=str))
    tmp.rename(final)
    return version


def load(family, version: str) -> tuple[dict, dict]:
    """(fitted, meta) of a saved version."""
    return family.load(family_dir(family.NAME) / version), meta(family.NAME, version)


def _set_active(family: str, version: str, action: str) -> None:
    d = family_dir(family)
    before = active(family)
    tmp = d / "ACTIVE.tmp"
    tmp.write_text(version + "\n")
    os.replace(tmp, d / "ACTIVE")                             # atomic: the hourly job sees the old or the new one
    with open(d / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"utc": str(pd.Timestamp.now(tz="UTC").floor("s")), "action": action,
                            "version": version, "previous": before}) + "\n")


def promote(family: str, version: str) -> None:
    if version not in versions(family):
        raise SystemExit(f"{family}: no saved version {version!r}; saved: {versions(family)}")
    if version == active(family):
        raise SystemExit(f"{family}: {version} is already active")
    _set_active(family, version, "promote")


def rollback(family: str) -> str:
    """Make active the version that was active before the current one was promoted (history.jsonl). Repeated
    rollbacks walk back along the promotions. Returns it."""
    p = family_dir(family) / "history.jsonl"
    events = [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
    now = active(family)
    prev = next((e["previous"] for e in reversed(events)
                 if e["action"] == "promote" and e["version"] == now and e["previous"]), None)
    if prev is None or prev not in versions(family):
        raise SystemExit(f"{family}: no previous version to roll back to (active {now})")
    _set_active(family, prev, "rollback")
    return prev


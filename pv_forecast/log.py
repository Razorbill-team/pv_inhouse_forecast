"""One log for every command: '<UTC time>Z <LEVEL> message' to stdout and logs/run_<YYYY-MM>.log."""

from __future__ import annotations

import sys

import pandas as pd

from . import settings as S

PROBLEMS: list[str] = []            # WARN and ERROR lines of this process (the hourly job reports them at the end)


def log(msg: str, level: str = "INFO") -> None:
    """Never raises: a full disk must not stop the forecast."""
    if level != "INFO":
        PROBLEMS.append(f"{level} {msg}"[:300])
    now = pd.Timestamp.now(tz="UTC")
    line = f"{now:%Y-%m-%d %H:%M:%S}Z {level:<5} " + " | ".join(str(msg).splitlines())
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass
    try:
        S.LOG_DIR.mkdir(exist_ok=True)
        with open(S.LOG_DIR / f"run_{now:%Y-%m}.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        print(f"(log file not written: {exc})", file=sys.stderr)

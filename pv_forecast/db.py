"""Database access: settings from the environment, engines, read-only queries, table-name checks, the write guard."""

from __future__ import annotations

import os
import re
import socket

import pandas as pd
import sqlalchemy as sa
from dotenv import load_dotenv

from . import settings as S

TABLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}\.[a-z_][a-z0-9_]{0,62}")


def load_settings() -> None:
    """Load the .env file in the repository folder into the environment (an already set variable wins)."""
    load_dotenv(S.ROOT / ".env")


def setting(key: str) -> str:
    """Environment variable `key`, or from the .env file in the repository folder (a set variable wins)."""
    load_settings()
    value = os.environ.get(key, "").strip()
    if not value:
        raise SystemExit(f"environment variable {key} is not set (see .env.example)")
    return value


def engine(env_key: str) -> sa.Engine:
    """Engine for the database URL in `env_key`."""
    return sa.create_engine(setting(env_key), connect_args={
        "connect_timeout": 10, "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10,
        "keepalives_count": 3})


def table(env_key: str) -> str:
    """Table name from `env_key` as "schema"."table". Checked, because a name cannot be a bound parameter: anything
    but two plain lowercase names stops the run."""
    value = setting(env_key)
    if not TABLE_NAME.fullmatch(value):
        raise SystemExit(f"{env_key}={value!r}: expected schema.table (lowercase letters, digits and _ only)")
    schema, name = value.split(".")
    return f'"{schema}"."{name}"'


def read_sql(eng: sa.Engine, query: str, **params) -> pd.DataFrame:
    """SELECT in a read-only transaction with a 120 s statement timeout."""
    with eng.connect() as c:
        c.execute(sa.text("set transaction read only"))
        c.execute(sa.text("set statement_timeout = '120s'"))
        return pd.read_sql(sa.text(query), c, params=params)


def _addresses(host: str | None) -> set[str]:
    try:
        return {a[4][0] for a in socket.getaddrinfo(host, None)} if host else set()
    except OSError:
        return set()


def write_engine(url_env: str, table_env: str) -> sa.Engine:
    """Engine for writing the table in `table_env`. On production (a host in PROD_HOSTS, or the same server and
    database as POSTDB_URL) only PROD_TABLES[table_env] may be written; anything else is refused."""
    ours = setting(table_env) == S.PROD_TABLES[table_env]
    eng = engine(url_env)
    w = eng.url
    if not w.host:
        raise SystemExit(f"{url_env} has no host; refusing to write (cannot check it is not production)")
    prod = set(S.PROD_HOSTS).union(*(_addresses(h) for h in S.PROD_HOSTS))
    read_url = os.environ.get(S.READ_DB_ENV)
    if read_url and not ours:
        r = sa.engine.make_url(read_url)
        if w.database == r.database and (r.host == w.host or _addresses(r.host) & _addresses(w.host)):
            raise SystemExit(f"{url_env} is the production database: only {S.PROD_TABLES[table_env]} there")
    if ({w.host} | _addresses(w.host)) & prod and not ours:
        raise SystemExit(f"{url_env} points at production host {w.host}: only {S.PROD_TABLES[table_env]} there")
    return eng


def describe(eng: sa.Engine, exc: Exception) -> str:
    """Log text for a database error: says plainly when the server cannot be reached."""
    where = f"{eng.url.host}:{eng.url.port or 5432}/{eng.url.database}"
    text = " ".join(str(getattr(exc, "orig", None) or exc).split())[:200]
    if isinstance(exc, sa.exc.OperationalError) and any(
            s in text.lower() for s in ("could not connect", "timeout expired", "connection refused",
                                        "server closed the connection", "could not translate host", "no route")):
        return f"server {where} NOT REACHABLE ({text})"
    return f"database error on {where}: {type(exc).__name__}: {text}"

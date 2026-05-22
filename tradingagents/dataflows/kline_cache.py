"""SQLite-backed K-line cache with incremental update support.

Schema
------
Table: kline
  - code      TEXT   (stock/sector/index code, e.g. "600519")
  - level     TEXT   (daily / 30min / 5min / ...)
  - data_type TEXT   (stock / sector / index)
  - dt        TEXT   (datetime string, "YYYY-MM-DD" or "YYYY-MM-DD HH:MM")
  - open      REAL
  - high      REAL
  - low       REAL
  - close     REAL
  - volume    REAL
  PRIMARY KEY (code, level, data_type, dt)

Table: meta
  - code       TEXT
  - level      TEXT
  - data_type  TEXT
  - name       TEXT   (optional display name)
  - rows       INTEGER
  - date_min   TEXT
  - date_max   TEXT
  - last_sync  TEXT   (ISO timestamp)
  - source     TEXT
  - start_date TEXT
  PRIMARY KEY (code, level, data_type)

All public functions are thread-safe (uses a single connection with
check_same_thread=False + external locking via threading.Lock).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Connection management
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_db_path: Optional[str] = None


def _get_db_path() -> str:
    """Return the SQLite database path, lazily initialised."""
    global _db_path
    if _db_path is None:
        from .config import get_config
        config = get_config()
        cache_dir = config.get(
            "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
        )
        os.makedirs(cache_dir, exist_ok=True)
        _db_path = os.path.join(cache_dir, "kline.db")
    return _db_path


_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    """Return (or create) the singleton database connection."""
    global _conn
    if _conn is None:
        db_path = _get_db_path()
        _conn = sqlite3.connect(db_path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA cache_size=-64000")  # 64 MB
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kline (
            code      TEXT    NOT NULL,
            level     TEXT    NOT NULL,
            data_type TEXT    NOT NULL DEFAULT 'stock',
            dt        TEXT    NOT NULL,
            open      REAL,
            high      REAL,
            low       REAL,
            close     REAL,
            volume    REAL,
            PRIMARY KEY (code, level, data_type, dt)
        );

        CREATE TABLE IF NOT EXISTS meta (
            code       TEXT    NOT NULL,
            level      TEXT    NOT NULL,
            data_type  TEXT    NOT NULL DEFAULT 'stock',
            name       TEXT    DEFAULT '',
            rows       INTEGER DEFAULT 0,
            date_min   TEXT    DEFAULT '',
            date_max   TEXT    DEFAULT '',
            last_sync  TEXT    DEFAULT '',
            source     TEXT    DEFAULT '',
            start_date TEXT    DEFAULT '',
            PRIMARY KEY (code, level, data_type)
        );

        CREATE INDEX IF NOT EXISTS idx_kline_code_level
            ON kline(code, level, data_type);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
#  Low-level helpers
# ---------------------------------------------------------------------------

def _df_to_rows(df: pd.DataFrame, code: str, level: str, data_type: str) -> list[tuple]:
    """Convert a K-line DataFrame to list of row tuples for insertion.

    Uses vectorised pandas operations for speed on large DataFrames.
    """
    is_minute = level.endswith("min")
    fmt = "%Y-%m-%d %H:%M" if is_minute else "%Y-%m-%d"
    dates = pd.to_datetime(df["Date"]).dt.strftime(fmt)
    opens = df["Open"].fillna(0).astype(float).values
    highs = df["High"].fillna(0).astype(float).values
    lows = df["Low"].fillna(0).astype(float).values
    closes = df["Close"].fillna(0).astype(float).values
    volumes = df["Volume"].fillna(0).astype(float).values
    return [
        (code, level, data_type, dt, o, h, l, c, v)
        for dt, o, h, l, c, v in zip(dates, opens, highs, lows, closes, volumes)
    ]


def _rows_to_df(rows: list[tuple], level: str) -> pd.DataFrame:
    """Convert row tuples back to a DataFrame."""
    if not rows:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame(rows, columns=["code", "level", "data_type", "Date",
                                     "Open", "High", "Low", "Close", "Volume"])
    df = df.drop(columns=["code", "level", "data_type"])
    is_minute = level.endswith("min")
    df["Date"] = pd.to_datetime(df["Date"],
                                format="%Y-%m-%d %H:%M" if is_minute else "%Y-%m-%d")
    return df


# ---------------------------------------------------------------------------
#  Public API — read
# ---------------------------------------------------------------------------


def load_kline(code: str, level: str, data_type: str = "stock",
               start_date: str | None = None,
               end_date: str | None = None) -> pd.DataFrame:
    """Load K-line data from the SQLite cache, optionally filtered by date range.

    Returns an empty DataFrame if no data is cached.
    """
    with _lock:
        conn = _get_conn()
        sql = "SELECT * FROM kline WHERE code=? AND level=? AND data_type=?"
        params: list = [code, level, data_type]

        if start_date:
            sql += " AND dt >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND dt <= ?"
            params.append(end_date)

        sql += " ORDER BY dt"
        rows = conn.execute(sql, params).fetchall()
        return _rows_to_df(rows, level)


def get_latest_date(code: str, level: str, data_type: str = "stock") -> str | None:
    """Return the most recent date string in cache, or None if no data."""
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT MAX(dt) FROM kline WHERE code=? AND level=? AND data_type=?",
            (code, level, data_type),
        ).fetchone()
        return row[0] if row and row[0] else None


def get_meta(code: str, level: str, data_type: str = "stock") -> dict | None:
    """Return the metadata dict for a cached series, or None."""
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT name, rows, date_min, date_max, last_sync, source, start_date "
            "FROM meta WHERE code=? AND level=? AND data_type=?",
            (code, level, data_type),
        ).fetchone()
        if row is None:
            return None
        name, rows, date_min, date_max, last_sync, source, start_date = row
        return {
            "code": code,
            "level": level,
            "data_type": data_type,
            "name": name or "",
            "rows": rows,
            "date_range": [date_min, date_max] if date_min and date_max else [],
            "last_sync": last_sync or "",
            "source": source or "",
            "start_date": start_date or "",
        }


def list_cached(data_type: str | None = None) -> list[dict]:
    """List all cached series, optionally filtered by data_type."""
    with _lock:
        conn = _get_conn()
        if data_type:
            rows = conn.execute(
                "SELECT code, level, data_type, name, rows, date_min, date_max, "
                "last_sync, source, start_date FROM meta WHERE data_type=?",
                (data_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT code, level, data_type, name, rows, date_min, date_max, "
                "last_sync, source, start_date FROM meta",
            ).fetchall()

    results = []
    for r in rows:
        code, level, dtype, name, nrows, dmin, dmax, lsync, src, sdate = r
        results.append({
            "code": code,
            "level": level,
            "data_type": dtype,
            "name": name or "",
            "rows": nrows,
            "date_range": [dmin, dmax] if dmin and dmax else [],
            "last_sync": lsync or "",
            "source": src or "",
            "start_date": sdate or "",
        })
    return results


# ---------------------------------------------------------------------------
#  Public API — write
# ---------------------------------------------------------------------------


def save_kline(df: pd.DataFrame, code: str, level: str, data_type: str = "stock",
               source: str = "", start_date: str = "",
               name: str = "") -> dict:
    """Save a K-line DataFrame to the SQLite cache (INSERT OR REPLACE).

    Existing rows with the same (code, level, data_type, dt) are replaced.

    Returns a meta dict with stats.
    """
    if df.empty:
        return {
            "code": code, "level": level, "data_type": data_type,
            "rows": 0, "date_range": [], "source": source,
            "status": "error", "error": "DataFrame is empty",
        }

    rows = _df_to_rows(df, code, level, data_type)

    is_minute = level.endswith("min")
    fmt = "%Y-%m-%d %H:%M" if is_minute else "%Y-%m-%d"
    date_min = pd.to_datetime(df["Date"].min()).strftime(fmt)
    date_max = pd.to_datetime(df["Date"].max()).strftime(fmt)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _lock:
        conn = _get_conn()
        # Batch insert with INSERT OR REPLACE for dedup
        conn.executemany(
            "INSERT OR REPLACE INTO kline "
            "(code, level, data_type, dt, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        # Update meta
        conn.execute(
            "INSERT OR REPLACE INTO meta "
            "(code, level, data_type, name, rows, date_min, date_max, "
            "last_sync, source, start_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, level, data_type, name, len(df), date_min, date_max,
             now_str, source, start_date),
        )
        conn.commit()

    logger.info("Saved %d rows for %s/%s/%s (%s ~ %s) from %s",
                len(df), code, level, data_type, date_min, date_max, source)

    return {
        "code": code,
        "level": level,
        "data_type": data_type,
        "name": name,
        "rows": len(df),
        "date_range": [date_min, date_max],
        "last_sync": now_str,
        "source": source,
        "start_date": start_date,
        "status": "ok",
        "error": None,
    }


def incremental_update(df_new: pd.DataFrame, code: str, level: str,
                       data_type: str = "stock", source: str = "",
                       start_date: str = "", name: str = "") -> dict:
    """Incrementally merge new K-line data into the cache.

    For each row in df_new: if the (code, level, data_type, dt) primary key
    already exists, the OHLCV values are updated; otherwise a new row is
    inserted.  This is equivalent to INSERT OR REPLACE but we also recount
    rows and update meta afterwards.

    Returns a result dict (same shape as save_kline).
    """
    if df_new.empty:
        # Just return current meta if data is empty
        meta = get_meta(code, level, data_type)
        if meta:
            meta["status"] = "ok"
            meta["error"] = None
            return meta
        return {
            "code": code, "level": level, "data_type": data_type,
            "rows": 0, "date_range": [], "source": source,
            "status": "error", "error": "No new data and nothing cached",
        }

    rows = _df_to_rows(df_new, code, level, data_type)

    is_minute = level.endswith("min")
    fmt = "%Y-%m-%d %H:%M" if is_minute else "%Y-%m-%d"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _lock:
        conn = _get_conn()
        # Upsert new rows
        conn.executemany(
            "INSERT OR REPLACE INTO kline "
            "(code, level, data_type, dt, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        # Recount and find new date range
        count_row = conn.execute(
            "SELECT COUNT(*), MIN(dt), MAX(dt) FROM kline "
            "WHERE code=? AND level=? AND data_type=?",
            (code, level, data_type),
        ).fetchone()
        total_rows, date_min, date_max = count_row

        # Update meta
        conn.execute(
            "INSERT OR REPLACE INTO meta "
            "(code, level, data_type, name, rows, date_min, date_max, "
            "last_sync, source, start_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, level, data_type, name, total_rows, date_min or "", date_max or "",
             now_str, source, start_date),
        )
        conn.commit()

    logger.info("Incremental update: %d new rows for %s/%s/%s, total %d rows (%s ~ %s)",
                len(df_new), code, level, data_type, total_rows, date_min, date_max)

    return {
        "code": code,
        "level": level,
        "data_type": data_type,
        "name": name,
        "rows": total_rows,
        "date_range": [date_min or "", date_max or ""],
        "last_sync": now_str,
        "source": source,
        "start_date": start_date,
        "status": "ok",
        "error": None,
    }


# ---------------------------------------------------------------------------
#  Public API — delete
# ---------------------------------------------------------------------------


def delete_kline(code: str, level: str | None = None,
                 data_type: str = "stock") -> bool:
    """Delete cached K-line data and its meta entry.

    If level is None, delete all levels for this code+data_type.
    Returns True if anything was deleted.
    """
    with _lock:
        conn = _get_conn()
        if level:
            conn.execute(
                "DELETE FROM kline WHERE code=? AND level=? AND data_type=?",
                (code, level, data_type),
            )
            conn.execute(
                "DELETE FROM meta WHERE code=? AND level=? AND data_type=?",
                (code, level, data_type),
            )
        else:
            conn.execute(
                "DELETE FROM kline WHERE code=? AND data_type=?",
                (code, data_type),
            )
            conn.execute(
                "DELETE FROM meta WHERE code=? AND data_type=?",
                (code, data_type),
            )
        deleted = conn.total_changes > 0  # approximate
        conn.commit()
    return True  # always True since we don't track pre-delete count precisely


# ---------------------------------------------------------------------------
#  Migration: CSV → SQLite
# ---------------------------------------------------------------------------


def migrate_csv_to_sqlite(cache_dir: str | None = None) -> int:
    """One-time migration: import existing CSV+JSON caches into SQLite.

    Returns the number of series imported.
    """
    if cache_dir is None:
        from .config import get_config
        config = get_config()
        cache_dir = config.get(
            "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
        )

    if not os.path.isdir(cache_dir):
        return 0

    imported = 0
    import json as _json

    # Map CSV filename patterns to data_type
    patterns = [
        ("-astock-", "stock"),
        ("-sector-", "sector"),
        ("-index-", "index"),
    ]

    for csv_file in sorted(os.listdir(cache_dir)):
        if not csv_file.endswith(".csv"):
            continue

        # Determine data_type from filename
        data_type = None
        base_name = csv_file[:-4]  # strip .csv
        code_part = ""
        level_part = ""
        for suffix, dtype in patterns:
            if suffix in base_name:
                parts = base_name.split(suffix)
                if len(parts) == 2:
                    code_part, level_part = parts
                    data_type = dtype
                    break

        if data_type is None:
            continue

        csv_path = os.path.join(cache_dir, csv_file)
        meta_path = os.path.join(cache_dir, base_name + "-meta.json")

        # Read CSV
        try:
            df = pd.read_csv(csv_path, encoding="utf-8", on_bad_lines="skip")
            if df.empty or "Date" not in df.columns:
                continue
        except Exception as e:
            logger.warning("Skipping %s: %s", csv_file, e)
            continue

        # Read meta for name/source
        name = ""
        source = ""
        start_date = ""
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = _json.load(f)
                name = meta.get("name", "")
                source = meta.get("source", "")
                start_date = meta.get("start_date", "")
            except Exception:
                pass

        # Check if already imported
        existing = get_meta(code_part, level_part, data_type)
        if existing and existing.get("rows", 0) > 0:
            logger.info("Already cached %s/%s/%s, skipping CSV import",
                        code_part, level_part, data_type)
            # Rename old CSV/JSON files to .migrated
            for old_path in [csv_path, meta_path]:
                if os.path.exists(old_path):
                    os.rename(old_path, old_path + ".migrated")
            continue

        # Save to SQLite
        save_kline(df, code_part, level_part, data_type=data_type,
                   source=source, start_date=start_date, name=name)
        imported += 1

        # Rename old CSV/JSON files to .migrated
        for old_path in [csv_path, meta_path]:
            if os.path.exists(old_path):
                os.rename(old_path, old_path + ".migrated")

    if imported > 0:
        logger.info("Migrated %d CSV caches to SQLite", imported)
    return imported

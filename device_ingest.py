"""
device_ingest.py - usage totals pushed by other machines running Claude Code.

This dashboard reads only this PC's ~/.claude/projects. Other machines (e.g.
Tam's Mac) run collector/claude_usage_collector.py, which aggregates their own
transcripts into per-hour totals and POSTs them to /api/ingest over the tailnet:

    {"device": "tams-macbook-air", "label": "Tam's Mac", "since": "2026-10-22T00",
     "collector": "1", "rows": [{"hour": "2026-11-05T06", "model": "claude-opus-5-5",
     "project": "fetchkin-vault", "session": "3f2a9c...", "input": 1, "output": 2,
     "cache_read": 3, "cache_creation": 4, "turns": 5}, ...]}

`hour` is a UTC hour, `project` the basename of the working folder, `session` a
hash of the Claude session id. No message content is ever sent. Each push
replaces that device's rows from `since` on, so re-sending a window is
idempotent. The request must carry `Authorization: Bearer <USAGE_INGEST_TOKEN>`
(read from the environment or ~/.config/fetchkin/.env); with no token
configured, ingest is refused.
"""

import hmac
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ENV_FILE = Path.home() / ".config" / "fetchkin" / ".env"
MAX_ROWS = 200_000
_HOUR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")
_DEVICE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_FIELDS = ("input", "output", "cache_read", "cache_creation", "turns")


def ingest_token():
    tok = os.environ.get("USAGE_INGEST_TOKEN", "").strip()
    if tok:
        return tok
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "USAGE_INGEST_TOKEN":
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def authorized(header_value):
    """True when the Authorization header carries the configured token."""
    expected = ingest_token()
    if not expected or not header_value or not header_value.startswith("Bearer "):
        return False
    return hmac.compare_digest(header_value[7:].strip().encode(), expected.encode())


def init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS device_usage (
            device         TEXT NOT NULL,
            hour           TEXT NOT NULL,
            model          TEXT NOT NULL,
            project        TEXT NOT NULL,
            session        TEXT NOT NULL,
            input          INTEGER DEFAULT 0,
            output         INTEGER DEFAULT 0,
            cache_read     INTEGER DEFAULT 0,
            cache_creation INTEGER DEFAULT 0,
            turns          INTEGER DEFAULT 0,
            PRIMARY KEY (device, hour, model, project, session)
        );
        CREATE TABLE IF NOT EXISTS devices (
            device     TEXT PRIMARY KEY,
            label      TEXT,
            last_push  TEXT,
            collector  TEXT
        );
    """)


def _str(v, limit=120):
    return str(v)[:limit] if v is not None else ""


def ingest(db_path, payload):
    """Store one push. Returns {"ok", "device", "rows"} or raises ValueError."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    device = payload.get("device")
    if not isinstance(device, str) or not _DEVICE_RE.match(device):
        raise ValueError("device must be 1-64 chars of A-Z a-z 0-9 . _ -")
    since = payload.get("since")
    if not isinstance(since, str) or not _HOUR_RE.match(since):
        raise ValueError("since must be a UTC hour YYYY-MM-DDTHH")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("rows must be a list of at most %d items" % MAX_ROWS)
    clean = []
    for r in rows:
        if not isinstance(r, dict) or not _HOUR_RE.match(str(r.get("hour", ""))) or r["hour"] < since:
            continue
        nums = []
        for f in _FIELDS:
            v = r.get(f, 0)
            if not isinstance(v, int) or v < 0:
                raise ValueError("%s must be a non-negative integer" % f)
            nums.append(v)
        clean.append((device, r["hour"], _str(r.get("model")) or "unknown", _str(r.get("project")) or "unknown",
                      _str(r.get("session"), 64), *nums))
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        init_tables(conn)
        with conn:
            conn.execute("DELETE FROM device_usage WHERE device = ? AND hour >= ?", (device, since))
            conn.executemany("""
                INSERT INTO device_usage (device, hour, model, project, session, input, output, cache_read, cache_creation, turns)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (device, hour, model, project, session) DO UPDATE SET
                    input = input + excluded.input, output = output + excluded.output,
                    cache_read = cache_read + excluded.cache_read,
                    cache_creation = cache_creation + excluded.cache_creation, turns = turns + excluded.turns
            """, clean)
            conn.execute("""
                INSERT INTO devices (device, label, last_push, collector) VALUES (?, ?, ?, ?)
                ON CONFLICT (device) DO UPDATE SET label = excluded.label, last_push = excluded.last_push,
                    collector = excluded.collector
            """, (device, _str(payload.get("label"), 64) or device,
                  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), _str(payload.get("collector"), 32)))
    finally:
        conn.close()
    return {"ok": True, "device": device, "rows": len(clean)}


def delete_device(db_path, device):
    """Remove every row a device has pushed (used to clean up test devices)."""
    if not isinstance(device, str) or not _DEVICE_RE.match(device):
        raise ValueError("bad device")
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        init_tables(conn)
        with conn:
            n = conn.execute("DELETE FROM device_usage WHERE device = ?", (device,)).rowcount
            conn.execute("DELETE FROM devices WHERE device = ?", (device,))
    finally:
        conn.close()
    return {"ok": True, "device": device, "deleted_rows": n}


def remote_rows(conn, start_iso=None, end_iso=None):
    """Pushed rows in [start_iso, end_iso) (UTC ISO bounds on whole hours), with the device label.

    Returns tuples (device, label, hour, model, project, session, input, output, cache_read, cache_creation, turns).
    """
    init_tables(conn)
    where, params = [], []
    if start_iso:
        where.append("u.hour >= ?")
        params.append(start_iso[:13])
    if end_iso:
        where.append("u.hour < ?")
        params.append(end_iso[:13])
    sql = ("SELECT u.device, COALESCE(d.label, u.device), u.hour, u.model, u.project, u.session, "
           "u.input, u.output, u.cache_read, u.cache_creation, u.turns "
           "FROM device_usage u LEFT JOIN devices d ON d.device = u.device"
           + (" WHERE " + " AND ".join(where) if where else ""))
    return conn.execute(sql, params).fetchall()


def device_list(conn):
    init_tables(conn)
    return [{"device": d, "label": l, "last_push": p, "collector": c}
            for d, l, p, c in conn.execute("SELECT device, label, last_push, collector FROM devices ORDER BY label")]

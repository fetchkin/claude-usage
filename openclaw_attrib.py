"""
openclaw_attrib.py - Attribute Claude Code sessions to the OpenClaw chat that ran them.

OpenClaw drives Claude Code through `claude-cli`, so each transcript in
~/.claude/projects belongs to some OpenClaw session (a Slack channel, a thread,
the main DM, a subagent worker or a scheduled automation). This module maps a
Claude session id to a readable "chat" bucket so the dashboard can show what
each chat costs.

Everything here is READ-ONLY against ~/.openclaw and the transcripts. The only
thing persisted is a short derived marker per session (`openclaw_hints` table in
usage.db: a cron job id, a channel id, or a hash + timestamp of a subagent
task), never message text.

Attribution, strongest signal first:
  1. OpenClaw's session store records the current claude-cli session id per
     session key (`cliSessionIds` / `cliSessionBindings` in session_nodes).
  2. Older Claude sessions (rolled over / reset) are matched from a marker in
     the transcript's first prompt:
       - "[cron:<jobId> ...]"           -> agent:<agent>:cron:<jobId>
       - "chat_id": "channel:<C...>"     -> agent:<agent>:slack:channel:<c...>
       - "[Subagent Context] ... [Subagent Task]" -> the subagent run whose task
         text matches (hash of the first 200 normalised chars), else the run
         spawned closest before the transcript started.
Then the key is rolled up: subagents to the chat that spawned them (following
requester links), threads to their channel, cron runs to their job.
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw"))

UNMAPPED_ID = "other:unmapped"
OUTSIDE_ID = "other:outside-openclaw"
OUTSIDE_LABEL = "Claude Code on this laptop (not via OpenClaw)"

_SUBAGENT_TASK_MARK = "[Subagent Task]\n"
_CRON_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?\[cron:([0-9a-fA-F-]{8,40})")
_CHAT_ID_RE = re.compile(r'"chat_id"\s*:\s*"([^"]{1,80})"')
_HASHTAG_RE = re.compile(r"#[A-Za-z0-9][A-Za-z0-9_-]*")
_SUBAGENT_FALLBACK_WINDOW_MS = 180_000
_FP_CHARS = 200


# ── Small pure helpers (unit-tested) ─────────────────────────────────────────

def _norm(text):
    return re.sub(r"\s+", " ", text or "").strip()


def task_fingerprint(task_text):
    """Stable short hash of a subagent task's opening text."""
    return hashlib.sha1(_norm(task_text)[:_FP_CHARS].encode("utf-8")).hexdigest()[:16]


def hint_from_prompt(text, ts_ms=None):
    """Derive a content-free marker from a transcript's first prompt.

    Returns a short string ("cron:<id>", "chat:<chat_id>", "task:<fp>@<ms>") or
    "none" when the prompt carries no OpenClaw marker.
    """
    if not text:
        return "none"
    head = text[:4000]
    m = _CRON_RE.match(head)
    if m:
        return "cron:" + m.group(1).lower()
    if "[Subagent Context]" in head[:400]:
        i = text.find(_SUBAGENT_TASK_MARK)
        body = text[i + len(_SUBAGENT_TASK_MARK):] if i >= 0 else ""
        return "task:%s@%d" % (task_fingerprint(body), int(ts_ms or 0))
    m = _CHAT_ID_RE.search(head)
    if m:
        return "chat:" + m.group(1)
    return "none"


def _iso_to_ms(ts):
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def read_first_prompt(path):
    """Return (text, ts_ms) of the first user prompt in a transcript, or (None, None).

    The text is used in memory only to derive a marker; it is never stored.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if n > 400:
                    break
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                content = None
                if rec.get("type") == "queue-operation" and rec.get("operation") == "enqueue":
                    content = rec.get("content")
                elif rec.get("type") == "user":
                    content = (rec.get("message") or {}).get("content")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content
                            if isinstance(c, dict) and c.get("type") == "text")
                if isinstance(content, str) and content.strip():
                    return content, _iso_to_ms(rec.get("timestamp") or "")
    except OSError:
        pass
    return None, None


def base_key(key):
    """Strip per-thread and per-run suffixes: channel threads -> channel, cron runs -> job."""
    if not key:
        return key
    for marker in (":thread:", ":run:"):
        i = key.find(marker)
        if i >= 0:
            key = key[:i]
    return key


def channel_name_from_labels(*labels):
    """Pick '#channel' out of OpenClaw's stored labels ('BuzzifyBoost #ai', 'slack:x-#ai')."""
    for lab in labels:
        if not lab:
            continue
        m = _HASHTAG_RE.search(lab)
        if m:
            return m.group(0)
    return None


# ── OpenClaw index ───────────────────────────────────────────────────────────

class OpenClawIndex:
    """In-memory view of the bits of OpenClaw state needed for attribution."""

    def __init__(self):
        self.cli_to_key = {}       # claude session id -> openclaw session key
        self.nodes = {}            # session key -> {"labels": [...], "parent": key|None}
        self.requester = {}        # subagent child key -> requester key
        self.runs_by_fp = {}       # task fingerprint -> [(created_ms, child_key)]
        self.runs = []             # [(created_ms, child_key)] sorted
        self.cron_names = {}       # job id -> name
        self.conv_labels = {}      # native channel id (lower) -> label

    # Resolution ------------------------------------------------------------
    def key_for_hint(self, hint):
        if not hint or hint == "none":
            return None
        kind, _, val = hint.partition(":")
        if kind == "cron":
            return "agent:%s:cron:%s" % (self._agent(), val)
        if kind == "chat":
            target_kind, _, target = val.partition(":")
            if target_kind == "channel" and target:
                return "agent:%s:slack:channel:%s" % (self._agent(), target.lower())
            if target_kind in ("user", "direct") and target:
                return "agent:%s:main" % self._agent()
            return None
        if kind == "task":
            fp, _, ms = val.partition("@")
            ms = int(ms or 0)
            cands = self.runs_by_fp.get(fp) or []
            if cands:
                # Same task text spawned more than once: take the latest spawn at/before start.
                before = [c for c in cands if c[0] <= ms + 5000] or cands
                return max(before)[1]
            # Fallback: the run spawned closest before this transcript started.
            best = None
            for created, child in self.runs:
                if ms - _SUBAGENT_FALLBACK_WINDOW_MS <= created <= ms + 5000:
                    if best is None or abs(ms - created) < abs(ms - best[0]):
                        best = (created, child)
            return best[1] if best else None
        return None

    def _agent(self):
        for k in self.nodes:
            parts = k.split(":")
            if len(parts) > 1 and parts[0] == "agent":
                return parts[1]
        return "james"

    def parent_of(self, key):
        if key in self.requester:
            return self.requester[key]
        node = self.nodes.get(key) or {}
        return node.get("parent")

    def bucket_for_key(self, key):
        """Roll a session key up to its chat bucket -> (bucket_id, label, kind)."""
        seen = set()
        k = key
        while k and ":subagent:" in k and k not in seen:
            seen.add(k)
            k = self.parent_of(k)
        if not k or ":subagent:" in k:
            return (UNMAPPED_ID, "Other / unmapped", "unmapped")
        k = base_key(k)
        parts = k.split(":")
        rest = parts[2:] if len(parts) > 2 and parts[0] == "agent" else parts
        if rest[:1] == ["main"]:
            return (k, "Main DM (Nub / Tam)", "dm")
        if rest[:1] == ["cron"] and len(rest) > 1:
            job = rest[1]
            name = self.cron_names.get(job)
            if not name:
                lab = self._labels(k)
                name = next((l.split(":", 1)[1].strip() for l in lab
                             if l and l.lower().startswith("automation:")), None)
            return (k, "Automation: " + (name or job[:8]), "cron")
        if rest[:2] == ["slack", "channel"] and len(rest) > 2:
            cid = rest[2]
            name = (channel_name_from_labels(*self._labels(k))
                    or channel_name_from_labels(self.conv_labels.get(cid.lower())))
            if not name:
                # A thread node may carry the label when the channel node doesn't.
                for nk, node in self.nodes.items():
                    if base_key(nk) == k:
                        name = channel_name_from_labels(*node["labels"])
                        if name:
                            break
            return (k, name or "#" + cid.upper(), "channel")
        if rest[:2] == ["slack", "direct"]:
            return (k, "Slack DM " + (rest[2] if len(rest) > 2 else ""), "dm")
        labels = [l for l in self._labels(k) if l]
        return (k, labels[0] if labels else k, "other")

    def _labels(self, key):
        return (self.nodes.get(key) or {}).get("labels") or []


def _ro_connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def load_index(openclaw_home=None, agent_dbs=None):
    """Build an OpenClawIndex from OpenClaw's sqlite stores (read-only).

    Missing files/tables just leave parts of the index empty.
    """
    home = Path(openclaw_home or OPENCLAW_HOME)
    idx = OpenClawIndex()
    if agent_dbs is None:
        agent_dbs = sorted(home.glob("agents/*/agent/openclaw-agent.sqlite"))
    for db in agent_dbs:
        try:
            conn = _ro_connect(db)
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                "SELECT session_key, entry_json, label, display_name, parent_session_key, spawned_by "
                "FROM session_nodes").fetchall()
            for key, entry_json, label, display_name, parent, spawned_by in rows:
                try:
                    e = json.loads(entry_json or "{}")
                except ValueError:
                    e = {}
                for cli in (
                    (e.get("cliSessionIds") or {}).get("claude-cli"),
                    ((e.get("cliSessionBindings") or {}).get("claude-cli") or {}).get("sessionId"),
                ):
                    if cli:
                        idx.cli_to_key[cli] = key
                origin = ((e.get("delivery") or {}).get("origin") or {})
                idx.nodes[key] = {
                    "labels": [label, display_name, e.get("label"), e.get("subject"),
                               e.get("displayName"), origin.get("label")],
                    "parent": spawned_by or parent or e.get("spawnedBy") or e.get("parentSessionKey"),
                }
            try:
                for cid, lab in conn.execute(
                        "SELECT native_channel_id, label FROM conversations WHERE native_channel_id IS NOT NULL"):
                    if lab and channel_name_from_labels(lab):
                        idx.conv_labels.setdefault(cid.lower(), lab)
            except sqlite3.Error:
                pass
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    state = home / "state" / "openclaw.sqlite"
    if state.exists():
        try:
            conn = _ro_connect(state)
            try:
                for child, req, created, payload in conn.execute(
                        "SELECT child_session_key, requester_session_key, created_at, payload_json FROM subagent_runs"):
                    idx.requester[child] = req
                    idx.runs.append((created or 0, child))
                    try:
                        task = json.loads(payload or "{}").get("task")
                    except ValueError:
                        task = None
                    if task:
                        idx.runs_by_fp.setdefault(task_fingerprint(task), []).append((created or 0, child))
                for job_id, name in conn.execute("SELECT job_id, name FROM cron_jobs"):
                    idx.cron_names[job_id] = name
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    idx.runs.sort()
    return idx


# ── Hints cache in usage.db ──────────────────────────────────────────────────

def ensure_hint_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS openclaw_hints (
            session_id TEXT PRIMARY KEY,
            hint       TEXT NOT NULL
        )
    """)


def refresh_hints(conn, session_ids):
    """Compute and cache markers for sessions that don't have one yet."""
    ensure_hint_table(conn)
    have = {r[0] for r in conn.execute("SELECT session_id FROM openclaw_hints")}
    todo = [s for s in session_ids if s not in have]
    if not todo:
        return
    paths = {}
    for (p,) in conn.execute("SELECT path FROM processed_files"):
        pp = Path(p)
        if pp.parent.name == "subagents":
            continue
        paths.setdefault(pp.stem, p)
    new = []
    for sid in todo:
        p = paths.get(sid)
        if not p:
            continue
        text, ts_ms = read_first_prompt(p)
        if text is None:
            continue  # no prompt yet (file still being written); retry next refresh
        new.append((sid, hint_from_prompt(text, ts_ms)))
    if new:
        conn.executemany("INSERT OR REPLACE INTO openclaw_hints (session_id, hint) VALUES (?, ?)", new)
        conn.commit()


def is_openclaw_project(project_name, cwd=None):
    s = ((project_name or "") + " " + (cwd or "")).lower()
    return "openclaw" in s


def attribute_session(session_id, hint, idx, project_name=None, cwd=None):
    """Map one Claude session to (bucket_id, label, kind, how).

    `how` says which signal matched: "binding", "marker", or "none".
    """
    key = idx.cli_to_key.get(session_id)
    how = "binding" if key else "none"
    if not key:
        key = idx.key_for_hint(hint)
        how = "marker" if key else "none"
    if key:
        return idx.bucket_for_key(key) + (how,)
    if not is_openclaw_project(project_name, cwd):
        return (OUTSIDE_ID, OUTSIDE_LABEL, "outside", "none")
    return (UNMAPPED_ID, "Other / unmapped", "unmapped", "none")


# ── Query used by the dashboard ──────────────────────────────────────────────

_index_cache = {"at": 0.0, "idx": None}
_index_lock = threading.Lock()


def get_index(max_age=20.0, openclaw_home=None):
    with _index_lock:
        now = time.time()
        if _index_cache["idx"] is None or now - _index_cache["at"] > max_age or openclaw_home:
            _index_cache["idx"] = load_index(openclaw_home)
            _index_cache["at"] = now
        return _index_cache["idx"]


# ── Date ranges (local-calendar days, default Australia/Sydney) ──────────────
#
# Every by-chat range is a run of whole local days: `from` and `to` are
# inclusive YYYY-MM-DD dates in RANGE_TZ, turned into a half-open UTC window
# [from 00:00 local, day after `to` 00:00 local). Presets resolve to dates the
# same way, so the UI, the CSV (built from the same payload) and the raw API
# all go through resolve_range().

RANGE_TZ = os.environ.get("CLAUDE_USAGE_TZ", "Australia/Sydney")
RANGE_PRESETS = {"today": 1, "7d": 7, "30d": 30}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _first_sunday(year, month):
    from datetime import date, timedelta
    d = date(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7)


class _SydneyFallback:
    """Australia/Sydney rules for Pythons without tzdata (3.8, bare Windows).

    AEDT (+11) from the first Sunday of October 02:00 AEST to the first Sunday
    of April 03:00 AEDT; both instants are 16:00 UTC the day before.
    """

    def offset(self, utc_naive):
        from datetime import datetime, timedelta
        y = utc_naive.year
        start = datetime.combine(_first_sunday(y, 10), datetime.min.time()) - timedelta(hours=8)
        end = datetime.combine(_first_sunday(y, 4), datetime.min.time()) - timedelta(hours=8)
        return timedelta(hours=11 if (utc_naive >= start or utc_naive < end) else 10)


def _zone(tz_name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:
        if tz_name == "Australia/Sydney":
            return _SydneyFallback()
        raise


def _utc_to_local(utc_naive, tz_name):
    """Naive UTC datetime -> naive local datetime in tz_name."""
    from datetime import timezone
    z = _zone(tz_name)
    if isinstance(z, _SydneyFallback):
        return utc_naive + z.offset(utc_naive)
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(z).replace(tzinfo=None)


def _local_midnight_utc(day, tz_name):
    """Naive UTC instant of 00:00 local time on `day`."""
    from datetime import datetime, timedelta, timezone
    z = _zone(tz_name)
    naive = datetime(day.year, day.month, day.day)
    if isinstance(z, _SydneyFallback):
        # Sydney midnight is never inside a transition hour, so one estimate is exact.
        return naive - z.offset(naive - timedelta(hours=10))
    return naive.replace(tzinfo=z).astimezone(timezone.utc).replace(tzinfo=None)


def utc_iso(dt):
    """Format a naive UTC datetime like transcript timestamps (2026-11-03T07:50:10.050Z).

    Turns are compared as strings, so a bound must use the same layout: a bare
    '...:00Z' sorts after '...:00.000Z' and would drop a turn on the boundary.
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def local_day(utc_ts, tz_name=None):
    """Local calendar date (YYYY-MM-DD) of a transcript UTC timestamp."""
    from datetime import datetime
    dt = datetime.fromisoformat(utc_ts.replace("Z", "+00:00")).replace(tzinfo=None)
    return _utc_to_local(dt, tz_name or RANGE_TZ).strftime("%Y-%m-%d")


def _parse_date(value):
    from datetime import date
    if not value or not _DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def resolve_range(preset=None, from_date=None, to_date=None, tz_name=None, now=None):
    """Resolve a by-chat range to whole local days.

    preset: "today" | "7d" | "30d" (ending today, local time), else custom/all.
    from_date / to_date: inclusive YYYY-MM-DD local dates; either may be open.
    now: naive UTC datetime (tests). Returns {"preset", "from", "to", "tz",
    "start_utc", "end_utc"}, with None for an open side.
    """
    from datetime import datetime, timedelta, timezone
    tz_name = tz_name or RANGE_TZ
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    f = t = None
    if preset in RANGE_PRESETS:
        t = _utc_to_local(now, tz_name).date()
        f = t - timedelta(days=RANGE_PRESETS[preset] - 1)
    else:
        f, t = _parse_date(from_date), _parse_date(to_date)
        if f and t and t < f:
            f, t = t, f
        preset = "custom" if (f or t) else "all"
    return {
        "preset": preset,
        "from": f.isoformat() if f else None,
        "to": t.isoformat() if t else None,
        "tz": tz_name,
        "start_utc": utc_iso(_local_midnight_utc(f, tz_name)) if f else None,
        "end_utc": utc_iso(_local_midnight_utc(t + timedelta(days=1), tz_name)) if t else None,
    }


def session_contexts(conn, start_iso=None, end_iso=None):
    """{session_id: {"context", "last_ts"}} from each session's last main-thread
    turn in the window: input + cache_read + cache_creation, i.e. how big the
    prompt was at that point (subagent sidechains have their own context)."""
    where, params = ["COALESCE(is_subagent, 0) = 0"], []
    if start_iso:
        where.append("timestamp >= ?")
        params.append(start_iso)
    if end_iso:
        where.append("timestamp < ?")
        params.append(end_iso)
    # SQLite returns the bare columns from the MAX(timestamp) row.
    sql = ("SELECT session_id, MAX(timestamp), "
           "COALESCE(input_tokens, 0) + COALESCE(cache_read_tokens, 0) + COALESCE(cache_creation_tokens, 0) "
           "FROM turns WHERE " + " AND ".join(where) + " GROUP BY session_id")
    return {sid: {"context": ctx or 0, "last_ts": ts or ""}
            for sid, ts, ctx in conn.execute(sql, params)}


def get_by_chat_data(db_path, start_iso=None, end_iso=None, idx=None, tz_name=None):
    """Per-chat token totals for turns with start_iso <= timestamp < end_iso (UTC ISO).

    Filtering is per turn (each assistant message's own timestamp), so a
    long-lived chat session only contributes its turns inside the window, and
    a bucket's `sessions` is the number of distinct Claude sessions with at
    least one turn in the window.

    Returns {"buckets": [{id, label, kind, sessions, models: {model: {...}}}],
    "days": [{date, tokens, turns, sessions}], "totals": {...}, ...}; `days`
    are local calendar days in tz_name. Costs are computed client-side per
    model with the dashboard's PRICING.
    """
    tz_name = tz_name or RANGE_TZ
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        where, params = [], []
        if start_iso:
            where.append("t.timestamp >= ?")
            params.append(start_iso)
        if end_iso:
            where.append("t.timestamp < ?")
            params.append(end_iso)
        # Also grouped by UTC hour: tz offsets here are whole hours, so each
        # row falls on exactly one local day for the per-day totals.
        sql = """
            SELECT t.session_id, COALESCE(NULLIF(t.model, ''), 'unknown') AS turn_model,
                   substr(t.timestamp, 1, 13) AS hour,
                   SUM(t.input_tokens), SUM(t.output_tokens),
                   SUM(t.cache_read_tokens), SUM(t.cache_creation_tokens), COUNT(*),
                   MAX(s.project_name), MAX(t.cwd)
            FROM turns t LEFT JOIN sessions s ON s.session_id = t.session_id
        """ + (" WHERE " + " AND ".join(where) if where else "") + """
            GROUP BY t.session_id, turn_model, hour
        """
        rows = conn.execute(sql, params).fetchall()
        session_ids = sorted({r[0] for r in rows if r[0]})
        refresh_hints(conn, session_ids)
        hints = dict(conn.execute("SELECT session_id, hint FROM openclaw_hints").fetchall())
        contexts = session_contexts(conn, start_iso, end_iso)
    finally:
        conn.close()

    if idx is None:
        idx = get_index()
    buckets = {}
    days = {}
    matched = {"binding": 0, "marker": 0, "none": 0}
    cache = {}
    day_of_hour = {}
    for sid, model, hour, inp, out, cr, cc, turns, project, cwd in rows:
        if sid not in cache:
            cache[sid] = attribute_session(sid, hints.get(sid), idx, project, cwd)
            matched[cache[sid][3]] += 1
        bid, label, kind, _how = cache[sid]
        b = buckets.setdefault(bid, {"id": bid, "label": label, "kind": kind,
                                     "sessions": set(), "models": {}})
        b["sessions"].add(sid)
        m = b["models"].setdefault(model, {"input": 0, "output": 0, "cache_read": 0,
                                           "cache_creation": 0, "turns": 0})
        m["input"] += inp or 0
        m["output"] += out or 0
        m["cache_read"] += cr or 0
        m["cache_creation"] += cc or 0
        m["turns"] += turns or 0
        if hour not in day_of_hour:
            try:
                day_of_hour[hour] = local_day(hour + ":00:00Z", tz_name)
            except (TypeError, ValueError):
                day_of_hour[hour] = None
        dk = day_of_hour[hour]
        if dk:
            d = days.setdefault(dk, {"date": dk, "tokens": 0, "turns": 0, "sessions": set()})
            d["tokens"] += (inp or 0) + (out or 0) + (cr or 0) + (cc or 0)
            d["turns"] += turns or 0
            d["sessions"].add(sid)
    # The laptop bucket is always listed (at zero if need be), so it's clear
    # direct Claude Code use is tracked, not silently missing.
    buckets.setdefault(OUTSIDE_ID, {"id": OUTSIDE_ID, "label": OUTSIDE_LABEL, "kind": "outside",
                                    "sessions": set(), "models": {}})
    out_buckets = []
    for b in buckets.values():
        ctx = sorted((dict(contexts[s], session_id=s) for s in b["sessions"] if s in contexts),
                     key=lambda c: c["last_ts"], reverse=True)
        # Context now = the most recently active session's last main-thread
        # turn; a long-lived chat keeps re-reading that much as cache on every turn.
        b["context"] = ctx[0]["context"] if ctx else 0
        b["context_sessions"] = ctx[:5]
        b["sessions"] = len(b["sessions"])
        out_buckets.append(b)
    out_days = []
    for dk in sorted(days):
        d = days[dk]
        d["sessions"] = len(d["sessions"])
        out_days.append(d)
    totals = {
        "tokens": sum(sum(m["input"] + m["output"] + m["cache_read"] + m["cache_creation"]
                          for m in b["models"].values()) for b in out_buckets),
        "turns": sum(sum(m["turns"] for m in b["models"].values()) for b in out_buckets),
        "sessions": len(cache),
    }
    return {"buckets": out_buckets, "days": out_days, "totals": totals,
            "sessions_by_signal": matched}


# ── By tool ──────────────────────────────────────────────────────────────────
#
# Tools aren't billed on their own. What a tool costs is its result: the text
# goes into the prompt, and from then on every later turn of that context
# re-reads it (mostly as cache reads) until the context is compacted or reset.
# Both numbers below are ESTIMATES from transcript sizes, not API counts.

CHARS_PER_TOKEN = 4       # rough average for English text, code and JSON
IMAGE_TOKENS = 1600       # a typical screenshot-sized image block
COMPACTION_DROP = 0.5     # context shrinking below half of the previous turn = compaction/reset


def est_tokens(chars, images=0):
    return int(round((chars or 0) / CHARS_PER_TOKEN)) + (images or 0) * IMAGE_TOKENS


def _context_timelines(conn, session_ids, start_iso, end_iso):
    """{(session_id, agent_id or ''): (turn timestamps, compaction timestamps)}.

    A compaction is a turn whose prompt is less than COMPACTION_DROP of the
    previous turn's prompt in the same context; results from before it are no
    longer re-read afterwards.
    """
    from collections import defaultdict
    if not session_ids:
        return {}
    where, params = [], []
    if start_iso:
        where.append("timestamp >= ?")
        params.append(start_iso)
    if end_iso:
        where.append("timestamp < ?")
        params.append(end_iso)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _tool_sids (sid TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _tool_sids")
    conn.executemany("INSERT OR IGNORE INTO _tool_sids VALUES (?)", [(s,) for s in session_ids])
    sql = ("SELECT session_id, COALESCE(agent_id, ''), timestamp, "
           "COALESCE(input_tokens, 0) + COALESCE(cache_read_tokens, 0) + COALESCE(cache_creation_tokens, 0) "
           "FROM turns WHERE session_id IN (SELECT sid FROM _tool_sids)"
           + (" AND " + " AND ".join(where) if where else "") + " ORDER BY session_id, timestamp")
    out = defaultdict(lambda: ([], []))
    prev = {}
    for sid, aid, ts, ctx in conn.execute(sql, params):
        key = (sid, aid)
        times, compactions = out[key]
        p = prev.get(key)
        if p and ctx < p * COMPACTION_DROP:
            compactions.append(ts)
        prev[key] = ctx
        times.append(ts)
    return dict(out)


def get_by_tool_data(db_path, start_iso=None, end_iso=None, chat=None, idx=None):
    """Per-tool call counts and estimated context tokens for calls in the window.

    chat: a bucket id from get_by_chat_data (None = all chats).
    Per tool: calls, errors, sessions, result_tokens (est. tokens of results
    added to context), input_tokens (est. tokens of the call arguments, which
    are model output), and reread_tokens (est. result tokens re-read on later
    turns of the same context inside the window, stopping at a compaction).
    """
    from bisect import bisect_right
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        have = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_calls'").fetchone()
        if not have:
            return {"tools": [], "chats": [], "totals": {"calls": 0, "result_tokens": 0, "reread_tokens": 0}}
        where, params = [], []
        ts_expr = "COALESCE(NULLIF(tc.timestamp, ''), tc.result_ts)"
        if start_iso:
            where.append(ts_expr + " >= ?")
            params.append(start_iso)
        if end_iso:
            where.append(ts_expr + " < ?")
            params.append(end_iso)
        sql = ("SELECT tc.session_id, COALESCE(tc.agent_id, ''), COALESCE(tc.tool_name, 'unknown'), "
               "COALESCE(tc.result_ts, " + ts_expr + "), COALESCE(tc.input_chars, 0), "
               "COALESCE(tc.result_chars, 0), COALESCE(tc.result_images, 0), COALESCE(tc.is_error, 0), "
               "s.project_name FROM tool_calls tc LEFT JOIN sessions s ON s.session_id = tc.session_id"
               + (" WHERE " + " AND ".join(where) if where else ""))
        rows = conn.execute(sql, params).fetchall()
        session_ids = sorted({r[0] for r in rows if r[0]})
        refresh_hints(conn, session_ids)
        hints = dict(conn.execute("SELECT session_id, hint FROM openclaw_hints").fetchall())
        timelines = _context_timelines(conn, session_ids, start_iso, end_iso)
    finally:
        conn.close()

    if idx is None:
        idx = get_index()
    attrib = {}
    chats = {}
    tools = {}
    for sid, aid, name, rts, in_chars, res_chars, images, err, project in rows:
        if sid not in attrib:
            attrib[sid] = attribute_session(sid, hints.get(sid), idx, project)
        bid, label, kind, _how = attrib[sid]
        c = chats.setdefault(bid, {"id": bid, "label": label, "kind": kind, "calls": 0})
        c["calls"] += 1
        if chat and bid != chat:
            continue
        res_tok = est_tokens(res_chars, images)
        rereads = 0
        tl = timelines.get((sid, aid))
        if tl and rts:
            times, compactions = tl
            later = bisect_right(times, rts)
            stop = len(times)
            ci = bisect_right(compactions, rts)
            if ci < len(compactions):
                stop = bisect_right(times, compactions[ci]) - 1  # the compacting turn no longer carries it
            # The first later turn reads the result fresh (cache write); the rest re-read it.
            rereads = max(0, stop - later - 1)
        t = tools.setdefault(name, {"tool": name, "calls": 0, "errors": 0, "sessions": set(),
                                    "result_tokens": 0, "input_tokens": 0, "reread_tokens": 0,
                                    "max_result_tokens": 0})
        t["calls"] += 1
        t["errors"] += err
        t["sessions"].add(sid)
        t["result_tokens"] += res_tok
        t["input_tokens"] += est_tokens(in_chars)
        t["reread_tokens"] += res_tok * rereads
        t["max_result_tokens"] = max(t["max_result_tokens"], res_tok)
    out = []
    for t in tools.values():
        t["sessions"] = len(t["sessions"])
        t["avg_result_tokens"] = int(round(t["result_tokens"] / t["calls"])) if t["calls"] else 0
        out.append(t)
    out.sort(key=lambda t: t["result_tokens"], reverse=True)
    totals = {k: sum(t[k] for t in out) for k in ("calls", "result_tokens", "input_tokens", "reread_tokens")}
    return {"tools": out, "totals": totals,
            "chats": sorted(chats.values(), key=lambda c: c["calls"], reverse=True),
            "chat": chat, "estimate": {"chars_per_token": CHARS_PER_TOKEN, "image_tokens": IMAGE_TOKENS}}

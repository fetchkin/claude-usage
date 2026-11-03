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
        return (OUTSIDE_ID, "Claude Code outside OpenClaw", "outside", "none")
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


def get_by_chat_data(db_path, start_iso=None, end_iso=None, idx=None):
    """Per-chat token totals for turns with start_iso <= timestamp < end_iso (UTC ISO).

    Returns {"buckets": [{id, label, kind, sessions, models: {model: {...}}}], ...}.
    Costs are computed client-side per model with the dashboard's PRICING.
    """
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
        sql = """
            SELECT t.session_id, COALESCE(NULLIF(t.model, ''), 'unknown') AS turn_model,
                   SUM(t.input_tokens), SUM(t.output_tokens),
                   SUM(t.cache_read_tokens), SUM(t.cache_creation_tokens), COUNT(*),
                   MAX(s.project_name), MAX(t.cwd)
            FROM turns t LEFT JOIN sessions s ON s.session_id = t.session_id
        """ + (" WHERE " + " AND ".join(where) if where else "") + """
            GROUP BY t.session_id, turn_model
        """
        rows = conn.execute(sql, params).fetchall()
        session_ids = sorted({r[0] for r in rows if r[0]})
        refresh_hints(conn, session_ids)
        hints = dict(conn.execute("SELECT session_id, hint FROM openclaw_hints").fetchall())
    finally:
        conn.close()

    if idx is None:
        idx = get_index()
    buckets = {}
    matched = {"binding": 0, "marker": 0, "none": 0}
    cache = {}
    for sid, model, inp, out, cr, cc, turns, project, cwd in rows:
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
    out_buckets = []
    for b in buckets.values():
        b["sessions"] = len(b["sessions"])
        out_buckets.append(b)
    return {"buckets": out_buckets, "sessions_by_signal": matched}

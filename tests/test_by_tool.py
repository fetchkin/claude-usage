"""Tests for the By-tool view: tool_calls scanning and the per-tool estimates."""

import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import openclaw_attrib as oa
import scanner
from scanner import get_db, init_db, upsert_sessions, insert_turns, upsert_tool_calls
from tests.test_openclaw_attrib import CHAN, make_openclaw_home


def _assistant(ts, mid, blocks, sid="sess1", usage=None):
    return {"type": "assistant", "sessionId": sid, "timestamp": ts, "cwd": "C:/Users/x/.openclaw/workspace",
            "message": {"id": mid, "model": "claude-opus-5-5", "content": blocks,
                        "usage": usage or {"input_tokens": 10, "output_tokens": 5}}}


def _tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _result(ts, tid, content, sid="sess1", is_error=False):
    return {"type": "user", "sessionId": sid, "timestamp": ts,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": is_error}]}}


def _write(path, records, mode="w"):
    with open(path, mode, encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class TestToolCallScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = Path(self.tmp) / "projects" / "C--Users-x--openclaw-workspace"
        self.proj.mkdir(parents=True)
        self.file = self.proj / "sess1.jsonl"
        self.db = Path(self.tmp) / "usage.db"
        use = _tool_use("tu1", "Read", {"file_path": "C:/a.txt"})
        _write(self.file, [
            {"type": "user", "sessionId": "sess1", "timestamp": "2026-11-01T01:00:00.000Z",
             "message": {"role": "user", "content": "hi"}},
            _assistant("2026-11-01T01:00:01.000Z", "m1", [use]),
            _assistant("2026-11-01T01:00:01.500Z", "m1", [use]),  # streaming replay
            _result("2026-11-01T01:00:02.000Z", "tu1", "x" * 400),
            _assistant("2026-11-01T01:00:03.000Z", "m2", [_tool_use("tu2", "mcp__openclaw__browser", {})]),
            _result("2026-11-01T01:00:04.000Z", "tu2",
                    [{"type": "text", "text": "y" * 80}, {"type": "image", "source": {"data": "zz"}}],
                    is_error=True),
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self):
        scanner.scan(db_path=self.db, projects_dirs=[self.proj.parent], verbose=False)

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return {r[0]: r[1:] for r in conn.execute(
                "SELECT tool_use_id, tool_name, session_id, result_chars, result_images, is_error, input_chars "
                "FROM tool_calls")}
        finally:
            conn.close()

    def test_use_and_result_join_on_id(self):
        self.scan()
        rows = self.rows()
        self.assertEqual(set(rows), {"tu1", "tu2"})  # the replayed tool_use collapses onto one row
        self.assertEqual(rows["tu1"][:5], ("Read", "sess1", 400, 0, 0))
        self.assertEqual(rows["tu1"][5], len(json.dumps({"file_path": "C:/a.txt"})))
        self.assertEqual(rows["tu2"][:5], ("mcp__openclaw__browser", "sess1", 80, 1, 1))

    def test_result_arriving_in_a_later_scan(self):
        self.scan()
        _write(self.file, [_assistant("2026-11-01T01:00:05.000Z", "m3", [_tool_use("tu3", "Bash", {"command": "ls"})])], "a")
        self.scan()
        self.assertEqual(self.rows()["tu3"][:3], ("Bash", "sess1", None))
        _write(self.file, [_result("2026-11-01T01:00:06.000Z", "tu3", "z" * 12)], "a")
        self.scan()
        self.assertEqual(self.rows()["tu3"][:3], ("Bash", "sess1", 12))

    def test_backfill_fills_already_processed_files(self):
        self.scan()
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM tool_calls")  # as if the DB predates tool_calls
        conn.execute("DELETE FROM schema_meta WHERE key = 'tool_calls_backfill_done'")
        conn.commit()
        conn.close()
        self.scan()  # file unchanged, so only the backfill can see it
        self.assertEqual(set(self.rows()), {"tu1", "tu2"})


def _turn(sid, ts, ctx, mid, agent_id=None):
    return {"session_id": sid, "timestamp": ts, "model": "claude-opus-5-5", "input_tokens": 0,
            "output_tokens": 1, "cache_read_tokens": ctx, "cache_creation_tokens": 0, "tool_name": None,
            "cwd": "C:/Users/x/.openclaw/workspace", "message_id": mid, "agent_id": agent_id}


class TestByToolData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = Path(self.tmp) / "openclaw"
        self.home.mkdir()
        make_openclaw_home(self.home)
        self.idx = oa.load_index(self.home)
        self.db = Path(self.tmp) / "usage.db"
        conn = get_db(self.db)
        init_db(conn)
        upsert_sessions(conn, [
            {"session_id": sid, "project_name": ".openclaw/workspace", "first_timestamp": "2026-11-01T01:00:00Z",
             "last_timestamp": "2026-11-01T02:00:00Z", "git_branch": "", "model": "claude-opus-5-5",
             "total_input_tokens": 0, "total_output_tokens": 0, "total_cache_read": 0,
             "total_cache_creation": 0, "turn_count": 0}
            for sid in ("cli-chan", "mystery")])
        # cli-chan: context grows, then drops below half at 01:04 (compaction).
        ctxs = [1000, 1100, 1200, 300, 400, 500]
        insert_turns(conn, [_turn("cli-chan", "2026-11-01T01:0%d:00.000Z" % (i + 1), c, "c%d" % i)
                            for i, c in enumerate(ctxs)]
                     + [_turn("mystery", "2026-11-01T01:00:00.000Z", 50, "u0"),
                        _turn("mystery", "2026-11-01T01:09:00.000Z", 70, "u1")])
        upsert_tool_calls(conn, [
            # Read result (400 chars = 100 tokens) lands after the 01:01 turn.
            {"kind": "use", "id": "a", "name": "Read", "session_id": "cli-chan", "agent_id": None,
             "timestamp": "2026-11-01T01:01:00.000Z", "input_chars": 40},
            {"kind": "result", "id": "a", "session_id": "cli-chan", "agent_id": None,
             "timestamp": "2026-11-01T01:01:30.000Z", "chars": 400, "images": 0, "is_error": 0},
            # Image result after the compaction: 01:05 writes it, 01:06 re-reads it.
            {"kind": "use", "id": "b", "name": "mcp__openclaw__view_image", "session_id": "cli-chan",
             "agent_id": None, "timestamp": "2026-11-01T01:04:00.000Z", "input_chars": 8},
            {"kind": "result", "id": "b", "session_id": "cli-chan", "agent_id": None,
             "timestamp": "2026-11-01T01:04:30.000Z", "chars": 0, "images": 1, "is_error": 0},
            {"kind": "use", "id": "c", "name": "Bash", "session_id": "mystery", "agent_id": None,
             "timestamp": "2026-11-01T01:00:10.000Z", "input_chars": 4},
            {"kind": "result", "id": "c", "session_id": "mystery", "agent_id": None,
             "timestamp": "2026-11-01T01:00:20.000Z", "chars": 8, "images": 0, "is_error": 1},
            # Outside the range.
            {"kind": "use", "id": "old", "name": "Read", "session_id": "cli-chan", "agent_id": None,
             "timestamp": "2026-10-01T00:00:00.000Z", "input_chars": 4},
        ])
        conn.commit()
        conn.close()
        self.start, self.end = "2026-10-31T13:00:00.000Z", "2026-11-01T13:00:00.000Z"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_estimates_and_rereads_stop_at_compaction(self):
        d = oa.get_by_tool_data(self.db, self.start, self.end, idx=self.idx)
        by = {t["tool"]: t for t in d["tools"]}
        self.assertEqual(set(by), {"Read", "mcp__openclaw__view_image", "Bash"})
        self.assertEqual((by["Read"]["calls"], by["Read"]["result_tokens"], by["Read"]["input_tokens"]), (1, 100, 10))
        # 01:02 writes it to cache, 01:03 re-reads it, the 01:04 compaction drops it.
        self.assertEqual(by["Read"]["reread_tokens"], 100)
        self.assertEqual(by["mcp__openclaw__view_image"]["result_tokens"], oa.IMAGE_TOKENS)
        self.assertEqual(by["mcp__openclaw__view_image"]["reread_tokens"], oa.IMAGE_TOKENS)
        self.assertEqual((by["Bash"]["result_tokens"], by["Bash"]["errors"], by["Bash"]["reread_tokens"]), (2, 1, 0))
        self.assertEqual(d["tools"][0]["tool"], "mcp__openclaw__view_image")  # sorted by result tokens
        self.assertEqual(d["totals"]["calls"], 3)

    def test_chat_filter(self):
        d = oa.get_by_tool_data(self.db, self.start, self.end, chat=CHAN, idx=self.idx)
        self.assertEqual({t["tool"] for t in d["tools"]}, {"Read", "mcp__openclaw__view_image"})
        # The dropdown still lists every chat with calls in range.
        self.assertEqual({c["id"]: c["calls"] for c in d["chats"]}, {CHAN: 2, oa.UNMAPPED_ID: 1})

    def test_by_chat_context_now(self):
        d = oa.get_by_chat_data(self.db, self.start, self.end, idx=self.idx)
        by_id = {b["id"]: b for b in d["buckets"]}
        self.assertEqual(by_id[CHAN]["context"], 500)  # last main-thread turn, not the peak
        self.assertEqual(by_id[oa.UNMAPPED_ID]["context"], 70)
        self.assertEqual(by_id[oa.OUTSIDE_ID]["label"], "Claude Code on this laptop (not via OpenClaw)")
        # A window ending before the last turns reports the context as of then.
        early = oa.get_by_chat_data(self.db, self.start, "2026-11-01T01:03:30.000Z", idx=self.idx)
        self.assertEqual({b["id"]: b["context"] for b in early["buckets"]}[CHAN], 1200)

    def test_api_endpoint(self):
        import dashboard
        from http.server import HTTPServer
        with mock.patch.object(dashboard, "DB_PATH", self.db), \
                mock.patch.object(oa, "get_index", lambda *a, **k: self.idx):
            server = HTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = "http://127.0.0.1:%d/api/by-tool?" % server.server_address[1]
                with urllib.request.urlopen(base + "from=2026-11-01&to=2026-11-01") as r:
                    d = json.loads(r.read())
                with urllib.request.urlopen(base + "from=2026-11-01&to=2026-11-01&chat=" + CHAN) as r:
                    d2 = json.loads(r.read())
            finally:
                server.shutdown()
                server.server_close()
        self.assertEqual(d["range"]["start_utc"], self.start)
        self.assertEqual(d["totals"]["calls"], 3)
        self.assertEqual(d2["totals"]["calls"], 2)
        self.assertEqual(d2["chat"], CHAN)


if __name__ == "__main__":
    unittest.main()

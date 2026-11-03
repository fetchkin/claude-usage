"""Tests for openclaw_attrib.py - mapping Claude sessions to OpenClaw chats."""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import openclaw_attrib as oa
from scanner import get_db, init_db, upsert_sessions, insert_turns

AGENT = "agent:james"
CHAN = AGENT + ":slack:channel:c0c8fgnn6hw"
THREAD = CHAN + ":thread:1792401077.746659"
SUB1 = AGENT + ":subagent:11111111-1111-1111-1111-111111111111"
SUB2 = AGENT + ":subagent:22222222-2222-2222-2222-222222222222"
CRON = AGENT + ":cron:9245106b-65db-41f4-86ae-135062010c46"
TASK = "Retention OS: build the Ask button.\n\nRules: work on a branch."


def make_openclaw_home(root):
    """Minimal OpenClaw stores with the tables/columns the module reads."""
    agent_dir = Path(root) / "agents" / "james" / "agent"
    agent_dir.mkdir(parents=True)
    conn = sqlite3.connect(agent_dir / "openclaw-agent.sqlite")
    conn.execute("""CREATE TABLE session_nodes (session_key TEXT PRIMARY KEY, current_session_id TEXT,
                    entry_json TEXT, label TEXT, display_name TEXT, parent_session_key TEXT, spawned_by TEXT)""")
    conn.execute("""CREATE TABLE conversations (conversation_id TEXT, native_channel_id TEXT, label TEXT)""")
    nodes = [
        (CHAN, {"cliSessionIds": {"claude-cli": "cli-chan"},
                "delivery": {"origin": {"label": "#dream100-copy channel"}}}, None, "slack:buzzifyboost-#dream100-copy", None, None),
        (THREAD, {"cliSessionBindings": {"claude-cli": {"sessionId": "cli-thread"}}}, None, None, None, None),
        (AGENT + ":main", {"cliSessionIds": {"claude-cli": "cli-main"}}, None, None, None, None),
        (SUB1, {}, None, None, None, THREAD),
        (SUB2, {}, None, None, None, None),  # parent only via subagent_runs
        (CRON, {}, "Automation: Dream 100 copy watcher", None, None, None),
        (AGENT + ":slack:channel:c0zzz", {}, None, None, None, None),  # name only in conversations
    ]
    for key, entry, label, dn, parent, spawned in nodes:
        conn.execute("INSERT INTO session_nodes VALUES (?,?,?,?,?,?,?)",
                     (key, "x", json.dumps(entry), label, dn, parent, spawned))
    conn.execute("INSERT INTO conversations VALUES ('c1', 'C0ZZZ', 'slack:buzzifyboost-#ai-dashboard')")
    conn.commit()
    conn.close()

    state_dir = Path(root) / "state"
    state_dir.mkdir()
    conn = sqlite3.connect(state_dir / "openclaw.sqlite")
    conn.execute("""CREATE TABLE subagent_runs (run_id TEXT, child_session_key TEXT, requester_session_key TEXT,
                    created_at INTEGER, payload_json TEXT)""")
    conn.execute("CREATE TABLE cron_jobs (job_id TEXT, name TEXT)")
    conn.execute("INSERT INTO subagent_runs VALUES ('r1', ?, ?, 1000000, ?)",
                 (SUB1, THREAD, json.dumps({"task": "Some other task"})))
    conn.execute("INSERT INTO subagent_runs VALUES ('r2', ?, ?, 2000000, ?)",
                 (SUB2, SUB1, json.dumps({"task": TASK})))
    conn.execute("INSERT INTO cron_jobs VALUES ('9245106b-65db-41f4-86ae-135062010c46', 'Dream 100 copy watcher')")
    conn.commit()
    conn.close()


class TestHints(unittest.TestCase):
    def test_cron_marker(self):
        h = oa.hint_from_prompt("[cron:9245106B-65db-41f4-86ae-135062010c46 Dream 100 copy watcher] check ClickUp")
        self.assertEqual(h, "cron:9245106b-65db-41f4-86ae-135062010c46")

    def test_cron_marker_after_timestamp(self):
        h = oa.hint_from_prompt("[Tue 2026-11-03 09:00 GMT+11] [cron:2f00b1db-6781-4fab-b618-6970c0166890 kpi]")
        self.assertEqual(h, "cron:2f00b1db-6781-4fab-b618-6970c0166890")

    def test_subagent_marker_hashes_task_body(self):
        prompt = ("[Tue 2026-10-27 20:55 GMT+11] [Subagent Context] Complete the current [Subagent Task]; "
                  "inherited conversation is background.\n\n[Subagent Task]\n" + TASK)
        h = oa.hint_from_prompt(prompt, 2_000_500)
        self.assertEqual(h, "task:%s@2000500" % oa.task_fingerprint(TASK))
        self.assertNotIn("Retention", h)  # marker never carries message text

    def test_chat_marker(self):
        prompt = 'Conversation info (untrusted metadata):\n```json\n{"chat_id": "channel:C0CCHTM87S5"}\n```\nhi'
        self.assertEqual(oa.hint_from_prompt(prompt), "chat:channel:C0CCHTM87S5")

    def test_no_marker(self):
        self.assertEqual(oa.hint_from_prompt("just a question"), "none")
        self.assertEqual(oa.hint_from_prompt(""), "none")

    def test_fingerprint_ignores_whitespace(self):
        self.assertEqual(oa.task_fingerprint("a  b\n\nc"), oa.task_fingerprint("a b c"))

    def test_base_key_and_names(self):
        self.assertEqual(oa.base_key(THREAD), CHAN)
        self.assertEqual(oa.base_key(CRON + ":run:abc"), CRON)
        self.assertEqual(oa.channel_name_from_labels(None, "BuzzifyBoost #ai-dream100"), "#ai-dream100")
        self.assertIsNone(oa.channel_name_from_labels("slack:channel:C0 channel"))


class TestAttribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        make_openclaw_home(cls.home)
        cls.idx = oa.load_index(cls.home)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def attr(self, sid, hint=None, project=".openclaw/workspace"):
        return oa.attribute_session(sid, hint, self.idx, project)

    def test_binding_channel(self):
        self.assertEqual(self.attr("cli-chan")[:3], (CHAN, "#dream100-copy", "channel"))
        self.assertEqual(self.attr("cli-chan")[3], "binding")

    def test_thread_rolls_up_to_channel(self):
        self.assertEqual(self.attr("cli-thread")[0], CHAN)

    def test_main_dm(self):
        self.assertEqual(self.attr("cli-main")[2], "dm")

    def test_cron_marker_uses_job_name(self):
        bid, label, kind, how = self.attr("old", "cron:9245106b-65db-41f4-86ae-135062010c46")
        self.assertEqual((bid, label, kind, how), (CRON, "Automation: Dream 100 copy watcher", "cron", "marker"))

    def test_cron_run_key_rolls_up(self):
        self.assertEqual(self.idx.bucket_for_key(CRON + ":run:xyz")[0], CRON)

    def test_chat_marker_maps_channel(self):
        self.assertEqual(self.attr("old", "chat:channel:C0C8FGNN6HW")[0], CHAN)

    def test_channel_name_from_conversations(self):
        self.assertEqual(self.attr("old", "chat:channel:C0ZZZ")[1], "#ai-dashboard")

    def test_nested_subagent_rolls_up_to_spawning_channel(self):
        # SUB2 (task match) -> requester SUB1 -> spawned_by THREAD -> CHAN
        hint = "task:%s@2000900" % oa.task_fingerprint(TASK)
        self.assertEqual(self.attr("worker", hint)[:3], (CHAN, "#dream100-copy", "channel"))

    def test_subagent_time_fallback(self):
        # Unknown task text, but spawned 30s before the transcript started -> SUB2.
        hint = "task:%s@2030000" % oa.task_fingerprint("unrecognised")
        self.assertEqual(self.attr("worker", hint)[0], CHAN)

    def test_subagent_too_far_in_time_is_unmapped(self):
        hint = "task:%s@9000000" % oa.task_fingerprint("unrecognised")
        self.assertEqual(self.attr("worker", hint)[0], oa.UNMAPPED_ID)

    def test_unmapped_vs_outside(self):
        self.assertEqual(self.attr("nope", "none")[0], oa.UNMAPPED_ID)
        self.assertEqual(self.attr("nope", "none", project="code/myapp")[0], oa.OUTSIDE_ID)

    def test_missing_openclaw_home_is_empty_index(self):
        idx = oa.load_index(os.path.join(self.home, "does-not-exist"))
        self.assertEqual(idx.cli_to_key, {})


class TestByChatData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "openclaw")
        os.makedirs(self.home)
        make_openclaw_home(self.home)
        self.db_path = Path(self.tmp) / "usage.db"
        proj = Path(self.tmp) / "projects" / "C--Users-x--openclaw-workspace"
        proj.mkdir(parents=True)
        # Transcript for the cron session (only its first prompt is read).
        (proj / "cron-sess.jsonl").write_text(json.dumps({
            "type": "queue-operation", "operation": "enqueue", "timestamp": "2026-11-01T00:00:00Z",
            "content": "[cron:9245106b-65db-41f4-86ae-135062010c46 Dream 100 copy watcher] go"}) + "\n",
            encoding="utf-8")
        conn = get_db(self.db_path)
        init_db(conn)
        sessions = []
        turns = []
        for sid, n in (("cli-chan", 3), ("cron-sess", 1), ("mystery", 1)):
            sessions.append({"session_id": sid, "project_name": ".openclaw/workspace",
                             "first_timestamp": "2026-11-01T01:00:00Z", "last_timestamp": "2026-11-01T02:00:00Z",
                             "git_branch": "", "model": "claude-opus-5-5", "total_input_tokens": 0,
                             "total_output_tokens": 0, "total_cache_read": 0, "total_cache_creation": 0,
                             "turn_count": n})
            for i in range(n):
                turns.append({"session_id": sid, "timestamp": "2026-11-01T01:0%d:00Z" % i,
                              "model": "claude-opus-5-5", "input_tokens": 100, "output_tokens": 10,
                              "cache_read_tokens": 1000, "cache_creation_tokens": 50, "tool_name": None,
                              "cwd": "C:/Users/x/.openclaw/workspace", "message_id": "%s-%d" % (sid, i)})
        # One turn outside the requested range.
        turns.append({"session_id": "cli-chan", "timestamp": "2026-10-01T00:00:00Z", "model": "claude-opus-5-5",
                      "input_tokens": 999999, "output_tokens": 0, "cache_read_tokens": 0,
                      "cache_creation_tokens": 0, "tool_name": None, "cwd": "", "message_id": "old"})
        upsert_sessions(conn, sessions)
        insert_turns(conn, turns)
        conn.execute("INSERT INTO processed_files (path, mtime, lines) VALUES (?, 0, 1)",
                     (str(proj / "cron-sess.jsonl"),))
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_buckets_and_range(self):
        idx = oa.load_index(self.home)
        d = oa.get_by_chat_data(self.db_path, "2026-10-31T13:00:00.000Z", None, idx=idx)
        by_id = {b["id"]: b for b in d["buckets"]}
        chan = by_id[CHAN]["models"]["claude-opus-5-5"]
        self.assertEqual(chan["input"], 300)  # the October turn is outside the range
        self.assertEqual(chan["turns"], 3)
        self.assertEqual(by_id[CRON]["label"], "Automation: Dream 100 copy watcher")
        self.assertEqual(by_id[oa.UNMAPPED_ID]["sessions"], 1)
        self.assertEqual(d["sessions_by_signal"], {"binding": 1, "marker": 1, "none": 1})
        # Markers are cached in usage.db, without any message text.
        conn = sqlite3.connect(self.db_path)
        hints = dict(conn.execute("SELECT session_id, hint FROM openclaw_hints"))
        conn.close()
        self.assertEqual(hints["cron-sess"], "cron:9245106b-65db-41f4-86ae-135062010c46")
        self.assertNotIn("go", hints["cron-sess"].split(" "))

    def test_api_endpoint(self):
        import dashboard
        from http.server import HTTPServer
        idx = oa.load_index(self.home)
        with mock.patch.object(dashboard, "DB_PATH", self.db_path), \
                mock.patch.object(dashboard, "_maybe_live_scan", lambda db_path: None), \
                mock.patch.object(oa, "get_index", lambda *a, **k: idx):
            server = HTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                port = server.server_address[1]
                url = "http://127.0.0.1:%d/api/by-chat?from=2026-10-31T13:00:00.000Z&to=bad'input" % port
                with urllib.request.urlopen(url) as r:
                    d = json.loads(r.read())
            finally:
                server.shutdown()
                server.server_close()
        self.assertEqual(d["from"], "2026-10-31T13:00:00.000Z")
        self.assertIsNone(d["to"])  # junk range values are dropped
        self.assertIn(CHAN, {b["id"] for b in d["buckets"]})


if __name__ == "__main__":
    unittest.main()

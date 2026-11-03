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
                base = "http://127.0.0.1:%d/api/by-chat?" % port
                with urllib.request.urlopen(base + "from=2026-10-31T13:00:00Z&to=bad'input") as r:
                    d = json.loads(r.read())
                # Sydney dates: 1 Nov AEDT = 31 Oct 13:00Z .. 1 Nov 13:00Z, `to` inclusive.
                with urllib.request.urlopen(base + "from=2026-11-01&to=2026-11-01") as r:
                    d2 = json.loads(r.read())
                with urllib.request.urlopen(base + "range=nonsense&from=2026-99-99") as r:
                    d3 = json.loads(r.read())
            finally:
                server.shutdown()
                server.server_close()
        self.assertEqual(d["from"], "2026-10-31T13:00:00.000Z")  # raw instants still work
        self.assertIsNone(d["to"])  # junk range values are dropped
        self.assertIn(CHAN, {b["id"] for b in d["buckets"]})
        self.assertEqual(d2["range"]["start_utc"], "2026-10-31T13:00:00.000Z")
        self.assertEqual(d2["range"]["end_utc"], "2026-11-01T13:00:00.000Z")
        self.assertEqual(d2["totals"]["sessions"], 3)
        self.assertEqual([x["date"] for x in d2["days"]], ["2026-11-01"])
        self.assertEqual(d3["range"]["preset"], "all")  # junk -> all time, not an error


def _turn(sid, ts, tokens, mid):
    return {"session_id": sid, "timestamp": ts, "model": "claude-opus-5-5", "input_tokens": tokens,
            "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0, "tool_name": None,
            "cwd": "C:/Users/x/.openclaw/workspace", "message_id": mid}


class TestRanges(unittest.TestCase):
    """Range maths: whole Australia/Sydney days, `to` inclusive."""

    def test_sydney_day_bounds_inclusive_to(self):
        r = oa.resolve_range(None, "2026-10-28", "2026-11-03", tz_name="Australia/Sydney")
        self.assertEqual(r["start_utc"], "2026-10-27T13:00:00.000Z")  # AEDT, +11
        self.assertEqual(r["end_utc"], "2026-11-03T13:00:00.000Z")    # end of 3 Nov, not its start
        r = oa.resolve_range(None, "2026-07-01", "2026-07-01", tz_name="Australia/Sydney")
        self.assertEqual(r["start_utc"], "2026-06-30T14:00:00.000Z")  # AEST, +10
        self.assertEqual(r["end_utc"], "2026-07-01T14:00:00.000Z")

    def test_presets_use_local_today(self):
        from datetime import datetime
        # 3 Nov 2026 07:50Z is 18:50 in Sydney; 14:00Z is already 4 Nov there.
        r = oa.resolve_range("7d", now=datetime(2026, 11, 3, 7, 50), tz_name="Australia/Sydney")
        self.assertEqual((r["from"], r["to"]), ("2026-10-28", "2026-11-03"))
        c = oa.resolve_range(None, "2026-10-28", "2026-11-03", tz_name="Australia/Sydney")
        self.assertEqual((r["start_utc"], r["end_utc"]), (c["start_utc"], c["end_utc"]))
        r = oa.resolve_range("today", now=datetime(2026, 11, 3, 14, 0), tz_name="Australia/Sydney")
        self.assertEqual((r["from"], r["to"]), ("2026-11-04", "2026-11-04"))

    def test_reversed_and_open_ranges(self):
        r = oa.resolve_range(None, "2026-11-03", "2026-10-28")
        self.assertEqual((r["from"], r["to"]), ("2026-10-28", "2026-11-03"))
        r = oa.resolve_range(None, None, "2026-11-03")
        self.assertIsNone(r["start_utc"])
        self.assertEqual(r["preset"], "custom")
        self.assertEqual(oa.resolve_range()["preset"], "all")

    def test_sydney_fallback_matches_zoneinfo(self):
        from datetime import date, datetime, timedelta
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("Australia/Sydney")
        except Exception:
            self.skipTest("no tzdata to compare against")
        fb = oa._SydneyFallback()
        day = date(2025, 1, 1)
        while day.year < 2028:
            m = datetime(day.year, day.month, day.day)
            self.assertEqual(oa._local_midnight_utc(day, "Australia/Sydney"),
                             m - fb.offset(m - timedelta(hours=10)), day)
            day += timedelta(days=1)

    def test_boundary_format_keeps_turn_on_the_edge(self):
        self.assertLess("2026-10-27T13:00:00.000Z", "2026-10-27T13:00:00Z")  # why utc_iso pads .000
        self.assertEqual(oa.local_day("2026-10-27T12:59:59.999Z", "Australia/Sydney"), "2026-10-27")
        self.assertEqual(oa.local_day("2026-10-27T13:00:00.000Z", "Australia/Sydney"), "2026-10-28")


class TestRangeAggregation(unittest.TestCase):
    """A long-lived session spanning range edges is split per turn."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "openclaw")
        os.makedirs(self.home)
        make_openclaw_home(self.home)
        self.idx = oa.load_index(self.home)
        self.db_path = Path(self.tmp) / "usage.db"
        conn = get_db(self.db_path)
        init_db(conn)
        # cli-chan: one channel session from 27 Oct to 4 Nov (Sydney), like #ai-dashboard.
        # cli-main: two turns on 28 Oct. cron-a / cron-b: one automation run each.
        turns = [
            _turn("cli-chan", "2026-10-26T13:00:00.000Z", 1000, "c1"),   # 27 Oct 00:00 Sydney
            _turn("cli-chan", "2026-10-27T12:59:59.999Z", 2000, "c2"),   # 27 Oct 23:59 Sydney
            _turn("cli-chan", "2026-10-27T13:00:00.000Z", 30, "c3"),     # 28 Oct 00:00 Sydney
            _turn("cli-chan", "2026-11-03T07:00:00.000Z", 400, "c4"),    # 3 Nov 18:00 Sydney
            _turn("cli-chan", "2026-11-03T13:00:00.000Z", 50000, "c5"),  # 4 Nov 00:00 Sydney
            _turn("cli-main", "2026-10-28T01:00:00.000Z", 5, "m1"),
            _turn("cli-main", "2026-10-28T02:00:00.000Z", 5, "m2"),
            _turn("cron-a", "2026-10-29T00:00:00.000Z", 7, "a1"),
            _turn("cron-b", "2026-11-02T00:00:00.000Z", 9, "b1"),
        ]
        # Session-level timestamps deliberately say "27 Oct" only: they must not matter.
        sessions = [{"session_id": sid, "project_name": ".openclaw/workspace",
                     "first_timestamp": "2026-10-26T13:00:00Z", "last_timestamp": "2026-10-26T13:00:00Z",
                     "git_branch": "", "model": "claude-opus-5-5", "total_input_tokens": 0,
                     "total_output_tokens": 0, "total_cache_read": 0, "total_cache_creation": 0,
                     "turn_count": 1} for sid in ("cli-chan", "cli-main", "cron-a", "cron-b")]
        upsert_sessions(conn, sessions)
        insert_turns(conn, turns)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def data(self, f, t):
        r = oa.resolve_range(None, f, t, tz_name="Australia/Sydney")
        return oa.get_by_chat_data(self.db_path, r["start_utc"], r["end_utc"], idx=self.idx,
                                   tz_name="Australia/Sydney")

    def chan_tokens(self, d):
        b = {x["id"]: x for x in d["buckets"]}.get(CHAN)
        return b["models"]["claude-opus-5-5"]["input"] if b else 0

    def test_session_spanning_boundary_is_split(self):
        self.assertEqual(self.chan_tokens(self.data("2026-10-27", "2026-10-27")), 3000)
        self.assertEqual(self.chan_tokens(self.data("2026-10-28", "2026-11-03")), 430)
        self.assertEqual(self.chan_tokens(self.data("2026-11-04", "2026-11-04")), 50000)

    def test_adjacent_ranges_add_up(self):
        a = self.data("2026-10-27", "2026-10-30")
        b = self.data("2026-10-31", "2026-11-04")
        both = self.data("2026-10-27", "2026-11-04")
        self.assertEqual(a["totals"]["tokens"] + b["totals"]["tokens"], both["totals"]["tokens"])
        self.assertEqual(both["totals"]["tokens"], 53456)
        self.assertEqual(self.chan_tokens(a) + self.chan_tokens(b), self.chan_tokens(both))
        # Daily totals add up to the range total, on Sydney days inside the range.
        self.assertEqual(sum(d["tokens"] for d in both["days"]), both["totals"]["tokens"])
        self.assertEqual([d["date"] for d in both["days"]],
                         ["2026-10-27", "2026-10-28", "2026-10-29", "2026-11-02", "2026-11-03", "2026-11-04"])

    def test_sessions_are_distinct_sessions_active_in_range(self):
        d = self.data("2026-10-28", "2026-11-03")
        self.assertEqual(d["totals"]["sessions"], 4)  # chan, main, cron-a, cron-b
        by_id = {b["id"]: b for b in d["buckets"]}
        self.assertEqual(by_id[CHAN]["sessions"], 1)  # two turns in range, still one session
        self.assertEqual(self.data("2026-10-27", "2026-10-27")["totals"]["sessions"], 1)
        empty = self.data("2026-10-30", "2026-11-01")
        self.assertEqual(empty["totals"]["sessions"], 0)
        # Only the always-listed laptop bucket, at zero.
        self.assertEqual([(b["id"], b["sessions"], b["models"]) for b in empty["buckets"]],
                         [(oa.OUTSIDE_ID, 0, {})])
        day28 = self.data("2026-10-28", "2026-10-28")["days"]
        self.assertEqual([(x["date"], x["sessions"], x["turns"], x["tokens"]) for x in day28],
                         [("2026-10-28", 2, 3, 40)])

    def test_ui_preset_and_api_dates_agree(self):
        import dashboard
        from datetime import datetime
        now = datetime(2026, 11, 3, 7, 50)
        with mock.patch.object(oa, "get_index", lambda *a, **k: self.idx):
            p = dashboard.get_by_chat_payload(self.db_path, "7d", live_scan=False, now=now)
            c = dashboard.get_by_chat_payload(self.db_path, None, "2026-10-28", "2026-11-03", live_scan=False)
        self.assertEqual(p["totals"], c["totals"])
        self.assertEqual(p["range"]["start_utc"], c["range"]["start_utc"])
        self.assertEqual(p["totals"]["tokens"], 430 + 10 + 7 + 9)


if __name__ == "__main__":
    unittest.main()

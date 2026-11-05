"""Other machines' pushed usage (device_ingest) and its merge into /api/by-chat."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard  # noqa: E402
import device_ingest  # noqa: E402
import openclaw_attrib as oa  # noqa: E402
from scanner import init_db  # noqa: E402

TOKEN = "test-token-123"


def row(hour, model="claude-opus-5-5", project="vault", session="s1", inp=1, out=2, cr=3, cc=4, turns=1):
    return {"hour": hour, "model": model, "project": project, "session": session,
            "input": inp, "output": out, "cache_read": cr, "cache_creation": cc, "turns": turns}


class DeviceIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "usage.db")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.close()
        self.idx = oa.load_index(os.path.join(self.tmp, "no-openclaw"))
        self.env = mock.patch.dict(os.environ, {"USAGE_INGEST_TOKEN": TOKEN})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def push(self, rows, since="2026-11-01T00", device="tams-mac"):
        return device_ingest.ingest(self.db_path, {"device": device, "label": "Tam's Mac", "since": since, "rows": rows})

    def test_auth(self):
        self.assertTrue(device_ingest.authorized("Bearer " + TOKEN))
        self.assertFalse(device_ingest.authorized("Bearer nope"))
        self.assertFalse(device_ingest.authorized(""))
        with mock.patch.dict(os.environ, {"USAGE_INGEST_TOKEN": ""}), \
                mock.patch.object(device_ingest, "ENV_FILE", Path(self.tmp) / "missing.env"):
            self.assertFalse(device_ingest.authorized("Bearer "))  # no token configured: always refuse

    def test_push_replaces_window(self):
        self.push([row("2026-11-03T02"), row("2026-11-04T02", session="s2")])
        self.push([row("2026-11-04T02", session="s2", inp=10)], since="2026-11-04T00")
        d = oa.get_by_chat_data(self.db_path, "2026-11-01T13:00:00.000Z", "2026-11-08T13:00:00.000Z", idx=self.idx,
                                tz_name="Australia/Sydney")
        b = [x for x in d["buckets"] if x["kind"] == "device"]
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["label"], "Tam's Mac — Claude Code · vault")
        self.assertEqual(b[0]["sessions"], 2)
        self.assertEqual(b[0]["models"]["claude-opus-5-5"]["input"], 11)  # 1 kept from before since, 10 replaced
        self.assertEqual(sum(x["tokens"] for x in d["days"]), d["totals"]["tokens"])
        self.assertEqual(sum(h["input"] + h["output"] + h["cache_read"] + h["cache_creation"] for h in d["hours"]),
                         d["totals"]["tokens"])
        self.assertTrue(all(h["device"] == "tams-mac" and h["interface"] == "outside" for h in d["hours"]))
        devs = {x["device"]: x for x in d["devices"]}
        self.assertEqual(devs["tams-mac"]["label"], "Tam's Mac")
        self.assertIn("this-laptop", devs)
        # Sydney local hour: 02 UTC on 3 Nov is 13:00 local
        self.assertIn("2026-11-03T13", {h["hour"] for h in d["hours"]})

    def test_validation_and_delete(self):
        with self.assertRaises(ValueError):
            self.push([row("2026-11-03T02")], device="bad device!")
        with self.assertRaises(ValueError):
            self.push([row("2026-11-03T02", inp=-1)])
        self.push([row("2026-11-03T02")])
        self.assertEqual(device_ingest.delete_device(self.db_path, "tams-mac")["deleted_rows"], 1)
        d = oa.get_by_chat_data(self.db_path, None, None, idx=self.idx)
        self.assertFalse([x for x in d["buckets"] if x["kind"] == "device"])

    def test_http_endpoint(self):
        with mock.patch.object(dashboard, "DB_PATH", Path(self.db_path)):
            srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = "http://127.0.0.1:%d/api/ingest" % srv.server_port
            body = json.dumps({"device": "tams-mac", "since": "2026-11-01T00", "rows": [row("2026-11-03T02")]}).encode()
            try:
                bad = urllib.request.Request(url, data=body, method="POST", headers={"Authorization": "Bearer wrong"})
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(bad)
                self.assertEqual(e.exception.code, 401)
                ok = urllib.request.Request(url, data=body, method="POST", headers={"Authorization": "Bearer " + TOKEN})
                self.assertEqual(json.loads(urllib.request.urlopen(ok).read())["rows"], 1)
                got = urllib.request.urlopen("http://127.0.0.1:%d/collector/claude_usage_collector.py" % srv.server_port).read()
                self.assertIn(b"USAGE_INGEST_TOKEN", got)
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen("http://127.0.0.1:%d/collector/../dashboard.py" % srv.server_port)
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()

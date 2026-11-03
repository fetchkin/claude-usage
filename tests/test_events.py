"""Tests for the Fetchkin event markers (events.json + ~/.claude/usage-events.json)."""

import json
import tempfile
import unittest
from pathlib import Path

import dashboard
from dashboard import load_events, HTML_TEMPLATE


class TestLoadEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data, bom=False):
        p = self.dir / name
        text = data if isinstance(data, str) else json.dumps(data)
        p.write_text(("﻿" if bom else "") + text, encoding="utf-8")
        return p

    def test_merges_and_overrides_by_date_and_label(self):
        repo = self.write("events.json", [
            {"date": "2026-11-03", "label": "/handover installed", "detail": "repo"},
            {"date": "2026-10-01", "label": "older", "detail": ""},
        ])
        local = self.write("usage-events.json", [
            {"date": "2026-11-03", "label": "/handover installed", "detail": "local"},
            {"date": "2026-11-05", "label": "extra"},
        ])
        evs = load_events([repo, local])
        self.assertEqual([e["date"] for e in evs], ["2026-10-01", "2026-11-03", "2026-11-05"])
        self.assertEqual(evs[1]["detail"], "local")
        self.assertEqual(evs[2]["detail"], "")

    def test_missing_bad_and_bom_files(self):
        bad = self.write("bad.json", "{not json")
        bom = self.write("bom.json", [{"date": "2026-11-03", "label": "x"}], bom=True)
        evs = load_events([self.dir / "missing.json", bad, bom])
        self.assertEqual(len(evs), 1)

    def test_skips_invalid_entries(self):
        p = self.write("e.json", [
            {"date": "3 Nov", "label": "bad date"},
            {"date": "2026-11-03", "label": ""},
            "string",
            {"date": "2026-11-03", "label": "ok"},
        ])
        self.assertEqual([e["label"] for e in load_events([p])], ["ok"])

    def test_repo_events_file_has_handover_seed(self):
        evs = load_events([Path(dashboard.__file__).resolve().parent / "events.json"])
        self.assertIn(("2026-11-03", "/handover installed"), [(e["date"], e["label"]) for e in evs])

    def test_template_has_inline_marker_plugin(self):
        self.assertIn("afterDatasetsDraw", HTML_TEMPLATE)
        self.assertIn("plugins: [eventMarkerPlugin]", HTML_TEMPLATE)
        self.assertIn('id="daily-events"', HTML_TEMPLATE)
        self.assertNotIn("chartjs-plugin-annotation", HTML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()

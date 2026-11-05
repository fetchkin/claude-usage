#!/usr/bin/env python3
"""
claude_usage_collector.py - push this machine's Claude Code usage totals to the
Fetchkin usage dashboard (claude-usage, POST /api/ingest) over the tailnet.

Zero dependencies (Python 3.8+ stdlib). Reads ~/.claude/projects/**/*.jsonl and
keeps only, per assistant turn: token counts, model, timestamp, a hash of the
session id and the basename of the working folder. Message content is never
read into the output, logged or sent. Totals are grouped by UTC hour, model,
project folder and session, for the last --days days, and each run re-sends
that whole window (the server replaces it, so runs are idempotent).

    python3 claude_usage_collector.py --device tams-macbook-air --label "Tam's Mac"
    python3 claude_usage_collector.py --dry-run          # print a summary, send nothing

Token: $USAGE_INGEST_TOKEN, else USAGE_INGEST_TOKEN= in --token-file
(default ~/.config/fetchkin/usage-collector.env). Run every 10 minutes by
launchd (macOS) or Task Scheduler (Windows); see install-mac.sh / install-windows.ps1.
"""

import argparse
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1"
DEFAULT_URL = "https://laptop-fevkm5fu.tailaecd9a.ts.net:8443/api/ingest"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "fetchkin" / "usage-collector.env"


def read_token(token_file):
    tok = os.environ.get("USAGE_INGEST_TOKEN", "").strip()
    if tok:
        return tok
    try:
        for line in Path(token_file).read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "USAGE_INGEST_TOKEN":
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def folder_name(cwd):
    parts = (cwd or "").replace("\\", "/").rstrip("/").split("/")
    return parts[-1] if parts and parts[-1] else "unknown"


def collect(projects_dir, since):
    """{(hour, model, project, session): [input, output, cache_read, cache_creation, turns]}"""
    since_iso = since.strftime("%Y-%m-%dT%H")
    cutoff = since.timestamp() - 3600
    turns = {}      # message id -> (hour, model, project, session, [counts]); last record wins
    no_id = []
    files = 0
    for f in Path(projects_dir).rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        files += 1
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"assistant"' not in line or '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    ts = rec.get("timestamp") or ""
                    if ts[:13] < since_iso:
                        continue
                    msg = rec.get("message") or {}
                    u = msg.get("usage") or {}
                    counts = [u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                              u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0]
                    if not sum(counts):
                        continue
                    sid = rec.get("sessionId") or ""
                    key = (ts[:13], msg.get("model") or "unknown", folder_name(rec.get("cwd")),
                           hashlib.sha256(sid.encode()).hexdigest()[:16] if sid else "")
                    if msg.get("id"):
                        turns[msg["id"]] = (key, counts)
                    else:
                        no_id.append((key, counts))
        except OSError:
            continue
    agg = {}
    for key, counts in list(turns.values()) + no_id:
        a = agg.setdefault(key, [0, 0, 0, 0, 0])
        for i in range(4):
            a[i] += int(counts[i])
        a[4] += 1
    return agg, files


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--device", default=socket.gethostname().split(".")[0].lower(), help="stable id, A-Z a-z 0-9 . _ -")
    ap.add_argument("--label", default=None, help='display name, e.g. "Tam\'s Mac"')
    ap.add_argument("--url", default=os.environ.get("USAGE_INGEST_URL", DEFAULT_URL))
    ap.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    ap.add_argument("--projects-dir", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--dry-run", action="store_true", help="print a summary; send nothing")
    a = ap.parse_args()

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    since = now - timedelta(days=a.days)
    t0 = time.time()
    agg, files = collect(a.projects_dir, since)
    rows = [{"hour": h, "model": m, "project": p, "session": s,
             "input": c[0], "output": c[1], "cache_read": c[2], "cache_creation": c[3], "turns": c[4]}
            for (h, m, p, s), c in sorted(agg.items())]
    tokens = sum(r["input"] + r["output"] + r["cache_read"] + r["cache_creation"] for r in rows)
    summary = "%s: %d files, %d rows, %d turns, %s tokens since %s UTC (%.1fs)" % (
        a.device, files, len(rows), sum(r["turns"] for r in rows), format(tokens, ","), since.strftime("%Y-%m-%d %H:00"), time.time() - t0)
    if a.dry_run:
        print("[dry run] " + summary)
        return 0
    token = read_token(a.token_file)
    if not token:
        print("No ingest token: set USAGE_INGEST_TOKEN or write it to %s" % a.token_file, file=sys.stderr)
        return 2
    body = json.dumps({"device": a.device, "label": a.label or a.device, "since": since.strftime("%Y-%m-%dT%H"),
                       "collector": VERSION, "rows": rows}).encode("utf-8")
    req = urllib.request.Request(a.url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("%s  -> HTTP %d %s" % (summary, e.code, e.read().decode("utf-8", "replace")[:200]), file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print("%s  -> could not reach %s: %s" % (summary, a.url, e), file=sys.stderr)
        return 1
    print("%s %s  -> stored %s rows" % (datetime.now().strftime("%Y-%m-%d %H:%M"), summary, res.get("rows")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

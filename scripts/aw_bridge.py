#!/usr/bin/env python3
"""Send app usage from ActivityWatch (running on this laptop) to the Cardinal hub. Standard library only.

What's sent: for each hour, the app names you used and for how many seconds, while you were at the computer
(ActivityWatch's AFK watcher decides that). Never window titles, URLs, keystrokes or screenshots.

Config: ~/.cardinal-aw.json  {"hub": "https://cardinal.<tailnet>.ts.net", "token": "...", "device": "Mac"}
Run every 15 minutes by launchd (Mac) or Task Scheduler (Windows); see the install scripts next to this file.
"""

import json
import os
import socket
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

AW = os.environ.get("AW_URL", "http://localhost:5600")
CONFIG = Path.home() / ".cardinal-aw.json"
STATE = Path.home() / ".cardinal-aw-state.json"
QUERY = [
    'window = flood(query_bucket(find_bucket("aw-watcher-window_")));',
    'afk = flood(query_bucket(find_bucket("aw-watcher-afk_")));',
    'not_afk = filter_keyvals(afk, "status", ["not-afk"]);',
    "window = filter_period_intersect(window, not_afk);",
    'merged = merge_events_by_keys(window, ["app"]);',
    "RETURN = sort_by_duration(merged);",
]


def http(method, url, body=None, headers=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def main():
    cfg = json.loads(CONFIG.read_text())
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    last = datetime.fromisoformat(state["last_full_hour"]) if state.get("last_full_hour") else now - timedelta(hours=48)
    start = max(min(last, now - timedelta(hours=2)), now - timedelta(hours=72))  # resend the last 2 hours: they fill in
    hours = []
    t = start
    while t <= now:
        hours.append(t)
        t += timedelta(hours=1)
    periods = [f"{h.isoformat()}/{(h + timedelta(hours=1)).isoformat()}" for h in hours]
    results = http("POST", f"{AW}/api/0/query/", {"timeperiods": periods, "query": QUERY})
    payload = {"device": cfg.get("device") or socket.gethostname(), "hours": []}
    for h, events in zip(hours, results):
        apps = [{"app": e["data"].get("app", "?"), "seconds": round(e["duration"])} for e in events if e.get("duration", 0) >= 30]
        payload["hours"].append({"start": h.isoformat(), "apps": apps})
    out = http("POST", cfg["hub"].rstrip("/") + "/api/apps/ingest", payload, {"X-Cardinal-Token": cfg["token"]})
    STATE.write_text(json.dumps({"last_full_hour": (now - timedelta(hours=1)).isoformat()}))
    print(f"Sent {len(payload['hours'])} hours: {out}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # launchd/Task Scheduler log this; the next run retries
        print(f"aw_bridge: {e}", file=sys.stderr)
        sys.exit(1)

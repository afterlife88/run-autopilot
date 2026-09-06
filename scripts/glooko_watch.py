#!/usr/bin/env python3
"""
Detect new Glooko pump uploads and refresh workout pump data.

The t:slim pump is uploaded to Glooko via USB roughly weekly. This script
runs from a systemd timer, checks whether newer pump/CGM data has appeared
on Glooko since the last check, and if so runs batch_pump_update.sh
(backfills pump data into all workouts missing it + rebuilds the dashboard).

First run only records a baseline. State advances only after a successful
batch update, so failures are retried on the next timer tick.
"""
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, LT_DIR)

from glooko_client import GlookoClient, _parse_ts

STATE_FILE = os.path.join(LT_DIR, "glooko_watch_state.json")
BATCH_SCRIPT = os.path.join(SCRIPT_DIR, "batch_pump_update.sh")
LOOKBACK_DAYS = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("glooko_watch")


# Pump-derived series only: CGM auto-syncs from the phone daily and must NOT
# trigger the (heavy) batch update — only a real pump USB upload should.
PUMP_SERIES = ["deliveredBolus", "scheduledBasal", "carbAll"]


def latest_data_ts(client):
    """Newest pump-data timestamp on Glooko in the recent window."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    data = client.get_graph_data(start, end, series=PUMP_SERIES)

    latest = None
    for points in (data.get("series") or {}).values():
        if not isinstance(points, list):
            continue
        for p in points:
            if not isinstance(p, dict):
                continue
            ts = _parse_ts(p.get("timestamp"))
            if ts and (latest is None or ts > latest):
                latest = ts
    return latest


def main():
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)

    client = GlookoClient(os.path.join(LT_DIR, "config.ini"))
    latest = latest_data_ts(client)
    state["last_checked"] = datetime.now().isoformat(timespec="seconds")

    if latest is None:
        log.info("no pump data in the last %d days on Glooko", LOOKBACK_DAYS)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=1)
        return 0

    prev = _parse_ts(state.get("last_data_ts"))
    log.info("latest Glooko pump data: %s (previous: %s)", latest, prev or "none")

    # prev=None means pump data (re)appeared after an empty window — that IS
    # a fresh upload, so fall through and trigger the update.
    if prev is not None and latest <= prev + timedelta(minutes=30):
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=1)
        log.info("no new pump upload")
        return 0

    log.info("new pump upload detected → running batch_pump_update.sh")
    rc = subprocess.call(["bash", BATCH_SCRIPT], cwd=LT_DIR)
    if rc != 0:
        log.error("batch_pump_update.sh failed (rc=%d); will retry next run", rc)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=1)
        return 1

    state["last_data_ts"] = latest.isoformat()
    state["last_update_run"] = datetime.now().isoformat(timespec="seconds")
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)
    log.info("pump data refreshed for all workouts")
    return 0


if __name__ == "__main__":
    sys.exit(main())

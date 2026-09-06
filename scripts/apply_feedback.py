#!/usr/bin/env python3
"""
Apply user feedback from Telegram to a synced Strava activity.

Usage:
  apply_feedback.py <garmin_activity_id> type "Threshold"
  apply_feedback.py <garmin_activity_id> gear <strava_gear_id>
  apply_feedback.py <garmin_activity_id> lactate '{"6": 2.8, "10": 3.8}'

Looks up the Strava activity via auto_sync_state.json, applies the change,
and keeps the state's record of "our" title/description in sync so the
auto-sync override protection keeps working.
"""
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, LT_DIR)

import strava_client as sc  # noqa: E402

STATE_FILE = os.path.join(LT_DIR, "auto_sync_state.json")

TYPE_EMOJI = {"Threshold": "⚡", "Interval": "⚡", "Tempo": "🔥", "Moderate": "🏃",
              "Easy": "🏃", "Recovery": "🚶", "Long Run": "🏃‍♂️", "Race": "🏁"}
# Strava workout_type for runs: 0 default, 1 race, 2 long run, 3 workout
STRAVA_WORKOUT_TYPE = {"Race": 1, "Long Run": 2, "Threshold": 3, "Interval": 3, "Tempo": 3}


def load_state():
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def main():
    aid, action, value = sys.argv[1], sys.argv[2], sys.argv[3]
    state = load_state()
    entry = state["activities"].get(aid)
    if not entry or not entry.get("strava_id"):
        print(f"ERROR: no synced activity {aid}")
        return 1
    sid = entry["strava_id"]

    token = sc.refresh_token_if_needed(sc.load_config())
    detail = sc.strava_request(token, f"activities/{sid}")
    update = {}

    if action == "gear":
        update["gear_id"] = value
        print(f"gear → {value}")

    elif action == "type":
        new_type = value
        old_title = detail.get("name", "")
        entry["type"] = new_type
        # Rebuild the title: keep a rep-style name (contains ×), otherwise
        # regenerate "<Type> <dist>k"; swap the leading emoji either way.
        m = re.match(r"^\S+\s+(.*?)( — .*)?$", old_title)
        core = m.group(1) if m else old_title
        tail = m.group(2) or "" if m else ""
        if "×" not in core:
            dist = (detail.get("distance") or 0) / 1000
            core = f"{new_type} {dist:.0f}k"
        new_title = f"{TYPE_EMOJI.get(new_type, '🏃')} {core}{tail}"
        update["name"] = new_title
        entry.setdefault("pushed", {})["title"] = new_title
        if new_type in STRAVA_WORKOUT_TYPE:
            update["workout_type"] = STRAVA_WORKOUT_TYPE[new_type]
        print(f"type → {new_type}, title → {new_title}")

    elif action == "lactate":
        lactate = json.loads(value)
        desc = (detail.get("description") or "").strip()
        # drop a previous lactate block if present
        desc = re.sub(r"\n?\n?🧪 Lactate:.*(?:\n .*)*", "", desc).strip()
        pairs = " · ".join(f"#{k}: {v}" for k, v in lactate.items())
        desc = f"{desc}\n\n🧪 Lactate: {pairs} mmol/L".strip()
        update["description"] = desc
        entry.setdefault("pushed", {})["description"] = desc
        entry["lactate"] = lactate
        print(f"lactate → {pairs}")

    else:
        print(f"ERROR: unknown action {action}")
        return 1

    sc.strava_request(token, f"activities/{sid}", method="PUT", data=update)
    save_state(state)
    print("OK: Strava updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())

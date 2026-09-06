#!/usr/bin/env python3
"""
Gather a training-load report as JSON for the coaching skill.

Combines:
- Stryd: fitness/fatigue/RSB (today + trend), CP history, last 28 days of
  activities with RSS/power/distance
- Local classification from auto_sync_state.json (workout types)
- Garmin: today's readiness context (sleep, HRV, body battery, stress)

Prints one JSON object to stdout. Network failures degrade gracefully —
each section is None rather than the whole report failing.
"""
import calendar
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, LT_DIR)

STATE_FILE = os.path.join(LT_DIR, "auto_sync_state.json")


def stryd_section():
    from stryd_client import StrydClient
    c = StrydClient()
    out = {}

    ff = c._get(f"/users/{c.user_id}/fatiguefitness/all")
    bl, ti = ff["balance_list"], ff["today_index"]

    def rsb_at(i):
        if 0 <= i < len(bl) and bl[i].get("fitness") is not None:
            return round(bl[i]["fitness"] - bl[i]["fatigue"], 1)
        return None

    out["today"] = {
        "fitness_ctl": round(bl[ti]["fitness"], 1),
        "fatigue_atl": round(bl[ti]["fatigue"], 1),
        "rsb": rsb_at(ti),
    }
    out["rsb_trend"] = {f"-{d}d": rsb_at(ti - d) for d in (1, 3, 7, 14, 28)}
    out["fitness_28d_ago"] = round(bl[ti - 28]["fitness"], 1) if ti >= 28 else None

    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
    hist = c._get(f"/users/{c.user_id}/cp/history",
                  {"startDate": start, "endDate": end})
    if isinstance(hist, list) and hist:
        by_date = sorted(hist, key=lambda h: h.get("created", 0))
        out["cp_now"] = round(by_date[-1].get("critical_power", 0), 1)
        out["cp_90d_ago"] = round(by_date[0].get("critical_power", 0), 1)

    # Long-run baseline from ~10 months of history: the athlete's own
    # envelope is the reference for recommendations, NOT the recent window
    # (a deload/travel week would otherwise read as the norm).
    now = int(datetime.now(timezone.utc).timestamp())
    hist_acts = c.get_activities(now - 300 * 86400, now)
    longs = [a for a in hist_acts
             if (a.get("distance") or 0) >= 19500 and a.get("ftp")
             and a.get("average_power")]
    if longs:
        pcts = sorted(a["average_power"] / a["ftp"] * 100 for a in longs)
        rsss = sorted(a.get("stress") or 0 for a in longs)
        out["long_run_baseline_300d"] = {
            "count": len(longs),
            "median_pct_cp": round(pcts[len(pcts) // 2]),
            "p90_pct_cp": round(pcts[int(len(pcts) * 0.9)]),
            "median_rss": round(rsss[len(rsss) // 2]),
            "max_rss": round(rsss[-1]),
            "max_km": round(max(a["distance"] for a in longs) / 1000, 1),
        }

    acts = [a for a in hist_acts if a["timestamp"] >= now - 28 * 86400]
    out["activities_28d"] = [{
        "date": datetime.fromtimestamp(a["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d"),
        "name": a.get("name"),
        "km": round((a.get("distance") or 0) / 1000, 1),
        "min": round((a.get("moving_time") or 0) / 60),
        "rss": round(a.get("stress") or 0),
        "avg_power": round(a.get("average_power") or 0),
        "pct_cp": round((a.get("average_power") or 0) / a["ftp"] * 100)
                  if a.get("ftp") else None,
    } for a in acts]
    return out


def weekly_section(activities):
    weeks = defaultdict(lambda: {"km": 0.0, "min": 0, "rss": 0, "runs": 0})
    for a in activities or []:
        iso = datetime.strptime(a["date"], "%Y-%m-%d").isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        w = weeks[key]
        w["km"] = round(w["km"] + a["km"], 1)
        w["min"] += a["min"]
        w["rss"] += a["rss"]
        w["runs"] += 1
    return dict(sorted(weeks.items()))


def types_section():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        state = json.load(f)
    cutoff = (datetime.now() - timedelta(days=28)).strftime("%Y-%m-%d")
    counts = defaultdict(int)
    for v in state.get("activities", {}).values():
        if (v.get("date") or "") >= cutoff and v.get("type"):
            counts[v["type"]] += 1
    return dict(counts)


def garmin_section():
    from garmin_client import GarminClient
    c = GarminClient(config_path=os.path.join(LT_DIR, "config.ini"))
    c.login()
    today = datetime.now().strftime("%Y-%m-%d")
    ctx = c.get_readiness_context(today)
    if not ctx:
        # after midnight the current day may be empty — try yesterday
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        ctx = c.get_readiness_context(yesterday)
    return ctx


def main():
    report = {"generated_at": datetime.now().isoformat(timespec="seconds")}

    try:
        report["stryd"] = stryd_section()
    except Exception as e:
        report["stryd"] = None
        report["stryd_error"] = str(e)

    acts = (report.get("stryd") or {}).get("activities_28d")
    report["weekly"] = weekly_section(acts)
    report["types_28d"] = types_section()

    try:
        report["garmin_readiness"] = garmin_section()
    except Exception as e:
        report["garmin_readiness"] = None
        report["garmin_error"] = str(e)

    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()

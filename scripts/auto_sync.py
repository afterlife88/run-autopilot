#!/usr/bin/env python3
"""
Unattended Garmin → Strava sync.

Polls Garmin Connect for new running activities, downloads FIT files,
auto-classifies the workout, and pushes a generated title + weather
description to the matching Strava activity. Designed to run from a
systemd timer — no user interaction, non-zero exit on errors.

The manual "process workout" flow (lactate, coach notes, glucose) still
works on top: it produces a richer description which overwrites ours,
because we only ever touch titles/descriptions we set ourselves or
Strava defaults ("Morning Run" etc.).

Usage:
  auto_sync.py                 # incremental: activities since last state
  auto_sync.py --since DATE    # backfill mode (does not touch pending list)
  auto_sync.py --dry-run       # show what would be pushed
"""
import argparse
import calendar
import fcntl
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, LT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from garmin_client import GarminClient
from fit_processor import preview_fit
from extract_tracks import extract_track
from add_weather import WMO_CODES, WMO_EMOJI, wind_direction_label
import strava_client as sc

FIT_DIR = os.path.join(LT_DIR, "trainings-files", "fit")
STATE_FILE = os.path.join(LT_DIR, "auto_sync_state.json")
PENDING_FILE = os.path.join(LT_DIR, "pending_workouts.json")
TRACKS_DIR = "/home/pi/fitness-dashboard/public/tracks"
LOCK_FILE = "/tmp/strava_autosync.lock"

# Fallback when a FIT file has no GPS at all (weather is skipped for indoor)
DEFAULT_LAT, DEFAULT_LON = 55.6761, 12.5683  # Copenhagen

# Garmin's auto-generated names look like "<Place> Running"
GARMIN_DEFAULT_RE = re.compile(r"^(.{0,40} )?(Running|Løb|Treadmill Running)$")
# Strava's auto-generated names, possibly with a trailing emoji
STRAVA_DEFAULT_RE = re.compile(r"^(Morning|Lunch|Afternoon|Evening|Night) (Run|Workout)\s*\W{0,4}$")
# Auto-generated descriptions from bots (safe to replace with ours)
BOT_DESC_RE = re.compile(r"by KlimatApp\s*$")

MAX_MATCH_ATTEMPTS = 3
API_PACING_SEC = 3

log = logging.getLogger("auto_sync")


# ─── State ────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"activities": {}}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ─── Weather (single activity, correct location) ─────────────────

def fetch_weather(date_str, hour_utc, lat, lon):
    """Fetch hourly weather for one date/hour at the given coords.

    Uses the forecast API (with past_days) for recent dates because the
    archive API lags several days behind.
    """
    from datetime import timezone as _tz
    age_days = (datetime.now(_tz.utc).date() - datetime.strptime(date_str, "%Y-%m-%d").date()).days
    if age_days <= 5:
        base = "https://api.open-meteo.com/v1/forecast"
        extra = f"&past_days={min(max(age_days + 1, 1), 7)}&forecast_days=1"
    else:
        base = "https://archive-api.open-meteo.com/v1/archive"
        extra = f"&start_date={date_str}&end_date={date_str}"

    url = (
        f"{base}?latitude={lat:.4f}&longitude={lon:.4f}{extra}"
        f"&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,"
        f"wind_direction_10m,wind_gusts_10m,precipitation,cloud_cover,weather_code"
        f"&timezone=UTC"
    )
    req = Request(url, headers={"User-Agent": "LT-trainings-autosync/1.0"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            break
        except (URLError, TimeoutError) as e:
            if attempt == 2:
                raise
            log.warning("Open-Meteo attempt %d failed: %s", attempt + 1, e)
            time.sleep(2)

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    target = f"{date_str}T{hour_utc:02d}:00"
    if target not in times:
        return None
    i = times.index(target)

    def val(key):
        arr = hourly.get(key, [])
        return arr[i] if i < len(arr) else None

    code = val("weather_code")
    if val("temperature_2m") is None:
        return None
    return {
        "temperature_c": val("temperature_2m"),
        "humidity_pct": val("relative_humidity_2m"),
        "wind_speed_kmh": val("wind_speed_10m"),
        "wind_gust_kmh": val("wind_gusts_10m"),
        "wind_direction_deg": val("wind_direction_10m"),
        "wind_direction": wind_direction_label(val("wind_direction_10m")),
        "precipitation_mm": val("precipitation"),
        "cloud_cover_pct": val("cloud_cover"),
        "weather_code": code,
        "description": WMO_CODES.get(code, "Unknown"),
        "emoji": WMO_EMOJI.get(code, ""),
        "source_hour_utc": hour_utc,
        "indoor": False,
    }


# ─── Glucose (Dexcom Share — only holds ~24h of history) ────────

def fetch_glucose(start_utc, elapsed_sec):
    """CGM readings for the workout window, or None. Silent on failure."""
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc).replace(tzinfo=None)
    if (now - start_utc).total_seconds() > 20 * 3600:
        return None  # Share API won't have data this old
    try:
        from dexcom_client import DexcomClient
        dex = DexcomClient(os.path.join(LT_DIR, "config.ini"))
        offset = dex.derive_utc_offset()
        if offset is None:
            return None
        local_start = start_utc + offset
        readings = dex.get_glucose_readings(
            local_start - timedelta(minutes=5),
            local_start + timedelta(seconds=elapsed_sec, minutes=5),
        )
        return readings or None
    except Exception as e:
        log.warning("  glucose fetch failed: %s", e)
        return None


def attach_glucose(intervals, readings):
    """Distribute CGM readings evenly across intervals for the description trend."""
    n = len(intervals)
    m = len(readings)
    if not n or not m:
        return
    for i, iv in enumerate(intervals):
        lo = int(i * m / n)
        hi = max(lo + 1, int((i + 1) * m / n))
        vals = [r["value"] for r in readings[lo:hi]]
        if vals:
            iv["glucose"] = {"avg": round(sum(vals) / len(vals), 1)}


# ─── Classification & naming ─────────────────────────────────────

def _pace_to_sec(pace_str):
    try:
        m, s = pace_str.split(":")
        return int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None


def _time_to_sec(time_str):
    parts = time_str.split(":") if time_str else []
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except ValueError:
        pass
    return None


def _rep_label(med_dist, med_sec):
    """Human label for a rep: prefer round distances, else round minutes."""
    for target, label in ((1000, "1k"), (2000, "2k"), (1600, "1600m"),
                          (1200, "1200m"), (800, "800m"), (600, "600m"),
                          (400, "400m"), (200, "200m"), (3000, "3k"), (5000, "5k")):
        if abs(med_dist - target) <= max(40, target * 0.04):
            return label
    minutes = med_sec / 60
    if abs(minutes - round(minutes)) * 60 <= 8 and round(minutes) >= 1:
        return f"{round(minutes)}'"
    return f"{int(med_sec // 60)}:{int(med_sec % 60):02d}"


def _detect_work_reps(intervals, cp=None):
    """Detect workout reps. Returns (n, label, wtype) or None.

    With CP available, work reps are laps at ≥90% CP — this correctly
    separates 1 km reps from 1 km auto-laps of an easy run (which the old
    duration-uniformity heuristic could not). Without CP, falls back to
    uniform-duration structure with a km-auto-lap guard.
    """
    durations = [_time_to_sec(iv.get("time")) or 0 for iv in intervals]
    distances = [iv.get("distance_m") or 0 for iv in intervals]

    if cp:
        work = [(d, dist, iv.get("power") or 0)
                for d, dist, iv in zip(durations, distances, intervals)
                if d >= 45 and (iv.get("power") or 0) >= 0.90 * cp]
        if len(work) >= 3:
            durs = sorted(w[0] for w in work)
            med = durs[len(durs) // 2]
            # Rep count from total work time: robust to a rep split across
            # two laps (pause mid-rep) or a stray surge lap
            n = max(1, round(sum(w[0] for w in work) / med))
            med_dist = sorted(w[1] for w in work)[len(work) // 2]
            # LT-style work (long reps) vs VO2-style (short reps)
            wtype = "Threshold" if med >= 180 else "Interval"
            return n, _rep_label(med_dist, med), wtype
        return None

    # ── no CP: legacy structural heuristic ──
    if len(intervals) < 3 or any(d == 0 for d in durations):
        return None
    km_laps = sum(1 for d in distances[:-1] if 950 <= d <= 1060)
    if distances and km_laps >= max(1, (len(distances) - 1) * 6 // 10):
        return None
    med = sorted(durations)[len(durations) // 2]
    if med < 45:
        return None
    uniform = [d for d in durations if abs(d - med) <= med * 0.2]
    if len(uniform) < max(3, len(durations) * 3 // 4):
        return None
    med_dist = sorted(distances)[len(distances) // 2]
    wtype = "Threshold" if med >= 240 else "Interval"
    return len(uniform), _rep_label(med_dist, med), wtype


def _avg_power(intervals):
    """Duration-weighted average lap power, or None."""
    pairs = [((iv.get("power") or 0), _time_to_sec(iv.get("time")) or 0)
             for iv in intervals]
    pairs = [(p, t) for p, t in pairs if p > 0 and t > 0]
    if not pairs:
        return None
    return sum(p * t for p, t in pairs) / sum(t for _, t in pairs)


def classify(preview, garmin_name="", cp=None):
    """Return (workout_type, workout_name) using Stryd power zones vs CP."""
    dist = preview.get("total_distance_km") or 0
    duration = preview.get("total_duration_min") or 0
    avg_hr = preview.get("avg_hr") or 0
    intervals = preview.get("intervals", [])

    custom_name = ""
    if garmin_name and not GARMIN_DEFAULT_RE.match(garmin_name.strip()):
        custom_name = garmin_name.strip()

    reps = _detect_work_reps(intervals, cp)
    if reps:
        n, label, wtype = reps
        return wtype, custom_name or f"{n}×{label}"

    avg_pwr = _avg_power(intervals)
    pct = (avg_pwr / cp) if (cp and avg_pwr) else None

    if dist >= 25 or duration >= 130:
        wtype = "Long Run"
    elif pct is not None:
        if pct < 0.68:
            wtype = "Recovery"
        elif pct < 0.82:
            wtype = "Easy"
        elif pct < 0.90:
            wtype = "Moderate"
        elif duration >= 25:
            wtype = "Tempo"
        else:
            wtype = "Easy"
    elif avg_hr and avg_hr < 132 and dist <= 12:
        wtype = "Recovery"
    else:
        wtype = "Easy"

    if custom_name:
        return wtype, custom_name

    dist_label = f"{dist:.0f}k" if dist >= 3 else f"{dist:.1f}k"
    return wtype, f"{wtype} {dist_label}"


# ─── Strava push with override protection ────────────────────────

def push_to_strava(token, activity, title, description, prev, dry_run=False):
    """Update title/description, but never clobber user edits.

    prev: previously pushed {"title":…, "description":…} for this activity, or {}.
    Returns {"title":…, "description":…} — what we believe is now ours on Strava
    (None for a field the user owns).
    """
    current_name = activity.get("name", "")
    update = {}
    ours = {"title": None, "description": None}

    if current_name == title:
        ours["title"] = title  # already correct
    elif STRAVA_DEFAULT_RE.match(current_name) or current_name == prev.get("title"):
        update["name"] = title
        ours["title"] = title
    else:
        log.info("  title kept (user-named): %r", current_name)

    if description:
        detail = sc.strava_request(token, f"activities/{activity['id']}")
        current_desc = (detail.get("description") or "").strip()
        if current_desc == description.strip():
            ours["description"] = description
        elif (not current_desc
              or current_desc == (prev.get("description") or "").strip()
              or BOT_DESC_RE.search(current_desc)):
            update["description"] = description
            ours["description"] = description
        else:
            log.info("  description kept (not ours)")

    if update:
        if dry_run:
            log.info("  [dry-run] would update: %s", ", ".join(update))
        else:
            sc.strava_request(token, f"activities/{activity['id']}", method="PUT", data=update)
            log.info("  updated: %s", ", ".join(update))
    else:
        log.info("  nothing to update")
    return ours


# ─── Pending list (feeds the manual enrichment flow) ─────────────

def append_pending(preview, act, fit_path):
    try:
        pending = []
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE) as f:
                pending = json.load(f)
        if any(w["activity_id"] == act["activity_id"] for w in pending):
            return
        pending.append({
            "activity_id": act["activity_id"],
            "date": preview.get("date"),
            "start_time": preview.get("start_time"),
            "activity_name": act.get("activity_name", ""),
            "distance_km": preview.get("total_distance_km"),
            "duration_min": preview.get("total_duration_min"),
            "avg_hr": preview.get("avg_hr"),
            "sport": preview.get("sport", "running"),
            "sub_sport": preview.get("sub_sport"),
            "intervals_count": len(preview.get("intervals", [])),
            "intervals_preview": preview.get("intervals", []),
            "fit_path": os.path.relpath(fit_path, LT_DIR),
            "status": "pending",
            "auto_synced": True,
            "detected_at": datetime.now().isoformat(timespec="seconds"),
        })
        with open(PENDING_FILE, "w") as f:
            json.dump(pending, f, indent=2, default=str)
    except Exception as e:
        log.warning("  could not append to pending list: %s", e)


# ─── Per-activity pipeline ───────────────────────────────────────

def process_activity(act, token, state, args):
    aid = act["activity_id"]
    entry = state["activities"].get(aid, {})

    fit_path = os.path.join(FIT_DIR, f"{aid}.fit")
    if not os.path.exists(fit_path):
        client = act["_client"]
        log.info("  downloading FIT…")
        client.download_activity_fit(aid, fit_path)

    preview = preview_fit(fit_path)

    # Stryd metrics first: CP drives the power-zone classification
    stryd_metrics = None
    start_gmt_dt = act.get("start_time_gmt")
    if start_gmt_dt:
        try:
            from stryd_client import StrydClient
            epoch0 = calendar.timegm(start_gmt_dt.timetuple())
            stryd_metrics = StrydClient(os.path.join(LT_DIR, "config.ini")).metrics_for(epoch0)
            if stryd_metrics:
                log.info("  stryd: RSS %s, CP %sW",
                         round(stryd_metrics.get("rss") or 0),
                         round(stryd_metrics.get("cp") or 0))
        except Exception as e:
            log.warning("  stryd fetch failed: %s", e)

    cp = None
    if stryd_metrics:
        cp = stryd_metrics.get("ftp") or stryd_metrics.get("cp")

    wtype, name = classify(preview, act.get("activity_name", ""), cp=cp)
    dist_km = preview.get("total_distance_km") or act["distance_km"]

    # GPS track → coords for weather + headwind
    track = None
    try:
        track = extract_track(fit_path)
    except Exception as e:
        log.warning("  track extraction failed: %s", e)
    indoor = preview.get("is_treadmill") or not track or not track.get("points")

    weather, headwind = None, None
    if not indoor:
        lat = track["start"]["lat"]
        lon = track["start"]["lon"]
        start_gmt = act.get("start_time_gmt")
        hour_utc = start_gmt.hour if start_gmt else 10
        try:
            weather = fetch_weather(preview["date"], hour_utc, lat, lon)
        except Exception as e:
            log.warning("  weather fetch failed: %s", e)
        if weather and weather.get("wind_direction_deg") is not None:
            headwind = sc.calc_headwind(track["points"], weather["wind_direction_deg"])

        # Save track for the dashboard map if not already there
        track_path = os.path.join(TRACKS_DIR, f"{preview['date']}.json")
        if not os.path.exists(track_path):
            try:
                os.makedirs(TRACKS_DIR, exist_ok=True)
                with open(track_path, "w") as f:
                    json.dump(track, f, separators=(",", ":"))
            except OSError as e:
                log.warning("  could not save track: %s", e)

    intervals = preview.get("intervals", [])
    if not indoor and preview.get("date") and preview.get("start_time"):
        start_utc = datetime.strptime(
            f"{preview['date']} {preview['start_time']}", "%Y-%m-%d %H:%M:%S")
        readings = fetch_glucose(start_utc, (preview.get("total_duration_min") or 0) * 60)
        if readings:
            attach_glucose(intervals, readings)
            log.info("  glucose: %d CGM readings attached", len(readings))

    workout = {
        "date": preview.get("date"),
        "type": wtype,
        "workout_name": name,
        "summary_data": {"distance": f"{dist_km:.1f} km"},
        "weather": weather or {"indoor": True},
        "intervals": intervals,
        "stryd": stryd_metrics or {},
    }
    title = sc.build_title(workout)
    description = sc.build_description(workout, headwind)  # None when indoor

    log.info("  → %s [%s]%s", title, wtype, " (indoor)" if indoor else "")

    # Match on Strava by exact start time + distance
    start_gmt = act.get("start_time_gmt")
    if not start_gmt:
        raise RuntimeError("no startTimeGMT from Garmin")
    epoch = calendar.timegm(start_gmt.timetuple())
    activity = sc.find_activity_near(token, epoch, distance_m=dist_km * 1000)
    if not activity:
        attempts = entry.get("match_attempts", 0) + 1
        status = "no_match_gave_up" if attempts >= MAX_MATCH_ATTEMPTS else "no_match"
        log.warning("  no Strava activity near %s (attempt %d)", start_gmt, attempts)
        state["activities"][aid] = {**entry, "status": status,
                                    "match_attempts": attempts,
                                    "date": preview.get("date"),
                                    "updated_at": datetime.now().isoformat(timespec="seconds")}
        return False

    ours = push_to_strava(token, activity, title, description,
                          prev=entry.get("pushed", {}), dry_run=args.dry_run)

    if not args.dry_run:
        state["activities"][aid] = {
            "status": "synced",
            "date": preview.get("date"),
            "strava_id": activity["id"],
            "type": wtype,
            "pushed": ours,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    if not args.since and not args.dry_run:
        append_pending(preview, act, fit_path)
    return True


# ─── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Unattended Garmin → Strava sync")
    parser.add_argument("--since", help="Backfill activities since date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N activities")
    parser.add_argument("--force", action="store_true", help="Re-push even if already synced")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another auto_sync run is in progress, exiting")
        return 0

    state = load_state()

    if args.since:
        since_dt = datetime.strptime(args.since, "%Y-%m-%d")
    else:
        dates = [v.get("date") for v in state["activities"].values() if v.get("date")]
        if dates:
            since_dt = datetime.strptime(max(dates), "%Y-%m-%d") - timedelta(days=2)
        else:
            since_dt = datetime.now() - timedelta(days=7)

    log.info("polling Garmin for running activities since %s", since_dt.date())
    client = GarminClient(config_path=os.path.join(LT_DIR, "config.ini"))
    client.login()
    activities = client.get_running_activities(since_timestamp=since_dt)
    log.info("found %d activities", len(activities))

    todo = []
    for act in sorted(activities, key=lambda a: a["start_time"] or datetime.min):
        entry = state["activities"].get(act["activity_id"], {})
        if not args.force and entry.get("status") in ("synced", "no_match_gave_up"):
            continue
        act["_client"] = client
        todo.append(act)
    if args.limit:
        todo = todo[:args.limit]

    if not todo:
        log.info("nothing to do")
        return 0

    token = sc.refresh_token_if_needed(sc.load_config())

    errors = 0
    for act in todo:
        log.info("%s %s %.1f km (id %s)", act["start_time"], act["activity_name"],
                 act["distance_km"], act["activity_id"])
        try:
            process_activity(act, token, state, args)
        except Exception as e:
            errors += 1
            log.error("  FAILED: %s", e)
        if not args.dry_run:
            save_state(state)
        time.sleep(API_PACING_SEC)

    log.info("done: %d processed, %d errors", len(todo) - errors, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

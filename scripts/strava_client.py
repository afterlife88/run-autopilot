#!/usr/bin/env python3
"""
Strava API client with auto-token-refresh and activity updater.

Updates Strava activity titles and descriptions with weather data,
adjusted pace, headwind analysis, and workout summaries.
"""
import configparser
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
RESULTS_DIR = os.path.join(BASE_DIR, "results")
TRACKS_DIR = os.path.join(os.path.dirname(BASE_DIR), "fitness-dashboard", "public", "tracks")

STRAVA_API = "https://www.strava.com/api/v3"


# ─── Config & Auth ────────────────────────────────────────────────

def load_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    return config


def save_config(config):
    """Persist only the [Strava] section, merged over a fresh read.

    Writing the whole in-memory snapshot would clobber keys other processes
    rotated meanwhile (e.g. the Stryd refresh token).
    """
    fresh = configparser.ConfigParser()
    fresh.read(CONFIG_FILE)
    if not fresh.has_section("Strava"):
        fresh.add_section("Strava")
    for k, v in config["Strava"].items():
        fresh["Strava"][k] = v
    with open(CONFIG_FILE, "w") as f:
        fresh.write(f)


def refresh_token_if_needed(config):
    """Auto-refresh Strava access token if expired."""
    expires_at = int(config["Strava"]["expires_at"])
    if time.time() < expires_at - 60:
        return config["Strava"]["access_token"]

    print("Strava token expired, refreshing...")
    data = (
        f"client_id={config['Strava']['client_id']}"
        f"&client_secret={config['Strava']['client_secret']}"
        f"&refresh_token={config['Strava']['refresh_token']}"
        f"&grant_type=refresh_token"
    ).encode()

    req = Request("https://www.strava.com/oauth/token", data=data, method="POST")
    with urlopen(req) as resp:
        result = json.loads(resp.read())

    config["Strava"]["access_token"] = result["access_token"]
    config["Strava"]["refresh_token"] = result["refresh_token"]
    config["Strava"]["expires_at"] = str(result["expires_at"])
    save_config(config)
    print(f"Token refreshed, expires at {datetime.fromtimestamp(result['expires_at'])}")
    return result["access_token"]


def strava_request(token, endpoint, method="GET", data=None):
    """Make an authenticated Strava API request. PUT data values are URL-encoded here."""
    from urllib.parse import quote

    url = f"{STRAVA_API}/{endpoint}"
    headers = {"Authorization": f"Bearer {token}"}

    if data and method == "PUT":
        encoded = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in data.items()).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = Request(url, data=encoded, headers=headers, method="PUT")
    else:
        req = Request(url, headers=headers, method=method)

    for attempt in range(3):
        try:
            with urlopen(req) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            # Strava 15-min quota exhausted: wait for the next window (:00/:15/:30/:45)
            wait = (900 - int(time.time()) % 900) + 30
            print(f"Strava 429, sleeping {wait}s until next rate-limit window...")
            time.sleep(wait)


# ─── Activity Matching ────────────────────────────────────────────

def find_activity_by_date(token, date_str):
    """Find a Run activity on a given date (YYYY-MM-DD)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    after = int(dt.timestamp())
    before = after + 86400

    activities = strava_request(
        token,
        f"athlete/activities?after={after}&before={before}&per_page=10"
    )

    # Prefer Run type
    runs = [a for a in activities if a["type"] == "Run"]
    if runs:
        # Return the longest run if multiple
        return max(runs, key=lambda a: a.get("distance", 0))

    return activities[0] if activities else None


def find_activity_near(token, start_epoch, distance_m=None, window_sec=3600):
    """
    Find the Run activity starting closest to start_epoch (UTC).

    Unlike find_activity_by_date, this matches a specific run even when there
    are multiple activities on the same day. If distance_m is given, candidates
    off by more than 10% (min 300 m) are rejected.
    """
    activities = strava_request(
        token,
        f"athlete/activities?after={start_epoch - window_sec}"
        f"&before={start_epoch + window_sec}&per_page=20"
    )

    best, best_delta = None, None
    for a in activities:
        if a.get("type") != "Run":
            continue
        try:
            act_epoch = int(datetime.strptime(
                a["start_date"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc).timestamp())
        except (KeyError, ValueError):
            continue
        if distance_m is not None:
            tolerance = max(300, distance_m * 0.10)
            if abs((a.get("distance") or 0) - distance_m) > tolerance:
                continue
        delta = abs(act_epoch - start_epoch)
        if best_delta is None or delta < best_delta:
            best, best_delta = a, delta

    return best


# ─── Weather Cost Calculation (mirrors frontend) ──────────────────

def temp_cost(temp):
    if 5 <= temp <= 15:
        return 0
    if temp < 5:
        return min(0.15 * (5 - temp), 4)
    if temp <= 20:
        return 0.4 * (temp - 15)
    if temp <= 30:
        return 2 + 1.0 * (temp - 20)
    return 12 + 2.0 * (temp - 30)


def effective_wind(avg, gust=None):
    if gust and gust > avg:
        return 0.6 * avg + 0.4 * gust
    return avg


def wind_cost(eff, exposure=0.5):
    if eff < 5:
        return 0
    w = eff - 5
    base = 0.007 * w * w
    return base * exposure * 2


def dew_point(temp, humidity):
    b, c = 17.67, 243.5
    gamma = math.log(humidity / 100) + (b * temp) / (c + temp)
    return (c * gamma) / (b - gamma)


def humidity_cost(dp):
    if dp < 10:
        return 0
    if dp <= 15:
        return 0.5 * (dp - 10)
    if dp <= 20:
        return 2.5 + 1.0 * (dp - 15)
    return 7.5 + 2.0 * (dp - 20)


def weather_label(total):
    if total < 1:
        return "ideal"
    if total < 3:
        return "mild"
    if total < 6:
        return "moderate"
    if total < 12:
        return "hard"
    return "severe"


LABEL_EMOJI = {
    "ideal": "✅",
    "mild": "🟢",
    "moderate": "🟡",
    "hard": "🟠",
    "severe": "🔴",
}


# ─── Headwind from GPS ───────────────────────────────────────────

def bearing(lat1, lon1, lat2, lon2):
    to_rad = math.pi / 180
    d_lon = (lon2 - lon1) * to_rad
    y = math.sin(d_lon) * math.cos(lat2 * to_rad)
    x = (math.cos(lat1 * to_rad) * math.sin(lat2 * to_rad) -
         math.sin(lat1 * to_rad) * math.cos(lat2 * to_rad) * math.cos(d_lon))
    brng = math.atan2(y, x) * (180 / math.pi)
    return (brng + 360) % 360


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    to_rad = math.pi / 180
    d_lat = (lat2 - lat1) * to_rad
    d_lon = (lon2 - lon1) * to_rad
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(lat1 * to_rad) * math.cos(lat2 * to_rad) * math.sin(d_lon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calc_headwind(track_points, wind_from_deg):
    """Calculate headwind exposure from GPS track."""
    if len(track_points) < 2:
        return None

    total_dist = 0
    weighted_hw = 0
    hw_dist = 0

    for i in range(len(track_points) - 1):
        p1, p2 = track_points[i], track_points[i + 1]
        dist = haversine(p1["lat"], p1["lon"], p2["lat"], p2["lon"])
        if dist < 1:
            continue

        brng = bearing(p1["lat"], p1["lon"], p2["lat"], p2["lon"])
        angle_diff = (wind_from_deg - brng) * math.pi / 180
        hw_frac = math.cos(angle_diff)

        weighted_hw += hw_frac * dist
        total_dist += dist
        if hw_frac > 0:
            hw_dist += dist

    if total_dist == 0:
        return None

    avg_frac = weighted_hw / total_dist
    exposure = max(0, (avg_frac + 1) / 2)
    hw_pct = round(hw_dist / total_dist * 100)

    return {
        "exposure": round(exposure, 2),
        "headwind_pct": hw_pct,
        "tailwind_pct": 100 - hw_pct,
    }


# ─── Description Builder ─────────────────────────────────────────

def format_pace(seconds):
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"


def _lap_seconds(time_str):
    parts = time_str.split(":") if time_str else []
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except ValueError:
        pass
    return None


def compass_arrow(deg):
    """Wind FROM direction to arrow showing where it blows TO."""
    # Array indexed by blows-to direction: 0°=↑(N), 45°=↗(NE), 90°=→(E), etc.
    arrows = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]
    blows_to = (deg + 180) % 360
    idx = round(blows_to / 45) % 8
    return arrows[idx]


def wind_dir_label(deg):
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                   "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return directions[round(deg / 22.5) % 16]


def build_description(workout, headwind_result=None):
    """Build a Strava description from workout data."""
    weather = workout.get("weather", {})
    if not weather or weather.get("indoor", True):
        return None  # No weather description for indoor runs

    lines = []

    # ─── Weather summary ───
    temp = weather.get("temperature_c")
    wind_avg = weather.get("wind_speed_kmh")
    wind_gust = weather.get("wind_gust_kmh")
    humidity = weather.get("humidity_pct")
    wind_dir_deg = weather.get("wind_direction_deg")
    precip = weather.get("precipitation_mm", 0)
    desc = weather.get("description", "")
    emoji = weather.get("emoji", "")

    lines.append(f"🌡️ {temp}°C {emoji} {desc}")

    wind_parts = []
    if wind_avg is not None:
        wind_parts.append(f"{wind_avg} km/h")
        if wind_dir_deg is not None:
            wind_parts.append(wind_dir_label(wind_dir_deg))
            wind_parts.append(compass_arrow(wind_dir_deg))
        if wind_gust and wind_gust > wind_avg:
            wind_parts.append(f"(gusts {wind_gust:.0f})")
    if wind_parts:
        lines.append(f"💨 Wind: {' '.join(wind_parts)}")

    if precip and precip > 0:
        lines.append(f"🌧️ Precipitation: {precip} mm")
    if humidity:
        lines.append(f"💧 Humidity: {humidity}%")

    # ─── Weather cost ───
    if temp is not None:
        t_cost = temp_cost(temp)
        eff_w = effective_wind(wind_avg or 0, wind_gust)
        exposure = headwind_result["exposure"] if headwind_result else 0.5
        w_cost = wind_cost(eff_w, exposure)

        dp = None
        h_cost = 0
        if humidity and humidity > 0:
            dp = dew_point(temp, humidity)
            h_cost = humidity_cost(dp)

        total = round(t_cost + w_cost + h_cost, 1)
        label = weather_label(total)
        label_e = LABEL_EMOJI.get(label, "")

        if total > 0:
            lines.append("")
            lines.append(f"⚡ Weather Impact: {label_e} {label} (+{total} s/km)")

            parts = []
            if t_cost > 0:
                parts.append(f"🌡️ temp +{t_cost:.1f}s")
            if w_cost > 0:
                parts.append(f"💨 wind +{w_cost:.1f}s")
            if h_cost > 0:
                parts.append(f"💧 humidity +{h_cost:.1f}s")
            if parts:
                lines.append("  " + " · ".join(parts))

            # Adjusted pace
            intervals = workout.get("intervals", [])
            paces = []
            for iv in intervals:
                pace_str = iv.get("pace") or iv.get("treadmill_pace")
                if pace_str:
                    m, s = pace_str.split(":")
                    paces.append(int(m) * 60 + float(s))
            if paces:
                avg_pace = sum(paces) / len(paces)
                ideal_pace = avg_pace - total
                lines.append(
                    f"  🏃 Actual: {format_pace(avg_pace)}/km → "
                    f"Ideal equiv: {format_pace(ideal_pace)}/km"
                )

            if headwind_result:
                hw = headwind_result
                hw_desc = (
                    "mostly into wind" if hw["headwind_pct"] > 65
                    else "balanced" if hw["headwind_pct"] > 40
                    else "mostly tailwind"
                )
                lines.append(
                    f"  🧭 Headwind: {hw['headwind_pct']}% ({hw_desc})"
                )
        else:
            lines.append("")
            lines.append("⚡ Weather: ✅ Ideal conditions")

    # ─── Blood glucose trend ───
    intervals = workout.get("intervals", [])
    w_type = workout.get("type", "")
    glucose_vals = [
        iv.get("glucose", {}).get("avg")
        for iv in intervals
        if iv.get("glucose", {}).get("avg") is not None
    ]
    if glucose_vals:
        first_bg = glucose_vals[0]
        last_bg = glucose_vals[-1]
        delta = last_bg - first_bg
        delta_str = f"+{delta:.1f}" if delta >= 0 else f"{delta:.1f}"

        # Trend arrow
        if delta < -2:
            bg_arrow = "📉"
        elif delta < -0.5:
            bg_arrow = "↘️"
        elif delta > 2:
            bg_arrow = "📈"
        elif delta > 0.5:
            bg_arrow = "↗️"
        else:
            bg_arrow = "→"

        # Mini sparkline: show key points (start, mid, end)
        lines.append("")
        lines.append(
            f"🩸 Glucose: {first_bg:.1f} → {last_bg:.1f} mmol/L "
            f"({delta_str}) {bg_arrow}"
        )
        # Range
        min_bg = min(glucose_vals)
        max_bg = max(glucose_vals)
        if max_bg - min_bg > 1:
            lines.append(f"  Range: {min_bg:.1f}–{max_bg:.1f} mmol/L")

    # ─── Stryd PowerCenter (RSS + Critical Power) ───
    stryd = workout.get("stryd") or {}
    if stryd.get("rss") or stryd.get("cp"):
        lines.append("")
        parts = []
        if stryd.get("rss") is not None:
            parts.append(f"RSS {stryd['rss']:.0f}")
        if stryd.get("cp"):
            parts.append(f"CP {stryd['cp']:.0f}W")
        if stryd.get("avg_power") and not any(iv.get("power") for iv in intervals):
            parts.append(f"avg {stryd['avg_power']:.0f}W")
        lines.append("🎽 Stryd: " + " · ".join(parts))

    # ─── Stryd power (avg over laps, weighted by duration) ───
    powered = [(iv.get("power") or 0,
                _lap_seconds(iv.get("time"))) for iv in intervals]
    powered = [(p, t) for p, t in powered if p > 0 and t]
    if powered and w_type not in ("Interval", "Threshold"):
        total_t = sum(t for _, t in powered)
        avg_power = round(sum(p * t for p, t in powered) / total_t)
        lines.append("")
        lines.append(f"🔋 Avg Power: {avg_power}W")

    if w_type in ("Interval", "Threshold") and len(intervals) > 1:
        lines.append("")
        lines.append("📊 Splits:")
        for iv in intervals:
            s = iv.get("set", "?")
            pace = iv.get("pace") or iv.get("treadmill_pace", "")
            hr = iv.get("hr", "")
            power = iv.get("power", "")
            t = iv.get("time", "")
            parts = [f"#{s}"]
            if pace:
                parts.append(f"{pace}/km")
            if t:
                parts.append(f"({t})")
            if power:
                parts.append(f"{power}W")
            if hr:
                parts.append(f"{hr}bpm")
            lac = iv.get("lactate")
            if lac:
                parts.append(f"🩸{lac}")
            lines.append("  " + " · ".join(parts))

    # ─── Lactate (always show if present) ───
    all_lactates = [(iv.get("set", "?"), iv["lactate"]) for iv in intervals if iv.get("lactate")]
    if all_lactates and w_type not in ("Interval", "Threshold"):
        # For non-interval workouts, show lactate as standalone line
        lines.append("")
        for s, lac in all_lactates:
            lines.append(f"🩸 Lactate: {lac} mmol/L (set #{s})")
    elif all_lactates and w_type in ("Interval", "Threshold"):
        # Already shown inline in splits above, but add summary
        lines.append("")
        final_lac = all_lactates[-1][1]
        lines.append(f"🩸 Post-workout lactate: {final_lac} mmol/L")

    return "\n".join(lines)


def build_title(workout):
    """Build a Strava activity title from workout data."""
    w_type = workout.get("type", "Run")
    name = workout.get("workout_name", "")
    distance = workout.get("summary_data", {}).get("distance", "")

    # Type emoji
    type_emoji = {
        "Threshold": "⚡",
        "Interval": "⚡",
        "Tempo": "🔥",
        "Moderate": "🏃",
        "Easy": "🏃",
        "Recovery": "🚶",
        "Long Run": "🏃‍♂️",
    }.get(w_type, "🏃")

    # Weather severity
    weather = workout.get("weather", {})
    weather_tag = ""
    if weather and not weather.get("indoor"):
        temp = weather.get("temperature_c")
        wind_gust = weather.get("wind_gust_kmh")
        if wind_gust and wind_gust > 40:
            weather_tag = " 🌬️"
        elif temp is not None and temp < 0:
            weather_tag = " 🥶"
        elif temp is not None and temp > 25:
            weather_tag = " 🥵"

    title = f"{type_emoji} {name}" if name else f"{type_emoji} {w_type}"
    if distance:
        title += f" — {distance}"
    title += weather_tag

    return title


# ─── Main ─────────────────────────────────────────────────────────

def update_activity(date_str, dry_run=False):
    """Update a Strava activity for the given date with weather data."""
    # Load workout
    result_file = os.path.join(RESULTS_DIR, f"{date_str}.json.txt")
    if not os.path.exists(result_file):
        print(f"No workout file for {date_str}")
        return None

    with open(result_file) as f:
        workout = json.load(f)

    # Load track for headwind
    headwind = None
    track_file = os.path.join(TRACKS_DIR, f"{date_str}.json")
    weather = workout.get("weather", {})
    wind_dir = weather.get("wind_direction_deg")

    if os.path.exists(track_file) and wind_dir is not None:
        with open(track_file) as f:
            track = json.load(f)
        if track.get("points"):
            headwind = calc_headwind(track["points"], wind_dir)

    # Build title and description
    title = build_title(workout)
    description = build_description(workout, headwind)

    if not description:
        print(f"{date_str}: Indoor/no weather — skipping")
        return None

    print(f"\n{'='*60}")
    print(f"Date: {date_str}")
    print(f"Title: {title}")
    print(f"\nDescription:\n{description}")
    print(f"{'='*60}")

    if dry_run:
        print("[DRY RUN — not updating Strava]")
        return {"title": title, "description": description}

    # Auth
    config = load_config()
    token = refresh_token_if_needed(config)

    # Find activity
    activity = find_activity_by_date(token, date_str)
    if not activity:
        print(f"No Strava activity found for {date_str}")
        return None

    print(f"Matched Strava activity: {activity['id']} ({activity['name']}, {activity.get('distance',0)/1000:.1f}km)")

    # Update (strava_request URL-encodes values)
    update_data = {"name": title, "description": description}
    result = strava_request(token, f"activities/{activity['id']}", method="PUT", data=update_data)
    print(f"✅ Updated: {result['name']}")
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Update Strava activities with weather data")
    parser.add_argument("date", nargs="?", help="Date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without updating")
    parser.add_argument("--all-outdoor", action="store_true", help="Update all outdoor workouts")
    args = parser.parse_args()

    if args.all_outdoor:
        for f in sorted(os.listdir(RESULTS_DIR)):
            if not f.endswith(".json.txt"):
                continue
            date = f.replace(".json.txt", "")
            with open(os.path.join(RESULTS_DIR, f)) as fh:
                w = json.load(fh)
            if w.get("weather", {}).get("indoor", True):
                continue
            try:
                update_activity(date, dry_run=args.dry_run)
            except Exception as e:
                print(f"  Error on {date}: {e}")
    else:
        date = args.date or datetime.now().strftime("%Y-%m-%d")
        update_activity(date, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

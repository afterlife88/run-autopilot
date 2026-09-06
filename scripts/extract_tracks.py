#!/usr/bin/env python3
"""
Extract GPS tracks from FIT files and save as simplified JSON for the dashboard map.

Output: public/tracks/{date}.json with ~300-500 points per track.

Each point: lat, lon, t (seconds from start), pace (min/km), hr, power, ele (meters).
Also includes bounds for map centering and weather summary from Stryd sensors.
"""
import json
import os
import sys
import glob
from datetime import datetime

import fitdecode

# Paths
FIT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trainings-files", "fit")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "results")
TRACKS_DIR = "/home/pi/fitness-dashboard/public/tracks"

# Semicircles to degrees
SEMI_TO_DEG = 180.0 / (2**31)

# Target number of points per track (simplification)
TARGET_POINTS = 400


def _safe_get(frame, field_name):
    """Safely get a field value from a FIT message, returning None if not found."""
    try:
        return frame.get_value(field_name)
    except (KeyError, ValueError):
        return None


def extract_track(fit_path: str) -> dict | None:
    """Extract GPS track + weather from a FIT file."""
    records = []
    session_start = None
    session_date = None
    weather = {}

    with fitdecode.FitReader(fit_path) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue

            if frame.name == "session":
                session_start = _safe_get(frame, "start_time")
                session_date = session_start.strftime("%Y-%m-%d") if session_start else None
                # Weather from session
                for field_name in ("avg_temperature", "min_temperature", "max_temperature"):
                    v = _safe_get(frame, field_name)
                    if v is not None:
                        weather[field_name] = v
                # Stryd baseline
                for field_name in ("Baseline Temperature", "Baseline Humidity", "Baseline Elevation"):
                    v = _safe_get(frame, field_name)
                    if v is not None:
                        key = field_name.lower().replace(" ", "_")
                        weather[key] = v

            elif frame.name == "record":
                lat = _safe_get(frame, "position_lat")
                lon = _safe_get(frame, "position_long")
                if lat is None or lon is None:
                    continue

                ts = _safe_get(frame, "timestamp")
                if ts is None:
                    continue

                speed = _safe_get(frame, "enhanced_speed")  # m/s
                hr = _safe_get(frame, "heart_rate")
                power = _safe_get(frame, "Power") or _safe_get(frame, "power")  # Stryd or native
                ele = _safe_get(frame, "enhanced_altitude")

                # Stryd per-record weather (take first valid)
                if "stryd_temperature" not in weather:
                    st = _safe_get(frame, "Stryd Temperature")
                    sh = _safe_get(frame, "Stryd Humidity")
                    if st is not None:
                        weather["stryd_temperature"] = st
                    if sh is not None:
                        weather["stryd_humidity"] = sh

                records.append({
                    "lat": lat * SEMI_TO_DEG,
                    "lon": lon * SEMI_TO_DEG,
                    "ts": ts,
                    "speed": speed,
                    "hr": hr,
                    "power": power,
                    "ele": ele,
                })

    if not records or not session_start:
        return None

    # Calculate time offsets
    start_ts = session_start.replace(tzinfo=None) if session_start.tzinfo else session_start
    for r in records:
        ts = r["ts"].replace(tzinfo=None) if r["ts"].tzinfo else r["ts"]
        r["t"] = (ts - start_ts).total_seconds()

    # Simplify: take every Nth point
    n = max(1, len(records) // TARGET_POINTS)
    simplified = records[::n]
    # Always include last point
    if simplified[-1] is not records[-1]:
        simplified.append(records[-1])

    # Build output points
    points = []
    for r in simplified:
        pace = None
        if r["speed"] and r["speed"] > 0.5:  # filter standing still
            pace = round(1000.0 / (r["speed"] * 60), 2)  # min/km
        points.append({
            "lat": round(r["lat"], 6),
            "lon": round(r["lon"], 6),
            "t": round(r["t"]),
            "pace": pace,
            "hr": r["hr"],
            "power": r["power"],
            "ele": round(r["ele"], 1) if r["ele"] is not None else None,
        })

    # Bounds
    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    bounds = {
        "north": max(lats),
        "south": min(lats),
        "east": max(lons),
        "west": min(lons),
    }

    return {
        "date": session_date,
        "total_points_raw": len(records),
        "total_points": len(points),
        "start": {"lat": points[0]["lat"], "lon": points[0]["lon"]},
        "end": {"lat": points[-1]["lat"], "lon": points[-1]["lon"]},
        "bounds": bounds,
        "weather": weather if weather else None,
        "points": points,
    }


def find_fit_for_date(date: str) -> str | None:
    """Find the FIT file that matches a workout date by checking result JSON."""
    # Check results dir for the date
    result_path = os.path.join(RESULTS_DIR, f"{date}.json.txt")
    if not os.path.exists(result_path):
        return None

    # Try to find FIT file reference in result, or just scan FIT files
    # Actually, we need to check each FIT file's session date
    return None  # Will use scan approach instead


def scan_all_fits() -> dict[str, str]:
    """Scan all FIT files and map date -> fit_path."""
    date_to_fit = {}
    fit_files = sorted(glob.glob(os.path.join(FIT_DIR, "*.fit")))

    for fit_path in fit_files:
        try:
            with fitdecode.FitReader(fit_path) as fit:
                for frame in fit:
                    if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "session":
                        start = _safe_get(frame, "start_time")
                        if start:
                            date = start.strftime("%Y-%m-%d")
                            date_to_fit[date] = fit_path
                        break
        except Exception as e:
            print(f"  ⚠ Error reading {os.path.basename(fit_path)}: {e}")

    return date_to_fit


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Extract GPS tracks from FIT files")
    parser.add_argument("--date", help="Process single date (YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing tracks")
    args = parser.parse_args()

    os.makedirs(TRACKS_DIR, exist_ok=True)

    print("Scanning FIT files...")
    date_to_fit = scan_all_fits()
    print(f"Found {len(date_to_fit)} FIT files")

    # Get list of workout dates from results
    result_dates = set()
    for f in os.listdir(RESULTS_DIR):
        if f.endswith(".json.txt"):
            result_dates.add(f.replace(".json.txt", ""))

    # Filter to specific date if requested
    if args.date:
        if args.date not in date_to_fit:
            print(f"No FIT file found for {args.date}")
            sys.exit(1)
        dates_to_process = [args.date]
    else:
        dates_to_process = sorted(date_to_fit.keys())

    extracted = 0
    skipped = 0
    failed = 0

    for date in dates_to_process:
        track_path = os.path.join(TRACKS_DIR, f"{date}.json")

        if os.path.exists(track_path) and not args.force:
            skipped += 1
            continue

        fit_path = date_to_fit.get(date)
        if not fit_path:
            continue

        try:
            track = extract_track(fit_path)
            if track and track["total_points"] > 10:
                with open(track_path, "w") as f:
                    json.dump(track, f, separators=(",", ":"))
                size_kb = os.path.getsize(track_path) / 1024
                print(f"  ✓ {date}: {track['total_points']} pts ({size_kb:.0f} KB) — {os.path.basename(fit_path)}")
                extracted += 1
            else:
                print(f"  ⚠ {date}: No GPS data or too few points")
                failed += 1
        except Exception as e:
            print(f"  ✗ {date}: {e}")
            failed += 1

    print(f"\nDone: {extracted} extracted, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()

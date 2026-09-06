#!/usr/bin/env python3
"""
Add weather data to fitness dashboard workouts.

Sources:
1. Open-Meteo Historical Weather API (hourly) — for all workouts
2. FIT sensor data (Stryd baseline + session temp) — from track files for outdoor runs

Adds a `weather` field to each workout in public/data.json.
"""
import json
import os
import sys
import time
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError

# Paths
DASHBOARD_DIR = "/home/pi/fitness-dashboard"
DATA_FILE = os.path.join(DASHBOARD_DIR, "public", "data.json")
TRACKS_DIR = os.path.join(DASHBOARD_DIR, "public", "tracks")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "results")
FIT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trainings-files", "fit")

# Copenhagen coordinates
LAT = 55.6761
LON = 12.5683

# WMO Weather interpretation codes → description
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

# WMO code → emoji
WMO_EMOJI = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️",
    45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌧️", 55: "🌧️",
    56: "🌧️", 57: "🌧️",
    61: "🌧️", 63: "🌧️", 65: "🌧️",
    66: "🌧️", 67: "🌧️",
    71: "🌨️", 73: "🌨️", 75: "🌨️", 77: "🌨️",
    80: "🌦️", 81: "🌧️", 82: "🌧️",
    85: "🌨️", 86: "🌨️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}


def get_workout_hours(workouts: list[dict]) -> dict[str, int]:
    """
    Determine the workout start hour (UTC) for each date.
    Uses pump_day.exercises if available, else defaults to 10 UTC.
    """
    date_hours = {}
    for w in workouts:
        date = w["date"]
        hour = 10  # default

        # Try to get from pump_day.exercises
        pump_day = w.get("pump_day", {})
        exercises = pump_day.get("exercises", [])
        if exercises:
            start_str = exercises[0].get("start", "")
            if start_str:
                try:
                    dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    hour = dt.hour
                except ValueError:
                    pass

        # Try workout_window
        elif pump_day.get("workout_window", {}).get("start"):
            try:
                dt = datetime.fromisoformat(
                    pump_day["workout_window"]["start"].replace("Z", "+00:00")
                )
                hour = dt.hour
            except ValueError:
                pass

        date_hours[date] = hour

    return date_hours


def fetch_open_meteo(start_date: str, end_date: str) -> dict:
    """
    Fetch hourly weather from Open-Meteo Historical API.
    Returns {date: {hour: {temp, humidity, wind_speed, wind_dir, precip, cloud, code, desc, emoji}}}
    """
    params = (
        f"latitude={LAT}&longitude={LON}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,"
        f"wind_direction_10m,wind_gusts_10m,precipitation,cloud_cover,weather_code"
        f"&timezone=UTC"
    )
    url = f"https://archive-api.open-meteo.com/v1/archive?{params}"

    print(f"Fetching Open-Meteo: {start_date} → {end_date}")
    req = Request(url, headers={"User-Agent": "fitness-dashboard/1.0"})

    for attempt in range(3):
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                break
        except (URLError, TimeoutError) as e:
            print(f"  Attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(2)
            else:
                raise

    hourly = data["hourly"]
    times = hourly["time"]  # ["2025-12-30T00:00", ...]

    result = {}
    for i, t in enumerate(times):
        date = t[:10]
        hour = int(t[11:13])

        if date not in result:
            result[date] = {}

        code = hourly["weather_code"][i]
        result[date][hour] = {
            "temperature_c": hourly["temperature_2m"][i],
            "humidity_pct": hourly["relative_humidity_2m"][i],
            "wind_speed_kmh": hourly["wind_speed_10m"][i],
            "wind_gust_kmh": hourly["wind_gusts_10m"][i],
            "wind_direction_deg": hourly["wind_direction_10m"][i],
            "precipitation_mm": hourly["precipitation"][i],
            "cloud_cover_pct": hourly["cloud_cover"][i],
            "weather_code": code,
            "description": WMO_CODES.get(code, "Unknown"),
            "emoji": WMO_EMOJI.get(code, "❓"),
        }

    return result


def load_fit_weather(date: str) -> dict | None:
    """Load FIT sensor weather from track file if it exists."""
    track_file = os.path.join(TRACKS_DIR, f"{date}.json")
    if not os.path.exists(track_file):
        return None

    with open(track_file) as f:
        track = json.load(f)

    return track.get("weather")


def wind_direction_label(deg: float | None) -> str:
    """Convert wind direction degrees to compass label."""
    if deg is None:
        return "?"
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                   "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = round(deg / 22.5) % 16
    return directions[idx]


def build_weather(workout: dict, meteo_data: dict, workout_hour: int) -> dict:
    """Build weather dict for a single workout."""
    date = workout["date"]
    weather = {}

    # Open-Meteo data for workout hour
    day_data = meteo_data.get(date, {})
    hour_data = day_data.get(workout_hour)

    if hour_data:
        weather["temperature_c"] = hour_data["temperature_c"]
        weather["humidity_pct"] = hour_data["humidity_pct"]
        weather["wind_speed_kmh"] = hour_data["wind_speed_kmh"]
        weather["wind_gust_kmh"] = hour_data.get("wind_gust_kmh")
        weather["wind_direction_deg"] = hour_data["wind_direction_deg"]
        weather["wind_direction"] = wind_direction_label(hour_data["wind_direction_deg"])
        weather["precipitation_mm"] = hour_data["precipitation_mm"]
        weather["cloud_cover_pct"] = hour_data["cloud_cover_pct"]
        weather["weather_code"] = hour_data["weather_code"]
        weather["description"] = hour_data["description"]
        weather["emoji"] = hour_data["emoji"]
        weather["source_hour_utc"] = workout_hour

        # Also grab min/max temp for the day (across all hours)
        day_temps = [h["temperature_c"] for h in day_data.values() if h["temperature_c"] is not None]
        if day_temps:
            weather["day_temp_min_c"] = min(day_temps)
            weather["day_temp_max_c"] = max(day_temps)

    # FIT sensor data (outdoor runs only)
    fit_weather = load_fit_weather(date)
    if fit_weather:
        weather["sensor"] = {
            "avg_temperature": fit_weather.get("avg_temperature"),
            "baseline_temperature": fit_weather.get("baseline_temperature"),
            "baseline_humidity": fit_weather.get("baseline_humidity"),
            "stryd_temperature": fit_weather.get("stryd_temperature"),
            "stryd_humidity": fit_weather.get("stryd_humidity"),
        }
        # Remove None values
        weather["sensor"] = {k: v for k, v in weather["sensor"].items() if v is not None}

    # Indoor flag
    weather["indoor"] = not os.path.exists(os.path.join(TRACKS_DIR, f"{date}.json"))

    return weather


def load_workouts_from_results() -> list[dict]:
    """Load workouts from individual result files (same source as API)."""
    workouts = []
    for f in sorted(os.listdir(RESULTS_DIR)):
        if not f.endswith(".json.txt"):
            continue
        path = os.path.join(RESULTS_DIR, f)
        with open(path) as fh:
            workouts.append(json.load(fh))
    workouts.sort(key=lambda w: w["date"])
    return workouts


def save_workout_result(workout: dict):
    """Save updated workout back to its result file."""
    path = os.path.join(RESULTS_DIR, f"{workout['date']}.json.txt")
    with open(path, "w") as f:
        json.dump(workout, f, indent=2, ensure_ascii=False)


def main():
    # Load workouts from result files (same source as API)
    workouts = load_workouts_from_results()
    print(f"Loaded {len(workouts)} workouts from {RESULTS_DIR}")

    # Get workout hours
    workout_hours = get_workout_hours(workouts)
    for date, hour in sorted(workout_hours.items()):
        print(f"  {date}: hour={hour} UTC")

    # Fetch Open-Meteo for full date range
    dates = sorted(workout_hours.keys())
    start_date = dates[0]
    end_date = dates[-1]
    meteo_data = fetch_open_meteo(start_date, end_date)
    print(f"Got meteo data for {len(meteo_data)} days")

    # Build weather for each workout
    updated = 0
    for w in workouts:
        date = w["date"]
        hour = workout_hours[date]
        weather = build_weather(w, meteo_data, hour)
        if weather:
            w["weather"] = weather
            save_workout_result(w)
            status = "outdoor" if not weather.get("indoor") else "indoor"
            desc = weather.get("description", "?")
            temp = weather.get("temperature_c", "?")
            print(f"  {date}: {temp}°C, {desc} ({status})")
            updated += 1

    print(f"Updated {updated}/{len(workouts)} workouts with weather data")


if __name__ == "__main__":
    main()

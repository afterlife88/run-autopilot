"""
FIT-only workout processor. Processes a single Garmin FIT file (with Stryd
power data embedded) into structured workout JSON matching the existing schema.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import fitdecode

# Optional Dexcom / Glooko
try:
    from dexcom_client import DexcomClient
    DEXCOM_AVAILABLE = True
except ImportError:
    DEXCOM_AVAILABLE = False

try:
    from glooko_client import GlookoClient
    GLOOKO_AVAILABLE = True
except ImportError:
    GLOOKO_AVAILABLE = False

# Import shared constants
try:
    from garmin_stryd_to_json import DEFAULT_LOCATION, RESULTS_DIR
except ImportError:
    DEFAULT_LOCATION = "Copenhagen, Denmark"
    RESULTS_DIR = "results"


def _rolling_avg(values, window=7):
    """Simple rolling average."""
    from collections import deque
    result = []
    q = deque()
    s = 0
    for v in values:
        q.append(v)
        s += v
        if len(q) > window:
            s -= q.popleft()
        result.append(s / len(q))
    return result


def _refine_interval_by_power(records, lap_start_time, lap_elapsed, session_start,
                               buffer_sec=15, smooth_window=7, threshold_pct=0.80):
    """
    Refine interval boundaries using per-second power data.

    Instead of trusting Garmin lap boundaries, detect the actual power plateau
    by finding where smoothed power crosses a threshold derived from the
    median power within the lap.

    Returns dict with refined metrics: power, hr, speed, duration_sec, 
    offset_start, offset_end (seconds from session start).
    Returns None if refinement fails.
    """
    if not records or not lap_start_time or not session_start:
        return None

    # Calculate lap offset from session start
    st_naive = session_start.replace(tzinfo=None) if hasattr(session_start, 'tzinfo') and session_start.tzinfo else session_start
    lap_naive = lap_start_time.replace(tzinfo=None) if hasattr(lap_start_time, 'tzinfo') and lap_start_time.tzinfo else lap_start_time
    lap_offset = (lap_naive - st_naive).total_seconds()

    # Get records within lap window + buffer
    search_start = lap_offset - buffer_sec
    search_end = lap_offset + lap_elapsed + buffer_sec

    lap_recs = []
    for r in records:
        ts = r.get("timestamp")
        if ts:
            ts_naive = ts.replace(tzinfo=None) if hasattr(ts, 'tzinfo') and ts.tzinfo else ts
            offset = (ts_naive - st_naive).total_seconds()
            if search_start <= offset <= search_end:
                lap_recs.append({
                    "offset": offset,
                    "power": r.get("power") or 0,
                    "hr": r.get("heart_rate") or 0,
                    "speed": r.get("enhanced_speed") or 0,
                })

    if len(lap_recs) < 10:
        return None

    # Calculate plateau power from records within the Garmin lap window
    within_lap = sorted(d["power"] for d in lap_recs
                        if lap_offset <= d["offset"] <= lap_offset + lap_elapsed and d["power"] > 0)
    if not within_lap:
        return None

    median_power = within_lap[len(within_lap) // 2]
    threshold = median_power * threshold_pct

    # Smooth power
    powers = [d["power"] for d in lap_recs]
    smoothed = _rolling_avg(powers, smooth_window)

    # Find onset: first time smoothed >= threshold
    onset_offset = lap_offset
    for j, d in enumerate(lap_recs):
        if j < len(smoothed) and smoothed[j] >= threshold:
            onset_offset = d["offset"]
            break

    # Find offset: last time smoothed >= threshold
    offset_end = lap_offset + lap_elapsed
    for j in range(len(lap_recs) - 1, -1, -1):
        if j < len(smoothed) and smoothed[j] >= threshold:
            offset_end = lap_recs[j]["offset"]
            break

    # Must be at least 10s
    if offset_end - onset_offset < 10:
        return None

    # Calculate metrics from refined window
    refined = [d for d in lap_recs if onset_offset <= d["offset"] <= offset_end]
    if not refined:
        return None

    avg_power = sum(d["power"] for d in refined) / len(refined)
    avg_hr = sum(d["hr"] for d in refined) / len(refined)
    avg_speed = sum(d["speed"] for d in refined) / len(refined)
    duration = offset_end - onset_offset

    return {
        "power": int(round(avg_power)),
        "hr": int(round(avg_hr)),
        "speed": avg_speed,
        "duration_sec": duration,
        "offset_start": onset_offset,
        "offset_end": offset_end,
    }


def _is_interval_workout(laps, records):
    """
    Detect whether this is an interval workout (with power spikes/drops)
    or a steady run (easy/long/threshold with consistent power).
    
    Returns True for interval workouts where power-based boundary
    detection should be applied.
    """
    # Check 1: Garmin intensity markers — if there are "rest" laps, it's intervals
    intensities = set(str(l.get("intensity", "")).lower() for l in laps)
    if "rest" in intensities:
        return True
    
    # Check 2: Power coefficient of variation
    # Steady runs: CV < 10%.  Intervals: CV > 12%.
    powers = [r.get("power") or 0 for r in records if (r.get("power") or 0) > 0]
    if len(powers) < 60:
        return False
    
    import statistics
    mean_p = statistics.mean(powers)
    if mean_p <= 0:
        return False
    sd_p = statistics.stdev(powers)
    cv = (sd_p / mean_p) * 100
    
    return cv > 10


def _find_closest_cgm(readings, target_time, max_delta_sec=600):
    """Find CGM value closest to target_time (within max_delta_sec)."""
    best = None
    best_diff = float('inf')
    for r in readings:
        try:
            ts = datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00')).replace(tzinfo=None)
            diff = abs((ts - target_time).total_seconds())
            if diff < best_diff and diff <= max_delta_sec:
                best_diff = diff
                best = r['value']
        except:
            continue
    return best


def _get_field(frame, name):
    """Get the first non-None value for a field name from a FIT frame."""
    for field in frame.fields:
        if field.name == name and field.value is not None:
            return field.value
    return None


def _format_time(seconds):
    """Format seconds to M:SS string."""
    if seconds is None or seconds <= 0:
        return "0:00"
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}:{secs:02d}"


def _format_pace(speed_ms):
    """Convert m/s speed to M:SS/km pace string."""
    if not speed_ms or speed_ms <= 0:
        return "0:00"
    pace_sec = 1000.0 / speed_ms
    mins = int(pace_sec // 60)
    secs = int(pace_sec % 60)
    return f"{mins}:{secs:02d}"


def _parse_fit(fit_path):
    """
    Parse a FIT file into structured lap, record, and session data.

    Returns:
        (laps, records, session) where:
        - laps: list of dicts with lap data
        - records: list of dicts with per-second data
        - session: dict with session summary
    """
    laps = []
    records = []
    session = {}

    with fitdecode.FitReader(fit_path) as reader:
        for frame in reader:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue

            if frame.name == "session":
                session = {
                    "start_time": _get_field(frame, "start_time"),
                    "total_distance": _get_field(frame, "total_distance"),
                    "total_elapsed_time": _get_field(frame, "total_elapsed_time"),
                    "avg_heart_rate": _get_field(frame, "avg_heart_rate"),
                    "sport": _get_field(frame, "sport"),
                    "sub_sport": _get_field(frame, "sub_sport"),
                }

            elif frame.name == "lap":
                lap = {
                    "intensity": _get_field(frame, "intensity"),
                    "start_time": _get_field(frame, "start_time"),
                    "total_elapsed_time": _get_field(frame, "total_elapsed_time"),
                    "total_distance": _get_field(frame, "total_distance"),
                    "enhanced_avg_speed": _get_field(frame, "enhanced_avg_speed"),
                    "avg_heart_rate": _get_field(frame, "avg_heart_rate"),
                    "lap_power": _get_field(frame, "Lap Power"),
                    "avg_running_cadence": _get_field(frame, "avg_running_cadence"),
                    "avg_stance_time": _get_field(frame, "avg_stance_time"),
                    "avg_vertical_oscillation": _get_field(frame, "avg_vertical_oscillation"),
                    "avg_step_length": _get_field(frame, "avg_step_length"),
                }
                laps.append(lap)

            elif frame.name == "record":
                record = {
                    "timestamp": _get_field(frame, "timestamp"),
                    "power": _get_field(frame, "Power"),
                    "heart_rate": _get_field(frame, "heart_rate"),
                    "enhanced_speed": _get_field(frame, "enhanced_speed"),
                    "distance": _get_field(frame, "distance"),
                    "cadence": _get_field(frame, "cadence"),
                }
                records.append(record)

    return laps, records, session


def preview_fit(fit_path):
    """
    Quick preview of a FIT file.

    Returns:
        Dict with date, start_time, total_distance_km, total_duration_min,
        avg_hr, sport, sub_sport, intervals list, and is_treadmill flag.
    """
    laps, records, session = _parse_fit(fit_path)

    start_time = session.get("start_time")
    total_distance = session.get("total_distance", 0) or 0
    total_elapsed = session.get("total_elapsed_time", 0) or 0

    active_laps = [lap for lap in laps if lap.get("intensity") == "active"]
    # For easy/recovery runs with no structured intervals, use all laps (auto-laps)
    if not active_laps:
        active_laps = laps

    intervals = []
    for i, lap in enumerate(active_laps):
        elapsed = lap.get("total_elapsed_time", 0) or 0
        speed = lap.get("enhanced_avg_speed")
        intervals.append({
            "set": str(i + 1),
            "time": _format_time(elapsed),
            "pace": _format_pace(speed),
            "power": int(lap.get("lap_power") or 0),
            "hr": int(lap.get("avg_heart_rate") or 0),
            "distance_m": round(lap.get("total_distance", 0) or 0, 1),
        })

    return {
        "date": start_time.strftime("%Y-%m-%d") if start_time else None,
        "start_time": start_time.strftime("%H:%M:%S") if start_time else None,
        "total_distance_km": round(total_distance / 1000, 2),
        "total_duration_min": round(total_elapsed / 60, 1),
        "avg_hr": session.get("avg_heart_rate"),
        "sport": session.get("sport"),
        "sub_sport": session.get("sub_sport"),
        "intervals": intervals,
        "is_treadmill": session.get("sub_sport") == "treadmill",
    }


def process_fit(fit_path, workout_meta=None, target_distance=None, no_glucose=False):
    """
    Process a Garmin FIT file into structured workout JSON.

    Args:
        fit_path: Path to the FIT file
        workout_meta: Dict with optional keys: coach_notes, workout_name,
                      workout_type, lactate (dict of interval_num: value)
        target_distance: If set, scale treadmill distances to this value in km
        no_glucose: Skip Dexcom glucose fetching

    Returns:
        Dict matching the output JSON schema.
    """
    workout_meta = workout_meta or {}
    laps, records, session = _parse_fit(fit_path)

    start_time = session.get("start_time")
    total_distance = session.get("total_distance", 0) or 0
    total_elapsed = session.get("total_elapsed_time", 0) or 0
    is_treadmill = session.get("sub_sport") == "treadmill"

    # Extract active laps (work intervals)
    active_laps = [lap for lap in laps if lap.get("intensity") == "active"]
    # For easy/recovery runs with no structured intervals, use all laps (auto-laps)
    has_structured_intervals = len(active_laps) > 0
    if not active_laps:
        active_laps = laps

    # Treadmill distance scaling
    scale_factor = 1.0
    if target_distance and total_distance > 0:
        recorded_km = total_distance / 1000
        scale_factor = target_distance / recorded_km
        total_distance = target_distance * 1000

    # Glucose data (Dexcom)
    glucose_readings = []
    config_path = os.path.join(os.path.dirname(__file__), "config.ini")
    
    if not no_glucose and DEXCOM_AVAILABLE and start_time and os.path.exists(config_path):
        try:
            # Garmin FIT is UTC, Dexcom Share returns account-local time.
            # Derive the offset live (handles travel); fall back to CET +1h.
            dexcom = DexcomClient(config_path)
            offset = dexcom.derive_utc_offset() or timedelta(hours=1)
            local_start = start_time + offset
            end_time = local_start + timedelta(seconds=total_elapsed)
            fetch_start = local_start - timedelta(minutes=10)
            fetch_end = end_time + timedelta(minutes=10)
            glucose_readings = dexcom.get_glucose_readings(fetch_start, fetch_end)
        except Exception:
            glucose_readings = []

    # Pump data (Glooko v3 API)
    pump_context = None
    if not no_glucose and GLOOKO_AVAILABLE and start_time and os.path.exists(config_path):
        try:
            glooko = GlookoClient(config_path)
            g_date = start_time.strftime("%Y-%m-%d")
            st_naive = start_time.replace(tzinfo=None) if start_time.tzinfo else start_time
            end_naive = st_naive + timedelta(seconds=total_elapsed)
            pump_context = glooko.get_pump_context(g_date, st_naive, end_naive)
        except Exception as e:
            print(f"Glooko fetch error: {e}")
            pump_context = None

    # Calculate cumulative time offsets for each active lap relative to workout start
    # Build a map of lap start/end times in seconds from workout start
    lap_offsets = []
    for lap in laps:
        lap_start = lap.get("start_time")
        lap_elapsed = lap.get("total_elapsed_time", 0) or 0
        if lap_start and start_time:
            start_sec = (lap_start - start_time).total_seconds()
            # Handle timezone differences
            if hasattr(start_time, 'tzinfo') and hasattr(lap_start, 'tzinfo'):
                start_naive = start_time.replace(tzinfo=None) if start_time.tzinfo else start_time
                lap_naive = lap_start.replace(tzinfo=None) if lap_start.tzinfo else lap_start
                start_sec = (lap_naive - start_naive).total_seconds()
        else:
            start_sec = 0
        lap_offsets.append((start_sec, start_sec + lap_elapsed))

    # Build intervals
    lactate_map = workout_meta.get("lactate", {})
    parsed_intervals = []
    active_idx = 0

    # Detect workout type: interval (power spikes) vs steady (consistent power)
    use_power_detection = _is_interval_workout(laps, records)

    # Build set of laps to include
    active_lap_set = set(id(lap) for lap in active_laps)

    for lap_idx, lap in enumerate(laps):
        if id(lap) not in active_lap_set:
            continue
        active_idx += 1

        elapsed = lap.get("total_elapsed_time", 0) or 0
        speed = lap.get("enhanced_avg_speed")
        distance_m = lap.get("total_distance", 0) or 0
        power = int(lap.get("lap_power") or 0)
        hr = int(lap.get("avg_heart_rate") or 0)

        if use_power_detection:
            # Interval workout: refine boundaries using per-second power data.
            # Determine available buffer from gaps to adjacent laps.
            prev_end = lap_offsets[lap_idx - 1][1] if lap_idx > 0 else None
            next_start = lap_offsets[lap_idx + 1][0] if lap_idx + 1 < len(lap_offsets) else None
            lap_start_sec = lap_offsets[lap_idx][0]
            lap_end_sec = lap_offsets[lap_idx][1]
            buf_before = min(15, lap_start_sec - prev_end) if prev_end is not None else 15
            buf_after = min(15, next_start - lap_end_sec) if next_start is not None else 15
            buf_before = max(0, buf_before)
            buf_after = max(0, buf_after)
            effective_buffer = min(buf_before, buf_after)

            refined = _refine_interval_by_power(
                records, lap.get("start_time"), elapsed, start_time,
                buffer_sec=effective_buffer
            )
            if refined:
                power = refined["power"]
                hr = refined["hr"]
                speed = refined["speed"]
                elapsed = refined["duration_sec"]
                # Update lap_offsets for glucose/pump alignment
                if lap_idx < len(lap_offsets):
                    lap_offsets[lap_idx] = (refined["offset_start"], refined["offset_end"])

        # Apply treadmill scaling
        if scale_factor != 1.0:
            distance_m *= scale_factor
            if distance_m > 0:
                speed = distance_m / elapsed  # recalculate speed

        pace = _format_pace(speed)

        interval = {
            "set": str(active_idx),
            "time": _format_time(elapsed),
            "pace": pace,
            "power": power,
            "hr": hr,
        }

        # Lactate
        lac = lactate_map.get(str(active_idx)) or lactate_map.get(active_idx)
        if lac is not None:
            interval["lactate"] = float(lac)

        # Glucose (Dexcom)
        # Apply +1h offset: Garmin FIT timestamps are UTC, Dexcom returns local time (CET = UTC+1)
        if glucose_readings and start_time and lap_idx < len(lap_offsets):
            try:
                start_sec, end_sec = lap_offsets[lap_idx]
                dexcom = DexcomClient()
                # Offset start_time by +1h so interval windows align with Dexcom local timestamps
                adjusted_start = start_time + timedelta(hours=1)
                g_data = dexcom.calculate_interval_glucose(
                    glucose_readings, start_sec, end_sec, adjusted_start
                )
                if g_data:
                    interval["glucose"] = {
                        "avg": g_data["glucose_avg"],
                        "start": g_data["glucose_start"],
                        "end": g_data["glucose_end"],
                        "trend": g_data["glucose_trend"],
                    }
            except Exception:
                pass

        # Pump Data (Glooko v3)
        if pump_context and start_time and lap_idx < len(lap_offsets):
            start_sec, end_sec = lap_offsets[lap_idx]
            st_naive = start_time.replace(tzinfo=None) if start_time.tzinfo else start_time
            
            # Apply +1h offset to align Glooko data (Pump time vs GPS time mismatch)
            # User observed 1h lag in pump data relative to workout
            offset = timedelta(hours=1)
            
            iv_start = st_naive + timedelta(seconds=start_sec) + offset
            iv_end = st_naive + timedelta(seconds=end_sec) + offset

            # CGM at interval start/end (find closest readings)
            cgm_readings = pump_context.get('cgm_readings', [])
            cgm_at_start = _find_closest_cgm(cgm_readings, iv_start)
            cgm_at_end = _find_closest_cgm(cgm_readings, iv_end)

            # Basal rate at interval time
            basal_rate = None
            if pump_context.get('basal_profile'):
                hour = iv_start.hour + iv_start.minute / 60
                for seg in pump_context['basal_profile']:
                    s = seg['segmentStart']
                    if s <= hour < s + seg['duration']:
                        basal_rate = seg['value']
                        break

            # Recent boluses (3h before interval start)
            recent = []
            for b in pump_context.get('boluses', []):
                recent.append(f"{b['insulin_delivered']}U @ {b['time']}"
                              + (f" ({b['type']})" if b['type'] != 'suggested' else ""))

            pump_data = {}
            if cgm_at_start is not None:
                pump_data['cgm_start'] = cgm_at_start
            if cgm_at_end is not None:
                pump_data['cgm_end'] = cgm_at_end
            if basal_rate is not None:
                pump_data['basal_rate'] = basal_rate
            if recent:
                pump_data['recent_boluses'] = recent

            if pump_data:
                interval['pump'] = pump_data

        parsed_intervals.append(interval)

    # Summary
    total_dist_km = total_distance / 1000
    avg_hr = session.get("avg_heart_rate", 0) or 0

    # Date
    if start_time:
        json_date = start_time.strftime("%Y-%m-%d")
    else:
        json_date = datetime.today().strftime("%Y-%m-%d")

    coach_notes = workout_meta.get("coach_notes", "")
    workout_name = workout_meta.get("workout_name", "Training Session")
    workout_type = workout_meta.get("workout_type", "Threshold")

    type_map = {
        "1": "Threshold", "2": "Recovery", "3": "Easy",
        "4": "Tempo", "5": "Interval",
    }
    workout_type = type_map.get(workout_type, workout_type)

    # Format duration
    total_mins = int(total_elapsed // 60)
    total_secs = int(total_elapsed % 60)
    duration_str = f"{total_mins}:{total_secs:02d}"

    result = {
        "date": json_date,
        "location": DEFAULT_LOCATION,
        "workout_name": workout_name,
        "type": workout_type,
        "summary_data": {
            "distance": f"{total_dist_km:.2f} km",
            "duration": duration_str,
            "avg_hr": int(avg_hr),
        },
        "intervals": parsed_intervals,
        "coach_notes": coach_notes,
    }

    # Readiness data (Garmin)
    if start_time:
        try:
            from garmin_client import GarminClient
            config_path_r = os.path.join(os.path.dirname(__file__), "config.ini")
            if os.path.exists(config_path_r):
                garmin = GarminClient(config_path_r)
                readiness_ctx = garmin.get_readiness_context(json_date)
                if readiness_ctx:
                    result['readiness'] = readiness_ctx
                    from readiness_scorer import compute_readiness_score
                    composite = compute_readiness_score(readiness_ctx, result, results_dir=RESULTS_DIR)
                    if composite:
                        result['readiness']['composite'] = composite
        except Exception as e:
            print(f"Readiness fetch error: {e}")

    # Pump summary (Glooko)
    if pump_context:
        result["pump_summary"] = {
            "basal_profile": pump_context.get('active_program'),
            "basal_at_start": pump_context.get('basal_at_start'),
            "daily_totals": pump_context.get('daily_totals'),
            "boluses_near_workout": pump_context.get('boluses', []),
            "carbs_near_workout": pump_context.get('carbs', []),
            "control_iq": pump_context.get('control_iq'),
        }

    return result


def _print_preview(fit_path):
    """Print a human-readable preview of a FIT file to stdout."""
    preview = preview_fit(fit_path)

    print(f"Date:     {preview['date']}")
    print(f"Start:    {preview['start_time']}")
    print(f"Distance: {preview['total_distance_km']} km")
    print(f"Duration: {preview['total_duration_min']} min")
    print(f"Avg HR:   {preview['avg_hr']} bpm")
    print(f"Sport:    {preview['sport']} / {preview['sub_sport']}")
    print(f"Treadmill: {preview['is_treadmill']}")
    print()

    intervals = preview["intervals"]
    if not intervals:
        print("No active intervals found.")
        return

    print(f"{'Set':>3s}  {'Time':>6s}  {'Pace':>6s}  {'Power':>5s}  {'HR':>4s}  {'Dist':>7s}")
    print("-" * 40)
    for iv in intervals:
        print(f"{iv['set']:>3s}  {iv['time']:>6s}  {iv['pace']:>6s}  {iv['power']:>5d}  {iv['hr']:>4d}  {iv['distance_m']:>6.0f}m")

    print(f"\n{len(intervals)} active intervals")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <fit_file>")
        sys.exit(1)

    _print_preview(sys.argv[1])

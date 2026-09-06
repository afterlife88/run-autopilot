#!/usr/bin/env python3
"""
Backfill Glooko pump data for ALL existing workout JSONs using the v3 API.
Patches JSON files directly without re-processing FIT files.
"""
import sys
import os
import json
import glob
import logging
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from glooko_client import GlookoClient, _parse_ts

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

LT_DIR = os.path.join(os.path.dirname(__file__), '..')
RESULTS_DIR = os.path.join(LT_DIR, 'results')
PENDING_FILE = os.path.join(LT_DIR, 'pending_workouts.json')
CONFIG_PATH = os.path.join(LT_DIR, 'config.ini')


def _find_closest_cgm(readings, target_time, max_delta_sec=600):
    best, best_diff = None, float('inf')
    for r in readings:
        try:
            ts = datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00')).replace(tzinfo=None)
            diff = abs((ts - target_time).total_seconds())
            if diff < best_diff and diff <= max_delta_sec:
                best_diff, best = diff, r['value']
        except:
            continue
    return best


def load_start_times():
    mapping = {}
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            for w in json.load(f):
                d, st = w.get('date'), w.get('start_time')
                if d and st:
                    mapping[d] = st
    return mapping


def overlay_pump_on_workout(workout, pump_context, start_dt):
    """Overlay rich pump data on a workout JSON."""
    intervals = workout.get('intervals', [])
    if not intervals or not pump_context:
        return False

    modified = False
    cumulative = 0

    for interval in intervals:
        # Parse duration
        parts = interval.get('time', '0:00').split(':')
        try:
            if len(parts) == 2:
                dur = int(parts[0]) * 60 + int(float(parts[1]))
            elif len(parts) == 3:
                dur = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
            else:
                dur = 0
        except ValueError:
            dur = 0

        # Apply +1h offset to align Glooko data (Pump time vs GPS time mismatch)
        offset = timedelta(hours=1)
        iv_start = start_dt + timedelta(seconds=cumulative) + offset
        iv_end = start_dt + timedelta(seconds=cumulative + dur) + offset
        cumulative += dur

        cgm_readings = pump_context.get('cgm_readings', [])
        cgm_s = _find_closest_cgm(cgm_readings, iv_start)
        cgm_e = _find_closest_cgm(cgm_readings, iv_end)

        basal = None
        if pump_context.get('basal_profile'):
            h = iv_start.hour + iv_start.minute / 60
            for seg in pump_context['basal_profile']:
                if seg['segmentStart'] <= h < seg['segmentStart'] + seg['duration']:
                    basal = seg['value']
                    break

        recent = []
        for b in pump_context.get('boluses', []):
            recent.append(f"{b['insulin_delivered']}U @ {b['time']}"
                          + (f" ({b['type']})" if b['type'] != 'suggested' else ''))

        pump = {}
        if cgm_s is not None:
            pump['cgm_start'] = cgm_s
        if cgm_e is not None:
            pump['cgm_end'] = cgm_e
        if basal is not None:
            pump['basal_rate'] = basal
        if recent:
            pump['recent_boluses'] = recent

        if pump:
            interval['pump'] = pump
            modified = True

    # Add summary
    if modified:
        workout['pump_summary'] = {
            'basal_profile': pump_context.get('active_program'),
            'basal_at_start': pump_context.get('basal_at_start'),
            'daily_totals': pump_context.get('daily_totals'),
            'boluses_near_workout': pump_context.get('boluses', []),
            'carbs_near_workout': pump_context.get('carbs', []),
            'control_iq': pump_context.get('control_iq'),
        }

    return modified


def backfill(force=False):
    start_times = load_start_times()
    glooko = GlookoClient(CONFIG_PATH)

    files = sorted(glob.glob(os.path.join(RESULTS_DIR, '*.json.txt')))
    logger.info(f"Found {len(files)} result files.")

    updated, skipped = 0, 0

    for fp in files:
        try:
            with open(fp, encoding='utf-8') as f:
                workout = json.load(f)
        except Exception:
            continue

        date = workout.get('date')
        if not date:
            continue

        # Skip if already has rich pump data (cgm_start)
        has_rich = any('cgm_start' in i.get('pump', {}) for i in workout.get('intervals', []))
        if has_rich and not force:
            logger.info(f"⏭️  {date} — already has v3 pump data")
            skipped += 1
            continue

        st_str = start_times.get(date, '12:00:00')
        try:
            start_dt = datetime.strptime(f"{date} {st_str}", "%Y-%m-%d %H:%M:%S")
        except:
            try:
                start_dt = datetime.strptime(f"{date} {st_str}", "%Y-%m-%d %H:%M")
            except:
                start_dt = datetime.strptime(date, "%Y-%m-%d").replace(hour=12)

        # Estimate end time from intervals
        total_dur = 0
        for iv in workout.get('intervals', []):
            parts = iv.get('time', '0:00').split(':')
            try:
                if len(parts) == 2:
                    total_dur += int(parts[0]) * 60 + int(float(parts[1]))
                elif len(parts) == 3:
                    total_dur += int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
            except ValueError:
                pass
        end_dt = start_dt + timedelta(seconds=total_dur + 600)

        try:
            ctx = glooko.get_pump_context(date, start_dt, end_dt)
        except Exception as e:
            logger.error(f"❌ {date} — Glooko error: {e}")
            skipped += 1
            continue

        # Also fetch full-day data for the chart
        try:
            # Apply +1h offset for workout window markers
            offset = timedelta(hours=1)
            pump_day = glooko.get_full_day_data(
                date,
                workout_start_utc=start_dt + offset,
                workout_end_utc=end_dt + offset,
            )
            workout['pump_day'] = pump_day
        except Exception as e:
            logger.warning(f"⚠️  {date} — pump_day fetch failed: {e}")

        if overlay_pump_on_workout(workout, ctx, start_dt):
            with open(fp, 'w', encoding='utf-8') as f:
                json.dump(workout, f, indent=2, ensure_ascii=False)
            name = workout.get('workout_name', '')
            logger.info(f"✅ {date} — {name}")
            updated += 1
        else:
            logger.info(f"⏭️  {date} — no matching pump data")
            skipped += 1

        time.sleep(0.5)

    logger.info(f"\nDone. Updated: {updated}, Skipped: {skipped}")


if __name__ == '__main__':
    force = '--force' in sys.argv
    backfill(force=force)

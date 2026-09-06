"""
Glooko API client for fetching pump, insulin, and CGM data.

Uses the eu.api.glooko.com v3 graph API which provides:
- Delivered bolus events (with IOB, carbs, correction/meal split)
- CGM readings (5-min resolution)
- Carb entries
- Daily insulin totals (basal vs bolus)
- Basal profile schedules
- Exercise events
"""

import requests
import re
import os
import configparser
import json
import logging
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

GRAPH_SERIES = [
    'deliveredBolus',
    'carbAll',
    'cgmHigh', 'cgmNormal', 'cgmLow',
    'bgHighManual', 'bgNormalManual', 'bgLowManual',
    'dailyInsulinTotals',
]

FULL_DAY_SERIES = GRAPH_SERIES + ['scheduledBasal']


class GlookoClient:
    MY_URL = "https://eu.my.glooko.com"
    API_URL = "https://eu.api.glooko.com"

    def __init__(self, config_path=None):
        if config_path is None:
            config_path = os.path.join(os.path.dirname(__file__), "config.ini")

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        })
        self.patient_code = None
        self.logged_in = False

        config = configparser.ConfigParser()
        config.read(config_path)
        self.username = config.get('Glooko', 'username', fallback=None)
        self.password = config.get('Glooko', 'password', fallback=None)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self):
        """Login via eu.my.glooko.com web form. Sets session cookies for API."""
        if not self.username or not self.password:
            raise ValueError("Glooko credentials not configured in config.ini")

        r = self.session.get(f"{self.MY_URL}/users/sign_in")
        soup = BeautifulSoup(r.text, 'html.parser')
        token = soup.find('input', {'name': 'authenticity_token'})
        if not token:
            raise RuntimeError("Could not find CSRF token on login page")

        self.session.post(f"{self.MY_URL}/users/sign_in", data={
            'authenticity_token': token['value'],
            'user[email]': self.username,
            'user[password]': self.password,
            'commit': 'Sign in',
        })

        # Hit dashboard to finalise session and extract patient code
        d = self.session.get(f"{self.MY_URL}/?locale=en")
        m = re.search(r'window\.patient\s*=\s*"([^"]+)"', d.text)
        if m:
            self.patient_code = m.group(1)
        else:
            raise RuntimeError("Login succeeded but could not extract patient code")

        self.logged_in = True
        logger.info(f"Glooko: logged in as {self.patient_code}")

    def _ensure_login(self):
        if not self.logged_in:
            self.login()

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _api_get(self, path, params=None):
        self._ensure_login()
        headers = {
            'Accept': 'application/json',
            'Origin': self.MY_URL,
            'Referer': f'{self.MY_URL}/',
        }
        r = self.session.get(f"{self.API_URL}{path}", params=params,
                             headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def get_graph_data(self, start_date, end_date, series=None):
        """
        Fetch graph data from api/v3/graph/data.

        Parameters
        ----------
        start_date, end_date : str  ISO-8601 or YYYY-MM-DD
        series : list[str] or None  Which series to fetch (default: all useful)

        Returns dict with 'series' and 'devices' keys.
        """
        if series is None:
            series = GRAPH_SERIES

        # Normalise dates
        if len(start_date) == 10:
            start_date += "T00:00:00.000Z"
        if len(end_date) == 10:
            end_date += "T23:59:59.999Z"

        params = {
            'patient': self.patient_code,
            'startDate': start_date,
            'endDate': end_date,
            'series[]': series,
        }
        return self._api_get('/api/v3/graph/data', params)

    def get_devices_and_settings(self):
        """Fetch pump profiles, basal schedules, bolus settings."""
        return self._api_get('/api/v3/devices_and_settings',
                             {'patient': self.patient_code})

    def get_exercise_data(self, start_date, end_date):
        if len(start_date) == 10:
            start_date += "T00:00:00.000Z"
        if len(end_date) == 10:
            end_date += "T23:59:59.999Z"
        return self._api_get('/api/v3/graph/exercise_data', {
            'patient': self.patient_code,
            'startDate': start_date,
            'endDate': end_date,
        })

    def get_statistics(self, start_date, end_date):
        if len(start_date) == 10:
            start_date += "T00:00:00.000Z"
        if len(end_date) == 10:
            end_date += "T23:59:59.999Z"
        return self._api_get('/api/v3/graph/statistics/overall', {
            'patient': self.patient_code,
            'startDate': start_date,
            'endDate': end_date,
            'includeInsulin': 'true',
            'includeExercise': 'true',
            'includePumpModes': 'true',
        })

    # ------------------------------------------------------------------
    # High-level: pump context for a workout
    # ------------------------------------------------------------------

    def get_pump_context(self, workout_date, start_time_utc, end_time_utc):
        """
        Get full pump context around a workout.

        Parameters
        ----------
        workout_date : str   YYYY-MM-DD
        start_time_utc : datetime
        end_time_utc : datetime

        Returns dict with keys:
            basal_profile, boluses, carbs, cgm_readings,
            daily_totals, pump_summary
        """
        # Fetch 1 day of data (the workout day)
        graph = self.get_graph_data(workout_date, workout_date)
        series = graph.get('series', {})

        # Window: 3h before workout start to workout end
        window_start = start_time_utc - timedelta(hours=3)
        window_end = end_time_utc + timedelta(minutes=15)

        # -- Boluses --
        boluses = []
        for b in series.get('deliveredBolus', []):
            ts = _parse_ts(b.get('timestamp'))
            if ts and window_start <= ts <= window_end:
                boluses.append({
                    'timestamp': b['timestamp'],
                    'time': ts.strftime('%H:%M'),
                    'insulin_delivered': b.get('insulinDelivered', 0),
                    'insulin_programmed': b.get('insulinProgrammed', 0),
                    'iob': b.get('insulinOnBoard'),
                    'carbs_input': b.get('carbsInput', 0),
                    'bg_input': b.get('bloodGlucoseInput'),
                    'correction': b.get('insulinRecommendationForCorrection'),
                    'for_carbs': b.get('insulinRecommendationForCarbs'),
                    'type': b.get('type', 'unknown'),
                    'is_interrupted': b.get('isInterrupted', False),
                    'extended_delivery': b.get('extendedDelivery'),
                    'extended_duration': b.get('extendedBolusDuration'),
                })

        # -- Carbs --
        carbs = []
        for c in series.get('carbAll', []):
            ts = _parse_ts(c.get('timestamp'))
            if ts and window_start <= ts <= window_end:
                carbs.append({
                    'timestamp': c['timestamp'],
                    'time': ts.strftime('%H:%M'),
                    'grams': c.get('yOrig', c.get('carbs', 0)),
                })

        # -- CGM readings --
        cgm = []
        for bucket in ['cgmNormal', 'cgmHigh', 'cgmLow']:
            for r in series.get(bucket, []):
                ts = _parse_ts(r.get('timestamp'))
                if ts and window_start <= ts <= window_end:
                    cgm.append({
                        'timestamp': r['timestamp'],
                        'value': r.get('y'),
                    })
        cgm.sort(key=lambda x: x['timestamp'])

        # -- Daily totals --
        daily_totals = {}
        dit = series.get('dailyInsulinTotals', {})
        if isinstance(dit, dict):
            for epoch_str, totals in dit.items():
                daily_totals = totals  # Take the workout day's totals
                break  # First (and likely only for single-day query)

        # -- Basal profile --
        basal_profile = self._get_basal_profile()

        # -- Basal rate at workout start (scheduled, not actual delivered) --
        basal_at_start = None
        if basal_profile:
            hour = start_time_utc.hour + start_time_utc.minute / 60
            for seg in basal_profile:
                seg_start = seg['segmentStart']
                seg_end = seg_start + seg['duration']
                if seg_start <= hour < seg_end:
                    basal_at_start = seg['value']
                    break

        # -- Control-IQ mode stats (if available) --
        control_iq = None
        try:
            g_date = start_time_utc.strftime("%Y-%m-%d")
            stats = self.get_statistics(g_date, g_date)
            control_iq = {
                'auto_pct': stats.get('controlIqPumpModeAutomaticPercentage'),
                'exercise_pct': stats.get('controlIqPumpModeExercisePercentage'),
                'sleep_pct': stats.get('controlIqPumpModeSleepPercentage'),
                'manual_pct': stats.get('controlIqPumpModeManualPercentage'),
                'scheduled_basal_sum': stats.get('scheduledBasalsSum'),
                'actual_basal_units': stats.get('basalUnitsPerDay'),
                'total_insulin': stats.get('totalPumpInsulinPerDay'),
            }
        except Exception:
            pass

        return {
            'basal_profile': basal_profile,
            'basal_at_start': basal_at_start,
            'active_program': 'Main',
            'boluses': boluses,
            'carbs': carbs,
            'cgm_readings': cgm,
            'daily_totals': {
                'total_insulin': daily_totals.get('totalInsulinPerDay'),
                'basal_units': daily_totals.get('basalUnitsPerDay'),
                'bolus_units': daily_totals.get('bolusUnitsPerDay'),
            },
            'control_iq': control_iq,
        }

    def get_full_day_data(self, workout_date, workout_start_utc=None, workout_end_utc=None):
        """
        Get full-day pump data for charting (CGM, basal, boluses, carbs, exercise).

        Returns a flat dict ready for JSON storage and React charting.
        """
        graph = self.get_graph_data(workout_date, workout_date, series=FULL_DAY_SERIES)
        series = graph.get('series', {})

        # -- CGM (full day, deduplicated) --
        cgm = []
        seen_ts = set()
        for bucket in ['cgmNormal', 'cgmHigh', 'cgmLow']:
            for r in series.get(bucket, []):
                ts = r.get('timestamp', '')
                if ts and ts not in seen_ts:
                    seen_ts.add(ts)
                    cgm.append({'time': ts, 'value': r.get('y')})
        cgm.sort(key=lambda x: x['time'])

        # -- Delivered basal (deduplicated, non-interpolated preferred) --
        basal_raw = series.get('scheduledBasal', [])
        basal = []
        seen_basal = set()
        for b in basal_raw:
            ts = b.get('timestamp', '')
            key = (ts, b.get('rate'))
            if key not in seen_basal:
                seen_basal.add(key)
                # Prefer non-interpolated entries
                if not b.get('interpolated', False) or (ts, b['rate']) not in seen_basal:
                    basal.append({
                        'time': ts,
                        'rate': b.get('rate', 0),
                        'duration': b.get('duration', 0),
                    })
        basal.sort(key=lambda x: x['time'])
        # Deduplicate further: keep only unique timestamps (prefer first/non-interp)
        final_basal = []
        seen_times = set()
        for b in basal:
            if b['time'] not in seen_times:
                seen_times.add(b['time'])
                final_basal.append(b)
        basal = final_basal

        # -- Boluses (full day) --
        boluses = []
        for b in series.get('deliveredBolus', []):
            ts = b.get('timestamp', '')
            if ts:
                boluses.append({
                    'time': ts,
                    'units': b.get('insulinDelivered', 0),
                    'carbs': b.get('carbsInput', 0),
                    'iob': b.get('insulinOnBoard'),
                    'type': b.get('type', 'unknown'),
                })
        boluses.sort(key=lambda x: x['time'])

        # -- Carbs (full day) --
        carbs = []
        for c in series.get('carbAll', []):
            ts = c.get('timestamp', '')
            if ts:
                carbs.append({
                    'time': ts,
                    'grams': c.get('yOrig', c.get('carbs', 0)),
                })
        carbs.sort(key=lambda x: x['time'])

        # -- Exercise events --
        exercises = []
        try:
            ex_data = self.get_exercise_data(workout_date, workout_date)
            for e in ex_data.get('series', {}).get('exercise', []):
                exercises.append({
                    'start': e.get('timestamp', ''),
                    'duration_sec': e.get('duration', 0),
                    'type': e.get('type', ''),
                    'calories': e.get('calories', 0),
                    'distance_km': e.get('distanceKilometers', 0),
                })
        except Exception:
            pass

        # -- Workout window (for highlighting) --
        workout_window = None
        if workout_start_utc and workout_end_utc:
            workout_window = {
                'start': workout_start_utc.isoformat() + 'Z',
                'end': workout_end_utc.isoformat() + 'Z',
            }

        return {
            'cgm': cgm,
            'basal': basal,
            'boluses': boluses,
            'carbs': carbs,
            'exercises': exercises,
            'workout_window': workout_window,
        }

    def _get_basal_profile(self):
        """Extract the active basal profile from device settings."""
        try:
            ds = self.get_devices_and_settings()
            pumps = ds.get('deviceSettings', {}).get('pumps', {})
            for pump_id, settings_by_ts in pumps.items():
                # Get most recent settings
                latest_ts = sorted(settings_by_ts.keys())[-1]
                latest = settings_by_ts[latest_ts]
                profiles = latest.get('pumpProfilesBasal', [])
                for p in profiles:
                    segs = p.get('segments', {})
                    if segs.get('current'):
                        return segs.get('data', [])
        except Exception as e:
            logger.warning(f"Could not fetch basal profile: {e}")
        return None


def _parse_ts(ts_str):
    """Parse ISO timestamp string to datetime."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace('Z', '+00:00')).replace(tzinfo=None)
    except:
        return None


# ------------------------------------------------------------------
# CLI test
# ------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = GlookoClient()
    ctx = client.get_pump_context(
        '2026-02-22',
        datetime(2026, 2, 22, 11, 59),
        datetime(2026, 2, 22, 13, 59),
    )
    print(json.dumps(ctx, indent=2, default=str))

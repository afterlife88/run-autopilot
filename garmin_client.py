"""
Garmin Connect API client for fetching workout data.
"""
import configparser
import os
import json
from datetime import datetime, timezone

from garminconnect import Garmin

TOKEN_DIR = os.path.expanduser("~/.garmin_tokens")
POLL_STATE_FILE = os.path.expanduser("~/.garmin_last_poll")


class GarminClient:
    def __init__(self, config_path="config.ini"):
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")

        config = configparser.ConfigParser()
        config.read(config_path)

        self.username = config.get("Garmin", "username")
        self.password = config.get("Garmin", "password")

        if not self.username or self.username == "user@email.com":
            raise ValueError("Please update config.ini with your Garmin credentials")

        self.client = None

    def login(self):
        """Authenticate with Garmin Connect. Uses cached tokens when possible."""
        self.client = Garmin(self.username, self.password)
        os.makedirs(TOKEN_DIR, exist_ok=True)
        self.client.login(tokenstore=TOKEN_DIR)
        return True

    def get_running_activities(self, since_timestamp=None, limit=10, max_activities=400):
        """
        Get recent running activities.

        Pages through Garmin history when since_timestamp is set, so activities
        older than the first page are not silently dropped. Without
        since_timestamp, returns at most `limit` most recent activities.

        Args:
            since_timestamp: datetime — only return activities after this time
            limit: max number of activities when since_timestamp is None
            max_activities: hard cap on paged fetching

        Returns:
            List of dicts with activity metadata, newest first.
        """
        if not self.client:
            self.login()

        results = []
        page_size = 50 if since_timestamp else limit
        start = 0

        while True:
            activities = self.client.get_activities(
                start=start, limit=page_size, activitytype="running"
            )
            if not activities:
                break

            reached_older = False
            for act in activities:
                start_dt = self._parse_dt(act.get("startTimeLocal", ""))
                start_dt_gmt = self._parse_dt(act.get("startTimeGMT", ""))

                if since_timestamp and start_dt:
                    since_naive = since_timestamp.replace(tzinfo=None) if since_timestamp.tzinfo else since_timestamp
                    if start_dt <= since_naive:
                        reached_older = True
                        continue

                duration_sec = act.get("duration", 0)
                results.append({
                    "activity_id": str(act.get("activityId", "")),
                    "activity_name": act.get("activityName", ""),
                    "start_time": start_dt,
                    "start_time_gmt": start_dt_gmt,
                    "distance_km": round((act.get("distance", 0) or 0) / 1000, 2),
                    "duration_min": round(duration_sec / 60, 1),
                    "avg_hr": act.get("averageHR"),
                    "sport_type": act.get("activityType", {}).get("typeKey", "running"),
                })

            if not since_timestamp or reached_older:
                break
            start += page_size
            if start >= max_activities or len(activities) < page_size:
                break

        return results

    @staticmethod
    def _parse_dt(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None

    def download_activity_fit(self, activity_id, save_path):
        """
        Download original FIT file for an activity.

        Args:
            activity_id: Garmin activity ID
            save_path: Path to save the FIT file
        """
        if not self.client:
            self.login()

        data = self.client.download_activity(
            activity_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
        )

        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        # The ORIGINAL format returns a ZIP; extract the .fit file
        import zipfile
        import io

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            fit_files = [f for f in zf.namelist() if f.endswith(".fit")]
            if not fit_files:
                # Might be raw FIT bytes (not zipped)
                with open(save_path, "wb") as f:
                    f.write(data)
                return save_path

            with zf.open(fit_files[0]) as src, open(save_path, "wb") as dst:
                dst.write(src.read())

        return save_path

    @staticmethod
    def get_last_poll_timestamp():
        """Read last poll timestamp from state file."""
        if not os.path.exists(POLL_STATE_FILE):
            return None
        try:
            with open(POLL_STATE_FILE, "r") as f:
                ts_str = f.read().strip()
            return datetime.fromisoformat(ts_str)
        except (ValueError, OSError):
            return None

    @staticmethod
    def set_last_poll_timestamp(ts):
        """Write poll timestamp to state file."""
        with open(POLL_STATE_FILE, "w") as f:
            f.write(ts.isoformat())


    def get_readiness_context(self, date_str):
        """Fetch readiness data for a workout date. date_str = 'YYYY-MM-DD'"""
        if not self.client:
            self.login()

        result = {}

        # Training readiness
        try:
            tr = self.client.get_training_readiness(date_str)
            if tr and isinstance(tr, list) and tr:
                morning = next((r for r in tr if r.get('inputContext') == 'AFTER_WAKEUP_RESET'), tr[0])
                if morning:
                    result['training_readiness'] = {
                        'score': morning.get('score'),
                        'level': morning.get('level'),
                        'feedback': morning.get('feedbackShort'),
                        'sleep_score': morning.get('sleepScore'),
                        'recovery_time_hrs': round(morning.get('recoveryTime', 0) / 60, 1),
                        'hrv_factor_pct': morning.get('hrvFactorPercent'),
                        'stress_history_pct': morning.get('stressHistoryFactorPercent'),
                        'acwr_pct': morning.get('acwrFactorPercent'),
                        'acute_load': morning.get('acuteLoad'),
                    }
        except Exception as e:
            print(f"  Training readiness fetch error: {e}")

        # Sleep data
        try:
            sleep = self.client.get_sleep_data(date_str)
            if sleep and isinstance(sleep, dict):
                daily = sleep.get('dailySleepDTO', {})
                scores = daily.get('sleepScores', {})
                total_sleep_sec = daily.get('sleepTimeSeconds', 0) or 0
                deep = daily.get('deepSleepSeconds', 0) or 0
                rem = daily.get('remSleepSeconds', 0) or 0
                light = daily.get('lightSleepSeconds', 0) or 0
                result['sleep'] = {
                    'score': scores.get('overall', {}).get('value'),
                    'quality': scores.get('overall', {}).get('qualifierKey'),
                    'duration_hrs': round(total_sleep_sec / 3600, 1) if total_sleep_sec else None,
                    'deep_pct': round(deep / total_sleep_sec * 100) if total_sleep_sec else None,
                    'rem_pct': round(rem / total_sleep_sec * 100) if total_sleep_sec else None,
                    'light_pct': round(light / total_sleep_sec * 100) if total_sleep_sec else None,
                    'avg_hr': daily.get('avgHeartRate') or daily.get('averageSpO2HRSleep'),
                    'avg_spo2': daily.get('averageSpO2Value'),
                    'avg_respiration': daily.get('averageRespirationValue'),
                    'feedback': daily.get('sleepScoreFeedback'),
                }
        except Exception as e:
            print(f"  Sleep data fetch error: {e}")

        # HRV
        try:
            hrv = self.client.get_hrv_data(date_str)
            if hrv and isinstance(hrv, dict):
                summary = hrv.get('hrvSummary', {})
                baseline = summary.get('baseline', {})
                result['hrv'] = {
                    'last_night_avg': summary.get('lastNightAvg'),
                    'weekly_avg': summary.get('weeklyAvg'),
                    'status': summary.get('status'),
                    'baseline_low': baseline.get('lowUpper'),
                    'baseline_high': baseline.get('balancedUpper'),
                }
        except Exception as e:
            print(f"  HRV data fetch error: {e}")

        # Body battery
        try:
            bb = self.client.get_body_battery(date_str)
            if bb and isinstance(bb, list) and bb:
                entry = bb[0]
                vals = entry.get('bodyBatteryValuesArray', [])
                if vals:
                    non_none = [v[1] for v in vals if v[1] is not None]
                    wakeup_val = max(non_none) if non_none else None
                    result['body_battery'] = {
                        'wakeup': wakeup_val,
                        'charged': entry.get('charged'),
                        'drained': entry.get('drained'),
                    }
        except Exception as e:
            print(f"  Body battery fetch error: {e}")

        # Stress
        try:
            stress = self.client.get_stress_data(date_str)
            if stress and isinstance(stress, dict):
                result['stress'] = {
                    'avg': stress.get('avgStressLevel'),
                    'max': stress.get('maxStressLevel'),
                }
        except Exception as e:
            print(f"  Stress data fetch error: {e}")

        return result if result else None


if __name__ == "__main__":
    client = GarminClient()
    client.login()
    print("Logged in to Garmin Connect")
    activities = client.get_running_activities(limit=3)
    for a in activities:
        print(f"  {a['start_time']}  {a['activity_name']}  {a['distance_km']} km  {a['duration_min']} min")

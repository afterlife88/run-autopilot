"""
Dexcom Share API client for fetching glucose readings.
"""
import configparser
import os
from datetime import datetime, timedelta
from pydexcom import Dexcom

class DexcomClient:
    def __init__(self, config_path='config.ini'):
        """Initialize Dexcom client with credentials from config file."""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")

        config = configparser.ConfigParser()
        config.read(config_path)

        self.username = config.get('Dexcom', 'username')
        self.password = config.get('Dexcom', 'password')
        self.region = config.get('Dexcom', 'region', fallback='us')

        if self.username == 'your_dexcom_username' or not self.username:
            raise ValueError("Please update config.ini with your Dexcom credentials")

        self.dexcom = None

    def connect(self):
        """Authenticate with Dexcom Share API."""
        try:
            print(f"🔐 Connecting to Dexcom Share ({self.region.upper()} region)...")
            self.dexcom = Dexcom(username=self.username, password=self.password, region=self.region)
            print("✅ Connected to Dexcom Share")
            return True
        except Exception as e:
            print(f"❌ Failed to connect to Dexcom: {e}")
            print("\nTroubleshooting:")
            print("1. Make sure Dexcom Share is enabled in your Dexcom G6 app")
            print("2. Verify your username and password in config.ini")
            print("3. Check that region is set correctly (us, ous, or jp)")
            print("4. Wait a few minutes after enabling Share for it to activate")
            return False

    def derive_utc_offset(self):
        """Derive the offset between Dexcom Share timestamps and UTC.

        Share reports the account's local time; the latest reading is at most
        ~5 min old, so (reading_time - utc_now) rounded to 30 min gives the
        current UTC offset — robust when travelling across timezones.
        Returns a timedelta, or None if no current reading is available.
        """
        if not self.dexcom:
            if not self.connect():
                return None
        try:
            reading = self.dexcom.get_current_glucose_reading()
            if reading is None:
                return None
            reading_dt = reading.datetime.replace(tzinfo=None)
            now_utc = datetime.utcnow()
            offset_sec = (reading_dt - now_utc).total_seconds()
            return timedelta(seconds=round(offset_sec / 1800) * 1800)
        except Exception as e:
            print(f"⚠️ Could not derive Dexcom UTC offset: {e}")
            return None

    def get_glucose_readings(self, start_time, end_time, max_count=288):
        """
        Fetch glucose readings between start_time and end_time.

        Args:
            start_time: datetime object for workout start
            end_time: datetime object for workout end
            max_count: Maximum number of readings to fetch (default 288 = 24 hours of 5-min readings)

        Returns:
            List of dicts with timestamp, value (mg/dL), and trend
        """
        if not self.dexcom:
            if not self.connect():
                return []

        try:
            print(f"📊 Fetching glucose readings from {start_time.strftime('%H:%M')} to {end_time.strftime('%H:%M')}...")

            # Fetch recent readings (pydexcom gets most recent N readings)
            glucose_readings = self.dexcom.get_glucose_readings(max_count=max_count)

            # Normalize to naive datetimes for comparison (remove timezone info)
            start_time_naive = start_time.replace(tzinfo=None) if start_time.tzinfo else start_time
            end_time_naive = end_time.replace(tzinfo=None) if end_time.tzinfo else end_time

            # Filter to workout timeframe
            filtered_readings = []
            for reading in glucose_readings:
                # Convert reading time to naive for comparison
                reading_time = reading.datetime.replace(tzinfo=None) if reading.datetime.tzinfo else reading.datetime

                if start_time_naive <= reading_time <= end_time_naive:
                    # Convert mg/dL to mmol/L (divide by 18.0)
                    glucose_mmol = round(reading.value / 18.0, 1)
                    filtered_readings.append({
                        'timestamp': reading_time,  # Use naive timestamp for consistency
                        'value': glucose_mmol,  # mmol/L
                        'trend': reading.trend_description  # e.g., "steady", "rising", "falling"
                    })

            # Sort by timestamp
            filtered_readings.sort(key=lambda x: x['timestamp'])

            print(f"   ✅ Found {len(filtered_readings)} glucose readings during workout")
            return filtered_readings

        except Exception as e:
            print(f"❌ Error fetching glucose data: {e}")
            return []

    def calculate_interval_glucose(self, readings, interval_start_sec, interval_end_sec, workout_start_time):
        """
        Calculate glucose metrics for a specific interval.

        Args:
            readings: List of glucose readings from get_glucose_readings()
            interval_start_sec: Interval start time in seconds from workout start
            interval_end_sec: Interval end time in seconds from workout start
            workout_start_time: datetime object for workout start

        Returns:
            Dict with glucose_avg, glucose_start, glucose_end, glucose_trend
        """
        if not readings:
            return None

        # Convert interval times to datetime (make sure they're naive for comparison)
        workout_start_naive = workout_start_time.replace(tzinfo=None) if workout_start_time.tzinfo else workout_start_time
        interval_start = workout_start_naive + timedelta(seconds=interval_start_sec)
        interval_end = workout_start_naive + timedelta(seconds=interval_end_sec)

        # Filter readings for this interval (timestamps in readings are already naive from get_glucose_readings)
        interval_readings = [
            r for r in readings
            if interval_start <= r['timestamp'] <= interval_end
        ]

        if not interval_readings:
            # No readings in interval, find closest reading
            closest = min(readings, key=lambda r: abs((r['timestamp'] - interval_start).total_seconds()))
            return {
                'glucose_avg': closest['value'],
                'glucose_start': closest['value'],
                'glucose_end': closest['value'],
                'glucose_trend': closest['trend']
            }

        # Calculate metrics (values already in mmol/L)
        values = [r['value'] for r in interval_readings]
        avg_glucose = round(sum(values) / len(values), 1)
        start_glucose = interval_readings[0]['value']
        end_glucose = interval_readings[-1]['value']

        # Determine overall trend for interval (0.6 mmol/L = ~10 mg/dL)
        if end_glucose > start_glucose + 0.6:
            trend = "rising"
        elif end_glucose < start_glucose - 0.6:
            trend = "falling"
        else:
            trend = "steady"

        return {
            'glucose_avg': avg_glucose,
            'glucose_start': start_glucose,
            'glucose_end': end_glucose,
            'glucose_trend': trend
        }


def test_connection():
    """Test Dexcom connection and fetch latest reading."""
    try:
        client = DexcomClient()
        if client.connect():
            reading = client.dexcom.get_current_glucose_reading()
            glucose_mmol = round(reading.value / 18.0, 1)
            print(f"\n📊 Latest glucose reading:")
            print(f"   Value: {glucose_mmol} mmol/L ({reading.value} mg/dL)")
            print(f"   Time: {reading.datetime}")
            print(f"   Trend: {reading.trend_description}")
            print("\n✅ Dexcom integration is working!")
            return True
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        print("\nMake sure:")
        print("1. You've copied config.ini.example to config.ini")
        print("2. You've added your Dexcom credentials to config.ini")
        print("3. Dexcom Share is enabled in your Dexcom G6 app")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    test_connection()

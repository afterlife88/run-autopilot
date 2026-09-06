"""Unit tests for the power-zone workout classifier.

The reference cases are real workouts (lap numbers taken from actual FIT
files) that the classifier must keep getting right.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auto_sync import classify  # noqa: E402


def iv(time, power, dist_m, hr=150):
    return {"set": "1", "time": time, "pace": time, "power": power,
            "hr": hr, "distance_m": float(dist_m)}


def preview(intervals, dist_km, duration_min, avg_hr=145):
    return {
        "total_distance_km": dist_km,
        "total_duration_min": duration_min,
        "avg_hr": avg_hr,
        "intervals": intervals,
        "is_treadmill": False,
    }


def test_10x1k_threshold_reps():
    # 2026-08-27 "LT 10x1k": 1 km reps must NOT be mistaken for km auto-laps
    laps = [iv("4:00", 323, 1000, 166) for _ in range(10)]
    wtype, name = classify(preview(laps, 17.0, 89), cp=297)
    assert (wtype, name) == ("Threshold", "10×1k")


def test_7x6min_with_split_first_rep():
    # 2026-08-20: first 6' rep split into two laps by a mid-rep pause
    laps = [iv("4:03", 320, 1000), iv("1:56", 325, 483)]
    laps += [iv("6:00", 322, 1510) for _ in range(6)]
    wtype, name = classify(preview(laps, 17.1, 85), cp=290)
    assert (wtype, name) == ("Threshold", "7×6'")


def test_4x800_interval():
    # 2026-08-22 "vo2 4x800": short hot reps → Interval
    laps = [iv("2:40", 351, 800, 170) for _ in range(4)]
    wtype, name = classify(preview(laps, 11.0, 50), cp=290)
    assert (wtype, name) == ("Interval", "4×800m")


def test_easy_run_km_autolaps_not_reps():
    laps = [iv("4:45", 220, 1000, 125) for _ in range(10)]
    wtype, name = classify(preview(laps, 10.0, 48, avg_hr=125), cp=304)
    assert (wtype, name) == ("Easy", "Easy 10k")


def test_moderate_by_power_zone():
    laps = [iv("4:55", 252, 1000, 152) for _ in range(16)]
    wtype, name = classify(preview(laps, 16.3, 80, avg_hr=152), cp=304)
    assert (wtype, name) == ("Moderate", "Moderate 16k")


def test_recovery_by_power_zone():
    laps = [iv("5:40", 200, 1000, 118) for _ in range(6)]
    wtype, name = classify(preview(laps, 6.0, 34, avg_hr=118), cp=304)
    assert (wtype, name) == ("Recovery", "Recovery 6k")


def test_long_run_by_distance():
    laps = [iv("4:58", 245, 1000, 140) for _ in range(28)]
    wtype, name = classify(preview(laps, 28.0, 139, avg_hr=140), cp=304)
    assert (wtype, name) == ("Long Run", "Long Run 28k")


def test_custom_watch_name_preserved():
    laps = [iv("4:00", 323, 1000) for _ in range(10)]
    wtype, name = classify(preview(laps, 17.0, 89), garmin_name="LT 10x1k", cp=297)
    assert (wtype, name) == ("Threshold", "LT 10x1k")


def test_garmin_default_name_replaced():
    laps = [iv("4:45", 220, 1000, 125) for _ in range(10)]
    _, name = classify(preview(laps, 10.0, 48, avg_hr=125),
                       garmin_name="Golem Running", cp=304)
    assert name == "Easy 10k"


def test_no_cp_fallback_uniform_reps():
    # Without CP, uniform non-km-lap structure is still detected
    laps = [iv("6:00", 322, 1510) for _ in range(6)]
    wtype, name = classify(preview(laps, 15.0, 70))
    assert (wtype, name) == ("Threshold", "6×6'")


def test_no_cp_fallback_hr_recovery():
    laps = [iv("5:40", 0, 1000, 120) for _ in range(6)]
    wtype, _ = classify(preview(laps, 6.0, 34, avg_hr=120))
    assert wtype == "Recovery"


def test_hard_long_run_not_mistaken_for_reps():
    # 2026-09-06: 30 km at ~89% CP — km auto-laps above 90% CP must not
    # become "25×1k"; continuous work covering the whole run = Long Run
    laps = [iv("4:47", 276 if i % 2 else 269, 1000, 155) for i in range(30)]
    wtype, name = classify(preview(laps, 30.1, 144, avg_hr=155), cp=304)
    assert (wtype, name) == ("Long Run", "Long Run 30k")

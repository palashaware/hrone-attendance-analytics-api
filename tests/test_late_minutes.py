from datetime import datetime, timedelta, timezone

from app.main import compute_late_minutes

IST = timezone(timedelta(hours=5, minutes=30))


def test_early_punch_in_before_day_shift_is_not_late():
    punch_in = datetime(2026, 7, 6, 3, 50, tzinfo=timezone.utc)  # 09:20 IST
    assert compute_late_minutes(punch_in, "09:30", "18:30") == 0


def test_early_overnight_hour_punch_in_before_day_shift_is_not_late():
    punch_in = datetime(2026, 7, 6, 19, 30, tzinfo=timezone.utc)  # 01:00 IST next day
    assert compute_late_minutes(punch_in, "09:30", "18:30") == 0


def test_exactly_ten_minutes_late_is_not_late():
    punch_in = datetime(2026, 7, 6, 4, 10, tzinfo=timezone.utc)  # 09:40 IST
    assert compute_late_minutes(punch_in, "09:30", "18:30") == 0


def test_more_than_ten_minutes_late_uses_floor_minutes():
    punch_in = datetime(2026, 7, 6, 4, 15, 1, tzinfo=timezone.utc)  # 09:45:01 IST
    assert compute_late_minutes(punch_in, "09:30", "18:30") == 15


def test_overnight_shift_uses_previous_day_shift_start():
    punch_in = datetime(2026, 7, 7, 0, 29, tzinfo=timezone.utc)  # 05:59 IST
    assert compute_late_minutes(punch_in, "22:00", "06:00") == 479


def test_early_punch_in_before_overnight_shift_is_not_late():
    punch_in = datetime(2026, 7, 6, 15, 30, tzinfo=timezone.utc)  # 21:00 IST
    assert compute_late_minutes(punch_in, "22:00", "06:00") == 0

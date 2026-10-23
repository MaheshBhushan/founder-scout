from datetime import datetime, timezone

from founder_scout.outreach import limit_reset

NOW = datetime(2026, 10, 23, 13, 54, tzinfo=timezone.utc)  # 15:54 in Berlin


def test_reset_later_today():
    r = limit_reset("claude exited 1: You've hit your session limit · resets 4:10pm (Europe/Berlin)", NOW)
    assert r == datetime(2026, 10, 23, 14, 10, tzinfo=timezone.utc)


def test_reset_time_already_passed_means_tomorrow():
    r = limit_reset("claude exited 1: You've hit your session limit · resets 9am (Europe/Berlin)", NOW)
    assert r == datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc)


def test_reset_with_date():
    r = limit_reset("claude exited 1: You've hit your weekly limit · resets Oct 27, 4pm (Europe/Berlin)", NOW)
    assert r == datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)


def test_unparseable_limit_waits_an_hour_and_other_errors_are_ignored():
    assert limit_reset("claude exited 1: usage limit reached", NOW) == datetime(2026, 10, 23, 14, 54, tzinfo=timezone.utc)
    assert limit_reset("tracker HTTP 500: boom", NOW) is None

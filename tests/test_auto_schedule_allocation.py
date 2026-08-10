"""
allocate_slots is the correctness-critical part of Auto Schedule: it must
never double-book a slot, must respect stated preferences, and must
prioritize by speedup_hours -- all deterministically, with no AI involved.
"""
import importlib

auto_schedule = importlib.import_module("cogs.auto_schedule")

STANDARD_SLOTS = [f"{h:02}:{m:02}" for h in range(24) for m in (0, 30)]


def test_higher_speedup_hours_wins_a_contested_slot():
    requests = [
        {"name": "Low", "speedup_hours": 10, "preferred_windows": [{"start": "12:00", "end": "12:00"}]},
        {"name": "High", "speedup_hours": 1000, "preferred_windows": [{"start": "12:00", "end": "12:00"}]},
    ]
    result = auto_schedule.allocate_slots(requests, STANDARD_SLOTS)

    high = next(a for a in result["assigned"] if a["name"] == "High")
    assert high["time"] == "12:00"
    low = next((a for a in result["assigned"] if a["name"] == "Low"), None)
    assert low is None or low["time"] != "12:00"


def test_equal_speedup_hours_keeps_message_order_on_tie():
    requests = [
        {"name": "First", "speedup_hours": 100, "preferred_windows": [{"start": "12:00", "end": "12:00"}]},
        {"name": "Second", "speedup_hours": 100, "preferred_windows": [{"start": "12:00", "end": "12:00"}]},
    ]
    result = auto_schedule.allocate_slots(requests, STANDARD_SLOTS)

    first = next(a for a in result["assigned"] if a["name"] == "First")
    assert first["time"] == "12:00"
    assert "Second" in result["unscheduled"]


def test_no_preferred_windows_means_fully_flexible():
    requests = [{"name": "Anyone", "speedup_hours": 5, "preferred_windows": []}]
    result = auto_schedule.allocate_slots(requests, STANDARD_SLOTS)

    assert len(result["assigned"]) == 1
    assert result["assigned"][0]["time"] in STANDARD_SLOTS


def test_full_day_window_matches_example_message_hestia():
    """'I need 00:00 - 23:59 time slot' -- must match every slot in the grid."""
    requests = [{"name": "Hestia", "speedup_hours": 7456, "preferred_windows": [{"start": "00:00", "end": "23:59"}]}]
    result = auto_schedule.allocate_slots(requests, STANDARD_SLOTS)

    assert result["assigned"][0]["name"] == "Hestia"
    assert result["assigned"][0]["time"] == "00:00", "first slot in the flexible window should be picked"


def test_second_preference_used_when_first_is_taken():
    requests = [
        {"name": "Blocker", "speedup_hours": 999, "preferred_windows": [{"start": "12:00", "end": "12:00"}]},
        {
            "name": "Joan of Arc",
            "speedup_hours": 720,  # "30 days" normalized to hours
            "preferred_windows": [
                {"start": "12:00", "end": "12:00"},
                {"start": "15:00", "end": "15:00"},
            ],
        },
    ]
    result = auto_schedule.allocate_slots(requests, STANDARD_SLOTS)

    joan = next(a for a in result["assigned"] if a["name"] == "Joan of Arc")
    assert joan["time"] == "15:00", "must fall through to the 2nd preference when the 1st is already taken"


def test_range_window_matches_example_message_dragutin():
    """'Desde las 6 UTC a 22 UTC' (20 dias) -- a wide range window."""
    requests = [{"name": "Dragutin", "speedup_hours": 480, "preferred_windows": [{"start": "06:00", "end": "22:00"}]}]
    result = auto_schedule.allocate_slots(requests, STANDARD_SLOTS)

    assert result["assigned"][0]["time"] == "06:00"


def test_person_with_no_free_candidate_slot_is_unscheduled_not_dropped_or_double_booked():
    requests = [
        {"name": "A", "speedup_hours": 100, "preferred_windows": [{"start": "12:00", "end": "12:00"}]},
        {"name": "B", "speedup_hours": 50, "preferred_windows": [{"start": "12:00", "end": "12:00"}]},
    ]
    result = auto_schedule.allocate_slots(requests, STANDARD_SLOTS)

    assert len(result["assigned"]) == 1
    assert result["assigned"][0]["name"] == "A"
    assert result["unscheduled"] == ["B"]


def test_wraparound_window_crossing_midnight():
    requests = [{"name": "NightOwl", "speedup_hours": 10, "preferred_windows": [{"start": "23:00", "end": "01:00"}]}]
    result = auto_schedule.allocate_slots(requests, STANDARD_SLOTS)

    assert result["assigned"][0]["time"] in ("23:00", "23:30", "00:00", "00:30", "01:00")


def test_never_double_books_a_slot_even_with_many_flexible_requests():
    requests = [{"name": f"Person{i}", "speedup_hours": i, "preferred_windows": []} for i in range(48)]
    result = auto_schedule.allocate_slots(requests, STANDARD_SLOTS)

    assigned_times = [a["time"] for a in result["assigned"]]
    assert len(assigned_times) == len(set(assigned_times)) == 48
    assert result["unscheduled"] == []


def test_capacity_exceeded_reports_unscheduled_instead_of_erroring():
    requests = [{"name": f"Person{i}", "speedup_hours": 48 - i, "preferred_windows": []} for i in range(50)]
    result = auto_schedule.allocate_slots(requests, STANDARD_SLOTS)

    assert len(result["assigned"]) == 48
    assert len(result["unscheduled"]) == 2
    # The two highest-numbered (lowest speedup_hours) requests lose out.
    assert set(result["unscheduled"]) == {"Person48", "Person49"}

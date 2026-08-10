"""
Auto Schedule: reads free-text scheduling requests posted in a configured
Discord channel, uses Gemini (cogs/auto_schedule_ai.py) to extract structured
{name, speedup_hours, preferred_windows} from each message, then deterministically
assigns each person exactly one of the 48 daily Chief-Minister-style slots.

Stored as appointments rows with appointment_type='Auto Schedule' -- a fully
independent schedule from the Chief Minister one (same table, different
appointment_type, so the existing unique indexes keep them from ever
colliding), always as manual_name rows since these are free-text names that
won't reliably match registered users.
"""
import logging

logger = logging.getLogger('bot')

APPOINTMENT_TYPE = "Auto Schedule"


def _slot_in_window(slot: str, window: dict) -> bool:
    """window: {"start": "HH:MM", "end": "HH:MM"}. Handles a window that
    wraps past midnight (start > end, e.g. 22:00-02:00)."""
    start, end = window.get("start", "00:00"), window.get("end", "23:59")
    if start <= end:
        return start <= slot <= end
    return slot >= start or slot <= end


def _candidate_slots(preferred_windows, time_slots):
    """Ordered, deduped candidate slots for one person: windows are tried in
    the order given (1st preference's slots before 2nd's); no windows at all
    means fully flexible -- every slot is a candidate."""
    if not preferred_windows:
        return list(time_slots)

    candidates = []
    seen = set()
    for window in preferred_windows:
        for slot in time_slots:
            if slot in seen:
                continue
            if _slot_in_window(slot, window):
                candidates.append(slot)
                seen.add(slot)
    return candidates


def allocate_slots(requests: list, time_slots: list) -> dict:
    """
    requests: [{"name": str, "speedup_hours": float, "preferred_windows": [{"start": "HH:MM", "end": "HH:MM"}, ...]}, ...]
    time_slots: the ordered 48-slot grid (from MinisterSchedule.get_time_slots)

    Deterministic, conflict-free: higher speedup_hours picks first; within a
    person's candidate slots, the earliest-listed free one wins; a person
    with no free candidate slot left is reported unscheduled rather than
    silently dropped or double-booking someone else.

    Returns {"assigned": [{"name": str, "time": "HH:MM"}], "unscheduled": [str]}.
    """
    # Stable sort: equal speedup_hours keep their original (message) order.
    ordered = sorted(requests, key=lambda r: -(r.get("speedup_hours") or 0))

    taken = set()
    assigned = []
    unscheduled = []

    for req in ordered:
        name = req["name"]
        candidates = _candidate_slots(req.get("preferred_windows"), time_slots)

        chosen = next((slot for slot in candidates if slot not in taken), None)
        if chosen:
            taken.add(chosen)
            assigned.append({"name": name, "time": chosen})
        else:
            unscheduled.append(name)

    return {"assigned": assigned, "unscheduled": unscheduled}

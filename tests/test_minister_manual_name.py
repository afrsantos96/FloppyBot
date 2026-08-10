"""
Portal-assigned slots have no registered fid -- they're stored as
(fid=NULL, manual_name=<text>). Every board-text generator and the Discord
reschedule conflict-check must treat those rows as "occupied", not silently
skip them (a NULL fid is falsy, and `fid != ?` is NULL/false in SQL for a
NULL row, so both are easy to get wrong).
"""
import asyncio
import importlib
import sqlite3
from types import SimpleNamespace

mm = importlib.import_module("cogs.minister_menu")
ms = importlib.import_module("cogs.minister_schedule")


def _dbs():
    svs = sqlite3.connect(":memory:")
    svs.execute("""CREATE TABLE appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fid INTEGER, manual_name TEXT, appointment_type TEXT, time TEXT, alliance INTEGER)""")
    svs.execute("CREATE TABLE reference (context TEXT PRIMARY KEY, context_id INTEGER)")
    svs.execute("INSERT INTO reference VALUES ('slot_mode', 0)")
    svs.commit()

    users = sqlite3.connect(":memory:")
    users.execute("CREATE TABLE users (fid INTEGER, nickname TEXT, alliance INTEGER)")
    users.executemany("INSERT INTO users VALUES (?,?,?)", [(1, "Alice", 5), (2, "Bob", 5)])
    users.commit()

    alliance = sqlite3.connect(":memory:")
    alliance.execute("CREATE TABLE alliance_list (alliance_id INTEGER, name TEXT)")
    alliance.execute("INSERT INTO alliance_list VALUES (5, 'TestAlli')")
    alliance.commit()
    return svs, users, alliance


def _mk_schedule_cog(svs, users, alliance):
    cog = ms.MinisterSchedule.__new__(ms.MinisterSchedule)
    cog.svs_conn = svs
    cog.svs_cursor = svs.cursor()
    cog.users_cursor = users.cursor()
    cog.alliance_cursor = alliance.cursor()
    return cog


def test_generate_time_list_shows_manual_name_slot():
    svs, users, alliance = _dbs()
    cog = _mk_schedule_cog(svs, users, alliance)

    time_list, booked = cog.generate_time_list({"09:00": (None, "Guest Governor", None)})

    joined = "\n".join(time_list)
    assert "Guest Governor" in joined


def test_generate_available_time_list_excludes_manual_name_slot():
    svs, users, alliance = _dbs()
    cog = _mk_schedule_cog(svs, users, alliance)

    booked = {"09:00": (None, "Guest Governor", None)}
    available = cog.generate_available_time_list(booked)

    assert not any("09:00" in line for line in available), "manual-name slot must not show as available"


def test_generate_booked_time_list_includes_manual_name_slot():
    svs, users, alliance = _dbs()
    cog = _mk_schedule_cog(svs, users, alliance)

    booked = {"09:00": (None, "Guest Governor", None)}
    booked_list = cog.generate_booked_time_list(booked)

    assert any("Guest Governor" in line for line in booked_list)


def test_generate_time_list_still_handles_fid_rows():
    """Regression guard: the manual_name branch must not break the existing
    fid + alliance display path."""
    svs, users, alliance = _dbs()
    cog = _mk_schedule_cog(svs, users, alliance)

    time_list, booked = cog.generate_time_list({"10:00": (1, None, 5)})

    joined = "\n".join(time_list)
    assert "Alice" in joined
    assert "TestAlli" in joined


def _mk_menu_cog(svs, users, alliance):
    cog = mm.MinisterMenu.__new__(mm.MinisterMenu)
    cog.svs_conn = svs
    cog.svs_cursor = svs.cursor()
    cog.users_cursor = users.cursor()
    cog.alliance_cursor = alliance.cursor()
    cog.bot = SimpleNamespace(get_cog=lambda name: None)

    async def show_filtered(interaction, activity, msg, is_error=False):
        cog._last_message = (msg, is_error)

    cog.show_filtered_user_select_with_message = show_filtered
    return cog


def _interaction():
    return SimpleNamespace(
        response=SimpleNamespace(is_done=lambda: True),
        followup=SimpleNamespace(send=lambda *a, **k: asyncio.sleep(0)),
        user=SimpleNamespace(display_name="Admin", avatar=None),
    )


def test_complete_booking_detects_conflict_with_manual_name_slot():
    """A slot occupied by a portal-assigned manual name must block a Discord
    fid booking attempt on the same slot -- previously `fid != ?` silently
    passed NULL-fid rows through, allowing a double-booking."""
    svs, users, alliance = _dbs()
    svs.execute(
        "INSERT INTO appointments (fid, manual_name, appointment_type, time, alliance) "
        "VALUES (NULL, 'Guest Governor', 'Chief Minister', '09:00', NULL)"
    )
    svs.commit()
    cog = _mk_menu_cog(svs, users, alliance)
    inter = _interaction()

    asyncio.run(cog.complete_booking(inter, "Chief Minister", "1", "09:00"))

    assert cog._last_message[1] is True, "must report a conflict, not silently overwrite the manual-name slot"
    assert "Guest Governor" in cog._last_message[0]
    # The manual-name row must still be there -- fid 1 must not have been inserted.
    row = svs.execute(
        "SELECT fid, manual_name FROM appointments WHERE appointment_type='Chief Minister' AND time='09:00'"
    ).fetchone()
    assert row == (None, "Guest Governor")

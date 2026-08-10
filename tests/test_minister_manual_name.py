"""
Portal-assigned slots have no registered fid -- they're stored as
(fid=NULL, manual_name=<text>). Every board-text generator must treat those
rows as "occupied", not silently skip them (a NULL fid is falsy, so it's
easy to get wrong).
"""
import importlib
import sqlite3

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



"""
After a portal save, the Discord board must show the same "who's booked
when" format as the /minister List command -- not the list-type-setting
slot list (which defaults to showing empty/available slots, not bookings).
"""
import asyncio
import importlib
import sqlite3
from types import SimpleNamespace

mm = importlib.import_module("cogs.minister_menu")


def _dbs():
    svs = sqlite3.connect(":memory:")
    svs.execute("""CREATE TABLE appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fid INTEGER, manual_name TEXT, appointment_type TEXT, time TEXT, alliance INTEGER)""")
    svs.commit()

    users = sqlite3.connect(":memory:")
    users.execute("CREATE TABLE users (fid INTEGER, nickname TEXT, alliance INTEGER)")
    users.execute("INSERT INTO users VALUES (1, 'Alice', 5)")
    users.commit()

    alliance = sqlite3.connect(":memory:")
    alliance.execute("CREATE TABLE alliance_list (alliance_id INTEGER, name TEXT)")
    alliance.execute("INSERT INTO alliance_list VALUES (5, 'TestAlli')")
    alliance.commit()
    return svs, users, alliance


def _mk_cog(svs, users, alliance):
    cog = mm.MinisterMenu.__new__(mm.MinisterMenu)
    cog.svs_conn = svs
    cog.svs_cursor = svs.cursor()
    cog.users_cursor = users.cursor()
    cog.alliance_cursor = alliance.cursor()
    return cog


def test_fetch_booking_lines_formats_fid_and_manual_name_rows():
    svs, users, alliance = _dbs()
    svs.execute("INSERT INTO appointments (fid, appointment_type, time, alliance) VALUES (1, 'Chief Minister', '10:00', 5)")
    svs.execute("INSERT INTO appointments (manual_name, appointment_type, time) VALUES ('Guest Governor', 'Chief Minister', '09:00')")
    svs.commit()
    cog = _mk_cog(svs, users, alliance)

    bookings, lines = cog._fetch_booking_lines("Chief Minister")

    assert len(bookings) == 2
    assert any("Guest Governor" in line for line in lines)
    assert any("Alice" in line and "TestAlli" in line for line in lines)


def test_update_channel_message_as_booking_list_posts_bookings_not_slots():
    """Regression guard for the actual bug: update_channel_message (the old
    call the portal used) shows all EMPTY slots by default (list_type=1) --
    the new method must show bookings regardless of that setting."""
    svs, users, alliance = _dbs()
    svs.execute("INSERT INTO appointments (fid, appointment_type, time, alliance) VALUES (1, 'Chief Minister', '10:00', 5)")
    svs.commit()
    cog = _mk_cog(svs, users, alliance)

    posted = {}

    async def get_channel_id(context):
        return 555 if context == "Chief Minister channel" else None

    async def get_log_guild(_guild):
        fake_channel = SimpleNamespace(id=555)
        return SimpleNamespace(get_channel=lambda cid: fake_channel if cid == 555 else None)

    async def get_or_create_message(context, message_content, channel):
        posted["context"] = context
        posted["content"] = message_content

    fake_schedule_cog = SimpleNamespace(
        get_channel_id=get_channel_id,
        get_log_guild=get_log_guild,
        get_or_create_message=get_or_create_message,
    )
    cog.bot = SimpleNamespace(get_cog=lambda name: fake_schedule_cog)

    asyncio.run(cog.update_channel_message_as_booking_list("Chief Minister"))

    assert "Alice" in posted["content"]
    assert "TestAlli" in posted["content"]
    assert "available" not in posted["content"].lower(), "must not fall back to the available-slots format"


def test_update_channel_message_as_booking_list_handles_no_bookings():
    svs, users, alliance = _dbs()
    cog = _mk_cog(svs, users, alliance)

    posted = {}

    async def get_channel_id(context):
        return 555

    async def get_log_guild(_guild):
        fake_channel = SimpleNamespace(id=555)
        return SimpleNamespace(get_channel=lambda cid: fake_channel)

    async def get_or_create_message(context, message_content, channel):
        posted["content"] = message_content

    fake_schedule_cog = SimpleNamespace(
        get_channel_id=get_channel_id,
        get_log_guild=get_log_guild,
        get_or_create_message=get_or_create_message,
    )
    cog.bot = SimpleNamespace(get_cog=lambda name: fake_schedule_cog)

    asyncio.run(cog.update_channel_message_as_booking_list("Chief Minister"))

    assert "No appointments currently booked" in posted["content"]

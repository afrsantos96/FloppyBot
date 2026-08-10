"""
The bot used to track three independent minister types (Construction Day,
Research Day, Troops Training Day). The game only has one Chief Minister
seat, so MinisterSchedule._migrate_legacy_appointment_types collapses all
three into a single "Chief Minister" schedule, including channel config.
"""
import importlib
import sqlite3

ms = importlib.import_module("cogs.minister_schedule")

LEGACY_TYPES = ("Construction Day", "Research Day", "Troops Training Day")


def _new_schema_conn():
    """A svs.sqlite already migrated to the surrogate-id appointments schema
    (i.e. past _migrate_appointments_table), still holding legacy-type rows."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fid INTEGER, manual_name TEXT, appointment_type TEXT, time TEXT, alliance INTEGER)""")
    conn.execute("CREATE TABLE reference (context TEXT PRIMARY KEY, context_id INTEGER)")
    conn.commit()
    return conn


def _mk_cog(conn):
    cog = ms.MinisterSchedule.__new__(ms.MinisterSchedule)
    cog.svs_conn = conn
    cog.svs_cursor = conn.cursor()
    return cog


def test_legacy_rows_renamed_to_chief_minister():
    conn = _new_schema_conn()
    conn.execute("INSERT INTO appointments (fid, appointment_type, time, alliance) VALUES (1, 'Construction Day', '10:00', 5)")
    conn.execute("INSERT INTO appointments (fid, appointment_type, time, alliance) VALUES (2, 'Research Day', '14:00', 5)")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_legacy_appointment_types()

    types = {row[0] for row in conn.execute("SELECT DISTINCT appointment_type FROM appointments").fetchall()}
    assert types == {"Chief Minister"}
    count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    assert count == 2, "both rows must survive, just renamed"


def test_no_op_when_nothing_legacy_present():
    conn = _new_schema_conn()
    conn.execute("INSERT INTO appointments (fid, appointment_type, time, alliance) VALUES (1, 'Chief Minister', '10:00', 5)")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_legacy_appointment_types()  # must not raise / must not touch anything

    row = conn.execute("SELECT appointment_type FROM appointments").fetchone()
    assert row == ("Chief Minister",)


def test_same_time_collision_across_legacy_types_keeps_one():
    """Construction Day 10:00 and Research Day 10:00 would both become
    Chief Minister 10:00 -- the unique (appointment_type, time) index means
    only one can survive. The migration must drop the extra, not crash."""
    conn = _new_schema_conn()
    conn.execute("CREATE UNIQUE INDEX idx_appt_type_time ON appointments(appointment_type, time)")
    conn.execute("INSERT INTO appointments (fid, appointment_type, time, alliance) VALUES (1, 'Construction Day', '10:00', 5)")
    conn.execute("INSERT INTO appointments (fid, appointment_type, time, alliance) VALUES (2, 'Research Day', '10:00', 5)")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_legacy_appointment_types()  # must not raise IntegrityError

    rows = conn.execute("SELECT fid, appointment_type, time FROM appointments").fetchall()
    assert rows == [(1, "Chief Minister", "10:00")], "the earlier row (lower id) must be kept"


def test_legacy_channels_consolidated_to_chief_minister_channel():
    conn = _new_schema_conn()
    conn.execute("INSERT INTO reference VALUES ('Construction Day channel', 555)")
    conn.execute("INSERT INTO reference VALUES ('Research Day channel', 555)")
    conn.execute("INSERT INTO reference VALUES ('Troops Training Day channel', 555)")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_legacy_appointment_types()

    row = conn.execute("SELECT context_id FROM reference WHERE context='Chief Minister channel'").fetchone()
    assert row == (555,)
    for legacy_type in LEGACY_TYPES:
        assert conn.execute(
            "SELECT 1 FROM reference WHERE context=?", (f"{legacy_type} channel",)
        ).fetchone() is None


def test_legacy_board_message_references_removed():
    """Board message ids were keyed by the bare activity name (e.g.
    'Construction Day' -> message_id) -- these must be cleared so a fresh
    'Chief Minister' board message gets created instead of reusing a stale id."""
    conn = _new_schema_conn()
    conn.execute("INSERT INTO reference VALUES ('Construction Day', 999888777)")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_legacy_appointment_types()

    assert conn.execute("SELECT 1 FROM reference WHERE context='Construction Day'").fetchone() is None


def test_migration_is_idempotent():
    conn = _new_schema_conn()
    conn.execute("INSERT INTO appointments (fid, appointment_type, time, alliance) VALUES (1, 'Construction Day', '10:00', 5)")
    conn.execute("INSERT INTO reference VALUES ('Research Day channel', 555)")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_legacy_appointment_types()
    cog._migrate_legacy_appointment_types()  # second run must be a safe no-op

    count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    assert count == 1
    row = conn.execute("SELECT context_id FROM reference WHERE context='Chief Minister channel'").fetchone()
    assert row == (555,)

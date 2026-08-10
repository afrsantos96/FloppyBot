"""
The merged single-minister schedule was originally called "Chief Minister",
now renamed to just "Appointment". MinisterSchedule._migrate_chief_minister_rename
does a one-time, idempotent rename of any existing live data still using the
old name -- a pure rename (no collision handling needed, unlike the earlier
3-legacy-types-into-one merge, since we're relabeling an already-unique value).
"""
import importlib
import sqlite3

ms = importlib.import_module("cogs.minister_schedule")


def _new_schema_conn():
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


def test_chief_minister_rows_renamed_to_appointment():
    conn = _new_schema_conn()
    conn.execute("INSERT INTO appointments (fid, appointment_type, time, alliance) VALUES (1, 'Chief Minister', '10:00', 5)")
    conn.execute("INSERT INTO appointments (manual_name, appointment_type, time) VALUES ('Guest', 'Chief Minister', '14:00')")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_chief_minister_rename()

    types = {row[0] for row in conn.execute("SELECT DISTINCT appointment_type FROM appointments").fetchall()}
    assert types == {"Appointment"}
    count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    assert count == 2, "both rows must survive, just renamed"


def test_no_op_when_already_appointment():
    conn = _new_schema_conn()
    conn.execute("INSERT INTO appointments (fid, appointment_type, time, alliance) VALUES (1, 'Appointment', '10:00', 5)")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_chief_minister_rename()  # must not raise / must not touch anything

    row = conn.execute("SELECT appointment_type FROM appointments").fetchone()
    assert row == ("Appointment",)


def test_no_op_when_nothing_present():
    conn = _new_schema_conn()
    cog = _mk_cog(conn)

    cog._migrate_chief_minister_rename()  # must not raise on an empty table

    count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    assert count == 0


def test_channel_reference_renamed():
    conn = _new_schema_conn()
    conn.execute("INSERT INTO reference VALUES ('Chief Minister channel', 555)")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_chief_minister_rename()

    row = conn.execute("SELECT context_id FROM reference WHERE context='Appointment channel'").fetchone()
    assert row == (555,)
    assert conn.execute("SELECT 1 FROM reference WHERE context='Chief Minister channel'").fetchone() is None


def test_board_message_reference_dropped_not_renamed():
    """Board message ids were keyed by the bare activity name -- must be
    cleared (not carried over) so a fresh 'Appointment' board message gets
    created instead of reusing a message id under the wrong key."""
    conn = _new_schema_conn()
    conn.execute("INSERT INTO reference VALUES ('Chief Minister', 999888777)")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_chief_minister_rename()

    assert conn.execute("SELECT 1 FROM reference WHERE context='Chief Minister'").fetchone() is None
    assert conn.execute("SELECT 1 FROM reference WHERE context='Appointment'").fetchone() is None


def test_archive_appointments_also_renamed_when_table_exists():
    conn = _new_schema_conn()
    conn.execute("""CREATE TABLE minister_archive_appointments (
        archive_id INTEGER NOT NULL, fid INTEGER, manual_name TEXT,
        appointment_type TEXT NOT NULL, time TEXT NOT NULL, alliance INTEGER, nickname TEXT NOT NULL)""")
    conn.execute("INSERT INTO minister_archive_appointments VALUES (1, 1, NULL, 'Chief Minister', '10:00', 5, 'Alice')")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_chief_minister_rename()

    row = conn.execute("SELECT appointment_type FROM minister_archive_appointments").fetchone()
    assert row == ("Appointment",)


def test_migration_is_idempotent():
    conn = _new_schema_conn()
    conn.execute("INSERT INTO appointments (fid, appointment_type, time, alliance) VALUES (1, 'Chief Minister', '10:00', 5)")
    conn.execute("INSERT INTO reference VALUES ('Chief Minister channel', 555)")
    conn.commit()
    cog = _mk_cog(conn)

    cog._migrate_chief_minister_rename()
    cog._migrate_chief_minister_rename()  # second run must be a safe no-op

    count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    assert count == 1
    row = conn.execute("SELECT context_id FROM reference WHERE context='Appointment channel'").fetchone()
    assert row == (555,)

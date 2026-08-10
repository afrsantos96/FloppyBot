"""
The `appointments` table gets rebuilt from the old (fid, appointment_type)
composite-PK schema to a surrogate id PK with a nullable manual_name column,
so a portal-assigned slot with no registered fid can be stored. sqlite can't
ALTER a PRIMARY KEY, so this is a copy/drop/rename done once at cog init.
Same shape of migration applies to minister_archive_appointments (fid was
NOT NULL there).
"""
import importlib
import sqlite3

ms = importlib.import_module("cogs.minister_schedule")
ma = importlib.import_module("cogs.minister_archive")


def _old_schema_svs_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE appointments (
        fid INTEGER, appointment_type TEXT, time TEXT, alliance INTEGER,
        PRIMARY KEY (fid, appointment_type))""")
    conn.execute("INSERT INTO appointments VALUES (1, 'Chief Minister', '10:00', 5)")
    conn.execute("INSERT INTO appointments VALUES (2, 'Chief Minister', '14:00', 5)")
    conn.commit()
    return conn


def _mk_schedule_cog(conn):
    cog = ms.MinisterSchedule.__new__(ms.MinisterSchedule)
    cog.svs_conn = conn
    cog.svs_cursor = conn.cursor()
    return cog


def test_migration_adds_id_and_manual_name_columns():
    conn = _old_schema_svs_conn()
    cog = _mk_schedule_cog(conn)

    cog._migrate_appointments_table()

    cols = {row[1] for row in conn.execute("PRAGMA table_info(appointments)").fetchall()}
    assert "id" in cols
    assert "manual_name" in cols


def test_migration_preserves_existing_rows():
    conn = _old_schema_svs_conn()
    cog = _mk_schedule_cog(conn)

    cog._migrate_appointments_table()

    rows = conn.execute(
        "SELECT fid, manual_name, appointment_type, time, alliance FROM appointments ORDER BY fid"
    ).fetchall()
    assert rows == [
        (1, None, "Chief Minister", "10:00", 5),
        (2, None, "Chief Minister", "14:00", 5),
    ]


def test_migration_is_idempotent():
    conn = _old_schema_svs_conn()
    cog = _mk_schedule_cog(conn)

    cog._migrate_appointments_table()
    cog._migrate_appointments_table()  # must be a no-op the second time

    count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    assert count == 2


def test_migrated_table_allows_manual_name_row_alongside_fid_rows():
    conn = _old_schema_svs_conn()
    cog = _mk_schedule_cog(conn)
    cog._migrate_appointments_table()

    conn.execute(
        "INSERT INTO appointments (fid, manual_name, appointment_type, time, alliance) "
        "VALUES (NULL, 'Guest Governor', 'Chief Minister', '09:00', NULL)"
    )
    conn.commit()

    row = conn.execute(
        "SELECT manual_name FROM appointments WHERE appointment_type='Chief Minister' AND time='09:00'"
    ).fetchone()
    assert row == ("Guest Governor",)


def test_migrated_table_rejects_double_booking_same_slot():
    """The (appointment_type, time) unique index must catch two different
    identities landing on the same slot, regardless of fid vs manual_name."""
    conn = _old_schema_svs_conn()
    cog = _mk_schedule_cog(conn)
    cog._migrate_appointments_table()

    conn.execute(
        "INSERT INTO appointments (fid, manual_name, appointment_type, time, alliance) "
        "VALUES (NULL, 'Guest A', 'Chief Minister', '09:00', NULL)"
    )
    conn.commit()

    try:
        conn.execute(
            "INSERT INTO appointments (fid, manual_name, appointment_type, time, alliance) "
            "VALUES (NULL, 'Guest B', 'Chief Minister', '09:00', NULL)"
        )
        conn.commit()
        assert False, "expected a unique constraint violation on (appointment_type, time)"
    except sqlite3.IntegrityError:
        pass


def test_migrated_table_allows_two_different_manual_names_on_different_slots():
    conn = _old_schema_svs_conn()
    cog = _mk_schedule_cog(conn)
    cog._migrate_appointments_table()

    conn.execute(
        "INSERT INTO appointments (fid, manual_name, appointment_type, time, alliance) "
        "VALUES (NULL, 'Guest A', 'Chief Minister', '09:00', NULL)"
    )
    conn.execute(
        "INSERT INTO appointments (fid, manual_name, appointment_type, time, alliance) "
        "VALUES (NULL, 'Guest B', 'Chief Minister', '09:30', NULL)"
    )
    conn.commit()

    count = conn.execute(
        "SELECT COUNT(*) FROM appointments WHERE appointment_type='Chief Minister' AND manual_name IS NOT NULL"
    ).fetchone()[0]
    assert count == 2


def _old_schema_archive_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE minister_archives (
        archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
        archive_name TEXT NOT NULL, created_at TIMESTAMP NOT NULL,
        created_by_id INTEGER NOT NULL, created_by_name TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE minister_archive_appointments (
        archive_id INTEGER NOT NULL, fid INTEGER NOT NULL,
        appointment_type TEXT NOT NULL, time TEXT NOT NULL,
        alliance INTEGER NOT NULL, nickname TEXT NOT NULL,
        FOREIGN KEY (archive_id) REFERENCES minister_archives(archive_id))""")
    conn.execute("INSERT INTO minister_archives VALUES (1, 'Week 1', '2026-01-01T00:00:00', 10, 'Admin')")
    conn.execute("INSERT INTO minister_archive_appointments VALUES (1, 1, 'Chief Minister', '10:00', 5, 'Alice')")
    conn.commit()
    return conn


def _mk_archive_cog(conn):
    cog = ma.MinisterArchive.__new__(ma.MinisterArchive)
    cog.svs_conn = conn
    cog.svs_cursor = conn.cursor()
    return cog


def test_archive_migration_adds_manual_name_and_relaxes_fid():
    conn = _old_schema_archive_conn()
    cog = _mk_archive_cog(conn)

    cog._migrate_archive_appointments_table()

    cols = {row[1] for row in conn.execute("PRAGMA table_info(minister_archive_appointments)").fetchall()}
    assert "manual_name" in cols

    # fid must now accept NULL (a manual/guest archived row)
    conn.execute(
        "INSERT INTO minister_archive_appointments (archive_id, fid, manual_name, appointment_type, time, alliance, nickname) "
        "VALUES (1, NULL, 'Guest Governor', 'Chief Minister', '11:00', NULL, 'Guest Governor')"
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM minister_archive_appointments").fetchone()[0]
    assert count == 2


def test_archive_migration_preserves_existing_row():
    conn = _old_schema_archive_conn()
    cog = _mk_archive_cog(conn)

    cog._migrate_archive_appointments_table()

    row = conn.execute(
        "SELECT archive_id, fid, manual_name, appointment_type, time, alliance, nickname "
        "FROM minister_archive_appointments"
    ).fetchone()
    assert row == (1, 1, None, "Chief Minister", "10:00", 5, "Alice")


def test_archive_migration_is_idempotent():
    conn = _old_schema_archive_conn()
    cog = _mk_archive_cog(conn)

    cog._migrate_archive_appointments_table()
    cog._migrate_archive_appointments_table()

    count = conn.execute("SELECT COUNT(*) FROM minister_archive_appointments").fetchone()[0]
    assert count == 1

"""
Synchronous DB access for the minister portal. Every function here opens its
own short-lived sqlite3 connection (matching PermissionManager's pattern)
rather than reaching into a cog's shared cursor -- these functions are meant
to be called via asyncio.to_thread from a worker thread, and sharing a cog's
cursor across threads while the bot's own event loop concurrently uses it
from Discord interactions would be unsafe.

Discord-flavored side effects (audit log entries via MinisterSchedule.log_change,
board message refresh via MinisterMenu.update_channel_message) are NOT done
here -- those need live discord.py objects and stay on the main event loop,
called by web/routes.py after these functions return.
"""
import sqlite3
import logging
from typing import Optional

from cogs.permission_handler import PermissionManager

logger = logging.getLogger('bot')

SVS_DB = "db/svs.sqlite"
USERS_DB = "db/users.sqlite"
ALLIANCE_DB = "db/alliance.sqlite"

POSITIONS = ["Appointment"]


def get_time_slots(slot_mode: int):
    """Pure duplicate of MinisterSchedule.get_time_slots -- intentionally not
    calling the cog method here since this module runs off the main thread."""
    time_slots = []
    if slot_mode == 0:
        for hour in range(24):
            for minute in (0, 30):
                time_slots.append(f"{hour:02}:{minute:02}")
    else:
        time_slots.append("00:00")
        for hour in range(24):
            for minute in (15, 45):
                if hour == 23 and minute == 45:
                    time_slots.append("23:45")
                    break
                time_slots.append(f"{hour:02}:{minute:02}")
    return time_slots


def _get_slot_mode(svs: sqlite3.Connection) -> int:
    row = svs.execute("SELECT context_id FROM reference WHERE context=?", ("slot_mode",)).fetchone()
    return int(row[0]) if row else 0


def fetch_schedule_snapshot(discord_user_id: int, guild_id: Optional[int]) -> Optional[dict]:
    """Returns the full portal payload, or None if the caller isn't an admin."""
    is_admin, is_global = PermissionManager.is_admin(discord_user_id)
    if not is_admin:
        return None

    members = PermissionManager.get_admin_users(discord_user_id, guild_id)  # [(fid, nickname, alliance)]

    with sqlite3.connect(SVS_DB, timeout=30.0) as svs:
        slot_mode = _get_slot_mode(svs)
        appointments = {}
        for position in POSITIONS:
            rows = svs.execute(
                "SELECT time, fid, manual_name, alliance FROM appointments WHERE appointment_type=?",
                (position,),
            ).fetchall()
            appointments[position] = {
                time_slot: {"fid": fid, "manual_name": manual_name, "alliance": alliance}
                for time_slot, fid, manual_name, alliance in rows
            }

    with sqlite3.connect(USERS_DB) as users_db, sqlite3.connect(ALLIANCE_DB) as alliance_db:
        alliance_name_cache: dict = {}

        def alliance_name(alliance_id):
            if alliance_id is None:
                return None
            if alliance_id not in alliance_name_cache:
                row = alliance_db.execute(
                    "SELECT name FROM alliance_list WHERE alliance_id=?", (alliance_id,)
                ).fetchone()
                alliance_name_cache[alliance_id] = row[0] if row else "Unknown"
            return alliance_name_cache[alliance_id]

        for position, slots in appointments.items():
            for time_slot, entry in slots.items():
                if entry["fid"]:
                    row = users_db.execute(
                        "SELECT nickname FROM users WHERE fid=?", (entry["fid"],)
                    ).fetchone()
                    entry["nickname"] = row[0] if row else f"ID: {entry['fid']}"
                    entry["alliance_name"] = alliance_name(entry["alliance"])
                else:
                    entry["nickname"] = entry["manual_name"]
                    entry["alliance_name"] = None

        member_list = [
            {
                "fid": fid,
                "nickname": nickname,
                "alliance": alliance,
                "alliance_name": alliance_name(alliance),
            }
            for fid, nickname, alliance in members
        ]

    return {
        "positions": POSITIONS,
        "slot_mode": slot_mode,
        "time_slots": {p: get_time_slots(slot_mode) for p in POSITIONS},
        "appointments": appointments,
        "members": member_list,
        "is_global": is_global,
    }


def apply_schedule_changes(changes: list, discord_user_id: int, guild_id: Optional[int]) -> dict:
    """
    Writes a batch of slot assignments. Each change is:
      {"appointment_type": str, "time": "HH:MM", "fid": int}          -- assign a registered member
      {"appointment_type": str, "time": "HH:MM", "manual_name": str}  -- assign a manual/guest name
      {"appointment_type": str, "time": "HH:MM", "clear": true}       -- empty the slot

    Semantics: the portal is a grid editor (set-the-cell), not a booking
    queue, so a valid change always overwrites whatever previously occupied
    that slot -- "conflicts" here means the change itself was invalid or not
    permitted (bad position/time, fid outside the caller's alliance scope),
    not "someone else already has this slot". Assigning an fid/manual_name
    that already holds a *different* slot for the same appointment_type
    moves it (matches the existing Discord reschedule behavior).

    Returns {"applied": [...], "conflicts": [...], "touched_types": [...]}.
    """
    is_admin, is_global = PermissionManager.is_admin(discord_user_id)
    if not is_admin:
        return {"error": "forbidden"}

    scoped_members = {
        fid: (nickname, alliance)
        for fid, nickname, alliance in PermissionManager.get_admin_users(discord_user_id, guild_id)
    }

    applied = []
    conflicts = []
    touched_types = set()

    conn = sqlite3.connect(SVS_DB, timeout=30.0)
    try:
        cur = conn.cursor()
        slot_mode = _get_slot_mode(conn)
        valid_times = set(get_time_slots(slot_mode))

        for change in changes:
            appointment_type = change.get("appointment_type")
            time_slot = change.get("time")

            if appointment_type not in POSITIONS:
                conflicts.append({"appointment_type": appointment_type, "time": time_slot, "reason": "invalid_position"})
                continue
            if time_slot not in valid_times:
                conflicts.append({"appointment_type": appointment_type, "time": time_slot, "reason": "invalid_time"})
                continue

            is_clear = bool(change.get("clear"))
            fid = None
            manual_name = None

            if not is_clear:
                # A clear ignores any fid/manual_name the request also sent --
                # otherwise the reschedule cleanup below (which frees any other
                # slot this identity holds) would delete an unrelated booking.
                raw_fid = change.get("fid")
                manual_name = (change.get("manual_name") or "").strip() or None
                if manual_name and len(manual_name) > 100:
                    manual_name = manual_name[:100]

                if raw_fid not in (None, ""):
                    try:
                        fid = int(raw_fid)
                    except (TypeError, ValueError):
                        conflicts.append({"appointment_type": appointment_type, "time": time_slot, "reason": "invalid_fid"})
                        continue

            if not is_clear and not fid and not manual_name:
                conflicts.append({"appointment_type": appointment_type, "time": time_slot, "reason": "no_assignee"})
                continue

            nickname_for_log = None
            alliance_for_row = None
            if fid:
                if fid not in scoped_members:
                    conflicts.append({"appointment_type": appointment_type, "time": time_slot, "reason": "fid_not_permitted"})
                    continue
                nickname_for_log, alliance_for_row = scoped_members[fid]

            # Capture the slot's current occupant (for logging + old_time)
            cur.execute(
                "SELECT fid, manual_name FROM appointments WHERE appointment_type=? AND time=?",
                (appointment_type, time_slot),
            )
            prior_at_slot = cur.fetchone()

            # Free up the slot and, when assigning, any other slot this same
            # identity already held for this appointment_type (reschedule).
            cur.execute(
                "DELETE FROM appointments WHERE appointment_type=? AND time=?",
                (appointment_type, time_slot),
            )
            old_time = None
            if fid:
                cur.execute(
                    "SELECT time FROM appointments WHERE appointment_type=? AND fid=?",
                    (appointment_type, fid),
                )
                row = cur.fetchone()
                if row:
                    old_time = row[0]
                    cur.execute(
                        "DELETE FROM appointments WHERE appointment_type=? AND fid=?",
                        (appointment_type, fid),
                    )
            elif manual_name:
                cur.execute(
                    "SELECT time FROM appointments WHERE appointment_type=? AND manual_name=?",
                    (appointment_type, manual_name),
                )
                row = cur.fetchone()
                if row:
                    old_time = row[0]
                    cur.execute(
                        "DELETE FROM appointments WHERE appointment_type=? AND manual_name=?",
                        (appointment_type, manual_name),
                    )

            if not is_clear:
                cur.execute(
                    "INSERT INTO appointments (fid, manual_name, appointment_type, time, alliance) VALUES (?, ?, ?, ?, ?)",
                    (fid, manual_name, appointment_type, time_slot, alliance_for_row),
                )
                action = "reschedule" if old_time and old_time != time_slot else "add"
            else:
                if not prior_at_slot:
                    continue  # clearing an already-empty slot is a no-op
                action = "remove"
                prior_fid, prior_manual = prior_at_slot
                fid = prior_fid
                manual_name = prior_manual
                if prior_fid:
                    with sqlite3.connect(USERS_DB) as users_db:
                        row = users_db.execute(
                            "SELECT nickname FROM users WHERE fid=?", (prior_fid,)
                        ).fetchone()
                    nickname_for_log = row[0] if row else f"ID: {prior_fid}"
                else:
                    nickname_for_log = prior_manual

            touched_types.add(appointment_type)
            applied.append({
                "appointment_type": appointment_type,
                "time": time_slot,
                "fid": fid,
                "manual_name": manual_name,
                "nickname": nickname_for_log,
                "old_time": old_time,
                "action": action,
            })

        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Portal schedule save failed, rolled back: {e}")
        return {"error": "internal_error"}
    finally:
        conn.close()

    return {"applied": applied, "conflicts": conflicts, "touched_types": sorted(touched_types)}

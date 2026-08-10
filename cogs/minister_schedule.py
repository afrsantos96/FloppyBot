"""
Minister rotation logic. Handles scheduling, swaps, and automatic role assignments.
"""
import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import sqlite3
import logging
import re
from datetime import datetime
from .pimp_my_bot import theme

logger = logging.getLogger('bot')

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    ARABIC_SUPPORT = True
except ImportError:
    ARABIC_SUPPORT = False


class ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, bot, context: str):
        self.bot = bot
        self.context = context

        super().__init__(
            placeholder="Select a channel...",
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.private,
                discord.ChannelType.news,
                discord.ChannelType.forum,
                discord.ChannelType.news_thread,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
                discord.ChannelType.stage_voice
            ],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        selected_channel = self.values[0]
        channel_id = selected_channel.id

        svs_conn = sqlite3.connect("db/svs.sqlite")
        svs_cursor = svs_conn.cursor()
        try:
            # Check if we're updating a minister channel
            if self.context.endswith("channel"):
                # Get the activity name from the context (e.g., "Appointment channel" -> "Appointment")
                activity_name = self.context.replace(" channel", "")

                # Check if this is a minister activity channel (Appointment or Auto Schedule)
                if activity_name in ("Appointment", "Auto Schedule"):
                    # Get the old channel ID if it exists
                    svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", (self.context,))
                    old_channel_row = svs_cursor.fetchone()
                    
                    if old_channel_row:
                        old_channel_id = int(old_channel_row[0])
                        # Get the message ID for this activity
                        svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", (activity_name,))
                        message_row = svs_cursor.fetchone()
                        
                        if message_row and old_channel_id != channel_id:
                            # Delete the old message if channel has changed
                            message_id = int(message_row[0])
                            guild = interaction.guild
                            if guild:
                                old_channel = guild.get_channel(old_channel_id)
                                if old_channel:
                                    try:
                                        old_message = await old_channel.fetch_message(message_id)
                                        await old_message.delete()
                                    except Exception:
                                        pass  # Message might already be deleted
                            
                            # Remove the message reference so it will be recreated in the new channel
                            svs_cursor.execute("DELETE FROM reference WHERE context=?", (activity_name,))
            
            # Update the channel reference
            svs_cursor.execute("""
                INSERT INTO reference (context, context_id)
                VALUES (?, ?)
                ON CONFLICT(context) DO UPDATE SET context_id = excluded.context_id;
            """, (self.context, channel_id))
            svs_conn.commit()
            
            # Trigger message update in the new channel
            if self.context.endswith("channel"):
                activity_name = self.context.replace(" channel", "")
                if activity_name == "Appointment":
                    minister_menu_cog = self.bot.get_cog("MinisterMenu")
                    if minister_menu_cog:
                        await minister_menu_cog.update_channel_message(activity_name)
                elif activity_name == "Auto Schedule":
                    minister_menu_cog = self.bot.get_cog("MinisterMenu")
                    if minister_menu_cog:
                        await minister_menu_cog.update_channel_message_as_booking_list(activity_name)

            # Check if this is being called from the minister menu system
            minister_menu_cog = self.bot.get_cog("MinisterMenu")
            if minister_menu_cog and self.context.endswith("channel"):
                # Return to channel configuration menu with confirmation
                embed = discord.Embed(
                    title=f"{theme.editListIcon} Channel Setup",
                    description=(
                        f"{theme.verifiedIcon} **{self.context}** set to <#{channel_id}>\n\n"
                        f"Configure channels for minister scheduling:\n\n"
                        f"**Channel Types**\n"
                        f"{theme.upperDivider}\n"
                        f"{theme.settingsIcon} **Appointment Channel** - Shows the Appointment schedule\n"
                        f"{theme.robotIcon} **Auto Schedule Channel** - Where governors post requests and the AI-generated schedule is shown\n"
                        f"{theme.documentIcon} **Log Channel** - Receives add/remove notifications\n"
                        f"{theme.lowerDivider}\n\n"
                        f"Select a channel type to configure:"
                    ),
                    color=theme.emColor3
                )

                # Get the ChannelConfigurationView from minister_menu
                import sys
                minister_menu_module = minister_menu_cog.__class__.__module__
                ChannelConfigurationView = getattr(sys.modules[minister_menu_module], 'ChannelConfigurationView')
                
                view = ChannelConfigurationView(self.bot, minister_menu_cog)
                
                await interaction.response.edit_message(
                    content=None, # Clear the "Select a channel for..." content
                    embed=embed,
                    view=view
                )
            else:
                # Fallback for other contexts
                await interaction.response.edit_message(
                    content=f"{theme.verifiedIcon} `{self.context}` set to <#{channel_id}>.\n\nChannel configured successfully!",
                    view=None
                )

        except Exception as e:
            try:
                await interaction.response.send_message(
                    f"{theme.deniedIcon} Failed to update:\n```{e}```",
                    ephemeral=True
                )
            except discord.InteractionResponded:
                await interaction.followup.send(
                    f"{theme.deniedIcon} Failed to update:\n```{e}```",
                    ephemeral=True
                )
        finally:
            svs_conn.close()

class MinisterSchedule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.users_conn = sqlite3.connect('db/users.sqlite', timeout=30.0, check_same_thread=False)
        self.users_cursor = self.users_conn.cursor()
        self.settings_conn = sqlite3.connect('db/settings.sqlite', timeout=30.0, check_same_thread=False)
        self.settings_cursor = self.settings_conn.cursor()
        self.alliance_conn = sqlite3.connect('db/alliance.sqlite', timeout=30.0, check_same_thread=False)
        self.alliance_cursor = self.alliance_conn.cursor()
        self.svs_conn = sqlite3.connect("db/svs.sqlite", timeout=30.0, check_same_thread=False)
        self.svs_cursor = self.svs_conn.cursor()

        # Enable WAL mode for better concurrent access
        self.users_conn.execute("PRAGMA journal_mode=WAL")
        self.users_conn.execute("PRAGMA synchronous=NORMAL")
        self.settings_conn.execute("PRAGMA journal_mode=WAL")
        self.settings_conn.execute("PRAGMA synchronous=NORMAL")
        self.alliance_conn.execute("PRAGMA journal_mode=WAL")
        self.alliance_conn.execute("PRAGMA synchronous=NORMAL")
        self.svs_conn.execute("PRAGMA journal_mode=WAL")
        self.svs_conn.execute("PRAGMA synchronous=NORMAL")

        self.svs_cursor.execute("""
                    CREATE TABLE IF NOT EXISTS appointments (
                        fid INTEGER,
                        appointment_type TEXT,
                        time TEXT,
                        alliance INTEGER,
                        PRIMARY KEY (fid, appointment_type)
                    );
                """)
        self._migrate_appointments_table()
        self.svs_cursor.execute("""
                    CREATE TABLE IF NOT EXISTS reference (
                        context TEXT PRIMARY KEY,
                        context_id INTEGER
                    );
                """)
        self.svs_cursor.execute("""
            INSERT OR IGNORE INTO reference (context, context_id)
            VALUES ('list type', 1);
        """)
        self.svs_cursor.execute("""
            INSERT OR IGNORE INTO reference (context, context_id)
            VALUES ('slot_mode', 0);
        """)
        self.svs_cursor.execute("""
            CREATE TABLE IF NOT EXISTS portal_tokens (
                jti TEXT PRIMARY KEY,
                discord_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                consumed_at TEXT
            );
        """)

        self.svs_conn.commit()
        self._migrate_legacy_appointment_types()
        self._migrate_chief_minister_rename()

    def _migrate_chief_minister_rename(self):
        """One-time rename: the merged single-minister schedule was originally
        called "Chief Minister", now just "Appointment". Idempotent -- only
        touches rows/reference keys that still say the old name."""
        OLD_NAME = "Chief Minister"
        NEW_NAME = "Appointment"

        archive_table_exists = self.svs_cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='minister_archive_appointments'"
        ).fetchone() is not None

        legacy_rows = self.svs_cursor.execute(
            "SELECT COUNT(*) FROM appointments WHERE appointment_type=?", (OLD_NAME,)
        ).fetchone()[0]
        legacy_reference_keys = self.svs_cursor.execute(
            "SELECT COUNT(*) FROM reference WHERE context IN (?, ?)",
            (OLD_NAME, f"{OLD_NAME} channel")
        ).fetchone()[0]
        legacy_archive_rows = 0
        if archive_table_exists:
            legacy_archive_rows = self.svs_cursor.execute(
                "SELECT COUNT(*) FROM minister_archive_appointments WHERE appointment_type=?", (OLD_NAME,)
            ).fetchone()[0]
        if not legacy_rows and not legacy_reference_keys and not legacy_archive_rows:
            return  # nothing to migrate

        try:
            # Pure rename of an already-unique type value -- no collision
            # possible, unlike the legacy 3-type merge above.
            self.svs_cursor.execute(
                "UPDATE appointments SET appointment_type=? WHERE appointment_type=?",
                (NEW_NAME, OLD_NAME)
            )

            if archive_table_exists:
                self.svs_cursor.execute(
                    "UPDATE minister_archive_appointments SET appointment_type=? WHERE appointment_type=?",
                    (NEW_NAME, OLD_NAME)
                )

            # Channel reference: rename "Chief Minister channel" -> "Appointment channel".
            row = self.svs_cursor.execute(
                "SELECT context_id FROM reference WHERE context=?", (f"{OLD_NAME} channel",)
            ).fetchone()
            if row:
                self.svs_cursor.execute(
                    "INSERT INTO reference (context, context_id) VALUES (?, ?) "
                    "ON CONFLICT(context) DO UPDATE SET context_id = excluded.context_id",
                    (f"{NEW_NAME} channel", row[0])
                )
                self.svs_cursor.execute("DELETE FROM reference WHERE context=?", (f"{OLD_NAME} channel",))

            # Board message reference (bare name key) is intentionally just
            # dropped, not renamed -- a fresh "Appointment" board message
            # gets created under the new key next time the board refreshes.
            self.svs_cursor.execute("DELETE FROM reference WHERE context=?", (OLD_NAME,))

            self.svs_conn.commit()
            logger.info(f"Migrated '{OLD_NAME}' appointment type to '{NEW_NAME}'")
        except sqlite3.Error as e:
            self.svs_conn.rollback()
            logger.error(f"Failed to migrate '{OLD_NAME}' rename: {e}")
            raise

    def _migrate_legacy_appointment_types(self):
        """One-time migration: the bot used to track three independent minister
        types (Construction/Research/Troops Training Day), but the game only has
        one Appointment seat, so these are now a single merged schedule.
        Idempotent -- a checked-for legacy row/channel is what gates a re-run.
        """
        LEGACY_TYPES = ("Construction Day", "Research Day", "Troops Training Day")

        legacy_rows = self.svs_cursor.execute(
            "SELECT COUNT(*) FROM appointments WHERE appointment_type IN (?, ?, ?)", LEGACY_TYPES
        ).fetchone()[0]
        legacy_channels = self.svs_cursor.execute(
            "SELECT COUNT(*) FROM reference WHERE context IN (?, ?, ?)",
            tuple(f"{t} channel" for t in LEGACY_TYPES)
        ).fetchone()[0]
        legacy_messages = self.svs_cursor.execute(
            "SELECT COUNT(*) FROM reference WHERE context IN (?, ?, ?)", LEGACY_TYPES
        ).fetchone()[0]
        if not legacy_rows and not legacy_channels and not legacy_messages:
            return  # nothing to migrate

        try:
            # Three separate unique indexes apply once every legacy row becomes
            # "Appointment": (appointment_type, time) -- two legacy rows at the
            # same time collide; (fid, appointment_type) -- the same member holding
            # both e.g. a Construction Day slot and a Troops Training Day slot
            # collides, since one person can only hold one Appointment seat;
            # (manual_name, appointment_type) -- same idea for portal-assigned
            # guest names. Keep the earliest row (by id) for each, drop the rest --
            # rare, but must not crash the merge.
            rows = self.svs_cursor.execute(
                "SELECT id, time, fid, manual_name FROM appointments WHERE appointment_type IN (?, ?, ?) ORDER BY id",
                LEGACY_TYPES
            ).fetchall()
            seen_times, seen_fids, seen_manual_names = set(), set(), set()
            for row_id, time_slot, fid, manual_name in rows:
                collides = (
                    time_slot in seen_times
                    or (fid is not None and fid in seen_fids)
                    or (manual_name is not None and manual_name in seen_manual_names)
                )
                if collides:
                    logger.warning(
                        f"Dropping appointment id={row_id} (fid={fid}, manual_name={manual_name!r}, "
                        f"time={time_slot}): collides with another legacy-type booking during the "
                        f"Appointment merge"
                    )
                    self.svs_cursor.execute("DELETE FROM appointments WHERE id=?", (row_id,))
                else:
                    seen_times.add(time_slot)
                    if fid is not None:
                        seen_fids.add(fid)
                    if manual_name is not None:
                        seen_manual_names.add(manual_name)

            self.svs_cursor.execute(
                "UPDATE appointments SET appointment_type='Appointment' WHERE appointment_type IN (?, ?, ?)",
                LEGACY_TYPES
            )

            archive_table_exists = self.svs_cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='minister_archive_appointments'"
            ).fetchone() is not None
            if archive_table_exists:
                self.svs_cursor.execute(
                    "UPDATE minister_archive_appointments SET appointment_type='Appointment' "
                    "WHERE appointment_type IN (?, ?, ?)",
                    LEGACY_TYPES
                )

            # Consolidate channel config: keep the first configured legacy channel,
            # and drop the per-type board-message references (they'll be recreated).
            for legacy_type in LEGACY_TYPES:
                row = self.svs_cursor.execute(
                    "SELECT context_id FROM reference WHERE context=?", (f"{legacy_type} channel",)
                ).fetchone()
                if row:
                    self.svs_cursor.execute(
                        "INSERT INTO reference (context, context_id) VALUES ('Appointment channel', ?) "
                        "ON CONFLICT(context) DO NOTHING",
                        (row[0],)
                    )
                self.svs_cursor.execute("DELETE FROM reference WHERE context=?", (f"{legacy_type} channel",))
                self.svs_cursor.execute("DELETE FROM reference WHERE context=?", (legacy_type,))

            self.svs_conn.commit()
            logger.info("Migrated legacy Construction/Research/Troops Training Day minister types to a single Appointment schedule")
        except sqlite3.Error as e:
            self.svs_conn.rollback()
            logger.error(f"Failed to migrate legacy minister appointment types: {e}")
            raise

    def _migrate_appointments_table(self):
        """One-time rebuild of `appointments` to add a surrogate id PK and a
        nullable manual_name column (for portal-assigned slots that have no
        registered fid). sqlite can't ALTER a PRIMARY KEY, so this rebuilds
        the table the first time it's missing the `id` column.
        """
        cols = [row[1] for row in self.svs_cursor.execute("PRAGMA table_info(appointments)").fetchall()]
        if "id" in cols:
            return  # already migrated

        try:
            self.svs_cursor.execute("""
                CREATE TABLE appointments_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fid INTEGER,
                    manual_name TEXT,
                    appointment_type TEXT NOT NULL,
                    time TEXT NOT NULL,
                    alliance INTEGER
                );
            """)
            self.svs_cursor.execute("""
                INSERT INTO appointments_new (fid, manual_name, appointment_type, time, alliance)
                SELECT fid, NULL, appointment_type, time, alliance FROM appointments;
            """)
            self.svs_cursor.execute("DROP TABLE appointments;")
            self.svs_cursor.execute("ALTER TABLE appointments_new RENAME TO appointments;")
            self.svs_cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_appt_fid_type ON appointments(fid, appointment_type) WHERE fid IS NOT NULL;"
            )
            self.svs_cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_appt_manual_type ON appointments(manual_name, appointment_type) WHERE manual_name IS NOT NULL;"
            )
            self.svs_cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_appt_type_time ON appointments(appointment_type, time);"
            )
            self.svs_conn.commit()
            logger.info("Migrated appointments table to surrogate id PK + manual_name column")
        except sqlite3.Error as e:
            self.svs_conn.rollback()
            logger.error(f"Failed to migrate appointments table: {e}")
            raise

    async def cog_unload(self):
        """Close database connections when cog is unloaded."""
        try:
            self.users_conn.close()
            self.settings_conn.close()
            self.alliance_conn.close()
            self.svs_conn.close()
        except Exception:
            pass

    async def send_embed_to_channel(self, embed):
        """Sends the embed message to a specific channel."""
        log_channel_id = await self.get_channel_id("minister log channel")
        log_channel = self.bot.get_channel(log_channel_id)

        if log_channel:
            await log_channel.send(embed=embed)
        else:
            logger.error("Could not find the log channel please change it to a valid channel")
            print(f"Error: Could not find the log channel please change it to a valid channel")

    async def is_admin(self, user_id: int) -> bool:
        if user_id == self.bot.owner_id:
            return True
        self.settings_cursor.execute("SELECT 1 FROM admin WHERE id=?", (user_id,))
        return self.settings_cursor.fetchone() is not None

    async def log_change(self, action_type: str, user, appointment_type: str = None, fid: int = None,
                        nickname: str = None, old_time: str = None, new_time: str = None,
                        alliance_name: str = None, additional_data: str = None, archive_id: int = None):
        """
        Log a change to the minister change history table.

        Args:
            action_type: Type of action (add, remove, reschedule, clear_all, time_slot_mode_change, archive_created)
            user: Discord user object who made the change
            appointment_type: Always "Appointment" now (single merged schedule)
            fid: User FID
            nickname: User nickname
            old_time: Previous time slot (for reschedule)
            new_time: New time slot (for add/reschedule)
            alliance_name: Alliance name
            additional_data: JSON string with extra context
            archive_id: Archive ID if this change is associated with an archive
        """
        try:
            timestamp = datetime.now().isoformat()
            discord_user_id = user.id
            discord_username = user.display_name

            self.svs_cursor.execute("""
                INSERT INTO minister_change_history
                (archive_id, timestamp, discord_user_id, discord_username, action_type,
                 appointment_type, fid, nickname, old_time, new_time, alliance_name, additional_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (archive_id, timestamp, discord_user_id, discord_username, action_type,
                  appointment_type, fid, nickname, old_time, new_time, alliance_name, additional_data))

            self.svs_conn.commit()
        except Exception as e:
            logger.error(f"Error logging change: {e}")
            print(f"Error logging change: {e}")

    def fix_arabic(self, text):
        """
        Fix Arabic text rendering by reshaping and applying bidirectional algorithm.
        """
        if not text or not ARABIC_SUPPORT:
            return text

        # Check if text contains Arabic characters
        if re.search(r'[\u0600-\u06FF]', text):
            try:
                reshaped = arabic_reshaper.reshape(text)
                return get_display(reshaped)
            except Exception:
                return text
        return text

    def get_time_slots(self, slot_mode: int):
        """
        Generate time slots based on the slot mode.

        Mode 0 (Standard): 00:00, 00:30, 01:00, ..., 23:30 (48 slots × 30min)
        Mode 1 (Offset): 00:00 (15min), 00:15, 00:45, 01:15, ..., 23:45 (15min to midnight)

        Returns: List of time strings in HH:MM format
        """
        time_slots = []

        if slot_mode == 0:
            # Standard mode: 30-minute intervals starting at 00:00
            for hour in range(24):
                for minute in (0, 30):
                    time_slots.append(f"{hour:02}:{minute:02}")
        else:
            # Offset mode: First slot at 00:00 (15min), then 30min slots at :15 and :45
            time_slots.append("00:00")  # First slot: 00:00-00:15
            for hour in range(24):
                for minute in (15, 45):
                    if hour == 23 and minute == 45:
                        time_slots.append("23:45")  # Last slot: 23:45-00:00
                        break
                    time_slots.append(f"{hour:02}:{minute:02}")

        return time_slots

    # Autocomplete handler for choices of what to show
    async def choice_autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            choices = [
                discord.app_commands.Choice(name="Show full minister list", value="all"),
                discord.app_commands.Choice(name="Show available slots only", value="available only")
            ]

            if current:
                filtered_choices = [choice for choice in choices if current.lower() in choice.name.lower()]
            else:
                filtered_choices = choices

            return filtered_choices
        except Exception as e:
            logger.error(f"Error in all_or_available autocomplete: {e}")
            print(f"Error in all_or_available autocomplete: {e}")
            return []

    def _format_booked_line(self, time_slot, booked_fid, booked_manual_name, booked_alliance):
        """
        Formats a single booked-slot line. booked_fid/booked_manual_name are
        mutually exclusive: a registered member has fid set and is looked up
        against users/alliance_list; a portal-assigned manual/guest name has
        manual_name set and no alliance.
        """
        if booked_fid:
            self.users_cursor.execute("SELECT nickname FROM users WHERE fid=?", (booked_fid,))
            user = self.users_cursor.fetchone()
            booked_nickname = user[0] if user else f"ID: {booked_fid}"

            self.alliance_cursor.execute("SELECT name FROM alliance_list WHERE alliance_id=?", (booked_alliance,))
            alliance_data = self.alliance_cursor.fetchone()
            booked_alliance_name = alliance_data[0] if alliance_data else "Unknown"

            # Wrap nickname in LTR embedding to prevent line reversal
            return f"`{time_slot}` - [{booked_alliance_name}]\u202a{booked_nickname}\u202c - {booked_fid}"
        elif booked_manual_name:
            return f"`{time_slot}` - \u202a{booked_manual_name}\u202c"
        return None

    # handler for looping through all times, reading current nicknames from the database
    # handler for looping through all times without updating fids
    def generate_time_list(self, booked_times):
        """
        Generates a list of time slots with their booking details.

        booked_times: {time_slot: (fid, manual_name, alliance)}
        """
        time_list = []
        booked_fids = {}

        # Get current slot mode
        self.svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", ("slot_mode",))
        row = self.svs_cursor.fetchone()
        slot_mode = int(row[0]) if row else 0

        # Generate time slots based on mode
        time_slots = self.get_time_slots(slot_mode)

        for time_slot in time_slots:
            booked_fid, booked_manual_name, booked_alliance = booked_times.get(time_slot, ("", "", ""))
            line = self._format_booked_line(time_slot, booked_fid, booked_manual_name, booked_alliance)
            if line is not None:
                time_list.append(line)
            else:
                time_list.append(f"`{time_slot}` - ")
            booked_fids[time_slot] = booked_fid

        return time_list, booked_fids

    # handler for looping through available times
    def generate_available_time_list(self, booked_times):
        """
        Generates a list of only available (non-booked) time slots.
        """
        time_list = []

        # Get current slot mode
        self.svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", ("slot_mode",))
        row = self.svs_cursor.fetchone()
        slot_mode = int(row[0]) if row else 0

        # Generate time slots based on mode
        time_slots = self.get_time_slots(slot_mode)

        for time_slot in time_slots:
            if time_slot not in booked_times:  # Only add unbooked slots
                time_list.append(f"`{time_slot}` - ")

        return time_list

    # handler for looping through unavailable times
    def generate_booked_time_list(self, booked_times):
        """
        Generates a list of only booked time slots with their details.

        booked_times: {time_slot: (fid, manual_name, alliance)}
        """
        time_list = []

        # Get current slot mode
        self.svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", ("slot_mode",))
        row = self.svs_cursor.fetchone()
        slot_mode = int(row[0]) if row else 0

        # Generate time slots based on mode
        time_slots = self.get_time_slots(slot_mode)

        for time_slot in time_slots:
            if time_slot in booked_times:
                booked_fid, booked_manual_name, booked_alliance = booked_times[time_slot]
                line = self._format_booked_line(time_slot, booked_fid, booked_manual_name, booked_alliance)
                if line is not None:
                    time_list.append(line)

        return time_list

    def split_message_content(self, header: str, time_list: list, max_length: int = 1900) -> list:
        """
        Splits message content into chunks that fit within Discord's character limit.
        Returns a list of message strings.
        """
        if not time_list:
            return [header]

        messages = []
        current_lines = []
        current_length = len(header) + 1  # for newline after header

        for line in time_list:
            line_length = len(line) + 1
            if current_length + line_length > max_length:
                # Save current chunk
                if current_lines:
                    messages.append(header + "\n" + "\n".join(current_lines))
                else:
                    messages.append(header)
                current_lines = [line]
                current_length = len(header) + 1 + line_length
            else:
                current_lines.append(line)
                current_length += line_length

        # Add remaining lines
        if current_lines:
            messages.append(header + "\n" + "\n".join(current_lines))
        elif not messages:
            messages.append(header)

        return messages

    # handler to get minister channel
    async def get_channel_id(self, context: str):
        self.svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", (context,))
        row = self.svs_cursor.fetchone()
        return int(row[0]) if row else None

    # handler to get minister message from channel to edit it
    async def get_or_create_message(self, context: str, message_content: str, channel: discord.TextChannel):
        # Check if content exceeds Discord's 2000 character limit
        if len(message_content) > 1900:
            truncated_content = message_content[:1850] + "\n\n*... (list truncated due to length)*"
            message_content = truncated_content

        self.svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", (context,))
        row = self.svs_cursor.fetchone()

        if row:
            message_id = int(row[0])
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(content=message_content)
                return message
            except discord.NotFound:
                pass

        # Send a new message if none found
        new_message = await channel.send(message_content)
        self.svs_cursor.execute(
            "REPLACE INTO reference (context, context_id) VALUES (?, ?)",
            (context, new_message.id)
        )
        self.svs_conn.commit()
        return new_message

    # handler to get guild id
    async def get_log_guild(self, log_guild: discord.Guild) -> discord.Guild | None:
        self.svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", ("minister guild id",))
        row = self.svs_cursor.fetchone()

        if not row:
            # Save the current guild as main guild if not found
            if log_guild:
                self.svs_cursor.execute(
                    "INSERT INTO reference (context, context_id) VALUES (?, ?)",
                    ("minister guild id", log_guild.id)
                )
                self.svs_conn.commit()
                return log_guild
            else:
                return None
        else:
            guild_id = int(row[0])
            guild = self.bot.get_guild(guild_id)
            if guild:
                return guild
            else:
                return None

    @discord.app_commands.command(name='minister_clear_all', description='Cancel all Appointment appointments.')
    async def minister_clear_all(self, interaction: discord.Interaction):
        appointment_type = "Appointment"
        if not await self.is_admin(interaction.user.id):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return
        await interaction.response.defer()

        log_guild = await self.get_log_guild(interaction.guild)

        # Check minister log channels
        context = f"{appointment_type}"
        channel_context = f"{appointment_type} channel"

        log_context = "minister log channel"
        log_channel_id = await self.get_channel_id(log_context)
        log_channel = log_guild.get_channel(log_channel_id)

        if not log_channel:
            await interaction.followup.send(
                f"[Warning] Could not find a log channel. Log channel is needed before clearing the appointment \n\nRun the `/settings` command --> Other Features --> Minister Scheduling --> Channel Setup and choose a log channel")
            return

        try:
            # Send a confirmation prompt
            embed = discord.Embed(
                title=f"{theme.warnIcon} Confirm clearing {appointment_type} list.",
                description=f"Are you sure you want to remove all minister appointment slots for: {appointment_type}?\n"
                            f"**{theme.warnIcon} This action cannot be undone and all names will be removed {theme.warnIcon}**.\n"
                            f"You have 10 seconds to reply with 'Yes' to confirm or 'No' to cancel.",
                color=discord.Color.orange()
            )
            confirmation_message = await interaction.followup.send(embed=embed)

            # Wait for user confirmation
            def check(message):
                return message.author == interaction.user and message.channel == interaction.channel

            try:
                response = await self.bot.wait_for('message', check=check, timeout=10.0)

                if response.content.lower() == "yes":
                    # Retrieve booked times before deletion
                    self.svs_cursor.execute("SELECT time, fid, manual_name, alliance FROM appointments WHERE appointment_type=?", (appointment_type,))
                    booked_times = {row[0]: (row[1], row[2], row[3]) for row in self.svs_cursor.fetchall()}

                    # Generate available times list
                    time_list, _ = self.generate_time_list(booked_times)

                    # Split into chunks if too long for embed description (4096 char limit)
                    header = f"**Previous {appointment_type} schedule** (before clearing):"
                    message_chunks = self.split_message_content(header, time_list, max_length=4000)

                    for i, chunk in enumerate(message_chunks):
                        title = f"Cleared {appointment_type}" if i == 0 else f"Cleared {appointment_type} (continued)"
                        clear_list_embed = discord.Embed(
                            title=title,
                            description=chunk,
                            color=discord.Color.orange()
                        )
                        await self.send_embed_to_channel(clear_list_embed)

                    # Regenerate empty list of available times
                    booked_times = {}
                    time_list = self.generate_available_time_list(booked_times)

                    message_content = f"**{appointment_type}** available slots:\n" + "\n".join(time_list)

                    # Get the channel to update
                    self.svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", (channel_context,))
                    channel_row = self.svs_cursor.fetchone()

                    if channel_row:
                        channel_id = int(channel_row[0])
                        channel = log_guild.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                        await self.get_or_create_message(context, message_content, channel)
                    else:
                        await confirmation_message.reply(f"[Warning] Could not find message or channel for {appointment_type}, skipping message update.\n\nConfigure a channel via Minister Scheduling --> Channel Setup and it will be used next time")

                    self.svs_cursor.execute("DELETE FROM appointments WHERE appointment_type=?", (appointment_type,))
                    self.svs_conn.commit()

                    # Log the change
                    await self.log_change(
                        action_type="clear_all",
                        user=interaction.user,
                        appointment_type=appointment_type,
                        fid=None,
                        nickname=None,
                        old_time=None,
                        new_time=None,
                        alliance_name=None
                    )

                    embed = discord.Embed(
                        title=f"Cleared {appointment_type} list",
                        description=f"All appointments for {appointment_type} have been successfully removed.",
                        color=theme.emColor2
                    )
                    embed.set_author(name=f"Cleared by {interaction.user.display_name}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)

                    await self.send_embed_to_channel(embed)
                    await interaction.followup.send(f"{theme.verifiedIcon} Deleted all {appointment_type} appointments.")
                else:
                    await confirmation_message.reply(f"Cancelled the action. Nothing was removed from {appointment_type}.")

            except asyncio.TimeoutError:
                await interaction.followup.send("Time ran out. Run the command again if you want to clear the appointment", ephemeral=True)
                await confirmation_message.reply(f"<@{interaction.user.id}> did not respond in time. The action has been cancelled.")

        except Exception as e:
            logger.error(f"An error occurred while clearing the appointments: {e}")
            print(f"An error occurred: {e}")
            await interaction.followup.send(f"An error occurred while clearing the appointments: {e}", ephemeral=True)
        
    @discord.app_commands.command(name='minister_list', description='View the Appointment schedule.')
    @app_commands.autocomplete(all_or_available=choice_autocomplete)
    @app_commands.describe(
        all_or_available="Show full schedule or only available slots.",
    )
    async def minister_list(self, interaction: discord.Interaction, all_or_available: str):
        appointment_type = "Appointment"
        try:
            await interaction.response.defer()

            # Fetch the booked times for the specific appointment type
            self.svs_cursor.execute("SELECT time, fid, manual_name, alliance FROM appointments WHERE appointment_type=?", (appointment_type,))
            booked_times = {row[0]: (row[1], row[2], row[3]) for row in self.svs_cursor.fetchall()}

            if all_or_available == "all":
                time_list, _ = self.generate_time_list(booked_times)

                # Format the time list for the embed
                time_list = "\n".join(time_list)

                if time_list:
                    embed = discord.Embed(
                        title=f"Schedule for {appointment_type}",
                        description=time_list,
                        color=theme.emColor1
                    )
                    try:
                        await interaction.edit_original_response(embed=embed)
                    except discord.NotFound:
                        logger.info("Interaction expired before final update.")

            elif all_or_available == "available only":
                available_slots = self.generate_available_time_list(booked_times)
                if available_slots:
                    time_list = "\n".join(available_slots)
                    await interaction.followup.send(f"{appointment_type} available slots:\n{time_list}")
                else:
                    await interaction.followup.send(f"All appointment slots are filled for {appointment_type}")

        except Exception as e:
            logger.error(f"An error occurred while fetching the schedule: {e}")
            print(f"An error occurred: {e}")
            await interaction.followup.send(f"An error occurred while fetching the schedule: {e}")

    @discord.app_commands.command(name='minister_archive_save', description='Save current minister schedule to an archive (Global Admin only)')
    @app_commands.describe(name="Optional name for the archive (defaults to current date)")
    async def minister_archive_save(self, interaction: discord.Interaction, name: str = None):
        # Check if user is global admin
        minister_menu_cog = self.bot.get_cog("MinisterMenu")
        if not minister_menu_cog:
            await interaction.response.send_message(f"{theme.deniedIcon} Minister Menu module not found.", ephemeral=True)
            return

        is_admin, is_global_admin, _ = await minister_menu_cog.get_admin_permissions(interaction.user.id)
        if not is_global_admin:
            await interaction.response.send_message(f"{theme.deniedIcon} Only global administrators can save archives.", ephemeral=True)
            return

        # Get archive cog
        archive_cog = self.bot.get_cog("MinisterArchive")
        if not archive_cog:
            await interaction.response.send_message(f"{theme.deniedIcon} Minister Archive module not found.", ephemeral=True)
            return

        # Generate name if not provided
        if not name:
            name = datetime.now().strftime("KvK %Y-%m-%d")

        # Save the current schedule
        await archive_cog.save_current_schedule(interaction, name)

    @discord.app_commands.command(name='minister_archive_list', description='View all saved minister archives (Global Admin only)')
    async def minister_archive_list(self, interaction: discord.Interaction):
        # Check if user is global admin
        minister_menu_cog = self.bot.get_cog("MinisterMenu")
        if not minister_menu_cog:
            await interaction.response.send_message(f"{theme.deniedIcon} Minister Menu module not found.", ephemeral=True)
            return

        is_admin, is_global_admin, _ = await minister_menu_cog.get_admin_permissions(interaction.user.id)
        if not is_global_admin:
            await interaction.response.send_message(f"{theme.deniedIcon} Only global administrators can view archives.", ephemeral=True)
            return

        # Get archive cog
        archive_cog = self.bot.get_cog("MinisterArchive")
        if not archive_cog:
            await interaction.response.send_message(f"{theme.deniedIcon} Minister Archive module not found.", ephemeral=True)
            return

        # Show archive list
        await archive_cog.show_archive_list(interaction)

    async def archive_id_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for archive IDs"""
        try:
            # Get all archives
            self.svs_cursor.execute("""
                SELECT archive_id, archive_name, created_at
                FROM minister_archives
                ORDER BY created_at DESC
                LIMIT 25
            """)
            archives = self.svs_cursor.fetchall()

            choices = []
            for archive_id, archive_name, created_at in archives:
                created_date = datetime.fromisoformat(created_at).strftime("%Y-%m-%d")
                label = f"{archive_name} ({created_date})"

                if current and current.lower() not in label.lower():
                    continue

                choices.append(discord.app_commands.Choice(name=label[:100], value=archive_id))

            return choices[:25]
        except Exception as e:
            logger.error(f"Error in archive autocomplete: {e}")
            print(f"Error in archive autocomplete: {e}")
            return []

    @discord.app_commands.command(name='minister_archive_history', description='View change history for minister appointments (Global Admin only)')
    @app_commands.describe(
        archive_id="Optional: Select an archive to view its change history (leave empty for current changes)",
        discord_user="Optional: Filter by specific Discord user who made changes"
    )
    @app_commands.autocomplete(archive_id=archive_id_autocomplete)
    async def minister_archive_history(
        self,
        interaction: discord.Interaction,
        archive_id: int = None,
        discord_user: discord.User = None
    ):
        # Check if user is global admin
        minister_menu_cog = self.bot.get_cog("MinisterMenu")
        if not minister_menu_cog:
            await interaction.response.send_message(f"{theme.deniedIcon} Minister Menu module not found.", ephemeral=True)
            return

        is_admin, is_global_admin, _ = await minister_menu_cog.get_admin_permissions(interaction.user.id)
        if not is_global_admin:
            await interaction.response.send_message(f"{theme.deniedIcon} Only global administrators can view change history.", ephemeral=True)
            return

        # Get archive cog
        archive_cog = self.bot.get_cog("MinisterArchive")
        if not archive_cog:
            await interaction.response.send_message(f"{theme.deniedIcon} Minister Archive module not found.", ephemeral=True)
            return

        # Build query based on filters
        query = """
            SELECT
                timestamp, discord_username, action_type, appointment_type,
                fid, nickname, old_time, new_time, alliance_name, additional_data
            FROM minister_change_history
            WHERE 1=1
        """
        params = []

        if archive_id is not None:
            query += " AND archive_id = ?"
            params.append(archive_id)
        else:
            query += " AND archive_id IS NULL"

        if discord_user:
            query += " AND discord_user_id = ?"
            params.append(discord_user.id)

        query += " ORDER BY timestamp DESC"

        self.svs_cursor.execute(query, params)
        history_records = self.svs_cursor.fetchall()

        if not history_records:
            await interaction.response.send_message("No change history found with the specified filters.", ephemeral=True)
            return

        # Show history via archive cog
        from .minister_archive import ChangeHistoryView
        view = ChangeHistoryView(self.bot, archive_cog, history_records, page=0, archive_id=archive_id)
        await archive_cog.update_history_embed(interaction, history_records, 0, archive_id, view)

async def setup(bot):
    await bot.add_cog(MinisterSchedule(bot))

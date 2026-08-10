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
import sqlite3

import discord
from discord.ext import commands

from .permission_handler import PermissionManager
from .pimp_my_bot import theme, safe_edit_message
from . import auto_schedule_ai

logger = logging.getLogger('bot')

APPOINTMENT_TYPE = "Auto Schedule"
MAX_MESSAGES_SCANNED = 500


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


class AutoScheduleView(discord.ui.View):
    def __init__(self, bot, cog, is_global: bool = False):
        super().__init__(timeout=None)
        self.bot = bot
        self.cog = cog
        self.is_global = is_global

    @discord.ui.button(label="Generate Schedule", style=discord.ButtonStyle.success, emoji=f"{theme.robotIcon}")
    async def generate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.generate_schedule(interaction)

    @discord.ui.button(label="List", style=discord.ButtonStyle.primary, emoji=f"{theme.listIcon}")
    async def list_schedule(self, interaction: discord.Interaction, button: discord.ui.Button):
        minister_menu_cog = self.bot.get_cog("MinisterMenu")
        if not minister_menu_cog:
            await interaction.response.send_message(f"{theme.deniedIcon} Minister Menu module not found.", ephemeral=True)
            return
        await minister_menu_cog.show_current_schedule_list(interaction, APPOINTMENT_TYPE)

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.danger, emoji=f"{theme.trashIcon}")
    async def clear_schedule(self, interaction: discord.Interaction, button: discord.ui.Button):
        minister_menu_cog = self.bot.get_cog("MinisterMenu")
        if not minister_menu_cog:
            await interaction.response.send_message(f"{theme.deniedIcon} Minister Menu module not found.", ephemeral=True)
            return
        await minister_menu_cog.show_clear_confirmation(interaction, APPOINTMENT_TYPE)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji=f"{theme.backIcon}")
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        minister_menu_cog = self.bot.get_cog("MinisterMenu")
        if minister_menu_cog:
            await minister_menu_cog.show_minister_channel_menu(interaction)


class AutoSchedule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.svs_conn = sqlite3.connect("db/svs.sqlite", timeout=30.0, check_same_thread=False)
        self.svs_cursor = self.svs_conn.cursor()
        self.svs_conn.execute("PRAGMA journal_mode=WAL")
        self.svs_conn.execute("PRAGMA synchronous=NORMAL")

    async def cog_unload(self):
        try:
            self.svs_conn.close()
        except Exception:
            pass

    async def is_admin(self, user_id: int) -> bool:
        if user_id == self.bot.owner_id:
            return True
        is_admin, _ = PermissionManager.is_admin(user_id)
        return is_admin

    async def show_auto_schedule_menu(self, interaction: discord.Interaction):
        is_admin, is_global = PermissionManager.is_admin(interaction.user.id)
        if not is_admin:
            await interaction.response.send_message(f"{theme.deniedIcon} You do not have permission to manage the Auto Schedule.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"{theme.robotIcon} Auto Schedule",
            description=(
                f"A separate schedule from Chief Minister, filled automatically from requests "
                f"posted in a channel -- each message is parsed by AI (Gemini) into a name, "
                f"a speedup amount, and a preferred time, then everyone is assigned exactly "
                f"one of the 48 daily slots (highest speedup amount gets first pick).\n\n"
                f"**Available Operations**\n"
                f"{theme.upperDivider}\n"
                f"{theme.robotIcon} **Generate Schedule**\n"
                f"└ Read every message in the configured channel and (re)build the schedule\n\n"
                f"{theme.listIcon} **List**\n"
                f"└ View the current Auto Schedule\n\n"
                f"{theme.trashIcon} **Clear**\n"
                f"└ Remove all Auto Schedule appointments\n\n"
                f"{theme.lowerDivider}\n\n"
                f"Configure the channel via Minister Scheduling → Channel Setup → Auto Schedule Channel."
            ),
            color=theme.emColor1
        )

        view = AutoScheduleView(self.bot, self, is_global)
        await safe_edit_message(interaction, embed=embed, view=view)

    async def generate_schedule(self, interaction: discord.Interaction):
        if not await self.is_admin(interaction.user.id):
            await interaction.response.send_message(f"{theme.deniedIcon} You do not have permission to do this.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        minister_schedule_cog = self.bot.get_cog("MinisterSchedule")
        minister_menu_cog = self.bot.get_cog("MinisterMenu")
        if not minister_schedule_cog or not minister_menu_cog:
            await interaction.followup.send(f"{theme.deniedIcon} Minister modules not loaded.", ephemeral=True)
            return

        channel_id = await minister_schedule_cog.get_channel_id(f"{APPOINTMENT_TYPE} channel")
        if not channel_id:
            await interaction.followup.send(
                f"{theme.deniedIcon} No Auto Schedule channel configured yet. Set one via "
                f"Minister Scheduling → Channel Setup → Auto Schedule Channel.",
                ephemeral=True
            )
            return

        log_guild = await minister_schedule_cog.get_log_guild(interaction.guild)
        channel = log_guild.get_channel(channel_id) if log_guild else None
        if not channel:
            await interaction.followup.send(f"{theme.deniedIcon} Could not find the configured Auto Schedule channel.", ephemeral=True)
            return

        try:
            raw_messages = []
            async for message in channel.history(limit=MAX_MESSAGES_SCANNED, oldest_first=True):
                if message.author.bot:
                    continue
                content = message.content.strip()
                if content:
                    raw_messages.append(content)
        except discord.Forbidden:
            await interaction.followup.send(f"{theme.deniedIcon} I don't have permission to read that channel's message history.", ephemeral=True)
            return

        if not raw_messages:
            await interaction.followup.send(f"{theme.warnIcon} No messages found in the Auto Schedule channel.", ephemeral=True)
            return

        try:
            parsed = await auto_schedule_ai.parse_schedule_requests(raw_messages)
        except RuntimeError as e:
            await interaction.followup.send(f"{theme.deniedIcon} {e}", ephemeral=True)
            return

        if not parsed:
            await interaction.followup.send(
                f"{theme.warnIcon} Could not extract any scheduling requests from {len(raw_messages)} message(s).",
                ephemeral=True
            )
            return

        slot_mode = await minister_schedule_cog.get_channel_id("slot_mode") or 0
        time_slots = minister_schedule_cog.get_time_slots(slot_mode)
        result = allocate_slots(parsed, time_slots)

        try:
            self.svs_cursor.execute("DELETE FROM appointments WHERE appointment_type=?", (APPOINTMENT_TYPE,))
            for entry in result["assigned"]:
                self.svs_cursor.execute(
                    "INSERT INTO appointments (fid, manual_name, appointment_type, time, alliance) VALUES (NULL, ?, ?, ?, NULL)",
                    (entry["name"], APPOINTMENT_TYPE, entry["time"])
                )
            self.svs_conn.commit()
        except sqlite3.Error as e:
            self.svs_conn.rollback()
            logger.error(f"Auto Schedule: failed to save generated schedule: {e}")
            await interaction.followup.send(f"{theme.deniedIcon} Failed to save the generated schedule: {e}", ephemeral=True)
            return

        for entry in result["assigned"]:
            try:
                await minister_schedule_cog.log_change(
                    action_type="add",
                    user=interaction.user,
                    appointment_type=APPOINTMENT_TYPE,
                    fid=None,
                    nickname=entry["name"],
                    old_time=None,
                    new_time=entry["time"],
                    alliance_name=None,
                )
            except Exception:
                logger.exception("Auto Schedule: failed to log a change entry")

        try:
            await minister_menu_cog.update_channel_message_as_booking_list(APPOINTMENT_TYPE)
        except Exception:
            logger.exception("Auto Schedule: failed to refresh the board")

        summary = [f"{theme.verifiedIcon} Generated Auto Schedule from {len(raw_messages)} message(s)."]
        summary.append(f"Assigned: {len(result['assigned'])}")
        skipped = len(raw_messages) - len(parsed)
        if skipped > 0:
            summary.append(f"Not recognized as scheduling requests: {skipped} message(s).")
        if result["unscheduled"]:
            summary.append(f"Could not fit into their preferred time ({len(result['unscheduled'])}): " + ", ".join(result["unscheduled"]))

        await interaction.followup.send("\n".join(summary), ephemeral=True)


async def setup(bot):
    await bot.add_cog(AutoSchedule(bot))

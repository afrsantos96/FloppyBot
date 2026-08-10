"""
Minister scheduling menu. UI for managing state minister appointments and rotations.
"""
import discord
from discord.ext import commands
import sqlite3
import logging
from contextlib import closing
from .permission_handler import PermissionManager
from .pimp_my_bot import theme, safe_edit_message
from .alliance_member_edit import apply_member_edit
from web.config import load_portal_config
from web.auth import issue_magic_link_payload, sign_token

logger = logging.getLogger('bot')


class UpdateNamesModal(discord.ui.Modal):
    """Manually set booked ministers' names, one `id, name` per line."""

    def __init__(self, cog, activity_name, prefill=""):
        super().__init__(title="Update Names")
        self.cog = cog
        self.activity_name = activity_name
        self.lines_input = discord.ui.TextInput(
            label="Per line: id, name",
            style=discord.TextStyle.paragraph,
            placeholder="12345678, PlayerName\n23456789, Another Name",
            default=prefill,
            required=True,
            max_length=4000,
        )
        self.add_item(self.lines_input)

    async def on_submit(self, interaction: discord.Interaction):
        updated = 0
        skipped = 0
        for line in self.lines_input.value.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(",", 1)
            if len(parts) != 2:
                skipped += 1
                continue
            fid_str, name = parts[0].strip(), parts[1].strip()
            if not fid_str.isdigit() or not name:
                skipped += 1
                continue
            if apply_member_edit(int(fid_str), nickname=name):
                updated += 1

        result_msg = f"Updated {updated} name(s) for {self.activity_name}"
        if skipped:
            result_msg += f" ({skipped} line(s) skipped)"

        _, is_global, _ = await self.cog.get_admin_permissions(interaction.user.id)
        embed = discord.Embed(
            title=f"{theme.settingsIcon} Minister Settings",
            description=(
                f"{theme.verifiedIcon} **{result_msg}**\n\n"
                f"Administrative settings for minister scheduling:\n\n"
                f"Available Actions\n"
                f"{theme.upperDivider}\n\n"
                f"{theme.editListIcon} **Update Names**\n"
                f"└ Manually set booked ministers' names\n\n"
                f"{theme.listIcon} **Schedule List Type**\n"
                f"└ Change the type of schedule list message when adding/removing people\n\n"
                f"{theme.calendarIcon} **Delete All Reservations**\n"
                f"└ Clear appointments for a specific day\n\n"
                f"{theme.announceIcon} **Clear Channels**\n"
                f"└ Clear channel configurations\n\n"
                f"{theme.fidIcon} **Delete Server ID**\n"
                f"└ Remove configured server from database\n\n"
                f"{theme.lowerDivider}"
            ),
            color=theme.emColor3
        )
        view = MinisterSettingsView(self.cog.bot, self.cog, is_global)
        await interaction.response.edit_message(embed=embed, view=view, content=None)

class ClearConfirmationView(discord.ui.View):
    def __init__(self, bot, cog, activity_name, is_global_admin, alliance_ids):
        super().__init__(timeout=7200)
        self.bot = bot
        self.cog = cog
        self.activity_name = activity_name
        self.is_global_admin = is_global_admin
        self.alliance_ids = alliance_ids
    
    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji=f"{theme.verifiedIcon}")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        minister_schedule_cog = self.bot.get_cog("MinisterSchedule")
        
        if self.is_global_admin:
            # Get all appointments to log before clearing
            self.cog.svs_cursor.execute("SELECT time, fid, manual_name, alliance FROM appointments WHERE appointment_type=?", (self.activity_name,))
            cleared_fids = {row[0]: (row[1], row[2], row[3]) for row in self.cog.svs_cursor.fetchall()}

            time_list, _ = minister_schedule_cog.generate_time_list(cleared_fids)

            # Split into chunks if too long for embed description (4096 char limit)
            header = f"**Previous {self.activity_name} schedule** (before clearing):"
            message_chunks = minister_schedule_cog.split_message_content(header, time_list, max_length=4000)

            for i, chunk in enumerate(message_chunks):
                title = f"Cleared {self.activity_name}" if i == 0 else f"Cleared {self.activity_name} (continued)"
                clear_list_embed = discord.Embed(
                    title=title,
                    description=chunk,
                    color=discord.Color.orange()
                )
                await minister_schedule_cog.send_embed_to_channel(clear_list_embed)

            # Clear all appointments
            self.cog.svs_cursor.execute("DELETE FROM appointments WHERE appointment_type=?", (self.activity_name,))
            self.cog.svs_conn.commit()
            
            cleared_count = len(cleared_fids)
            message = f"Cleared all {cleared_count} appointments for {self.activity_name}"
        else:
            # Get appointments for allowed alliances
            placeholders = ','.join('?' for _ in self.alliance_ids)
            query = f"SELECT fid FROM appointments WHERE appointment_type=? AND alliance IN ({placeholders})"
            self.cog.svs_cursor.execute(query, [self.activity_name] + self.alliance_ids)
            cleared_fids = [row[0] for row in self.cog.svs_cursor.fetchall()]
            
            # Clear alliance appointments
            query = f"DELETE FROM appointments WHERE appointment_type=? AND alliance IN ({placeholders})"
            self.cog.svs_cursor.execute(query, [self.activity_name] + self.alliance_ids)
            self.cog.svs_conn.commit()
            
            cleared_count = len(cleared_fids)
            message = f"Cleared {cleared_count} alliance appointments for {self.activity_name}"
        
        # Send log
        if minister_schedule_cog and cleared_count > 0:
            embed = discord.Embed(
                title=f"Appointments Cleared - {self.activity_name}",
                description=f"{cleared_count} appointments were cleared",
                color=theme.emColor2
            )
            embed.set_author(name=f"Cleared by {interaction.user.display_name}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
            await minister_schedule_cog.send_embed_to_channel(embed)
            await self.cog.update_channel_message(self.activity_name)
        
        # Return to settings menu with success message
        embed = discord.Embed(
            title=f"{theme.settingsIcon} Minister Settings",
            description=(
                f"{theme.verifiedIcon} **{message}**\n\n"
                f"Administrative settings for minister scheduling:\n\n"
                f"Available Actions\n"
                f"{theme.upperDivider}\n\n"
                f"{theme.editListIcon} **Update Names**\n"
                f"└ Manually set booked ministers' names\n\n"
                f"{theme.listIcon} **Schedule List Type**\n"
                f"└ Change the type of schedule list message when adding/removing people\n\n"
                f"{theme.calendarIcon} **Delete All Reservations**\n"
                f"└ Clear appointments for a specific day\n\n"
                f"{theme.announceIcon} **Clear Channels**\n"
                f"└ Clear channel configurations\n\n"
                f"{theme.fidIcon} **Delete Server ID**\n"
                f"└ Remove configured server from database\n\n"
                f"{theme.lowerDivider}"
            ),
            color=theme.emColor3
        )
        
        view = MinisterSettingsView(self.cog.bot, self.cog, self.is_global_admin)
        await interaction.followup.send(embed=embed, view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji=f"{theme.deniedIcon}")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_minister_channel_menu(interaction)

class MinisterSettingsView(discord.ui.View):
    def __init__(self, bot, cog, is_global: bool = False):
        super().__init__(timeout=None)
        self.bot = bot
        self.cog = cog
        self.is_global = is_global

        # Disable global-admin-only buttons for non-global admins
        if not is_global:
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.label in [
                    "Schedule List Type", "Time Slot Mode",
                    "Delete All Reservations", "Clear Channels", "Delete Server ID"
                ]:
                    child.disabled = True

    @discord.ui.button(label="Update Names", style=discord.ButtonStyle.secondary, emoji=f"{theme.editListIcon}", row=1)
    async def update_names(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is admin
        if not await self.cog.is_admin(interaction.user.id):
            await interaction.response.send_message(f"{theme.deniedIcon} You do not have permission to update names.", ephemeral=True)
            return

        await self.cog.show_activity_selection_for_update(interaction)

    @discord.ui.button(label="Schedule List Type", style=discord.ButtonStyle.secondary, emoji=f"{theme.listIcon}", row=1)
    async def list_type(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is admin
        if not await self.cog.is_admin(interaction.user.id):
            await interaction.response.send_message(f"{theme.deniedIcon} You do not have permission to update names.", ephemeral=True)
            return

        await self.cog.show_activity_selection_for_list_type(interaction)

    @discord.ui.button(label="Time Slot Mode", style=discord.ButtonStyle.secondary, emoji=f"{theme.timeIcon}", row=1)
    async def time_slot_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is admin
        if not await self.cog.is_admin(interaction.user.id):
            await interaction.response.send_message(f"{theme.deniedIcon} You do not have permission to change time slot mode.", ephemeral=True)
            return

        await self.cog.show_time_slot_mode_menu(interaction)
    
    @discord.ui.button(label="Delete All Reservations", style=discord.ButtonStyle.danger, emoji=f"{theme.calendarIcon}", row=2)
    async def clear_reservations(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is global admin
        is_admin, is_global_admin, _ = await self.cog.get_admin_permissions(interaction.user.id)
        if not is_global_admin:
            await interaction.response.send_message(f"{theme.deniedIcon} Only global administrators can clear reservations.", ephemeral=True)
            return
        
        await self.cog.show_activity_selection_for_clear(interaction)
    
    @discord.ui.button(label="Clear Channels", style=discord.ButtonStyle.danger, emoji=f"{theme.announceIcon}", row=2)
    async def clear_channels(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is global admin
        is_admin, is_global_admin, _ = await self.cog.get_admin_permissions(interaction.user.id)
        if not is_global_admin:
            await interaction.response.send_message(f"{theme.deniedIcon} Only global administrators can clear channel configurations.", ephemeral=True)
            return
        
        await self.cog.show_clear_channels_selection(interaction)
    
    @discord.ui.button(label="Delete Server ID", style=discord.ButtonStyle.danger, emoji=f"{theme.fidIcon}", row=3)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is global admin
        is_admin, is_global_admin, _ = await self.cog.get_admin_permissions(interaction.user.id)
        if not is_global_admin:
            await interaction.response.send_message(f"{theme.deniedIcon} Only global administrators can delete server configuration.", ephemeral=True)
            return
        
        try:
            with closing(sqlite3.connect("db/svs.sqlite")) as svs_conn:
                svs_cursor = svs_conn.cursor()
                svs_cursor.execute("DELETE FROM reference WHERE context=?", ("minister guild id",))
                svs_conn.commit()
            await interaction.response.send_message(f"{theme.verifiedIcon} Server ID deleted from the database.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"{theme.deniedIcon} Failed to delete server ID: {e}", ephemeral=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.primary, emoji=f"{theme.backIcon}", row=3)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_minister_channel_menu(interaction)

class MinisterChannelView(discord.ui.View):
    def __init__(self, bot, cog, is_global: bool = False):
        super().__init__(timeout=None)
        self.bot = bot
        self.cog = cog
        self.is_global = is_global

        # Disable global-admin-only buttons for non-global admins
        # Note: Channel Setup is server-specific, so it's allowed for server admins
        if not is_global:
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.label in [
                    "Event Archive"
                ]:
                    child.disabled = True

    @discord.ui.button(label="Channel Setup", style=discord.ButtonStyle.success, emoji=f"{theme.editListIcon}")
    async def channel_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Channel setup is server-specific, so any admin can configure it
        if not await self.cog.is_admin(interaction.user.id):
            await interaction.response.send_message(f"{theme.deniedIcon} You do not have permission to configure channels.", ephemeral=True)
            return

        await self.cog.show_channel_setup_menu(interaction)

    @discord.ui.button(label="Online Manage Portal", style=discord.ButtonStyle.success, emoji=f"{theme.linkIcon}")
    async def online_portal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.send_portal_link(interaction)

    @discord.ui.button(label="Event Archive", style=discord.ButtonStyle.secondary, emoji=f"{theme.archiveIcon}")
    async def event_archive(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is global admin
        is_admin, is_global_admin, _ = await self.cog.get_admin_permissions(interaction.user.id)
        if not is_global_admin:
            await interaction.response.send_message(f"{theme.deniedIcon} Only global administrators can access archives.", ephemeral=True)
            return

        # Get archive cog
        archive_cog = self.bot.get_cog("MinisterArchive")
        if not archive_cog:
            await interaction.response.send_message(f"{theme.deniedIcon} Minister Archive module not found.", ephemeral=True)
            return

        await archive_cog.show_archive_menu(interaction)

    @discord.ui.button(label="Settings", style=discord.ButtonStyle.secondary, emoji=f"{theme.settingsIcon}")
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_settings_menu(interaction)

    @discord.ui.button(label="List", style=discord.ButtonStyle.primary, emoji=f"{theme.listIcon}")
    async def list_schedule(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_current_schedule_list(interaction, "Appointment")

    @discord.ui.button(label="Full List", style=discord.ButtonStyle.primary, emoji=f"{theme.listIcon}", row=1)
    async def full_list_schedule(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_full_schedule_list(interaction, "Appointment")

    @discord.ui.button(label="Auto Schedule", style=discord.ButtonStyle.success, emoji=f"{theme.robotIcon}", row=1)
    async def auto_schedule(self, interaction: discord.Interaction, button: discord.ui.Button):
        auto_schedule_cog = self.bot.get_cog("AutoSchedule")
        if not auto_schedule_cog:
            await interaction.response.send_message(f"{theme.deniedIcon} Auto Schedule module not found.", ephemeral=True)
            return
        await auto_schedule_cog.show_auto_schedule_menu(interaction)

    @discord.ui.button(label="Main Menu", style=discord.ButtonStyle.secondary, emoji=f"{theme.homeIcon}", row=2)
    async def main_menu_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            main_menu_cog = self.cog.bot.get_cog("MainMenu")
            if main_menu_cog:
                await main_menu_cog.show_main_menu(interaction)
            else:
                await interaction.response.send_message(
                    f"{theme.deniedIcon} Main Menu module not found.",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.response.send_message(
                f"{theme.deniedIcon} An error occurred while returning to Main Menu: {e}",
                ephemeral=True
            )

class ChannelConfigurationView(discord.ui.View):
    def __init__(self, bot, cog):
        super().__init__(timeout=None)
        self.bot = bot
        self.cog = cog

    @discord.ui.button(label="Appointment Channel", style=discord.ButtonStyle.secondary, emoji=f"{theme.crownIcon}")
    async def appointment_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_channel_selection(interaction, "Appointment channel", "Appointment")

    @discord.ui.button(label="Auto Schedule Channel", style=discord.ButtonStyle.secondary, emoji=f"{theme.robotIcon}")
    async def auto_schedule_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_channel_selection(interaction, "Auto Schedule channel", "Auto Schedule")

    @discord.ui.button(label="Log Channel", style=discord.ButtonStyle.secondary, emoji=f"{theme.documentIcon}")
    async def log_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_channel_selection(interaction, "minister log channel", "general logging")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.primary, emoji=f"{theme.backIcon}", row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_minister_channel_menu(interaction)

    async def _handle_channel_selection(self, interaction: discord.Interaction, channel_context: str, activity_name: str):
        minister_schedule_cog = self.cog.bot.get_cog("MinisterSchedule")
        if not minister_schedule_cog:
            await interaction.response.send_message(f"{theme.deniedIcon} Minister Schedule module not found.", ephemeral=True)
            return

        import sys
        minister_module = minister_schedule_cog.__class__.__module__
        ChannelSelect = getattr(sys.modules[minister_module], 'ChannelSelect')
        
        # Create a custom view with a back button
        class ChannelSelectWithBackView(discord.ui.View):
            def __init__(self, bot, context, cog):
                super().__init__(timeout=None)
                self.bot = bot
                self.context = context
                self.cog = cog
                self.add_item(ChannelSelect(bot, context))
                
            @discord.ui.button(label="Back", style=discord.ButtonStyle.primary, emoji=f"{theme.backIcon}", row=1)
            async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                # Restore the menu with embed
                embed = discord.Embed(
                    title=f"{theme.listIcon} Channel Setup",
                    description=(
                        f"Configure channels for minister scheduling:\n\n"
                        f"Channel Types\n"
                        f"{theme.upperDivider}\n\n"
                        f"{theme.crownIcon} **Appointment Channel**\n"
                        f"└ Shows the Appointment schedule\n\n"
                        f"{theme.robotIcon} **Auto Schedule Channel**\n"
                        f"└ Where governors post requests and the AI-generated schedule is shown\n\n"
                        f"{theme.listIcon} **Log Channel**\n"
                        f"└ Receives add/remove notifications\n\n"
                        f"{theme.lowerDivider}\n\n"
                        f"Select a channel type to configure:"
                    ),
                    color=theme.emColor1
                )

                import sys
                minister_menu_module = self.cog.__class__.__module__
                ChannelConfigurationView = getattr(sys.modules[minister_menu_module], 'ChannelConfigurationView')
                
                view = ChannelConfigurationView(self.bot, self.cog)
                
                await interaction.response.edit_message(
                    content=None, # Clear the "Select a channel for..." content
                    embed=embed,
                    view=view
                )

        await interaction.response.edit_message(
            content=f"Select a channel for {activity_name}:",
            view=ChannelSelectWithBackView(self.bot, channel_context, self.cog),
            embed=None
        )

class MinisterMenu(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.users_conn = sqlite3.connect('db/users.sqlite', timeout=30.0, check_same_thread=False)
        self.users_cursor = self.users_conn.cursor()
        self.alliance_conn = sqlite3.connect('db/alliance.sqlite', timeout=30.0, check_same_thread=False)
        self.alliance_cursor = self.alliance_conn.cursor()
        self.svs_conn = sqlite3.connect("db/svs.sqlite", timeout=30.0, check_same_thread=False)
        self.svs_cursor = self.svs_conn.cursor()

    async def cog_unload(self):
        """Close database connections when cog is unloaded."""
        try:
            self.users_conn.close()
            self.alliance_conn.close()
            self.svs_conn.close()
        except Exception:
            pass

    async def is_admin(self, user_id: int) -> bool:
        with closing(sqlite3.connect('db/settings.sqlite')) as settings_conn:
            settings_cursor = settings_conn.cursor()

            if user_id == self.bot.owner_id:
                return True

            settings_cursor.execute("SELECT 1 FROM admin WHERE id=?", (user_id,))
            return settings_cursor.fetchone() is not None

    async def show_minister_channel_menu(self, interaction: discord.Interaction):
        # Store the original interaction for later updates

        # Get channel status and permissions
        channel_status, embed_color = await self.get_channel_status_display()
        _, is_global, _ = await self.get_admin_permissions(interaction.user.id)

        embed = discord.Embed(
            title="🏛️ Minister Scheduling",
            description=(
                f"Manage Appointment appointments here. Assigning ministers is done through "
                f"the Online Manage Portal -- use List to just view the current schedule.\n\n"
                f"**Channel Status**\n"
                f"{theme.upperDivider}\n"
                f"{channel_status}\n"
                f"{theme.middleDivider}\n\n"
                f"**Available Operations**\n"
                f"{theme.middleDivider}\n"
                f"{theme.editListIcon} **Channel Setup**\n"
                f"└ Configure channels for appointments and logging\n\n"
                f"{theme.linkIcon} **Online Manage Portal**\n"
                f"└ Get a one-time link to assign ministers in a browser\n\n"
                f"{theme.archiveIcon} **Event Archive**\n"
                f"└ Save and view past KvK minister schedules\n\n"
                f"{theme.settingsIcon} **Settings**\n"
                f"└ Update names, clear reservations and more\n\n"
                f"{theme.listIcon} **List**\n"
                f"└ View the current Appointment schedule\n"
                f"{theme.lowerDivider}"
            ),
            color=embed_color
        )

        view = MinisterChannelView(self.bot, self, is_global)
        await safe_edit_message(interaction, embed=embed, view=view)

    async def show_channel_setup_menu(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title=f"{theme.listIcon} Channel Setup",
            description=(
                f"Configure channels for minister scheduling:\n\n"
                f"Channel Types\n"
                f"{theme.upperDivider}\n\n"
                f"{theme.crownIcon} **Appointment Channel**\n"
                f"└ Shows the Appointment schedule\n\n"
                f"{theme.robotIcon} **Auto Schedule Channel**\n"
                f"└ Where governors post requests and the AI-generated schedule is shown\n\n"
                f"{theme.listIcon} **Log Channel**\n"
                f"└ Receives all change notifications\n\n"
                f"{theme.lowerDivider}\n\n"
                f"Select a channel type to configure:"
            ),
            color=theme.emColor1
        )

        view = ChannelConfigurationView(self.bot, self)
        await safe_edit_message(interaction, embed=embed, view=view)

    async def get_channel_status_display(self) -> tuple[str, discord.Color]:
        """
        Generate channel status display for main menu.
        Returns (status_text, embed_color)
        """
        minister_schedule_cog = self.bot.get_cog("MinisterSchedule")
        if not minister_schedule_cog:
            return f"{theme.warnIcon} **Minister Schedule module not loaded**\n", discord.Color.red()

        # Get the log guild to check channels
        try:
            log_guild = await minister_schedule_cog.get_log_guild(None)
        except Exception:
            log_guild = None

        # Define channels to check
        channels_config = [
            ("Appointment channel", f"{theme.crownIcon} Appointment"),
            ("minister log channel", f"{theme.listIcon} Log Channel")
        ]

        status_lines = []
        configured_count = 0
        invalid_count = 0

        for context, label in channels_config:
            channel_id = await minister_schedule_cog.get_channel_id(context)

            if not channel_id:
                status_lines.append(f"{label}: {theme.warnIcon} Not Configured")
            else:
                # Try to get the channel
                channel = None
                if log_guild:
                    channel = log_guild.get_channel(channel_id)

                if channel:
                    status_lines.append(f"{label}: {theme.verifiedIcon} {channel.mention}")
                    configured_count += 1
                else:
                    status_lines.append(f"{label}: {theme.deniedIcon} Invalid Channel")
                    invalid_count += 1

        # Determine embed color based on status
        total_channels = len(channels_config)
        if configured_count == total_channels:
            embed_color = discord.Color.green()
        elif configured_count > 0:
            embed_color = discord.Color.orange()
        else:
            embed_color = discord.Color.red()

        status_text = "\n".join(status_lines)
        return status_text, embed_color

    async def get_admin_permissions(self, user_id: int):
        """Get admin permissions - delegates to centralized PermissionManager"""
        is_admin, is_global = PermissionManager.is_admin(user_id)
        if not is_admin:
            return False, False, []
        if is_global:
            return True, True, []
        # Get alliance-specific permissions for server admin
        with sqlite3.connect('db/settings.sqlite') as db:
            cursor = db.cursor()
            cursor.execute("SELECT alliances_id FROM adminserver WHERE admin=?", (user_id,))
            alliance_ids = [row[0] for row in cursor.fetchall()]
        return True, False, alliance_ids

    async def send_portal_link(self, interaction: discord.Interaction):
        """Generate a one-time magic link into the web portal and reply with
        it ephemerally. No DM dependency, matches every other confirmation
        in this menu."""
        is_admin, is_global_admin, alliance_ids = await self.get_admin_permissions(interaction.user.id)
        if not is_admin:
            await interaction.response.send_message(
                f"{theme.deniedIcon} You do not have permission to use the online portal.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        cfg = load_portal_config()
        if not cfg:
            await interaction.followup.send(
                f"{theme.deniedIcon} The online portal is not configured on this bot. Contact the bot host.",
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id if interaction.guild else None
        if not guild_id:
            await interaction.followup.send(
                f"{theme.deniedIcon} The online portal must be opened from within a server.", ephemeral=True
            )
            return

        payload = issue_magic_link_payload(interaction.user.id, guild_id)
        token = sign_token(payload, cfg.signing_secret)
        link = f"{cfg.base_url}/portal/{token}"

        # A link-style button, not raw URL text: Discord's servers auto-crawl
        # plain-text URLs in messages to generate a link preview, which silently
        # consumes a single-use token before the admin ever clicks it. A button's
        # url isn't message content, so it isn't crawled.
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Portal", style=discord.ButtonStyle.link, url=link, emoji=f"{theme.linkIcon}"))

        await interaction.followup.send(
            f"{theme.verifiedIcon} Your portal link (valid for 5 minutes).",
            view=view,
            ephemeral=True
        )

    def _fetch_booking_lines(self, activity_name: str):
        """Returns (bookings, booking_lines) -- the same 'who's booked when'
        format used by both the List command and the post-portal-save board
        update, so the two always show the same thing."""
        self.svs_cursor.execute("SELECT time, fid, manual_name, alliance FROM appointments WHERE appointment_type=? ORDER BY time", (activity_name,))
        bookings = self.svs_cursor.fetchall()

        booking_lines = []
        for time, fid, manual_name, alliance_id in bookings:
            if fid:
                self.users_cursor.execute("SELECT nickname FROM users WHERE fid=?", (fid,))
                user_result = self.users_cursor.fetchone()
                nickname = user_result[0] if user_result else f"Unknown ({fid})"

                self.alliance_cursor.execute("SELECT name FROM alliance_list WHERE alliance_id=?", (alliance_id,))
                alliance_result = self.alliance_cursor.fetchone()
                alliance_name = alliance_result[0] if alliance_result else "Unknown"

                booking_lines.append(f"`{time}` - [{alliance_name}] {nickname} ({fid})")
            else:
                booking_lines.append(f"`{time}` - {manual_name}")

        return bookings, booking_lines

    async def update_channel_message_as_booking_list(self, activity_name: str):
        """Update the channel board with the current bookings (same format as
        the List command), instead of the list-type-setting-dependent slot
        list update_channel_message produces. Used after a portal save."""
        try:
            minister_schedule_cog = self.bot.get_cog("MinisterSchedule")
            if not minister_schedule_cog:
                return

            bookings, booking_lines = self._fetch_booking_lines(activity_name)
            if not bookings:
                message_content = f"**{activity_name} Schedule**\nNo appointments currently booked."
            else:
                message_content = (
                    f"**{activity_name} Schedule**\n" + "\n".join(booking_lines) +
                    f"\n\nTotal bookings: {len(bookings)}/48"
                )

            context = f"{activity_name}"
            channel_context = f"{activity_name} channel"

            channel_id = await minister_schedule_cog.get_channel_id(channel_context)
            if channel_id:
                log_guild = await minister_schedule_cog.get_log_guild(None)
                if log_guild:
                    channel = log_guild.get_channel(channel_id)
                    if channel:
                        await minister_schedule_cog.get_or_create_message(context, message_content, channel)

        except Exception as e:
            print(f"Error updating channel message: {e}")

    async def show_current_schedule_list(self, interaction: discord.Interaction, activity_name: str):
        """Show a paginated list of current bookings"""
        await interaction.response.defer()

        bookings, booking_lines = self._fetch_booking_lines(activity_name)

        if not bookings:
            embed = discord.Embed(
                title=f"{theme.listIcon} {activity_name} Schedule",
                description="No appointments currently booked.",
                color=theme.emColor1
            )
            await interaction.followup.send(embed=embed)
            return

        # Create embed with all bookings
        embed = discord.Embed(
            title=f"{theme.listIcon} {activity_name} Schedule",
            description="\n".join(booking_lines),
            color=theme.emColor1
        )
        embed.set_footer(text=f"Total bookings: {len(bookings)}/48")

        await interaction.followup.send(embed=embed)

    async def show_full_schedule_list(self, interaction: discord.Interaction, activity_name: str):
        """Show every one of the 48 slots, booked or not -- unlike
        show_current_schedule_list, which only lists actual bookings."""
        await interaction.response.defer()

        minister_schedule_cog = self.bot.get_cog("MinisterSchedule")
        if not minister_schedule_cog:
            await interaction.followup.send(f"{theme.deniedIcon} Minister Schedule module not found.", ephemeral=True)
            return

        self.svs_cursor.execute(
            "SELECT time, fid, manual_name, alliance FROM appointments WHERE appointment_type=?",
            (activity_name,)
        )
        booked_times = {row[0]: (row[1], row[2], row[3]) for row in self.svs_cursor.fetchall()}

        time_list, _ = minister_schedule_cog.generate_time_list(booked_times)
        description = "\n".join(time_list)
        if len(description) > 4000:
            description = description[:3950] + "\n\n*... (list truncated due to length)*"

        embed = discord.Embed(
            title=f"{theme.listIcon} {activity_name} Full Schedule (all 48 slots)",
            description=description,
            color=theme.emColor1
        )
        await interaction.followup.send(embed=embed)

    async def update_minister_names(self, interaction: discord.Interaction, activity_name: str):
        """Open a modal to manually set booked ministers' names for this activity."""
        self.svs_cursor.execute("SELECT fid FROM appointments WHERE appointment_type=? AND fid IS NOT NULL ORDER BY time", (activity_name,))
        fids = [row[0] for row in self.svs_cursor.fetchall()]

        if not fids:
            await interaction.response.send_message(f"{theme.deniedIcon} No booked ministers to update for {activity_name}.", ephemeral=True)
            return

        prefill_lines = []
        for fid in fids:
            self.users_cursor.execute("SELECT nickname FROM users WHERE fid=?", (fid,))
            row = self.users_cursor.fetchone()
            nickname = row[0] if row else ""
            prefill_lines.append(f"{fid}, {nickname}")

        modal = UpdateNamesModal(self, activity_name, "\n".join(prefill_lines))
        await interaction.response.send_modal(modal)
    
    async def show_clear_confirmation(self, interaction: discord.Interaction, activity_name: str):
        """Show confirmation for clearing appointments"""
        # Check permissions
        is_admin, is_global_admin, alliance_ids = await self.get_admin_permissions(interaction.user.id)
        
        if is_global_admin:
            # Count all appointments
            self.svs_cursor.execute("SELECT COUNT(*) FROM appointments WHERE appointment_type=?", (activity_name,))
            count = self.svs_cursor.fetchone()[0]
            
            embed = discord.Embed(
                title=f"{theme.warnIcon} Clear All Appointments",
                description=f"Are you sure you want to clear **ALL {count} appointments** for {activity_name}?\n\nThis action cannot be undone.",
                color=theme.emColor2
            )
        else:
            # Count appointments for allowed alliances
            if not alliance_ids:
                await interaction.response.send_message(f"{theme.deniedIcon} You don't have permission to clear appointments.", ephemeral=True)
                return
            
            placeholders = ','.join('?' for _ in alliance_ids)
            query = f"SELECT COUNT(*) FROM appointments WHERE appointment_type=? AND alliance IN ({placeholders})"
            self.svs_cursor.execute(query, [activity_name] + alliance_ids)
            count = self.svs_cursor.fetchone()[0]
            
            embed = discord.Embed(
                title=f"{theme.warnIcon} Clear Alliance Appointments",
                description=f"Are you sure you want to clear **{count} appointments** for your alliance(s) in {activity_name}?\n\nThis action cannot be undone.",
                color=theme.emColor2
            )
        
        view = ClearConfirmationView(self.bot, self, activity_name, is_global_admin, alliance_ids)
        
        await safe_edit_message(interaction, embed=embed, view=view)

    async def update_channel_message(self, activity_name: str):
        """Update the channel message with current available slots"""
        try:
            minister_schedule_cog = self.bot.get_cog("MinisterSchedule")
            if not minister_schedule_cog:
                return

            # Get current booked times
            self.svs_cursor.execute("SELECT time, fid, manual_name, alliance FROM appointments WHERE appointment_type=?", (activity_name,))
            booked_times = {row[0]: (row[1], row[2], row[3]) for row in self.svs_cursor.fetchall()}

            # Generate time list
            list_type = await minister_schedule_cog.get_channel_id("list type")
            if list_type == 3:
                time_list, _ = minister_schedule_cog.generate_time_list(booked_times)
                message_content = f"**{activity_name}** slots:\n" + "\n".join(time_list)
            elif list_type == 2:
                time_list = minister_schedule_cog.generate_booked_time_list(booked_times)
                message_content = f"**{activity_name}** booked slots:\n" + "\n".join(time_list)
            else:
                time_list = minister_schedule_cog.generate_available_time_list(booked_times)
                available_slots = len(time_list) > 0
                message_content = f"**{activity_name}** available slots:\n" + "\n".join(
                    time_list) if available_slots else f"All appointment slots are filled for {activity_name}"

            context = f"{activity_name}"
            channel_context = f"{activity_name} channel"

            # Get channel
            channel_id = await minister_schedule_cog.get_channel_id(channel_context)
            if channel_id:
                log_guild = await minister_schedule_cog.get_log_guild(None)
                if log_guild:
                    channel = log_guild.get_channel(channel_id)
                    if channel:
                        await minister_schedule_cog.get_or_create_message(context, message_content, channel)

        except Exception as e:
            print(f"Error updating channel message: {e}")
    
    async def show_clear_channels_selection(self, interaction: discord.Interaction):
        """Show channel selection menu for clearing configurations"""
        class ClearChannelsConfirmView(discord.ui.View):
            def __init__(self, parent_cog):
                super().__init__(timeout=7200)
                self.parent_cog = parent_cog
                
            @discord.ui.select(
                placeholder="Select channels to clear...",
                options=[
                    discord.SelectOption(label="Appointment Channel", value="Appointment", emoji=theme.crownIcon),
                    discord.SelectOption(label="Auto Schedule Channel", value="Auto Schedule", emoji=theme.robotIcon),
                    discord.SelectOption(label="Log Channel", value="minister log", emoji=theme.documentIcon),
                    discord.SelectOption(label="All Channels", value="ALL", emoji=theme.trashIcon, description="Clear all channel configurations")
                ],
                min_values=1,
                max_values=4
            )
            async def select_channels(self, interaction: discord.Interaction, select: discord.ui.Select):
                try:
                    await interaction.response.defer()

                    cleared_channels = []
                    with closing(sqlite3.connect("db/svs.sqlite")) as svs_conn:
                        svs_cursor = svs_conn.cursor()

                        for value in select.values:
                            if value == "ALL":
                                # Clear the minister channels
                                await self._clear_channel_config(svs_cursor, "Appointment", interaction.guild)
                                cleared_channels.append("Appointment channel")

                                await self._clear_channel_config(svs_cursor, "Auto Schedule", interaction.guild)
                                cleared_channels.append("Auto Schedule channel")

                                # Clear log channel
                                svs_cursor.execute("DELETE FROM reference WHERE context=?", ("minister log channel",))
                                cleared_channels.append("Log channel")
                            else:
                                if value == "minister log":
                                    svs_cursor.execute("DELETE FROM reference WHERE context=?", ("minister log channel",))
                                    cleared_channels.append("Log channel")
                                else:
                                    await self._clear_channel_config(svs_cursor, value, interaction.guild)
                                    cleared_channels.append(f"{value} channel")

                        svs_conn.commit()

                    # Show success message
                    success_message = "Successfully cleared the following configurations:\n" + "\n".join([f"• {ch}" for ch in cleared_channels])
                    
                    # Return to settings menu with success message
                    embed = discord.Embed(
                        title="⚙️ Minister Settings",
                        description=(
                            f"{theme.verifiedIcon} **{success_message}**\n\n"
                            f"Administrative settings for minister scheduling:\n\n"
                            f"Available Actions\n"
                            f"{theme.upperDivider}\n\n"
                            f"{theme.editListIcon} **Update Names**\n"
                            f"└ Manually set booked ministers' names\n\n"
                            f"{theme.listIcon} **Schedule List Type**\n"
                            f"└ Change the type of schedule list message when adding/removing people\n\n"
                            f"{theme.calendarIcon} **Delete All Reservations**\n"
                            f"└ Clear appointments for a specific day\n\n"
                            f"{theme.announceIcon} **Clear Channels**\n"
                            f"└ Clear channel configurations\n\n"
                            f"{theme.fidIcon} **Delete Server ID**\n"
                            f"└ Remove configured server from database\n\n"
                            f"{theme.lowerDivider}"
                        ),
                        color=theme.emColor3
                    )
                    
                    view = MinisterSettingsView(self.parent_cog.bot, self.parent_cog, is_global=True)
                    await interaction.followup.edit_message(
                        message_id=interaction.message.id,
                        embed=embed,
                        view=view
                    )

                except Exception as e:
                    await interaction.followup.send(f"{theme.deniedIcon} Error clearing channels: {e}", ephemeral=True)
            
            async def _clear_channel_config(self, svs_cursor, activity_name, guild):
                """Clear channel configuration and delete associated message - preserves appointment records"""
                # Get the channel and message IDs
                channel_context = f"{activity_name} channel"
                svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", (channel_context,))
                channel_row = svs_cursor.fetchone()
                
                if channel_row and guild:
                    channel_id = int(channel_row[0])
                    channel = guild.get_channel(channel_id)
                    
                    # Get the message ID
                    svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", (activity_name,))
                    message_row = svs_cursor.fetchone()
                    
                    if message_row and channel:
                        message_id = int(message_row[0])
                        try:
                            message = await channel.fetch_message(message_id)
                            await message.delete()
                        except Exception:
                            pass  # Message might already be deleted
                    
                    # Delete the message reference
                    svs_cursor.execute("DELETE FROM reference WHERE context=?", (activity_name,))
                
                # Delete the channel reference
                svs_cursor.execute("DELETE FROM reference WHERE context=?", (channel_context,))
                # NOTE: We do NOT delete appointment records - only channel configuration
            
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji=f"{theme.deniedIcon}")
            async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await self.parent_cog.show_settings_menu(interaction)
        
        embed = discord.Embed(
            title="🗑️ Clear Channel Configurations",
            description="Select which channel configurations you want to clear.\n\n**Warning:** This will remove the channel configuration and delete any existing appointment messages in those channels.\n\n**Note:** Appointment records will be preserved.",
            color=theme.emColor2
        )
        
        await interaction.response.edit_message(embed=embed, view=ClearChannelsConfirmView(self))
    
    async def show_settings_menu(self, interaction: discord.Interaction):
        """Show the minister settings menu"""
        _, is_global, _ = await self.get_admin_permissions(interaction.user.id)
        embed = discord.Embed(
            title=f"{theme.settingsIcon} Minister Settings",
            description=(
                f"Administrative settings for minister scheduling:\n\n"
                f"Available Actions\n"
                f"{theme.upperDivider}\n\n"
                f"{theme.editListIcon} **Update Names**\n"
                f"└ Manually set booked ministers' names\n\n"
                f"{theme.listIcon} **Schedule List Type**\n"
                f"└ Change the type of schedule list message when adding/removing people\n\n"
                f"{theme.timeIcon} **Time Slot Mode**\n"
                f"└ Toggle between standard (00:00/00:30) and offset (00:00/00:15/00:45) time slots\n\n"
                f"{theme.calendarIcon} **Delete All Reservations**\n"
                f"└ Clear appointments for a specific day\n\n"
                f"{theme.announceIcon} **Clear Channels**\n"
                f"└ Clear channel configurations\n\n"
                f"{theme.fidIcon} **Delete Server ID**\n"
                f"└ Remove configured server from database\n\n"
                f"{theme.lowerDivider}"
            ),
            color=theme.emColor1
        )

        view = MinisterSettingsView(self.bot, self, is_global)
        await safe_edit_message(interaction, embed=embed, view=view, content=None)

    async def show_activity_selection_for_update(self, interaction: discord.Interaction):
        """Update names for the Appointment schedule (only one activity now, so no selector needed)."""
        await self.update_minister_names(interaction, "Appointment")

    async def show_activity_selection_for_clear(self, interaction: discord.Interaction):
        """Clear reservations for the Appointment schedule (only one activity now, so no selector needed)."""

        minister_schedule_cog = self.bot.get_cog("MinisterSchedule")
        if not minister_schedule_cog:
            await interaction.followup.send("Couldn't load minister_schedule.py cog")
            return

        log_guild = await minister_schedule_cog.get_log_guild(interaction.guild)
        if not log_guild:
            await interaction.response.send_message(
                "Could not find the minister log server. Make sure the bot is in that server.\n\nIf issue persists, run the `/settings` command --> Other Features --> Minister Scheduling --> Delete Server ID and try again in the desired server",
                ephemeral=True
            )
            return

        log_channel_id = await minister_schedule_cog.get_channel_id("minister log channel")
        log_channel = log_guild.get_channel(log_channel_id)

        if not log_channel:
            await interaction.response.send_message(
                f"[Warning] Could not find a log channel. Log channel is needed before clearing the appointment \n\nRun the `/settings` command --> Other Features --> Minister Scheduling --> Channel Setup and choose a log channel", ephemeral=True)
            return

        await self.show_clear_confirmation(interaction, "Appointment")

    async def show_time_slot_mode_menu(self, interaction: discord.Interaction):
        """Show time slot mode selection menu"""
        self.svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", ("slot_mode",))
        row = self.svs_cursor.fetchone()
        current_mode = int(row[0]) if row else 0

        mode_labels = {
            0: "Standard (00:00, 00:30, 01:00...)",
            1: "Offset (00:00, 00:15, 00:45, 01:15...)"
        }
        current_label = mode_labels[current_mode]

        embed = discord.Embed(
            title=f"{theme.timeIcon} Time Slot Mode",
            description=(
                f"**Current Mode:** {current_label}\n\n"
                "**Mode 0 (Standard):**\n"
                "└ 48 slots: 00:00, 00:30, 01:00, 01:30... 23:30\n"
                "└ Each slot is 30 minutes\n\n"
                "**Mode 1 (Offset):**\n"
                "└ 48 slots: 00:00 (15min), 00:15, 00:45, 01:15... 23:45 (15min to midnight)\n"
                "└ First slot: 00:00-00:15 (15 min)\n"
                "└ Middle slots: 30 min each\n"
                "└ Last slot: 23:45-00:00 (15 min, ends at daily reset)\n\n"
                f"{theme.warnIcon} **Warning:** Changing modes will automatically migrate all existing reservations to the new time slots."
            ),
            color=theme.emColor1
        )

        view = discord.ui.View(timeout=60)

        select = discord.ui.Select(
            placeholder="Choose a time slot mode:",
            options=[
                discord.SelectOption(label="Standard", description="00:00, 00:30, 01:00... (30min slots)", value="0"),
                discord.SelectOption(label="Offset", description="00:00, 00:15, 00:45... (offset 15min)", value="1")
            ]
        )

        async def select_callback(interaction: discord.Interaction):
            new_mode = int(select.values[0])

            if new_mode == current_mode:
                await interaction.response.send_message(f"{theme.infoIcon} Already using this mode.", ephemeral=True)
                return

            # Migrate reservations
            await self.migrate_time_slots(interaction, current_mode, new_mode)

        select.callback = select_callback
        view.add_item(select)

        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.primary, emoji=f"{theme.backIcon}")

        async def back_callback(interaction: discord.Interaction):
            await self.show_settings_menu(interaction)

        back_button.callback = back_callback
        view.add_item(back_button)
        await safe_edit_message(interaction, embed=embed, view=view, content=None)

    async def migrate_time_slots(self, interaction: discord.Interaction, old_mode: int, new_mode: int):
        """Migrate all reservations from old mode to new mode"""
        try:
            await interaction.response.defer()

            # Get all appointments (by surrogate id, so this also covers portal-assigned
            # manual_name rows which have no fid to key an UPDATE off of)
            self.svs_cursor.execute("SELECT id, fid, appointment_type, time, alliance FROM appointments")
            appointments = self.svs_cursor.fetchall()

            if not appointments:
                # No appointments to migrate, just update mode
                self.svs_cursor.execute("UPDATE reference SET context_id=? WHERE context=?", (new_mode, "slot_mode"))
                self.svs_conn.commit()

                embed = discord.Embed(
                    title=f"{theme.verifiedIcon} Time Slot Mode Updated",
                    description=f"Successfully switched to **Mode {new_mode}** (no reservations to migrate).",
                    color=theme.emColor3
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                await self.show_settings_menu(interaction)
                return

            # Build migration mapping
            migrations = []
            for appt_id, fid, appointment_type, old_time, alliance in appointments:
                new_time = self.convert_time_slot(old_time, old_mode, new_mode)
                migrations.append((appt_id, new_time))

            # Update database atomically
            for appt_id, new_time in migrations:
                self.svs_cursor.execute(
                    "UPDATE appointments SET time=? WHERE id=?",
                    (new_time, appt_id)
                )

            # Update slot mode
            self.svs_cursor.execute("UPDATE reference SET context_id=? WHERE context=?", (new_mode, "slot_mode"))
            self.svs_conn.commit()

            # Log to minister log channel and change history
            minister_schedule_cog = self.bot.get_cog("MinisterSchedule")
            if minister_schedule_cog:
                migration_text = "\n".join([f"`{old}` → `{new}` - {atype}" for _, atype, old, new, _ in migrations[:20]])
                if len(migrations) > 20:
                    migration_text += f"\n... and {len(migrations) - 20} more"

                embed = discord.Embed(
                    title=f"Time Slot Mode Changed: Mode {old_mode} → Mode {new_mode}",
                    description=f"**Migrated {len(migrations)} reservations:**\n\n{migration_text}",
                    color=discord.Color.orange()
                )
                embed.set_author(name=f"Changed by {interaction.user.display_name}",
                               icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
                await minister_schedule_cog.send_embed_to_channel(embed)

                # Log the time slot mode change
                import json
                additional_data = json.dumps({
                    "old_mode": old_mode,
                    "new_mode": new_mode,
                    "migrations_count": len(migrations)
                })
                await minister_schedule_cog.log_change(
                    action_type="time_slot_mode_change",
                    user=interaction.user,
                    appointment_type=None,
                    fid=None,
                    nickname=None,
                    old_time=None,
                    new_time=None,
                    alliance_name=None,
                    additional_data=additional_data
                )

                # Update the channel message
                await self.update_channel_message("Appointment")

            # Show success
            mode_labels = {0: "Standard", 1: "Offset"}
            embed = discord.Embed(
                title=f"{theme.verifiedIcon} Time Slot Mode Updated",
                description=f"Successfully switched to **{mode_labels[new_mode]}** mode.\n\n{len(migrations)} reservations were migrated.",
                color=theme.emColor3
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            await self.show_settings_menu(interaction)

        except Exception as e:
            await interaction.followup.send(f"{theme.deniedIcon} Error migrating time slots: {e}", ephemeral=True)

    def convert_time_slot(self, time_str: str, old_mode: int, new_mode: int) -> str:
        """Convert a time slot from old mode to new mode"""
        hour, minute = map(int, time_str.split(":"))
        total_minutes = hour * 60 + minute

        if old_mode == 0 and new_mode == 1:
            # Standard → Offset
            if total_minutes == 0:
                return "00:00"
            new_minutes = total_minutes - 15
            new_hour = new_minutes // 60
            new_min = new_minutes % 60
            return f"{new_hour:02}:{new_min:02}"
        elif old_mode == 1 and new_mode == 0:
            # Offset → Standard
            # Special case: 23:45 → 23:30 (no 00:00 available as it's first slot)
            if total_minutes == 0:
                return "00:00"
            if time_str == "23:45":
                return "23:30"
            new_minutes = total_minutes + 15
            new_hour = new_minutes // 60
            new_min = new_minutes % 60
            return f"{new_hour:02}:{new_min:02}"

        return time_str

    async def show_activity_selection_for_list_type(self, interaction: discord.Interaction):
        """Show activity selection for changing the list type"""

        self.svs_cursor.execute("SELECT context_id FROM reference WHERE context=?", ("list type",))
        row = self.svs_cursor.fetchone()
        current_value = row[0]

        labels = {1: "Available", 2: "Booked", 3: "All"}
        current_label = labels[current_value]

        embed = discord.Embed(
            title=f"{theme.copyIcon} Schedule List Type",
            description=f"Select the type of generated minister list message when adding/removing people:\n\n**Currently showing:** {current_label}",
            color=theme.emColor3
        )

        view = discord.ui.View(timeout=60)

        select = discord.ui.Select(
            placeholder=f"Choose a schedule list type:",
            options=[
                discord.SelectOption(label="Available", description="Show only available slots", value="1"),
                discord.SelectOption(label="Booked", description="Show only booked slots", value="2"),
                discord.SelectOption(label="All", description="Show all slots", value="3")
            ]
        )

        async def select_callback(interaction: discord.Interaction):
            value = int(select.values[0])

            self.svs_cursor.execute(
                "UPDATE reference SET context_id=? WHERE context=?", (value, "list type")
            )
            self.svs_conn.commit()

            updated_embed = discord.Embed(
                title=f"{theme.copyIcon} Schedule List Type",
                description=f"{theme.verifiedIcon} Schedule list type updated successfully!\n\n**Now showing:** {labels[value]}\n\nNew changes will take effect when you add/remove a person to/from the minister schedule.",
                color=theme.emColor3
            )

            await interaction.response.edit_message(
                content=None,
                embed=updated_embed,
                view=view
            )

        select.callback = select_callback
        view.add_item(select)

        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.primary, emoji=f"{theme.backIcon}")

        async def back_callback(interaction: discord.Interaction):
            await self.show_settings_menu(interaction)

        back_button.callback = back_callback
        view.add_item(back_button)
        await safe_edit_message(interaction, embed=embed, view=view, content=None)

async def setup(bot):
    await bot.add_cog(MinisterMenu(bot))
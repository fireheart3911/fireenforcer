import datetime
import re

import discord
from discord.ext import commands
from discord import app_commands

import storage as store
import config


# ---------------------------------------------------------------------------
# Storage defaults
# ---------------------------------------------------------------------------

def _signup_data() -> dict:
    sd = store.storage.setdefault("signup", {})
    sd.setdefault("open", False)
    sd.setdefault("teams_enabled", False)
    sd.setdefault("message_id", None)
    sd.setdefault("channel_id", None)
    sd.setdefault("title", "Sign Up")
    sd.setdefault("description", "Click the button below to register!")
    return sd


def _signup_embed() -> discord.Embed:
    sd = _signup_data()
    is_open = sd.get("open", False)
    embed = discord.Embed(
        title=sd.get("title", "Sign Up"),
        description=sd.get("description", "Click the button below to register!"),
        color=discord.Color.green() if is_open else discord.Color.red(),
        timestamp=datetime.datetime.now(),
    )
    embed.add_field(name="Status", value="🟢 **OPEN**" if is_open else "🔴 **CLOSED**", inline=True)
    embed.add_field(name="Teams", value="Enabled" if sd.get("teams_enabled") else "Disabled", inline=True)
    if not is_open:
        embed.set_footer(text="Signups are currently closed")
    return embed


# ---------------------------------------------------------------------------
# Views / modal
# ---------------------------------------------------------------------------

class SignupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        is_open = _signup_data().get("open", False)
        button = discord.ui.Button(
            label="Sign Up",
            style=discord.ButtonStyle.green if is_open else discord.ButtonStyle.red,
            emoji="✍️",
            custom_id="signup:register",
            disabled=not is_open,
        )
        button.callback = self.signup_callback
        self.add_item(button)

    async def signup_callback(self, interaction: discord.Interaction):
        if not _signup_data().get("open", False):
            return await interaction.response.send_message("❌ Signups are currently closed.", ephemeral=True)

        signup_role = interaction.guild.get_role(config.SIGNUP_ROLE_ID)
        if signup_role and signup_role in interaction.user.roles:
            return await interaction.response.send_message("❌ You have already signed up!", ephemeral=True)

        await interaction.response.send_modal(SignupModal(_signup_data().get("teams_enabled", False)))


class SignupModal(discord.ui.Modal):
    def __init__(self, teams_enabled: bool = False):
        super().__init__(title="Sign Up")
        self.teams_enabled = teams_enabled
        self.username_input = discord.ui.TextInput(
            label="Username", style=discord.TextStyle.short,
            placeholder="Enter your in-game username...", required=True, max_length=32,
        )
        self.add_item(self.username_input)
        if teams_enabled:
            self.team_input = discord.ui.TextInput(
                label="Team Name", style=discord.TextStyle.short,
                placeholder="Enter your team name...", required=True, max_length=50,
            )
            self.add_item(self.team_input)

    async def on_submit(self, interaction: discord.Interaction):
        username = self.username_input.value.strip()
        team_name = self.team_input.value.strip() if self.teams_enabled else None

        signup_role = interaction.guild.get_role(config.SIGNUP_ROLE_ID)
        if signup_role:
            try:
                await interaction.user.add_roles(signup_role, reason="Signed up")
            except discord.Forbidden:
                return await interaction.response.send_message(
                    "❌ I don't have permission to assign roles.", ephemeral=True)

        # Set a [Player] prefix if the user has no bracket prefix already.
        current_nick = interaction.user.display_name
        if not re.match(r"^\[.+\]", current_nick):
            try:
                await interaction.user.edit(nick=f"[Player] {username}")
            except discord.Forbidden:
                pass

        log_channel = interaction.guild.get_channel(config.SIGNUP_LOG_CHANNEL_ID)
        if log_channel:
            embed = discord.Embed(title="New Signup", color=discord.Color.green(),
                                  timestamp=datetime.datetime.now())
            embed.add_field(name="Discord", value=interaction.user.mention, inline=True)
            embed.add_field(name="Username", value=username, inline=True)
            if team_name:
                embed.add_field(name="Team", value=team_name, inline=True)
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            await log_channel.send(embed=embed)

        response = f"✅ You have signed up as **{username}**"
        if team_name:
            response += f" on team **{team_name}**"
        await interaction.response.send_message(response + "!", ephemeral=True)


async def update_signup_embed(client: commands.Bot):
    sd = _signup_data()
    if not sd.get("message_id") or not sd.get("channel_id"):
        return
    try:
        channel = client.get_channel(int(sd["channel_id"]))
        if not channel:
            return
        message = await channel.fetch_message(int(sd["message_id"]))
        await message.edit(embed=_signup_embed(), view=SignupView())
    except discord.NotFound:
        print("Signup message not found, clearing stored ID")
        sd["message_id"] = None
        sd["channel_id"] = None
        store.save_data()


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class SignupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = app_commands.Group(
            name="signup", description="Signup management commands", guild_ids=[config.GUILD_ID]
        )
        self._register()
        bot.tree.add_command(self.group)

    def _register(self):
        group = self.group

        @group.command(name="create", description="Send the signup panel to the current channel")
        @app_commands.checks.has_permissions(administrator=True)
        async def signup_create(interaction: discord.Interaction):
            msg = await interaction.channel.send(embed=_signup_embed(), view=SignupView())
            sd = _signup_data()
            sd["message_id"] = str(msg.id)
            sd["channel_id"] = str(interaction.channel.id)
            store.save_data()
            await interaction.response.send_message("Signup panel created successfully!", ephemeral=True)

        @group.command(name="open", description="Open signups")
        @app_commands.checks.has_permissions(administrator=True)
        async def signup_open(interaction: discord.Interaction):
            _signup_data()["open"] = True
            store.save_data()
            await update_signup_embed(self.bot)
            await interaction.response.send_message("Signups are now **OPEN**!", ephemeral=True)

        @group.command(name="close", description="Close signups")
        @app_commands.checks.has_permissions(administrator=True)
        async def signup_close(interaction: discord.Interaction):
            _signup_data()["open"] = False
            store.save_data()
            await update_signup_embed(self.bot)
            await interaction.response.send_message("Signups are now **CLOSED**!", ephemeral=True)

        @group.command(name="teams", description="Enable or disable team signups")
        @app_commands.describe(enabled="Whether teams are required for signup")
        @app_commands.checks.has_permissions(administrator=True)
        async def signup_teams(interaction: discord.Interaction, enabled: bool):
            _signup_data()["teams_enabled"] = enabled
            store.save_data()
            await update_signup_embed(self.bot)
            await interaction.response.send_message(
                f"Team signups are now **{'enabled' if enabled else 'disabled'}**", ephemeral=True)

        @group.command(name="flavor", description="Update the signup panel title and description")
        @app_commands.describe(title="The title of the signup embed (optional)",
                               description="The description of the signup embed (optional)")
        @app_commands.checks.has_permissions(administrator=True)
        async def signup_flavor(interaction: discord.Interaction, title: str = None, description: str = None):
            sd = _signup_data()
            if title is None and description is None:
                return await interaction.response.send_message(
                    f"**Current Signup Flavor:**\n• Title: `{sd.get('title')}`\n• Description: `{sd.get('description')}`",
                    ephemeral=True)
            if title is not None:
                sd["title"] = title
            if description is not None:
                sd["description"] = description
            store.save_data()
            await update_signup_embed(self.bot)
            resp = "Signup flavor updated!"
            if title is not None:
                resp += f"\n• Title: `{title}`"
            if description is not None:
                resp += f"\n• Description: `{description}`"
            await interaction.response.send_message(resp, ephemeral=True)

        @group.command(name="reset", description="Move all signed-up users to a new role and remove signup role")
        @app_commands.describe(new_role="Optional role to grant users (leave empty to just remove signup role)")
        @app_commands.checks.has_permissions(administrator=True)
        async def signup_reset(interaction: discord.Interaction, new_role: discord.Role = None):
            await interaction.response.defer(ephemeral=True)
            signup_role = interaction.guild.get_role(config.SIGNUP_ROLE_ID)
            if not signup_role:
                return await interaction.followup.send("❌ Signup role not found!", ephemeral=True)
            members = [m for m in interaction.guild.members if signup_role in m.roles]
            if not members:
                return await interaction.followup.send("❌ No members have the signup role!", ephemeral=True)

            ok = fail = 0
            for member in members:
                try:
                    if new_role:
                        await member.add_roles(new_role, reason="Signup reset")
                    await member.remove_roles(signup_role, reason="Signup reset")
                    ok += 1
                except discord.Forbidden:
                    fail += 1

            resp = f"✅ Reset complete! Processed **{ok}** member(s)."
            if new_role:
                resp += f"\n• Granted: {new_role.mention}"
            resp += f"\n• Removed: {signup_role.mention}"
            if fail:
                resp += f"\n⚠️ Failed to process **{fail}** member(s) (permission issues)."
            await interaction.followup.send(resp, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SignupCog(bot))
    return SignupCog
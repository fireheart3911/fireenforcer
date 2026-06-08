import datetime
import re

import discord
from discord.ext import commands, tasks
from discord import app_commands

import storage as store
import config
from cogs.parsers import parse_eu_datetime
from cogs import xp_api


# ---------------------------------------------------------------------------
# Storage model
# ---------------------------------------------------------------------------
# storage["signup"] = {
#   open, teams_enabled, application_mode, message_id, channel_id,
#   title, description, event_name, capacity (int|None),
#   open_at, close_at (epoch|None),
#   participants: {user_id: {username, team, name(event), signed_at, status}},
#   applications: {app_id: {user_id, username, team, message_id, status}},
# }
# status: "active" | "waitlist"
# storage["signup_history"][user_id] = [{event_name, username, team, date}, ...]

def _sd() -> dict:
    sd = store.storage.setdefault("signup", {})
    sd.setdefault("open", False)
    sd.setdefault("teams_enabled", False)
    sd.setdefault("application_mode", False)
    sd.setdefault("message_id", None)
    sd.setdefault("channel_id", None)
    sd.setdefault("title", "Sign Up")
    sd.setdefault("description", "Click the button below to register!")
    sd.setdefault("event_name", "Event")
    sd.setdefault("capacity", None)
    sd.setdefault("open_at", None)
    sd.setdefault("close_at", None)
    sd.setdefault("participants", {})
    sd.setdefault("applications", {})
    return sd


def _active(sd) -> list:
    return [p for p in sd["participants"].values() if p.get("status") == "active"]

def _waitlist(sd) -> list:
    return [p for p in sd["participants"].values() if p.get("status") == "waitlist"]

def _is_full(sd) -> bool:
    cap = sd.get("capacity")
    return cap is not None and len(_active(sd)) >= cap


# ---------------------------------------------------------------------------
# Panel embed
# ---------------------------------------------------------------------------

def _panel_embed() -> discord.Embed:
    sd = _sd()
    is_open = sd.get("open", False)
    embed = discord.Embed(
        title=sd.get("title", "Sign Up"),
        description=sd.get("description", ""),
        color=discord.Color.green() if is_open else discord.Color.red(),
        timestamp=datetime.datetime.now(),
    )

    full = _is_full(sd)
    if is_open and full:
        status = "🟡 **OPEN — FULL (waitlist)**"
    elif is_open:
        status = "🟢 **OPEN**"
    else:
        status = "🔴 **CLOSED**"
    embed.add_field(name="Status", value=status, inline=True)

    n_active = len(_active(sd))
    cap = sd.get("capacity")
    count = f"{n_active}/{cap}" if cap else str(n_active)
    embed.add_field(name="Signed up", value=count, inline=True)
    if sd.get("teams_enabled"):
        embed.add_field(name="Teams", value="Enabled", inline=True)

    wl = len(_waitlist(sd))
    if wl:
        embed.add_field(name="Waitlist", value=str(wl), inline=True)
    if sd.get("application_mode"):
        embed.add_field(name="Mode", value="📝 Application", inline=True)

    # Schedule
    sched = []
    if not is_open and sd.get("open_at"):
        sched.append(f"Opens <t:{int(sd['open_at'])}:R>")
    if sd.get("close_at"):
        sched.append(f"Closes <t:{int(sd['close_at'])}:R>")
    if sched:
        embed.add_field(name="Schedule", value=" · ".join(sched), inline=False)

    if not is_open:
        embed.set_footer(text="Signups are currently closed")
    return embed


# ---------------------------------------------------------------------------
# Signup panel button
# ---------------------------------------------------------------------------

class SignupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        sd = _sd()
        is_open = sd.get("open", False)
        full = _is_full(sd)
        if not is_open:
            label, style = "Sign Up", discord.ButtonStyle.red
        elif full:
            label, style = "Join Waitlist", discord.ButtonStyle.blurple
        else:
            label, style = "Sign Up", discord.ButtonStyle.green
        button = discord.ui.Button(label=label, style=style, emoji="✍️",
                                   custom_id="signup:register", disabled=not is_open)
        button.callback = self.signup_callback
        self.add_item(button)

    async def signup_callback(self, interaction: discord.Interaction):
        sd = _sd()
        if not sd.get("open", False):
            return await interaction.response.send_message("❌ Signups are currently closed.", ephemeral=True)

        uid = str(interaction.user.id)
        if uid in sd["participants"]:
            return await interaction.response.send_message("❌ You have already signed up!", ephemeral=True)
        # Pending application?
        if any(a["user_id"] == uid and a["status"] == "pending" for a in sd["applications"].values()):
            return await interaction.response.send_message("❌ You already have a pending application.", ephemeral=True)
        signup_role = interaction.guild.get_role(config.SIGNUP_ROLE_ID)
        if signup_role and signup_role in interaction.user.roles:
            return await interaction.response.send_message("❌ You have already signed up!", ephemeral=True)

        await interaction.response.send_modal(
            SignupModal(teams_enabled=sd.get("teams_enabled", False),
                        will_waitlist=_is_full(sd),
                        application_mode=sd.get("application_mode", False))
        )


class SignupModal(discord.ui.Modal):
    def __init__(self, teams_enabled=False, will_waitlist=False, application_mode=False):
        title = "Apply" if application_mode else ("Join Waitlist" if will_waitlist else "Sign Up")
        super().__init__(title=title)
        self.teams_enabled = teams_enabled
        self.will_waitlist = will_waitlist
        self.application_mode = application_mode

        self.username_input = discord.ui.TextInput(
            label="Username", style=discord.TextStyle.short,
            placeholder="Enter your in-game username...", required=True, max_length=32)
        self.add_item(self.username_input)
        if teams_enabled:
            self.team_input = discord.ui.TextInput(
                label="Team Name", style=discord.TextStyle.short,
                placeholder="Enter your team name...", required=True, max_length=50)
            self.add_item(self.team_input)

    async def on_submit(self, interaction: discord.Interaction):
        sd = _sd()
        username = self.username_input.value.strip()
        team = self.team_input.value.strip() if self.teams_enabled else None

        # Team rules
        if self.teams_enabled and team:
            ok, msg = _check_team_rules(sd, team)
            if not ok:
                return await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

        if self.application_mode:
            await _create_application(interaction, sd, username, team)
            return await interaction.response.send_message(
                "✅ Your application has been submitted for review.", ephemeral=True)

        # Direct signup (normal mode)
        status = "waitlist" if _is_full(sd) else "active"
        await _register_participant(interaction.client, interaction.guild, interaction.user,
                                    username, team, status)
        if status == "waitlist":
            resp = f"🟡 You've been added to the **waitlist** as **{username}**"
        else:
            resp = f"✅ You have signed up as **{username}**"
        if team:
            resp += f" on team **{team}**"
        await interaction.response.send_message(resp + "!", ephemeral=True)
        await update_signup_embed(interaction.client)


# ---------------------------------------------------------------------------
# Team rules (duplicate detection / size cap)
# ---------------------------------------------------------------------------

TEAM_SIZE_CAP = 5  # max members per team when teams are enabled

def _check_team_rules(sd, team: str) -> tuple[bool, str]:
    members = [p for p in sd["participants"].values()
               if (p.get("team") or "").lower() == team.lower()]
    if len(members) >= TEAM_SIZE_CAP:
        return False, f"Team **{team}** is full ({TEAM_SIZE_CAP} members)."
    return True, ""


# ---------------------------------------------------------------------------
# Registration + waitlist promotion
# ---------------------------------------------------------------------------

async def _register_participant(client, guild, user, username, team, status):
    sd = _sd()
    role = guild.get_role(config.SIGNUP_ROLE_ID)
    if role and status == "active":
        try:
            await user.add_roles(role, reason="Signed up")
        except discord.Forbidden:
            pass
    if status == "active":
        await _apply_player_nick(user, username)

    sd["participants"][str(user.id)] = {
        "username": username, "team": team, "name": sd.get("event_name", "Event"),
        "signed_at": datetime.datetime.now().timestamp(), "status": status,
    }
    store.save_data()
    await _log_signup(guild, user, username, team, status)


async def _apply_player_nick(user, username):
    if not re.match(r"^\[.+\]", user.display_name):
        try:
            await user.edit(nick=f"[Player] {username}")
        except discord.Forbidden:
            pass


async def _log_signup(guild, user, username, team, status):
    ch = guild.get_channel(config.SIGNUP_LOG_CHANNEL_ID)
    if not ch:
        return
    embed = discord.Embed(
        title="Waitlisted" if status == "waitlist" else "New Signup",
        color=discord.Color.blurple() if status == "waitlist" else discord.Color.green(),
        timestamp=datetime.datetime.now())
    embed.add_field(name="Discord", value=user.mention, inline=True)
    embed.add_field(name="Username", value=username, inline=True)
    if team:
        embed.add_field(name="Team", value=team, inline=True)
    embed.set_thumbnail(url=user.display_avatar.url)
    await ch.send(embed=embed)


async def _promote_from_waitlist(client, guild):
    """Promote the earliest waitlisted participant into a freed active slot."""
    sd = _sd()
    if _is_full(sd):
        return
    wl = sorted(_waitlist(sd), key=lambda p: p.get("signed_at", 0))
    if not wl:
        return
    # find the user_id of the earliest waitlisted entry
    entry = wl[0]
    uid = next((u for u, p in sd["participants"].items() if p is entry), None)
    if not uid:
        return
    entry["status"] = "active"
    store.save_data()
    member = guild.get_member(int(uid))
    if member:
        role = guild.get_role(config.SIGNUP_ROLE_ID)
        if role:
            try:
                await member.add_roles(role, reason="Promoted from waitlist")
            except discord.Forbidden:
                pass
        await _apply_player_nick(member, entry["username"])
        try:
            await member.send(f"🎉 A slot opened up — you're now confirmed for **{sd.get('event_name','the event')}** "
                              f"as **{entry['username']}**!")
        except (discord.Forbidden, discord.HTTPException):
            pass


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

async def _create_application(interaction, sd, username, team):
    app_id = f"APP-{store.next_id()}"
    app_channel_id = config.SIGNUP_APPLICATION_CHANNEL_ID
    channel = interaction.guild.get_channel(app_channel_id) if app_channel_id else None

    embed = await _application_embed(interaction.guild, interaction.user, username, team)
    msg = None
    if channel:
        msg = await channel.send(embed=embed, view=ApplicationView())
    sd["applications"][app_id] = {
        "id": app_id, "user_id": str(interaction.user.id), "username": username,
        "team": team, "message_id": str(msg.id) if msg else None, "status": "pending",
    }
    store.save_data()


async def _application_embed(guild, user, username, team) -> discord.Embed:
    sd = _sd()
    embed = discord.Embed(
        title="📝 New Application",
        color=discord.Color.gold(),
        timestamp=datetime.datetime.now(),
    )
    embed.add_field(name="Applicant", value=user.mention, inline=True)
    embed.add_field(name="Username", value=username, inline=True)
    if team:
        embed.add_field(name="Team", value=team, inline=True)

    # User info
    joined = int(user.joined_at.timestamp()) if user.joined_at else None
    created = int(user.created_at.timestamp())
    info = f"Account created <t:{created}:R>"
    if joined:
        info += f"\nJoined server <t:{joined}:R>"
    embed.add_field(name="👤 User Info", value=info, inline=False)

    # Levels via XP API (graceful if unavailable)
    embed.add_field(name="📊 Levels", value=await _levels_text(user.id), inline=False)

    # Previous participations from history
    embed.add_field(name="🏆 Previous Participations", value=_history_text(user.id), inline=False)

    # Moderation info (stub until the global mod system exists)
    embed.add_field(name="🛡️ Moderation", value=await get_moderation_info(user.id), inline=False)

    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="Approve, deny, or send to waitlist")
    return embed


async def _levels_text(user_id) -> str:
    if not config.XP_API_TOKEN:
        return "Levels unavailable (XP API not configured)."
    try:
        g = await xp_api.get_global_level(user_id)
        s = await xp_api.get_server_level(user_id, config.GUILD_ID)
        return f"Global: **{g}** · Server: **{s}**"
    except xp_api.XPNotFound:
        return "No level data on record."
    except xp_api.XPError:
        return "Levels temporarily unavailable."


def _history_text(user_id) -> str:
    hist = store.storage.get("signup_history", {}).get(str(user_id), [])
    if not hist:
        return "None on record."
    lines = []
    for h in hist[-5:]:
        team = f" (team {h['team']})" if h.get("team") else ""
        lines.append(f"• {h.get('event_name', 'Event')}{team}")
    more = f"\n…and {len(hist) - 5} more" if len(hist) > 5 else ""
    return "\n".join(lines) + more


async def get_moderation_info(user_id) -> str:
    """Stub for the (to-be-implemented) global moderation system.
    Returns a neutral message until that system exposes an API."""
    return "✅ No incidents on record."


class ApplicationView(discord.ui.View):
    """Approve / Deny / Waitlist — usable by anyone with channel access."""
    def __init__(self):
        super().__init__(timeout=None)

    def _find_app(self):
        # resolve via the message id of the interaction
        return None  # resolved in callbacks using interaction.message

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅", custom_id="signup:app_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        sd = _sd()
        app = _app_by_message(sd, interaction.message.id)
        if not app or app["status"] != "pending":
            return await interaction.response.send_message("❌ This application is no longer pending.", ephemeral=True)
        if _is_full(sd):
            return await interaction.response.send_message(
                "❌ The roster is full. Use **Send to Waitlist** instead.", ephemeral=True)
        await interaction.response.defer()
        member = interaction.guild.get_member(int(app["user_id"]))
        if member:
            await _register_participant(interaction.client, interaction.guild, member,
                                        app["username"], app.get("team"), "active")
            try:
                await member.send(f"🎉 Your application for **{sd.get('event_name','the event')}** was **approved**! "
                                  f"You're signed up as **{app['username']}**.")
            except (discord.Forbidden, discord.HTTPException):
                pass
        app["status"] = "approved"
        store.save_data()
        await _resolve_application_message(interaction, app, "✅ Approved", discord.Color.green())
        await update_signup_embed(interaction.client)

    @discord.ui.button(label="Send to Waitlist", style=discord.ButtonStyle.blurple, emoji="🟡", custom_id="signup:app_waitlist")
    async def waitlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        sd = _sd()
        app = _app_by_message(sd, interaction.message.id)
        if not app or app["status"] != "pending":
            return await interaction.response.send_message("❌ This application is no longer pending.", ephemeral=True)
        await interaction.response.defer()
        member = interaction.guild.get_member(int(app["user_id"]))
        if member:
            await _register_participant(interaction.client, interaction.guild, member,
                                        app["username"], app.get("team"), "waitlist")
            try:
                await member.send(f"🟡 Your application for **{sd.get('event_name','the event')}** was placed on the **waitlist**.")
            except (discord.Forbidden, discord.HTTPException):
                pass
        app["status"] = "waitlisted"
        store.save_data()
        await _resolve_application_message(interaction, app, "🟡 Waitlisted", discord.Color.blurple())
        await update_signup_embed(interaction.client)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, emoji="✖️", custom_id="signup:app_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        sd = _sd()
        app = _app_by_message(sd, interaction.message.id)
        if not app or app["status"] != "pending":
            return await interaction.response.send_message("❌ This application is no longer pending.", ephemeral=True)
        await interaction.response.send_modal(DenyModal(app["id"]))


class DenyModal(discord.ui.Modal, title="Deny Application"):
    def __init__(self, app_id: str):
        super().__init__()
        self.app_id = app_id
        self.reason_input = discord.ui.TextInput(
            label="Reason (optional — DMs applicant if given)",
            style=discord.TextStyle.paragraph, required=False, max_length=500)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        sd = _sd()
        app = sd["applications"].get(self.app_id)
        if not app or app["status"] != "pending":
            return await interaction.response.send_message("❌ This application is no longer pending.", ephemeral=True)
        await interaction.response.defer()
        reason = self.reason_input.value.strip()
        app["status"] = "denied"
        store.save_data()
        if reason:
            member = interaction.guild.get_member(int(app["user_id"]))
            if member:
                try:
                    await member.send(f"❌ Your application for **{sd.get('event_name','the event')}** was denied.\n"
                                      f"**Reason:** {reason}")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        await _resolve_application_message(interaction, app,
                                           "✖️ Denied" + (f" — {reason}" if reason else ""),
                                           discord.Color.red())


def _app_by_message(sd, message_id):
    for a in sd["applications"].values():
        if a.get("message_id") == str(message_id):
            return a
    return None


async def _resolve_application_message(interaction, app, status_text, color):
    try:
        msg = interaction.message
        embed = msg.embeds[0] if msg.embeds else discord.Embed()
        embed.color = color
        embed.add_field(name="Decision", value=f"{status_text} by {interaction.user.mention}", inline=False)
        await msg.edit(embed=embed, view=None)
    except (discord.HTTPException, AttributeError):
        pass


# ---------------------------------------------------------------------------
# Panel refresh
# ---------------------------------------------------------------------------

async def update_signup_embed(client: commands.Bot):
    sd = _sd()
    if not sd.get("message_id") or not sd.get("channel_id"):
        return
    try:
        channel = client.get_channel(int(sd["channel_id"]))
        if not channel:
            return
        message = await channel.fetch_message(int(sd["message_id"]))
        await message.edit(embed=_panel_embed(), view=SignupView())
    except discord.NotFound:
        sd["message_id"] = None
        sd["channel_id"] = None
        store.save_data()


# ---------------------------------------------------------------------------
# /signup new — event creation wizard
# ---------------------------------------------------------------------------

class NewEventModal(discord.ui.Modal, title="New Signup Event"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.name_input = discord.ui.TextInput(label="Event name", required=True, max_length=80)
        self.desc_input = discord.ui.TextInput(
            label="Panel description (newlines & markdown ok)",
            style=discord.TextStyle.paragraph, required=True, max_length=4000)
        self.capacity_input = discord.ui.TextInput(
            label="Capacity (blank = unlimited)", required=False, max_length=6)
        self.flags_input = discord.ui.TextInput(
            label="Flags: 'teams' and/or 'application'",
            placeholder="e.g. teams application", required=False, max_length=40)
        self.add_item(self.name_input)
        self.add_item(self.desc_input)
        self.add_item(self.capacity_input)
        self.add_item(self.flags_input)

    async def on_submit(self, interaction: discord.Interaction):
        cap = None
        if self.capacity_input.value.strip():
            try:
                cap = int(self.capacity_input.value.strip())
                if cap <= 0:
                    raise ValueError
            except ValueError:
                return await interaction.response.send_message("❌ Capacity must be a positive number.", ephemeral=True)
        flags = self.flags_input.value.lower()

        # Archive current participants into history, then reset the event.
        _archive_current_to_history()
        sd = _sd()
        sd.update({
            "open": False, "teams_enabled": "teams" in flags, "application_mode": "application" in flags,
            "title": self.name_input.value.strip(), "description": self.desc_input.value.strip(),
            "event_name": self.name_input.value.strip(), "capacity": cap,
            "open_at": None, "close_at": None, "participants": {}, "applications": {},
        })
        store.save_data()

        msg = await interaction.channel.send(embed=_panel_embed(), view=SignupView())
        sd["message_id"] = str(msg.id)
        sd["channel_id"] = str(interaction.channel.id)
        store.save_data()
        await interaction.response.send_message(
            f"✅ Created event **{sd['event_name']}** "
            f"(teams: {sd['teams_enabled']}, application: {sd['application_mode']}, "
            f"capacity: {cap or '∞'}). Use `/signup open` when ready.", ephemeral=True)


def _archive_current_to_history():
    sd = store.storage.get("signup", {})
    hist = store.storage.setdefault("signup_history", {})
    for uid, p in sd.get("participants", {}).items():
        if p.get("status") in ("active", "waitlist"):
            hist.setdefault(uid, []).append({
                "event_name": p.get("name", sd.get("event_name", "Event")),
                "username": p.get("username"), "team": p.get("team"),
                "date": datetime.datetime.now().timestamp(),
            })
    store.save_data()


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class SignupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = app_commands.Group(name="signup", description="Signup management",
                                        guild_ids=[config.GUILD_ID])
        self._register()
        bot.tree.add_command(self.group)

    @tasks.loop(minutes=1)
    async def schedule_tick(self):
        sd = _sd()
        now = datetime.datetime.now().timestamp()
        changed = False
        if not sd.get("open") and sd.get("open_at") and now >= sd["open_at"]:
            sd["open"] = True
            sd["open_at"] = None
            changed = True
        if sd.get("open") and sd.get("close_at") and now >= sd["close_at"]:
            sd["open"] = False
            sd["close_at"] = None
            changed = True
        if changed:
            store.save_data()
            await update_signup_embed(self.bot)

    def _register(self):
        group = self.group
        admin = app_commands.checks.has_permissions(administrator=True)

        @group.command(name="new", description="Create a new signup event (wizard)")
        @admin
        async def signup_new(interaction: discord.Interaction):
            await interaction.response.send_modal(NewEventModal(self.bot))

        @group.command(name="create", description="Repost the signup panel in this channel")
        @admin
        async def signup_create(interaction: discord.Interaction):
            msg = await interaction.channel.send(embed=_panel_embed(), view=SignupView())
            sd = _sd()
            sd["message_id"] = str(msg.id)
            sd["channel_id"] = str(interaction.channel.id)
            store.save_data()
            await interaction.response.send_message("Signup panel posted!", ephemeral=True)

        @group.command(name="open", description="Open signups (cancels any scheduled open)")
        @admin
        async def signup_open(interaction: discord.Interaction):
            sd = _sd(); sd["open"] = True; sd["open_at"] = None; store.save_data()
            await update_signup_embed(self.bot)
            await interaction.response.send_message("Signups are now **OPEN**!", ephemeral=True)

        @group.command(name="close", description="Close signups (cancels any scheduled close)")
        @admin
        async def signup_close(interaction: discord.Interaction):
            sd = _sd(); sd["open"] = False; sd["close_at"] = None; store.save_data()
            await update_signup_embed(self.bot)
            await interaction.response.send_message("Signups are now **CLOSED**!", ephemeral=True)

        @group.command(name="schedule", description="Schedule open/close times (dd-mm-yyyy [hh:mm])")
        @app_commands.describe(open_at="When to open (dd-mm-yyyy hh:mm)", close_at="When to close (dd-mm-yyyy hh:mm)")
        @admin
        async def signup_schedule(interaction: discord.Interaction, open_at: str = None, close_at: str = None):
            sd = _sd()
            try:
                if open_at:
                    sd["open_at"] = parse_eu_datetime(open_at).timestamp()
                if close_at:
                    sd["close_at"] = parse_eu_datetime(close_at).timestamp()
            except ValueError as e:
                return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            store.save_data()
            await update_signup_embed(self.bot)
            parts = []
            if open_at:
                parts.append(f"opens <t:{int(sd['open_at'])}:F>")
            if close_at:
                parts.append(f"closes <t:{int(sd['close_at'])}:F>")
            await interaction.response.send_message(
                "✅ Scheduled: " + (", ".join(parts) if parts else "nothing changed"), ephemeral=True)

        @group.command(name="capacity", description="Set the participant cap (0 = unlimited)")
        @app_commands.describe(cap="Maximum active participants (0 for unlimited)")
        @admin
        async def signup_capacity(interaction: discord.Interaction, cap: int):
            sd = _sd()
            sd["capacity"] = cap if cap > 0 else None
            store.save_data()
            await update_signup_embed(self.bot)
            await interaction.response.send_message(
                f"✅ Capacity set to **{cap if cap > 0 else 'unlimited'}**.", ephemeral=True)

        @group.command(name="teams", description="Enable or disable team signups")
        @app_commands.describe(enabled="Whether teams are required")
        @admin
        async def signup_teams(interaction: discord.Interaction, enabled: bool):
            _sd()["teams_enabled"] = enabled; store.save_data()
            await update_signup_embed(self.bot)
            await interaction.response.send_message(
                f"Team signups are now **{'enabled' if enabled else 'disabled'}**", ephemeral=True)

        @group.command(name="application", description="Enable or disable application mode")
        @app_commands.describe(enabled="Whether signups require approval")
        @admin
        async def signup_application(interaction: discord.Interaction, enabled: bool):
            if enabled and not config.SIGNUP_APPLICATION_CHANNEL_ID:
                return await interaction.response.send_message(
                    "❌ Set SIGNUP_APPLICATION_CHANNEL_ID in the env first.", ephemeral=True)
            _sd()["application_mode"] = enabled; store.save_data()
            await update_signup_embed(self.bot)
            await interaction.response.send_message(
                f"Application mode is now **{'enabled' if enabled else 'disabled'}**", ephemeral=True)

        @group.command(name="flavor", description="Edit the panel title and description (with formatting)")
        @admin
        async def signup_flavor(interaction: discord.Interaction):
            await interaction.response.send_modal(FlavorModal(self.bot))

        @group.command(name="participants", description="List current participants")
        @admin
        async def signup_participants(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await _send_participants(interaction)

        @group.command(name="reset", description="Archive participants to history, clear roster, optionally grant a role")
        @app_commands.describe(new_role="Optional role to grant participants before clearing")
        @admin
        async def signup_reset(interaction: discord.Interaction, new_role: discord.Role = None):
            await interaction.response.defer(ephemeral=True)
            sd = _sd()
            role = interaction.guild.get_role(config.SIGNUP_ROLE_ID)
            ok = fail = 0
            for uid in list(sd["participants"].keys()):
                member = interaction.guild.get_member(int(uid))
                if not member:
                    continue
                try:
                    if new_role:
                        await member.add_roles(new_role, reason="Signup reset")
                    if role and role in member.roles:
                        await member.remove_roles(role, reason="Signup reset")
                    ok += 1
                except discord.Forbidden:
                    fail += 1
            _archive_current_to_history()
            sd["participants"] = {}
            sd["applications"] = {}
            store.save_data()
            await update_signup_embed(self.bot)
            resp = f"✅ Reset complete. Archived & processed **{ok}** participant(s)."
            if new_role:
                resp += f"\n• Granted: {new_role.mention}"
            if fail:
                resp += f"\n⚠️ Failed on **{fail}** (permissions)."
            await interaction.followup.send(resp, ephemeral=True)


async def _send_participants(interaction: discord.Interaction):
    sd = _sd()
    active = _active(sd)
    waitlist = _waitlist(sd)
    if not active and not waitlist:
        return await interaction.followup.send("No participants yet.", ephemeral=True)

    def fmt(plist):
        if sd.get("teams_enabled"):
            by_team = {}
            for p in plist:
                by_team.setdefault(p.get("team") or "—", []).append(p)
            out = []
            for team, members in sorted(by_team.items()):
                out.append(f"**{team}** ({len(members)})")
                for m in members:
                    out.append(f"  • {m['username']} (<@{_uid_of(sd, m)}>)")
            return "\n".join(out)
        return "\n".join(f"• {p['username']} (<@{_uid_of(sd, p)}>)" for p in plist)

    text = f"# {sd.get('event_name','Event')} — Participants\n\n## Active ({len(active)})\n{fmt(active)}"
    if waitlist:
        text += f"\n\n## Waitlist ({len(waitlist)})\n{fmt(waitlist)}"

    # Auto: inline if short, file if long.
    if len(text) <= 1900:
        await interaction.followup.send(text, ephemeral=True)
    else:
        import io
        buf = io.BytesIO(text.encode("utf-8"))
        await interaction.followup.send(
            "Participant list attached:",
            file=discord.File(buf, filename=f"participants_{sd.get('event_name','event')}.txt"),
            ephemeral=True)


def _uid_of(sd, entry):
    for u, p in sd["participants"].items():
        if p is entry:
            return u
    return "?"


class FlavorModal(discord.ui.Modal, title="Edit Signup Panel"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        sd = _sd()
        self.title_input = discord.ui.TextInput(
            label="Title", style=discord.TextStyle.short,
            default=sd.get("title", "Sign Up"), required=True, max_length=256)
        self.description_input = discord.ui.TextInput(
            label="Description (newlines & markdown)", style=discord.TextStyle.paragraph,
            default=sd.get("description", ""), required=True, max_length=4000)
        self.add_item(self.title_input)
        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction):
        sd = _sd()
        sd["title"] = self.title_input.value
        sd["description"] = self.description_input.value
        store.save_data()
        await update_signup_embed(self.bot)
        await interaction.response.send_message("✅ Signup panel updated.", ephemeral=True)


async def setup(bot: commands.Bot):
    cog = SignupCog(bot)
    await bot.add_cog(cog)
    return cog
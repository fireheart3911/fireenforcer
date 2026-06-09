import datetime
import io
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
#   open, teams_enabled, application_mode,
#   message_id, channel_id,              # the signup panel (button)
#   roster_message_id, roster_channel_id, roster_extra_id,  # the static roster
#   title, description, event_name, capacity (int|None),
#   open_at, close_at (epoch|None),
#   participants: {user_id: {username, team, name(event), signed_at, status, priority}},
#   applications: {app_id: {user_id, username, team, message_id, status}},
# }
# status: "active" | "waitlist"
# storage["signup_history"][user_id] = [{event_name, username, team, date}, ...]

TEAM_SIZE_CAP = 5

def _sd() -> dict:
    sd = store.storage.setdefault("signup", {})
    sd.setdefault("open", False)
    sd.setdefault("teams_enabled", False)
    sd.setdefault("application_mode", False)
    sd.setdefault("message_id", None)
    sd.setdefault("channel_id", None)
    sd.setdefault("roster_message_id", None)
    sd.setdefault("roster_channel_id", None)
    sd.setdefault("roster_extra_id", None)
    sd.setdefault("title", "Sign Up")
    sd.setdefault("description", "Click the button below to register!")
    sd.setdefault("event_name", "Event")
    sd.setdefault("capacity", None)
    sd.setdefault("open_at", None)
    sd.setdefault("close_at", None)
    sd.setdefault("participants", {})
    sd.setdefault("applications", {})
    sd.setdefault("denied", [])
    return sd


def _active(sd) -> list:
    return [p for p in sd["participants"].values() if p.get("status") == "active"]

def _waitlist_sorted(sd) -> list:
    """Waitlist ordered: priority first, then FIFO by signed_at."""
    wl = [p for p in sd["participants"].values() if p.get("status") == "waitlist"]
    return sorted(wl, key=lambda p: (not p.get("priority", False), p.get("signed_at", 0)))

def _is_full(sd) -> bool:
    cap = sd.get("capacity")
    return cap is not None and len(_active(sd)) >= cap

def _uid_of(sd, entry):
    for u, p in sd["participants"].items():
        if p is entry:
            return u
    return "?"

def _waitlist_position(sd, user_id) -> int | None:
    """1-based position of a user in the (priority-aware) waitlist, or None."""
    for i, p in enumerate(_waitlist_sorted(sd), 1):
        if _uid_of(sd, p) == str(user_id):
            return i
    return None


# ---------------------------------------------------------------------------
# DM helper (embeds)
# ---------------------------------------------------------------------------

async def _dm(client, user_id, title, description, color):
    try:
        user = await client.fetch_user(int(user_id))
        embed = discord.Embed(title=title, description=description, color=color,
                              timestamp=datetime.datetime.now())
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ---------------------------------------------------------------------------
# Panel embed (button panel)
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
    embed.add_field(name="Signed up", value=f"{n_active}/{cap}" if cap else str(n_active), inline=True)
    if sd.get("teams_enabled"):
        embed.add_field(name="Teams", value="Enabled", inline=True)
    wl = len(_waitlist_sorted(sd))
    if wl:
        embed.add_field(name="Waitlist", value=str(wl), inline=True)

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
# Roster embeds (static participant/waitlist message)
# ---------------------------------------------------------------------------

def _roster_embeds() -> list[discord.Embed]:
    """Return [participants_embed] or [participants, waitlist] if it would overflow."""
    sd = _sd()
    active = _active(sd)
    waitlist = _waitlist_sorted(sd)

    def fmt(plist, numbered=False):
        if not plist:
            return "—"
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
        lines = []
        for i, p in enumerate(plist, 1):
            prefix = f"{i}. " if numbered else "• "
            prio = " ⭐" if p.get("priority") else ""
            lines.append(f"{prefix}{p['username']} (<@{_uid_of(sd, p)}>){prio}")
        return "\n".join(lines)

    main = discord.Embed(
        title=f"{sd.get('event_name', 'Event')} — Roster",
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.now(),
    )
    cap = sd.get("capacity")
    main.add_field(
        name=f"✅ Participants ({len(active)}{'/' + str(cap) if cap else ''})",
        value=fmt(active)[:1024], inline=False)
    wl_text = fmt(waitlist, numbered=True)
    combined_len = len(main.fields[0].value) + len(wl_text)

    if waitlist and combined_len > 3500:
        wl_embed = discord.Embed(title=f"{sd.get('event_name','Event')} — Waitlist",
                                 color=discord.Color.greyple(), timestamp=datetime.datetime.now())
        wl_embed.description = wl_text[:4000]
        return [main, wl_embed]

    if waitlist:
        main.add_field(name=f"🟡 Waitlist ({len(waitlist)})", value=wl_text[:1024], inline=False)
    return [main]


async def update_roster_message(client: commands.Bot):
    sd = _sd()
    if not sd.get("roster_message_id") or not sd.get("roster_channel_id"):
        return
    channel = client.get_channel(int(sd["roster_channel_id"]))
    if not channel:
        return
    embeds = _roster_embeds()
    try:
        msg = await channel.fetch_message(int(sd["roster_message_id"]))
        await msg.edit(embed=embeds[0])
    except discord.NotFound:
        sd["roster_message_id"] = None
        sd["roster_channel_id"] = None
        store.save_data()
        return
    # Second (waitlist) embed in a follow-up tracked message
    if len(embeds) > 1:
        if sd.get("roster_extra_id"):
            try:
                m2 = await channel.fetch_message(int(sd["roster_extra_id"]))
                await m2.edit(embed=embeds[1])
                return
            except discord.NotFound:
                pass
        m2 = await channel.send(embed=embeds[1])
        sd["roster_extra_id"] = str(m2.id)
        store.save_data()
    elif sd.get("roster_extra_id"):
        # No longer need the overflow message
        try:
            m2 = await channel.fetch_message(int(sd["roster_extra_id"]))
            await m2.delete()
        except discord.NotFound:
            pass
        sd["roster_extra_id"] = None
        store.save_data()


async def _refresh_all(client):
    await update_signup_embed(client)
    await update_roster_message(client)


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
        if uid in sd.get("denied", []):
            return await interaction.response.send_message(
                "❌ Your registration for this event was declined. You can apply again at the next event.",
                ephemeral=True)
        if any(a["user_id"] == uid and a["status"] == "pending" for a in sd["applications"].values()):
            return await interaction.response.send_message("❌ You already have a pending application.", ephemeral=True)
        role = interaction.guild.get_role(config.SIGNUP_ROLE_ID)
        if role and role in interaction.user.roles:
            return await interaction.response.send_message("❌ You have already signed up!", ephemeral=True)
        await interaction.response.send_modal(SignupModal(
            teams_enabled=sd.get("teams_enabled", False),
            will_waitlist=_is_full(sd),
            application_mode=sd.get("application_mode", False)))


class SignupModal(discord.ui.Modal):
    def __init__(self, teams_enabled=False, will_waitlist=False, application_mode=False):
        super().__init__(title="Apply" if application_mode else ("Join Waitlist" if will_waitlist else "Sign Up"))
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
        if self.teams_enabled and team:
            ok, msg = _check_team_rules(sd, team)
            if not ok:
                return await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

        if self.application_mode:
            await _create_application(interaction, sd, username, team)
            return await interaction.response.send_message(
                "✅ Your registration has been submitted for review.", ephemeral=True)

        status = "waitlist" if _is_full(sd) else "active"
        await _register_participant(interaction.client, interaction.guild, interaction.user, username, team, status)
        if status == "waitlist":
            pos = _waitlist_position(sd, interaction.user.id)
            resp = f"🟡 You've been added to the **waitlist** (position **#{pos}**) as **{username}**"
        else:
            resp = f"✅ You have signed up as **{username}**"
        if team:
            resp += f" on team **{team}**"
        await interaction.response.send_message(resp + "!", ephemeral=True)
        await _refresh_all(interaction.client)


def _check_team_rules(sd, team: str) -> tuple[bool, str]:
    members = [p for p in sd["participants"].values() if (p.get("team") or "").lower() == team.lower()]
    if len(members) >= TEAM_SIZE_CAP:
        return False, f"Team **{team}** is full ({TEAM_SIZE_CAP} members)."
    return True, ""


# ---------------------------------------------------------------------------
# Registration / promotion / kick
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
        "signed_at": datetime.datetime.now().timestamp(), "status": status, "priority": False,
    }
    store.save_data()
    await _log_signup(guild, user, username, team, status)


async def _apply_player_nick(user, username):
    if not re.match(r"^\[.+\]", user.display_name):
        try:
            await user.edit(nick=f"[Player] {username}")
        except discord.Forbidden:
            pass


async def _remove_player_nick(member):
    """Remove the nick only if it's exactly a [Player] prefix (leave custom ones)."""
    if member.nick and member.nick.startswith("[Player] "):
        try:
            await member.edit(nick=None)
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
    sd = _sd()
    if _is_full(sd):
        return
    wl = _waitlist_sorted(sd)
    if not wl:
        return
    entry = wl[0]
    uid = _uid_of(sd, entry)
    entry["status"] = "active"
    entry.pop("priority", None)
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
    await _dm(client, uid, "You're in! 🎉",
              f"A slot opened up — you're now confirmed for **{sd.get('event_name','the event')}** "
              f"as **{entry['username']}**!", discord.Color.green())


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

async def _create_application(interaction, sd, username, team):
    app_id = f"APP-{store.next_id()}"
    channel = interaction.guild.get_channel(config.SIGNUP_APPLICATION_CHANNEL_ID) if config.SIGNUP_APPLICATION_CHANNEL_ID else None
    embed = await _application_embed(interaction.guild, interaction.user, username, team)
    msg = await channel.send(embed=embed, view=ApplicationView()) if channel else None
    sd["applications"][app_id] = {
        "id": app_id, "user_id": str(interaction.user.id), "username": username,
        "team": team, "message_id": str(msg.id) if msg else None, "status": "pending",
    }
    store.save_data()


async def _application_embed(guild, user, username, team) -> discord.Embed:
    sd = _sd()
    embed = discord.Embed(title="New Application", color=discord.Color.gold(),
                          timestamp=datetime.datetime.now())
    embed.add_field(name="Applicant", value=user.mention, inline=True)
    embed.add_field(name="Username", value=username, inline=True)
    if team:
        embed.add_field(name="Team", value=team, inline=True)

    created = int(user.created_at.timestamp())
    info = f"Account created <t:{created}:R>"
    if user.joined_at:
        info += f"\nJoined server <t:{int(user.joined_at.timestamp())}:R>"
    embed.add_field(name="User Info", value=info, inline=False)
    embed.add_field(name="Levels", value=await _levels_text(user.id), inline=False)
    embed.add_field(name="Previous Participations", value=_history_text(user.id), inline=False)
    embed.add_field(name="Moderation", value=await get_moderation_info(user.id), inline=False)

    # Reflect capacity state for the reviewer
    if _is_full(sd):
        embed.add_field(
            name="⚠️ Roster Full",
            value=f"All {sd.get('capacity')} slots are taken — approving isn't possible. "
                  f"Use **Send to Waitlist**.", inline=False)

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
    lines = [f"• {h.get('event_name', 'Event')}" + (f" (team {h['team']})" if h.get("team") else "")
             for h in hist[-5:]]
    more = f"\n…and {len(hist) - 5} more" if len(hist) > 5 else ""
    return "\n".join(lines) + more


async def get_moderation_info(user_id) -> str:
    """Stub for the (to-be-implemented) global moderation system."""
    return "✅ No incidents on record."


class ApplicationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

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
            await _dm(interaction.client, app["user_id"], "Registration approved 🎉",
                      f"Your registration for **{sd.get('event_name','the event')}** was **approved**! "
                      f"You're signed up as **{app['username']}**.", discord.Color.green())
        _resolve_app(sd, app)
        await _finish_application_message(interaction, "✅ Approved", discord.Color.green())
        await _refresh_all(interaction.client)

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
            pos = _waitlist_position(sd, app["user_id"])
            await _dm(interaction.client, app["user_id"], "Added to waitlist",
                      f"Your registration for **{sd.get('event_name','the event')}** was placed on the "
                      f"**waitlist** at position **#{pos}**.", discord.Color.blurple())
        _resolve_app(sd, app)
        await _finish_application_message(interaction, "🟡 Waitlisted", discord.Color.blurple())
        await _refresh_all(interaction.client)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, emoji="✖️", custom_id="signup:app_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        sd = _sd()
        app = _app_by_message(sd, interaction.message.id)
        if not app or app["status"] != "pending":
            return await interaction.response.send_message("❌ This application is no longer pending.", ephemeral=True)
        await interaction.response.send_modal(DenyModal(app["id"]))


class DenyModal(discord.ui.Modal, title="Deny Registration"):
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

        # Block reapplying for this event.
        if app["user_id"] not in sd.setdefault("denied", []):
            sd["denied"].append(app["user_id"])

        if reason:
            await _dm(interaction.client, app["user_id"], "Registration denied",
                      f"Your registration for **{sd.get('event_name','the event')}** was denied.\n"
                      f"**Reason:** {reason}", discord.Color.red())

        # Log the denial to the signup log channel.
        log_ch = interaction.guild.get_channel(config.SIGNUP_LOG_CHANNEL_ID)
        if log_ch:
            embed = discord.Embed(title="Registration Denied", color=discord.Color.red(),
                                  timestamp=datetime.datetime.now())
            embed.add_field(name="User", value=f"<@{app['user_id']}>", inline=True)
            embed.add_field(name="Username", value=app.get("username", "—"), inline=True)
            if app.get("team"):
                embed.add_field(name="Team", value=app["team"], inline=True)
            embed.add_field(name="Denied by", value=interaction.user.mention, inline=True)
            if reason:
                embed.add_field(name="Reason", value=reason, inline=False)
            await log_ch.send(embed=embed)

        _resolve_app(sd, app)
        await _finish_application_message(interaction, "✖️ Denied" + (f" — {reason}" if reason else ""),
                                          discord.Color.red())


def _app_by_message(sd, message_id):
    for a in sd["applications"].values():
        if a.get("message_id") == str(message_id):
            return a
    return None


def _resolve_app(sd, app):
    """Drop resolved application data from storage."""
    sd["applications"].pop(app["id"], None)
    store.save_data()


async def _finish_application_message(interaction, status_text, color):
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
    channel = client.get_channel(int(sd["channel_id"]))
    if not channel:
        return
    try:
        msg = await channel.fetch_message(int(sd["message_id"]))
        await msg.edit(embed=_panel_embed(), view=SignupView())
    except discord.NotFound:
        sd["message_id"] = None
        sd["channel_id"] = None
        store.save_data()


# ---------------------------------------------------------------------------
# History archival
# ---------------------------------------------------------------------------

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
# Modals: new event, flavor
# ---------------------------------------------------------------------------

class NewEventModal(discord.ui.Modal, title="New Signup Event"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.name_input = discord.ui.TextInput(label="Event name", required=True, max_length=80)
        self.desc_input = discord.ui.TextInput(
            label="Panel description (newlines & markdown ok)",
            style=discord.TextStyle.paragraph, required=True, max_length=4000)
        self.capacity_input = discord.ui.TextInput(label="Capacity (blank = unlimited)", required=False, max_length=6)
        self.flags_input = discord.ui.TextInput(
            label="Flags: 'teams' and/or 'application'",
            placeholder="e.g. teams application", required=False, max_length=40)
        self.add_item(self.name_input)
        self.add_item(self.desc_input)
        self.add_item(self.capacity_input)
        self.add_item(self.flags_input)

    async def on_submit(self, interaction: discord.Interaction):
        sd = _sd()
        if not sd.get("message_id") or not sd.get("channel_id"):
            return await interaction.response.send_message(
                "❌ No signup panel exists yet. Run `/panel signup` first, then `/signup new`.", ephemeral=True)
        cap = None
        if self.capacity_input.value.strip():
            try:
                cap = int(self.capacity_input.value.strip())
                if cap <= 0:
                    raise ValueError
            except ValueError:
                return await interaction.response.send_message("❌ Capacity must be a positive number.", ephemeral=True)
        flags = self.flags_input.value.lower()
        if "application" in flags and not config.SIGNUP_APPLICATION_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ Application mode needs SIGNUP_APPLICATION_CHANNEL_ID set in the env.", ephemeral=True)

        _archive_current_to_history()
        sd.update({
            "open": False, "teams_enabled": "teams" in flags, "application_mode": "application" in flags,
            "title": self.name_input.value.strip(), "description": self.desc_input.value.strip(),
            "event_name": self.name_input.value.strip(), "capacity": cap,
            "open_at": None, "close_at": None, "participants": {}, "applications": {}, "denied": [],
        })
        store.save_data()
        await _refresh_all(self.bot)
        await interaction.response.send_message(
            f"✅ Created event **{sd['event_name']}** (teams: {sd['teams_enabled']}, "
            f"application: {sd['application_mode']}, capacity: {cap or '∞'}) on the existing panel. "
            f"Use `/signup open` when ready.", ephemeral=True)


class FlavorModal(discord.ui.Modal, title="Edit Signup Panel"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        sd = _sd()
        self.title_input = discord.ui.TextInput(label="Title", style=discord.TextStyle.short,
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


class CapacityModal(discord.ui.Modal, title="Set Capacity"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        sd = _sd()
        self.cap_input = discord.ui.TextInput(
            label="Capacity (0 or blank = unlimited)",
            default=str(sd["capacity"]) if sd.get("capacity") else "", required=False, max_length=6)
        self.add_item(self.cap_input)

    async def on_submit(self, interaction: discord.Interaction):
        sd = _sd()
        raw = self.cap_input.value.strip()
        if not raw or raw == "0":
            sd["capacity"] = None
        else:
            try:
                cap = int(raw)
                if cap < 0:
                    raise ValueError
                sd["capacity"] = cap
            except ValueError:
                return await interaction.response.send_message("❌ Capacity must be a non-negative number.", ephemeral=True)
        store.save_data()
        await _refresh_all(self.bot)
        await interaction.response.send_message(
            f"✅ Capacity set to **{sd['capacity'] or 'unlimited'}**.", ephemeral=True)


# ---------------------------------------------------------------------------
# Settings panel
# ---------------------------------------------------------------------------

def _settings_summary() -> str:
    sd = _sd()
    return (f"## ⚙️ Signup Settings — {sd.get('event_name','Event')}\n"
            f"**Teams:** {'on' if sd.get('teams_enabled') else 'off'}\n"
            f"**Application mode:** {'on' if sd.get('application_mode') else 'off'}\n"
            f"**Capacity:** {sd.get('capacity') or 'unlimited'}\n"
            f"**Title:** {sd.get('title')}")


class SettingsView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot
        self._sync_labels()

    def _sync_labels(self):
        sd = _sd()
        self.toggle_teams.label = f"Teams: {'ON' if sd.get('teams_enabled') else 'OFF'}"
        self.toggle_teams.style = discord.ButtonStyle.green if sd.get("teams_enabled") else discord.ButtonStyle.gray
        self.toggle_app.label = f"Application: {'ON' if sd.get('application_mode') else 'OFF'}"
        self.toggle_app.style = discord.ButtonStyle.green if sd.get("application_mode") else discord.ButtonStyle.gray

    @discord.ui.button(label="Teams: OFF", style=discord.ButtonStyle.gray, row=0)
    async def toggle_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        sd = _sd(); sd["teams_enabled"] = not sd.get("teams_enabled"); store.save_data()
        await _refresh_all(self.bot)
        self._sync_labels()
        await interaction.response.edit_message(content=_settings_summary(), view=self)

    @discord.ui.button(label="Application: OFF", style=discord.ButtonStyle.gray, row=0)
    async def toggle_app(self, interaction: discord.Interaction, button: discord.ui.Button):
        sd = _sd()
        new = not sd.get("application_mode")
        if new and not config.SIGNUP_APPLICATION_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ Set SIGNUP_APPLICATION_CHANNEL_ID in the env first.", ephemeral=True)
        sd["application_mode"] = new; store.save_data()
        await _refresh_all(self.bot)
        self._sync_labels()
        await interaction.response.edit_message(content=_settings_summary(), view=self)

    @discord.ui.button(label="Set Capacity", emoji="🔢", style=discord.ButtonStyle.blurple, row=1)
    async def set_capacity(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CapacityModal(self.bot))

    @discord.ui.button(label="Edit Flavor", emoji="📝", style=discord.ButtonStyle.blurple, row=1)
    async def edit_flavor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FlavorModal(self.bot))


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
            sd["open"] = True; sd["open_at"] = None; changed = True
        if sd.get("open") and sd.get("close_at") and now >= sd["close_at"]:
            sd["open"] = False; sd["close_at"] = None; changed = True
        if changed:
            store.save_data()
            await _refresh_all(self.bot)

    def _register(self):
        group = self.group
        admin = app_commands.checks.has_permissions(administrator=True)

        @group.command(name="new", description="Create a new signup event (uses the existing panel)")
        @admin
        async def signup_new(interaction: discord.Interaction):
            await interaction.response.send_modal(NewEventModal(self.bot))

        @group.command(name="settings", description="Open the signup settings panel")
        @admin
        async def signup_settings(interaction: discord.Interaction):
            await interaction.response.send_message(content=_settings_summary(),
                                                    view=SettingsView(self.bot), ephemeral=True)

        @group.command(name="open", description="Open signups (cancels any scheduled open)")
        @admin
        async def signup_open(interaction: discord.Interaction):
            sd = _sd(); sd["open"] = True; sd["open_at"] = None; store.save_data()
            await _refresh_all(self.bot)
            await interaction.response.send_message("Signups are now **OPEN**!", ephemeral=True)

        @group.command(name="close", description="Close signups (cancels any scheduled close)")
        @admin
        async def signup_close(interaction: discord.Interaction):
            sd = _sd(); sd["open"] = False; sd["close_at"] = None; store.save_data()
            await _refresh_all(self.bot)
            await interaction.response.send_message("Signups are now **CLOSED**!", ephemeral=True)

        @group.command(name="schedule", description="Schedule open/close (dd-mm-yyyy [hh:mm])")
        @app_commands.describe(open_at="When to open", close_at="When to close")
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
            await _refresh_all(self.bot)
            await interaction.response.send_message("✅ Schedule updated.", ephemeral=True)

        @group.command(name="participants", description="List current participants (ephemeral)")
        @admin
        async def signup_participants(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await _send_participants(interaction)

        @group.command(name="priority", description="Toggle waitlist priority for a user")
        @app_commands.describe(user="The waitlisted user to (de)prioritize")
        @admin
        async def signup_priority(interaction: discord.Interaction, user: discord.Member):
            sd = _sd()
            p = sd["participants"].get(str(user.id))
            if not p or p.get("status") != "waitlist":
                return await interaction.response.send_message("❌ That user isn't on the waitlist.", ephemeral=True)
            p["priority"] = not p.get("priority", False)
            store.save_data()
            await update_roster_message(self.bot)
            pos = _waitlist_position(sd, user.id)
            await interaction.response.send_message(
                f"{'⭐ Prioritized' if p['priority'] else 'Removed priority for'} {user.mention} "
                f"(now position **#{pos}**).", ephemeral=True)

        @group.command(name="promote", description="Fill a slot from the waitlist (specific user, or the next in line)")
        @app_commands.describe(user="Who to promote (leave blank for the next in the waitlist)")
        @admin
        async def signup_promote(interaction: discord.Interaction, user: discord.Member = None):
            sd = _sd()
            if _is_full(sd):
                return await interaction.response.send_message(
                    "❌ The roster is full — free a slot first (e.g. `/signup kick`).", ephemeral=True)
            wl = _waitlist_sorted(sd)
            if not wl:
                return await interaction.response.send_message("❌ The waitlist is empty.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)

            if user is None:
                # Promote the next in line (reuses the shared promotion path).
                await _promote_from_waitlist(interaction.client, interaction.guild)
                await _refresh_all(self.bot)
                promoted = wl[0]
                return await interaction.followup.send(
                    f"✅ Promoted next in line: **{promoted['username']}** "
                    f"(<@{_uid_of(sd, promoted)}>).", ephemeral=True)

            entry = sd["participants"].get(str(user.id))
            if not entry or entry.get("status") != "waitlist":
                return await interaction.followup.send("❌ That user isn't on the waitlist.", ephemeral=True)
            entry["status"] = "active"
            entry.pop("priority", None)
            store.save_data()
            role = interaction.guild.get_role(config.SIGNUP_ROLE_ID)
            if role:
                try:
                    await user.add_roles(role, reason="Promoted from waitlist")
                except discord.Forbidden:
                    pass
            await _apply_player_nick(user, entry["username"])
            await _dm(interaction.client, str(user.id), "You're in! 🎉",
                      f"You've been moved off the waitlist into **{sd.get('event_name','the event')}** "
                      f"as **{entry['username']}**!", discord.Color.green())
            await _refresh_all(self.bot)
            await interaction.followup.send(f"✅ Promoted {user.mention} from the waitlist.", ephemeral=True)

        @group.command(name="export", description="Export participant usernames")
        @app_commands.describe(separator="How to separate usernames", include_waitlist="Include the waitlist too")
        @app_commands.choices(separator=[
            app_commands.Choice(name="Newline", value="newline"),
            app_commands.Choice(name="Comma", value="comma"),
        ])
        @admin
        async def signup_export(interaction: discord.Interaction,
                                separator: str = "newline", include_waitlist: bool = False):
            sd = _sd()
            sep = "\n" if separator == "newline" else ", "
            active_names = [p["username"] for p in _active(sd)]
            text = sep.join(active_names)
            if include_waitlist:
                wl_names = [p["username"] for p in _waitlist_sorted(sd)]
                if wl_names:
                    text += f"\n\n--- Waitlist ---\n" + sep.join(wl_names)
            if not text.strip():
                return await interaction.response.send_message("No participants to export.", ephemeral=True)
            buf = io.BytesIO(text.encode("utf-8"))
            await interaction.response.send_message(
                f"Export ({len(active_names)} participant(s)):",
                file=discord.File(buf, filename=f"{sd.get('event_name','event')}_usernames.txt"),
                ephemeral=True)

        @group.command(name="kick", description="Remove a participant from the event")
        @app_commands.describe(user="The participant to remove", notify="DM the user (default: yes)")
        @admin
        async def signup_kick(interaction: discord.Interaction, user: discord.Member, notify: bool = True):
            sd = _sd()
            p = sd["participants"].get(str(user.id))
            if not p:
                return await interaction.response.send_message("❌ That user isn't in this event.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            was_active = p.get("status") == "active"
            role = interaction.guild.get_role(config.SIGNUP_ROLE_ID)
            if role and role in user.roles:
                try:
                    await user.remove_roles(role, reason="Kicked from event")
                except discord.Forbidden:
                    pass
            await _remove_player_nick(user)
            del sd["participants"][str(user.id)]
            store.save_data()
            if notify:
                await _dm(interaction.client, str(user.id), "Removed from event",
                          f"You were removed from **{sd.get('event_name','the event')}** by an organizer.",
                          discord.Color.red())
            if was_active:
                await _promote_from_waitlist(interaction.client, interaction.guild)
            await _refresh_all(self.bot)
            await interaction.followup.send(f"✅ Removed {user.mention} from the event.", ephemeral=True)

        @group.command(name="reset", description="Archive participants, post final roster to log, clear the event")
        @app_commands.describe(new_role="Optional role to grant participants before clearing")
        @admin
        async def signup_reset(interaction: discord.Interaction, new_role: discord.Role = None):
            await interaction.response.defer(ephemeral=True)
            sd = _sd()
            role = interaction.guild.get_role(config.SIGNUP_ROLE_ID)

            # Archive final roster to the signup log channel
            log_ch = interaction.guild.get_channel(config.SIGNUP_LOG_CHANNEL_ID)
            if log_ch:
                for emb in _roster_embeds():
                    emb.title = f"[Archived] {emb.title}"
                    await log_ch.send(embed=emb)

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
            # Flush the whole event state back to a neutral, closed, empty event.
            sd["participants"] = {}
            sd["applications"] = {}
            sd["denied"] = []
            sd["open"] = False
            sd["open_at"] = None
            sd["close_at"] = None
            sd["capacity"] = None
            sd["teams_enabled"] = False
            sd["application_mode"] = False
            sd["event_name"] = "No event active"
            sd["title"] = "Nothing to see here"
            sd["description"] = "Check back later"
            store.save_data()

            # Flush the live roster + panel messages (clear them; keep them)
            await _flush_roster_message(self.bot)
            await update_signup_embed(self.bot)

            resp = f"✅ Reset complete. Archived & processed **{ok}** participant(s); final roster sent to the log."
            if new_role:
                resp += f"\n• Granted: {new_role.mention}"
            if fail:
                resp += f"\n⚠️ Failed on **{fail}** (permissions)."
            await interaction.followup.send(resp, ephemeral=True)


async def _flush_roster_message(client):
    sd = _sd()
    if not sd.get("roster_message_id") or not sd.get("roster_channel_id"):
        return
    channel = client.get_channel(int(sd["roster_channel_id"]))
    if not channel:
        return
    empty = discord.Embed(title=f"{sd.get('event_name','Event')} — Roster",
                          description="*No active event.*", color=discord.Color.greyple())
    try:
        msg = await channel.fetch_message(int(sd["roster_message_id"]))
        await msg.edit(embed=empty)
    except discord.NotFound:
        pass
    if sd.get("roster_extra_id"):
        try:
            m2 = await channel.fetch_message(int(sd["roster_extra_id"]))
            await m2.delete()
        except discord.NotFound:
            pass
        sd["roster_extra_id"] = None
        store.save_data()


async def _send_participants(interaction: discord.Interaction):
    sd = _sd()
    embeds = _roster_embeds()
    text = "\n\n".join(f"**{e.title}**\n" + (e.description or "") +
                       "\n".join(f"{f.name}\n{f.value}" for f in e.fields) for e in embeds)
    if len(text) <= 1900:
        await interaction.followup.send(text, ephemeral=True)
    else:
        buf = io.BytesIO(text.encode("utf-8"))
        await interaction.followup.send("Participant list attached:",
                                        file=discord.File(buf, filename="participants.txt"), ephemeral=True)


# ---- panel posting (called by the unified /panel command in main) ----

async def post_signup_panel(interaction: discord.Interaction):
    sd = _sd()
    msg = await interaction.channel.send(embed=_panel_embed(), view=SignupView())
    sd["message_id"] = str(msg.id)
    sd["channel_id"] = str(interaction.channel.id)
    store.save_data()


async def post_roster_panel(interaction: discord.Interaction):
    sd = _sd()
    embeds = _roster_embeds()
    msg = await interaction.channel.send(embed=embeds[0])
    sd["roster_message_id"] = str(msg.id)
    sd["roster_channel_id"] = str(interaction.channel.id)
    sd["roster_extra_id"] = None
    if len(embeds) > 1:
        m2 = await interaction.channel.send(embed=embeds[1])
        sd["roster_extra_id"] = str(m2.id)
    store.save_data()


async def setup(bot: commands.Bot):
    cog = SignupCog(bot)
    await bot.add_cog(cog)
    return cog
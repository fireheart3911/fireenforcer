import datetime
import io
import re

import discord
from discord.ext import commands, tasks
from discord import app_commands

import storage as store
import config
from cogs.parsers import parse_eu_datetime, parse_duration_seconds
from cogs import moderation


# ---------------------------------------------------------------------------
# Storage model
# ---------------------------------------------------------------------------
# storage["events"][event_id] = {
#   id, name, description, type ("generic"|"brainlag"), mode ("rsvp"|"datepoll"),
#   status ("open"|"locked"|"started"|"ended"|"cancelled"),
#   channel_id, panel_message_id, created_by, created_at,
#   capacity (int|None), start_at (epoch|None),
#   reminders [lead_seconds...], reminders_sent [...],
#   date_options: [{key, at, label}],          # datepoll mode
#   participants: {user_id: {username, joined_at, status, source, dates}},
# }
# participant status: "active" | "unavailable" (lost the date poll)
# participant source: "rsvp" | "host"

MAX_DATE_OPTIONS = 20
DEFAULT_REMINDERS = [3600, 0]          # 1h before + at start

_TERMINAL = ("ended", "cancelled")


def _events() -> dict:
    return store.storage.setdefault("events", {})


def _event(eid) -> dict | None:
    return _events().get(eid)


def _active_events() -> list:
    return [ev for ev in _events().values() if ev.get("status") not in _TERMINAL]


def _roster(ev, status="active") -> list:
    return [p for p in ev["participants"].values() if p.get("status") == status]


def _is_full(ev) -> bool:
    cap = ev.get("capacity")
    return cap is not None and len(_roster(ev)) >= cap


def _is_joinable(ev) -> bool:
    return ev.get("status") == "open"


def _uid_of(ev, entry):
    for u, p in ev["participants"].items():
        if p is entry:
            return u
    return "?"


def _date_votes(ev, key) -> int:
    return sum(1 for p in ev["participants"].values() if key in p.get("dates", []))


def _ranked_dates(ev) -> list:
    """Date options ordered by votes (desc), ties broken by earliest date."""
    return sorted(ev.get("date_options", []),
                  key=lambda o: (-_date_votes(ev, o["key"]), o["at"]))


# ---------------------------------------------------------------------------
# DM helper (embeds) — mirrors signup._dm
# ---------------------------------------------------------------------------

async def _dm(client, user_id, title, description, color):
    try:
        user = await client.fetch_user(int(user_id))
        embed = discord.Embed(title=title, description=description, color=color,
                              timestamp=datetime.datetime.now())
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


# ---------------------------------------------------------------------------
# Reminder parsing
# ---------------------------------------------------------------------------

def _parse_reminders(text: str) -> list[int]:
    """Parse a whitespace/comma list of lead times into sorted descending seconds.

    Tokens are durations ("1d", "1h", "30m") or "0"/"start" for an at-start ping.
    Blank → DEFAULT_REMINDERS. Raises ValueError on a bad token.
    """
    text = (text or "").strip().lower()
    if not text:
        return list(DEFAULT_REMINDERS)
    leads = set()
    for tok in re.split(r"[,\s]+", text):
        if not tok:
            continue
        if tok in ("0", "start", "now"):
            leads.add(0)
        else:
            leads.add(parse_duration_seconds(tok))
    return sorted(leads, reverse=True)


def _fmt_lead(sec: int) -> str:
    if sec <= 0:
        return "0"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return "".join(p for p in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "")) or "0"


def _format_reminders(leads) -> str:
    return " ".join(_fmt_lead(s) for s in (leads or []))


# ---------------------------------------------------------------------------
# Discord scheduled-event linking (link an existing event)
# ---------------------------------------------------------------------------

def _parse_discord_event(text: str) -> str | None:
    """Pull a scheduled-event id out of a discord.com/events/<guild>/<id> URL or a raw id."""
    text = (text or "").strip()
    m = re.search(r"events/(?:\d+/)?(\d{15,25})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{15,25}", text):
        return text
    return None


def _discord_event_url(event_id: str) -> str:
    return f"https://discord.com/events/{config.GUILD_ID}/{event_id}"


# ---------------------------------------------------------------------------
# Event-type registry (extensibility seam)
# ---------------------------------------------------------------------------

def _brainlag_panel_fields(ev) -> list[tuple[str, str]]:
    sent = ev.get("_brainlag_links_sent")
    return [("🧠 Brainlag", "Join links sent ✅" if sent else "Join links not sent yet")]


EVENT_TYPES = {
    "generic":  {"label": "Generic",  "extra_fields": None},
    "brainlag": {"label": "Brainlag", "extra_fields": _brainlag_panel_fields},
}


# ---------------------------------------------------------------------------
# Panel content (Components V2 container)
# ---------------------------------------------------------------------------

_STATUS_LABEL = {
    "open":      ("🟢 **OPEN**", discord.Color.green()),
    "locked":    ("🔒 **LOCKED**", discord.Color.orange()),
    "started":   ("▶️ **STARTED**", discord.Color.blurple()),
    "ended":     ("🏁 **ENDED**", discord.Color.greyple()),
    "cancelled": ("✖️ **CANCELLED**", discord.Color.red()),
}


def _status_display(ev) -> tuple[str, discord.Color]:
    """Status label + accent colour, with a FULL overlay on open rsvp events."""
    status_text, color = _STATUS_LABEL.get(ev.get("status"), ("—", discord.Color.greyple()))
    if ev.get("status") == "open" and ev.get("mode") != "datepoll" and _is_full(ev):
        return "🟠 **FULL**", discord.Color.orange()
    return status_text, color


def _datepoll_text(ev) -> str:
    """Top-3 dates by votes for the panel (the full list lives behind a button)."""
    opts = ev.get("date_options", [])
    ranked = _ranked_dates(ev)
    lead = _date_votes(ev, ranked[0]["key"]) if ranked else 0
    lines = ["**📊 Date poll** — top 3 by votes"]
    for o in ranked[:3]:
        votes = _date_votes(ev, o["key"])
        mark = " ⬅️ **leading**" if votes and votes == lead else ""
        lines.append(f"<t:{int(o['at'])}:F> — **{votes}** vote(s){mark}")
    if len(opts) > 3:
        lines.append(f"…and **{len(opts) - 3}** more — press **View all dates**")
    return "\n".join(lines)


def _datepoll_full_text(ev) -> str:
    """Every candidate date with its vote count and voters, ranked by votes."""
    lines = [f"**{ev['name']} — full date poll** (most votes first)"]
    for o in _ranked_dates(ev):
        voters = [p["username"] for p in ev["participants"].values() if o["key"] in p.get("dates", [])]
        lines.append(f"\n<t:{int(o['at'])}:F> — **{len(voters)}** vote(s)")
        if voters:
            lines.append("　" + ", ".join(voters))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persistent event panel view (one registered instance routes every event)
# ---------------------------------------------------------------------------

class EventView(discord.ui.LayoutView):
    """Single persistent Components V2 panel. Built with no event for registration
    (superset of custom_ids), or with a specific event to render its card. Callbacks
    resolve the target event from the message id, so one registration handles every
    event."""

    def __init__(self, ev: dict | None = None):
        super().__init__(timeout=None)
        if ev is None:
            self.add_item(self._registration_container())
            return

        status_text, color = _status_display(ev)
        c = discord.ui.Container(accent_colour=color)
        type_label = EVENT_TYPES.get(ev.get("type"), {}).get("label", ev.get("type", "—"))
        header = f"## {ev['name']}\n{status_text}　·　**Type:** {type_label}"
        if ev.get("host_id"):
            header += f"　·　**Host:** <@{ev['host_id']}>"
        c.add_item(discord.ui.TextDisplay(header))
        if ev.get("description"):
            c.add_item(discord.ui.TextDisplay(ev["description"][:2000]))
        c.add_item(discord.ui.Separator())

        n = len(_roster(ev))
        cap = ev.get("capacity")
        details = [f"**Participants:** {n}/{cap}" if cap else f"**Participants:** {n}"]
        if ev.get("start_at"):
            details.append(f"**Starts:** <t:{int(ev['start_at'])}:F> (<t:{int(ev['start_at'])}:R>)")
        if ev.get("discord_event_id"):
            details.append(f"**Discord event:** [open in Discord]({_discord_event_url(ev['discord_event_id'])})")
        c.add_item(discord.ui.TextDisplay("\n".join(details)))
        if n:
            c.add_item(discord.ui.TextDisplay(f"**Roster**\n{_roster_field(ev)}"))
        if ev.get("mode") == "datepoll" and ev.get("date_options"):
            c.add_item(discord.ui.TextDisplay(_datepoll_text(ev)))
        extra = EVENT_TYPES.get(ev.get("type"), {}).get("extra_fields")
        if extra:
            for name, value in extra(ev):
                c.add_item(discord.ui.TextDisplay(f"**{name}** {value}"))
        c.add_item(discord.ui.TextDisplay(f"-# Event {ev['id']}"))

        for row in self._action_rows(ev):
            c.add_item(row)
        self.add_item(c)

    # ---- component builders ----

    def _registration_container(self) -> discord.ui.Container:
        """Superset of custom_ids so one persistent registration routes every event."""
        c = discord.ui.Container()
        c.add_item(discord.ui.TextDisplay("Event"))
        join = discord.ui.Button(label="Join", custom_id="event:rsvp")
        join.callback = self._on_join
        vote = discord.ui.Button(label="Vote", custom_id="event:datevote")
        vote.callback = self._on_datevote
        leave = discord.ui.Button(label="Leave", custom_id="event:leave")
        leave.callback = self._on_leave
        viewd = discord.ui.Button(label="View all dates", custom_id="event:viewdates")
        viewd.callback = self._on_viewdates
        c.add_item(discord.ui.ActionRow(join, vote, leave, viewd))
        return c

    def _action_rows(self, ev) -> list:
        if ev.get("status") in _TERMINAL:
            return []
        joinable = _is_joinable(ev)
        if ev.get("mode") == "datepoll":
            vote = discord.ui.Button(label="Vote / change dates", style=discord.ButtonStyle.green,
                                     emoji="🗳️", custom_id="event:datevote", disabled=not joinable)
            vote.callback = self._on_datevote
            viewd = discord.ui.Button(label="View all dates", emoji="📋", custom_id="event:viewdates")
            viewd.callback = self._on_viewdates
            leave = discord.ui.Button(label="Leave", style=discord.ButtonStyle.gray, custom_id="event:leave")
            leave.callback = self._on_leave
            return [discord.ui.ActionRow(vote, viewd, leave)]

        full = _is_full(ev)
        join = discord.ui.Button(label="Join", style=discord.ButtonStyle.green, emoji="✍️",
                                 custom_id="event:rsvp", disabled=not joinable or full)
        join.callback = self._on_join
        leave = discord.ui.Button(label="Leave", style=discord.ButtonStyle.gray, custom_id="event:leave")
        leave.callback = self._on_leave
        return [discord.ui.ActionRow(join, leave)]

    async def _on_join(self, interaction: discord.Interaction):
        ev = _event_by_message(interaction.message.id)
        if not ev:
            return await interaction.response.send_message("❌ This event no longer exists.", ephemeral=True)
        if not _is_joinable(ev):
            return await interaction.response.send_message("❌ This event isn't open for signups.", ephemeral=True)
        uid = str(interaction.user.id)
        p = ev["participants"].get(uid)
        if p and p.get("status") == "active":
            return await interaction.response.send_message("❌ You've already joined this event!", ephemeral=True)
        if _is_full(ev):
            return await interaction.response.send_message("❌ This event is full.", ephemeral=True)
        allowed, why = moderation.gate_check(uid)
        if not allowed:
            return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        await interaction.response.send_modal(RSVPModal(ev["id"], mode="rsvp"))

    async def _on_datevote(self, interaction: discord.Interaction):
        ev = _event_by_message(interaction.message.id)
        if not ev:
            return await interaction.response.send_message("❌ This event no longer exists.", ephemeral=True)
        if not _is_joinable(ev):
            return await interaction.response.send_message("❌ This poll is closed.", ephemeral=True)
        uid = str(interaction.user.id)
        if uid not in ev["participants"]:
            allowed, why = moderation.gate_check(uid)
            if not allowed:
                return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
            # New voter — collect a username first, then show the date picker.
            return await interaction.response.send_modal(RSVPModal(ev["id"], mode="datepoll"))
        # Existing participant — personal, pre-filled picker (change votes without leaving).
        await interaction.response.send_message(
            content="Pick the date(s) you can attend — your current picks are pre-selected:",
            view=DateVoteView(ev["id"], uid), ephemeral=True)

    async def _on_leave(self, interaction: discord.Interaction):
        ev = _event_by_message(interaction.message.id)
        if not ev:
            return await interaction.response.send_message("❌ This event no longer exists.", ephemeral=True)
        uid = str(interaction.user.id)
        if uid not in ev["participants"]:
            return await interaction.response.send_message("❌ You're not in this event.", ephemeral=True)
        del ev["participants"][uid]
        store.save_data()
        await update_event_panel(interaction.client, ev)
        await interaction.response.send_message("👋 You've left the event.", ephemeral=True)

    async def _on_viewdates(self, interaction: discord.Interaction):
        ev = _event_by_message(interaction.message.id)
        if not ev:
            return await interaction.response.send_message("❌ This event no longer exists.", ephemeral=True)
        if ev.get("mode") != "datepoll" or not ev.get("date_options"):
            return await interaction.response.send_message("❌ This event has no date poll.", ephemeral=True)
        text = _datepoll_full_text(ev)
        if len(text) <= 1900:
            await interaction.response.send_message(text, ephemeral=True)
        else:
            buf = io.BytesIO(text.encode("utf-8"))
            await interaction.response.send_message(
                "Full date poll attached:",
                file=discord.File(buf, filename=f"{ev['id']}_dates.txt"), ephemeral=True)


def _event_by_message(message_id):
    mid = str(message_id)
    for ev in _events().values():
        if ev.get("panel_message_id") == mid:
            return ev
    return None


# ---------------------------------------------------------------------------
# RSVP modal (username, and optional date votes carried over)
# ---------------------------------------------------------------------------

class RSVPModal(discord.ui.Modal, title="Join Event"):
    def __init__(self, event_id: str, mode: str = "rsvp"):
        super().__init__()
        self.event_id = event_id
        self.mode = mode
        self.username_input = discord.ui.TextInput(
            label="Username", style=discord.TextStyle.short,
            placeholder="Enter your in-game username…", required=True, max_length=32)
        self.add_item(self.username_input)

    async def on_submit(self, interaction: discord.Interaction):
        ev = _event(self.event_id)
        if not ev or not _is_joinable(ev):
            return await interaction.response.send_message("❌ This event isn't open.", ephemeral=True)
        username = self.username_input.value.strip()
        uid = str(interaction.user.id)
        allowed, why = moderation.gate_check(uid)
        if not allowed:
            return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        if self.mode == "rsvp" and _is_full(ev):
            return await interaction.response.send_message("❌ This event is full.", ephemeral=True)
        ev["participants"][uid] = {
            "username": username, "joined_at": datetime.datetime.now().timestamp(),
            "status": "active", "source": "rsvp", "dates": [],
        }
        store.save_data()
        await update_event_panel(interaction.client, ev)
        if self.mode == "datepoll":
            return await interaction.response.send_message(
                content=f"✅ Registered as **{username}** — now pick the date(s) you can attend:",
                view=DateVoteView(ev["id"], uid), ephemeral=True)
        await interaction.response.send_message(
            f"✅ You've joined **{ev['name']}** as **{username}**!", ephemeral=True)


class DateVoteView(discord.ui.View):
    """Ephemeral, per-user date picker pre-filled with the caller's current votes —
    so a participant can change their date votes without leaving the event."""

    def __init__(self, event_id: str, user_id: str):
        super().__init__(timeout=180)
        self.event_id = event_id
        self.user_id = str(user_id)
        ev = _event(event_id) or {}
        current = set(ev.get("participants", {}).get(self.user_id, {}).get("dates", []))
        opts = []
        for o in sorted(ev.get("date_options", []), key=lambda o: o["at"]):
            dt = datetime.datetime.fromtimestamp(o["at"])
            opts.append(discord.SelectOption(label=dt.strftime("%a %d %b %Y %H:%M")[:100],
                                             value=o["key"], default=o["key"] in current))
        if not opts:
            opts = [discord.SelectOption(label="—", value="—")]
        sel = discord.ui.Select(placeholder="Select every date you can attend…",
                                min_values=0, max_values=len(opts), options=opts)
        sel.callback = self._on_pick
        self.add_item(sel)

    async def _on_pick(self, interaction: discord.Interaction):
        ev = _event(self.event_id)
        if not ev or not _is_joinable(ev):
            return await interaction.response.edit_message(content="❌ This poll is closed.", view=None)
        if self.user_id not in ev["participants"]:
            return await interaction.response.edit_message(content="❌ You're not in this event.", view=None)
        picked = [v for v in interaction.data.get("values", []) if v != "—"]
        ev["participants"][self.user_id]["dates"] = picked
        store.save_data()
        await update_event_panel(interaction.client, ev)
        chosen = "\n".join(f"• <t:{int(o['at'])}:F>"
                           for o in sorted(ev["date_options"], key=lambda o: o["at"]) if o["key"] in picked)
        await interaction.response.edit_message(
            content=f"✅ Your date votes updated — **{len(picked)}** date(s):\n{chosen or '—'}", view=None)


# ---------------------------------------------------------------------------
# Panel posting / refresh
# ---------------------------------------------------------------------------

async def post_event_panel(channel, ev):
    msg = await channel.send(view=EventView(ev))
    ev["channel_id"] = str(channel.id)
    ev["panel_message_id"] = str(msg.id)
    store.save_data()


async def update_event_panel(client, ev):
    if not ev.get("panel_message_id") or not ev.get("channel_id"):
        return
    channel = client.get_channel(int(ev["channel_id"]))
    if not channel:
        return
    try:
        msg = await channel.fetch_message(int(ev["panel_message_id"]))
    except discord.NotFound:
        ev["panel_message_id"] = None
        ev["channel_id"] = None
        store.save_data()
        return
    except discord.HTTPException:
        return
    # Terminal events keep the card (final state) but drop the action rows.
    try:
        await msg.edit(view=EventView(ev))
    except discord.HTTPException as e:
        # A panel created before the Components V2 migration is still an embed
        # message; Discord won't let a V2 view be edited onto it (error 50035).
        # Repost it fresh and track the new message (self-heals after one cycle).
        if getattr(e, "code", None) != 50035:
            return
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
        try:
            new = await channel.send(view=EventView(ev))
            ev["panel_message_id"] = str(new.id)
            store.save_data()
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Create modal
# ---------------------------------------------------------------------------

class CreateEventModal(discord.ui.Modal):
    def __init__(self, etype: str, mode: str, discord_event_id: str | None = None):
        super().__init__(title=f"New {EVENT_TYPES[etype]['label']} Event")
        self.etype = etype
        self.mode = mode
        self.discord_event_id = discord_event_id
        self.name_input = discord.ui.TextInput(label="Event name", required=True, max_length=80)
        self.desc_input = discord.ui.TextInput(
            label="Description (newlines & markdown ok)",
            style=discord.TextStyle.paragraph, required=True, max_length=4000)
        self.capacity_input = discord.ui.TextInput(
            label="Max participants (blank = unlimited)", required=False, max_length=6)
        self.add_item(self.name_input)
        self.add_item(self.desc_input)
        self.add_item(self.capacity_input)
        if mode == "datepoll":
            self.dates_input = discord.ui.TextInput(
                label="Candidate dates (one per line)",
                style=discord.TextStyle.paragraph, required=True, max_length=600,
                placeholder="dd-mm-yyyy hh:mm\ndd-mm-yyyy hh:mm")
            self.add_item(self.dates_input)
        else:
            self.start_input = discord.ui.TextInput(
                label="Start (dd-mm-yyyy [hh:mm], blank = TBD)", required=False, max_length=20)
            self.add_item(self.start_input)
        self.reminders_input = discord.ui.TextInput(
            label="Reminders before start (blank = 1h, 0)", required=False, max_length=40,
            placeholder="e.g. 1d 1h 0")
        self.add_item(self.reminders_input)

    async def on_submit(self, interaction: discord.Interaction):
        cap = None
        if self.capacity_input.value.strip():
            try:
                cap = int(self.capacity_input.value.strip())
                if cap <= 0:
                    raise ValueError
            except ValueError:
                return await interaction.response.send_message("❌ Max participants must be a positive number.", ephemeral=True)

        start_at = None
        date_options = []
        try:
            reminders = _parse_reminders(self.reminders_input.value)
            if self.mode == "datepoll":
                for i, line in enumerate(self.dates_input.value.splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    if len(date_options) >= MAX_DATE_OPTIONS:
                        raise ValueError(f"Too many dates (max {MAX_DATE_OPTIONS}).")
                    dt = parse_eu_datetime(line)
                    date_options.append({"key": f"d{i}", "at": dt.timestamp(),
                                         "label": dt.strftime("%a %d %b %Y %H:%M")})
                if not date_options:
                    raise ValueError("Provide at least one candidate date.")
            else:
                if self.start_input.value.strip():
                    start_at = parse_eu_datetime(self.start_input.value.strip()).timestamp()
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        eid = f"EVT-{store.next_id()}"
        ev = {
            "id": eid, "name": self.name_input.value.strip(),
            "description": self.desc_input.value.strip(),
            "type": self.etype, "mode": self.mode, "status": "open",
            "channel_id": None, "panel_message_id": None,
            "created_by": str(interaction.user.id),
            "created_at": datetime.datetime.now().timestamp(),
            "capacity": cap, "start_at": start_at,
            "reminders": reminders, "reminders_sent": [],
            "date_options": date_options, "participants": {},
            "discord_event_id": self.discord_event_id,
            "host_id": str(interaction.user.id),
        }
        _events()[eid] = ev
        await post_event_panel(interaction.channel, ev)
        await interaction.response.send_message(
            f"✅ Created **{ev['name']}** (`{eid}`, {EVENT_TYPES[self.etype]['label']}, "
            f"{self.mode}) and posted its panel here.", ephemeral=True)


class EditEventModal(discord.ui.Modal, title="Edit Event"):
    def __init__(self, ev):
        super().__init__()
        self.event_id = ev["id"]
        self.name_input = discord.ui.TextInput(label="Event name", required=True,
                                               default=ev["name"], max_length=80)
        self.desc_input = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, required=True,
            default=ev.get("description", ""), max_length=4000)
        self.start_input = discord.ui.TextInput(
            label="Start (dd-mm-yyyy [hh:mm], blank = TBD)", required=False, max_length=20,
            default=(datetime.datetime.fromtimestamp(ev["start_at"]).strftime("%d-%m-%Y %H:%M")
                     if ev.get("start_at") else ""))
        self.reminders_input = discord.ui.TextInput(
            label="Reminders before start", required=False, max_length=40,
            placeholder="e.g. 1d 1h 0", default=_format_reminders(ev.get("reminders")))
        self.capacity_input = discord.ui.TextInput(
            label="Max participants (blank = unlimited)", required=False, max_length=6,
            default=str(ev["capacity"]) if ev.get("capacity") else "")
        self.add_item(self.name_input)
        self.add_item(self.desc_input)
        self.add_item(self.start_input)
        self.add_item(self.reminders_input)
        self.add_item(self.capacity_input)

    async def on_submit(self, interaction: discord.Interaction):
        ev = _event(self.event_id)
        if not ev:
            return await interaction.response.send_message("❌ Event not found.", ephemeral=True)
        try:
            reminders = _parse_reminders(self.reminders_input.value)
            cap = None
            raw_cap = self.capacity_input.value.strip()
            if raw_cap:
                if not raw_cap.isdigit() or int(raw_cap) <= 0:
                    raise ValueError("Max participants must be a positive number.")
                cap = int(raw_cap)
            new_start = (parse_eu_datetime(self.start_input.value.strip()).timestamp()
                         if self.start_input.value.strip() else None)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        ev["name"] = self.name_input.value.strip()
        ev["description"] = self.desc_input.value.strip()
        ev["reminders"] = reminders
        ev["capacity"] = cap
        if new_start != ev.get("start_at"):
            ev["start_at"] = new_start
            ev["reminders_sent"] = []
        store.save_data()
        await update_event_panel(interaction.client, ev)
        await interaction.response.send_message("✅ Event updated.", ephemeral=True)


# ---------------------------------------------------------------------------
# Brainlag link distribution
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"^\s*(?P<label>.+?)\s*[:\-]\s*(?P<url>https?://\S+)\s*$")


class BrainlagLinksModal(discord.ui.Modal, title="Brainlag Join Links"):
    def __init__(self, event_id: str):
        super().__init__()
        self.event_id = event_id
        self.links_input = discord.ui.TextInput(
            label="Paste the Player: link block", style=discord.TextStyle.paragraph,
            required=True, max_length=4000,
            placeholder="Player 1: https://brainlag.fireheart.dev/player?code=...")
        self.add_item(self.links_input)

    async def on_submit(self, interaction: discord.Interaction):
        ev = _event(self.event_id)
        if not ev:
            return await interaction.response.send_message("❌ Event not found.", ephemeral=True)

        # Parse the pasted block.
        parsed = []
        for line in self.links_input.value.splitlines():
            if not line.strip():
                continue
            m = _LINK_RE.match(line)
            if m:
                parsed.append((m.group("label").strip(), m.group("url").strip()))
        if not parsed:
            return await interaction.response.send_message(
                "❌ Couldn't find any `Label: https://…` lines.", ephemeral=True)

        # Match labels to participant usernames (case-insensitive).
        by_name = {}
        for uid, p in ev["participants"].items():
            if p.get("status") == "active":
                by_name.setdefault(p["username"].strip().lower(), uid)
        matched = {}            # uid -> url
        unmatched = []          # urls whose label didn't match
        for label, url in parsed:
            uid = by_name.get(label.lower())
            if uid and uid not in matched:
                matched[uid] = url
            else:
                unmatched.append((label, url))

        without_link = [uid for uid, p in ev["participants"].items()
                        if p.get("status") == "active" and uid not in matched]

        if not unmatched:
            await interaction.response.defer(ephemeral=True)
            summary = await _send_brainlag_links(interaction.client, ev, matched)
            await interaction.followup.send(summary, ephemeral=True)
            return

        # Need host resolution for the leftover links.
        view = BrainlagResolveView(ev["id"], matched, unmatched, without_link)
        await interaction.response.send_message(
            content=view.prompt(), view=view, ephemeral=True)


async def _send_brainlag_links(client, ev, matched: dict) -> str:
    sent, failed = [], []
    for uid, url in matched.items():
        p = ev["participants"].get(uid)
        name = p["username"] if p else uid
        ok = await _dm(client, uid, f"Your {ev['name']} join link 🧠",
                       f"Here's your personal join link for **{ev['name']}**:\n{url}",
                       discord.Color.blurple())
        (sent if ok else failed).append((uid, name))
    ev["_brainlag_links_sent"] = True
    store.save_data()
    await update_event_panel(client, ev)

    no_link = [(uid, p["username"]) for uid, p in ev["participants"].items()
               if p.get("status") == "active" and uid not in matched]
    lines = [f"**Brainlag links — {ev['name']}**",
             f"✅ Sent: {len(sent)}" + ("".join(f"\n   • {n} (<@{u}>)" for u, n in sent) if sent else "")]
    if failed:
        lines.append(f"❌ DM failed (DMs closed): {len(failed)}" +
                     "".join(f"\n   • {n} (<@{u}>)" for u, n in failed))
    if no_link:
        lines.append(f"⚠️ No link assigned: {len(no_link)}" +
                     "".join(f"\n   • {n} (<@{u}>)" for u, n in no_link))
    return "\n".join(lines)[:1900]


class BrainlagResolveView(discord.ui.View):
    """Host resolves unmatched links to participants. Up to 4 selects per page
    (Discord allows 5 action rows; the 5th holds the Send button)."""

    PAGE = 4

    def __init__(self, event_id, matched, unmatched, without_link):
        super().__init__(timeout=600)
        self.event_id = event_id
        self.matched = dict(matched)
        self.unmatched = unmatched          # list[(label, url)]
        self.without_link = list(without_link)
        self.assign = {}                    # index -> uid

        ev = _event(event_id)
        page = self.unmatched[:self.PAGE]
        for i, (label, _url) in enumerate(page):
            opts = [discord.SelectOption(label="Skip this link", value="skip")]
            for uid in self.without_link:
                p = ev["participants"].get(uid, {})
                opts.append(discord.SelectOption(label=p.get("username", uid)[:100], value=uid))
            sel = discord.ui.Select(placeholder=f"Assign link for “{label}”…",
                                    min_values=1, max_values=1, options=opts[:25], row=i)
            sel.callback = self._make_cb(i)
            self.add_item(sel)

        send = discord.ui.Button(label="Send DMs", style=discord.ButtonStyle.green, emoji="📨", row=4)
        send.callback = self._on_send
        self.add_item(send)

    def prompt(self) -> str:
        extra = (f"\n_Showing the first {self.PAGE} of {len(self.unmatched)} unmatched links; "
                 f"send, then run the command again for the rest._"
                 if len(self.unmatched) > self.PAGE else "")
        return ("🧠 **Some links couldn't be matched automatically.**\n"
                "Assign each to a participant (or skip), then press **Send DMs**." + extra)

    def _make_cb(self, idx):
        async def cb(interaction: discord.Interaction):
            value = interaction.data["values"][0]
            if value == "skip":
                self.assign.pop(idx, None)
            else:
                self.assign[idx] = value
            await interaction.response.defer()
        return cb

    async def _on_send(self, interaction: discord.Interaction):
        ev = _event(self.event_id)
        if not ev:
            return await interaction.response.send_message("❌ Event not found.", ephemeral=True)
        final = dict(self.matched)
        for idx, uid in self.assign.items():
            if uid not in final:                 # don't overwrite an auto-match
                final[uid] = self.unmatched[idx][1]
        await interaction.response.defer()
        summary = await _send_brainlag_links(interaction.client, ev, final)
        await interaction.edit_original_response(content=summary, view=None)


# ---------------------------------------------------------------------------
# Date-poll close (host picks the winning date)
# ---------------------------------------------------------------------------

class PollCloseView(discord.ui.View):
    """Host-only flow: pick the winning date from the top 3, with options to
    notify confirmed participants and to remove those who voted for other dates."""

    def __init__(self, bot, event_id):
        super().__init__(timeout=300)
        self.bot = bot
        self.event_id = event_id
        self.notify = False
        self.kick = False
        ev = _event(event_id)
        self.top = _ranked_dates(ev)[:3] if ev else []
        self.chosen = self.top[0]["key"] if self.top else None

        opts = []
        for o in self.top:
            dt = datetime.datetime.fromtimestamp(o["at"])
            opts.append(discord.SelectOption(
                label=dt.strftime("%a %d %b %Y %H:%M"),
                description=f"{_date_votes(ev, o['key'])} vote(s)",
                value=o["key"], default=(o["key"] == self.chosen)))
        select = discord.ui.Select(
            placeholder="Pick the winning date…", row=0, min_values=1, max_values=1,
            options=opts or [discord.SelectOption(label="—", value="—")], disabled=not opts)
        select.callback = self._on_pick
        self.add_item(select)
        self._sync()

    def _sync(self):
        self.toggle_notify.label = f"Notify confirmed: {'ON' if self.notify else 'OFF'}"
        self.toggle_notify.style = discord.ButtonStyle.green if self.notify else discord.ButtonStyle.gray
        self.toggle_kick.label = f"Remove others: {'ON' if self.kick else 'OFF'}"
        self.toggle_kick.style = discord.ButtonStyle.danger if self.kick else discord.ButtonStyle.gray

    async def _on_pick(self, interaction: discord.Interaction):
        self.chosen = interaction.data["values"][0]
        await interaction.response.defer()

    @discord.ui.button(label="Notify confirmed: OFF", row=1)
    async def toggle_notify(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.notify = not self.notify
        self._sync()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Remove others: OFF", row=1)
    async def toggle_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.kick = not self.kick
        self._sync()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Confirm & close", style=discord.ButtonStyle.green, emoji="✅", row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        ev = _event(self.event_id)
        if not ev:
            return await interaction.response.send_message("❌ Event not found.", ephemeral=True)
        if not self.chosen or self.chosen == "—":
            return await interaction.response.send_message("❌ Pick a date first.", ephemeral=True)
        winner = next((o for o in ev.get("date_options", []) if o["key"] == self.chosen), None)
        if not winner:
            return await interaction.response.send_message("❌ That date no longer exists.", ephemeral=True)
        await interaction.response.defer()

        ev["start_at"] = winner["at"]
        ev["mode"] = "rsvp"
        ev["status"] = "locked"
        ev["reminders_sent"] = []
        winners, others = [], []
        for uid, p in list(ev["participants"].items()):
            if self.chosen in p.get("dates", []):
                p["status"] = "active"
                winners.append(uid)
            else:
                others.append(uid)
        for uid in others:
            if self.kick:
                ev["participants"].pop(uid, None)
            else:
                ev["participants"][uid]["status"] = "unavailable"
        store.save_data()
        await update_event_panel(self.bot, ev)

        # Channel notice.
        channel = self.bot.get_channel(int(ev["channel_id"])) if ev.get("channel_id") else None
        if channel:
            note = (f"📅 **{ev['name']}** date locked: <t:{int(winner['at'])}:F> "
                    f"(<t:{int(winner['at'])}:R>).")
            if others:
                note += f"\n{len(others)} non-attendee(s) {'removed' if self.kick else 'marked unavailable'}."
            await channel.send(note)

        # Optional DM to confirmed participants — plus the host, always.
        sent = 0
        if self.notify:
            recipients = list(winners)
            host = ev.get("host_id")
            if host and host not in recipients:
                recipients.append(host)
            for uid in recipients:
                if await _dm(self.bot, uid, f"{ev['name']} — date confirmed 📅",
                             f"The date for **{ev['name']}** is set: <t:{int(winner['at'])}:F> "
                             f"(<t:{int(winner['at'])}:R>). See you there!", discord.Color.green()):
                    sent += 1

        summary = (f"✅ Locked <t:{int(winner['at'])}:F> — {len(winners)} confirmed, "
                   f"{len(others)} {'removed' if self.kick else 'unavailable'}")
        summary += f", {sent} notified." if self.notify else "."
        await interaction.edit_original_response(content=summary, view=None)


# ---------------------------------------------------------------------------
# Roster field (shown on the event panel)
# ---------------------------------------------------------------------------

def _roster_field(ev) -> str:
    roster = sorted(_roster(ev), key=lambda p: p.get("joined_at", 0))
    if not roster:
        return "—"
    lines, total = [], len(roster)
    for p in roster:
        line = f"• {p['username']} (<@{_uid_of(ev, p)}>)"
        if sum(len(l) + 1 for l in lines) + len(line) > 980:
            lines.append(f"…and {total - len(lines)} more")
            break
        lines.append(line)
    return "\n".join(lines)[:1024]


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = app_commands.Group(name="event", description="Event management",
                                        guild_ids=[config.GUILD_ID],
                                        default_permissions=discord.Permissions(create_events=True))
        self.poll_group = app_commands.Group(name="poll", description="Date-poll controls",
                                             parent=self.group)
        self.brainlag_group = app_commands.Group(name="brainlag", description="Brainlag tools",
                                                 parent=self.group)
        self._register()
        bot.tree.add_command(self.group)

    @tasks.loop(minutes=1)
    async def events_tick(self):
        now = datetime.datetime.now().timestamp()
        changed = False
        for ev in list(_active_events()):
            if not ev.get("start_at"):
                continue
            for lead in ev.get("reminders", []):
                if lead in ev["reminders_sent"]:
                    continue
                if now >= ev["start_at"] - lead:
                    await self._send_reminder(ev, lead)
                    ev["reminders_sent"].append(lead)
                    changed = True
            if ev["status"] in ("open", "locked") and now >= ev["start_at"]:
                ev["status"] = "started"
                changed = True
                await update_event_panel(self.bot, ev)
        if changed:
            store.save_data()

    async def _send_reminder(self, ev, lead):
        channel = self.bot.get_channel(int(ev["channel_id"])) if ev.get("channel_id") else None
        if not channel:
            return
        mentions = " ".join(f"<@{uid}>" for uid, p in ev["participants"].items()
                            if p.get("status") == "active")
        when = ("is starting now" if lead == 0
                else f"starts <t:{int(ev['start_at'])}:R>")
        embed = discord.Embed(title=f"📣 {ev['name']}", description=f"The event {when}!",
                              color=discord.Color.blurple(), timestamp=datetime.datetime.now())
        await channel.send(content=mentions or None, embed=embed)

    async def _ev_autocomplete(self, interaction: discord.Interaction, current: str):
        cur = current.lower()
        out = []
        for ev in _active_events():
            label = f"{ev['name']} ({ev['id']})"
            if cur in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=ev["id"]))
        return out[:25]

    async def _resolve(self, interaction, event_id) -> dict | None:
        ev = _event(event_id)
        if not ev or ev.get("status") in _TERMINAL:
            await interaction.response.send_message("❌ No active event with that id.", ephemeral=True)
            return None
        return ev

    async def _set_status(self, interaction, event_id, status, verb):
        ev = await self._resolve(interaction, event_id)
        if not ev:
            return
        ev["status"] = status
        store.save_data()
        await update_event_panel(self.bot, ev)
        await interaction.response.send_message(f"✅ **{ev['name']}** {verb}.", ephemeral=True)

    def _register(self):
        group, poll, brainlag = self.group, self.poll_group, self.brainlag_group
        admin = app_commands.checks.has_permissions(create_events=True)
        ac = app_commands.autocomplete(event=self._ev_autocomplete)
        describe = app_commands.describe(event="Which event")

        @group.command(name="create", description="Create an event and post its panel here")
        @app_commands.describe(type="Event type", mode="How people join",
                               discord_link="Optional: link an existing Discord event (URL or ID)")
        @app_commands.choices(
            type=[app_commands.Choice(name="Generic", value="generic"),
                  app_commands.Choice(name="Brainlag", value="brainlag")],
            mode=[app_commands.Choice(name="RSVP (button)", value="rsvp"),
                  app_commands.Choice(name="Date poll", value="datepoll")])
        @admin
        async def event_create(interaction: discord.Interaction, type: str, mode: str,
                              discord_link: str = None):
            deid = None
            if discord_link:
                deid = _parse_discord_event(discord_link)
                if not deid:
                    return await interaction.response.send_message(
                        "❌ That doesn't look like a Discord event link or ID.", ephemeral=True)
            await interaction.response.send_modal(CreateEventModal(type, mode, deid))

        @group.command(name="edit",
                       description="Edit an event (name, description, start, reminders, capacity)")
        @app_commands.describe(event="Which event",
                               discord_link="Set/replace the linked Discord event (URL or ID); use '-' to unlink")
        @ac
        @admin
        async def event_edit(interaction: discord.Interaction, event: str, discord_link: str = None):
            ev = await self._resolve(interaction, event)
            if not ev:
                return
            if discord_link is not None:
                if discord_link.strip() in ("", "-"):
                    ev["discord_event_id"] = None
                else:
                    deid = _parse_discord_event(discord_link)
                    if not deid:
                        return await interaction.response.send_message(
                            "❌ That doesn't look like a Discord event link or ID.", ephemeral=True)
                    ev["discord_event_id"] = deid
                store.save_data()
                await update_event_panel(self.bot, ev)
            await interaction.response.send_modal(EditEventModal(ev))

        _STATUS_VERB = {
            "open": "is now **OPEN**", "locked": "is now **LOCKED**",
            "started": "has **STARTED**", "ended": "has **ENDED**",
            "cancelled": "was **CANCELLED**",
        }

        @group.command(name="status", description="Change an event's status")
        @app_commands.describe(event="Which event", state="New status")
        @app_commands.choices(state=[
            app_commands.Choice(name="Open (allow joins/votes)", value="open"),
            app_commands.Choice(name="Lock (freeze the roster)", value="locked"),
            app_commands.Choice(name="Started", value="started"),
            app_commands.Choice(name="End (disable panel)", value="ended"),
            app_commands.Choice(name="Cancel (disable panel)", value="cancelled")])
        @ac
        @admin
        async def event_status(interaction: discord.Interaction, event: str, state: str):
            await self._set_status(interaction, event, state, _STATUS_VERB[state])

        @group.command(name="roster",
                       description="Add a participant (give a username) or remove one (omit username)")
        @app_commands.describe(event="Which event", user="Member to add/remove",
                               username="In-game name to ADD them with — leave blank to REMOVE")
        @ac
        @admin
        async def event_roster(interaction: discord.Interaction, event: str,
                              user: discord.Member, username: str = None):
            ev = await self._resolve(interaction, event)
            if not ev:
                return
            uid = str(user.id)
            if username:
                existing = ev["participants"].get(uid)
                if not existing and _is_full(ev):
                    return await interaction.response.send_message("❌ Event is full.", ephemeral=True)
                ev["participants"][uid] = {
                    "username": username.strip(),
                    "joined_at": existing.get("joined_at") if existing else datetime.datetime.now().timestamp(),
                    "status": "active", "source": "host",
                    "dates": existing.get("dates", []) if existing else [],
                }
                store.save_data()
                await update_event_panel(self.bot, ev)
                info = moderation.get_moderation_info(user.id)
                note = "" if info.startswith("✅") else f"\n⚠️ Heads up — this user has a moderation record:\n{info}"
                await interaction.response.send_message(
                    f"✅ Added {user.mention} as **{username.strip()}**.{note}", ephemeral=True)
            else:
                if uid not in ev["participants"]:
                    return await interaction.response.send_message(
                        "❌ Not in this event (pass a username to add them).", ephemeral=True)
                del ev["participants"][uid]
                store.save_data()
                await update_event_panel(self.bot, ev)
                await interaction.response.send_message(f"✅ Removed {user.mention}.", ephemeral=True)

        # ---- date poll ----

        @poll.command(name="close", description="Close the date poll — pick the winning date (top 3 by votes)")
        @describe
        @ac
        @admin
        async def poll_close(interaction: discord.Interaction, event: str):
            ev = await self._resolve(interaction, event)
            if not ev:
                return
            if ev.get("mode") != "datepoll":
                return await interaction.response.send_message("❌ That event isn't a date poll.", ephemeral=True)
            if not ev.get("date_options"):
                return await interaction.response.send_message("❌ No candidate dates.", ephemeral=True)
            await interaction.response.send_message(
                content="**Close the date poll.** Pick the winning date (top 3 by votes), "
                        "toggle the options, then confirm:",
                view=PollCloseView(self.bot, ev["id"]), ephemeral=True)

        # ---- brainlag ----

        @brainlag.command(name="links", description="Distribute Brainlag join links to players")
        @describe
        @ac
        @admin
        async def brainlag_links(interaction: discord.Interaction, event: str):
            ev = await self._resolve(interaction, event)
            if not ev:
                return
            if ev.get("type") != "brainlag":
                return await interaction.response.send_message(
                    "❌ This is only available for Brainlag events.", ephemeral=True)
            if not _roster(ev):
                return await interaction.response.send_message(
                    "❌ This event has no participants yet.", ephemeral=True)
            await interaction.response.send_modal(BrainlagLinksModal(ev["id"]))


async def setup(bot: commands.Bot):
    cog = EventsCog(bot)
    await bot.add_cog(cog)
    return cog

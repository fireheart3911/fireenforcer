import datetime
import re

import discord
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo, available_timezones
except ImportError:  # pragma: no cover
    ZoneInfo = None
    available_timezones = lambda: set()

import storage as store
import config


DEFAULT_TZ = "Europe/Berlin"


def get_user_tz_name(user_id: str) -> str:
    """The user's stored IANA timezone, or the Europe/Berlin default."""
    return store.storage.get("user_prefs", {}).get(str(user_id), {}).get("timezone", DEFAULT_TZ)


def get_user_tz(user_id: str):
    """A ZoneInfo for the user, falling back to the default if unset/invalid."""
    if ZoneInfo is None:
        return None
    name = get_user_tz_name(user_id)
    try:
        return ZoneInfo(name)
    except Exception:
        try:
            return ZoneInfo(DEFAULT_TZ)
        except Exception:
            return None


def validate_tz_name(name: str) -> str:
    """Return a canonical IANA name or raise ValueError.

    On Windows this needs the `tzdata` package; we surface a helpful message.
    """
    name = name.strip()
    if ZoneInfo is None:
        raise ValueError("Timezone support is unavailable on this Python build.")
    try:
        ZoneInfo(name)
    except Exception:
        raise ValueError(
            f"Unknown timezone '{name}'. Use an IANA name like `Europe/Berlin` or `Asia/Tokyo`.\n"
            "(If you're on Windows and every name fails, run `pip install tzdata`.)"
        )
    return name


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# (key, emoji, label) — order is how they appear in the dropdown
AWAY_TYPES = [
    ("gaming",   "🎮", "In a game"),
    ("general",  "🌙", "Away"),
    ("brb",      "🚶", "Be right back"),
    ("eating",   "🍽️", "Eating"),
    ("sleeping", "😴", "Sleeping"),
    ("working",  "💼", "Working / Busy"),
    ("studying", "📚", "Studying"),
]
AWAY_LOOKUP = {key: (emoji, label) for key, emoji, label in AWAY_TYPES}

_DURATION_RE = re.compile(r"(\d+)\s*([dhm])")


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

def parse_away_duration(text: str, tz=None) -> datetime.datetime | None:
    """Parse an away time. Returns a future datetime, or None for indefinite.

    Accepts:
      - "" / "indefinite" / "none" / "-"  → None (no set return)
      - duration combos: "30m", "2h", "2h30m", "1d6h"  (timezone-independent)
      - absolute clock time: "14:30" — interpreted in `tz` (the user's zone)
    Raises ValueError on anything else.
    """
    text = text.strip().lower()
    if not text or text in ("indefinite", "none", "-"):
        return None

    # Absolute HH:MM (only if there are no duration units present)
    if ":" in text and not _DURATION_RE.search(text):
        try:
            hour, minute = map(int, text.split(":"))
        except ValueError:
            raise ValueError("Invalid clock time. Use HH:MM, e.g. 14:30")
        now_tz = datetime.datetime.now(tz)  # aware if tz given
        return_time = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if return_time <= now_tz:
            return_time += datetime.timedelta(days=1)
        return return_time

    # Duration combo — zone-independent, relative to now
    matches = _DURATION_RE.findall(text)
    consumed = "".join(f"{n}{u}" for n, u in matches)
    if not matches or consumed != re.sub(r"\s+", "", text):
        raise ValueError("Invalid format. Use 30m, 2h, 2h30m, 1d6h, or 14:30")

    delta = datetime.timedelta()
    for num, unit in matches:
        num = int(num)
        delta += datetime.timedelta(days=num) if unit == "d" else \
                 datetime.timedelta(hours=num) if unit == "h" else \
                 datetime.timedelta(minutes=num)
    return datetime.datetime.now() + delta


def parse_play_start(text: str, tz=None) -> datetime.datetime:
    """Parse a 'playing since' time for in-game status (counts UP).

    Accepts:
      - "" → now (just started)
      - duration combos: "30m", "2h30m" → that long ago  (timezone-independent)
      - absolute clock time: "14:30" → today in `tz` (or yesterday if future)
    Raises ValueError on anything else.
    """
    text = text.strip().lower()
    if not text:
        return datetime.datetime.now()

    if ":" in text and not _DURATION_RE.search(text):
        try:
            hour, minute = map(int, text.split(":"))
        except ValueError:
            raise ValueError("Invalid clock time. Use HH:MM, e.g. 14:30")
        now_tz = datetime.datetime.now(tz)
        start = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start > now_tz:
            start -= datetime.timedelta(days=1)
        return start

    matches = _DURATION_RE.findall(text)
    consumed = "".join(f"{n}{u}" for n, u in matches)
    if not matches or consumed != re.sub(r"\s+", "", text):
        raise ValueError("Invalid format. Leave blank for now, or use 30m, 2h30m, 14:30")

    delta = datetime.timedelta()
    for num, unit in matches:
        num = int(num)
        delta += datetime.timedelta(days=num) if unit == "d" else \
                 datetime.timedelta(hours=num) if unit == "h" else \
                 datetime.timedelta(minutes=num)
    return datetime.datetime.now() - delta


def parse_vacation_datetime(text: str) -> datetime.datetime:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' as a naive local datetime.

    Timezone is intentionally NOT applied here — dates are interpreted in the
    host's local time. The vacation 'timezone' field is purely informational
    (where the person will be / how reachable), shown on the board.
    """
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Invalid date '{text}'. Use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`.")


_TZ_ABBR = {
    "UTC": 0, "GMT": 0, "Z": 0,
    "CET": 1, "CEST": 2, "BST": 1, "WET": 0, "EET": 2, "EEST": 3,
    "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
    "JST": 9, "KST": 9, "IST": 5.5, "AEST": 10, "AEDT": 11, "NZST": 12,
}

_TZ_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?([+-]?\d{1,2})(?::?(\d{2}))?$")


def normalize_tz_offset(text: str) -> str:
    """Turn a free-text timezone into a 'UTC±X' string.

    Handles offsets ('+9', 'UTC-5', '+5:30', '530') and common abbreviations
    ('JST', 'PST', …). Falls back to the original text if it can't be parsed.
    """
    text = text.strip()
    if not text:
        return ""

    compact = text.upper().replace(" ", "")
    if compact in _TZ_ABBR:
        offset = _TZ_ABBR[compact]
    else:
        m = _TZ_OFFSET_RE.match(compact)
        if not m:
            return text  # unrecognised — keep what the user wrote
        hours = int(m.group(1))
        minutes = int(m.group(2)) if m.group(2) else 0
        offset = hours + (minutes / 60 if hours >= 0 else -minutes / 60)

    if offset == 0:
        return "UTC"
    sign = "+" if offset > 0 else "-"
    a = abs(offset)
    h = int(a)
    mm = int(round((a - h) * 60))
    return f"UTC{sign}{h}" + (f":{mm:02d}" if mm else "")


# ---------------------------------------------------------------------------
# Board rendering
# ---------------------------------------------------------------------------

def _build_status_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📊 User Status Board",
        description="Current user status updates:",
        color=discord.Color.blue(),
        timestamp=datetime.datetime.now(),
    )

    now = datetime.datetime.now().timestamp()

    # --- collect active lines: away/queue stops + currently-active vacations ---
    active_lines = []

    for info in store.storage.get("user_statuses", {}).values():
        active_lines.append(info["message"])

    upcoming_vac = []  # (start_at, line) so we can sort by start
    for uid, vac_list in store.storage.get("vacations", {}).items():
        for v in vac_list:
            # Board shows destination + timezone (reachability removed).
            tz = normalize_tz_offset(v.get("tz_note", ""))
            tz_part = f" · 🌍 {tz}" if tz else ""
            dest = v.get("destination", "").strip()
            if v["start_at"] <= now <= v["end_at"]:
                head = dest if dest else "on vacation"
                active_lines.append(
                    f"🏖️ <@{uid}> · {head} · back <t:{int(v['end_at'])}:R>{tz_part}"
                )
            elif v["start_at"] > now:
                dest_part = f"{dest} · " if dest else ""
                upcoming_vac.append((
                    v["start_at"],
                    f"📅 <@{uid}> · {dest_part}<t:{int(v['start_at'])}:d>–<t:{int(v['end_at'])}:d>{tz_part}"
                ))

    if active_lines:
        embed.add_field(name="🔔 Active Statuses", value="\n".join(active_lines), inline=False)
    else:
        embed.add_field(name="🔔 Active Statuses", value="No active statuses", inline=False)

    if upcoming_vac:
        upcoming_vac.sort(key=lambda t: t[0])
        embed.add_field(name="📅 Upcoming Vacations",
                        value="\n".join(line for _, line in upcoming_vac), inline=False)

    embed.set_footer(text="Use the buttons below to update your status")
    return embed


async def update_status_board(client: commands.Bot, status_channel_id: int):
    status_data = store.storage.get("status_message", {})
    if not status_data.get("message_id"):
        return

    status_channel = client.get_channel(int(status_data["channel_id"]))
    if not status_channel:
        return

    try:
        msg = await status_channel.fetch_message(int(status_data["message_id"]))
        await msg.edit(embed=_build_status_embed(), view=StatusView())
    except discord.NotFound:
        print("Status message not found, recreating...")
        await setup_status_message(client, status_channel_id)


async def setup_status_message(client: commands.Bot, status_channel_id: int):
    status_channel = client.get_channel(status_channel_id)
    if not status_channel:
        print("Status channel not found!")
        return

    status_data = store.storage.get("status_message", {})
    status_message_id = status_data.get("message_id")
    status_message = None

    if status_message_id:
        try:
            status_message = await status_channel.fetch_message(int(status_message_id))
        except discord.NotFound:
            print("Status message not found, creating new one.")

    embed = _build_status_embed()

    if status_message:
        await status_message.edit(embed=embed, view=StatusView())
        print("Updated existing status message.")
    else:
        status_message = await status_channel.send(embed=embed, view=StatusView())
        store.storage["status_message"]["message_id"] = str(status_message.id)
        store.storage["status_message"]["channel_id"] = str(status_channel.id)
        store.save_data()
        print("Created new status message.")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

async def log_status_event(
    client: commands.Bot,
    guild_id: int,
    status_log_channel_id: int,
    event_type: str,
    user_id: str,
    details: str = "",
):
    log_channel = client.get_channel(status_log_channel_id)
    if not log_channel:
        return
    try:
        time_str = datetime.datetime.now().strftime("%H:%M")
        guild = client.get_guild(guild_id)
        user_name = f"User-{user_id}"
        if guild:
            member = guild.get_member(int(user_id))
            if member:
                user_name = member.display_name
            else:
                try:
                    member = await guild.fetch_member(int(user_id))
                    user_name = member.display_name
                except Exception:
                    pass
        await log_channel.send(f"`[{time_str}]` {user_name} {event_type}: {details}")
    except Exception as e:
        print(f"Failed to log status event: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def has_active_vacation(user_id: str) -> bool:
    """True if the user is currently within one of their vacation windows."""
    now = datetime.datetime.now().timestamp()
    for v in store.storage.get("vacations", {}).get(user_id, []):
        if v["start_at"] <= now <= v["end_at"]:
            return True
    return False


# ---------------------------------------------------------------------------
# Queue-stop role ping
# ---------------------------------------------------------------------------

def get_ping_role(guild: discord.Guild):
    """Return the configured queue-stop ping role, or None."""
    if guild is None or config.QUEUE_STOP_PING_ROLE_ID is None:
        return None
    return guild.get_role(config.QUEUE_STOP_PING_ROLE_ID)


async def _get_or_create_qs_thread(client: commands.Bot):
    """Return the private queue-stop thread, creating it if needed. May return None."""
    data = store.storage.get("queue_stop_thread", {})
    thread_id = data.get("thread_id")

    if thread_id:
        thread = client.get_channel(int(thread_id))
        if thread is None:
            try:
                thread = await client.fetch_channel(int(thread_id))
            except (discord.NotFound, discord.HTTPException):
                thread = None
        if isinstance(thread, discord.Thread) and not thread.archived:
            return thread

    # Create a fresh private thread off the status channel
    parent = client.get_channel(config.STATUS_CHANNEL_ID.id)
    if not isinstance(parent, discord.TextChannel):
        return None
    try:
        thread = await parent.create_thread(
            name="queue-stop-notifications",
            type=discord.ChannelType.private_thread,
            invitable=False,
            reason="Queue stop notifications",
        )
    except discord.HTTPException as e:
        print(f"Failed to create queue-stop thread: {e}")
        return None

    store.storage["queue_stop_thread"] = {
        "channel_id": str(parent.id),
        "thread_id": str(thread.id),
    }
    store.save_data()
    return thread


async def _send_queue_stop_ping(client: commands.Bot, creator_id: str, arrive_ts: int):
    """Ping the queue-stop role inside the private notification thread."""
    if config.QUEUE_STOP_PING_ROLE_ID is None:
        return

    thread = await _get_or_create_qs_thread(client)
    if thread is None:
        return

    role_mention = f"<@&{config.QUEUE_STOP_PING_ROLE_ID}>"
    try:
        await thread.send(
            f"🛑 {role_mention} — <@{creator_id}> called a **queue stop**, here <t:{arrive_ts}:R>!",
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
    except discord.HTTPException as e:
        print(f"Failed to send queue-stop ping: {e}")


# ---------------------------------------------------------------------------
# Main persistent status board view
# ---------------------------------------------------------------------------

class StatusView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Queue Stop", style=discord.ButtonStyle.green, emoji="🛑", custom_id="status:queue_stop")
    async def queue_stop_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = discord.ui.Modal(title="Queue Stop")
        minutes_input = discord.ui.TextInput(
            label="Minutes until you'll be here (1-20)",
            style=discord.TextStyle.short,
            placeholder="Enter minutes (1-20)...",
            required=True, min_length=1, max_length=2,
        )
        modal.add_item(minutes_input)

        async def modal_callback(modal_interaction: discord.Interaction):
            client = interaction.client
            try:
                minutes = int(minutes_input.value)
                if not (1 <= minutes <= 20):
                    return await modal_interaction.response.send_message(
                        "❌ Please enter a number between 1 and 20.", ephemeral=True
                    )
                arrive_time = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
                arrive_ts = int(arrive_time.timestamp())

                creator_id = str(interaction.user.id)
                store.storage.setdefault("user_statuses", {})
                store.storage["user_statuses"][creator_id] = {
                    "type": "queue_stop",
                    "message": f"🛑 <@{creator_id}> will be here <t:{arrive_ts}:R>",
                    "remove_at": arrive_ts + 300,
                }
                store.save_data()

                await update_status_board(client, config.STATUS_CHANNEL_ID.id)
                await log_status_event(client, config.GUILD_ID, config.STATUS_LOG_CHANNEL_ID.id,
                                       "Queue Stop Created", creator_id, f"{minutes} minutes")
                await _send_queue_stop_ping(client, creator_id, arrive_ts)

                await modal_interaction.response.send_message(
                    f"✅ Status updated! You'll be here in {minutes} minutes.", ephemeral=True
                )
            except ValueError:
                await modal_interaction.response.send_message("❌ Please enter a valid number.", ephemeral=True)

        modal.on_submit = modal_callback
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Away", style=discord.ButtonStyle.primary, emoji="🌙", custom_id="status:away")
    async def away_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Opens the time modal immediately with a generic "Away" type.
        # Typed away statuses (sleeping/eating/…) live under More Options.
        await interaction.response.send_modal(AwayModal("general"))

    @discord.ui.button(label="Clear Status", style=discord.ButtonStyle.red, emoji="❌", custom_id="status:clear")
    async def clear_status_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        client = interaction.client
        user_statuses = store.storage.get("user_statuses", {})
        user_id = str(interaction.user.id)
        if user_id in user_statuses:
            status_type = user_statuses[user_id].get("type", "unknown")
            del user_statuses[user_id]
            store.save_data()
            await update_status_board(client, config.STATUS_CHANNEL_ID.id)
            await log_status_event(client, config.GUILD_ID, config.STATUS_LOG_CHANNEL_ID.id,
                                   "Status Manually Cleared", user_id, f"cleared {status_type}")
            await interaction.response.send_message("✅ Your status has been cleared.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ You don't have an active status to clear.", ephemeral=True)

    @discord.ui.button(label="More Options", style=discord.ButtonStyle.gray, emoji="⚙️", custom_id="status:more")
    async def more_options_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = MoreOptionsView(str(interaction.user.id))
        await interaction.response.send_message(view=view, ephemeral=True)
        view.bound_interaction = interaction


# ---------------------------------------------------------------------------
# Away modal
# ---------------------------------------------------------------------------

class AwayModal(discord.ui.Modal):
    def __init__(self, away_type: str):
        emoji, label = AWAY_LOOKUP.get(away_type, ("🌙", "Away"))
        super().__init__(title=label)
        self.away_type = away_type

        if away_type == "gaming":
            # Gaming counts UP from a start time, with an optional game name.
            self.game_input = discord.ui.TextInput(
                label="Game (optional)",
                style=discord.TextStyle.short,
                placeholder="e.g. Elden Ring",
                required=False, max_length=60,
            )
            self.since_input = discord.ui.TextInput(
                label="Playing since (optional)",
                style=discord.TextStyle.short,
                placeholder="leave blank = now · 30m · 2h30m · 14:30",
                required=False, max_length=15,
            )
            self.add_item(self.game_input)
            self.add_item(self.since_input)
        else:
            self.time_input = discord.ui.TextInput(
                label="Return time (optional)",
                style=discord.TextStyle.short,
                placeholder="30m · 2h30m · 1d6h · 14:30 · leave blank = no set return",
                required=False, max_length=15,
            )
            self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        client = interaction.client
        user_id = str(interaction.user.id)

        # --- gaming: count up from a start time ---
        if self.away_type == "gaming":
            try:
                start = parse_play_start(self.since_input.value, get_user_tz(user_id))
            except ValueError as e:
                return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

            start_ts = int(start.timestamp())
            game = self.game_input.value.strip()
            emoji, _ = AWAY_LOOKUP["gaming"]
            core = game if game else "In a game"
            message = f"{emoji} <@{user_id}> · {core} · started <t:{start_ts}:R>"

            store.storage.setdefault("user_statuses", {})
            store.storage["user_statuses"][user_id] = {
                "type": "away",
                "away_type": "gaming",
                "message": message,
                "remove_at": None,        # counts up until cleared
                "started_at": start_ts,
                "game": game,
            }
            store.save_data()

            await update_status_board(client, config.STATUS_CHANNEL_ID.id)
            await log_status_event(client, config.GUILD_ID, config.STATUS_LOG_CHANNEL_ID.id,
                                   "In-Game Status Created", user_id, game or "no game specified")
            confirm = "✅ Marked as in-game" + (f" — {game}" if game else "") + f", since <t:{start_ts}:R>."
            return await interaction.response.send_message(confirm, ephemeral=True)

        # --- everything else: count down to a return time ---
        try:
            return_dt = parse_away_duration(self.time_input.value, get_user_tz(user_id))
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        emoji, label = AWAY_LOOKUP.get(self.away_type, ("🌙", "Away"))

        if return_dt is None:
            remove_at = None
            message = f"{emoji} <@{user_id}> · {label} · no set return"
            confirm = f"✅ {label} status set with no return time."
        else:
            remove_at = int(return_dt.timestamp())
            message = f"{emoji} <@{user_id}> · {label} until <t:{remove_at}:t> (<t:{remove_at}:R>)"
            confirm = f"✅ {label} status set until <t:{remove_at}:t>."

        store.storage.setdefault("user_statuses", {})
        store.storage["user_statuses"][user_id] = {
            "type": "away",
            "away_type": self.away_type,
            "message": message,
            "remove_at": remove_at,
        }
        store.save_data()

        await update_status_board(client, config.STATUS_CHANNEL_ID.id)
        await log_status_event(client, config.GUILD_ID, config.STATUS_LOG_CHANNEL_ID.id,
                               "Away Status Created", user_id,
                               f"{label} ({self.time_input.value or 'indefinite'})")
        await interaction.response.send_message(confirm, ephemeral=True)


# ---------------------------------------------------------------------------
# Vacation modal
# ---------------------------------------------------------------------------

class VacationModal(discord.ui.Modal):
    def __init__(self, owner_view: "MoreOptionsView"):
        super().__init__(title="Schedule a Vacation")
        self.owner_view = owner_view
        self.start_input = discord.ui.TextInput(
            label="Start", placeholder="YYYY-MM-DD or YYYY-MM-DD HH:MM",
            required=True, max_length=16,
        )
        self.end_input = discord.ui.TextInput(
            label="End", placeholder="YYYY-MM-DD or YYYY-MM-DD HH:MM",
            required=True, max_length=16,
        )
        self.destination_input = discord.ui.TextInput(
            label="Where are you going? (optional)",
            placeholder="e.g. Tokyo, Japan",
            required=False, max_length=60,
        )
        self.tz_input = discord.ui.TextInput(
            label="Your timezone while away (info only)",
            placeholder="e.g. JST, PST, UTC+9 — shown so people know your hours",
            default=normalize_tz_offset(get_user_tz_name(owner_view.user_id)) if owner_view else "",
            required=False, max_length=40,
        )
        for item in (self.start_input, self.end_input, self.destination_input, self.tz_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        client = interaction.client
        try:
            start_dt = parse_vacation_datetime(self.start_input.value)
            end_dt = parse_vacation_datetime(self.end_input.value)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        now = datetime.datetime.now()
        if end_dt <= start_dt:
            return await interaction.response.send_message("❌ End must be after start.", ephemeral=True)
        if end_dt <= now:
            return await interaction.response.send_message("❌ That vacation is already over.", ephemeral=True)

        user_id = str(interaction.user.id)
        store.storage.setdefault("vacations", {}).setdefault(user_id, [])
        store.storage["vacations"][user_id].append({
            "id": f"VAC-{store.next_id()}",
            "start_at": int(start_dt.timestamp()),
            "end_at": int(end_dt.timestamp()),
            "destination": self.destination_input.value.strip(),
            "tz_note": self.tz_input.value.strip(),
            "created_at": str(now.timestamp()),
        })
        store.save_data()

        await update_status_board(client, config.STATUS_CHANNEL_ID.id)
        await log_status_event(client, config.GUILD_ID, config.STATUS_LOG_CHANNEL_ID.id,
                               "Vacation Scheduled", user_id,
                               f"{self.start_input.value} → {self.end_input.value}")

        # Refresh the More Options panel so the new vacation appears in the list
        await interaction.response.edit_message(view=MoreOptionsView(user_id, interaction))


# ---------------------------------------------------------------------------
# Timezone modal
# ---------------------------------------------------------------------------

class TimezoneModal(discord.ui.Modal, title="Set Your Timezone"):
    def __init__(self, owner_view: "MoreOptionsView"):
        super().__init__()
        self.owner_view = owner_view
        self.tz_input = discord.ui.TextInput(
            label="IANA timezone name",
            placeholder="e.g. Europe/Berlin, Asia/Tokyo, America/New_York",
            default=get_user_tz_name(owner_view.user_id),
            required=True, max_length=50,
        )
        self.add_item(self.tz_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            name = validate_tz_name(self.tz_input.value)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        uid = str(interaction.user.id)
        store.storage.setdefault("user_prefs", {}).setdefault(uid, {})["timezone"] = name
        store.save_data()
        await interaction.response.edit_message(view=MoreOptionsView(uid, interaction))


# ---------------------------------------------------------------------------
# More Options — Components V2 Container panel
# ---------------------------------------------------------------------------

class AwayTypeRow(discord.ui.ActionRow):
    @discord.ui.select(
        placeholder="Set a typed away status…",
        options=[discord.SelectOption(label=label, value=key, emoji=emoji)
                 for key, emoji, label in AWAY_TYPES],
        min_values=1, max_values=1,
    )
    async def pick_away(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(AwayModal(select.values[0]))


class PrefsRow(discord.ui.ActionRow):
    def __init__(self, view: "MoreOptionsView"):
        super().__init__()
        self.owner = view
        on = view.ping_opt_in
        self.toggle_pings.label = f"Queue Stop Pings: {'ON' if on else 'OFF'}"
        self.toggle_pings.emoji = "🔔" if on else "🔕"
        self.toggle_pings.style = discord.ButtonStyle.green if on else discord.ButtonStyle.gray

    @discord.ui.button(label="Queue Stop Pings: OFF", emoji="🔕", style=discord.ButtonStyle.gray)
    async def toggle_pings(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = self.owner.user_id
        role = get_ping_role(interaction.guild)

        if role is None:
            return await interaction.response.send_message(
                "❌ The queue-stop ping role isn't configured (or I can't see it). Ask an admin to set `QUEUE_STOP_PING_ROLE_ID`.",
                ephemeral=True,
            )

        member = interaction.user  # Member in a guild interaction
        new_state = role not in member.roles
        try:
            if new_state:
                await member.add_roles(role, reason="Opted into queue-stop pings")
            else:
                await member.remove_roles(role, reason="Opted out of queue-stop pings")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I don't have permission to manage that role. It must be **below my top role** and I need **Manage Roles**.",
                ephemeral=True,
            )
        await interaction.response.edit_message(view=MoreOptionsView(uid, interaction, ping_opt_in=new_state))

    @discord.ui.button(label="Schedule Vacation", emoji="🏖️", style=discord.ButtonStyle.blurple)
    async def schedule_vacation(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VacationModal(self.owner))

    @discord.ui.button(label="Set Timezone", emoji="🌍", style=discord.ButtonStyle.gray)
    async def set_timezone(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimezoneModal(self.owner))


class RemoveVacationRow(discord.ui.ActionRow):
    def __init__(self, view: "MoreOptionsView", vacations: list):
        super().__init__()
        self.owner = view
        options = []
        for v in vacations:
            start = datetime.datetime.fromtimestamp(v["start_at"]).strftime("%Y-%m-%d")
            end = datetime.datetime.fromtimestamp(v["end_at"]).strftime("%Y-%m-%d")
            dest = v.get("destination", "").strip()
            label = f"{start} → {end}" + (f" · {dest}" if dest else "")
            tz = normalize_tz_offset(v.get("tz_note", ""))
            options.append(discord.SelectOption(
                label=label[:100],
                description=(f"🌍 {tz}" if tz else None),
                value=v["id"],
            ))
        self.remove_select.options = options
        self.remove_select.placeholder = "Remove a scheduled vacation…"

    @discord.ui.select(min_values=1, max_values=1, options=[discord.SelectOption(label="placeholder", value="x")])
    async def remove_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        uid = self.owner.user_id
        vac_id = select.values[0]
        vac_list = store.storage.get("vacations", {}).get(uid, [])
        store.storage["vacations"][uid] = [v for v in vac_list if v["id"] != vac_id]
        if not store.storage["vacations"][uid]:
            del store.storage["vacations"][uid]
        store.save_data()
        await update_status_board(interaction.client, config.STATUS_CHANNEL_ID.id)
        await log_status_event(interaction.client, config.GUILD_ID, config.STATUS_LOG_CHANNEL_ID.id,
                               "Vacation Cancelled", uid, vac_id)
        await interaction.response.edit_message(view=MoreOptionsView(uid, interaction))


class MoreOptionsView(discord.ui.LayoutView):
    def __init__(self, user_id: str, bound_interaction: discord.Interaction | None = None,
                 ping_opt_in: bool | None = None):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.bound_interaction = bound_interaction

        # Resolve current opt-in state (does the member have the ping role?)
        if ping_opt_in is None:
            ping_opt_in = False
            if bound_interaction is not None:
                role = get_ping_role(bound_interaction.guild)
                member = bound_interaction.user
                if role is not None and isinstance(member, discord.Member):
                    ping_opt_in = role in member.roles
        self.ping_opt_in = ping_opt_in

        vacations = store.storage.get("vacations", {}).get(user_id, [])

        # Build the body text describing current vacations
        if vacations:
            lines = ["**Your scheduled vacations:**"]
            for v in sorted(vacations, key=lambda x: x["start_at"]):
                tz = normalize_tz_offset(v.get("tz_note", ""))
                tz_part = f" · 🌍 {tz}" if tz else ""
                dest = v.get("destination", "").strip()
                dest_part = f"📍 {dest} · " if dest else ""
                lines.append(f"• {dest_part}<t:{int(v['start_at'])}:d> → <t:{int(v['end_at'])}:d>{tz_part}")
            vac_text = "\n".join(lines)
        else:
            vac_text = "*You have no scheduled vacations.*"

        container = discord.ui.Container(accent_colour=discord.Color.blurple())
        container.add_item(discord.ui.TextDisplay(
            "## ⚙️ More Options\n"
            "Set a typed away status, manage queue-stop pings, or schedule vacations.\n"
            f"Your timezone: **{get_user_tz_name(user_id)}** "
            "(used to interpret clock times like `14:30`)."
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(AwayTypeRow())
        container.add_item(PrefsRow(self))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(vac_text))
        if vacations:
            container.add_item(RemoveVacationRow(self, vacations))
        self.add_item(container)

    async def on_timeout(self):
        # Destroy the ephemeral panel once its buttons stop working.
        if self.bound_interaction is not None:
            try:
                await self.bound_interaction.delete_original_response()
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class StatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot, guild_id: int, status_channel_id: int, status_log_channel_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.status_channel_id = status_channel_id
        self.status_log_channel_id = status_log_channel_id

    @tasks.loop(minutes=1)
    async def cleanup_expired_statuses(self):
        now = datetime.datetime.now().timestamp()
        changed = False

        # Expire timed away / queue-stop statuses (indefinite ones have remove_at=None)
        user_statuses = store.storage.get("user_statuses", {})
        to_remove = [(uid, info) for uid, info in user_statuses.items()
                     if info.get("remove_at") and now >= info["remove_at"]]
        for user_id, info in to_remove:
            del user_statuses[user_id]
            await log_status_event(self.bot, self.guild_id, self.status_log_channel_id,
                                   "Status Expired", user_id, f"{info.get('type', 'unknown')} expired")
            changed = True

        # Remove finished vacations (per-user lists)
        vacations = store.storage.get("vacations", {})
        for uid in list(vacations.keys()):
            kept = [v for v in vacations[uid] if now < v["end_at"]]
            if len(kept) != len(vacations[uid]):
                await log_status_event(self.bot, self.guild_id, self.status_log_channel_id,
                                       "Vacation Ended", uid, "vacation period over")
                changed = True
            if kept:
                vacations[uid] = kept
            else:
                del vacations[uid]

        if changed:
            store.save_data()
            await update_status_board(self.bot, self.status_channel_id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        user_id = str(member.id)
        user_statuses = store.storage.get("user_statuses", {})
        # Don't touch anyone's status while they're on vacation
        if has_active_vacation(user_id):
            return
        if user_id not in user_statuses or user_statuses[user_id].get("type") != "away":
            return
        # In-game status counts up and shouldn't be cleared just for joining voice
        if user_statuses[user_id].get("away_type") == "gaming":
            return

        cleared = False
        reason = ""
        if before.channel is None and after.channel is not None:
            reason = f"joined voice channel {after.channel.name}"
            cleared = True
        elif before.self_mute and not after.self_mute:
            reason = "self-unmuted"
            cleared = True

        if cleared:
            del user_statuses[user_id]
            store.save_data()
            await update_status_board(self.bot, self.status_channel_id)
            await log_status_event(self.bot, self.guild_id, self.status_log_channel_id,
                                   "Status Auto-Cleared", user_id, reason)


async def setup(bot: commands.Bot, guild_id: int, status_channel_id: int, status_log_channel_id: int):
    cog = StatusCog(bot, guild_id, status_channel_id, status_log_channel_id)
    await bot.add_cog(cog)
    return cog
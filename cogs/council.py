import datetime

import discord
from discord.ext import commands, tasks
from discord import app_commands

import storage as store
import config
from cogs import vote_logic as vl
from cogs import xp_api


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROMO_VOTE_PERIOD   = 72 * 3600
PROPOSAL_VOTE_PERIOD = 120 * 3600
COMMENT_PERIOD      = 24 * 3600
PROPOSAL_VETO_PERIOD = 72 * 3600
REATTEMPT_BLOCK     = 14 * 24 * 3600

PROMO_SPECS = {
    "member": {"role_attr": "MEMBER_ROLE_ID", "from_attr": "GUEST_ROLE_ID",
               "level_attr": "MEMBER_GLOBAL_LEVEL", "label": "Member"},
    "vip":    {"role_attr": "VIP_ROLE_ID", "from_attr": "MEMBER_ROLE_ID",
               "level_attr": "VIP_GLOBAL_LEVEL", "label": "VIP"},
}


def is_council(member: discord.Member) -> bool:
    return any(r.id in (config.COUNCIL_ROLE_ID, config.OWNER_ROLE_ID) for r in member.roles)


def is_owner_member(member: discord.Member) -> bool:
    return any(r.id == config.OWNER_ROLE_ID for r in member.roles)


def _member_is_bot(guild: discord.Guild, user_id) -> bool:
    m = guild.get_member(int(user_id))
    return bool(m and m.bot)


# ---- Alt-account linking --------------------------------------------------
# storage["alt_links"][alt_id] = primary_id  (an alt maps to its primary)

def primary_of(user_id: str) -> str:
    """Resolve a user to their primary account (itself if not an alt)."""
    return store.storage.get("alt_links", {}).get(str(user_id), str(user_id))


def link_alt(alt_id: str, primary_id: str):
    store.storage.setdefault("alt_links", {})[str(alt_id)] = str(primary_id)
    store.save_data()


def unlink_alt(alt_id: str) -> bool:
    links = store.storage.setdefault("alt_links", {})
    if str(alt_id) in links:
        del links[str(alt_id)]
        store.save_data()
        return True
    return False


async def count_eligible(guild: discord.Guild) -> int:
    """Number of people eligible to vote = council role holders ∪ owner role holders.

    Relies on the member cache, which requires the Server Members intent. If the
    guild hasn't been chunked yet, fetch it so role.members is complete.
    """
    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception as e:
            print(f"Failed to chunk guild for eligible count: {e}")
    return _count_eligible_cached(guild)


def _count_eligible_cached(guild: discord.Guild) -> int:
    """Synchronous count from the current cache (no chunking).

    Alts are collapsed to their primary so a person with multiple ranked
    accounts is only counted once.
    """
    council = guild.get_role(config.COUNCIL_ROLE_ID)
    owner = guild.get_role(config.OWNER_ROLE_ID)
    voters = set()
    if council:
        voters.update(primary_of(m.id) for m in council.members)
    if owner:
        voters.update(primary_of(m.id) for m in owner.members)
    return len(voters)


def apply_nick_prefix(current_nick: str, new_label: str) -> str:
    """Set the role prefix. Format is always '[text] name'.

    - If there's an existing '[...]' prefix, replace it.
    - If there's a *custom* prefix the user set… we can't distinguish that from a
      role prefix reliably, so per spec: only replace when a bracket prefix exists,
      otherwise prepend. (Custom prefixes are left alone — see note in council cog.)
    """
    name = current_nick or ""
    if name.startswith("[") and "] " in name:
        # Strip existing bracket prefix
        name = name.split("] ", 1)[1]
    if not new_label:
        return name.strip()
    return f"[{new_label}] {name}".strip()


async def council_log(client: commands.Bot, message: str = None, embed: discord.Embed = None):
    channel = client.get_channel(config.COUNCIL_LOG_CHANNEL_ID)
    if not channel:
        return
    try:
        await channel.send(content=message, embed=embed)
    except discord.HTTPException as e:
        print(f"Failed to write council log: {e}")


# ---------------------------------------------------------------------------
# Vote storage model
# ---------------------------------------------------------------------------
# storage["votes"][vote_id] = {
#   id, kind ("member"|"vip"|"proposal"), title, description,
#   initiator_id, target_id (promos only),
#   thread_id, message_id, owner_msg_id (proposals after pass),
#   status: comment|voting|veto|passed|failed|blocked|expired|vetoed|applied|quashed
#   mode, visibility ("counts"|"hidden"|"full"),
#   created_at, comment_ends_at, voting_ends_at, veto_ends_at,
#   votes: {user_id: "yes"|"no"|"abstain"},
# }
# storage["vote_blocks"][f"{kind}:{user_id}"] = unblock_at_ts
# storage["vote_counter"] = int  (CV running number for proposals)


def _new_vote_id() -> str:
    return f"VOTE-{store.next_id()}"


def _next_cv_number() -> int:
    store.storage.setdefault("vote_counter", 0)
    store.storage["vote_counter"] += 1
    return store.storage["vote_counter"]


def _block_key(kind: str, user_id: str) -> str:
    return f"{kind}:{user_id}"


def is_blocked(kind: str, user_id: str) -> float | None:
    """Return unblock timestamp if currently blocked, else None."""
    blocks = store.storage.get("vote_blocks", {})
    ts = blocks.get(_block_key(kind, user_id))
    if ts and ts > datetime.datetime.now().timestamp():
        return ts
    return None


def tally(vote: dict) -> tuple[int, int, int]:
    # Recused users are out of the denominator, so their (preserved) vote must
    # not count toward the tally either — exclude them here.
    recused = set(vote.get("recused", []))
    counted = {uid: c for uid, c in vote["votes"].items() if uid not in recused}
    yes = sum(1 for v in counted.values() if v == "yes")
    no = sum(1 for v in counted.values() if v == "no")
    abstain = sum(1 for v in counted.values() if v == "abstain")
    return yes, no, abstain


def counted_votes(vote: dict) -> int:
    """Number of votes that count (excludes recused users' preserved votes)."""
    recused = set(vote.get("recused", []))
    return sum(1 for uid in vote["votes"] if uid not in recused)


def effective_eligible(vote: dict, guild: discord.Guild = None) -> int:
    """Eligible count minus recused voters (recusal shrinks the denominator).

    Uses the snapshot taken at voting open when present; otherwise the live
    cached count. Recusal is not permitted under true_unanimous, so the
    recused list is ignored in that mode.
    """
    base = vote.get("eligible_snapshot")
    if base is None:
        base = _count_eligible_cached(guild) if guild else 0
    if vote.get("mode") == "true_unanimous":
        return base
    recused = len(vote.get("recused", []))
    return max(0, base - recused)


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------

def build_vote_embed(guild: discord.Guild, vote: dict) -> discord.Embed:
    kind = vote["kind"]
    status = vote["status"]

    color = {
        "comment": discord.Color.light_grey(),
        "voting": discord.Color.blurple(),
        "veto": discord.Color.orange(),
        "passed": discord.Color.green(),
        "applied": discord.Color.green(),
        "failed": discord.Color.red(),
        "blocked": discord.Color.dark_red(),
        "expired": discord.Color.dark_grey(),
        "vetoed": discord.Color.red(),
        "quashed": discord.Color.dark_red(),
    }.get(status, discord.Color.greyple())

    embed = discord.Embed(title=vote["title"], description=vote.get("description") or "", color=color)

    if vote.get("target_id"):
        embed.add_field(name="Candidate", value=f"<@{vote['target_id']}>", inline=True)
    embed.add_field(name="Initiator", value=f"<@{vote['initiator_id']}>", inline=True)
    embed.add_field(name="Threshold", value=vl.THRESHOLD_MODES[vote["mode"]]["label"], inline=True)

    # Status / timing line — build only the relevant one (timestamps may be None
    # for stages not yet reached, so we can't format them all eagerly).
    def _ts(key):
        return int(vote[key]) if vote.get(key) else 0

    if status == "comment":
        status_line = f"💬 Comment period — voting opens <t:{_ts('comment_ends_at')}:R>"
    elif status == "voting":
        status_line = f"🗳️ Voting open — closes <t:{_ts('voting_ends_at')}:R>"
    elif status == "veto":
        status_line = f"⚖️ Passed — owner veto window closes <t:{_ts('veto_ends_at')}:R>"
    else:
        status_line = {
            "passed": "✅ Passed",
            "applied": "✅ Passed & applied",
            "failed": "❌ Did not pass",
            "blocked": "🚫 Defeated (re-attempt blocked)",
            "expired": "⌛ Expired (insufficient support)",
            "vetoed": "🛑 Vetoed by an owner",
            "quashed": "🛑 Quashed by an owner",
        }.get(status, status)
    embed.add_field(name="Status", value=status_line, inline=False)

    # Vote counts according to visibility
    if status in ("voting", "veto") or status in ("passed", "applied", "failed", "blocked", "expired", "vetoed", "quashed"):
        yes, no, abstain = tally(vote)
        eligible = effective_eligible(vote, guild)
        recused = len(vote.get("recused", []))
        vis = vote.get("visibility", "counts")
        if vis == "hidden" and status == "voting":
            voted = counted_votes(vote)
            extra = f" · {recused} recused" if recused else ""
            embed.add_field(name="Participation", value=f"{voted}/{eligible} have voted{extra}", inline=False)
        else:
            need = vl.required_yes(vote["mode"], eligible)
            recused_line = f" · Recused: {recused}" if recused else ""
            embed.add_field(
                name="Tally",
                value=f"✅ Approve: **{yes}**  ·  ❌ Oppose: **{no}**  ·  ➖ Abstain: **{abstain}**\n\n"
                      f"Eligible: {eligible} · Needed to pass: {need}{recused_line}",
                inline=False,
            )
            if vis == "full":
                lines = []
                for uid, choice in vote["votes"].items():
                    icon = {"yes": "✅", "no": "❌", "abstain": "➖"}[choice]
                    lines.append(f"{icon} <@{uid}>")
                for uid in vote.get("recused", []):
                    lines.append(f"↩️ <@{uid}> (recused)")
                if lines:
                    embed.add_field(name="Voters", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Vote ID: {vote['id']}")
    return embed


# ---------------------------------------------------------------------------
# Persistent views (custom_id carries the vote id so they survive restarts)
# ---------------------------------------------------------------------------

class CommentView(discord.ui.View):
    """Shown during the comment period: lets initiator/admin open voting early."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start Voting", style=discord.ButtonStyle.blurple, emoji="🗳️", custom_id="cv:start")
    async def start_voting(self, interaction: discord.Interaction, button: discord.ui.Button):
        vote = _find_vote_by_thread(interaction.channel_id)
        if not vote:
            return await interaction.response.send_message("❌ No vote bound to this thread.", ephemeral=True)
        if vote["status"] != "comment":
            return await interaction.response.send_message("❌ Voting has already started or ended.", ephemeral=True)
        if interaction.user.id != int(vote["initiator_id"]) and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Only the initiator or an admin can start voting.", ephemeral=True)
        await _open_voting(interaction.client, vote)
        await interaction.response.send_message("✅ Voting is now open.", ephemeral=True)

    @discord.ui.button(label="Settings", style=discord.ButtonStyle.gray, emoji="⚙️", custom_id="cv:settings")
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        vote = _find_vote_by_thread(interaction.channel_id)
        if not vote:
            return await interaction.response.send_message("❌ No vote bound to this thread.", ephemeral=True)
        if vote["status"] != "comment":
            return await interaction.response.send_message(
                "❌ Settings can only be changed before voting starts.", ephemeral=True)
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Only admins can change vote settings.", ephemeral=True)
        await interaction.response.send_message(
            content=_settings_summary(vote), view=VoteSettingsView(vote["id"]), ephemeral=True)


class VoteView(discord.ui.View):
    """Approve / Oppose / Abstain / Recuse buttons during the voting period."""
    def __init__(self):
        super().__init__(timeout=None)

    async def _cast(self, interaction: discord.Interaction, choice: str):
        vote = _find_vote_by_thread(interaction.channel_id)
        if not vote:
            return await interaction.response.send_message("❌ No vote bound to this thread.", ephemeral=True)
        if vote["status"] != "voting":
            return await interaction.response.send_message("❌ Voting is not currently open.", ephemeral=True)
        if not is_council(interaction.user):
            return await interaction.response.send_message("❌ Only council/owners may vote.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        # Collapse alt accounts to the primary so one person = one vote.
        voter = primary_of(interaction.user.id)
        vote["votes"][voter] = choice
        # Casting a real vote un-recuses you.
        if voter in vote.get("recused", []):
            vote["recused"].remove(voter)
        store.save_data()
        await _refresh_vote_message(interaction.client, vote)

        # Early close if every (non-recused) eligible voter has voted.
        eligible = effective_eligible(vote, interaction.guild)
        if counted_votes(vote) >= eligible:
            await _close_voting(interaction.client, vote)

        await interaction.followup.send(f"Recorded your vote: **{choice}**", ephemeral=True)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅", custom_id="cv:yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cast(interaction, "yes")

    @discord.ui.button(label="Oppose", style=discord.ButtonStyle.red, emoji="✖️", custom_id="cv:no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cast(interaction, "no")

    @discord.ui.button(label="Abstain", style=discord.ButtonStyle.gray, emoji="➖", custom_id="cv:abstain")
    async def abstain(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cast(interaction, "abstain")

    @discord.ui.button(label="Recuse", style=discord.ButtonStyle.gray, emoji="↩️", custom_id="cv:recuse")
    async def recuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        vote = _find_vote_by_thread(interaction.channel_id)
        if not vote:
            return await interaction.response.send_message("❌ No vote bound to this thread.", ephemeral=True)
        if vote["status"] != "voting":
            return await interaction.response.send_message("❌ Voting is not currently open.", ephemeral=True)
        if not is_council(interaction.user):
            return await interaction.response.send_message("❌ Only council/owners may recuse.", ephemeral=True)
        if vote["mode"] == "true_unanimous":
            return await interaction.response.send_message(
                "❌ Recusal isn't allowed under a True Unanimous vote.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        voter = primary_of(interaction.user.id)
        recused = vote.setdefault("recused", [])
        if voter in recused:
            # Toggle off — un-recuse.
            recused.remove(voter)
            msg = "↩️ You are no longer recused."
        else:
            recused.append(voter)
            # Recusing removes any cast vote.
            vote["votes"].pop(voter, None)
            msg = "↩️ You have recused yourself from this vote."
        store.save_data()
        await _refresh_vote_message(interaction.client, vote)

        # Recusing can lower the threshold enough that the vote should resolve.
        eligible = effective_eligible(vote, interaction.guild)
        if eligible > 0 and counted_votes(vote) >= eligible:
            await _close_voting(interaction.client, vote)

        await interaction.followup.send(msg, ephemeral=True)


class VetoView(discord.ui.View):
    """Veto / won't-veto buttons posted in the owner channel for proposals."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Veto", style=discord.ButtonStyle.red, emoji="🛑", custom_id="cv:veto")
    async def veto(self, interaction: discord.Interaction, button: discord.ui.Button):
        vote = _find_vote_by_owner_msg(interaction.message.id)
        if not vote:
            return await interaction.response.send_message("❌ No vote bound to this message.", ephemeral=True)
        if vote["status"] != "veto":
            return await interaction.response.send_message("❌ This vote is not in the veto window.", ephemeral=True)
        if not is_owner_member(interaction.user):
            return await interaction.response.send_message("❌ Only owners may veto.", ephemeral=True)
        # Acknowledge first — _apply_veto does several network calls that would
        # otherwise blow past the 3s interaction window (error 10062).
        await interaction.response.defer(ephemeral=True)
        await _apply_veto(interaction.client, vote, interaction.user.id)
        await interaction.followup.send("🛑 Proposal vetoed.", ephemeral=True)

    @discord.ui.button(label="Won't Veto", style=discord.ButtonStyle.green, emoji="👍", custom_id="cv:noveto")
    async def no_veto(self, interaction: discord.Interaction, button: discord.ui.Button):
        vote = _find_vote_by_owner_msg(interaction.message.id)
        if not vote:
            return await interaction.response.send_message("❌ No vote bound to this message.", ephemeral=True)
        if vote["status"] != "veto":
            return await interaction.response.send_message("❌ This vote is not in the veto window.", ephemeral=True)
        if not is_owner_member(interaction.user):
            return await interaction.response.send_message("❌ Only owners may respond.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        await _finalize_proposal(interaction.client, vote)

        # Remove the buttons from the owner-channel message
        owner_channel = interaction.client.get_channel(config.OWNER_CHANNEL_ID)
        if owner_channel and vote.get("owner_msg_id"):
            try:
                msg = await owner_channel.fetch_message(int(vote["owner_msg_id"]))
                await msg.edit(view=None)
            except discord.HTTPException:
                pass

        await council_log(interaction.client,
                          f"👍 <@{interaction.user.id}> declined to veto **{vote['title']}** "
                          f"(`{vote['id']}`) — enacted early.")
        await interaction.followup.send("👍 Recorded — proposal enacted.", ephemeral=True)


class QuashView(discord.ui.View):
    """Quash button on applied promotions (owners only, any time)."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Quash Promotion", style=discord.ButtonStyle.red, emoji="🛑", custom_id="cv:quash")
    async def quash(self, interaction: discord.Interaction, button: discord.ui.Button):
        vote = _find_vote_by_thread(interaction.channel_id)
        if not vote:
            return await interaction.response.send_message("❌ No vote bound to this thread.", ephemeral=True)
        if vote["status"] != "applied":
            return await interaction.response.send_message("❌ Only an applied promotion can be quashed.", ephemeral=True)
        if not is_owner_member(interaction.user):
            return await interaction.response.send_message("❌ Only owners may quash.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await _quash_promotion(interaction.client, interaction.guild, vote, interaction.user.id)
        await interaction.followup.send("🛑 Promotion quashed and reverted.", ephemeral=True)


# ---------------------------------------------------------------------------
# Vote settings panel (admin-only, comment period only)
# ---------------------------------------------------------------------------

VISIBILITY_OPTIONS = {
    "counts": "Show vote counts (not who voted)",
    "hidden": "Hide counts — show only participation",
    "full": "Show counts and who voted",
}


def _fmt_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    if h >= 24 and h % 24 == 0:
        return f"{h // 24}d"
    if h >= 24:
        return f"{h // 24}d{h % 24}h"
    return f"{h}h"


def _settings_summary(vote: dict) -> str:
    remaining = max(0, int(vote["comment_ends_at"] - datetime.datetime.now().timestamp()))
    return (
        f"## ⚙️ Vote Settings — `{vote['id']}`\n"
        f"Adjustable until voting starts.\n\n"
        f"**Threshold:** {vl.THRESHOLD_MODES[vote['mode']]['label']}\n"
        f"**Visibility:** {VISIBILITY_OPTIONS[vote.get('visibility', 'counts')]}\n"
        f"**Voting period:** {_fmt_duration(vote['voting_period'])}\n"
        f"**Comment period ends:** <t:{int(vote['comment_ends_at'])}:R>"
    )


class VoteSettingsView(discord.ui.View):
    def __init__(self, vote_id: str):
        super().__init__(timeout=300)
        self.vote_id = vote_id

    def _vote(self) -> dict | None:
        return store.storage.get("votes", {}).get(self.vote_id)

    async def _guard(self, interaction: discord.Interaction) -> dict | None:
        vote = self._vote()
        if not vote:
            await interaction.response.send_message("❌ Vote no longer exists.", ephemeral=True)
            return None
        if vote["status"] != "comment":
            await interaction.response.send_message(
                "❌ Voting already started — settings are locked.", ephemeral=True)
            return None
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return None
        return vote

    @discord.ui.select(
        placeholder="Threshold mode…",
        options=[discord.SelectOption(label=cfg["label"], value=key)
                 for key, cfg in vl.THRESHOLD_MODES.items()],
    )
    async def mode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        vote = await self._guard(interaction)
        if not vote:
            return
        vote["mode"] = select.values[0]
        store.save_data()
        await _refresh_vote_message(interaction.client, vote)
        await interaction.response.edit_message(content=_settings_summary(vote), view=self)

    @discord.ui.select(
        placeholder="Vote visibility…",
        options=[discord.SelectOption(label=label, value=key)
                 for key, label in VISIBILITY_OPTIONS.items()],
    )
    async def visibility_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        vote = await self._guard(interaction)
        if not vote:
            return
        vote["visibility"] = select.values[0]
        store.save_data()
        await _refresh_vote_message(interaction.client, vote)
        await interaction.response.edit_message(content=_settings_summary(vote), view=self)

    @discord.ui.button(label="Set Durations", style=discord.ButtonStyle.blurple, emoji="⏱️", row=2)
    async def set_durations(self, interaction: discord.Interaction, button: discord.ui.Button):
        vote = await self._guard(interaction)
        if not vote:
            return
        await interaction.response.send_modal(DurationModal(self.vote_id))


class DurationModal(discord.ui.Modal, title="Vote Durations"):
    def __init__(self, vote_id: str):
        super().__init__()
        self.vote_id = vote_id
        vote = store.storage.get("votes", {}).get(vote_id, {})
        comment_left = max(0, int(vote.get("comment_ends_at", 0) - datetime.datetime.now().timestamp()))
        self.comment_input = discord.ui.TextInput(
            label="Comment period remaining (e.g. 24h, 2d)",
            default=_fmt_duration(comment_left), required=True, max_length=10,
        )
        self.voting_input = discord.ui.TextInput(
            label="Voting period (e.g. 72h, 5d)",
            default=_fmt_duration(vote.get("voting_period", 0)), required=True, max_length=10,
        )
        self.add_item(self.comment_input)
        self.add_item(self.voting_input)

    async def on_submit(self, interaction: discord.Interaction):
        vote = store.storage.get("votes", {}).get(self.vote_id)
        if not vote or vote["status"] != "comment":
            return await interaction.response.send_message("❌ Settings are locked.", ephemeral=True)
        try:
            comment_secs = _parse_duration(self.comment_input.value)
            voting_secs = _parse_duration(self.voting_input.value)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        vote["comment_ends_at"] = datetime.datetime.now().timestamp() + comment_secs
        vote["voting_period"] = voting_secs
        store.save_data()
        await _refresh_vote_message(interaction.client, vote)
        await interaction.response.send_message(
            f"✅ Updated — comment ends <t:{int(vote['comment_ends_at'])}:R>, "
            f"voting period {_fmt_duration(voting_secs)}.", ephemeral=True)


_DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60}


def _parse_duration(text: str) -> int:
    """Parse '24h', '2d', '1d6h' → seconds. Raises ValueError."""
    import re
    text = text.strip().lower()
    matches = re.findall(r"(\d+)([dhm])", text)
    consumed = "".join(f"{n}{u}" for n, u in matches)
    if not matches or consumed != re.sub(r"\s+", "", text):
        raise ValueError("Invalid duration. Use forms like 24h, 2d, 1d6h.")
    total = sum(int(n) * _DURATION_UNITS[u] for n, u in matches)
    if total <= 0:
        raise ValueError("Duration must be greater than zero.")
    return total


# ---------------------------------------------------------------------------
# Vote lookups
# ---------------------------------------------------------------------------

def _find_vote_by_thread(thread_id: int) -> dict | None:
    for v in store.storage.get("votes", {}).values():
        if v.get("thread_id") == str(thread_id):
            return v
    return None


def _find_vote_by_owner_msg(message_id: int) -> dict | None:
    for v in store.storage.get("votes", {}).values():
        if v.get("owner_msg_id") == str(message_id):
            return v
    return None


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

async def _refresh_vote_message(client: commands.Bot, vote: dict):
    thread = client.get_channel(int(vote["thread_id"]))
    if not thread:
        return
    try:
        msg = await thread.fetch_message(int(vote["message_id"]))
        view = VoteView() if vote["status"] == "voting" else (
            CommentView() if vote["status"] == "comment" else None)
        await msg.edit(embed=build_vote_embed(thread.guild, vote), view=view)
    except discord.HTTPException:
        pass


async def _open_voting(client: commands.Bot, vote: dict):
    guild = client.get_guild(config.GUILD_ID)
    vote["status"] = "voting"
    vote["voting_ends_at"] = datetime.datetime.now().timestamp() + vote["voting_period"]
    vote["eligible_snapshot"] = await count_eligible(guild)
    store.save_data()
    await _refresh_vote_message(client, vote)
    await council_log(client, f"🗳️ Voting opened for **{vote['title']}** (`{vote['id']}`).")


async def _close_voting(client: commands.Bot, vote: dict):
    guild = client.get_guild(config.GUILD_ID)
    eligible = effective_eligible(vote, guild)
    yes, no, abstain = tally(vote)
    outcome = vl.resolve(vote["mode"], eligible, yes, no, abstain)

    if outcome == "passed":
        if vote["kind"] == "proposal":
            await _enter_veto_window(client, vote)
        elif vote["kind"] == "ban":
            await _enter_ban_veto_window(client, vote)
        else:
            await _apply_promotion(client, guild, vote)
        return

    # Not passed
    if outcome == "blocked":
        vote["status"] = "blocked"
        if vote["kind"] in PROMO_SPECS and vote.get("target_id"):
            store.storage.setdefault("vote_blocks", {})[_block_key(vote["kind"], vote["target_id"])] = \
                datetime.datetime.now().timestamp() + REATTEMPT_BLOCK
    else:
        vote["status"] = "expired"
    store.save_data()
    await _refresh_vote_message(client, vote)
    await _archive_thread(client, vote)
    await council_log(client, f"{'🚫 Blocked' if outcome=='blocked' else '⌛ Expired'}: **{vote['title']}** "
                              f"(`{vote['id']}`) — yes {yes}/no {no}/abstain {abstain}.")


async def _enter_veto_window(client: commands.Bot, vote: dict):
    vote["status"] = "veto"
    vote["veto_ends_at"] = datetime.datetime.now().timestamp() + PROPOSAL_VETO_PERIOD
    store.save_data()
    await _refresh_vote_message(client, vote)

    owner_channel = client.get_channel(config.OWNER_CHANNEL_ID)
    if owner_channel:
        embed = build_vote_embed(client.get_guild(config.GUILD_ID), vote)
        embed.title = f"[Veto window] {vote['title']}"
        try:
            msg = await owner_channel.send(embed=embed, view=VetoView())
            vote["owner_msg_id"] = str(msg.id)
            store.save_data()
        except discord.HTTPException as e:
            print(f"Failed to post veto message: {e}")
    await council_log(client, f"⚖️ **{vote['title']}** (`{vote['id']}`) passed — entering 72h owner veto window.")


async def _finalize_proposal(client: commands.Bot, vote: dict):
    """Veto window elapsed with no veto → enacted."""
    vote["status"] = "passed"
    store.save_data()
    await _refresh_vote_message(client, vote)
    await _archive_thread(client, vote)
    await council_log(client, f"✅ Proposal enacted: **{vote['title']}** (`{vote['id']}`).")


async def _apply_veto(client: commands.Bot, vote: dict, owner_id: int):
    vote["status"] = "vetoed"
    vote["vetoed_by"] = str(owner_id)
    store.save_data()
    await _refresh_vote_message(client, vote)
    await _archive_thread(client, vote)
    # Disable the veto button
    owner_channel = client.get_channel(config.OWNER_CHANNEL_ID)
    if owner_channel and vote.get("owner_msg_id"):
        try:
            msg = await owner_channel.fetch_message(int(vote["owner_msg_id"]))
            await msg.edit(view=None)
        except discord.HTTPException:
            pass
    await council_log(client, f"🛑 Proposal **{vote['title']}** (`{vote['id']}`) vetoed by <@{owner_id}>.")


async def _clear_owner_buttons(client: commands.Bot, vote: dict):
    owner_channel = client.get_channel(config.OWNER_CHANNEL_ID)
    if owner_channel and vote.get("owner_msg_id"):
        try:
            msg = await owner_channel.fetch_message(int(vote["owner_msg_id"]))
            await msg.edit(view=None)
        except discord.HTTPException:
            pass


async def _enter_ban_veto_window(client: commands.Bot, vote: dict):
    """A ban vote passed — the owner decides: blacklist (global), local ban, or veto."""
    vote["status"] = "veto"
    vote["veto_ends_at"] = datetime.datetime.now().timestamp() + PROPOSAL_VETO_PERIOD
    store.save_data()
    await _refresh_vote_message(client, vote)

    owner_channel = client.get_channel(config.OWNER_CHANNEL_ID)
    if owner_channel:
        embed = build_vote_embed(client.get_guild(config.GUILD_ID), vote)
        embed.title = f"[Ban — owner decision] {vote['title']}"
        try:
            msg = await owner_channel.send(embed=embed, view=BanVetoView())
            vote["owner_msg_id"] = str(msg.id)
            store.save_data()
        except discord.HTTPException as e:
            print(f"Failed to post ban veto message: {e}")
    await council_log(client, f"⚖️ Ban vote **{vote['title']}** (`{vote['id']}`) passed — "
                              f"owner decision required (blacklist / local / veto).")


async def _apply_ban_vote(client: commands.Bot, vote: dict, scope: str, owner_id: int = None):
    """Enact a passed ban vote at the chosen scope (timeout default = local)."""
    from cogs import moderation, mod_db  # lazy: avoids a circular import

    guild = client.get_guild(config.GUILD_ID)
    term_days = vote.get("term_days")          # None = permanent (owner-approval only)
    owner_approved = owner_id is not None
    # Permanent requires explicit owner approval; on the no-action timeout it falls back
    # to a long finite local term so the privilege is never granted automatically.
    if term_days is None and not owner_approved:
        term_days = 1825
    expires_at = None if term_days is None else datetime.datetime.now().timestamp() + term_days * 86400
    target_id = vote["target_id"]
    reason = vote.get("reason") or "Council ban vote"
    rules = vote.get("violated_rules") or ""

    vote["status"] = "applied"
    vote["ban_scope"] = scope
    store.save_data()
    await _refresh_vote_message(client, vote)
    await _clear_owner_buttons(client, vote)
    await _archive_thread(client, vote)

    if not mod_db.is_ready():
        await council_log(client, f"⚠️ Ban `{vote['id']}` approved ({scope}) but the moderation "
                                  f"database is unavailable — not applied.")
        return

    await mod_db.add_ban(target_id, reason, rules, owner_id or vote["initiator_id"],
                         expires_at, scope, config.GUILD_ID)
    await moderation.sync_and_enforce(client)
    await moderation.send_ban_notice(client, target_id, reason, rules, expires_at, scope,
                                     guild.name if guild else "the server", approver="council")
    label = "blacklisted across the network" if scope == "global" else "banned locally"
    term_txt = "permanently" if expires_at is None else f"for {int(term_days)}d"
    by = f" (approved by <@{owner_id}>)" if owner_id else " (default after veto window)"
    await council_log(client, f"⛔ <@{target_id}> {label} {term_txt} via ban vote "
                              f"`{vote['id']}`{by}.")


async def _veto_ban_vote(client: commands.Bot, vote: dict, owner_id: int):
    vote["status"] = "vetoed"
    vote["vetoed_by"] = str(owner_id)
    store.save_data()
    await _refresh_vote_message(client, vote)
    await _clear_owner_buttons(client, vote)
    await _archive_thread(client, vote)
    await council_log(client, f"🛑 Ban vote **{vote['title']}** (`{vote['id']}`) vetoed by <@{owner_id}>.")


class BanVetoView(discord.ui.View):
    """Owner decision for a passed ban vote: blacklist (global), local ban, or veto."""
    def __init__(self):
        super().__init__(timeout=None)

    async def _guard(self, interaction: discord.Interaction) -> dict | None:
        vote = _find_vote_by_owner_msg(interaction.message.id)
        if not vote or vote.get("kind") != "ban":
            await interaction.response.send_message("❌ No ban vote bound to this message.", ephemeral=True)
            return None
        if vote["status"] != "veto":
            await interaction.response.send_message("❌ This vote isn't awaiting an owner decision.", ephemeral=True)
            return None
        if not is_owner_member(interaction.user):
            await interaction.response.send_message("❌ Only owners may decide.", ephemeral=True)
            return None
        return vote

    @discord.ui.button(label="Approve + Blacklist", style=discord.ButtonStyle.danger, emoji="⛔",
                       custom_id="cv:ban:blacklist")
    async def blacklist(self, interaction: discord.Interaction, button: discord.ui.Button):
        vote = await self._guard(interaction)
        if not vote:
            return
        await interaction.response.defer(ephemeral=True)
        await _apply_ban_vote(interaction.client, vote, scope="global", owner_id=interaction.user.id)
        await interaction.followup.send("⛔ Blacklisted across the network.", ephemeral=True)

    @discord.ui.button(label="Approve (local)", style=discord.ButtonStyle.secondary, emoji="🔨",
                       custom_id="cv:ban:local")
    async def local(self, interaction: discord.Interaction, button: discord.ui.Button):
        vote = await self._guard(interaction)
        if not vote:
            return
        await interaction.response.defer(ephemeral=True)
        await _apply_ban_vote(interaction.client, vote, scope="local", owner_id=interaction.user.id)
        await interaction.followup.send("🔨 Banned in this server only.", ephemeral=True)

    @discord.ui.button(label="Veto", style=discord.ButtonStyle.red, emoji="🛑", custom_id="cv:ban:veto")
    async def veto(self, interaction: discord.Interaction, button: discord.ui.Button):
        vote = await self._guard(interaction)
        if not vote:
            return
        await interaction.response.defer(ephemeral=True)
        await _veto_ban_vote(interaction.client, vote, interaction.user.id)
        await interaction.followup.send("🛑 Ban vote vetoed.", ephemeral=True)


async def _apply_promotion(client: commands.Bot, guild: discord.Guild, vote: dict):
    spec = PROMO_SPECS[vote["kind"]]
    member = guild.get_member(int(vote["target_id"]))
    vote["status"] = "applied"

    if member:
        new_role = guild.get_role(getattr(config, spec["role_attr"]))
        old_role = guild.get_role(getattr(config, spec["from_attr"]))
        try:
            if new_role:
                await member.add_roles(new_role, reason=f"Promotion vote {vote['id']} passed")
            if old_role and old_role in member.roles:
                await member.remove_roles(old_role, reason=f"Promotion to {spec['label']}")
            # Nickname prefix
            new_nick = apply_nick_prefix(member.nick or member.name, spec["label"])
            await member.edit(nick=new_nick, reason="Promotion prefix")
        except discord.Forbidden:
            await council_log(client, f"⚠️ Promotion `{vote['id']}` passed but I lack permission to update <@{vote['target_id']}>.")

    store.save_data()
    await _refresh_vote_message(client, vote)
    # Re-post message with quash button available to owners
    thread = client.get_channel(int(vote["thread_id"]))
    if thread:
        try:
            msg = await thread.fetch_message(int(vote["message_id"]))
            await msg.edit(view=QuashView())
        except discord.HTTPException:
            pass
    await council_log(client, f"✅ <@{vote['target_id']}> promoted to **{spec['label']}** (`{vote['id']}`).")


async def _quash_promotion(client: commands.Bot, guild: discord.Guild, vote: dict, owner_id: int):
    spec = PROMO_SPECS[vote["kind"]]
    member = guild.get_member(int(vote["target_id"]))
    vote["status"] = "quashed"

    if member:
        new_role = guild.get_role(getattr(config, spec["role_attr"]))
        old_role = guild.get_role(getattr(config, spec["from_attr"]))
        try:
            if new_role and new_role in member.roles:
                await member.remove_roles(new_role, reason=f"Promotion {vote['id']} quashed")
            if old_role:
                await member.add_roles(old_role, reason="Promotion quashed — reverting")
            reverted = apply_nick_prefix(member.nick or member.name,
                                         _label_for_role(guild, old_role) if old_role else "")
            await member.edit(nick=reverted, reason="Promotion quashed")
        except discord.Forbidden:
            pass

    store.save_data()
    await _refresh_vote_message(client, vote)
    thread = client.get_channel(int(vote["thread_id"]))
    if thread:
        try:
            msg = await thread.fetch_message(int(vote["message_id"]))
            await msg.edit(view=None)
        except discord.HTTPException:
            pass
    await council_log(client, f"🛑 Promotion of <@{vote['target_id']}> (`{vote['id']}`) quashed by <@{owner_id}>.")


def _label_for_role(guild: discord.Guild, role) -> str:
    if not role:
        return ""
    mapping = {config.GUEST_ROLE_ID: "Gast", config.MEMBER_ROLE_ID: "Member", config.VIP_ROLE_ID: "VIP"}
    return mapping.get(role.id, role.name)


async def _archive_thread(client: commands.Bot, vote: dict):
    thread = client.get_channel(int(vote["thread_id"]))
    if isinstance(thread, discord.Thread):
        try:
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class CouncilCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.council_group = app_commands.Group(
            name="council", description="Council procedures", guild_ids=[config.GUILD_ID]
        )
        self.vote_subgroup = app_commands.Group(
            name="vote", description="Start council votes", parent=self.council_group
        )
        self.admin_group = app_commands.Group(
            name="admin", description="Owner-only administration", guild_ids=[config.GUILD_ID]
        )
        self.admin_vote_subgroup = app_commands.Group(
            name="vote", description="Modify a vote in progress", parent=self.admin_group
        )
        self._register_commands()
        bot.tree.add_command(self.council_group)
        bot.tree.add_command(self.admin_group)

    @tasks.loop(minutes=1)
    async def tick(self):
        """Advance vote lifecycles on schedule."""
        now = datetime.datetime.now().timestamp()
        for vote in list(store.storage.get("votes", {}).values()):
            try:
                if vote["status"] == "comment" and now >= vote["comment_ends_at"]:
                    await _open_voting(self.bot, vote)
                elif vote["status"] == "voting" and now >= vote["voting_ends_at"]:
                    await _close_voting(self.bot, vote)
                elif vote["status"] == "veto" and now >= vote["veto_ends_at"]:
                    if vote["kind"] == "ban":
                        await _apply_ban_vote(self.bot, vote, scope="local")
                    else:
                        await _finalize_proposal(self.bot, vote)
            except Exception as e:
                print(f"Vote tick error on {vote.get('id')}: {e}")

    # ---- /verify ----

    @app_commands.command(name="verify", description="Verify a new user (assign Guest role)")
    @app_commands.guilds(discord.Object(id=config.GUILD_ID))
    @app_commands.describe(user="The user to verify")
    async def verify(self, interaction: discord.Interaction, user: discord.Member):
        if not is_council(interaction.user):
            return await interaction.response.send_message("❌ Only council/owners can verify users.", ephemeral=True)

        # Linked alts can't be verified directly — verify the primary account.
        primary = primary_of(str(user.id))
        if primary != str(user.id):
            return await interaction.response.send_message(
                f"❌ {user.mention} is a linked alt of <@{primary}>. "
                f"Alts can't be verified directly — verify the primary account instead.", ephemeral=True)

        guest_role = interaction.guild.get_role(config.GUEST_ROLE_ID)
        if not guest_role:
            return await interaction.response.send_message("❌ Guest role not found.", ephemeral=True)

        # Only unranked users can be verified — not existing Guests/Members/VIPs/Council/Owners.
        rank_role_ids = {config.GUEST_ROLE_ID, config.MEMBER_ROLE_ID, config.VIP_ROLE_ID,
                         config.COUNCIL_ROLE_ID, config.OWNER_ROLE_ID}
        held = next((r for r in user.roles if r.id in rank_role_ids), None)
        if held:
            return await interaction.response.send_message(
                f"❌ {user.mention} already has a rank (**{held.name}**) — nothing to verify.", ephemeral=True)

        # The bot must outrank the target (and they can't be the server owner) to set a nickname.
        me = interaction.guild.me
        if user.id == interaction.guild.owner_id or user.top_role >= me.top_role:
            return await interaction.response.send_message(
                f"❌ I can't manage {user.mention} — their top role is above mine (or they own the server). "
                f"Move my role higher and retry.", ephemeral=True)

        try:
            await user.add_roles(guest_role, reason=f"Verified by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I can't assign the Guest role — check my Manage Roles permission and that the role is below mine.",
                ephemeral=True)
        try:
            await user.edit(nick=apply_nick_prefix(user.name, "Gast"), reason="Verified — Gast prefix")
        except discord.Forbidden:
            await council_log(self.bot,
                              f"⚠️ Verified <@{user.id}> but couldn't set their nickname (permissions).")
            return await interaction.response.send_message(
                f"⚠️ Gave {user.mention} the Guest role, but couldn't set the **[Gast]** nickname "
                f"(their role may sit above mine).", ephemeral=True)

        await council_log(self.bot,
                          f"✅ <@{user.id}> verified by <@{interaction.user.id}> — assigned Guest.")
        note = ""
        if config.module_enabled("moderation"):
            from cogs import moderation  # lazy
            info = moderation.get_moderation_info(user.id)
            if not info.startswith("✅"):
                note = f"\n⚠️ Note — this user has a moderation record:\n{info}"
        await interaction.response.send_message(f"✅ Verified {user.mention} as Gast.{note}", ephemeral=True)

    def _register_commands(self):
        vote_group = self.vote_subgroup

        async def _start_promo(interaction: discord.Interaction, kind: str, user: discord.Member):
            if not is_council(interaction.user):
                return await interaction.response.send_message("❌ Council/owners only.", ephemeral=True)

            spec = PROMO_SPECS[kind]

            # Re-attempt block check
            unblock = is_blocked(kind, str(user.id))
            if unblock:
                return await interaction.response.send_message(
                    f"🚫 {user.mention} was recently defeated for **{spec['label']}**. "
                    f"Re-attempt allowed <t:{int(unblock)}:R>.", ephemeral=True)

            # Already has the target role?
            if interaction.guild.get_role(getattr(config, spec["role_attr"])) in user.roles:
                return await interaction.response.send_message(
                    f"❌ {user.mention} already holds **{spec['label']}**.", ephemeral=True)

            await interaction.response.defer(ephemeral=True)

            # Level eligibility via XP API
            required = getattr(config, spec["level_attr"])
            try:
                ok, detail = await xp_api.check_eligibility(
                    user.id, config.GUILD_ID, required, config.SERVER_LEVEL_RATIO)
            except xp_api.XPNotFound:
                return await interaction.followup.send(
                    f"❌ No level data found for {user.mention} (have they earned XP here?).", ephemeral=True)
            except xp_api.XPAuthError:
                return await interaction.followup.send("❌ XP API auth failed — check the token.", ephemeral=True)
            except xp_api.XPPathError:
                return await interaction.followup.send("❌ XP API path error — check the API base URL.", ephemeral=True)
            except xp_api.XPError as e:
                return await interaction.followup.send(f"❌ XP API error: {e}", ephemeral=True)

            if not ok:
                return await interaction.followup.send(
                    f"❌ {user.mention} is not yet eligible for **{spec['label']}** ({detail}).", ephemeral=True)

            vote = await self._create_vote(
                interaction, kind=kind,
                title=f"{spec['label']} promotion: {user.display_name}",
                description=f"Promotion vote for {user.mention} to **{spec['label']}**.\nEligibility: {detail}",
                target=user, voting_period=PROMO_VOTE_PERIOD,
            )
            await interaction.followup.send(
                f"✅ Started **{spec['label']}** promotion vote for {user.mention}: <#{vote['thread_id']}>",
                ephemeral=True)

        @vote_group.command(name="member", description="Start a Member promotion vote")
        @app_commands.describe(user="Candidate for Member")
        async def vote_member(interaction: discord.Interaction, user: discord.Member):
            await _start_promo(interaction, "member", user)

        @vote_group.command(name="vip", description="Start a VIP promotion vote")
        @app_commands.describe(user="Candidate for VIP")
        async def vote_vip(interaction: discord.Interaction, user: discord.Member):
            await _start_promo(interaction, "vip", user)

        @vote_group.command(name="proposal", description="Start a council proposal vote")
        async def vote_proposal(interaction: discord.Interaction):
            if not is_council(interaction.user):
                return await interaction.response.send_message("❌ Council/owners only.", ephemeral=True)
            await interaction.response.send_modal(ProposalModal(self))

        # Ban vote — only when the moderation module is also enabled (it does the banning).
        if config.module_enabled("moderation"):
            async def _start_ban(interaction: discord.Interaction, target: discord.Member,
                                 reason: str, rules: str, term: int, blacklist: bool):
                if not is_council(interaction.user):
                    return await interaction.response.send_message("❌ Council/owners only.", ephemeral=True)
                from cogs import moderation  # lazy
                if moderation._is_protected(target):
                    return await interaction.response.send_message(
                        "❌ That member is protected (admin/owner/council) and can't be ban-voted.",
                        ephemeral=True)
                await interaction.response.defer(ephemeral=True)
                term_days = None if term == 0 else int(term)   # term 0 = permanent
                rule_txt = ", ".join(f"Rule {r}" for r in moderation._parse_rule_ids(rules)) or "—"
                term_label = "Permanent" if term_days is None else f"{term_days} days"
                # Permanent term and/or network blacklist are owner-approval-only escalations.
                privileged = [n for n, on in (("permanent term", term_days is None),
                                              ("network blacklist", blacklist)) if on]
                note = (f"\n⚠️ Requested **{' + '.join(privileged)}** — requires **owner approval** "
                        f"at the veto window (otherwise it falls back to a finite local ban)." if privileged else "")
                desc = (f"Ban vote for {target.mention}.\n"
                        f"**Reason:** {reason}\n**Rules:** {rule_txt}\n"
                        f"**Term if passed:** {term_label}\n"
                        f"**Blacklist requested:** {'Yes' if blacklist else 'No'}{note}\n\n"
                        f"On pass, the owner chooses **Blacklist** (network-wide), **Local ban**, or **Veto**.")
                vote = await self._create_vote(
                    interaction, kind="ban", title=f"Ban: {target.display_name}",
                    description=desc, target=target, voting_period=PROMO_VOTE_PERIOD)
                vote["reason"] = reason
                vote["violated_rules"] = rules or ""
                vote["term_days"] = term_days            # None = permanent
                vote["blacklist_requested"] = bool(blacklist)
                store.save_data()
                await interaction.followup.send(
                    f"✅ Started a ban vote for {target.mention}: <#{vote['thread_id']}>", ephemeral=True)

            @vote_group.command(name="ban", description="Start a council ban vote")
            @app_commands.describe(target="User to ban", reason="Reason for the ban",
                                   rules="Comma-separated rule IDs (e.g. 1,4)",
                                   term="Ban length if it passes",
                                   blacklist="Request a network-wide blacklist (needs owner approval)")
            @app_commands.choices(term=[
                app_commands.Choice(name="90 days", value=90),
                app_commands.Choice(name="180 days", value=180),
                app_commands.Choice(name="365 days", value=365),
                app_commands.Choice(name="720 days", value=720),
                app_commands.Choice(name="1825 days", value=1825),
                app_commands.Choice(name="Permanent (needs owner approval)", value=0),
            ])
            async def vote_ban(interaction: discord.Interaction, target: discord.Member,
                               reason: str, rules: str = None, term: int = 365, blacklist: bool = False):
                await _start_ban(interaction, target, reason, rules, term, blacklist)

        admin_group = self.admin_group
        admin_vote = self.admin_vote_subgroup

        async def _active_vote_autocomplete(interaction: discord.Interaction, current: str):
            choices = []
            for vid, v in store.storage.get("votes", {}).items():
                if v.get("status") not in ("comment", "voting", "veto"):
                    continue
                label = f"{vid} · {v.get('title', '')}"[:100]
                if current.lower() in label.lower():
                    choices.append(app_commands.Choice(name=label, value=vid))
            return choices[:25]

        @admin_group.command(name="link", description="[OWNER] Link an alt account to a primary (counts as one voter)")
        @app_commands.describe(alt="The secondary account", primary="The main account it belongs to")
        async def admin_link(interaction: discord.Interaction, alt: discord.Member, primary: discord.Member):
            if not is_owner_member(interaction.user):
                return await interaction.response.send_message("❌ Owners only.", ephemeral=True)
            if alt.id == primary.id:
                return await interaction.response.send_message("❌ An account can't be linked to itself.", ephemeral=True)
            root = primary_of(primary.id)
            if str(alt.id) == root:
                return await interaction.response.send_message("❌ That would create a circular link.", ephemeral=True)
            link_alt(alt.id, root)
            await council_log(self.bot,
                              f"🔗 <@{interaction.user.id}> linked alt <@{alt.id}> → primary <@{root}>.")
            await interaction.response.send_message(
                f"🔗 Linked {alt.mention} as an alt of <@{root}>. They now count as one voter.",
                ephemeral=True)

        @admin_group.command(name="unlink", description="[OWNER] Remove an alt-account link")
        @app_commands.describe(alt="The secondary account to unlink")
        async def admin_unlink(interaction: discord.Interaction, alt: discord.Member):
            if not is_owner_member(interaction.user):
                return await interaction.response.send_message("❌ Owners only.", ephemeral=True)
            if unlink_alt(alt.id):
                await council_log(self.bot, f"🔗 <@{interaction.user.id}> unlinked alt <@{alt.id}>.")
                await interaction.response.send_message(f"🔗 Unlinked {alt.mention}.", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ {alt.mention} isn't linked as an alt.", ephemeral=True)

        @admin_group.command(name="links", description="[OWNER] List all linked alt accounts")
        async def admin_links(interaction: discord.Interaction):
            if not is_owner_member(interaction.user):
                return await interaction.response.send_message("❌ Owners only.", ephemeral=True)
            links = store.storage.get("alt_links", {})
            if not links:
                return await interaction.response.send_message("No alt accounts are linked.", ephemeral=True)
            lines = [f"• <@{alt}> → <@{primary}>" for alt, primary in links.items()]
            await interaction.response.send_message("**Linked alt accounts:**\n" + "\n".join(lines), ephemeral=True)

        @admin_group.command(name="voters", description="[OWNER] Show who is eligible to vote and how the count is derived")
        async def admin_voters(interaction: discord.Interaction):
            if not is_owner_member(interaction.user):
                return await interaction.response.send_message("❌ Owners only.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)

            guild = interaction.guild
            if not guild.chunked:
                try:
                    await guild.chunk()
                except Exception:
                    pass

            council_role = guild.get_role(config.COUNCIL_ROLE_ID)
            owner_role = guild.get_role(config.OWNER_ROLE_ID)
            council_members = list(council_role.members) if council_role else []
            owner_members = list(owner_role.members) if owner_role else []

            raw = {}  # primary_id -> set of source account ids
            for m in council_members:
                raw.setdefault(primary_of(m.id), set()).add(m.id)
            for m in owner_members:
                raw.setdefault(primary_of(m.id), set()).add(m.id)

            lines = []
            for prim, sources in raw.items():
                alts = [s for s in sources if str(s) != str(prim)]
                tag = ""
                if alts:
                    tag = " (via alt " + ", ".join(f"<@{a}>" for a in alts) + ")"
                prim_has_role = any(str(m.id) == str(prim) for m in council_members + owner_members)
                if not prim_has_role:
                    tag += " ⚠️ primary has no council/owner role"
                bot_flag = " 🤖 BOT" if any(_member_is_bot(guild, s) for s in sources) else ""
                lines.append(f"• <@{prim}>{tag}{bot_flag}")

            eligible = len(raw)
            embed = discord.Embed(
                title="🗳️ Eligible Voters",
                description="\n".join(sorted(lines)) or "No eligible voters found.",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Council role members", value=str(len(council_members)), inline=True)
            embed.add_field(name="Owner role members", value=str(len(owner_members)), inline=True)
            embed.add_field(name="Eligible (deduped)", value=f"**{eligible}**", inline=True)
            embed.set_footer(text="Eligible = council ∪ owners, alts collapsed to their primary.")
            await interaction.followup.send(embed=embed, ephemeral=True)

        # ---- /admin vote ... — modify a vote in progress ----

        async def _notify_vote(client, vote, text):
            """Post a notice to the vote thread and the council log."""
            thread = client.get_channel(int(vote["thread_id"]))
            if isinstance(thread, discord.Thread):
                try:
                    await thread.send(text)
                except discord.HTTPException:
                    pass
            await council_log(client, f"{text} (`{vote['id']}`)")

        @admin_vote.command(name="visibility", description="[OWNER] Change a running vote's visibility")
        @app_commands.describe(vote_id="The vote to modify", visibility="New visibility")
        @app_commands.autocomplete(vote_id=_active_vote_autocomplete)
        @app_commands.choices(visibility=[
            app_commands.Choice(name="Counts only (not who voted)", value="counts"),
            app_commands.Choice(name="Hidden (participation only)", value="hidden"),
            app_commands.Choice(name="Full (counts + who voted)", value="full"),
        ])
        async def admin_vote_visibility(interaction: discord.Interaction, vote_id: str, visibility: str):
            if not is_owner_member(interaction.user):
                return await interaction.response.send_message("❌ Owners only.", ephemeral=True)
            vote = store.storage.get("votes", {}).get(vote_id)
            if not vote or vote.get("status") not in ("comment", "voting", "veto"):
                return await interaction.response.send_message("❌ No active vote with that ID.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            vote["visibility"] = visibility
            store.save_data()
            await _refresh_vote_message(interaction.client, vote)
            await _notify_vote(interaction.client, vote,
                               f"👁️ <@{interaction.user.id}> set vote visibility to **{VISIBILITY_OPTIONS[visibility]}**")
            await interaction.followup.send("✅ Visibility updated.", ephemeral=True)

        @admin_vote.command(name="time", description="[OWNER] Change the remaining voting time")
        @app_commands.describe(vote_id="The vote to modify", remaining="New remaining time, e.g. 24h, 2d, 1d6h")
        @app_commands.autocomplete(vote_id=_active_vote_autocomplete)
        async def admin_vote_time(interaction: discord.Interaction, vote_id: str, remaining: str):
            if not is_owner_member(interaction.user):
                return await interaction.response.send_message("❌ Owners only.", ephemeral=True)
            vote = store.storage.get("votes", {}).get(vote_id)
            if not vote or vote.get("status") != "voting":
                return await interaction.response.send_message(
                    "❌ That vote isn't currently in its voting period.", ephemeral=True)
            try:
                secs = _parse_duration(remaining)
            except ValueError as e:
                return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            vote["voting_ends_at"] = datetime.datetime.now().timestamp() + secs
            store.save_data()
            await _refresh_vote_message(interaction.client, vote)
            await _notify_vote(interaction.client, vote,
                               f"⏱️ <@{interaction.user.id}> changed the remaining voting time — "
                               f"now closes <t:{int(vote['voting_ends_at'])}:R>")
            await interaction.followup.send("✅ Voting time updated.", ephemeral=True)

        @admin_vote.command(name="forcerecuse", description="[OWNER] Recuse a user from a running vote (e.g. unavailable)")
        @app_commands.describe(vote_id="The vote to modify", user="The user to recuse")
        @app_commands.autocomplete(vote_id=_active_vote_autocomplete)
        async def admin_vote_recuse(interaction: discord.Interaction, vote_id: str, user: discord.Member):
            if not is_owner_member(interaction.user):
                return await interaction.response.send_message("❌ Owners only.", ephemeral=True)
            vote = store.storage.get("votes", {}).get(vote_id)
            if not vote or vote.get("status") not in ("comment", "voting"):
                return await interaction.response.send_message("❌ No active vote with that ID.", ephemeral=True)
            if vote["mode"] == "true_unanimous":
                return await interaction.response.send_message(
                    "❌ Recusal isn't possible under a True Unanimous vote.", ephemeral=True)

            await interaction.response.defer(ephemeral=True)
            target = primary_of(user.id)
            recused = vote.setdefault("recused", [])
            if target in recused:
                return await interaction.followup.send(f"{user.mention} is already recused.", ephemeral=True)
            recused.append(target)
            # Forced recusal keeps any existing vote (per spec) — tally() ignores it.
            store.save_data()
            await _refresh_vote_message(interaction.client, vote)

            # Recusing shrinks the denominator; the vote may now be able to close.
            if vote["status"] == "voting":
                eligible = effective_eligible(vote, interaction.guild)
                if eligible > 0 and counted_votes(vote) >= eligible:
                    await _close_voting(interaction.client, vote)

            await _notify_vote(interaction.client, vote,
                               f"⭕ <@{interaction.user.id}> recused <@{user.id}> from this vote")
            await interaction.followup.send(f"✅ Recused {user.mention}.", ephemeral=True)

    async def _create_vote(self, interaction, kind, title, description, target, voting_period):
        guild = interaction.guild
        now = datetime.datetime.now().timestamp()

        vote_id = _new_vote_id()
        if kind == "proposal":
            cv_num = _next_cv_number()
            thread_name = f"CV{cv_num:02d} - {title}"[:100]
        else:
            thread_name = title[:100]

        vote_channel = guild.get_channel(config.VOTE_CHANNEL_ID)
        # Create the thread + initial message
        thread = await vote_channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
            reason=f"Council vote {vote_id}",
        )

        vote = {
            "id": vote_id,
            "kind": kind,
            "title": thread_name if kind == "proposal" else title,
            "description": description,
            "initiator_id": str(interaction.user.id),
            "target_id": str(target.id) if target else None,
            "thread_id": str(thread.id),
            "message_id": None,
            "owner_msg_id": None,
            "status": "comment",
            "mode": vl.DEFAULT_MODE,
            "visibility": "counts",
            "created_at": now,
            "comment_ends_at": now + COMMENT_PERIOD,
            "voting_ends_at": None,
            "veto_ends_at": None,
            "voting_period": voting_period,
            "eligible_snapshot": None,
            "votes": {},
            "recused": [],
        }
        store.storage.setdefault("votes", {})[vote_id] = vote

        msg = await thread.send(embed=build_vote_embed(guild, vote), view=CommentView())
        vote["message_id"] = str(msg.id)
        store.save_data()

        await council_log(self.bot,
                          f"📋 New {kind} vote **{vote['title']}** (`{vote_id}`) started by <@{interaction.user.id}>.")
        return vote


# ---------------------------------------------------------------------------
# Proposal modal & settings
# ---------------------------------------------------------------------------

class ProposalModal(discord.ui.Modal, title="New Council Proposal"):
    def __init__(self, cog: "CouncilCog"):
        super().__init__()
        self.cog = cog
        self.title_input = discord.ui.TextInput(label="Title", max_length=80, required=True)
        self.body_input = discord.ui.TextInput(
            label="Details", style=discord.TextStyle.paragraph, max_length=1500, required=True)
        self.add_item(self.title_input)
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        vote = await self.cog._create_vote(
            interaction, kind="proposal",
            title=self.title_input.value.strip(),
            description=self.body_input.value.strip(),
            target=None, voting_period=PROPOSAL_VOTE_PERIOD,
        )
        await interaction.followup.send(f"✅ Proposal thread created: <#{vote['thread_id']}>", ephemeral=True)


async def setup(bot: commands.Bot):
    cog = CouncilCog(bot)
    await bot.add_cog(cog)
    return cog
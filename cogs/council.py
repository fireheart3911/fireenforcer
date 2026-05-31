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


def count_eligible(guild: discord.Guild) -> int:
    """Number of people eligible to vote = council role holders ∪ owner role holders."""
    council = guild.get_role(config.COUNCIL_ROLE_ID)
    owner = guild.get_role(config.OWNER_ROLE_ID)
    voters = set()
    if council:
        voters.update(m.id for m in council.members)
    if owner:
        voters.update(m.id for m in owner.members)
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
    yes = sum(1 for v in vote["votes"].values() if v == "yes")
    no = sum(1 for v in vote["votes"].values() if v == "no")
    abstain = sum(1 for v in vote["votes"].values() if v == "abstain")
    return yes, no, abstain


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
        eligible = vote.get("eligible_snapshot") or count_eligible(guild)
        vis = vote.get("visibility", "counts")
        if vis == "hidden" and status == "voting":
            voted = len(vote["votes"])
            embed.add_field(name="Participation", value=f"{voted}/{eligible} have voted", inline=False)
        else:
            need = vl.required_yes(vote["mode"], eligible)
            embed.add_field(
                name="Tally",
                value=f"✅ Approve: **{yes}**  ·  ❌ Oppose: **{no}**  ·  ➖ Abstain: **{abstain}**\n"
                      f"Eligible: {eligible} · Needed to pass: {need}",
                inline=False,
            )
            if vis == "full":
                lines = []
                for uid, choice in vote["votes"].items():
                    icon = {"yes": "✅", "no": "❌", "abstain": "➖"}[choice]
                    lines.append(f"{icon} <@{uid}>")
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
    """Yes / No / Abstain buttons during the voting period."""
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

        vote["votes"][str(interaction.user.id)] = choice
        store.save_data()
        await _refresh_vote_message(interaction.client, vote)

        # Early close if everyone eligible has voted
        eligible = vote.get("eligible_snapshot") or count_eligible(interaction.guild)
        if len(vote["votes"]) >= eligible:
            await _close_voting(interaction.client, vote)

        await interaction.response.send_message(f"Recorded your vote: **{choice}**", ephemeral=True)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅", custom_id="cv:yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cast(interaction, "yes")

    @discord.ui.button(label="Oppose", style=discord.ButtonStyle.red, emoji="❌", custom_id="cv:no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cast(interaction, "no")

    @discord.ui.button(label="Abstain", style=discord.ButtonStyle.gray, emoji="➖", custom_id="cv:abstain")
    async def abstain(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cast(interaction, "abstain")


class VetoView(discord.ui.View):
    """Single veto button posted in the owner channel for proposals."""
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
        await _apply_veto(interaction.client, vote, interaction.user.id)
        await interaction.response.send_message("🛑 Proposal vetoed.", ephemeral=True)


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
        await _quash_promotion(interaction.client, interaction.guild, vote, interaction.user.id)
        await interaction.response.send_message("🛑 Promotion quashed and reverted.", ephemeral=True)


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
    vote["eligible_snapshot"] = count_eligible(guild)
    store.save_data()
    await _refresh_vote_message(client, vote)
    await council_log(client, f"🗳️ Voting opened for **{vote['title']}** (`{vote['id']}`).")


async def _close_voting(client: commands.Bot, vote: dict):
    guild = client.get_guild(config.GUILD_ID)
    eligible = vote.get("eligible_snapshot") or count_eligible(guild)
    yes, no, abstain = tally(vote)
    outcome = vl.resolve(vote["mode"], eligible, yes, no, abstain)

    if outcome == "passed":
        if vote["kind"] == "proposal":
            await _enter_veto_window(client, vote)
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
    mapping = {config.GUEST_ROLE_ID: "Guest", config.MEMBER_ROLE_ID: "Member", config.VIP_ROLE_ID: "VIP"}
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
        self._register_commands()
        bot.tree.add_command(self.council_group)

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

        guest_role = interaction.guild.get_role(config.GUEST_ROLE_ID)
        if not guest_role:
            return await interaction.response.send_message("❌ Guest role not found.", ephemeral=True)

        try:
            await user.add_roles(guest_role, reason=f"Verified by {interaction.user}")
            new_nick = apply_nick_prefix(user.nick or user.name, "Guest")
            await user.edit(nick=new_nick, reason="Verified — Guest prefix")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I lack permission to assign the role / change the nickname.", ephemeral=True)

        await council_log(self.bot,
                          f"✅ <@{user.id}> verified by <@{interaction.user.id}> — assigned Guest.")
        await interaction.response.send_message(f"✅ Verified {user.mention} as Guest.", ephemeral=True)

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
"""
Moderation module — cross-instance global banlist + fairplay flags.

The shared MariaDB (cogs/mod_db.py) is the source of truth; each instance keeps a
local mirror in storage["moderation"] so the hot-path gate checks are fast and
survive a DB blip. A global ban is enforced as a real Discord ban in every
instance's guild via the reconcile loop.

The module-level helpers (gate_check, get_moderation_info, is_banned, …) read the
mirror and are safe to import/call even when the module is disabled — they no-op.
mod_db imports aiomysql lazily, so `from cogs import moderation` never hard-requires
the dependency.
"""

import asyncio
import datetime
import json
import re
import time

import discord
from discord.ext import commands, tasks
from discord import app_commands

import storage as store
import config
from cogs import mod_db
from cogs.parsers import parse_duration_seconds


STUN_BASE = 300          # 5 minutes
STUN_CAP = 86_400        # 24 hours
HEAT_DECAY_PER_HOUR = 1.0

# Flag categories: stored value (DB) → label shown to staff.
FLAG_CATEGORIES = {
    "cheating":         "Cheating",
    "fairplay":         "Fair Play Violation",
    "toxicity":         "Toxicity / Harassment",
    "spam":             "Spam / Advertising",
    "ban_evasion":      "Ban Evasion",
    "scam":             "Scam / Fraud",
    "event_misconduct": "Event Misconduct",
    "note":             "Note",
    "other":            "Other",
}
_FLAG_CHOICES = [app_commands.Choice(name=label, value=value)
                 for value, label in FLAG_CATEGORIES.items()]


def _category_label(value) -> str:
    """Pretty label for a stored category value (legacy free-text → Title Case)."""
    return FLAG_CATEGORIES.get(value, (value or "flag").replace("_", " ").title())


# ---------------------------------------------------------------------------
# Local mirror + alt clustering
# ---------------------------------------------------------------------------

# The moderation mirror/heat live in their OWN file, never data.json — so a
# moderation write can never touch the bot's primary storage. It's just a cache
# of the shared DB (bans/flags rebuild from the DB on sync; only heat is local),
# so losing it is harmless.
_CACHE_FILE = "moderation_cache.json"
_cache = None


def _load_cache():
    global _cache
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _cache = {}


def _save_cache():
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"[moderation] failed to write {_CACHE_FILE}: {e}")


def _mod() -> dict:
    global _cache
    if _cache is None:
        _load_cache()
    _cache.setdefault("bans", {})
    _cache.setdefault("flags", {})
    _cache.setdefault("heat", {})
    _cache.setdefault("synced_at", 0)
    return _cache


def _cluster(user_id) -> set:
    """A user plus their linked alts (and shared primary), as string ids."""
    uid = str(user_id)
    links = store.storage.get("alt_links", {})
    primary = links.get(uid, uid)
    cluster = {primary}
    for alt, prim in links.items():
        if prim == primary:
            cluster.add(alt)
    return cluster


# ---------------------------------------------------------------------------
# Protected members (never auto-actioned)
# ---------------------------------------------------------------------------

def _is_protected(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if member.guild and member.id == member.guild.owner_id:
        return True
    if config.module_enabled("council"):
        protected = {config.COUNCIL_ROLE_ID, config.OWNER_ROLE_ID}
        if any(r.id in protected for r in member.roles):
            return True
    return False


def _is_owner(member: discord.Member) -> bool:
    if member.guild and member.id == member.guild.owner_id:
        return True
    if config.module_enabled("council") and config.OWNER_ROLE_ID:
        return any(r.id == config.OWNER_ROLE_ID for r in member.roles)
    return False


# ---------------------------------------------------------------------------
# Read helpers (mirror-backed; used by hooks in signup/events/council)
# ---------------------------------------------------------------------------

def is_banned(user_id) -> bool:
    bans = _mod()["bans"]
    return any(c in bans for c in _cluster(user_id))


def blocking_flags(user_id) -> list:
    flags = _mod()["flags"]
    out = []
    for c in _cluster(user_id):
        out.extend(f for f in flags.get(c, []) if f.get("blocks"))
    return out


def gate_check(user_id) -> tuple[bool, str]:
    """(allowed, reason). Fail-open / no-op when moderation is disabled."""
    if not config.module_enabled("moderation"):
        return True, ""
    if is_banned(user_id):
        return False, "You are banned and can't take part in this."
    bf = blocking_flags(user_id)
    if bf:
        why = bf[0].get("reason") or bf[0].get("type") or "a moderation flag"
        return False, f"You're blocked from participating ({why})."
    return True, ""


def get_moderation_info(user_id) -> str:
    """Short human summary for embeds (signup application review, /mod record)."""
    if not config.module_enabled("moderation"):
        return "✅ No incidents on record."
    m = _mod()
    cluster = _cluster(user_id)
    bans = [m["bans"][c] for c in cluster if c in m["bans"]]
    flags = [f for c in cluster for f in m["flags"].get(c, [])]
    if not bans and not flags:
        return "✅ No incidents on record."
    lines = []
    for b in bans:
        exp = "permanent" if not b.get("expires_at") else f"until <t:{int(b['expires_at'])}:R>"
        label = "Blacklisted" if b.get("scope") == "global" else "Banned"
        lines.append(f"⛔ **{label}** ({exp}) — {b.get('reason') or '—'}")
    for f in flags:
        blk = " 🚫" if f.get("blocks") else ""
        sev = f" [{f['severity']}]" if f.get("severity") else ""
        exp = "" if not f.get("expires_at") else f" · until <t:{int(f['expires_at'])}:R>"
        desc = f.get("reason") or "—"
        if len(desc) > 140:
            desc = desc[:140] + "…"
        rids = _parse_rule_ids(f.get("violated_rules"))
        rule_txt = f" · rules {','.join(map(str, rids))}" if rids else ""
        lines.append(f"⚑ **{_category_label(f.get('type'))}**{sev}{blk} — {desc}{rule_txt}{exp}")
    return "\n".join(lines)[:1024]


# ---------------------------------------------------------------------------
# Formatting + rule lookup
# ---------------------------------------------------------------------------

def _fmt_duration(secs: int) -> str:
    secs = int(secs)
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d} day{'s' if d != 1 else ''}")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    return " ".join(parts) or f"{secs}s"


def _term_word(expires_at) -> str:
    return "temporarily" if expires_at else "permanently"


def _duration_field(expires_at) -> str:
    if not expires_at:
        return "Permanent"
    return f"expires <t:{int(expires_at)}:F> (<t:{int(expires_at)}:R>)"


def _parse_rule_ids(s) -> list:
    return [int(t) for t in re.split(r"[,\s]+", (s or "").strip()) if t.isdigit()]


def _rule_fields(violated_rules) -> list:
    out = []
    for rid in _parse_rule_ids(violated_rules):
        r = config.MOD_RULES.get(rid)
        if r:
            out.append((f"Rule {rid}: {r['title']}", (r["text"] or "—")[:1024]))
        else:
            out.append((f"Rule {rid}", "—"))
    return out


# ---------------------------------------------------------------------------
# DM notices
# ---------------------------------------------------------------------------

async def _dm(client, user_id, embed) -> bool:
    try:
        user = await client.fetch_user(int(user_id))
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def _stun_notice(guild_name, secs, expires_at, reason) -> discord.Embed:
    e = discord.Embed(
        title="⏳ Calm down", color=discord.Color.orange(),
        description=f"You've been temporarily timed out in **{guild_name}** so things can settle down.")
    e.add_field(name="Duration", value=f"{_fmt_duration(secs)} · expires <t:{int(expires_at)}:R>", inline=True)
    e.add_field(name="Reason", value=reason or "No reason given", inline=True)
    e.set_footer(text="Repeated timeouts stack and get longer. Take a breather and come back calmer.")
    return e


def _appeal_field(embed: discord.Embed):
    if config.MOD_APPEAL_LINK:
        embed.add_field(
            name="Appeal",
            value=f"If you believe this is a mistake, you may appeal here: {config.MOD_APPEAL_LINK}",
            inline=False)


def _ban_notice(guild_name, reason, violated_rules, expires_at) -> discord.Embed:
    e = discord.Embed(
        title=f"❌ You are {_term_word(expires_at)} banned from {guild_name}",
        color=discord.Color.red(),
        description=(f"Your access to **{guild_name}** has been suspended. This was a "
                     f"moderation decision based on the conduct described below."))
    e.add_field(name="Duration", value=_duration_field(expires_at), inline=False)
    e.add_field(name="Reason", value=reason or "No reason provided", inline=False)
    for name, val in _rule_fields(violated_rules):
        e.add_field(name=name, value=val, inline=False)
    _appeal_field(e)
    e.set_footer(text=f"— {guild_name} Moderation")
    return e


def _blacklist_notice(reason, violated_rules, expires_at, approver) -> discord.Embed:
    net = config.MOD_NETWORK_NAME
    e = discord.Embed(
        title=f"⛔ You have been {_term_word(expires_at)} blacklisted",
        color=discord.Color.dark_red(),
        description=(f"You have been added to the global blacklist and are now banned from "
                     f"every server in the **{net}** network. This action was approved by {approver}."))
    e.add_field(name="Duration", value=_duration_field(expires_at), inline=False)
    e.add_field(name="Reason", value=reason or "No reason provided", inline=False)
    for name, val in _rule_fields(violated_rules):
        e.add_field(name=name, value=val, inline=False)
    _appeal_field(e)
    e.set_footer(text=f"— {net} Network Moderation")
    return e


async def send_ban_notice(client, user_id, reason, violated_rules, expires_at, scope,
                          guild_name, approver="the moderation team") -> bool:
    """Global (scope='global') → blacklist notice; local → ban notice. Sent once."""
    if scope == "global":
        embed = _blacklist_notice(reason, violated_rules, expires_at, approver)
    else:
        embed = _ban_notice(guild_name, reason, violated_rules, expires_at)
    return await _dm(client, user_id, embed)


# ---------------------------------------------------------------------------
# Mirror sync + Discord enforcement (module-level so council can drive them too)
# ---------------------------------------------------------------------------

async def _log(client, message=None, embed=None):
    if not config.MOD_LOG_CHANNEL_ID:
        return
    ch = client.get_channel(config.MOD_LOG_CHANNEL_ID)
    if ch:
        try:
            await ch.send(content=message, embed=embed)
        except discord.HTTPException:
            pass


async def sync_mirror(client):
    """Pull active bans/flags from the DB into the local mirror."""
    if not mod_db.is_ready():
        return
    await mod_db.deactivate_expired_bans()
    await mod_db.deactivate_expired_flags()
    bans = await mod_db.fetch_active_bans() or []
    flags = await mod_db.fetch_active_flags() or []
    m = _mod()
    m["bans"] = {
        str(b["user_id"]): {
            "reason": b["reason"], "violated_rules": b["violated_rules"],
            "scope": b["scope"], "source_guild": b["source_guild"],
            "expires_at": b["expires_at"],
        } for b in bans
    }
    fmap = {}
    for f in flags:
        fmap.setdefault(str(f["user_id"]), []).append({
            "id": f["id"], "type": f["type"], "reason": f["reason"],
            "severity": f.get("severity"), "violated_rules": f.get("violated_rules"),
            "blocks": bool(f["blocks"]), "expires_at": f["expires_at"],
        })
    m["flags"] = fmap
    m["synced_at"] = int(time.time())
    _save_cache()


async def enforce_guild(client, guild):
    """Reconcile this guild's Discord bans against the (already-synced) mirror."""
    if not guild or not mod_db.is_ready():
        return
    bans = _mod()["bans"]
    desired = set()
    for uid_s, b in bans.items():
        if b.get("scope") == "global" or int(b.get("source_guild") or 0) == guild.id:
            desired |= {int(c) for c in _cluster(uid_s)}
    applied = await mod_db.applied_for_guild(guild.id)

    for uid in desired - applied:
        member = guild.get_member(uid)
        if member and _is_protected(member):
            await _log(client, f"⚠️ Skipped auto-ban of protected member <@{uid}>.")
            continue
        try:
            await guild.ban(discord.Object(id=uid), reason="GlobalBan (moderation sync)")
            await mod_db.record_applied_ban(uid, guild.id)
        except discord.Forbidden:
            await _log(client, f"⚠️ Missing permission to ban <@{uid}>.")
        except discord.HTTPException:
            pass
    for uid in applied - desired:
        try:
            await guild.unban(discord.Object(id=uid), reason="GlobalBan lifted (moderation sync)")
        except (discord.NotFound, discord.HTTPException):
            pass
        await mod_db.remove_applied_ban(uid, guild.id)


async def sync_and_enforce(client):
    await sync_mirror(client)
    await enforce_guild(client, client.get_guild(config.GUILD_ID))


# ---------------------------------------------------------------------------
# Flag form
# ---------------------------------------------------------------------------

class FlagModal(discord.ui.Modal, title="Flag a user"):
    def __init__(self, target, category, severity, blocks, expires_at):
        super().__init__()
        self.target = target
        self.category = category
        self.severity = severity
        self.blocks = blocks
        self.expires_at = expires_at
        self.desc_input = discord.ui.TextInput(
            label="Description — what happened", style=discord.TextStyle.paragraph,
            required=True, max_length=1500)
        self.rules_input = discord.ui.TextInput(
            label="Violated rule IDs (e.g. 1,4) — optional", required=False, max_length=60)
        self.add_item(self.desc_input)
        self.add_item(self.rules_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not mod_db.is_ready():
            return await interaction.response.send_message(
                "❌ The moderation database isn't available.", ephemeral=True)
        label = _category_label(self.category)
        desc = self.desc_input.value.strip()
        rules = self.rules_input.value.strip()
        fid = await mod_db.add_flag(
            self.target.id, self.category, desc, self.blocks, interaction.user.id,
            self.expires_at, interaction.guild.id, severity=self.severity, violated_rules=rules)
        await sync_mirror(interaction.client)
        rule_txt = ", ".join(f"Rule {r}" for r in _parse_rule_ids(rules)) or "—"
        await _log(interaction.client,
                   f"⚑ <@{self.target.id}> flagged **{label}** [{self.severity}]"
                   f"{' (blocking)' if self.blocks else ''} by <@{interaction.user.id}> "
                   f"(`#{fid}`) · rules {rule_txt}\n{desc[:500]}")
        await interaction.response.send_message(
            f"⚑ Flagged {self.target.mention} — **{label}** [{self.severity}]"
            f"{' (blocks participation)' if self.blocks else ''}. Flag `#{fid}`.", ephemeral=True)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = app_commands.Group(
            name="mod", description="Moderation (global banlist & flags)",
            guild_ids=[config.GUILD_ID],
            default_permissions=discord.Permissions(administrator=True))
        self._register()
        bot.tree.add_command(self.group)

    # ---- mirror sync + enforcement (delegate to module-level helpers) ----

    async def _sync_mirror(self):
        await sync_mirror(self.bot)

    async def _sync_and_enforce(self):
        await sync_and_enforce(self.bot)

    async def _log(self, message=None, embed=None):
        await _log(self.bot, message, embed)

    @tasks.loop(minutes=2)
    async def reconcile(self):
        try:
            await sync_and_enforce(self.bot)
        except Exception as e:  # never let the loop die
            print(f"[moderation] reconcile error: {type(e).__name__}: {e}")

    # ---- commands ----

    def _register(self):
        group = self.group

        @group.command(name="ban", description="Add a user to the global blacklist (Discord ban across the network)")
        @app_commands.describe(user="User to blacklist", duration="e.g. 30d, 1825d (blank = permanent)",
                               reason="Why", rules="Comma-separated rule IDs (e.g. 1,4)",
                               delete_days="Delete this many days of their messages (0-7)")
        async def mod_ban(interaction: discord.Interaction, user: discord.User,
                          duration: str = None, reason: str = None,
                          rules: str = None, delete_days: int = 0):
            if not mod_db.is_ready():
                return await interaction.response.send_message(
                    "❌ The moderation database isn't configured on this instance.", ephemeral=True)
            member = interaction.guild.get_member(user.id)
            if member and _is_protected(member) and not _is_owner(interaction.user):
                return await interaction.response.send_message(
                    "❌ That member is protected (admin/owner/council). Only the owner can ban them.", ephemeral=True)
            expires_at = None
            if duration:
                try:
                    expires_at = time.time() + parse_duration_seconds(duration)
                except ValueError as e:
                    return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            reason = reason or "No reason provided"
            await interaction.response.defer(ephemeral=True)

            await mod_db.add_ban(user.id, reason, rules or "", interaction.user.id,
                                 expires_at, "global", interaction.guild.id)
            # Immediate, delete-aware ban of the primary target; cluster + sync handle the rest.
            try:
                await interaction.guild.ban(
                    discord.Object(id=user.id), reason=f"GlobalBan: {reason}",
                    delete_message_seconds=max(0, min(int(delete_days), 7)) * 86400)
                await mod_db.record_applied_ban(user.id, interaction.guild.id)
            except discord.Forbidden:
                await interaction.followup.send("⚠️ Recorded, but I lack permission to ban here.", ephemeral=True)
            await self._sync_and_enforce()
            await send_ban_notice(self.bot, user.id, reason, rules or "", expires_at,
                                  "global", interaction.guild.name)
            term = "permanent" if not expires_at else _fmt_duration(expires_at - time.time())
            await self._log(f"⛔ <@{user.id}> blacklisted by <@{interaction.user.id}> ({term}) — {reason}")
            await interaction.followup.send(
                f"⛔ Blacklisted {user.mention} ({term}). Synced to the network.", ephemeral=True)

        @group.command(name="unban", description="Remove a user from the global blacklist")
        @app_commands.describe(user="User to unban")
        async def mod_unban(interaction: discord.Interaction, user: discord.User):
            if not mod_db.is_ready():
                return await interaction.response.send_message(
                    "❌ The moderation database isn't configured on this instance.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            await mod_db.deactivate_ban(user.id)
            await self._sync_and_enforce()
            await self._log(f"✅ <@{user.id}> un-blacklisted by <@{interaction.user.id}>.")
            await interaction.followup.send(f"✅ Removed {user.mention} from the blacklist.", ephemeral=True)

        @group.command(name="flag", description="Flag a user — opens a form for the offence details")
        @app_commands.describe(user="User to flag", category="What kind of offence",
                               severity="How serious", blocks="Block them from events/signups?",
                               duration="Auto-expire, e.g. 30d (blank = until removed)")
        @app_commands.choices(category=_FLAG_CHOICES, severity=[
            app_commands.Choice(name="Low", value="low"),
            app_commands.Choice(name="Medium", value="medium"),
            app_commands.Choice(name="High", value="high")])
        async def mod_flag(interaction: discord.Interaction, user: discord.User, category: str,
                           severity: str = "medium", blocks: bool = False, duration: str = None):
            if not mod_db.is_ready():
                return await interaction.response.send_message(
                    "❌ The moderation database isn't configured on this instance.", ephemeral=True)
            expires_at = None
            if duration:
                try:
                    expires_at = time.time() + parse_duration_seconds(duration)
                except ValueError as e:
                    return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            await interaction.response.send_modal(FlagModal(user, category, severity, blocks, expires_at))

        @group.command(name="unflag", description="Remove a flag by its id")
        @app_commands.describe(flag_id="The flag id from /mod record")
        async def mod_unflag(interaction: discord.Interaction, flag_id: int):
            if not mod_db.is_ready():
                return await interaction.response.send_message(
                    "❌ The moderation database isn't configured on this instance.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            n = await mod_db.deactivate_flag(flag_id)
            await self._sync_mirror()
            if n:
                await self._log(f"✅ Flag `#{flag_id}` removed by <@{interaction.user.id}>.")
                await interaction.followup.send(f"✅ Removed flag `#{flag_id}`.", ephemeral=True)
            else:
                await interaction.followup.send("❌ No active flag with that id.", ephemeral=True)

        @group.command(name="record", description="Show a user's moderation record")
        @app_commands.describe(user="User to look up")
        async def mod_record(interaction: discord.Interaction, user: discord.User):
            embed = discord.Embed(title=f"Moderation record — {user}",
                                  description=_record_text(user.id), color=discord.Color.dark_grey(),
                                  timestamp=datetime.datetime.now())
            embed.set_thumbnail(url=user.display_avatar.url)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @group.command(name="list", description="Summary of active bans and flags")
        async def mod_list(interaction: discord.Interaction):
            m = _mod()
            nb, nf = len(m["bans"]), sum(len(v) for v in m["flags"].values())
            lines = [f"**Active bans:** {nb} · **Active flags:** {nf}",
                     f"_Mirror synced <t:{m['synced_at']}:R>_" if m.get("synced_at") else ""]
            for uid, b in list(m["bans"].items())[:15]:
                exp = "perm" if not b.get("expires_at") else f"<t:{int(b['expires_at'])}:R>"
                lines.append(f"⛔ <@{uid}> ({b.get('scope', 'global')}, {exp}) — {b.get('reason') or '—'}")
            await interaction.response.send_message("\n".join(l for l in lines if l)[:1900], ephemeral=True)

        # ---- /stun (top-level, only when council is also enabled) ----
        if config.module_enabled("council"):
            @self.bot.tree.command(name="stun", description="Heat-based calm-down timeout (escalates with repeats)",
                                   guild=config.GUILD_LIST[0])
            @app_commands.default_permissions(administrator=True)
            @app_commands.describe(user="Member to time out", reason="Why")
            async def stun(interaction: discord.Interaction, user: discord.Member, reason: str = None):
                if user.bot:
                    return await interaction.response.send_message("❌ Can't stun a bot.", ephemeral=True)
                if _is_protected(user):
                    return await interaction.response.send_message(
                        "❌ That member is protected (admin/owner/council).", ephemeral=True)
                m = _mod()
                now = time.time()
                h = m["heat"].get(str(user.id), {"heat": 0.0, "updated_at": now})
                decayed = max(0.0, h["heat"] - (now - h["updated_at"]) / 3600.0 * HEAT_DECAY_PER_HOUR)
                new_heat = decayed + 1
                m["heat"][str(user.id)] = {"heat": new_heat, "updated_at": now}
                _save_cache()
                level = max(1, round(new_heat))
                secs = min(STUN_BASE * (4 ** (level - 1)), STUN_CAP)
                until = discord.utils.utcnow() + datetime.timedelta(seconds=secs)
                try:
                    await user.timeout(until, reason=reason or f"Stunned by {interaction.user}")
                except discord.Forbidden:
                    return await interaction.response.send_message(
                        "❌ I can't time them out (missing Moderate Members or their role is above mine).",
                        ephemeral=True)
                await _dm(self.bot, user.id, _stun_notice(interaction.guild.name, secs, until.timestamp(), reason))
                await self._log(f"⏳ <@{user.id}> stunned for {_fmt_duration(secs)} by <@{interaction.user.id}> "
                                f"(heat {new_heat:.1f}) — {reason or '—'}")
                await interaction.response.send_message(
                    f"⏳ Timed out {user.mention} for **{_fmt_duration(secs)}**.", ephemeral=True)


def _record_text(user_id) -> str:
    m = _mod()
    cluster = _cluster(user_id)
    bans = [(c, m["bans"][c]) for c in cluster if c in m["bans"]]
    flags = [(c, f) for c in cluster for f in m["flags"].get(c, [])]
    if not bans and not flags:
        return "✅ No incidents on record."
    lines = []
    for c, b in bans:
        exp = "permanent" if not b.get("expires_at") else f"until <t:{int(b['expires_at'])}:R>"
        label = "Blacklist" if b.get("scope") == "global" else "Ban"
        lines.append(f"⛔ **{label}** (<@{c}>) — {exp} — {b.get('reason') or '—'}")
    for c, f in flags:
        blk = " 🚫" if f.get("blocks") else ""
        sev = f" [{f['severity']}]" if f.get("severity") else ""
        exp = "" if not f.get("expires_at") else f" · expires <t:{int(f['expires_at'])}:R>"
        line = (f"⚑ `#{f['id']}` **{_category_label(f.get('type'))}**{sev}{blk} (<@{c}>){exp}\n"
                f"　{f.get('reason') or '—'}")
        rfields = _rule_fields(f.get("violated_rules"))
        if rfields:
            line += "\n　Rules: " + "; ".join(name for name, _ in rfields)
        lines.append(line)
    return "\n".join(lines)[:4000]


async def setup(bot: commands.Bot):
    cog = ModerationCog(bot)
    await bot.add_cog(cog)
    _load_cache()  # moderation's own mirror file (never touches data.json)
    # Bound DB setup hard so an unreachable/slow database can never hang startup
    # (this runs inside setup_hook, before the bot finishes logging in).
    try:
        ready = await asyncio.wait_for(mod_db.init_pool(), timeout=15)
        if ready:
            await asyncio.wait_for(cog._sync_mirror(), timeout=15)
            print("[moderation] connected to shared DB and synced mirror.")
        else:
            print("[moderation] DB not configured — running in local-mirror/degraded mode.")
    except Exception as e:
        kind = "timed out" if isinstance(e, asyncio.TimeoutError) else f"{type(e).__name__}: {e}"
        print(f"[moderation] DB init/sync {kind} — degraded mode (moderation features inert).")
        try:
            await mod_db.close_pool()
        except Exception:
            pass
    return cog

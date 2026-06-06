import datetime

import discord
from discord.ext import commands
from discord import app_commands

import storage as store
import config


# ---------------------------------------------------------------------------
# Categories (driven by config.json → config.TICKET_CATEGORIES)
# ---------------------------------------------------------------------------
# config.TICKET_CATEGORIES is {key: {label, emoji, style, role_id, description}}.

_STYLE_MAP = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    "blurple": discord.ButtonStyle.primary,
    "grey": discord.ButtonStyle.secondary,
    "gray": discord.ButtonStyle.secondary,
    "green": discord.ButtonStyle.success,
    "red": discord.ButtonStyle.danger,
}

_COLOR_MAP = {
    "primary": discord.Color.blurple(),
    "blurple": discord.Color.blurple(),
    "secondary": discord.Color.greyple(),
    "grey": discord.Color.greyple(),
    "gray": discord.Color.greyple(),
    "success": discord.Color.green(),
    "green": discord.Color.green(),
    "danger": discord.Color.red(),
    "red": discord.Color.red(),
}


def _categories() -> dict:
    return config.TICKET_CATEGORIES


def _cat_style(cat: dict) -> discord.ButtonStyle:
    return _STYLE_MAP.get(cat.get("style", "secondary"), discord.ButtonStyle.secondary)


def _cat_color(cat: dict) -> discord.Color:
    return _COLOR_MAP.get(cat.get("style", "secondary"), discord.Color.greyple())


def _category_role_id(cat_key: str):
    cat = _categories().get(cat_key)
    return cat.get("role_id") if cat else None


def _panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎫 Open a Ticket",
        description="Pick the category that best fits your request. "
                    "A private thread will be created for you.",
        color=discord.Color.blurple(),
    )
    for cat in _categories().values():
        embed.add_field(
            name=f"{cat.get('emoji', '')} {cat['label']}".strip(),
            value=cat.get("description", ""),
            inline=False,
        )
    return embed
    return embed


# ---------------------------------------------------------------------------
# Shared close routine — used by the button, the closerequest prompt and auto-close
# ---------------------------------------------------------------------------

async def close_ticket(client: commands.Bot, thread: discord.Thread, ticket_id: str,
                       closed_by_id: str | None, reason: str | None, auto: bool = False):
    """Post a closing embed in the thread, log it, archive, and drop the record.

    closed_by_id=None + auto=True → auto-closed (no responder).
    reason=None → reason field is omitted everywhere.
    """
    data = store.storage.get("tickets", {}).get(ticket_id)
    reason = (reason or "").strip() or None

    # --- closing embed in the thread ---
    if auto:
        thread_desc = "🔒 This ticket was auto-closed (no response to the close request)."
    else:
        thread_desc = f"🔒 Ticket closed by <@{closed_by_id}>."
    thread_embed = discord.Embed(
        title="Ticket Closed",
        description=thread_desc,
        color=discord.Color.orange() if auto else discord.Color.green(),
        timestamp=datetime.datetime.now(),
    )
    if reason:
        thread_embed.add_field(name="📝 Reason", value=reason, inline=False)
    try:
        await thread.send(embed=thread_embed)
    except discord.HTTPException:
        pass

    # --- log embed ---
    log_channel = client.get_channel(config.TICKET_LOG_CHANNEL_ID.id)
    if log_channel and data:
        cat = data.get("ticket_type", "?")
        cat_label = _categories().get(cat, {}).get("label", cat)
        embed = discord.Embed(
            title="Ticket Auto-Closed" if auto else "Ticket Closed",
            description=f"Ticket Thread: {thread.mention}",
            color=discord.Color.orange() if auto else discord.Color.green(),
            timestamp=datetime.datetime.now(),
        )
        embed.add_field(name="#️⃣ Ticket ID", value=ticket_id, inline=True)
        embed.add_field(name="📥 Opened by", value=f"<@{data['user_id']}>", inline=True)
        if auto:
            embed.add_field(name="🕓 Auto-closed", value="No response", inline=True)
        elif closed_by_id:
            embed.add_field(name="📤 Closed by", value=f"<@{closed_by_id}>", inline=True)
        if data.get("claimed_by"):
            embed.add_field(name="🙋 Claimed by", value=f"<@{data['claimed_by']}>", inline=True)
        embed.add_field(name="🕓 Open Time", value=f"<t:{int(float(data['timestamp']))}:R>", inline=True)
        embed.add_field(name="📑 Topic", value=data.get("reason", "—"), inline=True)
        embed.add_field(name="🛠️ Category", value=cat_label, inline=True)
        if reason:
            embed.add_field(name="📝 Close Reason", value=reason, inline=True)
        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # --- DM the owner ---
    if data:
        try:
            owner = await client.fetch_user(int(data["user_id"]))
            cat_label = _categories().get(data.get("ticket_type"), {}).get("label", data.get("ticket_type", "?"))
            dm = discord.Embed(
                title="Your ticket was closed",
                description=(f"Your **{cat_label}** ticket (#{ticket_id}) has been closed."
                            + (" It auto-closed because there was no response to the close request."
                               if auto else "")),
                color=discord.Color.orange() if auto else discord.Color.green(),
                timestamp=datetime.datetime.now(),
            )
            if reason:
                dm.add_field(name="📝 Reason", value=reason, inline=False)
            dm.set_footer(text="If you still need help, feel free to open a new ticket.")
            await owner.send(embed=dm)
        except (discord.Forbidden, discord.HTTPException):
            pass  # DMs disabled or user unreachable — not fatal

    # --- archive + drop record ---
    try:
        await thread.edit(archived=True, locked=True)
    except discord.HTTPException:
        pass
    if ticket_id in store.storage.get("tickets", {}):
        del store.storage["tickets"][ticket_id]
        store.save_data()


def _is_pinged_role(member: discord.Member, ticket: dict) -> bool:
    """True if the member holds the role that was pinged for this ticket's category."""
    role_id = ticket.get("role_id")
    if not role_id:
        return False
    return any(str(r.id) == str(role_id) for r in member.roles)


def _ticket_from_thread(thread: discord.Thread) -> tuple[str | None, dict | None]:
    """Resolve a thread to (ticket_id, data) via stored thread_id (robust to renames)."""
    for tid, data in store.storage.get("tickets", {}).items():
        if str(data.get("thread_id")) == str(thread.id):
            return tid, data
    return None, None


# ---------------------------------------------------------------------------
# Panel view
# ---------------------------------------------------------------------------

class TicketOpenButton(discord.ui.Button):
    def __init__(self, cat_key: str, cat: dict):
        super().__init__(
            label=cat["label"],
            style=_cat_style(cat),
            emoji=cat.get("emoji") or None,
            custom_id=f"tickets:open:{cat_key}",
        )
        self.cat_key = cat_key

    async def callback(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.cat_key)


class TicketView(discord.ui.View):
    """Panel view — one button per configured category.

    Buttons use stable custom_ids (tickets:open:<key>) so the view is
    persistent and survives restarts. The set of categories is read from config.
    """
    def __init__(self):
        super().__init__(timeout=None)
        for key, cat in _categories().items():
            self.add_item(TicketOpenButton(key, cat))


async def create_ticket(interaction: discord.Interaction, cat_key: str):
    cat = _categories().get(cat_key)
    if not cat:
        return await interaction.response.send_message("❌ Unknown ticket category.", ephemeral=True)
    role_id = cat.get("role_id")

    modal = discord.ui.Modal(title=f"{cat['label']} Ticket")
    reason_input = discord.ui.TextInput(
        label="Reason for opening the ticket",
        style=discord.TextStyle.paragraph,
        placeholder="Please describe your issue...",
        required=True,
        max_length=500,
    )
    modal.add_item(reason_input)

    async def modal_callback(modal_interaction: discord.Interaction):
        new_id = store.next_id()
        thread = await interaction.channel.create_thread(
            name=f"{cat_key}-{new_id}-{interaction.user.name}",
            type=discord.ChannelType.private_thread,
            reason="New ticket",
        )
        embed = discord.Embed(
            title=f"{cat.get('emoji', '')} {cat['label']} Ticket Opened".strip(),
            description=f"Hello {interaction.user.mention}, thanks for opening a **{cat['label']}** ticket!\n"
                        f"A member of the team will be with you shortly.",
            color=_cat_color(cat),
        )
        embed.add_field(name="Reason", value=reason_input.value, inline=False)

        ping = f"<@&{role_id}>" if role_id else ""
        opened_msg = await thread.send(f"{interaction.user.mention} {ping}".strip(),
                                       embed=embed, view=TicketControlView())
        await modal_interaction.response.send_message(f"✅ Ticket opened: {thread.mention}", ephemeral=True)

        store.storage.setdefault("tickets", {})[str(new_id)] = {
            "user_id": str(interaction.user.id),
            "ticket_id": str(new_id),
            "ticket_type": cat_key,
            "reason": str(reason_input.value),
            "thread_id": str(thread.id),
            "message_id": str(opened_msg.id),
            "role_id": str(role_id) if role_id else None,
            "claimed_by": None,
            "timestamp": str(datetime.datetime.now().timestamp()),
        }
        store.save_data()

    modal.on_submit = modal_callback
    await interaction.response.send_modal(modal)


# ---------------------------------------------------------------------------
# In-thread controls: Claim / Close
# ---------------------------------------------------------------------------

async def _update_ticket_embed(client: commands.Bot, thread: discord.Thread, data: dict):
    """Re-render the original ticket-opened embed to reflect current claim state."""
    msg_id = data.get("message_id")
    if not msg_id:
        return
    try:
        msg = await thread.fetch_message(int(msg_id))
    except discord.HTTPException:
        return
    if not msg.embeds:
        return

    spec = _categories().get(data.get("ticket_type"), {})
    embed = msg.embeds[0]
    # Rebuild fields: keep Reason, set/replace the Claimed field.
    new = discord.Embed(title=embed.title, description=embed.description, color=embed.color)
    new.add_field(name="Reason", value=data.get("reason", "—"), inline=False)
    if data.get("claimed_by"):
        new.add_field(name="🙋 Claimed by", value=f"<@{data['claimed_by']}>", inline=False)
    else:
        new.add_field(name="🙋 Status", value="Unclaimed", inline=False)
    try:
        await msg.edit(embed=new)
    except discord.HTTPException:
        pass


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, emoji="🙋", custom_id="tickets:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message("Use this inside a ticket thread.", ephemeral=True)
        tid, data = _ticket_from_thread(interaction.channel)
        if not data:
            return await interaction.response.send_message("❌ Ticket data not found.", ephemeral=True)
        if not _is_pinged_role(interaction.user, data):
            return await interaction.response.send_message(
                "❌ Only the team handling this category can claim it.", ephemeral=True)

        if data.get("claimed_by"):
            if str(data["claimed_by"]) == str(interaction.user.id):
                data["claimed_by"] = None
                store.save_data()
                await _update_ticket_embed(interaction.client, interaction.channel, data)
                return await interaction.response.send_message("↩️ You released this ticket.", ephemeral=True)
            return await interaction.response.send_message(
                f"❌ Already claimed by <@{data['claimed_by']}>. They must unclaim it first.", ephemeral=True)

        data["claimed_by"] = str(interaction.user.id)
        store.save_data()
        await _update_ticket_embed(interaction.client, interaction.channel, data)
        await interaction.response.send_message("✅ You claimed this ticket.", ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.red, emoji="🔒", custom_id="tickets:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message("Use this inside a ticket thread.", ephemeral=True)
        tid, data = _ticket_from_thread(interaction.channel)
        if not data:
            return await interaction.response.send_message("❌ Ticket data not found.", ephemeral=True)
        await interaction.response.send_modal(CloseReasonModal(tid))


class CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    def __init__(self, ticket_id: str):
        super().__init__()
        self.ticket_id = ticket_id
        self.reason_input = discord.ui.TextInput(
            label="Close reason (optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Leave blank to close without a reason.",
            required=False,
            max_length=500,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("🔒 Closing ticket…", ephemeral=True)
        await close_ticket(interaction.client, interaction.channel, self.ticket_id,
                           closed_by_id=str(interaction.user.id),
                           reason=self.reason_input.value)


# ---------------------------------------------------------------------------
# Close request prompt (owner chooses; auto-close on timeout)
# ---------------------------------------------------------------------------

class CloseRequestView(discord.ui.View):
    """Posted by /closerequest. Only the ticket owner may press the buttons."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Now", style=discord.ButtonStyle.red, emoji="🔒", custom_id="tickets:cr_close")
    async def close_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        tid, data = _ticket_from_thread(interaction.channel) if isinstance(interaction.channel, discord.Thread) else (None, None)
        if not data:
            return await interaction.response.send_message("❌ Ticket data not found.", ephemeral=True)
        if str(interaction.user.id) != str(data["user_id"]):
            return await interaction.response.send_message(
                "❌ Only the ticket owner can answer the close request.", ephemeral=True)
        # Use any reason captured on the request.
        reason = data.get("autoclose_reason")
        data.pop("autoclose_at", None)
        data.pop("autoclose_reason", None)
        store.save_data()
        await interaction.response.send_message("🔒 Closing…", ephemeral=True)
        await _disable_message(interaction.message)
        await close_ticket(interaction.client, interaction.channel, tid,
                           closed_by_id=str(interaction.user.id), reason=reason)

    @discord.ui.button(label="Keep Open", style=discord.ButtonStyle.green, emoji="✅", custom_id="tickets:cr_keep")
    async def keep_open(self, interaction: discord.Interaction, button: discord.ui.Button):
        tid, data = _ticket_from_thread(interaction.channel) if isinstance(interaction.channel, discord.Thread) else (None, None)
        if not data:
            return await interaction.response.send_message("❌ Ticket data not found.", ephemeral=True)
        if str(interaction.user.id) != str(data["user_id"]):
            return await interaction.response.send_message(
                "❌ Only the ticket owner can answer the close request.", ephemeral=True)
        # Cancel the pending auto-close entirely.
        data.pop("autoclose_at", None)
        data.pop("autoclose_reason", None)
        store.save_data()
        await _disable_message(interaction.message)
        await interaction.response.send_message("✅ Kept open — the close request was cancelled.", ephemeral=False)


async def _disable_message(message: discord.Message):
    try:
        await message.edit(view=None)
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ticketpanel", description="Send the ticket panel with category buttons")
    @app_commands.guilds(discord.Object(id=config.GUILD_ID))
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticketpanel(self, interaction: discord.Interaction):
        await interaction.channel.send(embed=_panel_embed(), view=TicketView())
        await interaction.response.send_message("Ticket panel sent!", ephemeral=True)

    @app_commands.command(name="closerequest", description="Ask the ticket owner whether the ticket can be closed")
    @app_commands.guilds(discord.Object(id=config.GUILD_ID))
    @app_commands.describe(hours="Hours until the ticket auto-closes with no response",
                           reason="Optional reason, shown in the request and on close")
    async def closerequest(self, interaction: discord.Interaction, hours: float = 24.0, reason: str = None):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return await interaction.response.send_message(
                "You must use this command inside a ticket thread.", ephemeral=True)

        tid, data = _ticket_from_thread(thread)
        if not data:
            return await interaction.response.send_message("Ticket data not found.", ephemeral=True)

        # Only the role pinged on this ticket's category may request a close.
        if not _is_pinged_role(interaction.user, data):
            return await interaction.response.send_message(
                "❌ Only the team handling this ticket can request a close.", ephemeral=True)

        if hours <= 0:
            return await interaction.response.send_message("❌ Hours must be greater than zero.", ephemeral=True)

        autoclose_at = datetime.datetime.now().timestamp() + hours * 3600
        data["autoclose_at"] = autoclose_at
        if reason:
            data["autoclose_reason"] = reason.strip()
        store.save_data()

        embed = discord.Embed(
            title="🔔 Close Request",
            description=f"<@{data['user_id']}>, can this ticket be closed?\n\n"
                        f"If there's no response, it will auto-close <t:{int(autoclose_at)}:R>.",
            color=discord.Color.orange(),
        )
        if reason:
            embed.add_field(name="📝 Reason", value=reason.strip(), inline=False)

        await interaction.response.send_message("✅ Close request sent.", ephemeral=True)
        await thread.send(content=f"<@{data['user_id']}>", embed=embed, view=CloseRequestView())


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketsCog(bot))
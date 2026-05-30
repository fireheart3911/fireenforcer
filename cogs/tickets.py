import datetime
import discord
from discord.ext import commands
from discord import app_commands

import storage as store
import config


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Support Ticket", style=discord.ButtonStyle.primary, emoji="🛂", custom_id="tickets:support")
    async def support_ticket_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(button, interaction, "support", config.SUPPORT_ROLE_ID, discord.Color.green(), "Support")

    @discord.ui.button(label="Moderation Ticket", style=discord.ButtonStyle.primary, emoji="⚖️", custom_id="tickets:moderation")
    async def moderation_ticket_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(button, interaction, "moderation", config.MODERATION_ROLE_ID, discord.Color.blue(), "Moderation")

    @discord.ui.button(label="Administration Ticket", style=discord.ButtonStyle.danger, emoji="🛠️", custom_id="tickets:administration")
    async def administration_ticket_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(button, interaction, "administration", config.ADMINISTRATION_ROLE_ID, discord.Color.red(), "Administration")


class CloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, emoji="🔒", custom_id="tickets:close")
    async def close_ticket_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message(
                "This button can only be used in a ticket thread.", ephemeral=True
            )

        await interaction.channel.send(f"Ticket closed by {interaction.user.mention}")
        await interaction.response.send_message("Ticket closed.", ephemeral=True)
        await interaction.channel.edit(archived=True, locked=True)

        ticket_id = str(interaction.channel.name.split("-")[1])
        data = store.storage["tickets"].get(ticket_id)
        if data:
            log_channel = interaction.client.get_channel(config.TICKET_LOG_CHANNEL_ID.id)
            embed = discord.Embed(
                title="Ticket Closed",
                description=f"Ticket Thread: {interaction.channel.mention}",
                color=discord.Color.green(),
                timestamp=datetime.datetime.now(),
            )
            embed.add_field(name="#️⃣ Ticket ID", value=ticket_id, inline=True)
            embed.add_field(name="📥 Opened by", value=f"<@{data['user_id']}>", inline=True)
            embed.add_field(name="📤 Closed by", value=f"<@{interaction.user.id}>", inline=True)
            embed.add_field(name="🕓 Open Time", value=f"<t:{int(float(data['timestamp']))}:R>", inline=True)
            embed.add_field(name="📑 Topic", value=data["reason"], inline=True)
            embed.add_field(name="🛠️ Category", value=data["ticket_type"], inline=True)
            await log_channel.send(embed=embed)

            del store.storage["tickets"][ticket_id]
            store.save_data()


async def create_ticket(button, interaction, ticket_type, role_id, color, title):
    modal = discord.ui.Modal(title="Create a Ticket")
    reason_input = discord.ui.TextInput(
        label="Reason for opening the ticket",
        style=discord.TextStyle.short,
        placeholder="Please describe your issue...",
        required=True,
        max_length=500,
    )
    modal.add_item(reason_input)

    async def modal_callback(modal_interaction: discord.Interaction):
        new_id = store.next_id()
        thread = await interaction.channel.create_thread(
            name=f"{ticket_type}-{new_id}-{interaction.user.name}",
            type=discord.ChannelType.private_thread,
            reason="New ticket",
        )
        embed = discord.Embed(
            title=f"{title} Ticket Opened",
            description=f"Hello {interaction.user.mention}, thanks for opening a **{title}** ticket!",
            color=color,
        )
        embed.add_field(name="Reason", value=reason_input.value, inline=False)
        await thread.send(f"{interaction.user.mention} <@&{role_id}>", embed=embed, view=CloseView())
        await modal_interaction.response.send_message(f"✅ Ticket opened: {thread.mention}", ephemeral=True)

        store.storage["tickets"][str(new_id)] = {
            "user_id": str(interaction.user.id),
            "ticket_id": str(new_id),
            "ticket_type": str(ticket_type),
            "reason": str(reason_input.value),
            "thread_id": str(thread.id),
            "timestamp": str(datetime.datetime.now().timestamp()),
        }
        store.save_data()

    modal.on_submit = modal_callback
    await interaction.response.send_modal(modal)


class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ticketpanel", description="Send the ticket panel with category buttons")
    @commands.has_permissions(manage_guild=True)
    async def ticketpanel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Create a Ticket",
            description="Click the button below to create a ticket.\n ",
            color=discord.Color.blue(),
        )
        await interaction.channel.send(embed=embed, view=TicketView())
        await interaction.response.send_message("Ticket panel sent!", ephemeral=True)

    @app_commands.command(name="closerequest", description="Request ticket auto-close")
    @app_commands.describe(hours="Time in hours until auto-close", reason="Reason for closing")
    async def closerequest(self, interaction: discord.Interaction, hours: float, reason: str):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return await interaction.response.send_message(
                "You must use this command inside a ticket thread.", ephemeral=True
            )

        ticket_id = str(thread.name.split("-")[1])
        data = store.storage.get("tickets", {}).get(ticket_id)
        if not data:
            return await interaction.response.send_message("Ticket data not found.", ephemeral=True)

        data["autoclose_at"] = datetime.datetime.now().timestamp() + hours * 3600
        data["autoclose_reason"] = reason
        store.save_data()

        await interaction.response.send_message(
            f"✅ This ticket will auto-close in {hours:.2f} hour(s).", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketsCog(bot))

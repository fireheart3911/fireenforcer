import datetime
import math
import os
import urllib.error
import urllib.request
import asyncio

import discord
from discord.ext import commands, tasks

import storage as store
from config import (
    GUILD_ID, GUILD_LIST, TICKET_LOG_CHANNEL_ID, STATUS_CHANNEL_ID,
    STATUS_LOG_CHANNEL_ID, ELO_LOG_CHANNEL_ID, HEARTBEAT_URL,
)
from cogs.tickets import (
    TicketView, TicketControlView, CloseRequestView, close_ticket,
    setup as setup_tickets,
)
from cogs.status import StatusView, setup_status_message, setup as setup_status
from cogs.elo import EloSessionView, setup as setup_elo
from cogs.council import (
    CommentView, VoteView, VetoView, QuashView, setup as setup_council,
)

# ---------------------------------------------------------------------------
# Bot client
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True  # needed for complete role.members / eligible-voter counts


class BotClient(commands.Bot):

    async def setup_hook(self) -> None:
        # Register persistent views so buttons survive restarts
        self.add_view(TicketView())
        self.add_view(TicketControlView())
        self.add_view(CloseRequestView())
        self.add_view(StatusView())
        self.add_view(CommentView())
        self.add_view(VoteView())
        self.add_view(VetoView())
        self.add_view(QuashView())

        # Load cogs
        await setup_tickets(self)
        self._status_cog = await setup_status(
            self,
            guild_id=GUILD_ID,
            status_channel_id=STATUS_CHANNEL_ID.id,
            status_log_channel_id=STATUS_LOG_CHANNEL_ID.id,
        )
        self._elo_cog = await setup_elo(
            self,
            guild_id=GUILD_ID,
            elo_log_channel_id=ELO_LOG_CHANNEL_ID.id,
        )
        self._council_cog = await setup_council(self)

        # Sync slash commands
        for guild_obj in GUILD_LIST:
            try:
                synced = await self.tree.sync(guild=guild_obj)
                print(f"Synced {len(synced)} commands to guild {guild_obj.id}")
            except Exception as e:
                print(f"Error syncing commands: {e}")

    async def on_ready(self):
        print(f"Logged in as {self.user}")

        await self.change_presence(
            status=discord.Status.idle,
            activity=discord.Activity(type=discord.ActivityType.custom, name="🛰️ Running v1.4.8"),
        )

        store.load_data()
        await setup_status_message(self, STATUS_CHANNEL_ID.id)

        # Start task loops
        for loop in (
            self._status_cog.cleanup_expired_statuses,
            self._elo_cog.cleanup_old_sessions,
            self._council_cog.tick,
            self.autoclose_tickets,
        ):
            if not loop.is_running():
                loop.start()
        if HEARTBEAT_URL and not self.heartbeat.is_running():
            self.heartbeat.start()
            print(f"Heartbeat task started, pinging every 60 seconds")

    # ---- task loops ----

    @tasks.loop(minutes=1)
    async def autoclose_tickets(self):
        now = datetime.datetime.now().timestamp()
        to_close = [
            (tid, data)
            for tid, data in store.storage.get("tickets", {}).items()
            if data.get("autoclose_at") and now >= data["autoclose_at"]
        ]
        for tid, data in to_close:
            thread = self.get_channel(int(data["thread_id"]))
            if thread and isinstance(thread, discord.Thread):
                await close_ticket(self, thread, tid, closed_by_id=None,
                                   reason=data.get("autoclose_reason"), auto=True)
            else:
                # Thread is gone; just drop the orphaned record.
                del store.storage["tickets"][tid]
                store.save_data()

    @tasks.loop(seconds=60)
    async def heartbeat(self):
        if not HEARTBEAT_URL:
            return

        ping_ms = round(self.latency * 1000) if math.isfinite(self.latency) else 0
        url = f"{HEARTBEAT_URL}{ping_ms}"

        def make_request():
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    return r.status, None
            except urllib.error.HTTPError as e:
                return e.code, str(e)
            except urllib.error.URLError as e:
                return None, f"URLError: {e.reason}"
            except Exception as e:
                return None, f"{type(e).__name__}: {e}"

        try:
            status, error = await asyncio.get_event_loop().run_in_executor(None, make_request)
            if status and status != 200:
                print(f"⚠️ Heartbeat returned HTTP {status}")
            elif not status:
                server = HEARTBEAT_URL.split("/api/")[0] if "/api/" in HEARTBEAT_URL else HEARTBEAT_URL.split("?")[0]
                print(f"❌ Heartbeat failed: {error} (Server: {server})")
        except Exception as e:
            print(f"❌ Heartbeat error: {type(e).__name__}: {e or 'Unknown error'}")

    # ---- owner-only setup command ----

    @commands.command()
    @commands.is_owner()
    async def prepare(self, ctx: commands.Context):
        await ctx.send("Setup: TicketView", view=TicketView())

client = BotClient(command_prefix="!", intents=intents)


@client.tree.command(name="ping", description="Check bot latency", guild=GUILD_LIST[0])
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"{client.latency * 1000:.2f} ms", ephemeral=True)

client.run(os.environ["TOKEN"])
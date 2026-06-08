import asyncio
import datetime
import math
import os
import random
import urllib.error
import urllib.request

import discord
from discord.ext import commands, tasks

import storage as store
import config

# Cog imports — all live in the repo; whether they're *loaded* is config-driven.
from cogs.tickets import (
    TicketView, TicketControlView, CloseRequestView, close_ticket,
    setup as setup_tickets,
)
from cogs.status import StatusView, setup_status_message, setup as setup_status
from cogs.elo import EloSessionView, setup as setup_elo
from cogs.council import CommentView, VoteView, VetoView, QuashView, setup as setup_council
from cogs.signup import SignupView, ApplicationView, setup as setup_signup


# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True  # role.members completeness + nickname/role management


class BotClient(commands.Bot):

    async def setup_hook(self) -> None:
        enabled = config.module_enabled

        # --- persistent views (only for enabled modules) ---
        if enabled("tickets"):
            self.add_view(TicketView())
            self.add_view(TicketControlView())
            self.add_view(CloseRequestView())
        if enabled("status"):
            self.add_view(StatusView())
        if enabled("council"):
            self.add_view(CommentView())
            self.add_view(VoteView())
            self.add_view(VetoView())
            self.add_view(QuashView())
        if enabled("signup"):
            self.add_view(SignupView())
            self.add_view(ApplicationView())

        # --- load cogs ---
        self._status_cog = self._elo_cog = self._council_cog = self._signup_cog = None

        if enabled("tickets"):
            await setup_tickets(self)
        if enabled("status"):
            self._status_cog = await setup_status(
                self, guild_id=config.GUILD_ID,
                status_channel_id=config.STATUS_CHANNEL_ID.id,
                status_log_channel_id=config.STATUS_LOG_CHANNEL_ID.id,
            )
        if enabled("elo"):
            self._elo_cog = await setup_elo(
                self, guild_id=config.GUILD_ID,
                elo_log_channel_id=config.ELO_LOG_CHANNEL_ID.id,
            )
        if enabled("council"):
            self._council_cog = await setup_council(self)
        if enabled("signup"):
            self._signup_cog = await setup_signup(self)

        # --- sync slash commands once (here, not in on_ready) ---
        for guild_obj in config.GUILD_LIST:
            try:
                synced = await self.tree.sync(guild=guild_obj)
                print(f"Synced {len(synced)} commands to guild {guild_obj.id}")
            except Exception as e:
                print(f"Error syncing commands: {e}")

    async def on_ready(self):
        print(f"Logged in as {self.user}  |  modules: {', '.join(config.MODULES)}")

        await self._apply_presence()

        store.load_data()

        if config.module_enabled("status"):
            await setup_status_message(self, config.STATUS_CHANNEL_ID.id)

        # --- start loops (guarded against reconnect re-fire) ---
        loops = []
        if self._status_cog:
            loops.append(self._status_cog.cleanup_expired_statuses)
        if self._elo_cog:
            loops.append(self._elo_cog.cleanup_old_sessions)
        if self._council_cog:
            loops.append(self._council_cog.tick)
        if self._signup_cog:
            loops.append(self._signup_cog.schedule_tick)
        if config.module_enabled("tickets"):
            loops.append(self.autoclose_tickets)
        for loop in loops:
            if not loop.is_running():
                loop.start()

        if config.HEARTBEAT_URL and not self.heartbeat.is_running():
            self.heartbeat.start()
            print("Heartbeat task started, pinging every 60 seconds")

        if config.PRESENCE.get("mode") == "rotate" and not self.rotate_status.is_running():
            self.rotate_status.start()

    # ---- presence ----

    async def _apply_presence(self):
        p = config.PRESENCE or {}
        mode = p.get("mode", "fixed")
        if mode == "fixed":
            text = p.get("text", "")
            if text:
                await self.change_presence(
                    status=discord.Status.idle,
                    activity=discord.CustomActivity(name=text),
                )
        # rotate mode is handled by the rotate_status loop

    @tasks.loop(minutes=5)
    async def rotate_status(self):
        p = config.PRESENCE or {}
        path = p.get("file", "status.txt")
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if lines:
                await self.change_presence(activity=discord.CustomActivity(name=random.choice(lines)))
        except FileNotFoundError:
            pass

    # ---- ticket auto-close loop ----

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
                del store.storage["tickets"][tid]
                store.save_data()

    # ---- heartbeat ----

    @tasks.loop(seconds=60)
    async def heartbeat(self):
        if not config.HEARTBEAT_URL:
            return
        ping_ms = round(self.latency * 1000) if math.isfinite(self.latency) else 0
        url = f"{config.HEARTBEAT_URL}{ping_ms}"

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
                server = config.HEARTBEAT_URL.split("/api/")[0] if "/api/" in config.HEARTBEAT_URL else config.HEARTBEAT_URL.split("?")[0]
                print(f"❌ Heartbeat failed: {error} (Server: {server})")
        except Exception as e:
            print(f"❌ Heartbeat error: {type(e).__name__}: {e or 'Unknown error'}")

    # ---- owner setup command ----

    @commands.command()
    @commands.is_owner()
    async def prepare(self, ctx: commands.Context):
        if config.module_enabled("tickets"):
            await ctx.send("Setup: TicketView", view=TicketView())
        if config.module_enabled("signup"):
            await ctx.send("Setup: SignupView", view=SignupView())


client = BotClient(command_prefix=config.PREFIX, intents=intents)


@client.tree.command(name="ping", description="Check bot latency", guild=config.GUILD_LIST[0])
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"{client.latency * 1000:.2f} ms", ephemeral=True)


client.run(os.environ["TOKEN"])
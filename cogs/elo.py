import datetime
import discord
from discord.ext import commands, tasks
from discord import app_commands

import storage as store
import config
from cogs.elo_helpers import calculate_k_factor, calculate_elo_change, parse_match_scores


# ---------------------------------------------------------------------------
# Autocomplete / validation helpers
# ---------------------------------------------------------------------------

def validate_elo_type(elo_type: str) -> tuple[bool, str]:
    elo_type = elo_type.lower()
    if elo_type not in store.storage.get("elo_types", {}):
        available = ", ".join(f"'{t}'" for t in store.storage.get("elo_types", {}).keys())
        return False, (
            f"Elo type '{elo_type}' does not exist. Available types: {available}\n"
            "Ask an admin to create it with `/elo create_type`"
        )
    return True, elo_type


async def elo_type_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    choices = []
    for elo_id, elo_data in store.storage.get("elo_types", {}).items():
        display_name = elo_data.get("display_name", elo_id)
        if current.lower() in elo_id.lower() or current.lower() in display_name.lower():
            choices.append(app_commands.Choice(name=f"{display_name} ({elo_id})", value=elo_id))
    return choices[:25]


def get_match_type(session_id: str) -> str:
    session = store.storage["elo_sessions"].get(session_id)
    if not session:
        return "bo5"
    elo_type = session.get("elo_type", "default")
    return store.storage.get("elo_types", {}).get(elo_type, {}).get("match_type", "bo5")


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def build_session_dm_embed(
    session_id: str,
    user_id: str,
    is_player1: bool,
    match_details: list | None = None,
) -> discord.Embed | None:
    session = store.storage["elo_sessions"].get(session_id)
    if not session:
        return None

    elo_type = session.get("elo_type", "default")
    player1_id = session["player1_id"]
    player2_id = session["player2_id"]
    elo_type_data = store.storage.get("elo_types", {}).get(elo_type, {})
    display_name = elo_type_data.get("display_name", elo_type.title())
    match_type = elo_type_data.get("match_type", "bo5")
    status = session["status"]

    player1 = store.storage["elo_players"][player1_id][elo_type]
    player2 = store.storage["elo_players"][player2_id][elo_type]

    color_map = {
        "active": discord.Color.blurple(),
        "review": discord.Color.gold(),
        "verified": discord.Color.green(),
        "cancelled": discord.Color.dark_gray(),
    }
    prefix_map = {"active": "🎮", "review": "📋", "verified": "✅", "cancelled": "❌"}
    color = color_map.get(status, discord.Color.red())
    title_prefix = prefix_map.get(status, "⚠️")

    embed = discord.Embed(
        title=f"{title_prefix} Elo Session: {session_id}",
        description=f"**{display_name}** Elo ({match_type.upper()})",
        color=color,
        timestamp=datetime.datetime.now(),
    )

    # Players from caller's perspective
    you_id, opp_id = (player1_id, player2_id) if is_player1 else (player2_id, player1_id)
    you_data = player1 if is_player1 else player2
    opp_data = player2 if is_player1 else player1
    embed.add_field(name="You", value=f"<@{you_id}>\nRating: **{you_data['rating']:.0f}**", inline=True)
    embed.add_field(name="Opponent", value=f"<@{opp_id}>\nRating: **{opp_data['rating']:.0f}**", inline=True)

    status_text = {
        "active": "🟢 ACTIVE - Report matches below",
        "review": "🟡 REVIEW - Approve or deny results",
        "verified": "✅ VERIFIED - Elo updated!",
        "cancelled": "⚫ CANCELLED - No changes applied",
        "denied": "❌ DENIED",
    }
    embed.add_field(name="Status", value=status_text.get(status, status.upper()), inline=False)

    if session["matches"]:
        matches_text = ""
        your_wins = opp_wins = 0
        for i, match in enumerate(session["matches"], 1):
            p1s, p2s = match["player1_score"], match["player2_score"]
            your_score, opp_score = (p1s, p2s) if is_player1 else (p2s, p1s)
            if your_score > opp_score:
                winner = "🟢"; your_wins += 1
            else:
                winner = "🔴"; opp_wins += 1

            if match_details and i <= len(match_details):
                change = match_details[i - 1]["p1_change" if is_player1 else "p2_change"]
                matches_text += f"Match {i}: **{your_score}-{opp_score}** {winner} ({change:+.1f})\n"
            else:
                matches_text += f"Match {i}: **{your_score}-{opp_score}** {winner}\n"

        field_name = f"📊 Matches ({your_wins}-{opp_wins})"
        if status == "verified" and match_details:
            total_change = sum(d["p1_change" if is_player1 else "p2_change"] for d in match_details)
            field_name = f"📊 Final Results ({your_wins}-{opp_wins}) | Total: {total_change:+.1f}"
        embed.add_field(name=field_name, value=matches_text, inline=False)
    else:
        hint = "No matches yet. Use the quick buttons or 'Report Matches'!" if match_type == "bo5" else "No matches yet. Click 'Report Matches' to add scores!"
        embed.add_field(name="📊 Matches", value=hint, inline=False)

    if status == "review":
        p1s = "✅" if session.get("player1_approved") else "⏳"
        p2s = "✅" if session.get("player2_approved") else "⏳"
        embed.add_field(
            name="Approval Status",
            value=f"<@{player1_id}>: {p1s}\n<@{player2_id}>: {p2s}",
            inline=False,
        )

    footer_map = {
        "active": ("Use quick buttons or 'Report Matches' • 'End Session' when done" if match_type == "bo5"
                   else "Both players can report matches • Click 'End Session' when done"),
        "review": "Edit scores or approve/deny the results",
        "verified": "Session complete! Ratings have been updated.",
        "cancelled": "Session was cancelled. No rating changes.",
    }
    embed.set_footer(text=footer_map.get(status, ""))
    return embed


def create_session_embed(session_id: str) -> discord.Embed:
    session = store.storage["elo_sessions"][session_id]
    p1_id = session["player1_id"]
    p2_id = session["player2_id"]
    elo_type = session.get("elo_type", "default")
    p1 = store.storage["elo_players"][p1_id][elo_type]
    p2 = store.storage["elo_players"][p2_id][elo_type]
    display_name = store.storage.get("elo_types", {}).get(elo_type, {}).get("display_name", elo_type.title())

    embed = discord.Embed(
        title=f"🎮 Elo Session: {session_id}",
        description=f"**{display_name}** Elo\n<@{p1_id}> ({p1['rating']:.0f}) vs <@{p2_id}> ({p2['rating']:.0f})",
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.fromtimestamp(float(session["created_at"])),
    )

    if session["matches"]:
        text = ""
        p1w = p2w = 0
        for i, m in enumerate(session["matches"], 1):
            s1, s2 = m["player1_score"], m["player2_score"]
            if s1 > s2:
                icon = "🟢"; p1w += 1
            elif s2 > s1:
                icon = "🔴"; p2w += 1
            else:
                icon = "⚪"
            text += f"Match {i}: **{s1}-{s2}** {icon}\n"
        embed.add_field(name=f"📊 Matches ({p1w}-{p2w})", value=text, inline=False)
    else:
        embed.add_field(name="📊 Matches", value="No matches yet. Click 'Report Matches' to begin!", inline=False)

    status_emoji = {"active": "🟢", "review": "🟡", "verified": "✅", "denied": "❌", "cancelled": "⚫"}
    status_text = {"active": "ACTIVE", "review": "UNDER REVIEW", "verified": "VERIFIED", "denied": "DENIED", "cancelled": "CANCELLED"}
    embed.add_field(
        name="Status",
        value=f"{status_emoji.get(session['status'], '⚪')} {status_text.get(session['status'], 'UNKNOWN')}",
        inline=True,
    )
    embed.set_footer(text="Click 'View in DMs' to manage this session")
    return embed


# ---------------------------------------------------------------------------
# DM helpers
# ---------------------------------------------------------------------------

async def send_session_dm_to_player(
    bot: commands.Bot, session_id: str, user_id: str, is_player1: bool
) -> bool:
    session = store.storage["elo_sessions"].get(session_id)
    if not session:
        return False

    embed = build_session_dm_embed(session_id, user_id, is_player1)
    if not embed:
        return False

    view = None
    if session["status"] == "active":
        view = EloDMSessionView(session_id, is_player1)
    elif session["status"] == "review":
        view = EloReviewView(session_id, is_player1)

    try:
        user = await bot.fetch_user(int(user_id))
        msg = await user.send(embed=embed, view=view)
        session.setdefault("dm_messages", {})[user_id] = str(msg.id)
        store.save_data()
        return True
    except discord.Forbidden:
        print(f"Cannot send DM to {user_id} - DMs disabled")
        session.setdefault("dm_disabled", [])
        if user_id not in session["dm_disabled"]:
            session["dm_disabled"].append(user_id)
        store.save_data()
        return False
    except discord.HTTPException as e:
        print(f"Failed to send session DM to {user_id}: {e}")
        return False


async def update_session_dm_embeds(
    bot: commands.Bot, session_id: str, match_details: list | None = None
):
    session = store.storage["elo_sessions"].get(session_id)
    if not session:
        return

    p1_id = session["player1_id"]
    for user_id, msg_id in session.get("dm_messages", {}).items():
        is_player1 = user_id == p1_id
        try:
            user = await bot.fetch_user(int(user_id))
            dm = user.dm_channel or await user.create_dm()
            msg = await dm.fetch_message(int(msg_id))

            embed = build_session_dm_embed(session_id, user_id, is_player1, match_details)
            if not embed:
                continue

            view = None
            if session["status"] == "active":
                view = EloDMSessionView(session_id, is_player1)
            elif session["status"] == "review":
                view = EloReviewView(session_id, is_player1)

            await msg.edit(embed=embed, view=view)
        except Exception as e:
            print(f"Failed to update DM for {user_id}: {e}")


async def update_session_channel_embed(bot: commands.Bot, session_id: str):
    session = store.storage["elo_sessions"].get(session_id)
    if not session or not session.get("channel_id") or not session.get("message_id"):
        return
    try:
        channel = bot.get_channel(int(session["channel_id"]))
        if not channel:
            return
        msg = await channel.fetch_message(int(session["message_id"]))
        await msg.edit(embed=create_session_embed(session_id))
    except Exception:
        pass


async def send_review_dm(bot: commands.Bot, session_id: str):
    session = store.storage["elo_sessions"].get(session_id)
    if not session:
        return

    if session.get("dm_messages"):
        await update_session_dm_embeds(bot, session_id)
        return

    for user_id, is_player1 in [(session["player1_id"], True), (session["player2_id"], False)]:
        try:
            user = await bot.fetch_user(int(user_id))
            embed = build_session_dm_embed(session_id, user_id, is_player1)
            if not embed:
                continue
            msg = await user.send(embed=embed, view=EloReviewView(session_id, is_player1))
            session.setdefault("dm_messages", {})[user_id] = str(msg.id)
            store.save_data()
        except discord.Forbidden:
            session.setdefault("dm_disabled", [])
            if user_id not in session["dm_disabled"]:
                session["dm_disabled"].append(user_id)
            store.save_data()
        except discord.HTTPException as e:
            print(f"Failed to send review DM to {user_id}: {e}")


# ---------------------------------------------------------------------------
# Session verification
# ---------------------------------------------------------------------------

async def process_session_verification(
    bot: commands.Bot, elo_log_channel_id: int, session_id: str, silent: bool = False
):
    session = store.storage["elo_sessions"][session_id]
    p1_id = session["player1_id"]
    p2_id = session["player2_id"]
    elo_type = session.get("elo_type", "default")

    p1 = store.storage["elo_players"][p1_id][elo_type]
    p2 = store.storage["elo_players"][p2_id][elo_type]

    total_p1 = total_p2 = 0.0
    p1_wins = p2_wins = 0
    match_details = []

    for i, match in enumerate(session["matches"], 1):
        s1, s2 = match["player1_score"], match["player2_score"]
        if s1 > s2:
            p1_wins += 1; result = "P1 Win"
        elif s2 > s1:
            p2_wins += 1; result = "P2 Win"
        else:
            result = "Draw"

        k = (calculate_k_factor(p1["matches_played"]) + calculate_k_factor(p2["matches_played"])) / 2
        c1, c2 = calculate_elo_change(p1["rating"], p2["rating"], s1, s2, k)

        match_details.append({
            "match_num": i,
            "score": f"{s1}-{s2}",
            "result": result,
            "p1_change": c1,
            "p2_change": c2,
            "p1_rating_before": p1["rating"],
            "p2_rating_before": p2["rating"],
        })

        total_p1 += c1
        total_p2 += c2
        p1["rating"] += c1
        p2["rating"] += c2
        p1["matches_played"] += 1
        p2["matches_played"] += 1

    p1["wins"] += p1_wins; p1["losses"] += p2_wins
    p2["wins"] += p2_wins; p2["losses"] += p1_wins
    p1["peak_rating"] = max(p1["peak_rating"], p1["rating"])
    p2["peak_rating"] = max(p2["peak_rating"], p2["rating"])

    session["status"] = "verified"
    session["verified_at"] = str(datetime.datetime.now().timestamp())
    store.save_data()

    if not silent:
        await update_session_dm_embeds(bot, session_id, match_details)
        await log_elo_event(
            bot, elo_log_channel_id, "session_verified", elo_type,
            session_id=session_id, player1_id=p1_id, player2_id=p2_id,
            p1_change=total_p1, p2_change=total_p2,
            num_matches=len(session["matches"]), match_details=match_details,
        )

    # Update channel embed
    try:
        if session.get("channel_id") and session.get("message_id"):
            channel = await bot.fetch_channel(int(session["channel_id"]))
            msg = await channel.fetch_message(int(session["message_id"]))
            await msg.edit(embed=create_session_embed(session_id), view=None)
    except Exception:
        pass

    del store.storage["elo_sessions"][session_id]
    store.save_data()


# ---------------------------------------------------------------------------
# Log helper
# ---------------------------------------------------------------------------

async def log_elo_event(bot: commands.Bot, elo_log_channel_id: int, event_type: str, elo_type: str = "default", **kwargs):
    log_channel = bot.get_channel(elo_log_channel_id)
    if not log_channel:
        return

    display_name = store.storage.get("elo_types", {}).get(elo_type, {}).get("display_name", elo_type.title())

    if event_type == "registered":
        user_id = kwargs["user_id"]
        player = store.storage["elo_players"][user_id][elo_type]
        embed = discord.Embed(title="📝 Player Registered", description=f"New player registered for **{display_name}** Elo", color=discord.Color.blue(), timestamp=datetime.datetime.now())
        embed.add_field(name="Player", value=f"<@{user_id}>", inline=True)
        embed.add_field(name="Starting Rating", value=f"**{player['rating']:.0f}**", inline=True)

    elif event_type == "session_created":
        session_id = kwargs["session_id"]
        p1_id = kwargs["player1_id"]
        p2_id = kwargs["player2_id"]
        p1 = store.storage["elo_players"][p1_id][elo_type]
        p2 = store.storage["elo_players"][p2_id][elo_type]
        embed = discord.Embed(title="🎮 Session Created", description=f"New **{display_name}** Elo session: `{session_id}`", color=discord.Color.blurple(), timestamp=datetime.datetime.now())
        embed.add_field(name="Player 1", value=f"<@{p1_id}>\nRating: **{p1['rating']:.0f}**", inline=True)
        embed.add_field(name="Player 2", value=f"<@{p2_id}>\nRating: **{p2['rating']:.0f}**", inline=True)

    elif event_type == "session_verified":
        session_id = kwargs["session_id"]
        p1_id = kwargs["player1_id"]
        p2_id = kwargs["player2_id"]
        p1_change = kwargs.get("p1_change", 0)
        p2_change = kwargs.get("p2_change", 0)
        num_matches = kwargs.get("num_matches", 0)
        match_details = kwargs.get("match_details", [])
        p1 = store.storage["elo_players"][p1_id][elo_type]
        p2 = store.storage["elo_players"][p2_id][elo_type]
        embed = discord.Embed(title="✅ Session Verified", description=f"Session `{session_id}` completed - **{display_name}** Elo", color=discord.Color.green(), timestamp=datetime.datetime.now())
        embed.add_field(name=f"<@{p1_id}>", value=f"Rating: **{p1['rating']:.0f}** ({p1_change:+.1f})\nRecord: {p1['wins']}W-{p1['losses']}L", inline=True)
        embed.add_field(name=f"<@{p2_id}>", value=f"Rating: **{p2['rating']:.0f}** ({p2_change:+.1f})\nRecord: {p2['wins']}W-{p2['losses']}L", inline=True)
        if match_details:
            breakdown = ""
            for d in match_details:
                breakdown += f"**Match {d['match_num']}:** {d['score']} ({d['result']})\n"
                breakdown += f"  <@{p1_id}>: {d['p1_rating_before']:.0f} → {d['p1_rating_before'] + d['p1_change']:.0f} ({d['p1_change']:+.1f})\n"
                breakdown += f"  <@{p2_id}>: {d['p2_rating_before']:.0f} → {d['p2_rating_before'] + d['p2_change']:.0f} ({d['p2_change']:+.1f})\n"
            embed.add_field(name="📊 Match Breakdown", value=breakdown, inline=False)
        embed.add_field(name="Total Matches", value=str(num_matches), inline=False)

    elif event_type == "type_created":
        elo_id = kwargs["elo_id"]
        type_display_name = kwargs["display_name"]
        created_by = kwargs["created_by"]
        embed = discord.Embed(title="🆕 Elo Type Created", description="New Elo type added to the system", color=discord.Color.gold(), timestamp=datetime.datetime.now())
        embed.add_field(name="ID", value=f"`{elo_id}`", inline=True)
        embed.add_field(name="Display Name", value=f"**{type_display_name}**", inline=True)
        embed.add_field(name="Created By", value=f"<@{created_by}>", inline=True)
    else:
        return

    await log_channel.send(embed=embed)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class EloDMSessionView(discord.ui.View):
    def __init__(self, session_id: str, is_player1: bool):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.is_player1 = is_player1
        if get_match_type(session_id) == "bo5":
            self._add_bo5_buttons()

    def _add_bo5_buttons(self):
        for opp_score in range(5):
            btn = discord.ui.Button(label=f"5-{opp_score}", style=discord.ButtonStyle.green, row=0)
            btn.callback = self._make_quick_submit(5, opp_score)
            self.add_item(btn)
        for opp_score in range(5):
            btn = discord.ui.Button(label=f"{opp_score}-5", style=discord.ButtonStyle.red, row=1)
            btn.callback = self._make_quick_submit(opp_score, 5)
            self.add_item(btn)

    def _make_quick_submit(self, your_score: int, opp_score: int):
        async def callback(interaction: discord.Interaction):
            client = interaction.client
            session = store.storage["elo_sessions"].get(self.session_id)
            if not session:
                return await interaction.response.send_message("❌ Session not found!", ephemeral=True)
            if session["status"] != "active":
                return await interaction.response.send_message(f"❌ Session status: {session['status']}", ephemeral=True)

            p1s, p2s = (your_score, opp_score) if self.is_player1 else (opp_score, your_score)
            session["matches"].append({"player1_score": p1s, "player2_score": p2s})
            store.save_data()

            await update_session_dm_embeds(client, self.session_id)
            await update_session_channel_embed(client, self.session_id)
            await interaction.response.send_message(f"✅ Reported match: **{your_score}-{opp_score}**", ephemeral=True)
        return callback

    @discord.ui.button(label="Report Matches", style=discord.ButtonStyle.gray, emoji="📊", row=2)
    async def report_matches_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = store.storage["elo_sessions"].get(self.session_id)
        if not session:
            return await interaction.response.send_message("❌ Session not found!", ephemeral=True)
        if session["status"] != "active":
            return await interaction.response.send_message(f"❌ Session status: {session['status']}", ephemeral=True)
        await interaction.response.send_modal(
            ReportMatchesDMModal(self.session_id, self.is_player1, get_match_type(self.session_id))
        )

    @discord.ui.button(label="End Session", style=discord.ButtonStyle.blurple, emoji="✅", row=2)
    async def end_session_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        client = interaction.client
        session = store.storage["elo_sessions"].get(self.session_id)
        if not session:
            return await interaction.response.send_message("❌ Session not found!", ephemeral=True)
        if session["status"] != "active":
            return await interaction.response.send_message(f"❌ Session status: {session['status']}", ephemeral=True)
        if not session["matches"]:
            return await interaction.response.send_message("❌ Report at least one match before ending the session!", ephemeral=True)

        session["status"] = "review"
        user_id = str(interaction.user.id)
        session["player1_approved"] = user_id == session["player1_id"]
        session["player2_approved"] = user_id == session["player2_id"]
        store.save_data()

        await update_session_dm_embeds(client, self.session_id)
        await update_session_channel_embed(client, self.session_id)
        await interaction.response.send_message("✅ Session ended. Review the results and approve!", ephemeral=True)

    @discord.ui.button(label="Cancel Session", style=discord.ButtonStyle.red, emoji="🗑️", row=2)
    async def cancel_session_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        client = interaction.client
        session = store.storage["elo_sessions"].get(self.session_id)
        if not session:
            return await interaction.response.send_message("❌ Session not found!", ephemeral=True)
        if session["status"] not in ("active", "review"):
            return await interaction.response.send_message(f"❌ Cannot cancel session with status: {session['status']}", ephemeral=True)

        session["status"] = "cancelled"
        store.save_data()
        await update_session_channel_embed(client, self.session_id)
        await update_session_dm_embeds(client, self.session_id)
        await interaction.response.send_message("❌ Session cancelled. No Elo changes applied.", ephemeral=True)


class EloReviewView(discord.ui.View):
    def __init__(self, session_id: str, is_player1: bool):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.is_player1 = is_player1

    @discord.ui.button(label="Edit Scores", style=discord.ButtonStyle.gray, emoji="✏️")
    async def edit_scores_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = store.storage["elo_sessions"].get(self.session_id)
        if not session:
            return await interaction.response.send_message("❌ Session not found!", ephemeral=True)
        if session["status"] != "review":
            return await interaction.response.send_message(f"❌ Session is not in review.", ephemeral=True)
        await interaction.response.send_modal(EditScoresModal(self.session_id, self.is_player1))

    @discord.ui.button(label="Approve Results", style=discord.ButtonStyle.green, emoji="✅")
    async def approve_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        client = interaction.client
        session = store.storage["elo_sessions"].get(self.session_id)
        if not session:
            return await interaction.response.send_message("❌ Session not found!", ephemeral=True)
        if session["status"] != "review":
            return await interaction.response.send_message(f"❌ Session is not in review.", ephemeral=True)

        user_id = str(interaction.user.id)
        if user_id == session["player1_id"]:
            session["player1_approved"] = True
        else:
            session["player2_approved"] = True
        store.save_data()

        if session.get("player1_approved") and session.get("player2_approved"):
            await process_session_verification(client, config.ELO_LOG_CHANNEL_ID.id, self.session_id)
            await update_session_channel_embed(client, self.session_id)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
            await interaction.response.send_message(
                "✅ Both players approved! Session verified and Elo ratings have been updated. Check your DMs for the match report.",
                ephemeral=True,
            )
        else:
            other_id = session["player2_id"] if user_id == session["player1_id"] else session["player1_id"]
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
            await interaction.response.send_message(
                f"✅ You approved the results! Waiting for <@{other_id}> to approve.",
                ephemeral=True,
            )

    @discord.ui.button(label="Deny Results", style=discord.ButtonStyle.red, emoji="❌")
    async def deny_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        client = interaction.client
        session = store.storage["elo_sessions"].get(self.session_id)
        if not session:
            return await interaction.response.send_message("❌ Session not found!", ephemeral=True)
        if session["status"] != "review":
            return await interaction.response.send_message(f"❌ Session is not in review.", ephemeral=True)

        session["status"] = "active"
        session["player1_approved"] = False
        session["player2_approved"] = False
        store.save_data()

        user_id = str(interaction.user.id)
        other_id = session["player2_id"] if user_id == session["player1_id"] else session["player1_id"]

        try:
            other_user = await client.fetch_user(int(other_id))
            await other_user.send(
                f"⚠️ <@{user_id}> denied the results for session **{self.session_id}**.\n"
                "The session is now active again. Please report corrected scores."
            )
        except Exception:
            pass

        is_player1 = user_id == session["player1_id"]
        await send_session_dm_to_player(client, self.session_id, user_id, is_player1)
        await update_session_channel_embed(client, self.session_id)

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            "❌ Results denied. Session is now active again. Check your new DM to make changes.",
            ephemeral=True,
        )


class EloSessionView(discord.ui.View):
    """Lightweight channel-side view — just sends a DM to the requesting player."""

    def __init__(self, session_id: str):
        super().__init__(timeout=None)
        self.session_id = session_id

    @discord.ui.button(label="View in DMs", style=discord.ButtonStyle.gray, emoji="📬", custom_id="elo:view_dm")
    async def view_dm_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        client = interaction.client
        session = store.storage["elo_sessions"].get(self.session_id)
        if not session:
            return await interaction.response.send_message("❌ Session not found!", ephemeral=True)

        user_id = str(interaction.user.id)
        if user_id not in (session["player1_id"], session["player2_id"]):
            return await interaction.response.send_message("❌ You are not part of this session!", ephemeral=True)

        is_player1 = user_id == session["player1_id"]
        try:
            if session["status"] == "active":
                await send_session_dm_to_player(client, self.session_id, user_id, is_player1)
                await interaction.response.send_message("✅ Check your DMs!", ephemeral=True)
            elif session["status"] == "review":
                await send_review_dm(client, self.session_id)
                await interaction.response.send_message("✅ Check your DMs for the review!", ephemeral=True)
            else:
                await interaction.response.send_message(f"Session status: {session['status']}", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I couldn't send you a DM! Please enable DMs from server members.", ephemeral=True
            )


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class ReportMatchesDMModal(discord.ui.Modal):
    def __init__(self, session_id: str, is_player1: bool, match_type: str = "bo5"):
        super().__init__(title="Report Bo5 Match" if match_type == "bo5" else "Report Matches")
        self.session_id = session_id
        self.is_player1 = is_player1
        self.scores_input = discord.ui.TextInput(
            label="Match Scores (Your score first)",
            style=discord.TextStyle.paragraph,
            placeholder="Format: YOUR_SCORE-OPPONENT_SCORE\nExample: 5-4 3-5 5-2\nMax 5 per player, first to 5 wins",
            required=True,
            max_length=500,
        )
        self.add_item(self.scores_input)

    async def on_submit(self, interaction: discord.Interaction):
        client = interaction.client
        try:
            raw = parse_match_scores(self.scores_input.value)
            matches = raw if self.is_player1 else [{"player1_score": m["player2_score"], "player2_score": m["player1_score"]} for m in raw]
            store.storage["elo_sessions"][self.session_id]["matches"].extend(matches)
            store.save_data()
            await update_session_dm_embeds(client, self.session_id)
            await update_session_channel_embed(client, self.session_id)
            await interaction.response.send_message(f"✅ Reported {len(matches)} match(es) successfully!", ephemeral=True)
        except ValueError as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Unexpected error: {e}", ephemeral=True)


class EditScoresModal(discord.ui.Modal):
    def __init__(self, session_id: str, is_player1: bool):
        super().__init__(title="Edit All Scores")
        self.session_id = session_id
        self.is_player1 = is_player1

        session = store.storage["elo_sessions"].get(session_id, {})
        current = " ".join(
            f"{m['player1_score']}-{m['player2_score']}" if is_player1 else f"{m['player2_score']}-{m['player1_score']}"
            for m in session.get("matches", [])
        )
        self.scores_input = discord.ui.TextInput(
            label="All Match Scores (Your score first)",
            style=discord.TextStyle.paragraph,
            placeholder="Replace ALL scores. Format: YOUR_SCORE-OPPONENT_SCORE",
            default=current,
            required=True,
            max_length=500,
        )
        self.add_item(self.scores_input)

    async def on_submit(self, interaction: discord.Interaction):
        client = interaction.client
        try:
            raw = parse_match_scores(self.scores_input.value)
            matches = raw if self.is_player1 else [{"player1_score": m["player2_score"], "player2_score": m["player1_score"]} for m in raw]
            session = store.storage["elo_sessions"][self.session_id]
            session["matches"] = matches
            session["player1_approved"] = False
            session["player2_approved"] = False
            store.save_data()
            await send_review_dm(client, self.session_id)
            await update_session_channel_embed(client, self.session_id)
            await interaction.response.send_message("✅ Scores updated! Both players need to re-approve.", ephemeral=True)
        except ValueError as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Unexpected error: {e}", ephemeral=True)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class EloCog(commands.Cog):
    def __init__(self, bot: commands.Bot, guild_id: int, elo_log_channel_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.elo_log_channel_id = elo_log_channel_id

        self.elo_group = app_commands.Group(
            name="elo",
            description="Elo rating system commands",
            guild_ids=[guild_id],
        )
        self._register_commands()
        bot.tree.add_command(self.elo_group)

    @tasks.loop(hours=24)
    async def cleanup_old_sessions(self):
        now = datetime.datetime.now().timestamp()
        cutoff = now - 7 * 24 * 60 * 60
        to_deny = [
            sid for sid, s in store.storage.get("elo_sessions", {}).items()
            if s.get("status") in ("pending", "review", "active")
            and float(s.get("created_at", now)) <= cutoff
        ]
        if to_deny:
            log_channel = self.bot.get_channel(self.elo_log_channel_id)
            for sid in to_deny:
                s = store.storage["elo_sessions"][sid]
                s["status"] = "denied"
                if log_channel:
                    await log_channel.send(
                        f"⏱️ Session {sid} auto-denied (pending > 7 days): "
                        f"<@{s['player1_id']}> vs <@{s['player2_id']}>"
                    )
            store.save_data()
            print(f"Auto-denied {len(to_deny)} old sessions")

    def _register_commands(self):
        group = self.elo_group

        @group.command(name="register", description="Register for the Elo rating system")
        @app_commands.describe(elo_type="The type of Elo to register for")
        @app_commands.autocomplete(elo_type=elo_type_autocomplete)
        async def elo_register(interaction: discord.Interaction, elo_type: str):
            user_id = str(interaction.user.id)
            ok, result = validate_elo_type(elo_type)
            if not ok:
                return await interaction.response.send_message(f"❌ {result}", ephemeral=True)
            elo_type = result

            store.storage["elo_players"].setdefault(user_id, {})
            if elo_type in store.storage["elo_players"][user_id]:
                return await interaction.response.send_message(f"❌ You're already registered for **{elo_type}** Elo!", ephemeral=True)

            store.storage["elo_players"][user_id][elo_type] = {
                "discord_id": user_id,
                "rating": 1200.0,
                "matches_played": 0,
                "wins": 0,
                "losses": 0,
                "peak_rating": 1200.0,
                "created_at": str(datetime.datetime.now().timestamp()),
            }
            store.save_data()

            display_name = store.storage["elo_types"][elo_type]["display_name"]
            await log_elo_event(self.bot, self.elo_log_channel_id, "registered", elo_type, user_id=user_id)
            await interaction.response.send_message(f"✅ Registered for **{display_name}** Elo! Starting rating: **1200**", ephemeral=True)

        @group.command(name="session", description="Create an Elo session with an opponent")
        @app_commands.describe(opponent="The opponent to play against", elo_type="The type of Elo")
        @app_commands.autocomplete(elo_type=elo_type_autocomplete)
        async def elo_session(interaction: discord.Interaction, opponent: discord.Member, elo_type: str):
            user_id = str(interaction.user.id)
            opp_id = str(opponent.id)
            ok, result = validate_elo_type(elo_type)
            if not ok:
                return await interaction.response.send_message(f"❌ {result}", ephemeral=True)
            elo_type = result
            display_name = store.storage["elo_types"][elo_type]["display_name"]

            if elo_type not in store.storage.get("elo_players", {}).get(user_id, {}):
                return await interaction.response.send_message(f"❌ You must register for **{display_name}** Elo first! Use `/elo register elo_type:{elo_type}`", ephemeral=True)
            if elo_type not in store.storage.get("elo_players", {}).get(opp_id, {}):
                return await interaction.response.send_message(f"❌ {opponent.mention} must register for **{display_name}** Elo first!", ephemeral=True)
            if user_id == opp_id:
                return await interaction.response.send_message("❌ You can't create a session with yourself!", ephemeral=True)

            session_id = f"ELO-{store.next_id()}"
            store.storage["elo_sessions"][session_id] = {
                "session_id": session_id,
                "player1_id": user_id,
                "player2_id": opp_id,
                "elo_type": elo_type,
                "matches": [],
                "status": "active",
                "created_at": str(datetime.datetime.now().timestamp()),
                "verified_at": None,
                "message_id": None,
                "channel_id": str(interaction.channel.id),
            }

            embed = create_session_embed(session_id)
            msg = await interaction.channel.send(
                content=f"{interaction.user.mention} vs {opponent.mention}",
                embed=embed,
                view=EloSessionView(session_id),
            )
            store.storage["elo_sessions"][session_id]["message_id"] = str(msg.id)
            store.save_data()

            await log_elo_event(self.bot, self.elo_log_channel_id, "session_created", elo_type, session_id=session_id, player1_id=user_id, player2_id=opp_id)

            failures = []
            if not await send_session_dm_to_player(self.bot, session_id, user_id, True):
                failures.append(interaction.user.mention)
            if not await send_session_dm_to_player(self.bot, session_id, opp_id, False):
                failures.append(opponent.mention)

            msg_text = f"✅ Session created: **{session_id}**"
            if failures:
                msg_text += f"\n\n⚠️ **Could not send DM to:** {', '.join(failures)}\nPlease enable DMs from server members!"
            else:
                msg_text += "\nBoth players have been notified via DM!"
            await interaction.response.send_message(msg_text, ephemeral=True)

        @group.command(name="stats", description="View your or another player's Elo stats")
        @app_commands.describe(player="The player to check (leave blank for yourself)", elo_type="The type of Elo")
        @app_commands.autocomplete(elo_type=elo_type_autocomplete)
        async def elo_stats(interaction: discord.Interaction, player: discord.Member = None, elo_type: str = None):
            target = player or interaction.user
            tid = str(target.id)

            if tid not in store.storage.get("elo_players", {}):
                return await interaction.response.send_message(f"❌ {target.mention} is not registered for any Elo type!", ephemeral=True)

            if elo_type is None:
                user_elos = store.storage["elo_players"][tid]
                embed = discord.Embed(title=f"📊 All Elo Stats: {target.display_name}", color=discord.Color.blue(), timestamp=datetime.datetime.now())
                embed.set_thumbnail(url=target.display_avatar.url)
                for eid, data in user_elos.items():
                    dname = store.storage.get("elo_types", {}).get(eid, {}).get("display_name", eid.title())
                    wr = (data["wins"] / data["matches_played"] * 100) if data["matches_played"] > 0 else 0.0
                    embed.add_field(name=f"**{dname}**", value=f"Rating: **{data['rating']:.0f}** (Peak: {data['peak_rating']:.0f})\n{data['wins']}W-{data['losses']}L ({wr:.1f}%) | {data['matches_played']} matches", inline=False)
                return await interaction.response.send_message(embed=embed)

            ok, result = validate_elo_type(elo_type)
            if not ok:
                return await interaction.response.send_message(f"❌ {result}", ephemeral=True)
            elo_type = result
            display_name = store.storage["elo_types"][elo_type]["display_name"]

            if elo_type not in store.storage["elo_players"].get(tid, {}):
                return await interaction.response.send_message(f"❌ {target.mention} is not registered for **{display_name}** Elo!", ephemeral=True)

            data = store.storage["elo_players"][tid][elo_type]
            wr = (data["wins"] / data["matches_played"] * 100) if data["matches_played"] > 0 else 0.0
            embed = discord.Embed(title=f"📊 Elo Stats: {target.display_name}", description=f"**{display_name}** Elo", color=discord.Color.blue(), timestamp=datetime.datetime.now())
            embed.add_field(name="Current Rating", value=f"**{data['rating']:.0f}**", inline=True)
            embed.add_field(name="Peak Rating", value=f"{data['peak_rating']:.0f}", inline=True)
            embed.add_field(name="Matches Played", value=str(data["matches_played"]), inline=True)
            embed.add_field(name="Record", value=f"{data['wins']}W - {data['losses']}L ({wr:.1f}%)", inline=False)
            embed.set_thumbnail(url=target.display_avatar.url)
            await interaction.response.send_message(embed=embed)

        @group.command(name="leaderboard", description="View the Elo leaderboard")
        @app_commands.describe(elo_type="The type of Elo")
        @app_commands.autocomplete(elo_type=elo_type_autocomplete)
        async def elo_leaderboard(interaction: discord.Interaction, elo_type: str):
            await interaction.response.defer()
            ok, result = validate_elo_type(elo_type)
            if not ok:
                return await interaction.followup.send(f"❌ {result}", ephemeral=True)
            elo_type = result
            display_name = store.storage["elo_types"][elo_type]["display_name"]

            filtered = {uid: d[elo_type] for uid, d in store.storage.get("elo_players", {}).items() if elo_type in d}
            embed = discord.Embed(title="🏆 Elo Leaderboard", color=discord.Color.gold(), timestamp=datetime.datetime.now())

            if not filtered:
                embed.description = f"No players registered for **{display_name}** Elo yet!"
                return await interaction.followup.send(embed=embed)

            sorted_players = sorted(filtered.items(), key=lambda x: x[1]["rating"], reverse=True)
            text = ""
            for i, (uid, data) in enumerate(sorted_players[:10], 1):
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"**{i}.**")
                wr = (data["wins"] / data["matches_played"] * 100) if data["matches_played"] > 0 else 0.0
                text += f"{medal} <@{uid}> — **{data['rating']:.0f}** Elo | {data['wins']}W-{data['losses']}L ({wr:.1f}%)\n"

            embed.description = f"Top ranked players - **{display_name}** Elo\n\n{text}"
            embed.set_footer(text="Use /elo stats to view detailed statistics")
            await interaction.followup.send(embed=embed)

        @group.command(name="force_match", description="[ADMIN] Force a match result without verification")
        @app_commands.describe(player1="First player", player2="Second player", scores="Match scores (e.g., 5-4 3-6 5-3)", elo_type="The type of Elo", silent="Don't send match reports or log (default: False)")
        @app_commands.autocomplete(elo_type=elo_type_autocomplete)
        @app_commands.checks.has_permissions(administrator=True)
        async def elo_force_match(interaction: discord.Interaction, player1: discord.Member, player2: discord.Member, scores: str, elo_type: str, silent: bool = False):
            await interaction.response.defer(ephemeral=True)
            ok, result = validate_elo_type(elo_type)
            if not ok:
                return await interaction.followup.send(f"❌ {result}", ephemeral=True)
            elo_type = result
            display_name = store.storage["elo_types"][elo_type]["display_name"]
            p1_id, p2_id = str(player1.id), str(player2.id)

            if elo_type not in store.storage.get("elo_players", {}).get(p1_id, {}):
                return await interaction.followup.send(f"❌ {player1.mention} is not registered for **{display_name}** Elo!", ephemeral=True)
            if elo_type not in store.storage.get("elo_players", {}).get(p2_id, {}):
                return await interaction.followup.send(f"❌ {player2.mention} is not registered for **{display_name}** Elo!", ephemeral=True)

            try:
                matches = parse_match_scores(scores)
                session_id = f"ADMIN-{store.next_id()}"
                store.storage["elo_sessions"][session_id] = {
                    "session_id": session_id,
                    "player1_id": p1_id,
                    "player2_id": p2_id,
                    "elo_type": elo_type,
                    "matches": matches,
                    "status": "verified",
                    "created_at": str(datetime.datetime.now().timestamp()),
                    "verified_at": str(datetime.datetime.now().timestamp()),
                    "message_id": None,
                }
                store.save_data()
                await process_session_verification(self.bot, self.elo_log_channel_id, session_id, silent=silent)
                await interaction.followup.send(
                    f"✅ Force matched {len(matches)} game(s) between {player1.mention} and {player2.mention}" + (" (silent mode)" if silent else ""),
                    ephemeral=True,
                )
            except ValueError as e:
                await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

        @group.command(name="create_type", description="[ADMIN] Create a new Elo type")
        @app_commands.describe(elo_id="ID (lowercase, no spaces, e.g. 'ranked')", display_name="Display name (e.g. 'Ranked 1v1')", match_type="bo5 = Best of 5 with quick buttons, standard = manual entry")
        @app_commands.choices(match_type=[
            app_commands.Choice(name="Best of 5 (Bo5)", value="bo5"),
            app_commands.Choice(name="Standard", value="standard"),
        ])
        @app_commands.checks.has_permissions(administrator=True)
        async def elo_create_type(interaction: discord.Interaction, elo_id: str, display_name: str, match_type: str = "bo5"):
            elo_id = elo_id.lower().strip()
            display_name = display_name.strip()
            if not elo_id or not display_name:
                return await interaction.response.send_message("❌ Both ID and display name are required!", ephemeral=True)
            if " " in elo_id:
                return await interaction.response.send_message("❌ Elo ID cannot contain spaces!", ephemeral=True)
            if not elo_id.replace("_", "").replace("-", "").isalnum():
                return await interaction.response.send_message("❌ Elo ID can only contain letters, numbers, underscores, and hyphens!", ephemeral=True)
            if elo_id in store.storage.get("elo_types", {}):
                return await interaction.response.send_message(f"❌ Elo type `{elo_id}` already exists!", ephemeral=True)

            store.storage.setdefault("elo_types", {})[elo_id] = {
                "display_name": display_name,
                "match_type": match_type,
                "created_at": str(datetime.datetime.now().timestamp()),
                "created_by": str(interaction.user.id),
            }
            store.save_data()
            await log_elo_event(self.bot, self.elo_log_channel_id, "type_created", elo_id, elo_id=elo_id, display_name=display_name, match_type=match_type, created_by=str(interaction.user.id))

            mt_display = "Best of 5 (quick buttons)" if match_type == "bo5" else "Standard (manual entry)"
            embed = discord.Embed(title="✅ Elo Type Created", color=discord.Color.green())
            embed.add_field(name="ID", value=f"`{elo_id}`", inline=True)
            embed.add_field(name="Display Name", value=f"**{display_name}**", inline=True)
            embed.add_field(name="Match Type", value=mt_display, inline=True)
            embed.add_field(name="Usage", value=f"Players can now use:\n`/elo register elo_type:{elo_id}`", inline=False)
            await interaction.response.send_message(embed=embed)

        @group.command(name="list_types", description="List all Elo types in the system")
        async def elo_list_types(interaction: discord.Interaction):
            elo_types = store.storage.get("elo_types", {})
            if not elo_types:
                return await interaction.response.send_message("❌ No Elo types found.", ephemeral=True)

            embed = discord.Embed(title="📋 Available Elo Types", description="All Elo rating types in the system:", color=discord.Color.blue())
            players = store.storage.get("elo_players", {})
            for eid, edata in sorted(elo_types.items()):
                count = sum(1 for ud in players.values() if eid in ud)
                embed.add_field(
                    name=f"**{edata['display_name']}** ({edata.get('match_type', 'bo5').upper()})",
                    value=f"ID: `{eid}`\n{count} player(s) registered",
                    inline=True,
                )
            await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot, guild_id: int, elo_log_channel_id: int):
    cog = EloCog(bot, guild_id, elo_log_channel_id)
    await bot.add_cog(cog)
    return cog

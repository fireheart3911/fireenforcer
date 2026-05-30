import os
import discord
import dotenv

dotenv.load_dotenv()

GUILD_ID               = int(os.getenv("GUILD_ID"))
TICKET_LOG_CHANNEL_ID  = discord.Object(id=int(os.getenv("TICKET_LOG_CHANNEL_ID")))
STATUS_CHANNEL_ID      = discord.Object(id=int(os.getenv("STATUS_CHANNEL_ID")))
STATUS_LOG_CHANNEL_ID  = discord.Object(id=int(os.getenv("STATUS_LOG_CHANNEL_ID")))
ELO_LOG_CHANNEL_ID     = discord.Object(id=int(os.getenv("ELO_LOG_CHANNEL_ID")))
HEARTBEAT_URL          = os.getenv("HEARTBEAT_URL")
SUPPORT_ROLE_ID        = os.getenv("SUPPORT_ROLE_ID")
MODERATION_ROLE_ID     = os.getenv("MODERATION_ROLE_ID")
ADMINISTRATION_ROLE_ID = os.getenv("ADMINISTRATION_ROLE_ID")

GUILD_LIST = [discord.Object(id=GUILD_ID)]

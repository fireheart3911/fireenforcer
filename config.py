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

# Optional: role pinged when someone calls a queue stop. Leave unset to disable pings.
_qs_role = os.getenv("QUEUE_STOP_PING_ROLE_ID")
QUEUE_STOP_PING_ROLE_ID = int(_qs_role) if _qs_role else None

# ---- Council / voting module ----
VOTE_CHANNEL_ID         = int(os.getenv("VOTE_CHANNEL_ID"))
OWNER_CHANNEL_ID        = int(os.getenv("OWNER_CHANNEL_ID"))
COUNCIL_LOG_CHANNEL_ID  = int(os.getenv("COUNCIL_LOG_CHANNEL_ID"))

GUEST_ROLE_ID   = int(os.getenv("GUEST_ROLE_ID"))
MEMBER_ROLE_ID  = int(os.getenv("MEMBER_ROLE_ID"))
VIP_ROLE_ID     = int(os.getenv("VIP_ROLE_ID"))
COUNCIL_ROLE_ID = int(os.getenv("COUNCIL_ROLE_ID"))
OWNER_ROLE_ID   = int(os.getenv("OWNER_ROLE_ID"))

# Level eligibility thresholds (global level required; server level must be ratio * global)
MEMBER_GLOBAL_LEVEL = int(os.getenv("MEMBER_GLOBAL_LEVEL", "50"))
VIP_GLOBAL_LEVEL    = int(os.getenv("VIP_GLOBAL_LEVEL", "150"))
SERVER_LEVEL_RATIO  = float(os.getenv("SERVER_LEVEL_RATIO", "0.85"))

# XP API
XP_API_BASE  = os.getenv("XP_API_BASE")
XP_API_TOKEN = os.getenv("XP_API_TOKEN")

GUILD_LIST = [discord.Object(id=GUILD_ID)]
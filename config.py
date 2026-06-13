"""
Per-instance configuration loader.

Two sources, both gitignored and uploaded manually per bot:
  • .env         — secrets + scalar IDs (TOKEN, GUILD_ID, channel/role IDs, …)
  • config.json  — structured, per-instance settings (modules, ticket
                   categories, presence, prefix)

If config.json is ABSENT, we fall back to "bot 1" defaults so the original
instance keeps behaving exactly as before the multi-instance refactor (it can
pull this code and run unchanged until a config.json is uploaded).

Module-specific env vars are only required when that module is enabled, so an
instance that doesn't run (e.g.) the council module never needs its env vars.
"""

import json
import os

import discord
import dotenv

dotenv.load_dotenv()


# ---------------------------------------------------------------------------
# Small env helpers
# ---------------------------------------------------------------------------

def _req_int(name: str) -> int:
    """Required int env var — raises a clear error if missing/blank."""
    val = os.getenv(name)
    if val is None or val.strip() == "":
        raise RuntimeError(f"Required environment variable {name} is not set (needed by an enabled module).")
    return int(val)

def _opt_int(name: str, default=None):
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default

def _opt_str(name: str, default=None):
    val = os.getenv(name)
    return val if val not in (None, "") else default


# ---------------------------------------------------------------------------
# Load config.json (or fall back to bot-1 defaults)
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    # Fallback
    "modules": ["tickets"],
    "prefix": "sudo",
    "presence": {"mode": "fixed", "text": "🛰️ Running v1.4.8"},
    "ticket_categories": [
        {
            "key": "support", "label": "Support", "emoji": "🛂", "style": "success",
            "role_id": None, "role_env": "SUPPORT_ROLE_ID",
            "description": ("General help and everyday questions: how things work, event or "
                            "role requests, reporting a minor issue, or anything that doesn't "
                            "fit the categories above."),
        },
    ],
}

CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")

def _load_json_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"Loaded instance config from {CONFIG_PATH}")
        return data
    except FileNotFoundError:
        print(f"No {CONFIG_PATH} found — using built-in defaults (bot-1 behaviour).")
        return dict(_DEFAULT_CONFIG)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{CONFIG_PATH} is not valid JSON: {e}")

_cfg = _load_json_config()


# ---------------------------------------------------------------------------
# Universal settings (every instance)
# ---------------------------------------------------------------------------

MODULES   = list(_cfg.get("modules", _DEFAULT_CONFIG["modules"]))
PREFIX    = _cfg.get("prefix", _DEFAULT_CONFIG["prefix"])
PRESENCE  = _cfg.get("presence", _DEFAULT_CONFIG["presence"])

GUILD_ID  = _req_int("GUILD_ID")
GUILD_LIST = [discord.Object(id=GUILD_ID)]
HEARTBEAT_URL = _opt_str("HEARTBEAT_URL")

def module_enabled(name: str) -> bool:
    return name in MODULES


# ---------------------------------------------------------------------------
# Ticket categories (resolved: role_id taken directly, or from role_env)
# ---------------------------------------------------------------------------

def _resolve_ticket_categories():
    cats = {}
    for c in _cfg.get("ticket_categories", _DEFAULT_CONFIG["ticket_categories"]):
        role_id = c.get("role_id")
        if role_id in (None, "") and c.get("role_env"):
            role_id = _opt_int(c["role_env"])
        cats[c["key"]] = {
            "label": c["label"],
            "emoji": c.get("emoji"),
            "style": c.get("style", "secondary"),
            "role_id": int(role_id) if role_id not in (None, "") else None,
            "description": c.get("description", ""),
        }
    return cats

TICKET_CATEGORIES = _resolve_ticket_categories()

# Tickets module always needs its log channel (tickets is enabled on both bots).
TICKET_LOG_CHANNEL_ID = discord.Object(id=_req_int("TICKET_LOG_CHANNEL_ID")) if module_enabled("tickets") else None


# ---------------------------------------------------------------------------
# Status module
# ---------------------------------------------------------------------------

if module_enabled("status"):
    STATUS_CHANNEL_ID     = discord.Object(id=_req_int("STATUS_CHANNEL_ID"))
    STATUS_LOG_CHANNEL_ID = discord.Object(id=_req_int("STATUS_LOG_CHANNEL_ID"))
    _qs_role = _opt_int("QUEUE_STOP_PING_ROLE_ID")
    QUEUE_STOP_PING_ROLE_ID = _qs_role
else:
    STATUS_CHANNEL_ID = STATUS_LOG_CHANNEL_ID = None
    QUEUE_STOP_PING_ROLE_ID = None


# ---------------------------------------------------------------------------
# Elo module
# ---------------------------------------------------------------------------

if module_enabled("elo"):
    ELO_LOG_CHANNEL_ID = discord.Object(id=_req_int("ELO_LOG_CHANNEL_ID"))
else:
    ELO_LOG_CHANNEL_ID = None


# ---------------------------------------------------------------------------
# Shared: XP API (used by council promotions and signup application mode)
# ---------------------------------------------------------------------------
# Optional regardless of module — features that need it degrade gracefully when
# the token is absent rather than failing at config load.
XP_API_BASE  = _opt_str("XP_API_BASE")
XP_API_TOKEN = _opt_str("XP_API_TOKEN")


# ---------------------------------------------------------------------------
# Council module
# ---------------------------------------------------------------------------

if module_enabled("council"):
    VOTE_CHANNEL_ID        = _req_int("VOTE_CHANNEL_ID")
    OWNER_CHANNEL_ID       = _req_int("OWNER_CHANNEL_ID")
    COUNCIL_LOG_CHANNEL_ID = _req_int("COUNCIL_LOG_CHANNEL_ID")
    GUEST_ROLE_ID   = _req_int("GUEST_ROLE_ID")
    MEMBER_ROLE_ID  = _req_int("MEMBER_ROLE_ID")
    VIP_ROLE_ID     = _req_int("VIP_ROLE_ID")
    COUNCIL_ROLE_ID = _req_int("COUNCIL_ROLE_ID")
    OWNER_ROLE_ID   = _req_int("OWNER_ROLE_ID")
    MEMBER_GLOBAL_LEVEL = _opt_int("MEMBER_GLOBAL_LEVEL", 50)
    VIP_GLOBAL_LEVEL    = _opt_int("VIP_GLOBAL_LEVEL", 150)
    SERVER_LEVEL_RATIO  = float(_opt_str("SERVER_LEVEL_RATIO", "0.85"))
else:
    VOTE_CHANNEL_ID = OWNER_CHANNEL_ID = COUNCIL_LOG_CHANNEL_ID = None
    GUEST_ROLE_ID = MEMBER_ROLE_ID = VIP_ROLE_ID = COUNCIL_ROLE_ID = OWNER_ROLE_ID = None
    MEMBER_GLOBAL_LEVEL = VIP_GLOBAL_LEVEL = None
    SERVER_LEVEL_RATIO = None


# ---------------------------------------------------------------------------
# Signup module
# ---------------------------------------------------------------------------

if module_enabled("signup"):
    SIGNUP_ROLE_ID        = _req_int("SIGNUP_ROLE_ID")
    SIGNUP_LOG_CHANNEL_ID = _req_int("SIGNUP_LOG_CHANNEL_ID")
    # Channel applications are posted to (only needed if you use application mode).
    SIGNUP_APPLICATION_CHANNEL_ID = _opt_int("SIGNUP_APPLICATION_CHANNEL_ID")
else:
    SIGNUP_ROLE_ID = SIGNUP_LOG_CHANNEL_ID = SIGNUP_APPLICATION_CHANNEL_ID = None


# ---------------------------------------------------------------------------
# Events module
# ---------------------------------------------------------------------------
# Events post their own panels in whatever channel they're created in, so no
# channel is required. The log channel is optional (reserved for future use).

if module_enabled("events"):
    EVENTS_LOG_CHANNEL_ID = _opt_int("EVENTS_LOG_CHANNEL_ID")
else:
    EVENTS_LOG_CHANNEL_ID = None
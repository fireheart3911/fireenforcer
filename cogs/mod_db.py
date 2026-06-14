"""
Shared moderation database (Pterodactyl MariaDB/MySQL).

Source of truth for the cross-instance global banlist and fairplay flags. Each
instance keeps a local mirror (storage["moderation"]) for fast/offline reads; this
module is the only thing that talks to the DB.

aiomysql is imported lazily inside init_pool() so this module — and anything that
imports it (e.g. cogs.moderation, and through it signup/events) — stays importable
on instances that don't run moderation or don't have the dependency installed.
"""

import time

import config

_pool = None
_aiomysql = None


_DDL = (
    """
    CREATE TABLE IF NOT EXISTS global_bans (
        user_id        BIGINT       NOT NULL PRIMARY KEY,
        reason         TEXT,
        violated_rules TEXT,
        moderator_id   BIGINT,
        created_at     BIGINT,
        expires_at     BIGINT       NULL,
        scope          VARCHAR(10)  NOT NULL DEFAULT 'global',
        source_guild   BIGINT,
        active         TINYINT      NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mod_flags (
        id             BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id        BIGINT       NOT NULL,
        type           VARCHAR(40),
        reason         TEXT,
        blocks         TINYINT      NOT NULL DEFAULT 0,
        moderator_id   BIGINT,
        created_at     BIGINT,
        expires_at     BIGINT       NULL,
        source_guild   BIGINT,
        active         TINYINT      NOT NULL DEFAULT 1,
        INDEX idx_flags_user (user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS applied_bans (
        user_id    BIGINT NOT NULL,
        guild_id   BIGINT NOT NULL,
        applied_at BIGINT,
        PRIMARY KEY (user_id, guild_id)
    )
    """,
)


def _now() -> int:
    return int(time.time())


def is_ready() -> bool:
    return _pool is not None


async def init_pool() -> bool:
    """Open the pool and ensure the schema. Returns True on success, False when the
    DB isn't configured. Raises only on an actual connection/auth failure."""
    global _pool, _aiomysql
    if _pool is not None:
        return True
    if not (config.MOD_DB_HOST and config.MOD_DB_USER and config.MOD_DB_NAME):
        return False
    import aiomysql  # lazy — only instances running moderation need the dependency
    _aiomysql = aiomysql
    _pool = await aiomysql.create_pool(
        host=config.MOD_DB_HOST, port=config.MOD_DB_PORT or 3306,
        user=config.MOD_DB_USER, password=config.MOD_DB_PASSWORD or "",
        db=config.MOD_DB_NAME, autocommit=True, minsize=1, maxsize=5,
        pool_recycle=300,
    )
    await _ensure_schema()
    return True


async def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


async def _execute(query, args=(), *, fetch=None):
    if _pool is None:
        raise RuntimeError("mod_db pool is not initialised")
    async with _pool.acquire() as conn:
        async with conn.cursor(_aiomysql.DictCursor) as cur:
            await cur.execute(query, args)
            if fetch == "one":
                return await cur.fetchone()
            if fetch == "all":
                return await cur.fetchall()
            return cur.lastrowid


async def _ensure_schema():
    for ddl in _DDL:
        await _execute(ddl)


# ---------------------------------------------------------------------------
# Bans
# ---------------------------------------------------------------------------

async def add_ban(user_id, reason, violated_rules, moderator_id,
                  expires_at, scope, source_guild):
    """Insert or re-activate a ban (user_id is the PK — re-banning overwrites)."""
    await _execute(
        """
        INSERT INTO global_bans
            (user_id, reason, violated_rules, moderator_id, created_at,
             expires_at, scope, source_guild, active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
        ON DUPLICATE KEY UPDATE
            reason=VALUES(reason), violated_rules=VALUES(violated_rules),
            moderator_id=VALUES(moderator_id), created_at=VALUES(created_at),
            expires_at=VALUES(expires_at), scope=VALUES(scope),
            source_guild=VALUES(source_guild), active=1
        """,
        (int(user_id), reason, violated_rules,
         int(moderator_id) if moderator_id else None, _now(),
         int(expires_at) if expires_at else None, scope, int(source_guild)),
    )


async def deactivate_ban(user_id):
    await _execute("UPDATE global_bans SET active=0 WHERE user_id=%s", (int(user_id),))


async def get_ban(user_id):
    return await _execute(
        "SELECT * FROM global_bans WHERE user_id=%s AND active=1 "
        "AND (expires_at IS NULL OR expires_at > %s)",
        (int(user_id), _now()), fetch="one")


async def fetch_active_bans():
    return await _execute(
        "SELECT * FROM global_bans WHERE active=1 "
        "AND (expires_at IS NULL OR expires_at > %s)", (_now(),), fetch="all")


async def deactivate_expired_bans() -> int:
    """Flip expired tempbans inactive so they drop out of the active set."""
    return await _execute(
        "UPDATE global_bans SET active=0 WHERE active=1 "
        "AND expires_at IS NOT NULL AND expires_at <= %s", (_now(),)) or 0


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

async def add_flag(user_id, type_, reason, blocks, moderator_id,
                   expires_at, source_guild) -> int:
    return await _execute(
        """
        INSERT INTO mod_flags
            (user_id, type, reason, blocks, moderator_id, created_at,
             expires_at, source_guild, active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
        """,
        (int(user_id), type_, reason, 1 if blocks else 0,
         int(moderator_id) if moderator_id else None, _now(),
         int(expires_at) if expires_at else None, int(source_guild)))


async def deactivate_flag(flag_id) -> int:
    return await _execute(
        "UPDATE mod_flags SET active=0 WHERE id=%s AND active=1", (int(flag_id),)) or 0


async def get_flags(user_id):
    return await _execute(
        "SELECT * FROM mod_flags WHERE user_id=%s AND active=1 "
        "AND (expires_at IS NULL OR expires_at > %s) ORDER BY created_at",
        (int(user_id), _now()), fetch="all")


async def fetch_active_flags():
    return await _execute(
        "SELECT * FROM mod_flags WHERE active=1 "
        "AND (expires_at IS NULL OR expires_at > %s) ORDER BY created_at",
        (_now(),), fetch="all")


async def deactivate_expired_flags() -> int:
    return await _execute(
        "UPDATE mod_flags SET active=0 WHERE active=1 "
        "AND expires_at IS NOT NULL AND expires_at <= %s", (_now(),)) or 0


# ---------------------------------------------------------------------------
# Applied-ban tracking (so we only ever lift bans WE placed)
# ---------------------------------------------------------------------------

async def record_applied_ban(user_id, guild_id):
    await _execute(
        "INSERT IGNORE INTO applied_bans (user_id, guild_id, applied_at) "
        "VALUES (%s, %s, %s)", (int(user_id), int(guild_id), _now()))


async def remove_applied_ban(user_id, guild_id):
    await _execute("DELETE FROM applied_bans WHERE user_id=%s AND guild_id=%s",
                   (int(user_id), int(guild_id)))


async def applied_for_guild(guild_id) -> set:
    rows = await _execute("SELECT user_id FROM applied_bans WHERE guild_id=%s",
                          (int(guild_id),), fetch="all")
    return {int(r["user_id"]) for r in (rows or [])}

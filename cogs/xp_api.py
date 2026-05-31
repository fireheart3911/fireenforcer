"""
Thin async client for the level/XP API.

  GET {base}/{user_id}              -> global level
  GET {base}/{user_id}/{server_id}  -> server level

Both return {"level": int, "xp": int} with an Authorization header.

Status handling:
  200 -> data
  204 -> no data for this user            -> XPNotFound
  401 -> bad auth                          -> XPAuthError
  404 -> wrong path                        -> XPPathError
  other -> XPError
"""

import asyncio
import json
import math
import urllib.error
import urllib.request

import config


class XPError(Exception):
    """Generic XP API failure."""


class XPNotFound(XPError):
    """204 — the requested user/server data does not exist."""


class XPAuthError(XPError):
    """401 — wrong/missing authorization."""


class XPPathError(XPError):
    """404 — wrong API path."""


def _request(url: str) -> dict:
    """Blocking GET. Runs inside an executor via the async wrappers below."""
    req = urllib.request.Request(url, method="GET")
    if config.XP_API_TOKEN:
        req.add_header("Authorization", config.XP_API_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            if status == 204:
                raise XPNotFound("No level data for that user/server.")
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else {}
            return data
    except urllib.error.HTTPError as e:
        if e.code == 204:
            raise XPNotFound("No level data for that user/server.")
        if e.code == 401:
            raise XPAuthError("XP API rejected the authorization token.")
        if e.code == 404:
            raise XPPathError("XP API path not found.")
        raise XPError(f"XP API returned HTTP {e.code}.")
    except urllib.error.URLError as e:
        raise XPError(f"Could not reach XP API: {e.reason}")
    except json.JSONDecodeError:
        raise XPError("XP API returned malformed JSON.")


async def _request_async(url: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _request, url)


async def get_global_level(user_id: str | int) -> int:
    data = await _request_async(f"{config.XP_API_BASE}/{user_id}")
    return int(data.get("level", 0))


async def get_server_level(user_id: str | int, server_id: str | int) -> int:
    data = await _request_async(f"{config.XP_API_BASE}/{user_id}/{server_id}")
    return int(data.get("level", 0))


async def check_eligibility(user_id: str | int, server_id: str | int,
                            required_global: int, server_ratio: float) -> tuple[bool, str]:
    """Return (eligible, human_readable_detail).

    Eligible iff global level >= required_global AND
    server level >= ceil-ish(required_global * server_ratio).
    Raises XP* errors on API problems (caller decides how to surface).
    """
    # Half-up rounding (round(42.5) would give 42 via banker's rounding;
    # the spec example wants 50*0.85=42.5 -> 43).
    required_server = math.floor(required_global * server_ratio + 0.5)

    global_level = await get_global_level(user_id)
    server_level = await get_server_level(user_id, server_id)

    ok = global_level >= required_global and server_level >= required_server
    detail = (
        f"global **{global_level}**/{required_global}, "
        f"server **{server_level}**/{required_server}"
    )
    return ok, detail
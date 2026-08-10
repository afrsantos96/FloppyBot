"""
HTTP route handlers for the minister portal.
"""
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from . import auth
from . import db

logger = logging.getLogger('bot')

STATIC_DIR = Path(__file__).parent / "static"

# Chat apps auto-crawl plain-text URLs server-side to build a link preview --
# a request from one of these must not consume a single-use token, or the
# admin's own click finds it already "used" before they ever open it. This
# is defense-in-depth: the Discord button also avoids putting the raw URL in
# message text (which is what triggers Discord's own crawler) in the first
# place, but other platforms/relays this link gets forwarded through could
# still crawl it.
_LINK_PREVIEW_BOT_UA_MARKERS = (
    "discordbot", "facebookexternalhit", "slackbot", "telegrambot",
    "whatsapp", "twitterbot", "linkedinbot", "skypeuripreview", "embedly",
)


def _looks_like_link_preview_bot(request: web.Request) -> bool:
    ua = request.headers.get("User-Agent", "").lower()
    return any(marker in ua for marker in _LINK_PREVIEW_BOT_UA_MARKERS)


def _resolve_log_user(bot, discord_user_id: int, guild_id):
    """Best-effort discord.py user-like object for MinisterSchedule.log_change,
    which reads .id/.display_name. Falls back to a plain object carrying just
    those two attributes if the bot can't resolve a live Member/User (e.g.
    they left the server between issuing the link and saving)."""
    guild = bot.get_guild(guild_id) if guild_id else None
    member = guild.get_member(discord_user_id) if guild else None
    if member:
        return member
    user = bot.get_user(discord_user_id)
    if user:
        return user
    return SimpleNamespace(id=discord_user_id, display_name=f"Portal User {discord_user_id}")


async def redeem_token(request: web.Request) -> web.StreamResponse:
    """GET /portal/{token} -- consume a one-time magic link and start a session."""
    cfg = request.app["portal_config"]
    token = request.match_info["token"]

    payload = auth.verify_token(token, cfg.signing_secret, auth.MAGIC_LINK_MAX_AGE_SECONDS)
    if not payload or "jti" not in payload:
        return web.Response(
            text="This portal link is invalid or has expired. Generate a new one from Discord "
                 "(Minister Scheduling → Online Manage Portal).",
            status=400,
        )

    if _looks_like_link_preview_bot(request):
        # Don't burn the single use on an automated preview fetch -- reply
        # without touching portal_tokens so the admin's real click still works.
        return web.Response(text="Kingshot minister portal link.", status=200)

    consumed = await asyncio.to_thread(auth.consume_portal_token, payload["jti"])
    if not consumed:
        return web.Response(
            text="This portal link has already been used. Generate a new one from Discord.",
            status=400,
        )

    session_payload = auth.issue_session_payload(payload["discord_user_id"], payload["guild_id"])
    session_token = auth.sign_token(session_payload, cfg.signing_secret)

    response = web.HTTPFound("/portal/schedule")
    auth.set_session_cookie(response, session_token, secure=cfg.base_url.startswith("https://"))
    return response


async def portal_schedule_page(request: web.Request) -> web.StreamResponse:
    """GET /portal/schedule -- the static HTML shell, gated on a valid session."""
    if not request.get("session"):
        return web.Response(
            text="Your portal session has expired. Ask Discord for a new link "
                 "(Minister Scheduling → Online Manage Portal).",
            status=401,
        )
    return web.FileResponse(STATIC_DIR / "schedule.html")


async def portal_logout(request: web.Request) -> web.StreamResponse:
    response = web.Response(text="Logged out.")
    auth.clear_session_cookie(response)
    return response


async def api_get_schedule(request: web.Request) -> web.Response:
    """GET /api/schedule -- positions, slots, current appointments, and the
    caller's permission-scoped member list."""
    session = request.get("session")
    if not session:
        return web.json_response({"error": "unauthorized"}, status=401)

    snapshot = await asyncio.to_thread(
        db.fetch_schedule_snapshot, session["discord_user_id"], session["guild_id"]
    )
    if snapshot is None:
        return web.json_response({"error": "forbidden"}, status=403)
    return web.json_response(snapshot)


async def api_save_schedule(request: web.Request) -> web.Response:
    """POST /api/schedule -- apply a batch of slot assignments, then refresh
    the Discord board(s) for every appointment type that changed."""
    session = request.get("session")
    if not session:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    changes = body.get("changes")
    if not isinstance(changes, list) or not changes:
        return web.json_response({"error": "no_changes"}, status=400)
    if len(changes) > 200:
        return web.json_response({"error": "too_many_changes"}, status=400)

    result = await asyncio.to_thread(
        db.apply_schedule_changes, changes, session["discord_user_id"], session["guild_id"]
    )
    if "error" in result:
        status = 403 if result["error"] == "forbidden" else 500
        return web.json_response(result, status=status)

    bot = request.app["bot"]
    minister_schedule_cog = bot.get_cog("MinisterSchedule")
    minister_menu_cog = bot.get_cog("MinisterMenu")
    log_user = _resolve_log_user(bot, session["discord_user_id"], session["guild_id"])

    if minister_schedule_cog:
        for entry in result["applied"]:
            try:
                await minister_schedule_cog.log_change(
                    action_type=entry["action"],
                    user=log_user,
                    appointment_type=entry["appointment_type"],
                    fid=entry["fid"],
                    nickname=entry["nickname"],
                    old_time=entry["old_time"],
                    new_time=entry["time"] if entry["action"] != "remove" else None,
                    alliance_name=None,
                    additional_data='{"source": "portal"}',
                )
            except Exception:
                logger.exception("Failed to log a portal schedule change")
    else:
        logger.warning("MinisterSchedule cog not loaded; portal changes were saved but not logged")

    if minister_menu_cog:
        for appointment_type in result["touched_types"]:
            try:
                await minister_menu_cog.update_channel_message(appointment_type)
            except Exception:
                logger.exception(f"Failed to refresh the Discord board for {appointment_type}")
    else:
        logger.warning("MinisterMenu cog not loaded; portal changes were saved but the board was not refreshed")

    return web.json_response({
        "applied": len(result["applied"]),
        "conflicts": result["conflicts"],
    })

"""
Magic-link + session auth for the minister portal. Uses stdlib hmac/secrets
only (no itsdangerous) -- the token shape is simple enough that adding a
dependency isn't worth it.

Two token kinds share the same sign/verify machinery:
  - the magic link (short expiry, reusable until it expires -- not single-use,
    since chat-client link crawlers and page reloads made single-use links
    unreliable in practice)
  - the session cookie issued after opening a link (longer expiry, never
    appears in a URL/referrer/log line)

Discord identity is trusted because the token's discord_user_id is only ever
set server-side, at the moment the bot's own button callback fires -- by
then Discord's interaction signature verification has already proven who
clicked. No separate OAuth exchange is needed to "prove" it to the web layer.
"""
import base64
import hashlib
import hmac
import json
import time
import secrets as secrets_mod
from typing import Optional

from aiohttp import web

SESSION_COOKIE_NAME = "ks_portal_session"
SESSION_MAX_AGE_SECONDS = 2 * 60 * 60      # 2 hours
MAGIC_LINK_MAX_AGE_SECONDS = 5 * 60        # 5 minutes


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_token(payload: dict, secret: str) -> str:
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(sig)}"


def verify_token(token: str, secret: str, max_age_seconds: int) -> Optional[dict]:
    try:
        body, sig_b64 = token.split(".", 1)
    except ValueError:
        return None

    expected_sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    try:
        actual_sig = _b64decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected_sig, actual_sig):
        return None

    try:
        payload = json.loads(_b64decode(body))
    except Exception:
        return None

    now = time.time()
    if payload.get("exp", 0) < now:
        return None
    if payload.get("iat", now + 1) > now + 5:  # small clock-skew allowance
        return None

    return payload


def issue_magic_link_payload(discord_user_id: int, guild_id: int) -> dict:
    now = time.time()
    return {
        "discord_user_id": discord_user_id,
        "guild_id": guild_id,
        "jti": secrets_mod.token_urlsafe(16),
        "iat": now,
        "exp": now + MAGIC_LINK_MAX_AGE_SECONDS,
    }


def issue_session_payload(discord_user_id: int, guild_id: int) -> dict:
    now = time.time()
    return {
        "discord_user_id": discord_user_id,
        "guild_id": guild_id,
        "iat": now,
        "exp": now + SESSION_MAX_AGE_SECONDS,
    }




def set_session_cookie(response: web.Response, token: str, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=secure,
        samesite="Strict",
        path="/",
    )


def clear_session_cookie(response: web.Response) -> None:
    response.del_cookie(SESSION_COOKIE_NAME, path="/")


def get_session(request: web.Request) -> Optional[dict]:
    """Read+verify the session cookie. Returns the payload dict or None."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    secret = request.app["portal_config"].signing_secret
    return verify_token(token, secret, SESSION_MAX_AGE_SECONDS)


@web.middleware
async def session_middleware(request: web.Request, handler):
    """Attaches request['session'] when a valid cookie is present. Does NOT
    reject requests itself -- individual handlers decide what a missing
    session means (401 JSON for /api/*, a friendly page for /portal/*),
    since there's no password-login page to redirect an expired browser
    session to."""
    request["session"] = get_session(request)
    return await handler(request)

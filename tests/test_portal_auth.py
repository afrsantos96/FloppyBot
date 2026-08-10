"""
sign_token/verify_token -- the magic-link auth primitives the portal's whole
security model rests on. Links are reusable (not single-use) until their own
expiry, since chat-client link crawlers and page reloads made single-use
links unreliable in practice -- see web/auth.py's module docstring.
"""
import importlib
import time

auth = importlib.import_module("web.auth")


def test_sign_and_verify_roundtrip():
    token = auth.sign_token({"a": 1, "exp": time.time() + 60, "iat": time.time()}, "secret")
    payload = auth.verify_token(token, "secret", max_age_seconds=60)
    assert payload is not None
    assert payload["a"] == 1


def test_verify_rejects_tampered_signature():
    token = auth.sign_token({"a": 1, "exp": time.time() + 60, "iat": time.time()}, "secret")
    body, sig = token.split(".", 1)
    tampered = body + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B")
    assert auth.verify_token(tampered, "secret", max_age_seconds=60) is None


def test_verify_rejects_wrong_secret():
    token = auth.sign_token({"a": 1, "exp": time.time() + 60, "iat": time.time()}, "secret")
    assert auth.verify_token(token, "different-secret", max_age_seconds=60) is None


def test_verify_rejects_expired_token():
    payload = {"a": 1, "iat": time.time() - 120, "exp": time.time() - 60}
    token = auth.sign_token(payload, "secret")
    assert auth.verify_token(token, "secret", max_age_seconds=60) is None


def test_verify_rejects_malformed_token():
    assert auth.verify_token("not-a-real-token", "secret", max_age_seconds=60) is None
    assert auth.verify_token("", "secret", max_age_seconds=60) is None


def test_magic_link_payload_has_short_expiry():
    payload = auth.issue_magic_link_payload(discord_user_id=42, guild_id=99)
    assert payload["discord_user_id"] == 42
    assert payload["guild_id"] == 99
    assert "jti" in payload
    assert payload["exp"] - payload["iat"] == auth.MAGIC_LINK_MAX_AGE_SECONDS


def test_session_payload_has_longer_expiry():
    payload = auth.issue_session_payload(discord_user_id=42, guild_id=99)
    assert payload["exp"] - payload["iat"] == auth.SESSION_MAX_AGE_SECONDS
    assert auth.SESSION_MAX_AGE_SECONDS > auth.MAGIC_LINK_MAX_AGE_SECONDS


def test_magic_link_token_verifies_repeatedly_before_expiry():
    """The whole point of dropping single-use tracking: the same link must
    keep working across multiple opens as long as it hasn't expired yet."""
    payload = auth.issue_magic_link_payload(discord_user_id=42, guild_id=99)
    token = auth.sign_token(payload, "secret")

    first = auth.verify_token(token, "secret", auth.MAGIC_LINK_MAX_AGE_SECONDS)
    second = auth.verify_token(token, "secret", auth.MAGIC_LINK_MAX_AGE_SECONDS)
    third = auth.verify_token(token, "secret", auth.MAGIC_LINK_MAX_AGE_SECONDS)

    assert first is not None
    assert second is not None
    assert third is not None
    assert first["jti"] == second["jti"] == third["jti"]

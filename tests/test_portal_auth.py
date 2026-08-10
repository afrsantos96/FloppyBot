"""
sign_token/verify_token and single-use portal_tokens consumption -- the
magic-link auth primitives the portal's whole security model rests on.
"""
import importlib
import sqlite3
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


def _portal_tokens_db(tmp_path, monkeypatch):
    db_path = tmp_path / "svs.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE portal_tokens (
        jti TEXT PRIMARY KEY, discord_user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, consumed_at TEXT)""")
    conn.commit()
    conn.close()
    monkeypatch.setattr(auth, "SVS_DB", str(db_path))
    return db_path


def test_portal_token_is_single_use(tmp_path, monkeypatch):
    _portal_tokens_db(tmp_path, monkeypatch)

    auth.record_portal_token("jti-123", discord_user_id=42)

    assert auth.consume_portal_token("jti-123") is True, "first redemption must succeed"
    assert auth.consume_portal_token("jti-123") is False, "second redemption of the same link must fail"


def test_consuming_unknown_token_fails(tmp_path, monkeypatch):
    _portal_tokens_db(tmp_path, monkeypatch)

    assert auth.consume_portal_token("never-issued") is False

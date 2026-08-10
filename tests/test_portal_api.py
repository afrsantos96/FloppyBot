"""
Integration test for the portal's /api/schedule routes, run against a real
aiohttp app (TestServer/TestClient) backed by tmp-path sqlite files. No
pytest-asyncio/pytest-aiohttp dependency is added -- like the rest of this
suite, each test is a plain sync function that drives asyncio.run() itself.
"""
import asyncio
import importlib
import sqlite3
from types import SimpleNamespace

from aiohttp import test_utils

auth = importlib.import_module("web.auth")
db = importlib.import_module("web.db")
server = importlib.import_module("web.server")
config = importlib.import_module("web.config")
permission_handler = importlib.import_module("cogs.permission_handler")

PortalConfig = config.PortalConfig
PermissionManager = permission_handler.PermissionManager


def _setup_dbs(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.sqlite"
    alliance_path = tmp_path / "alliance.sqlite"
    users_path = tmp_path / "users.sqlite"
    svs_path = tmp_path / "svs.sqlite"

    with sqlite3.connect(settings_path) as c:
        c.execute("CREATE TABLE admin (id INTEGER PRIMARY KEY, is_initial INTEGER)")
        c.execute("CREATE TABLE adminserver (admin INTEGER, alliances_id INTEGER)")
        c.execute("INSERT INTO admin VALUES (1001, 1)")  # global admin
        c.commit()

    with sqlite3.connect(alliance_path) as c:
        c.execute("CREATE TABLE alliance_list (alliance_id INTEGER, name TEXT, discord_server_id INTEGER, kid INTEGER, multistate INTEGER, state_locked INTEGER)")
        c.execute("INSERT INTO alliance_list VALUES (5, 'TestAlli', 999, NULL, 0, 0)")
        c.commit()

    with sqlite3.connect(users_path) as c:
        c.execute("CREATE TABLE users (fid INTEGER, nickname TEXT, alliance INTEGER)")
        c.execute("INSERT INTO users VALUES (1, 'Alice', 5)")
        c.commit()

    with sqlite3.connect(svs_path) as c:
        c.execute("""CREATE TABLE appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fid INTEGER, manual_name TEXT, appointment_type TEXT, time TEXT, alliance INTEGER)""")
        c.execute("CREATE TABLE reference (context TEXT PRIMARY KEY, context_id INTEGER)")
        c.execute("INSERT INTO reference VALUES ('slot_mode', 0)")
        c.execute("CREATE TABLE portal_tokens (jti TEXT PRIMARY KEY, discord_user_id INTEGER NOT NULL, created_at TEXT NOT NULL, consumed_at TEXT)")
        c.commit()

    monkeypatch.setattr(PermissionManager, "SETTINGS_DB", str(settings_path))
    monkeypatch.setattr(PermissionManager, "ALLIANCE_DB", str(alliance_path))
    monkeypatch.setattr(PermissionManager, "USERS_DB", str(users_path))
    PermissionManager._admin_cache = None  # avoid bleed-through from another test's TTL cache

    monkeypatch.setattr(db, "SVS_DB", str(svs_path))
    monkeypatch.setattr(db, "USERS_DB", str(users_path))
    monkeypatch.setattr(db, "ALLIANCE_DB", str(alliance_path))

    return svs_path


def _session_cookie(cfg, discord_user_id=1001, guild_id=999):
    payload = auth.issue_session_payload(discord_user_id, guild_id)
    token = auth.sign_token(payload, cfg.signing_secret)
    return {auth.SESSION_COOKIE_NAME: token}


async def _with_client(cfg, coro_fn):
    # get_guild/get_user simulate "not cached" (falls through to the
    # SimpleNamespace fallback in _resolve_log_user) -- a real discord.py
    # Bot always has both, this fixture just needs to mimic their shape.
    fake_bot = SimpleNamespace(get_cog=lambda name: None, get_guild=lambda gid: None, get_user=lambda uid: None)
    app = server.create_app(fake_bot, cfg)
    test_server = test_utils.TestServer(app)
    client = test_utils.TestClient(test_server)
    await client.start_server()
    try:
        return await coro_fn(client)
    finally:
        await client.close()


def test_get_schedule_requires_session(tmp_path, monkeypatch):
    _setup_dbs(tmp_path, monkeypatch)
    cfg = PortalConfig(port=0, base_url="http://testserver", signing_secret="test-secret")

    async def run(client):
        resp = await client.get("/api/schedule")
        return resp.status

    status = asyncio.run(_with_client(cfg, run))
    assert status == 401


def test_get_schedule_returns_positions_and_members(tmp_path, monkeypatch):
    _setup_dbs(tmp_path, monkeypatch)
    cfg = PortalConfig(port=0, base_url="http://testserver", signing_secret="test-secret")
    cookies = _session_cookie(cfg)

    async def run(client):
        resp = await client.get("/api/schedule", cookies=cookies)
        assert resp.status == 200
        return await resp.json()

    body = asyncio.run(_with_client(cfg, run))
    assert body["positions"] == ["Chief Minister"]
    assert any(m["fid"] == 1 and m["nickname"] == "Alice" for m in body["members"])


def test_post_schedule_assigns_registered_member(tmp_path, monkeypatch):
    svs_path = _setup_dbs(tmp_path, monkeypatch)
    cfg = PortalConfig(port=0, base_url="http://testserver", signing_secret="test-secret")
    cookies = _session_cookie(cfg)

    async def run(client):
        resp = await client.post(
            "/api/schedule",
            cookies=cookies,
            json={"changes": [{"appointment_type": "Chief Minister", "time": "09:00", "fid": 1}]},
        )
        assert resp.status == 200
        return await resp.json()

    body = asyncio.run(_with_client(cfg, run))
    assert body["applied"] == 1
    assert body["conflicts"] == []

    with sqlite3.connect(svs_path) as c:
        row = c.execute(
            "SELECT fid, manual_name FROM appointments WHERE appointment_type='Chief Minister' AND time='09:00'"
        ).fetchone()
    assert row == (1, None)


def test_post_schedule_manual_name_assignment(tmp_path, monkeypatch):
    svs_path = _setup_dbs(tmp_path, monkeypatch)
    cfg = PortalConfig(port=0, base_url="http://testserver", signing_secret="test-secret")
    cookies = _session_cookie(cfg)

    async def run(client):
        resp = await client.post(
            "/api/schedule",
            cookies=cookies,
            json={"changes": [{"appointment_type": "Chief Minister", "time": "10:30", "manual_name": "Guest Governor"}]},
        )
        assert resp.status == 200
        return await resp.json()

    body = asyncio.run(_with_client(cfg, run))
    assert body["applied"] == 1

    with sqlite3.connect(svs_path) as c:
        row = c.execute(
            "SELECT fid, manual_name FROM appointments WHERE appointment_type='Chief Minister' AND time='10:30'"
        ).fetchone()
    assert row == (None, "Guest Governor")


def test_post_schedule_rejects_fid_outside_permission_scope(tmp_path, monkeypatch):
    _setup_dbs(tmp_path, monkeypatch)
    cfg = PortalConfig(port=0, base_url="http://testserver", signing_secret="test-secret")
    cookies = _session_cookie(cfg)

    async def run(client):
        resp = await client.post(
            "/api/schedule",
            cookies=cookies,
            json={"changes": [{"appointment_type": "Chief Minister", "time": "09:00", "fid": 999999}]},
        )
        assert resp.status == 200
        return await resp.json()

    body = asyncio.run(_with_client(cfg, run))
    assert body["applied"] == 0
    assert body["conflicts"][0]["reason"] == "fid_not_permitted"


def test_clear_does_not_touch_a_different_slot_for_the_same_fid(tmp_path, monkeypatch):
    """A malformed/direct API call combining clear:true with an fid must only
    empty the targeted slot -- it must not also free up that fid's other
    booking (the reschedule-cleanup path is only meant to run for a real
    assignment, not a clear)."""
    svs_path = _setup_dbs(tmp_path, monkeypatch)
    with sqlite3.connect(svs_path) as c:
        c.execute(
            "INSERT INTO appointments (fid, manual_name, appointment_type, time, alliance) "
            "VALUES (1, NULL, 'Chief Minister', '09:00', 5)"
        )
        c.execute(
            "INSERT INTO appointments (fid, manual_name, appointment_type, time, alliance) "
            "VALUES (NULL, 'Guest', 'Chief Minister', '10:00', NULL)"
        )
        c.commit()

    result = db.apply_schedule_changes(
        [{"appointment_type": "Chief Minister", "time": "10:00", "clear": True, "fid": 1}],
        discord_user_id=1001,
        guild_id=999,
    )

    assert result["applied"][0]["action"] == "remove"
    with sqlite3.connect(svs_path) as c:
        rows = c.execute("SELECT fid, time FROM appointments WHERE appointment_type='Chief Minister'").fetchall()
    assert rows == [(1, "09:00")], "fid 1's own 09:00 booking must survive a clear targeted at 10:00"


def test_link_preview_bot_does_not_get_a_session(tmp_path, monkeypatch):
    """Discord (and other chat apps) auto-fetch plain-text URLs server-side to
    build a link preview. A request carrying a known link-preview-bot
    User-Agent must be answered without issuing a session cookie or redirect,
    while a real client hitting the same link still works normally."""
    _setup_dbs(tmp_path, monkeypatch)
    cfg = PortalConfig(port=0, base_url="http://testserver", signing_secret="test-secret")

    payload = auth.issue_magic_link_payload(discord_user_id=1001, guild_id=999)
    token = auth.sign_token(payload, cfg.signing_secret)

    async def run(client):
        bot_resp = await client.get(f"/portal/{token}", headers={"User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"}, allow_redirects=False)
        bot_status = bot_resp.status
        bot_got_cookie = auth.SESSION_COOKIE_NAME in bot_resp.cookies

        real_resp = await client.get(f"/portal/{token}", allow_redirects=False)
        real_status = real_resp.status

        return bot_status, bot_got_cookie, real_status

    bot_status, bot_got_cookie, real_status = asyncio.run(_with_client(cfg, run))
    assert bot_status == 200, "the crawler request itself must not error"
    assert bot_got_cookie is False, "a crawler must not be handed a session cookie"
    assert real_status == 302, "a real client hitting the link must still redeem it successfully"


def test_magic_link_is_reusable_across_multiple_opens(tmp_path, monkeypatch):
    """Links are valid until they expire, not single-use -- opening the same
    link twice (e.g. a page reload, or clicking it again) must not fail."""
    _setup_dbs(tmp_path, monkeypatch)
    cfg = PortalConfig(port=0, base_url="http://testserver", signing_secret="test-secret")

    payload = auth.issue_magic_link_payload(discord_user_id=1001, guild_id=999)
    token = auth.sign_token(payload, cfg.signing_secret)

    async def run(client):
        statuses = []
        for _ in range(3):
            resp = await client.get(f"/portal/{token}", allow_redirects=False)
            statuses.append(resp.status)
        return statuses

    statuses = asyncio.run(_with_client(cfg, run))
    assert statuses == [302, 302, 302], "every open of the same link must succeed until it expires"

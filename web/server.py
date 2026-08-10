"""
aiohttp.web app factory + lifecycle for the minister portal. Started once
from main.py's start_bot(), stopped in the same function's cleanup block --
not from on_ready, since on_ready re-fires on every gateway reconnect and
the portal has no reason to bounce with it.
"""
import logging

from aiohttp import web

from . import auth
from . import routes
from .config import load_portal_config

logger = logging.getLogger('bot')


def create_app(bot, cfg) -> web.Application:
    app = web.Application(middlewares=[auth.session_middleware])
    app["bot"] = bot
    app["portal_config"] = cfg

    # Literal routes must be registered before the /portal/{token} catch-all
    # so "schedule"/"logout" can never be swallowed as a token value.
    app.router.add_get("/portal/schedule", routes.portal_schedule_page)
    app.router.add_get("/portal/logout", routes.portal_logout)
    app.router.add_static("/portal/static/", routes.STATIC_DIR, name="portal_static")
    app.router.add_get("/api/schedule", routes.api_get_schedule)
    app.router.add_post("/api/schedule", routes.api_save_schedule)
    app.router.add_get("/portal/{token}", routes.redeem_token)

    return app


async def start_portal(bot):
    """Returns a running AppRunner, or None if the portal is disabled/misconfigured."""
    cfg = load_portal_config()
    if not cfg:
        return None

    app = create_app(bot, cfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.port)
    await site.start()
    logger.info(f"Minister portal listening on 0.0.0.0:{cfg.port} (base_url={cfg.base_url})")
    return runner


async def stop_portal(runner):
    if runner is not None:
        await runner.cleanup()

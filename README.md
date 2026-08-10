# :crown: Kingshot Discord Bot

Kingshot Discord Bot that supports alliance management, event reminders and attendance tracking, gift code redemption, minister appointment planning and more. This bot is free, open source and self-hosted. It is specifically designed for [Kingshot](https://www.centurygames.com/games/kingshot/).

## 🚀 Getting Started

To get started with the bot, head over to the [wiki](https://github.com/kingshot-project/Kingshot-Discord-Bot/wiki) for instructions and other information.

If you have any issues with the bot, head over to the [common issues](https://github.com/kingshot-project/Kingshot-Discord-Bot/wiki/Getting-Help) page or join our [discord server](https://discord.gg/apYByj6K2m) for support.

## 🌐 Minister Scheduling Web Portal (optional)

The "Online Manage Portal" button under Minister Scheduling opens a small web page where admins can assign minister slots, including manually-typed names for members who aren't registered with the bot. It's disabled unless configured via environment variables:

| Variable | Required | Description |
|---|---|---|
| `PORTAL_ENABLED` | no | Set to `0` to disable the portal entirely. Defaults on. |
| `PORTAL_PORT` | no | Internal port the portal listens on. Defaults to `8090`. |
| `PORTAL_BASE_URL` | yes | Public URL admins reach the portal at, e.g. `https://portal.example.com`. Used to build the one-time links sent in Discord. |
| `PORTAL_SIGNING_SECRET` | yes | Random secret used to sign portal links/sessions. Generate one with `openssl rand -hex 32` and keep it stable across restarts. |

The portal binds to `127.0.0.1` in the provided `docker-compose.yml` — put a reverse proxy (nginx, Caddy, etc.) in front to terminate TLS on your domain and forward to it. If `PORTAL_BASE_URL`/`PORTAL_SIGNING_SECRET` aren't set, the portal simply doesn't start; every other bot feature works normally.

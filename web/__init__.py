"""
Minister scheduling web portal. A small aiohttp.web app that runs in-process
alongside the Discord bot, letting admins assign minister slots (including
manually-typed, unregistered names) from a browser instead of only via
Discord dropdowns. See web/server.py for the app factory and lifecycle.
"""

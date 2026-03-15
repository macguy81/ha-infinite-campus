"""
Web Server for the Infinite Campus HA Add-on.

Provides a status dashboard, data views, and REST API.
All configuration is done via the HA add-on Configuration tab.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from infinite_campus_api import InfiniteCampusAPI
from whatsapp_notify import WhatsAppNotifier
from scheduler import ICScheduler

logger = logging.getLogger(__name__)

# Paths
DATA_DIR = Path("/data")
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def load_config() -> dict:
    """Load configuration from HA add-on options (injected by Supervisor)."""
    ha_options = Path("/data/options.json")
    if ha_options.exists():
        with open(ha_options) as f:
            config = json.load(f)
            logger.info(f"Loaded config from HA options: {list(config.keys())}")
            return config
    logger.warning("No /data/options.json found - add-on not configured yet")
    return {}


class ICWebServer:
    """Web server for the add-on's UI and API."""

    def __init__(self):
        self.app = web.Application()
        self.config = load_config()
        self.api: Optional[InfiniteCampusAPI] = None
        self.notifier: Optional[WhatsAppNotifier] = None
        self.scheduler: Optional[ICScheduler] = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Register all HTTP routes."""
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/status", self.handle_status)
        self.app.router.add_get("/api/data", self.handle_data)
        self.app.router.add_get("/api/data/{category}", self.handle_data_category)
        self.app.router.add_get("/api/config", self.handle_get_config)
        self.app.router.add_post("/api/poll", self.handle_poll_now)
        self.app.router.add_post("/api/start", self.handle_start)
        self.app.router.add_post("/api/stop", self.handle_stop)
        self.app.router.add_post("/api/test-whatsapp", self.handle_test_whatsapp)
        self.app.router.add_static("/static", STATIC_DIR)

    async def handle_index(self, request: web.Request) -> web.Response:
        """Serve the main dashboard page."""
        html_path = TEMPLATES_DIR / "index.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="Dashboard not found", status=404)

    async def handle_status(self, request: web.Request) -> web.Response:
        """Return current service status."""
        status = {
            "configured": bool(self.config.get("ic_base_url")),
            "authenticated": self.api._authenticated if self.api else False,
            "scheduler_running": self.scheduler._running if self.scheduler else False,
            "whatsapp_configured": bool(
                self.config.get("whatsapp_phone") and self.config.get("whatsapp_api_key")
            ),
            "scheduler": self.scheduler.get_status() if self.scheduler else None,
            "config_summary": {
                "base_url": self.config.get("ic_base_url", "Not set"),
                "district": self.config.get("ic_district", "Not set"),
                "username": self.config.get("ic_username", "Not set"),
                "whatsapp": "Configured" if self.config.get("whatsapp_phone") else "Not set",
                "poll_interval": self.config.get("poll_interval", 900),
            },
            "ha_entities_count": (
                self.scheduler.get_status().get("ha_entities_count", 0)
                if self.scheduler else 0
            ),
            "timestamp": datetime.now().isoformat(),
        }
        return web.json_response(status)

    async def handle_data(self, request: web.Request) -> web.Response:
        """Return all cached data."""
        if self.scheduler and self.scheduler.latest_data:
            return web.json_response(self.scheduler.latest_data, dumps=lambda x: json.dumps(x, default=str))
        return web.json_response({"error": "No data available yet"}, status=404)

    async def handle_data_category(self, request: web.Request) -> web.Response:
        """Return data for a specific category."""
        category = request.match_info["category"]
        if self.scheduler and self.scheduler.latest_data:
            data = self.scheduler.latest_data.get(category)
            if data is not None:
                return web.json_response(data, dumps=lambda x: json.dumps(x, default=str))
        return web.json_response({"error": f"No {category} data available"}, status=404)

    async def handle_get_config(self, request: web.Request) -> web.Response:
        """Return current config summary (passwords masked)."""
        safe_config = {
            "ic_base_url": self.config.get("ic_base_url", ""),
            "ic_district": self.config.get("ic_district", ""),
            "ic_username": self.config.get("ic_username", ""),
            "whatsapp_phone": self.config.get("whatsapp_phone", ""),
            "poll_interval": self.config.get("poll_interval", 900),
            "notify_grades": self.config.get("notify_grades", True),
            "notify_assignments": self.config.get("notify_assignments", True),
            "notify_attendance": self.config.get("notify_attendance", True),
            "notify_notifications": self.config.get("notify_notifications", True),
            "daily_summary": self.config.get("daily_summary", True),
            "daily_summary_hour": self.config.get("daily_summary_hour", 18),
            "auto_start": self.config.get("auto_start", True),
            "whatsapp_phone_2": self.config.get("whatsapp_phone_2", ""),
        }
        return web.json_response(safe_config)

    async def handle_poll_now(self, request: web.Request) -> web.Response:
        """Trigger an immediate data poll."""
        if not self.scheduler:
            return web.json_response(
                {"error": "Service not started. Click Start Service."}, status=400
            )
        try:
            data = await self.scheduler.poll_now()
            return web.json_response(
                {"success": True, "data_summary": {k: len(v) if isinstance(v, list) else v for k, v in data.items()}},
                dumps=lambda x: json.dumps(x, default=str),
            )
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_start(self, request: web.Request) -> web.Response:
        """Start the polling scheduler."""
        try:
            # Reload config from HA options in case it was updated
            self.config = load_config()
            await self._init_services()
            if self.scheduler:
                await self.scheduler.start()
            return web.json_response({"success": True, "message": "Service started"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_stop(self, request: web.Request) -> web.Response:
        """Stop the polling scheduler."""
        await self._stop_services()
        return web.json_response({"success": True, "message": "Service stopped"})

    async def handle_test_whatsapp(self, request: web.Request) -> web.Response:
        """Send a test WhatsApp message to verify configuration."""
        if not self.notifier:
            # Try to initialize notifier from current config
            config = load_config()
            phone = config.get("whatsapp_phone", "")
            api_key = config.get("whatsapp_api_key", "")
            if not phone or not api_key:
                return web.json_response(
                    {"success": False, "error": "WhatsApp not configured. Add phone number and API key in the HA Configuration tab, then restart the add-on."},
                    status=400,
                )
            # Create a temporary notifier for testing
            notifier = WhatsAppNotifier(
                phone_number=phone,
                api_key=api_key,
                phone_number_2=config.get("whatsapp_phone_2", ""),
                api_key_2=config.get("whatsapp_api_key_2", ""),
            )
        else:
            notifier = self.notifier

        try:
            result = await notifier.test_connection()
            if notifier is not self.notifier:
                await notifier.close()
            return web.json_response(result)
        except Exception as e:
            if notifier is not self.notifier:
                await notifier.close()
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def _init_services(self) -> None:
        """Initialize API client, notifier, and scheduler from HA config."""
        config = self.config

        if not config.get("ic_base_url"):
            logger.warning("Infinite Campus not configured. Set credentials in HA Configuration tab.")
            return

        # Initialize IC API client
        self.api = InfiniteCampusAPI(
            base_url=config["ic_base_url"],
            district=config.get("ic_district", ""),
            username=config.get("ic_username", ""),
            password=config.get("ic_password", ""),
        )

        # Initialize WhatsApp notifier (optional, supports 2 numbers)
        self.notifier = None
        if config.get("whatsapp_phone") and config.get("whatsapp_api_key"):
            self.notifier = WhatsAppNotifier(
                phone_number=config["whatsapp_phone"],
                api_key=config["whatsapp_api_key"],
                phone_number_2=config.get("whatsapp_phone_2", ""),
                api_key_2=config.get("whatsapp_api_key_2", ""),
            )

        # Initialize scheduler
        self.scheduler = ICScheduler(
            api=self.api,
            notifier=self.notifier,
            poll_interval=config.get("poll_interval", 900),
            notify_grades=config.get("notify_grades", True),
            notify_assignments=config.get("notify_assignments", True),
            notify_attendance=config.get("notify_attendance", True),
            notify_notifications=config.get("notify_notifications", True),
            daily_summary=config.get("daily_summary", True),
            daily_summary_hour=config.get("daily_summary_hour", 18),
        )

    async def _stop_services(self) -> None:
        """Stop all running services."""
        if self.scheduler:
            await self.scheduler.stop()
        if self.api:
            await self.api.close()
        if self.notifier:
            await self.notifier.close()

    async def start(self, host: str = "0.0.0.0", port: int = 8099) -> None:
        """Start the web server and services."""
        # Initialize services if configured
        if self.config.get("ic_base_url"):
            try:
                await self._init_services()
                if self.scheduler and self.config.get("auto_start", True):
                    await self.scheduler.start()
                    logger.info("Auto-started scheduler")
            except Exception as e:
                logger.error(f"Failed to auto-start services: {e}")

        # Start web server
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"Web server running at http://{host}:{port}")

        # Keep running
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await self._stop_services()
            await runner.cleanup()


async def main():
    """Entry point for the web server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    server = ICWebServer()
    await server.start()


if __name__ == "__main__":
    asyncio.run(main())

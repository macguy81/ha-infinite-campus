"""
Web Server for the Infinite Campus HA Add-on.

Provides a configuration UI, status dashboard, and REST API
for managing the Infinite Campus monitoring service.
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
CONFIG_FILE = DATA_DIR / "options.json"
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def load_config() -> dict:
    """Load configuration from HA add-on options or local file."""
    # First try HA add-on options (injected by HA Supervisor)
    ha_options = Path("/data/options.json")
    if ha_options.exists():
        with open(ha_options) as f:
            return json.load(f)

    # Fallback to local config
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)

    return {}


def save_config(config: dict) -> None:
    """Save configuration to file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


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
        self.app.router.add_post("/api/config", self.handle_save_config)
        self.app.router.add_get("/api/config", self.handle_get_config)
        self.app.router.add_post("/api/test-connection", self.handle_test_connection)
        self.app.router.add_post("/api/test-whatsapp", self.handle_test_whatsapp)
        self.app.router.add_post("/api/poll", self.handle_poll_now)
        self.app.router.add_post("/api/start", self.handle_start)
        self.app.router.add_post("/api/stop", self.handle_stop)
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
        """Return current config (with password masked)."""
        safe_config = dict(self.config)
        if "ic_password" in safe_config:
            safe_config["ic_password"] = "********"
        if "whatsapp_api_key" in safe_config:
            safe_config["whatsapp_api_key"] = "********"
        return web.json_response(safe_config)

    async def handle_save_config(self, request: web.Request) -> web.Response:
        """Save configuration and restart services."""
        try:
            new_config = await request.json()

            # Don't overwrite passwords with masked values
            if new_config.get("ic_password") == "********":
                new_config["ic_password"] = self.config.get("ic_password", "")
            if new_config.get("whatsapp_api_key") == "********":
                new_config["whatsapp_api_key"] = self.config.get("whatsapp_api_key", "")

            self.config.update(new_config)
            save_config(self.config)

            # Restart services with new config
            await self._stop_services()
            await self._init_services()

            return web.json_response({"success": True, "message": "Configuration saved"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_test_connection(self, request: web.Request) -> web.Response:
        """Test the Infinite Campus connection."""
        try:
            config = await request.json()

            test_api = InfiniteCampusAPI(
                base_url=config.get("ic_base_url", self.config.get("ic_base_url", "")),
                district=config.get("ic_district", self.config.get("ic_district", "")),
                username=config.get("ic_username", self.config.get("ic_username", "")),
                password=config.get("ic_password", self.config.get("ic_password", ""))
                if config.get("ic_password") != "********"
                else self.config.get("ic_password", ""),
            )

            await test_api.authenticate()
            students = await test_api.get_students()
            await test_api.close()

            return web.json_response({
                "success": True,
                "message": f"Connected! Found {len(students)} student(s)",
                "students": [
                    {
                        "name": s.get("firstName", "") + " " + s.get("lastName", ""),
                        "id": s.get("personID", s.get("studentID", "")),
                    }
                    for s in students
                ],
            })
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_test_whatsapp(self, request: web.Request) -> web.Response:
        """Test the WhatsApp notification."""
        try:
            config = await request.json()
            phone = config.get("whatsapp_phone", self.config.get("whatsapp_phone", ""))
            api_key = config.get("whatsapp_api_key", self.config.get("whatsapp_api_key", ""))
            if config.get("whatsapp_api_key") == "********":
                api_key = self.config.get("whatsapp_api_key", "")

            test_notifier = WhatsAppNotifier(phone_number=phone, api_key=api_key)
            result = await test_notifier.test_connection()
            await test_notifier.close()

            return web.json_response(result)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_poll_now(self, request: web.Request) -> web.Response:
        """Trigger an immediate data poll."""
        if not self.scheduler:
            return web.json_response(
                {"error": "Service not started"}, status=400
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

    async def _init_services(self) -> None:
        """Initialize API client, notifier, and scheduler from config."""
        config = self.config

        if not config.get("ic_base_url"):
            logger.warning("Infinite Campus not configured yet")
            return

        # Initialize IC API client
        self.api = InfiniteCampusAPI(
            base_url=config["ic_base_url"],
            district=config.get("ic_district", ""),
            username=config.get("ic_username", ""),
            password=config.get("ic_password", ""),
        )

        # Initialize WhatsApp notifier (optional)
        self.notifier = None
        if config.get("whatsapp_phone") and config.get("whatsapp_api_key"):
            self.notifier = WhatsAppNotifier(
                phone_number=config["whatsapp_phone"],
                api_key=config["whatsapp_api_key"],
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

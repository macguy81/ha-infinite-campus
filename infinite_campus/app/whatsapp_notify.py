"""
WhatsApp Notification Service via CallMeBot.

Free WhatsApp messaging API that sends notifications to your
personal WhatsApp number. Supports message formatting and
rate limiting.

Setup:
1. Save CallMeBot's number: +34 644 51 95 23
2. Send "I allow callmebot to send me messages" to that number on WhatsApp
3. Wait for the API key response
4. Enter the phone number and API key in the add-on config
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)


class WhatsAppNotifier:
    """
    WhatsApp notification sender using CallMeBot free API.

    Rate limits: ~25 messages per day (CallMeBot fair use).
    Messages are queued and sent with delays to avoid rate limiting.
    """

    CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
    MIN_INTERVAL_SECONDS = 5  # Minimum delay between messages

    def __init__(
        self,
        phone_number: str,
        api_key: str,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        """
        Initialize the WhatsApp notifier.

        Args:
            phone_number: WhatsApp number with country code (e.g., +12125551234)
            api_key: CallMeBot API key
            session: Optional aiohttp session to reuse
        """
        self.phone_number = self._normalize_phone(phone_number)
        self.api_key = api_key
        self._session = session
        self._owns_session = session is None
        self._last_sent: Optional[datetime] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._queue_task: Optional[asyncio.Task] = None
        self._daily_count = 0
        self._daily_reset: Optional[datetime] = None

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Normalize phone number to include + prefix."""
        phone = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not phone.startswith("+"):
            phone = "+" + phone
        return phone

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Create or return the aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _check_daily_limit(self) -> bool:
        """Check if we've exceeded the daily message limit."""
        now = datetime.now()
        if self._daily_reset is None or now.date() > self._daily_reset.date():
            self._daily_count = 0
            self._daily_reset = now
        return self._daily_count < 25

    async def send_message(self, message: str) -> dict:
        """
        Send a WhatsApp message immediately.

        Args:
            message: The message text to send. Supports WhatsApp formatting:
                     *bold*, _italic_, ~strikethrough~, ```code```

        Returns:
            dict with status and details
        """
        if not self._check_daily_limit():
            return {
                "success": False,
                "error": "Daily message limit reached (25/day)",
            }

        # Rate limit: wait if we sent recently
        if self._last_sent:
            elapsed = (datetime.now() - self._last_sent).total_seconds()
            if elapsed < self.MIN_INTERVAL_SECONDS:
                await asyncio.sleep(self.MIN_INTERVAL_SECONDS - elapsed)

        session = await self._ensure_session()

        # URL-encode the message
        encoded_message = quote(message)

        params = {
            "phone": self.phone_number,
            "text": message,
            "apikey": self.api_key,
        }

        try:
            async with session.get(
                self.CALLMEBOT_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                response_text = await resp.text()
                self._last_sent = datetime.now()
                self._daily_count += 1

                if resp.status == 200 and "queued" in response_text.lower():
                    logger.info(f"WhatsApp message sent successfully ({self._daily_count}/25 today)")
                    return {
                        "success": True,
                        "message": "Message queued for delivery",
                        "daily_count": self._daily_count,
                    }
                else:
                    logger.warning(
                        f"WhatsApp send issue: HTTP {resp.status} - {response_text[:200]}"
                    )
                    return {
                        "success": False,
                        "error": f"HTTP {resp.status}: {response_text[:200]}",
                    }

        except asyncio.TimeoutError:
            return {"success": False, "error": "Request timed out"}
        except aiohttp.ClientError as e:
            return {"success": False, "error": f"Connection error: {e}"}

    async def send_queued(self, message: str) -> None:
        """Add a message to the send queue."""
        await self._message_queue.put(message)

        if self._queue_task is None or self._queue_task.done():
            self._queue_task = asyncio.create_task(self._process_queue())

    async def _process_queue(self) -> None:
        """Process queued messages with rate limiting."""
        while not self._message_queue.empty():
            message = await self._message_queue.get()
            result = await self.send_message(message)
            if not result["success"]:
                logger.error(f"Failed to send queued message: {result['error']}")
            self._message_queue.task_done()

    # ─── Formatted Message Helpers ──────────────────────────────────

    def format_grade_alert(
        self,
        student_name: str,
        course_name: str,
        assignment_name: str,
        score: str,
        total: str,
        letter_grade: str = "",
    ) -> str:
        """Format a grade notification message."""
        msg = (
            f"📚 *Grade Update*\n\n"
            f"👤 Student: *{student_name}*\n"
            f"📖 Course: {course_name}\n"
            f"📝 Assignment: {assignment_name}\n"
            f"✅ Score: *{score}/{total}*"
        )
        if letter_grade:
            msg += f" ({letter_grade})"
        msg += f"\n\n🕐 {datetime.now().strftime('%b %d, %Y %I:%M %p')}"
        return msg

    def format_assignment_alert(
        self,
        student_name: str,
        course_name: str,
        assignment_name: str,
        due_date: str,
    ) -> str:
        """Format a new assignment notification."""
        return (
            f"📋 *New Assignment*\n\n"
            f"👤 Student: *{student_name}*\n"
            f"📖 Course: {course_name}\n"
            f"📝 Assignment: {assignment_name}\n"
            f"📅 Due: {due_date}\n\n"
            f"🕐 {datetime.now().strftime('%b %d, %Y %I:%M %p')}"
        )

    def format_attendance_alert(
        self,
        student_name: str,
        date: str,
        status: str,
        period: str = "",
    ) -> str:
        """Format an attendance notification."""
        return (
            f"🏫 *Attendance Alert*\n\n"
            f"👤 Student: *{student_name}*\n"
            f"📅 Date: {date}\n"
            f"📋 Status: *{status}*"
            + (f"\n⏰ Period: {period}" if period else "")
            + f"\n\n🕐 {datetime.now().strftime('%b %d, %Y %I:%M %p')}"
        )

    def format_daily_summary(
        self,
        student_name: str,
        courses_count: int,
        assignments_due: int,
        new_grades: int,
        gpa: str = "",
    ) -> str:
        """Format a daily summary message."""
        msg = (
            f"📊 *Daily Summary*\n\n"
            f"👤 Student: *{student_name}*\n"
            f"📅 {datetime.now().strftime('%A, %B %d, %Y')}\n\n"
            f"📖 Courses: {courses_count}\n"
            f"📝 Assignments Due: {assignments_due}\n"
            f"✅ New Grades: {new_grades}"
        )
        if gpa:
            msg += f"\n📈 GPA: *{gpa}*"
        return msg

    def format_notification(self, title: str, body: str) -> str:
        """Format a general notification."""
        return (
            f"🔔 *{title}*\n\n"
            f"{body}\n\n"
            f"🕐 {datetime.now().strftime('%b %d, %Y %I:%M %p')}"
        )

    async def close(self) -> None:
        """Close the HTTP session and cancel queue processing."""
        if self._queue_task and not self._queue_task.done():
            self._queue_task.cancel()
        if self._session and self._owns_session and not self._session.closed:
            await self._session.close()

    async def test_connection(self) -> dict:
        """Send a test message to verify the configuration works."""
        return await self.send_message(
            "✅ *Infinite Campus Monitor*\n\n"
            "WhatsApp notifications configured successfully!\n"
            f"🕐 {datetime.now().strftime('%b %d, %Y %I:%M %p')}"
        )

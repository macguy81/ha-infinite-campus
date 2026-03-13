"""
Data Polling Scheduler.

Periodically fetches data from Infinite Campus, detects changes,
and triggers WhatsApp notifications for updates.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from infinite_campus_api import InfiniteCampusAPI
from whatsapp_notify import WhatsAppNotifier

logger = logging.getLogger(__name__)

DATA_DIR = Path("/data")
CACHE_FILE = DATA_DIR / "ic_cache.json"


class ChangeDetector:
    """Detects changes between cached and fresh data from Infinite Campus."""

    def __init__(self):
        self._cache: dict[str, Any] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """Load cached data from disk."""
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, "r") as f:
                    self._cache = json.load(f)
                logger.info("Loaded cached data from disk")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load cache: {e}")
                self._cache = {}

    def _save_cache(self) -> None:
        """Persist cached data to disk."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump(self._cache, f, indent=2, default=str)
        except IOError as e:
            logger.error(f"Failed to save cache: {e}")

    def detect_changes(self, category: str, new_data: list[dict]) -> dict:
        """
        Compare new data against cached data and detect changes.

        Returns:
            dict with 'added', 'modified', 'removed' lists
        """
        old_data = self._cache.get(category, [])

        # Create lookup maps using a composite key
        def make_key(item: dict) -> str:
            """Create a unique key for an item based on available fields."""
            key_fields = [
                "assignmentID", "courseID", "studentID", "personID",
                "termID", "attendanceID", "notificationID", "id",
                "assignmentName", "courseName", "date",
            ]
            parts = []
            for field in key_fields:
                if field in item:
                    parts.append(f"{field}={item[field]}")
            return "|".join(parts) if parts else json.dumps(item, sort_keys=True, default=str)

        old_map = {make_key(item): item for item in old_data}
        new_map = {make_key(item): item for item in new_data}

        added = [
            new_map[k] for k in new_map if k not in old_map
        ]
        removed = [
            old_map[k] for k in old_map if k not in new_map
        ]
        modified = []
        for k in new_map:
            if k in old_map:
                # Compare JSON representations
                if json.dumps(new_map[k], sort_keys=True, default=str) != json.dumps(
                    old_map[k], sort_keys=True, default=str
                ):
                    modified.append(new_map[k])

        # Update cache
        self._cache[category] = new_data
        self._save_cache()

        return {"added": added, "modified": modified, "removed": removed}

    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._cache = {}
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()


class ICScheduler:
    """
    Polls Infinite Campus at regular intervals and sends
    WhatsApp notifications on changes.
    """

    def __init__(
        self,
        api: InfiniteCampusAPI,
        notifier: Optional[WhatsAppNotifier],
        poll_interval: int = 900,  # 15 minutes default
        notify_grades: bool = True,
        notify_assignments: bool = True,
        notify_attendance: bool = True,
        notify_notifications: bool = True,
        daily_summary: bool = True,
        daily_summary_hour: int = 18,  # 6 PM
    ):
        self.api = api
        self.notifier = notifier
        self.poll_interval = max(300, poll_interval)  # Minimum 5 min
        self.notify_grades = notify_grades
        self.notify_assignments = notify_assignments
        self.notify_attendance = notify_attendance
        self.notify_notifications = notify_notifications
        self.daily_summary = daily_summary
        self.daily_summary_hour = daily_summary_hour

        self._detector = ChangeDetector()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_poll: Optional[datetime] = None
        self._last_summary_date: Optional[str] = None
        self._student_names: dict[str, str] = {}

        # Track latest data for the web UI
        self.latest_data: dict[str, Any] = {}
        self.poll_count = 0
        self.error_count = 0
        self.last_error: Optional[str] = None
        self.notifications_sent = 0

    async def start(self) -> None:
        """Start the polling scheduler."""
        if self._running:
            logger.warning("Scheduler is already running")
            return

        self._running = True
        logger.info(
            f"Starting scheduler with {self.poll_interval}s interval"
        )

        # Authenticate first
        try:
            await self.api.authenticate()
            logger.info("Initial authentication successful")
        except Exception as e:
            logger.error(f"Initial authentication failed: {e}")
            self.last_error = str(e)

        # Start polling loop
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop the polling scheduler."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler stopped")

    async def poll_now(self) -> dict[str, Any]:
        """Trigger an immediate poll and return the data."""
        return await self._do_poll()

    async def _poll_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                await self._do_poll()

                # Check if it's time for daily summary
                if self.daily_summary:
                    await self._check_daily_summary()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.error_count += 1
                self.last_error = str(e)
                logger.error(f"Poll error: {e}")

            # Wait for next poll
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def _do_poll(self) -> dict[str, Any]:
        """Execute a single poll cycle."""
        logger.info("Starting poll cycle...")
        self.poll_count += 1

        try:
            # Fetch all data
            data = await self.api.get_all_data()
            self.latest_data = data
            self._last_poll = datetime.now()

            # Build student name lookup
            for student in data.get("students", []):
                sid = str(
                    student.get("personID", student.get("studentID", ""))
                )
                name = student.get(
                    "firstName",
                    student.get("name", f"Student {sid}"),
                )
                last = student.get("lastName", "")
                if last:
                    name = f"{name} {last}"
                self._student_names[sid] = name

            # Detect changes and send notifications
            for category in [
                "grades",
                "assignments",
                "attendance",
                "notifications",
            ]:
                if data.get(category):
                    changes = self._detector.detect_changes(
                        category, data[category]
                    )
                    await self._process_changes(category, changes)

            # Also detect course changes (no notifications, just cache)
            for category in ["courses", "terms", "schedule"]:
                if data.get(category):
                    self._detector.detect_changes(category, data[category])

            logger.info(
                f"Poll complete. Students: {len(data.get('students', []))}, "
                f"Courses: {len(data.get('courses', []))}, "
                f"Assignments: {len(data.get('assignments', []))}"
            )

            return data

        except Exception as e:
            self.error_count += 1
            self.last_error = str(e)
            logger.error(f"Poll failed: {e}")
            raise

    async def _process_changes(
        self, category: str, changes: dict
    ) -> None:
        """Process detected changes and send appropriate notifications."""
        if not self.notifier:
            return

        added = changes.get("added", [])
        modified = changes.get("modified", [])

        if not added and not modified:
            return

        if category == "grades" and self.notify_grades:
            for item in added + modified:
                sid = str(item.get("studentID", ""))
                name = self._student_names.get(sid, f"Student {sid}")
                msg = self.notifier.format_grade_alert(
                    student_name=name,
                    course_name=item.get("courseName", "Unknown Course"),
                    assignment_name=item.get(
                        "assignmentName", "Unknown Assignment"
                    ),
                    score=str(item.get("score", "N/A")),
                    total=str(item.get("totalPoints", "N/A")),
                    letter_grade=item.get("letterGrade", ""),
                )
                await self._send_notification(msg)

        elif category == "assignments" and self.notify_assignments:
            for item in added:
                sid = str(item.get("studentID", ""))
                name = self._student_names.get(sid, f"Student {sid}")
                msg = self.notifier.format_assignment_alert(
                    student_name=name,
                    course_name=item.get("courseName", "Unknown Course"),
                    assignment_name=item.get(
                        "assignmentName", "Unknown Assignment"
                    ),
                    due_date=item.get("dueDate", "N/A"),
                )
                await self._send_notification(msg)

        elif category == "attendance" and self.notify_attendance:
            for item in added:
                sid = str(item.get("studentID", ""))
                name = self._student_names.get(sid, f"Student {sid}")
                status = item.get(
                    "status", item.get("attendanceType", "Unknown")
                )
                if status.lower() not in ["present", "on time"]:
                    msg = self.notifier.format_attendance_alert(
                        student_name=name,
                        date=item.get("date", "N/A"),
                        status=status,
                        period=item.get("period", ""),
                    )
                    await self._send_notification(msg)

        elif category == "notifications" and self.notify_notifications:
            for item in added:
                msg = self.notifier.format_notification(
                    title=item.get("title", "School Notification"),
                    body=item.get(
                        "message", item.get("body", "New notification")
                    ),
                )
                await self._send_notification(msg)

    async def _send_notification(self, message: str) -> None:
        """Send a notification with error handling."""
        if not self.notifier:
            return
        try:
            result = await self.notifier.send_message(message)
            if result["success"]:
                self.notifications_sent += 1
            else:
                logger.warning(f"Notification failed: {result['error']}")
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    async def _check_daily_summary(self) -> None:
        """Send daily summary if it's time."""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        if (
            now.hour == self.daily_summary_hour
            and self._last_summary_date != today
            and self.notifier
            and self.latest_data
        ):
            self._last_summary_date = today

            students = self.latest_data.get("students", [])
            for student in students:
                sid = str(
                    student.get("personID", student.get("studentID", ""))
                )
                name = self._student_names.get(sid, f"Student {sid}")

                # Count courses for this student
                courses = [
                    c
                    for c in self.latest_data.get("courses", [])
                    if str(c.get("studentID", "")) == sid
                ]
                assignments = [
                    a
                    for a in self.latest_data.get("assignments", [])
                    if str(a.get("studentID", "")) == sid
                ]
                grades = [
                    g
                    for g in self.latest_data.get("grades", [])
                    if str(g.get("studentID", "")) == sid
                ]
                gpa_data = [
                    g
                    for g in self.latest_data.get("gpa", [])
                    if str(g.get("studentID", "")) == sid
                ]

                gpa_str = ""
                if gpa_data:
                    gpa_str = str(
                        gpa_data[0].get(
                            "cumulativeGPA",
                            gpa_data[0].get("gpa", ""),
                        )
                    )

                # Count assignments due today or upcoming
                due_count = 0
                for a in assignments:
                    due = a.get("dueDate", "")
                    if due and today in due:
                        due_count += 1

                msg = self.notifier.format_daily_summary(
                    student_name=name,
                    courses_count=len(courses),
                    assignments_due=due_count,
                    new_grades=len(grades),
                    gpa=gpa_str,
                )
                await self._send_notification(msg)

    def get_status(self) -> dict:
        """Get current scheduler status."""
        return {
            "running": self._running,
            "poll_count": self.poll_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_poll": self._last_poll.isoformat() if self._last_poll else None,
            "notifications_sent": self.notifications_sent,
            "poll_interval": self.poll_interval,
            "students_found": len(self._student_names),
            "student_names": list(self._student_names.values()),
        }

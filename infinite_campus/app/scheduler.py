"""
Data Polling Scheduler.

Periodically fetches data from Infinite Campus, detects changes,
triggers WhatsApp notifications for updates, and pushes data to
Home Assistant entities.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from infinite_campus_api import InfiniteCampusAPI
from whatsapp_notify import WhatsAppNotifier
from ha_entities import HAEntityManager

logger = logging.getLogger(__name__)

DATA_DIR = Path("/data")
CACHE_FILE = DATA_DIR / "ic_cache.json"


class ChangeDetector:
    """Detects changes between cached and fresh data from Infinite Campus."""

    def __init__(self):
        self._cache: dict[str, Any] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, "r") as f:
                    self._cache = json.load(f)
                logger.info("Loaded cached data from disk")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load cache: {e}")
                self._cache = {}

    def _save_cache(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump(self._cache, f, indent=2, default=str)
        except IOError as e:
            logger.error(f"Failed to save cache: {e}")

    def detect_changes(self, category: str, new_data: list[dict]) -> dict:
        """Compare new data against cached data and detect changes."""
        old_data = self._cache.get(category, [])

        def make_key(item: dict) -> str:
            key_fields = [
                "assignmentID", "objectSectionID", "courseID", "studentID",
                "personID", "termID", "attendanceID", "notificationID", "id",
                "assignmentName", "courseName", "date", "sectionID",
            ]
            parts = []
            for field in key_fields:
                if field in item:
                    parts.append(f"{field}={item[field]}")
            return "|".join(parts) if parts else json.dumps(item, sort_keys=True, default=str)

        old_map = {make_key(item): item for item in old_data if isinstance(item, dict)}
        new_map = {make_key(item): item for item in new_data if isinstance(item, dict)}

        added = [new_map[k] for k in new_map if k not in old_map]
        removed = [old_map[k] for k in old_map if k not in new_map]
        modified = []
        for k in new_map:
            if k in old_map:
                if json.dumps(new_map[k], sort_keys=True, default=str) != json.dumps(
                    old_map[k], sort_keys=True, default=str
                ):
                    modified.append({"new": new_map[k], "old": old_map[k]})

        # Update cache
        self._cache[category] = new_data
        self._save_cache()

        return {"added": added, "modified": modified, "removed": removed}

    def clear_cache(self) -> None:
        self._cache = {}
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()


class ICScheduler:
    """
    Polls Infinite Campus at regular intervals, sends WhatsApp
    notifications on changes, and updates HA entities.
    """

    def __init__(
        self,
        api: InfiniteCampusAPI,
        notifier: Optional[WhatsAppNotifier],
        poll_interval: int = 900,
        notify_grades: bool = True,
        notify_assignments: bool = True,
        notify_attendance: bool = True,
        notify_notifications: bool = True,
        daily_summary: bool = True,
        daily_summary_hour: int = 18,
    ):
        self.api = api
        self.notifier = notifier
        self.poll_interval = max(300, poll_interval)
        self.notify_grades = notify_grades
        self.notify_assignments = notify_assignments
        self.notify_attendance = notify_attendance
        self.notify_notifications = notify_notifications
        self.daily_summary = daily_summary
        self.daily_summary_hour = daily_summary_hour

        self._detector = ChangeDetector()
        self._ha_entities = HAEntityManager()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_poll: Optional[datetime] = None
        self._last_summary_date: Optional[str] = None
        self._student_names: dict[str, str] = {}
        self._first_poll = True  # Skip notifications on first poll to avoid flood

        # Web UI state
        self.latest_data: dict[str, Any] = {}
        self.poll_count = 0
        self.error_count = 0
        self.last_error: Optional[str] = None
        self.notifications_sent = 0

    async def start(self) -> None:
        if self._running:
            logger.warning("Scheduler is already running")
            return

        self._running = True
        logger.info(f"Starting scheduler with {self.poll_interval}s interval")

        try:
            await self.api.authenticate()
            logger.info("Initial authentication successful")
        except Exception as e:
            logger.error(f"Initial authentication failed: {e}")
            self.last_error = str(e)

        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._ha_entities.close()
        logger.info("Scheduler stopped")

    async def poll_now(self) -> dict[str, Any]:
        return await self._do_poll()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._do_poll()
                if self.daily_summary:
                    await self._check_daily_summary()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.error_count += 1
                self.last_error = str(e)
                logger.error(f"Poll error: {e}")

            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def _do_poll(self) -> dict[str, Any]:
        logger.info("Starting poll cycle...")
        self.poll_count += 1

        try:
            data = await self.api.get_all_data()
            self.latest_data = data
            self._last_poll = datetime.now(timezone.utc)

            # Build student name lookup
            for student in data.get("students", []):
                sid = str(student.get("personID", student.get("studentID", "")))
                first = student.get("firstName", "")
                last = student.get("lastName", "")
                name = f"{first} {last}".strip() or f"Student {sid}"
                self._student_names[sid] = name

            # Detect changes and send notifications
            for category in ["grades", "assignments", "attendance", "notifications"]:
                cat_data = data.get(category, [])
                if cat_data and isinstance(cat_data, list):
                    changes = self._detector.detect_changes(category, cat_data)
                    if self._first_poll:
                        added_count = len(changes.get("added", []))
                        if added_count > 0:
                            logger.info(f"First poll: skipping {added_count} {category} notifications (baseline)")
                    else:
                        await self._process_changes(category, changes)

            if self._first_poll:
                self._first_poll = False
                logger.info("First poll complete — future changes will trigger notifications")

            # Cache other categories without notifications
            for category in ["courses", "terms", "schedule"]:
                cat_data = data.get(category, [])
                if cat_data and isinstance(cat_data, list):
                    self._detector.detect_changes(category, cat_data)

            # Update HA entities
            try:
                entity_count = await self._ha_entities.update_from_data(
                    data, self._student_names
                )
                if entity_count > 0:
                    logger.info(f"Updated {entity_count} HA entities")
            except Exception as e:
                logger.warning(f"HA entity update error: {e}")

            logger.info(
                f"Poll complete. Students: {len(data.get('students', []))}, "
                f"Courses: {len(data.get('courses', []))}, "
                f"Grades: {len(data.get('grades', []))}, "
                f"Assignments: {len(data.get('assignments', []))}"
            )

            return data

        except Exception as e:
            self.error_count += 1
            self.last_error = str(e)
            logger.error(f"Poll failed: {e}")
            raise

    async def _process_changes(self, category: str, changes: dict) -> None:
        """Process detected changes and send notifications."""
        if not self.notifier:
            return

        added = changes.get("added", [])
        modified = changes.get("modified", [])

        if not added and not modified:
            return

        logger.info(f"Changes detected in {category}: {len(added)} added, {len(modified)} modified")

        if category == "grades" and self.notify_grades:
            await self._notify_grade_changes(added, modified)

        elif category == "assignments" and self.notify_assignments:
            await self._notify_assignment_changes(added, modified)

        elif category == "attendance" and self.notify_attendance:
            await self._notify_attendance_changes(added)

        elif category == "notifications" and self.notify_notifications:
            for item in added:
                msg = self.notifier.format_notification(
                    title=item.get("title", "School Notification"),
                    body=item.get("message", item.get("body", "New notification")),
                )
                await self._send_notification(msg)

    async def _notify_grade_changes(self, added: list, modified: list) -> None:
        """Send notifications for grade changes."""
        for item in added + [m["new"] for m in modified if isinstance(m, dict) and "new" in m]:
            sid = str(item.get("studentID", ""))
            name = self._student_names.get(sid, f"Student {sid}")
            pct = item.get("progressPercent") or item.get("percent") or ""
            score = item.get("progressScore") or item.get("score") or ""
            total = str(item.get("progressTotalPoints") or item.get("totalPoints") or "")
            earned = str(item.get("progressPointsEarned") or item.get("pointsEarned") or "")

            # Derive letter grade
            letter = ""
            if pct:
                try:
                    p = float(pct)
                    if p >= 93: letter = "A"
                    elif p >= 90: letter = "A-"
                    elif p >= 87: letter = "B+"
                    elif p >= 83: letter = "B"
                    elif p >= 80: letter = "B-"
                    elif p >= 77: letter = "C+"
                    elif p >= 73: letter = "C"
                    elif p >= 70: letter = "C-"
                    elif p >= 67: letter = "D+"
                    elif p >= 60: letter = "D"
                    else: letter = "F"
                except (ValueError, TypeError):
                    pass

            score_str = earned + "/" + total if earned and total else score or "N/A"
            try:
                pct_str = f" ({float(pct):.1f}%)" if pct else ""
            except (ValueError, TypeError):
                pct_str = ""

            msg = (
                f"📚 *Grade Update*\n\n"
                f"👤 *{name}*\n"
                f"📖 {item.get('courseName', 'Unknown Course')}\n"
                f"📊 Score: *{score_str}{pct_str}*"
            )
            if letter:
                msg += f" — *{letter}*"
            if item.get("termName"):
                msg += f"\n📅 Term: {item['termName']}"
            msg += f"\n\n🕐 {datetime.now().strftime('%b %d, %I:%M %p')}"
            await self._send_notification(msg)

    async def _notify_assignment_changes(self, added: list, modified: list) -> None:
        """Send notifications for assignment changes."""
        for item in added:
            sid = str(item.get("studentID", item.get("personID", "")))
            name = self._student_names.get(sid, f"Student {sid}")

            # Check for specific statuses
            if item.get("missing"):
                msg = (
                    f"⚠️ *Missing Assignment*\n\n"
                    f"👤 *{name}*\n"
                    f"📖 {item.get('courseName', '')}\n"
                    f"📝 {item.get('assignmentName', '')}\n"
                    f"📅 Due: {item.get('dueDate', 'N/A')}\n\n"
                    f"🕐 {datetime.now().strftime('%b %d, %I:%M %p')}"
                )
                await self._send_notification(msg)
            elif item.get("score") is not None and item.get("score") != "":
                # New graded assignment
                score_str = f"{item['score']}/{item.get('totalPoints', '?')}"
                pct = item.get("scorePercentage")
                try:
                    pct_str = f" ({float(pct):.0f}%)" if pct is not None else ""
                except (ValueError, TypeError):
                    pct_str = ""
                msg = (
                    f"✅ *Assignment Graded*\n\n"
                    f"👤 *{name}*\n"
                    f"📖 {item.get('courseName', '')}\n"
                    f"📝 {item.get('assignmentName', '')}\n"
                    f"📊 Score: *{score_str}{pct_str}*\n\n"
                    f"🕐 {datetime.now().strftime('%b %d, %I:%M %p')}"
                )
                await self._send_notification(msg)
            else:
                # New assignment posted
                msg = self.notifier.format_assignment_alert(
                    student_name=name,
                    course_name=item.get("courseName", ""),
                    assignment_name=item.get("assignmentName", ""),
                    due_date=item.get("dueDate", "N/A"),
                )
                await self._send_notification(msg)

        # Check modified assignments for newly graded or newly missing
        for change in modified:
            if not isinstance(change, dict) or "new" not in change:
                continue
            new_item = change["new"]
            old_item = change.get("old", {})
            sid = str(new_item.get("studentID", new_item.get("personID", "")))
            name = self._student_names.get(sid, f"Student {sid}")

            # Newly graded (had no score before, now has score)
            old_score = old_item.get("score")
            new_score = new_item.get("score")
            if (old_score is None or old_score == "") and new_score is not None and new_score != "":
                score_str = f"{new_score}/{new_item.get('totalPoints', '?')}"
                pct = new_item.get("scorePercentage")
                try:
                    pct_str = f" ({float(pct):.0f}%)" if pct is not None else ""
                except (ValueError, TypeError):
                    pct_str = ""
                msg = (
                    f"✅ *Assignment Graded*\n\n"
                    f"👤 *{name}*\n"
                    f"📖 {new_item.get('courseName', '')}\n"
                    f"📝 {new_item.get('assignmentName', '')}\n"
                    f"📊 Score: *{score_str}{pct_str}*\n\n"
                    f"🕐 {datetime.now().strftime('%b %d, %I:%M %p')}"
                )
                await self._send_notification(msg)

            # Newly missing
            if not old_item.get("missing") and new_item.get("missing"):
                msg = (
                    f"⚠️ *Assignment Now Missing*\n\n"
                    f"👤 *{name}*\n"
                    f"📖 {new_item.get('courseName', '')}\n"
                    f"📝 {new_item.get('assignmentName', '')}\n\n"
                    f"🕐 {datetime.now().strftime('%b %d, %I:%M %p')}"
                )
                await self._send_notification(msg)

            # Score changed
            if old_score is not None and new_score is not None and old_score != new_score:
                msg = (
                    f"📝 *Score Updated*\n\n"
                    f"👤 *{name}*\n"
                    f"📖 {new_item.get('courseName', '')}\n"
                    f"📝 {new_item.get('assignmentName', '')}\n"
                    f"📊 {old_score} → *{new_score}*/{new_item.get('totalPoints', '?')}\n\n"
                    f"🕐 {datetime.now().strftime('%b %d, %I:%M %p')}"
                )
                await self._send_notification(msg)

    async def _notify_attendance_changes(self, added: list) -> None:
        """Send notifications for attendance changes (non-present only)."""
        for item in added:
            sid = str(item.get("studentID", ""))
            name = self._student_names.get(sid, f"Student {sid}")
            status = item.get("status", item.get("attendanceType", "Unknown"))
            if status.lower() not in ["present", "on time", ""]:
                msg = (
                    f"🏫 *Attendance Alert*\n\n"
                    f"👤 *{name}*\n"
                    f"📅 {item.get('date', 'N/A')}\n"
                    f"📋 Status: *{status}*"
                )
                period = item.get("period", item.get("periodName", ""))
                if period:
                    msg += f"\n⏰ Period: {period}"
                msg += f"\n\n🕐 {datetime.now().strftime('%b %d, %I:%M %p')}"
                await self._send_notification(msg)

    async def _send_notification(self, message: str) -> None:
        if not self.notifier:
            return
        try:
            result = await self.notifier.send_message(message)
            if result.get("success"):
                self.notifications_sent += 1
            else:
                logger.warning(f"Notification failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    def _load_summary_date(self) -> Optional[str]:
        """Load last summary date from disk to survive restarts."""
        summary_file = DATA_DIR / "last_summary_date.txt"
        try:
            if summary_file.exists():
                return summary_file.read_text().strip()
        except IOError:
            pass
        return None

    def _save_summary_date(self, date_str: str) -> None:
        """Persist last summary date to disk."""
        summary_file = DATA_DIR / "last_summary_date.txt"
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            summary_file.write_text(date_str)
        except IOError as e:
            logger.error(f"Failed to save summary date: {e}")

    async def _check_daily_summary(self, force: bool = False) -> None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        if not force:
            # Load persisted date if we don't have one in memory
            if self._last_summary_date is None:
                self._last_summary_date = self._load_summary_date()

            # Check if we're at or past the summary hour and haven't sent today
            if now.hour < self.daily_summary_hour:
                return
            if self._last_summary_date == today:
                return

        if not self.notifier or not self.latest_data:
            logger.debug(f"Daily summary skipped: notifier={bool(self.notifier)}, data={bool(self.latest_data)}")
            return

        logger.info(f"Sending daily summary for {today} (hour={now.hour}, target={self.daily_summary_hour})")
        self._last_summary_date = today
        self._save_summary_date(today)

        for sid, name in self._student_names.items():
            assignments = [
                a for a in self.latest_data.get("assignments", [])
                if str(a.get("studentID", a.get("personID", ""))) == sid
            ]
            grades = [
                g for g in self.latest_data.get("grades", [])
                if str(g.get("studentID", "")) == sid
            ]
            courses = [
                c for c in self.latest_data.get("courses", [])
                if str(c.get("studentID", c.get("personID", ""))) == sid
            ]

            missing = len([a for a in assignments if a.get("missing")])
            due_today = 0
            for a in assignments:
                due = a.get("dueDate", "")
                if due and today in due:
                    due_today += 1

            # Calculate average grade
            pcts = []
            for g in grades:
                pct = g.get("progressPercent") or g.get("percent") or ""
                try:
                    pcts.append(float(pct))
                except (ValueError, TypeError):
                    pass
            avg_str = f"\n📈 Average: *{sum(pcts)/len(pcts):.1f}%*" if pcts else ""

            msg = (
                f"📊 *Daily Summary*\n\n"
                f"👤 *{name}*\n"
                f"📅 {now.strftime('%A, %B %d, %Y')}\n\n"
                f"📖 Courses: {len(courses)}\n"
                f"📝 Assignments Due Today: {due_today}\n"
                f"⚠️ Missing Assignments: {missing}\n"
                f"✅ Total Grades: {len(grades)}"
                f"{avg_str}\n\n"
                f"🕐 {now.strftime('%I:%M %p')}"
            )
            await self._send_notification(msg)
            logger.info(f"Daily summary sent for {name}")

    def get_status(self) -> dict:
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
            "ha_entities_count": self._ha_entities.entity_count,
        }

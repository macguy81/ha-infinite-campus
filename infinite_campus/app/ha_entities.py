"""
Home Assistant Entity Integration.

Creates and updates HA sensors via the Supervisor API so that
Infinite Campus data is available for HA automations, dashboards,
and notifications.

Entities created per student:
  - sensor.infinite_campus_{name}_grade_{course}  (per course)
  - sensor.infinite_campus_{name}_missing_assignments
  - sensor.infinite_campus_{name}_upcoming_assignments
  - sensor.infinite_campus_{name}_attendance
  - sensor.infinite_campus_{name}_gpa

Global entities:
  - sensor.infinite_campus_last_updated
  - binary_sensor.infinite_campus_connected

Uses the Supervisor API at http://supervisor/core/api/states/...
with the SUPERVISOR_TOKEN env var for auth.
"""

import logging
import os
import re
from datetime import datetime
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

SUPERVISOR_API = "http://supervisor/core/api"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def _slugify(text: str) -> str:
    """Convert text to a HA-friendly entity slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text[:64]  # HA has limits


class HAEntityManager:
    """Manages Home Assistant entities via the Supervisor API."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._headers = {
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        }
        self._entities_created: list[str] = []
        self._enabled = bool(SUPERVISOR_TOKEN)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _set_state(
        self,
        entity_id: str,
        state: str,
        attributes: dict[str, Any],
    ) -> bool:
        """Set or update a HA entity state via Supervisor API."""
        if not self._enabled:
            return False

        session = await self._ensure_session()
        url = f"{SUPERVISOR_API}/states/{entity_id}"
        payload = {
            "state": str(state),
            "attributes": attributes,
        }

        try:
            async with session.post(
                url, json=payload, headers=self._headers
            ) as resp:
                if resp.status in (200, 201):
                    if entity_id not in self._entities_created:
                        self._entities_created.append(entity_id)
                    return True
                else:
                    text = await resp.text()
                    logger.warning(
                        f"Failed to set {entity_id}: HTTP {resp.status} - {text[:200]}"
                    )
                    return False
        except Exception as e:
            logger.debug(f"HA API error for {entity_id}: {e}")
            return False

    async def update_from_data(
        self,
        data: dict[str, Any],
        student_names: dict[str, str],
    ) -> int:
        """
        Update all HA entities from fetched IC data.
        Returns count of entities updated.
        """
        if not self._enabled:
            logger.debug("HA entity updates disabled (no SUPERVISOR_TOKEN)")
            return 0

        count = 0

        # ── Global: connection status ──
        ok = await self._set_state(
            "binary_sensor.infinite_campus_connected",
            "on",
            {
                "friendly_name": "Infinite Campus Connected",
                "device_class": "connectivity",
                "icon": "mdi:school",
                "students": len(student_names),
                "last_updated": data.get("last_updated", ""),
            },
        )
        if ok:
            count += 1

        # ── Global: last updated timestamp ──
        ok = await self._set_state(
            "sensor.infinite_campus_last_updated",
            data.get("last_updated", datetime.now().isoformat()),
            {
                "friendly_name": "IC Last Updated",
                "icon": "mdi:clock-outline",
                "device_class": "timestamp",
                "students_count": len(student_names),
                "courses_count": len(data.get("courses", [])),
                "assignments_count": len(data.get("assignments", [])),
                "grades_count": len(data.get("grades", [])),
            },
        )
        if ok:
            count += 1

        # ── Per-student entities ──
        for sid, name in student_names.items():
            slug = _slugify(name)
            count += await self._update_student_entities(
                sid, name, slug, data
            )

        logger.info(f"Updated {count} HA entities")
        return count

    async def _update_student_entities(
        self,
        student_id: str,
        student_name: str,
        slug: str,
        data: dict[str, Any],
    ) -> int:
        count = 0

        # Filter data for this student
        assignments = [
            a for a in data.get("assignments", [])
            if str(a.get("studentID", a.get("personID", ""))) == student_id
        ]
        grades = [
            g for g in data.get("grades", [])
            if str(g.get("studentID", "")) == student_id
        ]
        courses = [
            c for c in data.get("courses", [])
            if str(c.get("studentID", c.get("personID", ""))) == student_id
        ]

        # ── Missing assignments count ──
        missing = [a for a in assignments if a.get("missing")]
        late = [a for a in assignments if a.get("late")]
        ok = await self._set_state(
            f"sensor.infinite_campus_{slug}_missing_assignments",
            str(len(missing)),
            {
                "friendly_name": f"{student_name} Missing Assignments",
                "icon": "mdi:alert-circle",
                "unit_of_measurement": "assignments",
                "student_id": student_id,
                "missing_list": [
                    {
                        "name": a.get("assignmentName", ""),
                        "course": a.get("courseName", ""),
                        "due": a.get("dueDate", ""),
                    }
                    for a in missing[:10]
                ],
                "late_count": len(late),
            },
        )
        if ok:
            count += 1

        # ── Upcoming assignments (due in next 7 days) ──
        now = datetime.now()
        upcoming = []
        for a in assignments:
            due = a.get("dueDate", "")
            if due:
                try:
                    due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                    days_until = (due_dt.replace(tzinfo=None) - now).days
                    if 0 <= days_until <= 7:
                        upcoming.append({
                            "name": a.get("assignmentName", ""),
                            "course": a.get("courseName", ""),
                            "due": due,
                            "days_until": days_until,
                        })
                except (ValueError, TypeError):
                    pass

        ok = await self._set_state(
            f"sensor.infinite_campus_{slug}_upcoming_assignments",
            str(len(upcoming)),
            {
                "friendly_name": f"{student_name} Upcoming Assignments",
                "icon": "mdi:calendar-clock",
                "unit_of_measurement": "assignments",
                "student_id": student_id,
                "assignments": upcoming[:15],
            },
        )
        if ok:
            count += 1

        # ── Total assignments ──
        ok = await self._set_state(
            f"sensor.infinite_campus_{slug}_total_assignments",
            str(len(assignments)),
            {
                "friendly_name": f"{student_name} Total Assignments",
                "icon": "mdi:notebook",
                "unit_of_measurement": "assignments",
                "student_id": student_id,
                "graded": len([a for a in assignments if a.get("score") is not None and a.get("score") != ""]),
                "turned_in": len([a for a in assignments if a.get("turnedIn")]),
            },
        )
        if ok:
            count += 1

        # ── Course count ──
        ok = await self._set_state(
            f"sensor.infinite_campus_{slug}_courses",
            str(len(courses)),
            {
                "friendly_name": f"{student_name} Courses",
                "icon": "mdi:book-open-variant",
                "unit_of_measurement": "courses",
                "student_id": student_id,
                "course_list": [c.get("courseName", "") for c in courses[:20]],
            },
        )
        if ok:
            count += 1

        # ── Per-course grade sensors ──
        for g in grades:
            course_name = g.get("courseName", "")
            if not course_name:
                continue
            course_slug = _slugify(course_name)
            pct = g.get("progressPercent") or g.get("percent") or g.get("percentage") or ""
            score_val = g.get("progressScore") or g.get("score") or ""
            letter = g.get("letterGrade", "")

            if pct:
                try:
                    pct_f = float(pct)
                    if not letter:
                        if pct_f >= 93: letter = "A"
                        elif pct_f >= 90: letter = "A-"
                        elif pct_f >= 87: letter = "B+"
                        elif pct_f >= 83: letter = "B"
                        elif pct_f >= 80: letter = "B-"
                        elif pct_f >= 77: letter = "C+"
                        elif pct_f >= 73: letter = "C"
                        elif pct_f >= 70: letter = "C-"
                        elif pct_f >= 67: letter = "D+"
                        elif pct_f >= 60: letter = "D"
                        else: letter = "F"
                except (ValueError, TypeError):
                    pass

            display_state = letter if letter else (str(pct) + "%" if pct else score_val or "N/A")

            ok = await self._set_state(
                f"sensor.infinite_campus_{slug}_{course_slug}",
                display_state,
                {
                    "friendly_name": f"{student_name} - {course_name}",
                    "icon": "mdi:school",
                    "student_id": student_id,
                    "course_name": course_name,
                    "score": score_val,
                    "percent": str(pct),
                    "letter_grade": letter,
                    "teacher": g.get("teacherDisplay", ""),
                    "term": g.get("termName", ""),
                    "total_points": str(g.get("progressTotalPoints") or g.get("totalPoints") or ""),
                    "points_earned": str(g.get("progressPointsEarned") or g.get("pointsEarned") or ""),
                },
            )
            if ok:
                count += 1

        return count

    @property
    def entity_count(self) -> int:
        return len(self._entities_created)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

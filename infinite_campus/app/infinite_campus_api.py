"""
Infinite Campus Parent Portal API Client.

Handles authentication and data retrieval from the Infinite Campus
Parent Portal API. Supports fetching students, courses, assignments,
grades, attendance, schedule, and notifications.
"""

import asyncio
import logging
import re
import json
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, parse_qs

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class InfiniteCampusError(Exception):
    """Base exception for Infinite Campus API errors."""
    pass


class AuthenticationError(InfiniteCampusError):
    """Raised when authentication fails."""
    pass


class APIError(InfiniteCampusError):
    """Raised when an API call fails."""
    pass


class InfiniteCampusAPI:
    """
    Async client for the Infinite Campus Parent Portal.

    Authenticates via the parent portal login and fetches data
    using prism API endpoints and REST API endpoints.
    """

    def __init__(
        self,
        base_url: str,
        district: str,
        username: str,
        password: str,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.district = district
        self.username = username
        self.password = password
        self._session = session
        self._owns_session = session is None
        self._authenticated = False
        self._person_id: Optional[str] = None
        self._student_ids: list[str] = []
        self._student_data: list[dict] = []
        self._calendar_ids: list[str] = []
        self._school_ids: list[str] = []
        self._cookies: Optional[aiohttp.CookieJar] = None
        self._auth_headers: dict[str, str] = {}
        self._last_auth: Optional[datetime] = None
        self._portal_outline: Optional[dict] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Create or return the aiohttp session."""
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(
                cookie_jar=jar,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
            self._owns_session = True
        return self._session

    async def authenticate(self) -> bool:
        """
        Authenticate with the Infinite Campus parent portal.
        """
        session = await self._ensure_session()

        try:
            # Step 1: Load the login page
            login_url = f"{self.base_url}/campus/portal/parents/{self.district}.jsp"
            logger.info(f"Loading login page: {login_url}")

            async with session.get(login_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise AuthenticationError(
                        f"Failed to load login page: HTTP {resp.status}"
                    )
                login_html = await resp.text()

            # Step 2: Submit login credentials
            auth_url = f"{self.base_url}/campus/verify.jsp"
            login_data = {
                "appName": self.district,
                "username": self.username,
                "password": self.password,
                "portalLoginPage": f"portal/parents/{self.district}.jsp",
            }

            logger.info("Submitting login credentials...")
            async with session.post(
                auth_url,
                data=login_data,
                allow_redirects=True,
            ) as resp:
                auth_html = await resp.text()

                if resp.status != 200:
                    raise AuthenticationError(
                        f"Login request failed: HTTP {resp.status}"
                    )

                if any(
                    indicator in auth_html.lower()
                    for indicator in [
                        "invalid username",
                        "invalid password",
                        "login failed",
                        "incorrect credentials",
                        "authentication failed",
                    ]
                ):
                    raise AuthenticationError("Invalid username or password")

            # Step 3: Navigate to portal home
            portal_url = f"{self.base_url}/campus/portal/students/portal.html"
            async with session.get(portal_url, allow_redirects=True) as resp:
                portal_html = await resp.text()

            # Step 4: Get portal outline (contains students, terms, calendars)
            await self._fetch_portal_outline(session)

            # Step 5: Get student list from API
            await self._fetch_students(session)

            self._authenticated = True
            self._last_auth = datetime.now()

            logger.info(
                f"Authentication successful. Found {len(self._student_ids)} student(s)"
            )
            return True

        except AuthenticationError:
            raise
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            raise AuthenticationError(f"Authentication failed: {e}")

    async def _fetch_portal_outline(self, session: aiohttp.ClientSession) -> None:
        """Fetch the portal outline which contains calendar/term info."""
        try:
            url = f"{self.base_url}/campus/prism?x=portal.PortalOutline&lang=en"
            async with session.get(url) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    try:
                        self._portal_outline = json.loads(text)
                        logger.info(f"Portal outline keys: {list(self._portal_outline.keys()) if isinstance(self._portal_outline, dict) else 'not a dict'}")

                        # Extract calendar IDs and school IDs
                        if isinstance(self._portal_outline, dict):
                            for student in self._portal_outline.get("StudentList", []):
                                for school in student.get("SchoolList", []):
                                    school_id = str(school.get("schoolID", ""))
                                    if school_id and school_id not in self._school_ids:
                                        self._school_ids.append(school_id)
                                    for calendar in school.get("CalendarList", []):
                                        cal_id = str(calendar.get("calendarID", ""))
                                        if cal_id and cal_id not in self._calendar_ids:
                                            self._calendar_ids.append(cal_id)

                            logger.info(f"Found calendar IDs: {self._calendar_ids}")
                            logger.info(f"Found school IDs: {self._school_ids}")
                    except json.JSONDecodeError:
                        logger.warning("Portal outline response was not JSON")
                        logger.debug(f"Portal outline text: {text[:500]}")
                else:
                    logger.warning(f"Portal outline returned HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"Failed to fetch portal outline: {e}")

    async def _fetch_students(self, session: aiohttp.ClientSession) -> None:
        """Fetch student list from API and extract enrollment info."""
        try:
            url = f"{self.base_url}/campus/api/portal/students"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        self._student_data = data
                        self._student_ids = [
                            str(s.get("personID", s.get("studentID", "")))
                            for s in data
                            if s.get("personID") or s.get("studentID")
                        ]
                        logger.info(f"Found students via API: {self._student_ids}")
                        if data:
                            logger.info(f"Student data keys: {list(data[0].keys())}")

                        # Extract calendar and school IDs from enrollments
                        for student in data:
                            for enrollment in student.get("enrollments", []):
                                cal_id = str(enrollment.get("calendarID", enrollment.get("calendarId", "")))
                                school_id = str(enrollment.get("schoolID", enrollment.get("schoolId", "")))
                                struct_id = str(enrollment.get("structureID", enrollment.get("structureId", "")))
                                if cal_id and cal_id not in self._calendar_ids:
                                    self._calendar_ids.append(cal_id)
                                if school_id and school_id not in self._school_ids:
                                    self._school_ids.append(school_id)
                                # Log enrollment keys for debugging
                                logger.info(f"Enrollment keys: {list(enrollment.keys())}")
                                logger.info(f"Enrollment data: calendarID={cal_id}, schoolID={school_id}, structureID={struct_id}")

                        logger.info(f"Extracted calendar IDs: {self._calendar_ids}")
                        logger.info(f"Extracted school IDs: {self._school_ids}")
                        return
        except Exception as e:
            logger.debug(f"API student fetch failed: {e}")

        # Fallback: extract from portal outline
        if self._portal_outline and isinstance(self._portal_outline, dict):
            students = self._portal_outline.get("StudentList", [])
            if students:
                self._student_ids = [str(s.get("personID", "")) for s in students]
                logger.info(f"Found students via outline: {self._student_ids}")
                return

        logger.warning("Could not extract student IDs automatically")

    async def _ensure_authenticated(self) -> None:
        """Re-authenticate if session has expired (>30 min)."""
        if not self._authenticated or (
            self._last_auth
            and datetime.now() - self._last_auth > timedelta(minutes=30)
        ):
            await self.authenticate()

    async def _safe_get(self, session: aiohttp.ClientSession, url: str,
                        params: Optional[dict] = None) -> Optional[Any]:
        """Make a GET request and return parsed JSON (dict or list) or None on failure.
        Never returns raw strings — only structured data or None."""
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                        # Only return structured data (dict/list), not primitives
                        if isinstance(data, (dict, list)):
                            return data
                        return None
                    except json.JSONDecodeError:
                        # Response was HTML or plain text, not JSON
                        return None
                elif resp.status == 401:
                    self._authenticated = False
                    await self.authenticate()
                    async with session.get(url, params=params) as resp2:
                        if resp2.status == 200:
                            try:
                                data = await resp2.json()
                                if isinstance(data, (dict, list)):
                                    return data
                            except Exception:
                                pass
                logger.debug(f"GET {url} returned HTTP {resp.status}")
                return None
        except Exception as e:
            logger.debug(f"GET {url} failed: {e}")
            return None

    # ─── Endpoint Discovery ──────────────────────────────────────

    async def discover_endpoints(self) -> dict[str, Any]:
        """
        Try multiple known IC API endpoint patterns and report which ones work.
        This helps debug which endpoints are available for this district.
        """
        await self._ensure_authenticated()
        session = await self._ensure_session()
        results = {}

        sid = self._student_ids[0] if self._student_ids else ""
        cal_id = self._calendar_ids[0] if self._calendar_ids else ""
        school_id = self._school_ids[0] if self._school_ids else ""

        # List of endpoints to try
        endpoints = {
            # REST API patterns
            "api_students": f"{self.base_url}/campus/api/portal/students",
            "api_student_detail": f"{self.base_url}/campus/api/portal/students/{sid}" if sid else None,

            # Prism API patterns (most universal)
            "prism_outline": (f"{self.base_url}/campus/prism", {"x": "portal.PortalOutline", "lang": "en"}),
            "prism_grades": (f"{self.base_url}/campus/prism", {"x": "portal.PortalGrades", "studentID": sid, "lang": "en"}) if sid else None,
            "prism_grades_cal": (f"{self.base_url}/campus/prism", {"x": "portal.PortalGrades", "studentID": sid, "calendarID": cal_id, "lang": "en"}) if sid and cal_id else None,
            "prism_assignments": (f"{self.base_url}/campus/prism", {"x": "portal.PortalAssignments", "studentID": sid, "lang": "en"}) if sid else None,
            "prism_attendance": (f"{self.base_url}/campus/prism", {"x": "portal.PortalAttendance", "studentID": sid, "lang": "en"}) if sid else None,
            "prism_schedule": (f"{self.base_url}/campus/prism", {"x": "portal.PortalSchedule", "studentID": sid, "lang": "en"}) if sid else None,
            "prism_notifications": (f"{self.base_url}/campus/prism", {"x": "portal.PortalNotifications", "lang": "en"}),

            # Resource API patterns
            "res_grades": (f"{self.base_url}/campus/resources/portal/grades", {"studentID": sid, "schoolID": school_id, "calendarID": cal_id}) if sid else None,
            "res_assignments": (f"{self.base_url}/campus/resources/portal/assignments", {"studentID": sid}) if sid else None,

            # Newer API patterns
            "api_grades": (f"{self.base_url}/campus/api/portal/grades", {"studentID": sid}) if sid else None,
            "api_assignments": (f"{self.base_url}/campus/api/portal/assignment/student/{sid}") if sid else None,
            "api_attendance": (f"{self.base_url}/campus/api/portal/attendance", {"studentID": sid}) if sid else None,
            "api_schedule": (f"{self.base_url}/campus/api/portal/schedule", {"studentID": sid}) if sid else None,
            "api_notifications": f"{self.base_url}/campus/api/portal/notifications",
            "api_announcements": f"{self.base_url}/campus/api/portal/announcements",

            # Additional patterns
            "api_displaygrades": f"{self.base_url}/campus/api/portal/displaygrades/{sid}" if sid else None,
            "api_gradebook": f"{self.base_url}/campus/api/portal/gradebook/student/{sid}" if sid else None,

            # Enrollment-based patterns (newer IC versions)
            "api_grades_cal": (f"{self.base_url}/campus/api/portal/grades", {"studentID": sid, "calendarID": cal_id}) if sid and cal_id else None,
            "api_assignments_cal": (f"{self.base_url}/campus/api/portal/assignments", {"studentID": sid, "calendarID": cal_id}) if sid and cal_id else None,
            "api_schedule_cal": (f"{self.base_url}/campus/api/portal/schedule", {"studentID": sid, "calendarID": cal_id}) if sid and cal_id else None,
            "api_attendance_cal": (f"{self.base_url}/campus/api/portal/attendance", {"studentID": sid, "calendarID": cal_id}) if sid and cal_id else None,

            # Section-based patterns
            "api_student_sections": f"{self.base_url}/campus/api/portal/students/{sid}/sections" if sid else None,
            "api_student_schedule": f"{self.base_url}/campus/api/portal/students/{sid}/schedule" if sid else None,

            # Portal term grades pattern (common in newer IC)
            "res_grades_portal": (f"{self.base_url}/campus/resources/portal/grades/{sid}") if sid else None,

            # Portals academic plan
            "api_academic_plan": f"{self.base_url}/campus/api/portal/academic-plan/{sid}" if sid else None,

            # Course history
            "api_course_history": f"{self.base_url}/campus/api/portal/students/{sid}/courseHistory" if sid else None,

            # Direct student enrollment info
            "api_student_enrollments": f"{self.base_url}/campus/api/portal/students/{sid}/enrollments" if sid else None,
        }

        # Also log the IDs we're working with
        results["_context"] = {
            "student_ids": self._student_ids,
            "calendar_ids": self._calendar_ids,
            "school_ids": self._school_ids,
        }

        for name, spec in endpoints.items():
            if spec is None:
                results[name] = {"status": "skipped", "reason": "missing IDs"}
                continue

            if isinstance(spec, tuple):
                url, params = spec
            else:
                url, params = spec, None

            try:
                async with session.get(url, params=params) as resp:
                    status = resp.status
                    if status == 200:
                        text = await resp.text()
                        try:
                            data = json.loads(text)
                            # Summarize the response
                            if isinstance(data, list):
                                summary = f"list[{len(data)}]"
                                if data:
                                    summary += f" keys={list(data[0].keys()) if isinstance(data[0], dict) else 'primitives'}"
                            elif isinstance(data, dict):
                                summary = f"dict keys={list(data.keys())}"
                            else:
                                summary = str(type(data).__name__)
                            results[name] = {"status": 200, "data": summary, "sample": str(data)[:300]}
                        except json.JSONDecodeError:
                            results[name] = {"status": 200, "data": f"text[{len(text)}]", "sample": text[:200]}
                    else:
                        results[name] = {"status": status}
            except Exception as e:
                results[name] = {"status": "error", "error": str(e)}

        return results

    # ─── Data Fetching Methods ──────────────────────────────────────

    async def get_students(self) -> list[dict]:
        """Fetch all students linked to this parent account."""
        await self._ensure_authenticated()
        session = await self._ensure_session()

        # Return cached student data if available
        if self._student_data:
            return self._student_data

        # Try API endpoint
        data = await self._safe_get(session, f"{self.base_url}/campus/api/portal/students")
        if isinstance(data, list) and data:
            self._student_data = data
            return data

        # Try portal outline
        if self._portal_outline and isinstance(self._portal_outline, dict):
            return self._portal_outline.get("StudentList", [])

        return []

    async def get_courses(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch courses for a student or all students."""
        await self._ensure_authenticated()
        session = await self._ensure_session()
        all_courses = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            # Try prism PortalGrades (contains course info)
            for cal_id in (self._calendar_ids or [""]):
                params = {"x": "portal.PortalGrades", "studentID": sid, "lang": "en"}
                if cal_id:
                    params["calendarID"] = cal_id
                data = await self._safe_get(
                    session, f"{self.base_url}/campus/prism", params=params
                )
                if isinstance(data, dict):
                    for term in data.get("TermList", data.get("terms", [])):
                        for course in term.get("CourseList", term.get("courses", [])):
                            course["studentID"] = sid
                            course["termName"] = term.get("termName", "")
                            all_courses.append(course)
                    if all_courses:
                        break
                elif isinstance(data, list):
                    for item in data:
                        item["studentID"] = sid
                    all_courses.extend(data)
                    if all_courses:
                        break

            # Fallback: try REST patterns
            if not all_courses:
                for endpoint in [
                    f"api/portal/displaygrades/{sid}",
                    f"api/portal/gradebook/student/{sid}",
                ]:
                    data = await self._safe_get(
                        session, f"{self.base_url}/campus/{endpoint}"
                    )
                    if data:
                        courses = data if isinstance(data, list) else data.get("courses", data.get("CourseList", []))
                        if isinstance(courses, list):
                            for c in courses:
                                c["studentID"] = sid
                            all_courses.extend(courses)
                            break

        if all_courses:
            logger.info(f"Fetched {len(all_courses)} courses")
        else:
            logger.warning("No courses found via any endpoint")
        return all_courses

    async def get_assignments(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch assignments for a student or all students."""
        await self._ensure_authenticated()
        session = await self._ensure_session()
        all_assignments = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            # Try prism PortalAssignments
            for cal_id in (self._calendar_ids or [""]):
                params = {"x": "portal.PortalAssignments", "studentID": sid, "lang": "en"}
                if cal_id:
                    params["calendarID"] = cal_id
                data = await self._safe_get(
                    session, f"{self.base_url}/campus/prism", params=params
                )
                if data and data != []:
                    assignments = data if isinstance(data, list) else data.get("AssignmentList", data.get("assignments", []))
                    if isinstance(assignments, list):
                        for a in assignments:
                            a["studentID"] = sid
                        all_assignments.extend(assignments)
                        break

            # Try REST API
            if not all_assignments:
                for endpoint in [
                    (f"api/portal/assignment/student/{sid}", None),
                    ("api/portal/assignments", {"studentID": sid}),
                    ("resources/portal/assignments", {"studentID": sid}),
                ]:
                    url_path, params = endpoint if isinstance(endpoint, tuple) else (endpoint, None)
                    data = await self._safe_get(
                        session, f"{self.base_url}/campus/{url_path}", params=params
                    )
                    if data:
                        assignments = data if isinstance(data, list) else data.get("assignments", [])
                        if isinstance(assignments, list) and assignments:
                            for a in assignments:
                                a["studentID"] = sid
                            all_assignments.extend(assignments)
                            break

        if all_assignments:
            logger.info(f"Fetched {len(all_assignments)} assignments")
        else:
            logger.warning("No assignments found via any endpoint")
        return all_assignments

    async def get_grades(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch grade information for a student or all students."""
        await self._ensure_authenticated()
        session = await self._ensure_session()
        all_grades = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            # Try prism PortalGrades (primary method)
            for cal_id in (self._calendar_ids or [""]):
                params = {"x": "portal.PortalGrades", "studentID": sid, "lang": "en"}
                if cal_id:
                    params["calendarID"] = cal_id
                data = await self._safe_get(
                    session, f"{self.base_url}/campus/prism", params=params
                )
                if isinstance(data, dict):
                    # Parse the grade structure
                    for term in data.get("TermList", data.get("terms", [])):
                        term_name = term.get("termName", term.get("name", ""))
                        for course in term.get("CourseList", term.get("courses", [])):
                            grade_entry = {
                                "studentID": sid,
                                "courseName": course.get("courseName", course.get("name", "")),
                                "courseNumber": course.get("courseNumber", ""),
                                "teacherName": course.get("teacherDisplay", course.get("teacher", "")),
                                "termName": term_name,
                                "score": course.get("score", course.get("percent", "")),
                                "grade": course.get("grade", course.get("letterGrade", "")),
                                "calendarID": cal_id,
                            }
                            # Also check for TaskList within courses
                            for task in course.get("TaskList", []):
                                grade_entry["score"] = task.get("score", grade_entry["score"])
                                grade_entry["grade"] = task.get("grade", grade_entry["grade"])
                                grade_entry["percent"] = task.get("percent", "")
                                grade_entry["taskName"] = task.get("taskName", "")
                            all_grades.append(grade_entry)
                    if all_grades:
                        break
                elif isinstance(data, list) and data:
                    for g in data:
                        g["studentID"] = sid
                    all_grades.extend(data)
                    break

            # Try REST patterns
            if not all_grades:
                for endpoint in [
                    f"api/portal/displaygrades/{sid}",
                    f"api/portal/gradebook/student/{sid}",
                ]:
                    data = await self._safe_get(
                        session, f"{self.base_url}/campus/{endpoint}"
                    )
                    if data:
                        if isinstance(data, list):
                            for g in data:
                                g["studentID"] = sid
                            all_grades.extend(data)
                        elif isinstance(data, dict):
                            # Try to extract grade data from dict
                            grades = data.get("grades", data.get("GradeList", []))
                            if isinstance(grades, list):
                                for g in grades:
                                    g["studentID"] = sid
                                all_grades.extend(grades)
                        if all_grades:
                            break

            # Try resource endpoint
            if not all_grades:
                for school_id in (self._school_ids or [""]):
                    for cal_id in (self._calendar_ids or [""]):
                        params = {"studentID": sid}
                        if school_id:
                            params["schoolID"] = school_id
                        if cal_id:
                            params["calendarID"] = cal_id
                        data = await self._safe_get(
                            session,
                            f"{self.base_url}/campus/resources/portal/grades",
                            params=params,
                        )
                        if data:
                            grades = data if isinstance(data, list) else data.get("grades", [])
                            if isinstance(grades, list) and grades:
                                for g in grades:
                                    g["studentID"] = sid
                                all_grades.extend(grades)
                                break

        if all_grades:
            logger.info(f"Fetched {len(all_grades)} grade entries")
        else:
            logger.warning("No grades found via any endpoint")
        return all_grades

    async def get_attendance(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch attendance records."""
        await self._ensure_authenticated()
        session = await self._ensure_session()
        all_attendance = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            # Try prism PortalAttendance
            for cal_id in (self._calendar_ids or [""]):
                params = {"x": "portal.PortalAttendance", "studentID": sid, "lang": "en"}
                if cal_id:
                    params["calendarID"] = cal_id
                data = await self._safe_get(
                    session, f"{self.base_url}/campus/prism", params=params
                )
                if data:
                    records = data if isinstance(data, list) else data.get("AttendanceList", data.get("attendance", []))
                    if isinstance(records, list):
                        for r in records:
                            if isinstance(r, dict):
                                r["studentID"] = sid
                        all_attendance.extend(records)
                        if all_attendance:
                            break

            # Try REST
            if not all_attendance:
                data = await self._safe_get(
                    session,
                    f"{self.base_url}/campus/api/portal/attendance",
                    params={"studentID": sid},
                )
                if data:
                    records = data if isinstance(data, list) else data.get("attendance", [])
                    if isinstance(records, list):
                        for r in records:
                            if isinstance(r, dict):
                                r["studentID"] = sid
                        all_attendance.extend(records)

        if all_attendance:
            logger.info(f"Fetched {len(all_attendance)} attendance records")
        return all_attendance

    async def get_schedule(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch daily schedule."""
        await self._ensure_authenticated()
        session = await self._ensure_session()
        all_schedule = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            # Try prism PortalSchedule
            for cal_id in (self._calendar_ids or [""]):
                params = {"x": "portal.PortalSchedule", "studentID": sid, "lang": "en"}
                if cal_id:
                    params["calendarID"] = cal_id
                data = await self._safe_get(
                    session, f"{self.base_url}/campus/prism", params=params
                )
                if data:
                    items = data if isinstance(data, list) else data.get("ScheduleList", data.get("schedule", []))
                    if isinstance(items, list):
                        for s in items:
                            if isinstance(s, dict):
                                s["studentID"] = sid
                        all_schedule.extend(items)
                        if all_schedule:
                            break

        if all_schedule:
            logger.info(f"Fetched {len(all_schedule)} schedule entries")
        return all_schedule

    async def get_terms(self) -> list[dict]:
        """Fetch academic terms/semesters."""
        if self._portal_outline and isinstance(self._portal_outline, dict):
            terms = self._portal_outline.get("TermList", [])
            if terms:
                return terms

        await self._ensure_authenticated()
        session = await self._ensure_session()

        data = await self._safe_get(
            session,
            f"{self.base_url}/campus/prism",
            params={"x": "portal.PortalOutline", "lang": "en"},
        )
        if isinstance(data, dict):
            return data.get("TermList", [])
        return []

    async def get_notifications(self) -> list[dict]:
        """Fetch portal notifications/announcements."""
        await self._ensure_authenticated()
        session = await self._ensure_session()

        # Try prism notifications
        data = await self._safe_get(
            session,
            f"{self.base_url}/campus/prism",
            params={"x": "portal.PortalNotifications", "lang": "en"},
        )
        if data:
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("NotificationList", data.get("notifications", []))

        # Try REST announcements
        data = await self._safe_get(
            session, f"{self.base_url}/campus/api/portal/announcements"
        )
        if data:
            return data if isinstance(data, list) else data.get("announcements", [])

        return []

    async def get_report_cards(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch report card data."""
        # Report cards are typically part of grades data
        return []

    async def get_gpa(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch GPA information."""
        # GPA is typically embedded in grade data
        return []

    async def get_all_data(self) -> dict[str, Any]:
        """
        Fetch all available data in one call.
        Returns a dictionary with all data categories.
        """
        results = {}

        tasks = {
            "students": self.get_students(),
            "courses": self.get_courses(),
            "assignments": self.get_assignments(),
            "grades": self.get_grades(),
            "attendance": self.get_attendance(),
            "schedule": self.get_schedule(),
            "terms": self.get_terms(),
            "notifications": self.get_notifications(),
        }

        gathered = await asyncio.gather(
            *tasks.values(), return_exceptions=True
        )

        for key, result in zip(tasks.keys(), gathered):
            if isinstance(result, Exception):
                logger.warning(f"Failed to fetch {key}: {result}")
                results[key] = []
            else:
                results[key] = result

        results["last_updated"] = datetime.now().isoformat()
        return results

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and self._owns_session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

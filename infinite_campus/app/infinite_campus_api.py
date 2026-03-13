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

    Authenticates via the parent portal login and scrapes/fetches
    data from the portal API endpoints.
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
        self._cookies: Optional[aiohttp.CookieJar] = None
        self._auth_headers: dict[str, str] = {}
        self._last_auth: Optional[datetime] = None
        self._api_base: Optional[str] = None

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

        Uses form-based login to establish a session, then
        extracts necessary tokens and IDs for API calls.
        """
        session = await self._ensure_session()

        try:
            # Step 1: Load the login page to get CSRF tokens and form structure
            login_url = f"{self.base_url}/campus/portal/parents/{self.district}.jsp"
            logger.info(f"Loading login page: {login_url}")

            async with session.get(login_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    # Try alternate login URL format
                    login_url = f"{self.base_url}/campus/portal/parents/{self.district}.jsp"
                    async with session.get(login_url, allow_redirects=True) as resp2:
                        if resp2.status != 200:
                            raise AuthenticationError(
                                f"Failed to load login page: HTTP {resp2.status}"
                            )
                        login_html = await resp2.text()
                else:
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
                final_url = str(resp.url)

                if resp.status != 200:
                    raise AuthenticationError(
                        f"Login request failed: HTTP {resp.status}"
                    )

                # Check for login failure indicators
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
                    raise AuthenticationError(
                        "Invalid username or password"
                    )

            # Step 3: Navigate to the portal home to establish full session
            portal_url = f"{self.base_url}/campus/portal/students/portal.html"
            async with session.get(portal_url, allow_redirects=True) as resp:
                portal_html = await resp.text()

            # Try to extract person ID and student info from the portal
            await self._extract_session_info(session, portal_html)

            self._authenticated = True
            self._last_auth = datetime.now()
            self._api_base = f"{self.base_url}/campus/api/portal"

            logger.info(
                f"Authentication successful. Found {len(self._student_ids)} student(s)"
            )
            return True

        except AuthenticationError:
            raise
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            raise AuthenticationError(f"Authentication failed: {e}")

    async def _extract_session_info(
        self, session: aiohttp.ClientSession, html: str
    ) -> None:
        """Extract person ID and student IDs from portal session."""
        # Try multiple methods to get student info

        # Method 1: Check the portal API for student list
        try:
            students_url = f"{self.base_url}/campus/api/portal/students"
            async with session.get(students_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        self._student_ids = [
                            str(s.get("personID", s.get("studentID", "")))
                            for s in data
                            if s.get("personID") or s.get("studentID")
                        ]
                        logger.info(
                            f"Found students via API: {self._student_ids}"
                        )
                        return
        except Exception as e:
            logger.debug(f"API student fetch failed: {e}")

        # Method 2: Try the prism API endpoint
        try:
            prism_url = f"{self.base_url}/campus/prism?x=portal.PortalOutline&lang=en"
            async with session.get(prism_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    students = data.get("StudentList", [])
                    if students:
                        self._student_ids = [
                            str(s.get("personID", "")) for s in students
                        ]
                        logger.info(
                            f"Found students via prism: {self._student_ids}"
                        )
                        return
        except Exception as e:
            logger.debug(f"Prism student fetch failed: {e}")

        # Method 3: Parse HTML for student data
        try:
            soup = BeautifulSoup(html, "html.parser")
            # Look for student selectors or embedded JSON
            scripts = soup.find_all("script")
            for script in scripts:
                text = script.string or ""
                # Look for student data patterns
                patterns = [
                    r'"personID"\s*:\s*(\d+)',
                    r'"studentID"\s*:\s*(\d+)',
                    r"personID=(\d+)",
                ]
                for pattern in patterns:
                    matches = re.findall(pattern, text)
                    if matches:
                        self._student_ids = list(set(matches))
                        logger.info(
                            f"Found students via HTML: {self._student_ids}"
                        )
                        return
        except Exception as e:
            logger.debug(f"HTML student extraction failed: {e}")

        logger.warning("Could not extract student IDs automatically")

    async def _ensure_authenticated(self) -> None:
        """Re-authenticate if session has expired (>30 min)."""
        if not self._authenticated or (
            self._last_auth
            and datetime.now() - self._last_auth > timedelta(minutes=30)
        ):
            await self.authenticate()

    async def _api_get(
        self, endpoint: str, params: Optional[dict] = None
    ) -> Any:
        """Make an authenticated GET request to the campus API."""
        await self._ensure_authenticated()
        session = await self._ensure_session()

        url = f"{self.base_url}/campus/api/portal/{endpoint}"
        logger.debug(f"API GET: {url}")

        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 401:
                    # Session expired, re-auth and retry
                    self._authenticated = False
                    await self.authenticate()
                    async with session.get(url, params=params) as resp2:
                        if resp2.status != 200:
                            raise APIError(
                                f"API request failed: HTTP {resp2.status}"
                            )
                        return await resp2.json()

                if resp.status != 200:
                    text = await resp.text()
                    raise APIError(
                        f"API request failed: HTTP {resp.status} - {text[:200]}"
                    )
                return await resp.json()
        except (aiohttp.ClientError, json.JSONDecodeError) as e:
            raise APIError(f"API request error: {e}")

    async def _prism_get(self, action: str, params: Optional[dict] = None) -> Any:
        """Make an authenticated GET request to the campus prism API."""
        await self._ensure_authenticated()
        session = await self._ensure_session()

        url = f"{self.base_url}/campus/prism"
        all_params = {"x": action, "lang": "en"}
        if params:
            all_params.update(params)

        logger.debug(f"Prism GET: {url} with action {action}")

        try:
            async with session.get(url, params=all_params) as resp:
                if resp.status == 401:
                    self._authenticated = False
                    await self.authenticate()
                    async with session.get(url, params=all_params) as resp2:
                        if resp2.status != 200:
                            raise APIError(f"Prism request failed: HTTP {resp2.status}")
                        return await resp2.json()
                if resp.status != 200:
                    text = await resp.text()
                    raise APIError(f"Prism request failed: HTTP {resp.status} - {text[:200]}")
                return await resp.json()
        except (aiohttp.ClientError, json.JSONDecodeError) as e:
            raise APIError(f"Prism request error: {e}")

    async def _resource_get(self, path: str, params: Optional[dict] = None) -> Any:
        """Make an authenticated GET request to campus resources."""
        await self._ensure_authenticated()
        session = await self._ensure_session()

        url = f"{self.base_url}/campus/resources/{path}"
        logger.debug(f"Resource GET: {url}")

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    raise APIError(f"Resource request failed: HTTP {resp.status}")
                return await resp.json()
        except (aiohttp.ClientError, json.JSONDecodeError) as e:
            raise APIError(f"Resource request error: {e}")

    # ─── Data Fetching Methods ──────────────────────────────────────

    async def get_students(self) -> list[dict]:
        """Fetch all students linked to this parent account."""
        try:
            data = await self._api_get("students")
            if isinstance(data, list):
                return data
            return data.get("students", data.get("StudentList", []))
        except APIError:
            # Fallback to prism
            try:
                data = await self._prism_get("portal.PortalOutline")
                return data.get("StudentList", [])
            except Exception:
                return []

    async def get_courses(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch courses for a student or all students."""
        all_courses = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            try:
                data = await self._api_get(f"students/{sid}/courses")
                courses = data if isinstance(data, list) else data.get("courses", [])
                for course in courses:
                    course["studentID"] = sid
                all_courses.extend(courses)
            except APIError as e:
                logger.warning(f"Failed to fetch courses for student {sid}: {e}")

        return all_courses

    async def get_assignments(
        self, student_id: Optional[str] = None
    ) -> list[dict]:
        """Fetch assignments for a student or all students."""
        all_assignments = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            try:
                data = await self._api_get(f"students/{sid}/assignments")
                assignments = (
                    data if isinstance(data, list) else data.get("assignments", [])
                )
                for a in assignments:
                    a["studentID"] = sid
                all_assignments.extend(assignments)
            except APIError as e:
                logger.warning(
                    f"Failed to fetch assignments for student {sid}: {e}"
                )

        return all_assignments

    async def get_grades(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch grade information for a student or all students."""
        all_grades = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            try:
                # Try the grades endpoint
                data = await self._api_get(f"students/{sid}/grades")
                grades = data if isinstance(data, list) else data.get("grades", [])
                for g in grades:
                    g["studentID"] = sid
                all_grades.extend(grades)
            except APIError:
                # Fallback: extract grades from assignments
                try:
                    data = await self._api_get(f"students/{sid}/assignments")
                    assignments = (
                        data
                        if isinstance(data, list)
                        else data.get("assignments", [])
                    )
                    graded = [
                        {
                            "studentID": sid,
                            "assignmentName": a.get("assignmentName", ""),
                            "courseName": a.get("courseName", ""),
                            "score": a.get("score", ""),
                            "totalPoints": a.get("totalPoints", ""),
                            "percentage": a.get("percentage", ""),
                            "letterGrade": a.get("letterGrade", ""),
                            "date": a.get("dueDate", ""),
                        }
                        for a in assignments
                        if a.get("score") is not None
                    ]
                    all_grades.extend(graded)
                except APIError as e:
                    logger.warning(f"Failed to fetch grades for student {sid}: {e}")

        return all_grades

    async def get_attendance(
        self, student_id: Optional[str] = None
    ) -> list[dict]:
        """Fetch attendance records for a student or all students."""
        all_attendance = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            try:
                data = await self._api_get(f"students/{sid}/attendance")
                records = (
                    data
                    if isinstance(data, list)
                    else data.get("attendance", [])
                )
                for r in records:
                    r["studentID"] = sid
                all_attendance.extend(records)
            except APIError as e:
                logger.warning(
                    f"Failed to fetch attendance for student {sid}: {e}"
                )

        return all_attendance

    async def get_schedule(
        self, student_id: Optional[str] = None
    ) -> list[dict]:
        """Fetch daily schedule for a student or all students."""
        all_schedule = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            try:
                data = await self._api_get(f"students/{sid}/schedule")
                schedule = (
                    data if isinstance(data, list) else data.get("schedule", [])
                )
                for s in schedule:
                    s["studentID"] = sid
                all_schedule.extend(schedule)
            except APIError as e:
                logger.warning(
                    f"Failed to fetch schedule for student {sid}: {e}"
                )

        return all_schedule

    async def get_terms(self) -> list[dict]:
        """Fetch academic terms/semesters."""
        try:
            data = await self._api_get("terms")
            return data if isinstance(data, list) else data.get("terms", [])
        except APIError:
            try:
                data = await self._prism_get("portal.PortalOutline")
                return data.get("TermList", [])
            except APIError:
                return []

    async def get_notifications(self) -> list[dict]:
        """Fetch portal notifications/announcements."""
        try:
            data = await self._api_get("notifications")
            return data if isinstance(data, list) else data.get("notifications", [])
        except APIError:
            try:
                data = await self._api_get("announcements")
                return (
                    data
                    if isinstance(data, list)
                    else data.get("announcements", [])
                )
            except APIError:
                return []

    async def get_report_cards(
        self, student_id: Optional[str] = None
    ) -> list[dict]:
        """Fetch report card data for a student or all students."""
        all_reports = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            try:
                data = await self._api_get(f"students/{sid}/reportcards")
                reports = (
                    data
                    if isinstance(data, list)
                    else data.get("reportCards", [])
                )
                for r in reports:
                    r["studentID"] = sid
                all_reports.extend(reports)
            except APIError as e:
                logger.warning(
                    f"Failed to fetch report cards for student {sid}: {e}"
                )

        return all_reports

    async def get_gpa(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch GPA information for a student or all students."""
        all_gpa = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            try:
                data = await self._api_get(f"students/{sid}/gpa")
                gpa = data if isinstance(data, list) else [data]
                for g in gpa:
                    g["studentID"] = sid
                all_gpa.extend(gpa)
            except APIError as e:
                logger.warning(f"Failed to fetch GPA for student {sid}: {e}")

        return all_gpa

    async def get_all_data(self) -> dict[str, Any]:
        """
        Fetch all available data in one call.
        Returns a dictionary with all data categories.
        """
        results = {}

        # Fetch all data concurrently
        tasks = {
            "students": self.get_students(),
            "courses": self.get_courses(),
            "assignments": self.get_assignments(),
            "grades": self.get_grades(),
            "attendance": self.get_attendance(),
            "schedule": self.get_schedule(),
            "terms": self.get_terms(),
            "notifications": self.get_notifications(),
            "report_cards": self.get_report_cards(),
            "gpa": self.get_gpa(),
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

"""
Infinite Campus Parent Portal API Client.

Handles authentication and data retrieval from the Infinite Campus
Parent Portal API. Supports fetching students, courses, assignments,
grades, attendance, schedule, and notifications.

Endpoint patterns discovered from two reference implementations:
  - tonyzimbinski/infinite-campus (Node.js):
      resources/portal/grades (NO params) → all grades
      resources/portal/roster?_expand={sectionPlacements-{term}} (NO params) → courses
  - schwartzpub/ic_parent_api (Python):
      api/portal/students (NO params) → students
      resources/portal/roster?personID={id} → courses for student
      api/portal/assignment/listView?personID={id} → assignments
      resources/term?structureID={id} → terms
      resources/calendar/instructionalDay?calendarID={id} → schedule days
"""

import asyncio
import logging
import json
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

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
    using multiple API endpoint patterns discovered from reference
    implementations and endpoint discovery.
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
        # Maps student personID -> list of enrollment dicts
        self._student_enrollments: dict[str, list[dict]] = {}
        self._cookies: Optional[aiohttp.CookieJar] = None
        self._auth_headers: dict[str, str] = {}
        self._last_auth: Optional[datetime] = None

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
                    ),
                    "Accept": "application/json",
                },
            )
            self._owns_session = True
        return self._session

    async def authenticate(self) -> bool:
        """Authenticate with the Infinite Campus parent portal."""
        session = await self._ensure_session()

        try:
            # Step 1: Load the login page to get session cookies
            login_url = f"{self.base_url}/campus/portal/parents/{self.district}.jsp"
            logger.info(f"Loading login page: {login_url}")

            async with session.get(login_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise AuthenticationError(
                        f"Failed to load login page: HTTP {resp.status}"
                    )
                await resp.text()

            # Step 2: Submit login via verify.jsp
            # Using both POST data (standard) and query params (ic_parent_api style)
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
                        "password-error",
                    ]
                ):
                    raise AuthenticationError("Invalid username or password")

            # Step 3: Navigate to portal to establish session
            portal_url = f"{self.base_url}/campus/portal/students/portal.html"
            async with session.get(portal_url, allow_redirects=True) as resp:
                await resp.text()

            # Step 4: Get student list from API
            await self._fetch_students(session)

            self._authenticated = True
            self._last_auth = datetime.now()

            logger.info(
                f"Authentication successful. Found {len(self._student_ids)} student(s): {self._student_ids}"
            )
            return True

        except AuthenticationError:
            raise
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            raise AuthenticationError(f"Authentication failed: {e}")

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

                        # Extract enrollment info per student
                        for student in data:
                            sid = str(student.get("personID", ""))
                            self._student_enrollments[sid] = []
                            for enrollment in student.get("enrollments", []):
                                cal_id = str(enrollment.get("calendarID", ""))
                                school_id = str(enrollment.get("schoolID", ""))
                                struct_id = str(enrollment.get("structureID", ""))
                                enroll_info = {
                                    "calendarID": cal_id,
                                    "schoolID": school_id,
                                    "structureID": struct_id,
                                    "calendarName": enrollment.get("calendarName", ""),
                                    "schoolName": enrollment.get("schoolName", ""),
                                    "grade": enrollment.get("grade", ""),
                                    "endYear": enrollment.get("endYear", ""),
                                    "enrollmentID": str(enrollment.get("enrollmentID", "")),
                                    "personID": sid,
                                }
                                self._student_enrollments[sid].append(enroll_info)
                                if cal_id and cal_id not in self._calendar_ids:
                                    self._calendar_ids.append(cal_id)
                                if school_id and school_id not in self._school_ids:
                                    self._school_ids.append(school_id)

                            logger.info(
                                f"Student {sid} ({student.get('firstName', '')} {student.get('lastName', '')}): "
                                f"enrollments={self._student_enrollments[sid]}"
                            )

                        logger.info(f"Calendar IDs: {self._calendar_ids}, School IDs: {self._school_ids}")
                        return
        except Exception as e:
            logger.error(f"API student fetch failed: {e}")

        logger.warning("Could not fetch student list from API")

    async def _ensure_authenticated(self) -> None:
        """Re-authenticate if session has expired (>30 min)."""
        if not self._authenticated or (
            self._last_auth
            and datetime.now() - self._last_auth > timedelta(minutes=30)
        ):
            await self.authenticate()

    async def _safe_get(self, session: aiohttp.ClientSession, url: str,
                        params: Optional[dict] = None,
                        accept_json: bool = True) -> Optional[Any]:
        """Make a GET request and return parsed JSON (dict or list) or None.
        Never returns raw strings — only structured data or None."""
        try:
            headers = {"Accept": "application/json"} if accept_json else {}
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                        if isinstance(data, (dict, list)):
                            return data
                        return None
                    except json.JSONDecodeError:
                        return None
                elif resp.status == 401:
                    self._authenticated = False
                    await self.authenticate()
                    async with session.get(url, params=params, headers=headers) as resp2:
                        if resp2.status == 200:
                            try:
                                data = await resp2.json()
                                if isinstance(data, (dict, list)):
                                    return data
                            except Exception:
                                pass
                logger.debug(f"GET {url} params={params} returned HTTP {resp.status}")
                return None
        except Exception as e:
            logger.debug(f"GET {url} failed: {e}")
            return None

    # ─── Endpoint Discovery ──────────────────────────────────────

    async def discover_endpoints(self) -> dict[str, Any]:
        """
        Try multiple known IC API endpoint patterns and report which ones work.
        Tries endpoints from both reference implementations.
        """
        await self._ensure_authenticated()
        session = await self._ensure_session()
        results = {}

        sid = self._student_ids[0] if self._student_ids else ""
        cal_id = self._calendar_ids[0] if self._calendar_ids else ""
        school_id = self._school_ids[0] if self._school_ids else ""
        struct_id = ""
        if sid and self._student_enrollments.get(sid):
            struct_id = self._student_enrollments[sid][0].get("structureID", "")

        # Encoded _expand parameter for roster (from tonyzimbinski reference)
        roster_expand = quote("{sectionPlacements-{term}}")

        endpoints = {
            # ── WORKING: Students API ──
            "api_students": f"{self.base_url}/campus/api/portal/students",

            # ── GRADES: Try NO params first (tonyzimbinski pattern) ──
            "res_grades_NO_PARAMS": f"{self.base_url}/campus/resources/portal/grades",
            "res_grades_personID": (f"{self.base_url}/campus/resources/portal/grades", {"personID": sid}) if sid else None,
            "res_grades_studentID": (f"{self.base_url}/campus/resources/portal/grades", {"studentID": sid}) if sid else None,
            "res_grades_full": (f"{self.base_url}/campus/resources/portal/grades", {
                "studentID": sid, "calendarID": cal_id, "schoolID": school_id
            }) if sid and cal_id else None,

            # ── ROSTER/COURSES: Multiple patterns ──
            "res_roster_NO_PARAMS": f"{self.base_url}/campus/resources/portal/roster",
            "res_roster_expand": f"{self.base_url}/campus/resources/portal/roster?_expand={roster_expand}",
            "res_roster_personID": (f"{self.base_url}/campus/resources/portal/roster", {"personID": sid}) if sid else None,
            "res_roster_personID_expand": (f"{self.base_url}/campus/resources/portal/roster", {
                "personID": sid, "_expand": "{sectionPlacements-{term}}"
            }) if sid else None,

            # ── ASSIGNMENTS: ic_parent_api pattern ──
            "api_assignment_listView": (f"{self.base_url}/campus/api/portal/assignment/listView", {"personID": sid}) if sid else None,
            "api_assignment_listView_studentID": (f"{self.base_url}/campus/api/portal/assignment/listView", {"studentID": sid}) if sid else None,
            "res_assignments_NO_PARAMS": f"{self.base_url}/campus/resources/portal/assignments",
            "res_assignments_personID": (f"{self.base_url}/campus/resources/portal/assignments", {"personID": sid}) if sid else None,

            # ── ATTENDANCE ──
            "res_attendance_NO_PARAMS": f"{self.base_url}/campus/resources/portal/attendance",
            "res_attendance_personID": (f"{self.base_url}/campus/resources/portal/attendance", {"personID": sid}) if sid else None,

            # ── TERMS ──
            "res_terms": (f"{self.base_url}/campus/resources/term", {"structureID": struct_id}) if struct_id else None,
            "res_terms_all": f"{self.base_url}/campus/resources/term",

            # ── SCHEDULE / CALENDAR ──
            "res_schedule_NO_PARAMS": f"{self.base_url}/campus/resources/portal/schedule",
            "res_instructional_day": (f"{self.base_url}/campus/resources/calendar/instructionalDay", {"calendarID": cal_id}) if cal_id else None,

            # ── NOTIFICATIONS ──
            "api_notifications": f"{self.base_url}/campus/api/portal/notifications",
            "api_announcements": f"{self.base_url}/campus/api/portal/announcements",
            "res_notifications": f"{self.base_url}/campus/resources/portal/notifications",

            # ── OTHER API PATTERNS ──
            "api_grades": (f"{self.base_url}/campus/api/portal/grades", {"studentID": sid}) if sid else None,
            "api_attendance": (f"{self.base_url}/campus/api/portal/attendance", {"studentID": sid}) if sid else None,
            "api_schedule": (f"{self.base_url}/campus/api/portal/schedule", {"studentID": sid}) if sid else None,
        }

        # Context info
        results["_context"] = {
            "student_ids": self._student_ids,
            "calendar_ids": self._calendar_ids,
            "school_ids": self._school_ids,
            "enrollments": self._student_enrollments,
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
                            if isinstance(data, list):
                                summary = f"list[{len(data)}]"
                                if data and isinstance(data[0], dict):
                                    summary += f" keys={list(data[0].keys())[:10]}"
                            elif isinstance(data, dict):
                                summary = f"dict keys={list(data.keys())[:10]}"
                            else:
                                summary = str(type(data).__name__)
                            results[name] = {
                                "status": 200,
                                "data": summary,
                                "sample": str(data)[:500]
                            }
                        except json.JSONDecodeError:
                            is_html = "<html" in text.lower() or "<!doctype" in text.lower()
                            results[name] = {
                                "status": 200,
                                "data": f"{'HTML' if is_html else 'text'}[{len(text)}]",
                                "sample": text[:200]
                            }
                    else:
                        results[name] = {"status": status}
            except Exception as e:
                results[name] = {"status": "error", "error": str(e)}

        return results

    # ─── Data Fetching Methods ──────────────────────────────────────

    async def get_students(self) -> list[dict]:
        """Fetch all students linked to this parent account."""
        await self._ensure_authenticated()
        if self._student_data:
            return self._student_data
        session = await self._ensure_session()
        data = await self._safe_get(session, f"{self.base_url}/campus/api/portal/students")
        if isinstance(data, list) and data:
            self._student_data = data
            return data
        return []

    async def get_grades(self, student_id: Optional[str] = None) -> list[dict]:
        """
        Fetch grade information for all students.

        Strategy (from tonyzimbinski reference):
        1. Try resources/portal/grades with NO params (returns all grades for session)
        2. Fallback: try with personID param (ic_parent_api pattern)
        3. Fallback: try with studentID + enrollment params
        """
        await self._ensure_authenticated()
        session = await self._ensure_session()
        all_grades = []

        # ── Strategy 1: NO params (tonyzimbinski pattern) ──
        url = f"{self.base_url}/campus/resources/portal/grades"
        data = await self._safe_get(session, url)
        if data:
            grades_list = self._extract_grades(data)
            if grades_list:
                logger.info(f"[grades] Got {len(grades_list)} entries via NO-PARAM call")
                return grades_list

        # ── Strategy 2: Per-student with personID (ic_parent_api pattern) ──
        student_ids = [student_id] if student_id else self._student_ids
        for sid in student_ids:
            data = await self._safe_get(session, url, params={"personID": sid})
            if data:
                grades_list = self._extract_grades(data, sid)
                if grades_list:
                    logger.info(f"[grades] Got {len(grades_list)} entries for student {sid} via personID")
                    all_grades.extend(grades_list)
                    continue

            # Strategy 3: with studentID
            data = await self._safe_get(session, url, params={"studentID": sid})
            if data:
                grades_list = self._extract_grades(data, sid)
                if grades_list:
                    logger.info(f"[grades] Got {len(grades_list)} entries for student {sid} via studentID")
                    all_grades.extend(grades_list)
                    continue

            # Strategy 4: with enrollment params
            for enroll in self._student_enrollments.get(sid, []):
                params = {
                    "studentID": sid,
                    "calendarID": enroll.get("calendarID", ""),
                    "schoolID": enroll.get("schoolID", ""),
                }
                data = await self._safe_get(session, url, params=params)
                if data:
                    grades_list = self._extract_grades(data, sid)
                    if grades_list:
                        logger.info(f"[grades] Got {len(grades_list)} entries for student {sid} via enrollment params")
                        all_grades.extend(grades_list)
                        break

        if all_grades:
            logger.info(f"[grades] Total: {len(all_grades)} grade entries")
        else:
            logger.warning("[grades] No grades found via any endpoint pattern")
        return all_grades

    def _extract_grades(self, data: Any, student_id: str = "") -> list[dict]:
        """
        Extract grades from API response.

        Reference structure (tonyzimbinski):
        grades[schoolIndex].terms[].courses[].gradingTasks[0] with fields:
        progressScore, progressPercent, progressTotalPoints, progressPointsEarned,
        score, percent, totalPoints, pointsEarned, courseName, courseNumber,
        roomName, teacherDisplay, _id
        """
        results = []

        if isinstance(data, list):
            if not data:
                return []
            # Could be list of school objects or flat list of grade entries
            first = data[0] if data else {}
            if isinstance(first, dict) and "terms" in first:
                # tonyzimbinski structure: list of schools with terms
                for school_idx, school in enumerate(data):
                    if not isinstance(school, dict):
                        continue
                    school_name = school.get("schoolName", f"School {school_idx}")
                    for term in school.get("terms", []):
                        if not isinstance(term, dict):
                            continue
                        term_name = term.get("termName", "")
                        for course in term.get("courses", []):
                            if not isinstance(course, dict):
                                continue
                            grade_entry = {
                                "studentID": student_id,
                                "schoolName": school_name,
                                "termName": term_name,
                                "courseName": course.get("courseName", ""),
                                "courseNumber": course.get("courseNumber", ""),
                                "teacherDisplay": course.get("teacherDisplay", ""),
                                "roomName": course.get("roomName", ""),
                            }
                            # Extract grading task scores
                            tasks = course.get("gradingTasks", [])
                            if tasks and isinstance(tasks, list):
                                task = tasks[0] if isinstance(tasks[0], dict) else {}
                                grade_entry.update({
                                    "score": task.get("score", ""),
                                    "percent": task.get("percent", ""),
                                    "progressScore": task.get("progressScore", ""),
                                    "progressPercent": task.get("progressPercent", ""),
                                    "totalPoints": task.get("totalPoints", ""),
                                    "pointsEarned": task.get("pointsEarned", ""),
                                    "progressTotalPoints": task.get("progressTotalPoints", ""),
                                    "progressPointsEarned": task.get("progressPointsEarned", ""),
                                })
                            results.append(grade_entry)
            else:
                # Flat list — tag with studentID and return as-is
                for item in data:
                    if isinstance(item, dict):
                        if student_id:
                            item["studentID"] = student_id
                        results.append(item)

        elif isinstance(data, dict):
            # Try nested keys
            for key in ["grades", "GradeList", "terms", "TermList", "courses", "CourseList"]:
                items = data.get(key, [])
                if isinstance(items, list) and items:
                    return self._extract_grades(items, student_id)
            # Single grade entry?
            if data:
                if student_id:
                    data["studentID"] = student_id
                results.append(data)

        return results

    async def get_courses(self, student_id: Optional[str] = None) -> list[dict]:
        """
        Fetch course roster.

        Strategy:
        1. resources/portal/roster with _expand and NO other params (tonyzimbinski)
        2. resources/portal/roster with personID (ic_parent_api)
        3. Fallback to grades data which also contains course info
        """
        await self._ensure_authenticated()
        session = await self._ensure_session()
        all_courses = []

        base_url = f"{self.base_url}/campus/resources/portal/roster"

        # ── Strategy 1: NO params with _expand (tonyzimbinski) ──
        data = await self._safe_get(
            session, base_url,
            params={"_expand": "{sectionPlacements-{term}}"}
        )
        if isinstance(data, list) and data:
            logger.info(f"[courses] Got {len(data)} courses via roster NO-PARAM+expand")
            for item in data:
                if isinstance(item, dict):
                    item["_source"] = "roster"
            return data

        # Also try with no params at all
        data = await self._safe_get(session, base_url)
        if isinstance(data, list) and data:
            logger.info(f"[courses] Got {len(data)} courses via roster NO-PARAM")
            return data

        # ── Strategy 2: Per-student with personID (ic_parent_api) ──
        student_ids = [student_id] if student_id else self._student_ids
        for sid in student_ids:
            data = await self._safe_get(session, base_url, params={"personID": sid})
            if isinstance(data, list) and data:
                logger.info(f"[courses] Got {len(data)} courses for student {sid} via personID")
                for item in data:
                    if isinstance(item, dict):
                        item["studentID"] = sid
                all_courses.extend(data)
                continue

            # Try with personID + _expand
            data = await self._safe_get(
                session, base_url,
                params={"personID": sid, "_expand": "{sectionPlacements-{term}}"}
            )
            if isinstance(data, list) and data:
                logger.info(f"[courses] Got {len(data)} courses for student {sid} via personID+expand")
                for item in data:
                    if isinstance(item, dict):
                        item["studentID"] = sid
                all_courses.extend(data)
                continue

            # Try with studentID
            data = await self._safe_get(session, base_url, params={"studentID": sid})
            if isinstance(data, list) and data:
                logger.info(f"[courses] Got {len(data)} courses for student {sid} via studentID")
                for item in data:
                    if isinstance(item, dict):
                        item["studentID"] = sid
                all_courses.extend(data)

        if all_courses:
            logger.info(f"[courses] Total: {len(all_courses)} courses")
        else:
            logger.warning("[courses] No courses found, will try to extract from grades")
        return all_courses

    async def get_assignments(self, student_id: Optional[str] = None) -> list[dict]:
        """
        Fetch assignments.

        Strategy:
        1. api/portal/assignment/listView with personID (ic_parent_api pattern — NEW!)
        2. resources/portal/assignments with NO params
        3. resources/portal/assignments with personID/studentID
        """
        await self._ensure_authenticated()
        session = await self._ensure_session()
        all_assignments = []
        student_ids = [student_id] if student_id else self._student_ids

        for sid in student_ids:
            # ── Strategy 1: assignment/listView with personID (ic_parent_api) ──
            url1 = f"{self.base_url}/campus/api/portal/assignment/listView"
            data = await self._safe_get(session, url1, params={"personID": sid})
            if isinstance(data, list) and data:
                logger.info(f"[assignments] Got {len(data)} via listView personID for student {sid}")
                for a in data:
                    if isinstance(a, dict):
                        a["studentID"] = sid
                all_assignments.extend(data)
                continue

            # Try listView with studentID
            data = await self._safe_get(session, url1, params={"studentID": sid})
            if isinstance(data, list) and data:
                logger.info(f"[assignments] Got {len(data)} via listView studentID for student {sid}")
                for a in data:
                    if isinstance(a, dict):
                        a["studentID"] = sid
                all_assignments.extend(data)
                continue

            # ── Strategy 2: resources/portal/assignments NO params ──
            url2 = f"{self.base_url}/campus/resources/portal/assignments"
            data = await self._safe_get(session, url2)
            if isinstance(data, list) and data:
                logger.info(f"[assignments] Got {len(data)} via resources NO-PARAM")
                for a in data:
                    if isinstance(a, dict):
                        a["studentID"] = sid
                all_assignments.extend(data)
                continue

            # ── Strategy 3: resources with personID/studentID ──
            data = await self._safe_get(session, url2, params={"personID": sid})
            if isinstance(data, list) and data:
                logger.info(f"[assignments] Got {len(data)} via resources personID")
                for a in data:
                    if isinstance(a, dict):
                        a["studentID"] = sid
                all_assignments.extend(data)
                continue

            data = await self._safe_get(session, url2, params={"studentID": sid})
            if isinstance(data, list) and data:
                logger.info(f"[assignments] Got {len(data)} via resources studentID")
                for a in data:
                    if isinstance(a, dict):
                        a["studentID"] = sid
                all_assignments.extend(data)

        if all_assignments:
            logger.info(f"[assignments] Total: {len(all_assignments)} assignments")
        else:
            logger.warning("[assignments] No assignments found via any endpoint")
        return all_assignments

    async def get_attendance(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch attendance records."""
        await self._ensure_authenticated()
        session = await self._ensure_session()
        all_attendance = []

        url = f"{self.base_url}/campus/resources/portal/attendance"

        # Try NO params first
        data = await self._safe_get(session, url)
        if isinstance(data, list) and data:
            logger.info(f"[attendance] Got {len(data)} records via NO-PARAM")
            return data

        # Per-student
        student_ids = [student_id] if student_id else self._student_ids
        for sid in student_ids:
            for param_name in ["personID", "studentID"]:
                data = await self._safe_get(session, url, params={param_name: sid})
                if isinstance(data, list) and data:
                    logger.info(f"[attendance] Got {len(data)} records for student {sid} via {param_name}")
                    for r in data:
                        if isinstance(r, dict):
                            r["studentID"] = sid
                    all_attendance.extend(data)
                    break

        if all_attendance:
            logger.info(f"[attendance] Total: {len(all_attendance)} records")
        return all_attendance

    async def get_schedule(self, student_id: Optional[str] = None) -> list[dict]:
        """
        Fetch schedule data.

        Tries:
        1. resources/portal/schedule (no params)
        2. resources/calendar/instructionalDay with calendarID (ic_parent_api)
        """
        await self._ensure_authenticated()
        session = await self._ensure_session()
        all_schedule = []

        # Try portal schedule
        url = f"{self.base_url}/campus/resources/portal/schedule"
        data = await self._safe_get(session, url)
        if isinstance(data, list) and data:
            logger.info(f"[schedule] Got {len(data)} entries via NO-PARAM")
            return data

        # Per-student with personID
        student_ids = [student_id] if student_id else self._student_ids
        for sid in student_ids:
            data = await self._safe_get(session, url, params={"personID": sid})
            if isinstance(data, list) and data:
                logger.info(f"[schedule] Got {len(data)} entries for student {sid}")
                all_schedule.extend(data)
                continue

        # Try instructional days per calendar (ic_parent_api pattern)
        if not all_schedule:
            for cal_id in self._calendar_ids:
                url2 = f"{self.base_url}/campus/resources/calendar/instructionalDay"
                data = await self._safe_get(session, url2, params={"calendarID": cal_id})
                if isinstance(data, list) and data:
                    logger.info(f"[schedule] Got {len(data)} instructional days for calendar {cal_id}")
                    all_schedule.extend(data)

        if all_schedule:
            logger.info(f"[schedule] Total: {len(all_schedule)} entries")
        return all_schedule

    async def get_terms(self) -> list[dict]:
        """Fetch academic terms using resources/term endpoint."""
        await self._ensure_authenticated()
        session = await self._ensure_session()

        # Try with each structureID from enrollments
        struct_ids = set()
        for sid, enrollments in self._student_enrollments.items():
            for e in enrollments:
                if e.get("structureID"):
                    struct_ids.add(e["structureID"])

        url = f"{self.base_url}/campus/resources/term"
        for struct_id in struct_ids:
            data = await self._safe_get(session, url, params={"structureID": struct_id})
            if isinstance(data, list) and data:
                logger.info(f"[terms] Got {len(data)} terms for structure {struct_id}")
                return data

        # Try with no params
        data = await self._safe_get(session, url)
        if isinstance(data, list) and data:
            logger.info(f"[terms] Got {len(data)} terms via NO-PARAM")
            return data

        return []

    async def get_notifications(self) -> list[dict]:
        """Fetch portal notifications/announcements."""
        await self._ensure_authenticated()
        session = await self._ensure_session()

        # Try multiple endpoints
        endpoints = [
            f"{self.base_url}/campus/resources/portal/notifications",
            f"{self.base_url}/campus/api/portal/notifications",
            f"{self.base_url}/campus/api/portal/announcements",
        ]

        for url in endpoints:
            data = await self._safe_get(session, url)
            if isinstance(data, list) and data:
                logger.info(f"[notifications] Got {len(data)} from {url}")
                return data
            if isinstance(data, dict):
                items = data.get("NotificationList", data.get("notifications", data.get("announcements", [])))
                if isinstance(items, list) and items:
                    return items

        return []

    async def get_report_cards(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch report card data (typically part of grades)."""
        return []

    async def get_gpa(self, student_id: Optional[str] = None) -> list[dict]:
        """Fetch GPA information (typically embedded in grade data)."""
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

        # Log summary
        for key in tasks:
            count = len(results.get(key, [])) if isinstance(results.get(key), list) else "N/A"
            logger.info(f"[summary] {key}: {count}")

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

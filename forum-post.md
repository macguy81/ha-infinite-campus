# Infinite Campus Monitor — Track Your Kids' Grades, Assignments & Attendance in HA

**TL;DR:** I built a Home Assistant add-on that connects to Infinite Campus (parent portal) and gives you real-time grades, assignments, attendance, and course info — with WhatsApp notifications and auto-created HA sensors for automations.

---

## What is this?

If your school district uses **Infinite Campus**, you know the drill — you have to keep logging in to check grades, scroll through assignments, hope you catch a missing assignment before it tanks the grade. I wanted all of that data inside Home Assistant where I could automate around it.

This add-on:

- **Fetches data** from your Infinite Campus parent portal (grades, assignments, attendance, courses, schedule)
- **Creates HA sensors** automatically — per-course grade sensors, missing assignment count, upcoming assignments, etc.
- **Sends WhatsApp notifications** (free via CallMeBot) when grades change, new assignments are posted, assignments go missing, or attendance events happen
- **Has a full web dashboard** via HA ingress with student/term filters, grade trends, and per-course drill-down
- **Supports multiple students** on one parent account

---

## Screenshots

### Dashboard
![Dashboard](https://raw.githubusercontent.com/macguy81/ha-infinite-campus/main/screenshots/dashboard.svg)

### Grades & Insights
Compact grade trend table with colored cells, standards-based grading support (G/NY marks), and expandable course cards showing individual assessments.

![Grades](https://raw.githubusercontent.com/macguy81/ha-infinite-campus/main/screenshots/grades.svg)

### Mobile View
Fully responsive — tables become card layouts on phones.

![Mobile](https://raw.githubusercontent.com/macguy81/ha-infinite-campus/main/screenshots/mobile.svg)

*Screenshots use sample data — no real student information.*

---

## Key Features

### HA Sensors (created automatically)
- `sensor.infinite_campus_<student>_<course>` — letter grade as state, percent/score/teacher as attributes
- `sensor.infinite_campus_<student>_missing_assignments` — count of missing assignments
- `sensor.infinite_campus_<student>_upcoming_assignments` — assignments due in the next 7 days
- `sensor.infinite_campus_<student>_total_assignments` — graded and turned-in counts
- `binary_sensor.infinite_campus_connected` — connection health

### WhatsApp Alerts (free via CallMeBot)
- Grade posted or changed (score + letter grade)
- New assignments with due dates
- Missing assignment alerts
- Assignment graded or score changed (old → new)
- Attendance events (absences, tardies)
- Optional daily summary at your chosen time
- Supports 2 phone numbers (both parents)

### Web Dashboard
- Apple-style clean UI with dark/light support
- Dashboard with stats, quick actions, and recent assignments
- Grades tab with student/term filters, grade trend charts across marking periods, distribution donut charts
- Expandable course cards with individual assessment drill-down
- Supports both percentage-based grading and standards-based marks (G/NY/E)
- Mobile-first responsive design

---

## Installation

1. Go to **Settings → Add-ons → Add-on Store**
2. Three dots menu → **Repositories** → add: `https://github.com/macguy81/ha-infinite-campus`
3. Find **Infinite Campus Monitor** and click **Install**
4. Go to **Configuration** tab and enter your IC district URL, username, and password
5. Click **Start** and open the **Web UI**

### Finding Your District Info
Visit your Infinite Campus login page — the URL is your base URL (e.g., `https://downingtownpa.infinitecampus.org`). Right-click → Inspect → search for `appName` in the page source to find your district identifier.

### WhatsApp Setup (Optional, Free)
1. Save **+34 644 51 95 23** as a contact
2. Send "I allow callmebot to send me messages" to that number on WhatsApp
3. You'll get an API key — enter it in the add-on config
4. Free with ~25 messages/day fair-use limit

---

## Automation Examples

**Alert when a grade drops below C:**
```yaml
automation:
  - alias: "Grade Drop Alert"
    trigger:
      - platform: state
        entity_id: sensor.infinite_campus_aarav_adhikari_ela_7
    condition:
      - condition: template
        value_template: >
          {{ state_attr('sensor.infinite_campus_aarav_adhikari_ela_7', 'percent') | float(100) < 70 }}
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Grade Alert"
          message: "Grade dropped to {{ states('sensor.infinite_campus_aarav_adhikari_ela_7') }}"
```

**Turn on desk lamp when homework is due:**
```yaml
automation:
  - alias: "Homework Reminder Light"
    trigger:
      - platform: numeric_state
        entity_id: sensor.infinite_campus_aarav_adhikari_upcoming_assignments
        above: 0
    condition:
      - condition: time
        after: "16:00:00"
    action:
      - service: light.turn_on
        target:
          entity_id: light.desk_lamp
        data:
          color_name: blue
```

**Missing assignment alert:**
```yaml
automation:
  - alias: "Missing Assignment Alert"
    trigger:
      - platform: numeric_state
        entity_id: sensor.infinite_campus_aarav_adhikari_missing_assignments
        above: 0
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: "{{ states('sensor.infinite_campus_aarav_adhikari_missing_assignments') }} missing assignment(s)!"
```

---

## Technical Details

- Docker-based add-on using aiohttp
- Python async for IC portal auth and data fetching
- Polls at configurable intervals (default 15 min)
- Uses HA Supervisor API to create/update sensors automatically
- All data stays local — only talks to IC servers (your credentials) and optionally CallMeBot (notification text only)
- Tested with Infinite Campus districts using both traditional and standards-based grading

---

## Links

- **GitHub:** https://github.com/macguy81/ha-infinite-campus
- **Current version:** 1.3.3

---

## Feedback Welcome

This is my first HA add-on so I'd love feedback! If your district uses Infinite Campus and you try it out, let me know how it goes. The IC API can vary slightly between districts so the more people test it, the more robust it becomes.

If you run into issues, check the add-on logs first — they're pretty verbose about what's happening. Feel free to open a GitHub issue or post here.

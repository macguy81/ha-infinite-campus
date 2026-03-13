# Infinite Campus Monitor for Home Assistant

A Home Assistant add-on that connects to the Infinite Campus parent portal to monitor your children's grades, assignments, attendance, and school notifications -- with free WhatsApp alerts.

## Features

- **Multi-Student Support** -- Monitor all children linked to your parent account
- **Real-Time Monitoring** -- Polls Infinite Campus at configurable intervals
- **WhatsApp Notifications** -- Free alerts via CallMeBot for grade changes, new assignments, attendance, and school announcements
- **Web Dashboard** -- Built-in UI for viewing student data, grades, and configuration
- **Daily Summaries** -- Automated daily digest of each student's academic status
- **Change Detection** -- Only notifies you when something actually changes
- **Privacy-First** -- All data stays local on your Home Assistant instance

## Quick Start

1. **Add this repository** to your Home Assistant add-on store:
   ```
   https://github.com/macguy81/ha-infinite-campus
   ```

2. **Install** the "Infinite Campus Monitor" add-on

3. **Configure** your Infinite Campus credentials in the add-on settings:
   - Base URL (e.g., `https://downingtownpa.infinitecampus.org`)
   - District / appName (e.g., `downingtown`)
   - Username and password

4. **(Optional) Set up WhatsApp** notifications:
   - Send "I allow callmebot to send me messages" to +34 644 51 95 23 on WhatsApp
   - Enter your phone number and the API key you receive

5. **Start** the add-on and open the Web UI

## Finding Your District Info

Visit your Infinite Campus login page, then:
1. Note the URL -- that's your **Base URL**
2. Right-click > Inspect > search for `appName` -- that value is your **District**

## Data Available

| Category | Description |
|----------|-------------|
| Students | Names, IDs for all children |
| Courses | Classes, teachers, periods |
| Assignments | Homework with due dates |
| Grades | Scores, percentages, letter grades |
| Attendance | Daily records, tardies |
| Schedule | Class schedule |
| GPA | Cumulative GPA |
| Notifications | School announcements |

## WhatsApp Notification Examples

- Grade update: "Score: 95/100 (A) in Math - Chapter 5 Test"
- New assignment: "Science Lab Report due March 20"
- Attendance: "Tardy - Period 2"
- Daily summary: Courses, assignments due, new grades, GPA

## Architecture

```
ha-infinite-campus/
├── repository.yaml          # HA add-on repository config
├── README.md
├── LICENSE
└── infinite_campus/
    ├── config.yaml          # HA add-on configuration
    ├── Dockerfile           # Container build
    ├── build.yaml           # Multi-arch build targets
    ├── DOCS.md              # Detailed documentation
    ├── CHANGELOG.md
    ├── translations/
    │   └── en.yaml          # UI strings
    └── app/
        ├── server.py            # Web server & API
        ├── infinite_campus_api.py  # IC portal client
        ├── whatsapp_notify.py   # CallMeBot integration
        ├── scheduler.py         # Polling & change detection
        ├── requirements.txt
        ├── templates/
        │   └── index.html       # Dashboard UI
        └── static/              # Static assets
```

## Contributing

Contributions welcome! Please open an issue or pull request.

## License

MIT License -- see [LICENSE](LICENSE) for details.

## Disclaimer

This is an unofficial, community-developed add-on. It is not affiliated with or endorsed by Infinite Campus, Inc. Use for personal purposes only and comply with your school district's data policies. Student data is sensitive -- handle responsibly.

# Infinite Campus Monitor - Documentation

## Overview

This Home Assistant add-on connects to your school district's Infinite Campus parent portal and monitors your children's academic data. It can send real-time WhatsApp notifications when grades, assignments, or attendance records change.

## Prerequisites

- A Home Assistant instance (OS or Supervised installation)
- An Infinite Campus parent account with portal access
- (Optional) A WhatsApp account for receiving notifications

## Installation

### Method 1: Add Repository (Recommended)

1. In Home Assistant, go to **Settings > Add-ons > Add-on Store**
2. Click the three-dot menu (top right) and select **Repositories**
3. Add this repository URL: `https://github.com/macguy81/ha-infinite-campus`
4. Click **Add**, then find "Infinite Campus Monitor" in the store
5. Click **Install**

### Method 2: Manual Installation

1. SSH into your Home Assistant instance
2. Navigate to `/addons/` directory
3. Clone this repository:
   ```
   git clone https://github.com/macguy81/ha-infinite-campus.git
   ```
4. Go to **Settings > Add-ons** and click **Reload**
5. Find "Infinite Campus Monitor" under Local add-ons

## Configuration

### Finding Your Infinite Campus Details

**Base URL:** Visit your school's Infinite Campus login page. The URL will look like `https://yourdistrict.infinitecampus.org`. Copy this base URL.

**District (appName):** On the login page:
1. Right-click and select "Inspect" (or press F12)
2. Search the HTML for `appName`
3. Look for `<input name="appName" value="yourdistrict">`
4. The `value` is your district identifier

**Username & Password:** Your regular parent portal login credentials.

### Setting Up WhatsApp Notifications (Free)

This add-on uses [CallMeBot](https://www.callmebot.com/) for free WhatsApp messaging:

1. Save the number **+34 644 51 95 23** in your phone contacts
2. Open WhatsApp and send this exact message to that number:
   ```
   I allow callmebot to send me messages
   ```
3. Wait for a reply containing your **API key**
4. Enter your phone number (with country code, e.g., `+12125551234`) and API key in the add-on settings

**Note:** CallMeBot has a fair-use limit of approximately 25 messages per day. The add-on respects this limit automatically.

### Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| `ic_base_url` | (required) | Your Infinite Campus URL |
| `ic_district` | (required) | District identifier (appName) |
| `ic_username` | (required) | Parent portal username |
| `ic_password` | (required) | Parent portal password |
| `whatsapp_phone` | (optional) | WhatsApp number with country code |
| `whatsapp_api_key` | (optional) | CallMeBot API key |
| `poll_interval` | 900 | Check interval in seconds (min: 300) |
| `notify_grades` | true | Alert on grade updates |
| `notify_assignments` | true | Alert on new assignments |
| `notify_attendance` | true | Alert on attendance changes |
| `notify_notifications` | true | Alert on school announcements |
| `daily_summary` | true | Send daily summary |
| `daily_summary_hour` | 18 | Hour for daily summary (0-23) |
| `auto_start` | true | Start monitoring automatically |

## Web Dashboard

After installation, access the dashboard via the add-on's **Web UI** button or through the sidebar (if ingress is enabled). The dashboard provides:

- **Dashboard tab:** Live stats, quick actions, recent activity
- **Students tab:** Student profiles and enrolled courses
- **Grades tab:** All grades with color-coded letter grades
- **Settings tab:** Configure all options with test buttons

## Data Collected

The add-on fetches the following data from Infinite Campus:

- **Students:** Names, IDs, basic info for all children linked to your account
- **Courses:** Class names, teachers, periods/sections
- **Assignments:** Homework, projects, tests with due dates
- **Grades:** Scores, percentages, letter grades
- **Attendance:** Daily attendance records and tardies
- **Schedule:** Daily/weekly class schedules
- **Terms:** Academic periods and semesters
- **Notifications:** School announcements and alerts
- **Report Cards:** Periodic grade reports
- **GPA:** Cumulative GPA data

## Troubleshooting

**"Authentication failed"**
- Double-check your username and password
- Verify the base URL matches your login page exactly
- Ensure the district/appName is correct (inspect the login page HTML)

**"No students found"**
- Confirm your account has student portal access
- Try logging into the portal normally to verify it works
- Some districts may require a different URL format

**WhatsApp messages not arriving**
- Verify you sent the activation message to CallMeBot
- Check the phone number includes the country code
- Ensure you haven't exceeded the daily limit (25 messages)

**High error count**
- Check Home Assistant logs for details: Settings > System > Logs
- Infinite Campus may have changed their API (community-maintained)
- Increase the poll interval if being rate-limited

## Privacy & Security

- Credentials are stored locally in Home Assistant's protected data store
- All communication with Infinite Campus uses HTTPS
- Student data is cached locally and never sent to third parties (except WhatsApp notifications through CallMeBot, which only sends summaries)
- This add-on is for personal use only - comply with your school's data policies

## Support

- **Issues:** [GitHub Issues](https://github.com/macguy81/ha-infinite-campus/issues)
- **Community:** [Home Assistant Community Forum](https://community.home-assistant.io/)

# Project: Composite Biometric Explorer

Personal dashboard hosted on GitHub Pages. One repo, one Firebase project, multiple single-page apps.

## Pages

| File | Title | Status |
|---|---|---|
| `index.html` | Command Center (home) | live |
| `tracker.html` | Time Tracker | live |
| `health.html` | Health Tracker (overview) | live |
| `correlations.html` | Health · Correlations | live |
| `viz.html` | Health · Visualizations | live |
| `gym.html` | Gym Tracker | live |
| `blog.html` | Book Blog | live (WIP) |
| `event-radar.html` | Event Radar — Vienna | live |

## Firebase

- **Project:** `time-tracker-df33b`
- **SDK:** Firebase compat v9.23.0 (CDN)
- **apiKey:** `AIzaSyAnRbmN4bVSbegq4IDvQfCsMmlcc1nX3bs`
- Config object is inlined in each HTML file; search for `firebaseConfig`

### Firestore collections

| Collection | Purpose |
|---|---|
| `health_daily/{date}` | Daily Garmin metrics (HRV, sleep, steps, stress, etc.) |
| `health_daily/{date}.custom` | Lifestyle log entries (user-defined variables) |
| `gym_sessions/{date}` | Gym workout sessions |
| `_config/garmin_tokens` | Cached Garmin session tokens (base64 blob) |
| `_config/garmin_mfa_pending` | MFA handshake between sync job and health.html UI |
| `_config/custom_vars` | User-defined lifestyle log variable definitions |

## Style System

All pages share the same design language — **do not deviate from these values**:

```css
/* Font */
font-family: 'Share Tech Mono', 'Courier New', monospace;
font-size: 16px; /* body base */

/* Background */
background: #06091a;

/* Text */
color: #ddeeff; /* body / primary text */
color: #0eb8ff; /* accent / headings */

/* Key opacities for secondary text */
color: rgba(14,184,255, 0.88-0.95); /* labels, subheadings */
color: rgba(14,184,255, 0.72-0.82); /* subtitles, secondary */

/* Cards / panels */
background: rgba(8,12,32,0.97);
border: 1px solid rgba(14,184,255,0.12);
border-top: 2px solid rgba(14,184,255,0.28);

/* Chart axis tick color */
const TICK_COLOR = 'rgba(220,240,255,0.95)';
```

**Never use** `#c2e4ff` (old dim color) or opacities below 0.7 for readable text.

### Heading sizes
- `h1` on inner pages: `2.2em`
- `h1` on index: `2.4em`
- `.title-pre` / `.hdr-pre`: `0.9em`
- `.title-sub` / `.hdr-sub`: `0.9em`

## Charts

- **Library:** Chart.js 4.4.3 (CDN)
- **3D scatter:** Plotly.js 2.27.0 (lazy-loaded)
- All charts use `TICK_COLOR = 'rgba(220,240,255,0.95)'` for axis labels
- Tick font size: `12`

## Garmin Sync

- **Script:** `sync/garmin_sync.py`
- **Trigger:** GitHub Actions (scheduled)
- **Auth flow:** Firestore tokens → TOTP → manual MFA code
- **MFA UI:** floating panel bottom-right of `health.html` — enter 6-digit code when sync job is waiting
- **Metrics synced:** HRV, sleep score/duration, steps, resting HR, stress, body battery, distance, intensity minutes, sedentary hours, respiration, VO2max, training readiness, weight

## Gym Tracker

- **Workouts:** A and B, alternating
- **Exercises A:** Bench Press, Rowing Machine, Squat/Bent Over Row, DB Row, Incline DB Press, Lateral Raise/Cable
- **Exercises B:** Lat Pulldown, Cable Row, Deadlift DB, Lunges, Bicep Curl, Core
- **Flags:** `++ + - -- !! ! NXT ?? injured`
- **Historical data:** seeded from GymLog.pdf (Nov 2025 – Apr 2026, ~39 sessions)
- **Firestore:** `gym_sessions/{date}`

## Book Blog

- **Source:** `myBookBlog.pdf` (main branch) — parsed with pdftotext
- **Coverage:** ~100+ books, 2008–2025, organized by year
- **Tags:** investing, tech/product, parenting, mindfulness, science/health, history/society, psychology, fiction/other
- **Status values:** finished, reading, gave up, planning
- **Rating:** 0–5 stars

## Git

- **Deploy branch:** `claude/hello-world-click-counter-8k1UP`
- **Main branch:** `main` (source PDFs live here: GymLog.pdf, myBookBlog.pdf)
- GitHub Pages serves from the deploy branch

## GitHub Actions Secrets (for sync job)

- `GARMIN_EMAIL`, `GARMIN_PASSWORD`, `GARMIN_TOTP_SECRET`
- `FIREBASE_CREDENTIALS_JSON`
- `GARMIN_MFA_CODE` (optional one-shot fallback)

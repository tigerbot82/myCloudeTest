#!/usr/bin/env python3
"""
Garmin → Firestore sync script.

Fetches the last N days of Garmin health data and writes each day as a
document in the Firestore `health_daily` collection, keyed by YYYY-MM-DD.

Required environment variables:
  GARMIN_TOKENS     - base64 token blob from garmin_get_tokens.py (preferred)
  GARMIN_EMAIL      - fallback: Garmin Connect email (may hit 429 from cloud IPs)
  GARMIN_PASSWORD   - fallback: Garmin Connect password
  FIREBASE_CREDENTIALS_JSON - full JSON string of your Firebase service account key
  SYNC_DAYS         - (optional) number of past days to sync, default 7
"""

import base64
import json
import os
import tempfile
from datetime import date, timedelta

import garminconnect
import firebase_admin
from firebase_admin import credentials, firestore


# ── config ────────────────────────────────────────────────────────────────────────────────

GARMIN_TOKENS   = os.environ.get("GARMIN_TOKENS")
GARMIN_EMAIL    = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
FIREBASE_CREDS  = os.environ["FIREBASE_CREDENTIALS_JSON"]
SYNC_DAYS       = int(os.environ.get("SYNC_DAYS", "7"))


# ── firebase init ───────────────────────────────────────────────────────────────────────────

cred = credentials.Certificate(json.loads(FIREBASE_CREDS))
firebase_admin.initialize_app(cred)
db = firestore.client()
col = db.collection("health_daily")


# ── garmin init ───────────────────────────────────────────────────────────────────────────────

if GARMIN_TOKENS:
    print("Authenticating via saved OAuth tokens...")
    token_data = json.loads(base64.b64decode(GARMIN_TOKENS).decode())
    with tempfile.TemporaryDirectory() as tmpdir:
        for fname, content in token_data.items():
            with open(os.path.join(tmpdir, fname), "w") as f:
                f.write(content)
        api = garminconnect.Garmin()
        api.login(tokenstore=tmpdir)
    print("Authenticated via tokens.")
elif GARMIN_EMAIL and GARMIN_PASSWORD:
    print("Logging in with email/password (may fail from cloud IPs)...")
    api = garminconnect.Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    api.login()
    print("Logged in.")
else:
    raise EnvironmentError("Set GARMIN_TOKENS (preferred) or GARMIN_EMAIL + GARMIN_PASSWORD")


# ── helpers ───────────────────────────────────────────────────────────────────────────────

def safe(fn, *args, **kwargs):
    """Call fn, return None on any error (don't abort the whole sync)."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"  warning: {fn.__name__} failed — {e}")
        return None


def minutes_to_hours(m):
    return round(m / 60, 2) if m is not None else None


# ── fetch + write ────────────────────────────────────────────────────────────────────────────

today = date.today()

for i in range(SYNC_DAYS - 1, -1, -1):
    d  = today - timedelta(days=i)
    ds = d.isoformat()          # "YYYY-MM-DD"

    print(f"\nSyncing {ds}...")

    # ── steps / daily summary ──────────────────────────────────────────────────
    summary = safe(api.get_stats, ds) or {}
    steps           = summary.get("totalSteps")
    calories        = summary.get("totalKilocalories")
    active_calories = summary.get("activeKilocalories")
    floors          = summary.get("floorsAscended")
    avg_stress      = summary.get("averageStressLevel")
    resting_hr      = summary.get("restingHeartRate")
    avg_hr          = summary.get("averageHeartRate")

    # ── heart rate ────────────────────────────────────────────────────────────────────
    hr_data = safe(api.get_heart_rates, ds) or {}
    max_hr  = hr_data.get("maxHeartRate")

    # ── body battery ──────────────────────────────────────────────────────────────────
    bb_list = safe(api.get_body_battery, ds, ds) or []
    bb      = bb_list[0] if bb_list else {}
    body_battery = {
        "start":   bb.get("startTimestampLocal") and bb.get("startValue"),
        "end":     bb.get("endValue"),
        "charged": bb.get("charged"),
        "drained": bb.get("drained"),
    } if bb else None

    # ── sleep ────────────────────────────────────────────────────────────────────────────
    sleep_raw = safe(api.get_sleep_data, ds) or {}
    sd        = sleep_raw.get("dailySleepDTO") or {}
    sleep = {
        "durationHours":   minutes_to_hours(sd.get("sleepTimeSeconds")  and sd["sleepTimeSeconds"]  // 60),
        "deepSleepHours":  minutes_to_hours(sd.get("deepSleepSeconds")  and sd["deepSleepSeconds"]  // 60),
        "lightSleepHours": minutes_to_hours(sd.get("lightSleepSeconds") and sd["lightSleepSeconds"] // 60),
        "remSleepHours":   minutes_to_hours(sd.get("remSleepSeconds")   and sd["remSleepSeconds"]   // 60),
        "awakeHours":      minutes_to_hours(sd.get("awakeSleepSeconds") and sd["awakeSleepSeconds"] // 60),
        "score":           sd.get("sleepScores", {}).get("overall", {}).get("value") if sd.get("sleepScores") else None,
    } if sd else None

    # ── HRV ───────────────────────────────────────────────────────────────────────────────
    hrv_raw = safe(api.get_hrv_data, ds) or {}
    hrv_sum = hrv_raw.get("hrvSummary") or {}
    hrv = {
        "lastNight": hrv_sum.get("lastNight"),
        "weeklyAvg": hrv_sum.get("weeklyAvg"),
        "status":    hrv_sum.get("hrvStatus"),
    } if hrv_sum else None

    # ── SpO2 ──────────────────────────────────────────────────────────────────────────────
    spo2_raw = safe(api.get_spo2_data, ds) or {}
    spo2_avg = spo2_raw.get("averageSpO2")
    spo2_min = spo2_raw.get("lowestSpO2")
    spo2 = {"avg": spo2_avg, "min": spo2_min} if spo2_avg else None

    # ── assemble document ──────────────────────────────────────────────────────────────────────────
    doc = {
        "date":           ds,
        "steps":          steps,
        "calories":       calories,
        "activeCalories": active_calories,
        "floors":         floors,
        "avgStress":      avg_stress,
        "restingHR":      resting_hr,
        "avgHR":          avg_hr,
        "maxHR":          max_hr,
        "bodyBattery":    body_battery,
        "sleep":          sleep,
        "hrv":            hrv,
        "spo2":           spo2,
        "syncedAt":       firestore.SERVER_TIMESTAMP,
    }

    # strip None values so Firestore stays clean
    doc = {k: v for k, v in doc.items() if v is not None}

    col.document(ds).set(doc, merge=True)
    print(f"  ✓ wrote {ds}: steps={steps}, sleep={sleep and sleep.get('durationHours')}h, hrv={hrv and hrv.get('lastNight')}")

print(f"\nSync complete — {SYNC_DAYS} days written to Firestore `health_daily`.")

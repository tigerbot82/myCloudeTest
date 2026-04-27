#!/usr/bin/env python3
"""
Garmin → Firestore sync script.

Auth priority:
  1. Tokens stored in Firestore _config/garmin_tokens  (auto-saved after login)
  2. GARMIN_TOKENS env var (base64 blob, legacy)
  3. Email + password + TOTP (fully automated — preferred for re-auth)
  4. Email + password + manual MFA code (fallback)

Required env vars:
  GARMIN_EMAIL              - Garmin Connect email
  GARMIN_PASSWORD           - Garmin Connect password
  FIREBASE_CREDENTIALS_JSON - Firebase service account key JSON string
  GARMIN_TOTP_SECRET        - TOTP secret from Garmin authenticator setup (recommended)
  GARMIN_MFA_CODE           - (optional) manual one-time MFA code fallback
  SYNC_DAYS                 - (optional) days to sync, default 7
"""

import base64
import json
import os
import tempfile
from datetime import date, timedelta

import pyotp
import garminconnect
import firebase_admin
from firebase_admin import credentials, firestore


# ── firebase init ─────────────────────────────────────────────────────────────

FIREBASE_CREDS = os.environ["FIREBASE_CREDENTIALS_JSON"]
cred = credentials.Certificate(json.loads(FIREBASE_CREDS))
firebase_admin.initialize_app(cred)
db  = firestore.client()
col = db.collection("health_daily")
cfg = db.collection("_config")

SYNC_DAYS          = int(os.environ.get("SYNC_DAYS", "7"))
GARMIN_EMAIL       = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD    = os.environ.get("GARMIN_PASSWORD")
GARMIN_TOTP_SECRET = os.environ.get("GARMIN_TOTP_SECRET", "").strip()
GARMIN_MFA_CODE    = os.environ.get("GARMIN_MFA_CODE", "").strip()
GARMIN_TOKENS      = os.environ.get("GARMIN_TOKENS")  # legacy


# ── token helpers ─────────────────────────────────────────────────────────────

def load_tokens_from_firestore():
    doc = cfg.document("garmin_tokens").get()
    if doc.exists:
        return doc.to_dict().get("tokens")
    return None


def save_tokens_to_firestore(api):
    with tempfile.TemporaryDirectory() as tmpdir:
        # try every known dump method across garminconnect versions
        dumped = False
        for attempt in [
            lambda: api.garth.dump(tmpdir),
            lambda: api.client.dump(tmpdir),
            lambda: api.dump(tmpdir),
        ]:
            try:
                attempt()
                dumped = True
                break
            except AttributeError:
                continue

        if not dumped:
            print("  warning: could not save tokens (unknown garminconnect version) — will re-auth next run")
            return

        token_data = {}
        for fname in os.listdir(tmpdir):
            with open(os.path.join(tmpdir, fname)) as f:
                token_data[fname] = f.read()

    if not token_data:
        print("  warning: token directory was empty — tokens not saved")
        return

    blob = base64.b64encode(json.dumps(token_data).encode()).decode()
    cfg.document("garmin_tokens").set({
        "tokens":    blob,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    })
    print("  ✓ tokens saved to Firestore — future runs won't need MFA")


def login_with_blob(blob):
    token_data = json.loads(base64.b64decode(blob).decode())
    with tempfile.TemporaryDirectory() as tmpdir:
        for fname, content in token_data.items():
            with open(os.path.join(tmpdir, fname), "w") as f:
                f.write(content)
        api = garminconnect.Garmin()
        api.login(tokenstore=tmpdir)
    return api


# ── garmin auth ───────────────────────────────────────────────────────────────

api = None

# 1. try Firestore tokens
blob = load_tokens_from_firestore()
if blob:
    print("Authenticating via Firestore tokens...")
    try:
        api = login_with_blob(blob)
        print("Authenticated via Firestore tokens.")
    except Exception as e:
        print(f"  Firestore token auth failed ({e}), falling back to login...")

# 2. try legacy GARMIN_TOKENS env var
if api is None and GARMIN_TOKENS:
    print("Authenticating via GARMIN_TOKENS env var...")
    try:
        api = login_with_blob(GARMIN_TOKENS)
        print("Authenticated via env token.")
        save_tokens_to_firestore(api)
    except Exception as e:
        print(f"  Env token auth failed ({e}), falling back to login...")

# 3. email + password (TOTP or manual MFA)
if api is None:
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        raise EnvironmentError("Set GARMIN_EMAIL and GARMIN_PASSWORD")

    def mfa_callback():
        import time

        # Option A: TOTP (fully automated, best option)
        if GARMIN_TOTP_SECRET:
            code = pyotp.TOTP(GARMIN_TOTP_SECRET).now()
            print(f"  using TOTP code (auto-computed): {code}")
            return code

        # Option B: Firestore relay — write a waiting flag, poll for user input
        print("  MFA required — writing pending request to Firestore...")
        mfa_ref = cfg.document("garmin_mfa_pending")
        mfa_ref.set({"status": "waiting", "requestedAt": firestore.SERVER_TIMESTAMP})
        print("  open health.html and enter the code from your Garmin email")
        print("  polling for up to 6 minutes...")

        for _ in range(72):   # 72 × 5s = 6 minutes
            time.sleep(5)
            snap = mfa_ref.get()
            if snap.exists:
                data = snap.to_dict()
                if data.get("status") == "submitted" and data.get("code"):
                    code = str(data["code"]).strip()
                    mfa_ref.delete()
                    print(f"  received MFA code via Firestore: {code}")
                    return code

        mfa_ref.delete()

        # Option C: manual env var fallback
        if GARMIN_MFA_CODE:
            print(f"  timed out on Firestore relay, using GARMIN_MFA_CODE: {GARMIN_MFA_CODE}")
            return GARMIN_MFA_CODE

        raise RuntimeError("Timed out waiting for MFA code (6 minutes)")

    print("Logging in with email/password...")
    api = garminconnect.Garmin(GARMIN_EMAIL, GARMIN_PASSWORD, prompt_mfa=mfa_callback)
    api.login()
    print("Logged in.")
    save_tokens_to_firestore(api)


# ── helpers ───────────────────────────────────────────────────────────────────

SYNC_DEBUG = os.environ.get("SYNC_DEBUG", "").lower() in ("1", "true", "yes")


def safe(fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
        if SYNC_DEBUG:
            import json as _json
            preview = _json.dumps(result, default=str)[:400]
            print(f"  [debug] {fn.__name__}: {preview}")
        return result
    except Exception as e:
        print(f"  warning: {fn.__name__} failed — {e}")
        return None


def minutes_to_hours(m):
    return round(m / 60, 2) if m is not None else None


# ── fetch + write ─────────────────────────────────────────────────────────────

today = date.today()

for i in range(SYNC_DAYS - 1, -1, -1):
    d  = today - timedelta(days=i)
    ds = d.isoformat()

    print(f"\nSyncing {ds}...")

    summary         = safe(api.get_stats, ds) or {}
    steps           = summary.get("totalSteps")
    calories        = summary.get("totalKilocalories")
    active_calories = summary.get("activeKilocalories")
    floors          = summary.get("floorsAscended")
    avg_stress      = summary.get("averageStressLevel")
    resting_hr      = summary.get("restingHeartRate")
    avg_hr          = summary.get("averageHeartRate")
    distance_km     = round(summary["totalDistanceMeters"] / 1000, 2) if summary.get("totalDistanceMeters") else None
    mod_min         = summary.get("moderateIntensityMinutes") or 0
    vig_min         = summary.get("vigorousIntensityMinutes") or 0
    intensity_min   = (mod_min + vig_min * 2) or None  # WHO formula: vigorous counts double
    sedentary_hrs   = round(summary["sedentarySeconds"] / 3600, 2) if summary.get("sedentarySeconds") else None

    hr_data = safe(api.get_heart_rates, ds) or {}
    max_hr  = hr_data.get("maxHeartRate")

    bb_list = safe(api.get_body_battery, ds, ds) or []
    bb      = bb_list[0] if bb_list else {}
    body_battery = {
        "start":   bb.get("startTimestampLocal") and bb.get("startValue"),
        "end":     bb.get("endValue"),
        "charged": bb.get("charged"),
        "drained": bb.get("drained"),
    } if bb else None

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

    hrv_raw = safe(api.get_hrv_data, ds) or {}
    hrv_sum = hrv_raw.get("hrvSummary") or {}
    hrv = {
        "lastNight": hrv_sum.get("lastNight"),
        "weeklyAvg": hrv_sum.get("weeklyAvg"),
        "status":    hrv_sum.get("status") or hrv_sum.get("hrvStatus"),
    } if hrv_sum else None

    spo2_raw = safe(api.get_spo2_data, ds) or {}
    spo2_avg = spo2_raw.get("averageSpO2")
    spo2_min = spo2_raw.get("lowestSpO2")
    spo2 = {"avg": spo2_avg, "min": spo2_min} if spo2_avg else None

    resp_raw  = safe(api.get_respiration_data, ds) or {}
    resp_avg  = resp_raw.get("avgWakingRespirationValue") or resp_raw.get("avgRespirationValue")
    resp_sleep = resp_raw.get("avgSleepRespirationValue")
    respiration = {"avg": resp_avg, "sleep": resp_sleep} if (resp_avg or resp_sleep) else None

    mm_raw    = safe(api.get_max_metrics, ds) or {}
    vo2max    = None
    for entry in (mm_raw if isinstance(mm_raw, list) else []):
        v = entry.get("generic", {}).get("vo2MaxPreciseValue") or entry.get("generic", {}).get("vo2MaxValue")
        if v:
            vo2max = round(float(v), 1)
            break

    tr_raw   = safe(api.get_training_readiness, ds) or []
    tr_score = None
    tr_list  = tr_raw if isinstance(tr_raw, list) else ([tr_raw] if tr_raw else [])
    for entry in tr_list:
        if not isinstance(entry, dict):
            continue
        s = entry.get("score") or entry.get("trainingReadinessScore")
        if s is not None:
            tr_score = int(s)
            break

    body_raw  = safe(api.get_body_composition, ds) or {}
    weight_kg = None
    bmi       = None
    if isinstance(body_raw, dict):
        wt = body_raw.get("weight") or body_raw.get("startWeight")
        if wt:
            weight_kg = round(wt / 1000, 1) if wt > 500 else round(float(wt), 1)
        bmi = body_raw.get("bmi")

    doc = {
        "date":             ds,
        "steps":            steps,
        "calories":         calories,
        "activeCalories":   active_calories,
        "floors":           floors,
        "distanceKm":       distance_km,
        "intensityMinutes": intensity_min,
        "sedentaryHours":   sedentary_hrs,
        "avgStress":        avg_stress,
        "restingHR":        resting_hr,
        "avgHR":            avg_hr,
        "maxHR":            max_hr,
        "bodyBattery":      body_battery,
        "sleep":            sleep,
        "hrv":              hrv,
        "spo2":             spo2,
        "respiration":      respiration,
        "vo2max":           vo2max,
        "trainingReadiness": tr_score,
        "weight":           {"kg": weight_kg, "bmi": bmi} if weight_kg else None,
        "syncedAt":         firestore.SERVER_TIMESTAMP,
    }
    doc = {k: v for k, v in doc.items() if v is not None}

    col.document(ds).set(doc, merge=True)
    bb_end = body_battery.get('end') if body_battery else None
    print(f"  ✓ {ds}: steps={steps}, sleep={sleep and sleep.get('durationHours')}h, hrv={hrv and hrv.get('lastNight')}, battery={bb_end}, resp={resp_avg}, vo2={vo2max}, readiness={tr_score}, spo2={spo2 and spo2.get('avg')}")

print(f"\nSync complete — {SYNC_DAYS} days written to Firestore `health_daily`.")

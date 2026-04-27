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

def first(d, *keys, default=None):
    """Return the first non-None value found among keys in dict d."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def score_val(d):
    """Extract a numeric score from {value: X} or plain X."""
    if isinstance(d, dict):
        return d.get("value")
    return d


today = date.today()

for i in range(SYNC_DAYS - 1, -1, -1):
    d  = today - timedelta(days=i)
    ds = d.isoformat()

    print(f"\nSyncing {ds}...")

    # ── activity summary (single API call covers most daily metrics) ──────────
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

    # ── body battery ──────────────────────────────────────────────────────────
    bb_list = safe(api.get_body_battery, ds, ds) or []
    bb      = bb_list[0] if bb_list else {}
    if bb:
        vals = bb.get("bodyBatteryValuesArray") or []
        body_battery = {
            "start":   vals[0][1]  if vals else None,
            "end":     vals[-1][1] if vals else None,
            "charged": bb.get("charged"),
            "drained": bb.get("drained"),
        }
    else:
        body_battery = None

    # ── sleep (+ sub-scores) ──────────────────────────────────────────────────
    sleep_raw = safe(api.get_sleep_data, ds) or {}
    sd        = sleep_raw.get("dailySleepDTO") or {}
    if sd:
        sc = sd.get("sleepScores") or {}
        sleep = {
            "durationHours":   minutes_to_hours(sd.get("sleepTimeSeconds")  and sd["sleepTimeSeconds"]  // 60),
            "napHours":        minutes_to_hours(sd.get("napTimeSeconds")     and sd["napTimeSeconds"]     // 60),
            "deepSleepHours":  minutes_to_hours(sd.get("deepSleepSeconds")  and sd["deepSleepSeconds"]  // 60),
            "lightSleepHours": minutes_to_hours(sd.get("lightSleepSeconds") and sd["lightSleepSeconds"] // 60),
            "remSleepHours":   minutes_to_hours(sd.get("remSleepSeconds")   and sd["remSleepSeconds"]   // 60),
            "awakeHours":      minutes_to_hours(sd.get("awakeSleepSeconds") and sd["awakeSleepSeconds"] // 60),
            "score":           score_val(sc.get("overall")),
            "bodyScore":       score_val(sc.get("body")),
            "mindScore":       score_val(sc.get("mind")),
            "remScore":        score_val(sc.get("remPercentage")),
            "deepScore":       score_val(sc.get("deepPercentage")),
            "restlessness":    score_val(sc.get("restlessness")),
        }
    else:
        sleep = None

    # ── HRV ───────────────────────────────────────────────────────────────────
    hrv_raw = safe(api.get_hrv_data, ds) or {}
    hrv_sum = hrv_raw.get("hrvSummary") or {}
    hrv = {
        "lastNight": hrv_sum.get("lastNightAvg") or hrv_sum.get("lastNight"),
        "weeklyAvg": hrv_sum.get("weeklyAvg"),
        "status":    hrv_sum.get("status") or hrv_sum.get("hrvStatus"),
    } if hrv_sum else None

    # ── SpO2 + respiration ────────────────────────────────────────────────────
    spo2_raw  = safe(api.get_spo2_data, ds) or {}
    spo2_avg  = spo2_raw.get("averageSpO2")
    spo2_min  = spo2_raw.get("lowestSpO2")
    spo2      = {"avg": spo2_avg, "min": spo2_min} if spo2_avg else None

    resp_raw   = safe(api.get_respiration_data, ds) or {}
    resp_avg   = resp_raw.get("avgWakingRespirationValue") or resp_raw.get("avgRespirationValue")
    resp_sleep = resp_raw.get("avgSleepRespirationValue")
    respiration = {"avg": resp_avg, "sleep": resp_sleep} if (resp_avg or resp_sleep) else None

    # ── VO2max ────────────────────────────────────────────────────────────────
    mm_raw = safe(api.get_max_metrics, ds) or {}
    vo2max = None
    for entry in (mm_raw if isinstance(mm_raw, list) else []):
        v = entry.get("generic", {}).get("vo2MaxPreciseValue") or entry.get("generic", {}).get("vo2MaxValue")
        if v:
            vo2max = round(float(v), 1)
            break

    # ── training readiness ────────────────────────────────────────────────────
    tr_raw   = safe(api.get_training_readiness, ds) or []
    tr_score = None
    for entry in (tr_raw if isinstance(tr_raw, list) else ([tr_raw] if tr_raw else [])):
        if not isinstance(entry, dict):
            continue
        s = entry.get("score") or entry.get("trainingReadinessScore")
        if s is not None:
            tr_score = int(s)
            break

    # ── weight / body composition ─────────────────────────────────────────────
    wi_raw = safe(api.get_daily_weigh_ins, ds) or {}
    weight = None
    wi_list = wi_raw.get("dateWeightList") or wi_raw.get("allWeightMetrics") or []
    if wi_list:
        wi = wi_list[0]
        wt = wi.get("weight") or wi.get("weightInGrams")
        if wt:
            wt_kg = round(wt / 1000, 1) if wt > 500 else round(float(wt), 1)
            weight = {
                "kg":        wt_kg,
                "bmi":       wi.get("bmi"),
                "bodyFatPct": wi.get("bodyFat") or wi.get("bodyFatPercentage"),
                "muscleMassKg": round(wi["muscleMass"] / 1000, 1) if wi.get("muscleMass") and wi["muscleMass"] > 500 else wi.get("muscleMass"),
                "bodyWaterPct": wi.get("bodyWater") or wi.get("bodyWaterPercentage"),
            }

    # ── stress (detailed) ─────────────────────────────────────────────────────
    stress_raw = safe(api.get_stress_data, ds) or {}
    stress_detail = None
    if stress_raw:
        stress_detail = {
            "avg":             stress_raw.get("overallStressLevel") or avg_stress,
            "restPct":         stress_raw.get("restStressPercentage"),
            "activityPct":     stress_raw.get("activityStressPercentage"),
            "lowPct":          stress_raw.get("lowStressPercentage"),
            "mediumPct":       stress_raw.get("mediumStressPercentage"),
            "highPct":         stress_raw.get("highStressPercentage"),
        }
        stress_detail = {k: v for k, v in stress_detail.items() if v is not None} or None

    # ── hydration ─────────────────────────────────────────────────────────────
    hyd_raw    = safe(api.get_hydration_data, ds) or {}
    hydration  = None
    hyd_intake = first(hyd_raw, "totalIntakeInML", "valueInML", "sweatLossInML")
    hyd_goal   = first(hyd_raw, "goalInML", "dailyGoalInML")
    if hyd_intake:
        hydration = {"intakeMl": hyd_intake, "goalMl": hyd_goal}

    # ── endurance score ───────────────────────────────────────────────────────
    end_raw = safe(api.get_endurance_score, ds) or {}
    endurance_score = None
    if end_raw:
        v = end_raw.get("overallScore") or end_raw.get("score")
        if v is not None:
            endurance_score = score_val(v) if isinstance(v, dict) else v

    # ── hill score ────────────────────────────────────────────────────────────
    hill_raw = safe(api.get_hill_score, ds) or {}
    hill_score = None
    if hill_raw:
        v = hill_raw.get("overallScore") or hill_raw.get("score")
        if v is not None:
            hill_score = score_val(v) if isinstance(v, dict) else v

    # ── fitness age ───────────────────────────────────────────────────────────
    fa_raw     = safe(api.get_fitnessage_data, ds) or {}
    fitness_age = first(fa_raw, "biologicalAge", "fitnessAge", "bilogicalAge")

    # ── race predictions ──────────────────────────────────────────────────────
    rp_raw = safe(api.get_race_predictions, ds, ds) or {}
    race_predictions = None
    rp = rp_raw if isinstance(rp_raw, dict) else (rp_raw[0] if isinstance(rp_raw, list) and rp_raw else {})
    if rp:
        def secs_to_min(s):
            return round(s / 60, 1) if s else None
        race_predictions = {
            "5kMin":          secs_to_min(first(rp, "time5K", "fiveKTime")),
            "10kMin":         secs_to_min(first(rp, "time10K", "tenKTime")),
            "halfMarathonMin": secs_to_min(first(rp, "timeHalfMarathon", "halfMarathonTime")),
            "marathonMin":    secs_to_min(first(rp, "timeMarathon", "marathonTime")),
        }
        race_predictions = {k: v for k, v in race_predictions.items() if v} or None

    # ── blood pressure ────────────────────────────────────────────────────────
    bp_raw = safe(api.get_blood_pressure, ds, ds) or {}
    blood_pressure = None
    bp_list = bp_raw.get("measurementSummaries") or bp_raw.get("bloodPressureSummaries") or []
    if not bp_list and isinstance(bp_raw, list):
        bp_list = bp_raw
    if bp_list:
        bp = bp_list[0]
        sys = first(bp, "systolic", "systolicValue")
        dia = first(bp, "diastolic", "diastolicValue")
        if sys and dia:
            blood_pressure = {"systolic": sys, "diastolic": dia, "pulse": bp.get("pulse")}

    # ── lactate threshold ─────────────────────────────────────────────────────
    lt_raw = safe(api.get_lactate_threshold, latest=False, start_date=ds, end_date=ds) or {}
    lactate_threshold = None
    lt = lt_raw if isinstance(lt_raw, dict) else (lt_raw[0] if isinstance(lt_raw, list) and lt_raw else {})
    if lt:
        lt_hr = first(lt, "heartRate", "heartRateBpm", "ltHeartRate")
        if lt_hr:
            lactate_threshold = {
                "heartRate": lt_hr,
                "speed":     lt.get("speed") or lt.get("ltSpeed"),
                "pace":      lt.get("pace") or lt.get("ltPace"),
            }

    # ── running tolerance ─────────────────────────────────────────────────────
    rt_raw = safe(api.get_running_tolerance, ds, ds, aggregation="daily") or []
    running_tolerance = None
    rt = rt_raw[0] if isinstance(rt_raw, list) and rt_raw else (rt_raw if isinstance(rt_raw, dict) else {})
    if rt:
        acute  = first(rt, "acuteLoad", "acuteTrainingLoad")
        chronic = first(rt, "chronicLoad", "chronicTrainingLoad")
        if acute or chronic:
            running_tolerance = {
                "acuteLoad":   acute,
                "chronicLoad": chronic,
                "loadRatio":   rt.get("loadRatio") or rt.get("acwr"),
            }

    # ── assemble document ─────────────────────────────────────────────────────
    doc = {
        "date":              ds,
        "steps":             steps,
        "calories":          calories,
        "activeCalories":    active_calories,
        "floors":            floors,
        "distanceKm":        distance_km,
        "intensityMinutes":  intensity_min,
        "sedentaryHours":    sedentary_hrs,
        "avgStress":         avg_stress,
        "stressDetail":      stress_detail,
        "restingHR":         resting_hr,
        "avgHR":             avg_hr,
        "maxHR":             max_hr,
        "bodyBattery":       body_battery,
        "sleep":             sleep,
        "hrv":               hrv,
        "spo2":              spo2,
        "respiration":       respiration,
        "hydration":         hydration,
        "vo2max":            vo2max,
        "trainingReadiness": tr_score,
        "enduranceScore":    endurance_score,
        "hillScore":         hill_score,
        "fitnessAge":        fitness_age,
        "racePredictions":   race_predictions,
        "weight":            weight,
        "bloodPressure":     blood_pressure,
        "lactateThreshold":  lactate_threshold,
        "runningTolerance":  running_tolerance,
        "syncedAt":          firestore.SERVER_TIMESTAMP,
    }
    doc = {k: v for k, v in doc.items() if v is not None}

    col.document(ds).set(doc, merge=True)
    bb_end = body_battery.get("end") if body_battery else None
    print(f"  ✓ {ds}: steps={steps}, sleep={sleep and sleep.get('durationHours')}h, "
          f"hrv={hrv and hrv.get('lastNight')}, battery={bb_end}, "
          f"resp={resp_avg}, readiness={tr_score}, "
          f"endurance={endurance_score}, fitness_age={fitness_age}")

print(f"\nSync complete — {SYNC_DAYS} days written to Firestore `health_daily`.")

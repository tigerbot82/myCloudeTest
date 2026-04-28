"""
MCP server exposing personal health + gym data from Firestore.
Run via Claude Code config — do not start manually.

Credentials: set FIREBASE_CREDENTIALS_JSON (JSON string) or
             GOOGLE_APPLICATION_CREDENTIALS (path to service account file).
"""

import json
import os
import statistics
from datetime import date, timedelta

import firebase_admin
from firebase_admin import credentials, firestore
from mcp.server.fastmcp import FastMCP

# ── Firebase init ──────────────────────────────────────────────────────────────

def _init_firebase():
    if firebase_admin._apps:
        return firestore.client()
    raw = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if raw:
        cred = credentials.Certificate(json.loads(raw))
    else:
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not path:
            raise RuntimeError(
                "Set FIREBASE_CREDENTIALS_JSON or GOOGLE_APPLICATION_CREDENTIALS"
            )
        cred = credentials.Certificate(path)
    firebase_admin.initialize_app(cred, {"projectId": "time-tracker-df33b"})
    return firestore.client()


db = _init_firebase()
mcp = FastMCP("health-data")

# ── Helpers ────────────────────────────────────────────────────────────────────

def _date_range(n: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=n - 1)
    return start.isoformat(), end.isoformat()


def _fetch_health(n: int) -> list[dict]:
    start, end = _date_range(n)
    docs = (
        db.collection("health_daily")
        .where("__name__", ">=", start)
        .where("__name__", "<=", end)
        .stream()
    )
    results = []
    for doc in docs:
        d = doc.to_dict() or {}
        d["date"] = doc.id
        results.append(d)
    return sorted(results, key=lambda x: x["date"])


def _safe(doc: dict, *keys):
    """Drill into nested keys, return None if missing."""
    v = doc
    for k in keys:
        if not isinstance(v, dict):
            return None
        v = v.get(k)
    return v


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_health_days(days: int = 30) -> str:
    """
    Return raw daily health metrics for the last `days` days.
    Includes HRV, sleep, steps, stress, body battery, resting HR, weight, etc.
    Each record is one Firestore health_daily document.
    """
    docs = _fetch_health(days)
    if not docs:
        return "No health data found for that range."
    return json.dumps(docs, default=str, indent=2)


@mcp.tool()
def get_health_summary(days: int = 30) -> str:
    """
    Compute mean ± stdev for key biometrics over the last `days` days.
    Returns a compact summary table — use this for quick trend questions.
    """
    docs = _fetch_health(days)
    if not docs:
        return "No health data found."

    metrics = {
        "hrv_lastNight":       lambda d: _safe(d, "hrv", "lastNight"),
        "sleep_score":         lambda d: _safe(d, "sleep", "score"),
        "sleep_hours":         lambda d: (
            (_safe(d, "sleep", "duration") or 0) / 3600
            if _safe(d, "sleep", "duration") else None
        ),
        "steps":               lambda d: d.get("steps"),
        "resting_hr":          lambda d: d.get("restingHeartRate"),
        "stress_avg":          lambda d: _safe(d, "stress", "avg"),
        "body_battery_max":    lambda d: _safe(d, "bodyBattery", "max"),
        "weight_kg":           lambda d: d.get("weight"),
        "vo2max":              lambda d: d.get("vo2max"),
        "training_readiness":  lambda d: d.get("trainingReadiness"),
        "respiration_avg":     lambda d: _safe(d, "respiration", "avg"),
        "intensity_minutes":   lambda d: d.get("intensityMinutes"),
    }

    rows = []
    for name, fn in metrics.items():
        vals = [v for d in docs if (v := fn(d)) is not None]
        if not vals:
            rows.append(f"{name:30s}  no data")
            continue
        mean = statistics.mean(vals)
        stdev = statistics.stdev(vals) if len(vals) > 1 else 0
        rows.append(
            f"{name:30s}  mean={mean:7.2f}  stdev={stdev:6.2f}  n={len(vals)}"
        )

    header = f"Health summary — last {days} days ({len(docs)} records)\n"
    return header + "\n".join(rows)


@mcp.tool()
def get_gym_sessions(n: int = 20) -> str:
    """
    Return the last `n` gym sessions (workout A/B, exercises, sets/reps/weight, flags).
    """
    docs = list(
        db.collection("gym_sessions")
        .order_by("__name__", direction=firestore.Query.DESCENDING)
        .limit(n)
        .stream()
    )
    if not docs:
        return "No gym sessions found."
    results = []
    for doc in docs:
        d = doc.to_dict() or {}
        d["date"] = doc.id
        results.append(d)
    return json.dumps(sorted(results, key=lambda x: x["date"]), default=str, indent=2)


@mcp.tool()
def get_metric_trend(metric: str, days: int = 60) -> str:
    """
    Return daily values for a single metric over the last `days` days.
    Supported metrics: hrv_lastNight, sleep_score, sleep_hours, steps,
    resting_hr, stress_avg, body_battery_max, weight_kg, vo2max,
    training_readiness, intensity_minutes.
    Returns date + value pairs, skipping days with no data.
    """
    extractors = {
        "hrv_lastNight":      lambda d: _safe(d, "hrv", "lastNight"),
        "sleep_score":        lambda d: _safe(d, "sleep", "score"),
        "sleep_hours":        lambda d: (
            (_safe(d, "sleep", "duration") or 0) / 3600
            if _safe(d, "sleep", "duration") else None
        ),
        "steps":              lambda d: d.get("steps"),
        "resting_hr":         lambda d: d.get("restingHeartRate"),
        "stress_avg":         lambda d: _safe(d, "stress", "avg"),
        "body_battery_max":   lambda d: _safe(d, "bodyBattery", "max"),
        "weight_kg":          lambda d: d.get("weight"),
        "vo2max":             lambda d: d.get("vo2max"),
        "training_readiness": lambda d: d.get("trainingReadiness"),
        "intensity_minutes":  lambda d: d.get("intensityMinutes"),
    }
    fn = extractors.get(metric)
    if not fn:
        return f"Unknown metric '{metric}'. Supported: {', '.join(extractors)}"

    docs = _fetch_health(days)
    pairs = [{"date": d["date"], "value": fn(d)} for d in docs if fn(d) is not None]
    if not pairs:
        return f"No data for '{metric}' in the last {days} days."
    return json.dumps(pairs, indent=2)


if __name__ == "__main__":
    mcp.run()

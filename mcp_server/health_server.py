"""
MCP server exposing personal health + gym data from Firestore.
Uses Firestore REST API (not gRPC) to work in proxied cloud environments.
Run via Claude Code config — do not start manually.

Credentials: set GOOGLE_APPLICATION_CREDENTIALS (path to service account JSON).
"""

import json
import os
import statistics
from datetime import date, timedelta

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest
from mcp.server.fastmcp import FastMCP

# ── Auth + REST client ─────────────────────────────────────────────────────────

PROJECT = "time-tracker-df33b"
FIRESTORE_BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"
SCOPES = ["https://www.googleapis.com/auth/datastore"]

_creds = None

def _get_token() -> str:
    global _creds
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not key_path:
        raise RuntimeError("Set GOOGLE_APPLICATION_CREDENTIALS to the service account JSON path")
    if _creds is None:
        _creds = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
    if not _creds.valid:
        _creds.refresh(GoogleAuthRequest())
    return _creds.token


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}"}


def _fs_value(v: dict):
    """Unwrap a Firestore REST value object to a Python scalar."""
    if v is None:
        return None
    for t in ("integerValue", "doubleValue", "booleanValue", "stringValue",
              "timestampValue", "nullValue"):
        if t in v:
            raw = v[t]
            if t == "integerValue":
                return int(raw)
            if t == "doubleValue":
                return float(raw)
            if t == "nullValue":
                return None
            return raw
    if "mapValue" in v:
        return {k: _fs_value(fv) for k, fv in v["mapValue"].get("fields", {}).items()}
    if "arrayValue" in v:
        return [_fs_value(item) for item in v["arrayValue"].get("values", [])]
    return None


def _doc_to_dict(doc: dict) -> dict:
    """Convert a Firestore REST document to a plain dict."""
    name = doc.get("name", "")
    doc_id = name.split("/")[-1]
    fields = {k: _fs_value(v) for k, v in doc.get("fields", {}).items()}
    fields["date"] = doc_id
    return fields


def _query_range(collection: str, start: str, end: str) -> list[dict]:
    """Fetch documents from `collection` where doc ID is between start and end."""
    url = f"{FIRESTORE_BASE}:runQuery"
    body = {
        "structuredQuery": {
            "from": [{"collectionId": collection}],
            "where": {
                "compositeFilter": {
                    "op": "AND",
                    "filters": [
                        {"fieldFilter": {"field": {"fieldPath": "__name__"},
                                         "op": "GREATER_THAN_OR_EQUAL",
                                         "value": {"referenceValue": f"projects/{PROJECT}/databases/(default)/documents/{collection}/{start}"}}},
                        {"fieldFilter": {"field": {"fieldPath": "__name__"},
                                         "op": "LESS_THAN_OR_EQUAL",
                                         "value": {"referenceValue": f"projects/{PROJECT}/databases/(default)/documents/{collection}/{end}"}}}
                    ]
                }
            },
            "orderBy": [{"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}]
        }
    }
    resp = requests.post(url, json=body, headers=_headers(), timeout=30)
    resp.raise_for_status()
    results = []
    for item in resp.json():
        if "document" in item:
            results.append(_doc_to_dict(item["document"]))
    return results


def _query_latest(collection: str, n: int) -> list[dict]:
    """Fetch the latest n documents from a collection, ordered by doc ID desc."""
    url = f"{FIRESTORE_BASE}:runQuery"
    body = {
        "structuredQuery": {
            "from": [{"collectionId": collection}],
            "orderBy": [{"field": {"fieldPath": "__name__"}, "direction": "DESCENDING"}],
            "limit": n
        }
    }
    resp = requests.post(url, json=body, headers=_headers(), timeout=30)
    resp.raise_for_status()
    results = []
    for item in resp.json():
        if "document" in item:
            results.append(_doc_to_dict(item["document"]))
    return sorted(results, key=lambda x: x["date"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _date_range(n: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=n - 1)
    return start.isoformat(), end.isoformat()


def _fetch_health(n: int) -> list[dict]:
    start, end = _date_range(n)
    return _query_range("health_daily", start, end)


def _safe(doc: dict, *keys):
    v = doc
    for k in keys:
        if not isinstance(v, dict):
            return None
        v = v.get(k)
    return v


# ── MCP server ─────────────────────────────────────────────────────────────────

mcp = FastMCP("health-data")


@mcp.tool()
def get_health_days(days: int = 30) -> str:
    """
    Return raw daily health metrics for the last `days` days.
    Includes HRV, sleep, steps, stress, body battery, resting HR, weight, etc.
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
        "hrv_lastNight":      lambda d: _safe(d, "hrv", "lastNight"),
        "sleep_score":        lambda d: _safe(d, "sleep", "score"),
        "sleep_hours":        lambda d: _safe(d, "sleep", "durationHours"),
        "steps":              lambda d: d.get("steps"),
        "resting_hr":         lambda d: d.get("restingHR"),
        "stress_avg":         lambda d: d.get("avgStress"),
        "body_battery_end":   lambda d: _safe(d, "bodyBattery", "end"),
        "weight_kg":          lambda d: d.get("weight"),
        "vo2max":             lambda d: d.get("vo2max"),
        "training_readiness": lambda d: d.get("trainingReadiness"),
        "intensity_minutes":  lambda d: d.get("intensityMinutes"),
    }

    rows = []
    for name, fn in metrics.items():
        vals = [v for d in docs if (v := fn(d)) is not None and isinstance(v, (int, float))]
        if not vals:
            rows.append(f"{name:30s}  no data")
            continue
        mean = statistics.mean(vals)
        stdev = statistics.stdev(vals) if len(vals) > 1 else 0
        rows.append(f"{name:30s}  mean={mean:7.2f}  stdev={stdev:6.2f}  n={len(vals)}")

    return f"Health summary — last {days} days ({len(docs)} records)\n" + "\n".join(rows)


@mcp.tool()
def get_gym_sessions(n: int = 20) -> str:
    """
    Return the last `n` gym sessions (workout A/B, exercises, sets/reps/weight, flags).
    """
    docs = _query_latest("gym_sessions", n)
    if not docs:
        return "No gym sessions found."
    return json.dumps(docs, default=str, indent=2)


@mcp.tool()
def get_metric_trend(metric: str, days: int = 60) -> str:
    """
    Return daily values for a single metric over the last `days` days.
    Supported metrics: hrv_lastNight, sleep_score, sleep_hours, steps,
    resting_hr, stress_avg, body_battery_max, weight_kg, vo2max,
    training_readiness, intensity_minutes.
    """
    extractors = {
        "hrv_lastNight":      lambda d: _safe(d, "hrv", "lastNight"),
        "sleep_score":        lambda d: _safe(d, "sleep", "score"),
        "sleep_hours":        lambda d: _safe(d, "sleep", "durationHours"),
        "steps":              lambda d: d.get("steps"),
        "resting_hr":         lambda d: d.get("restingHR"),
        "stress_avg":         lambda d: d.get("avgStress"),
        "body_battery_end":   lambda d: _safe(d, "bodyBattery", "end"),
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

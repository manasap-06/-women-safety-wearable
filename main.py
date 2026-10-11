import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "database.db")))
API_KEY = os.getenv("WEARABLE_API_KEY", "local-dev-key-change-me")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://127.0.0.1:5500,http://localhost:5500,http://127.0.0.1:8000,http://localhost:8000,http://localhost:3000")

app = Flask(__name__)
allowed_origins = [origin.strip() for origin in CORS_ORIGINS.split(",") if origin.strip()]
CORS(app, resources={r"/api/*": {"origins": allowed_origins or "*"}}, allow_headers=["Content-Type", "X-API-Key"])

# World Bank indicator: proportion of ever-partnered women ages 15-49 subjected
# to physical and/or sexual violence by an intimate partner in the previous 12 months.
WB_INDICATOR = "SG.VAW.1549.ZS"
WB_URL = f"https://api.worldbank.org/v2/country/{{country}}/indicator/{WB_INDICATOR}?format=json&per_page=100"
STATS_CACHE = {"fetched_at": 0, "payload": None, "error": None}
STATS_CACHE_SECONDS = 6 * 60 * 60


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'SOS',
            message TEXT NOT NULL DEFAULT 'Emergency alert received',
            zone TEXT NOT NULL DEFAULT 'Unknown zone',
            latitude REAL,
            longitude REAL,
            source TEXT NOT NULL DEFAULT 'wearable',
            created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            last_seen TEXT NOT NULL,
            battery_percent INTEGER,
            zone TEXT NOT NULL DEFAULT 'Unknown zone'
        )""")
        conn.commit()


def record_device(conn, device_id, battery_percent=None, zone="Unknown zone"):
    conn.execute("""INSERT INTO devices(device_id, last_seen, battery_percent, zone)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET last_seen=excluded.last_seen,
        battery_percent=COALESCE(excluded.battery_percent, devices.battery_percent), zone=excluded.zone""",
        (device_id, utc_now(), battery_percent, zone))


def row_to_dict(row):
    return {key: row[key] for key in row.keys()}


def fetch_indicator(country_code):
    url = WB_URL.format(country=country_code)
    response = requests.get(url, timeout=12, headers={"User-Agent": "WomenSafetyWearableStudentProject/1.0"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        return None
    rows = [row for row in payload[1] if row.get("value") is not None]
    if not rows:
        return None
    latest = max(rows, key=lambda row: int(row["date"]))
    return {
        "country_code": country_code,
        "country": latest.get("country", {}).get("value", country_code),
        "indicator": WB_INDICATOR,
        "indicator_name": latest.get("indicator", {}).get("value", "Partner violence against women"),
        "year": int(latest["date"]),
        "value_percent": latest["value"],
        "unit": "% of ever-partnered women ages 15-49",
        "source": "World Bank Gender Data / UN Statistics Division / WHO",
        "source_url": f"https://data.worldbank.org/indicator/{WB_INDICATOR}?locations={country_code}",
    }


def get_vawg_stats(force=False):
    now = time.time()
    if not force and STATS_CACHE["payload"] and now - STATS_CACHE["fetched_at"] < STATS_CACHE_SECONDS:
        return STATS_CACHE["payload"], None
    result = {"indicator": WB_INDICATOR, "definition": "Percentage of ever-partnered women ages 15–49 subjected to physical and/or sexual violence by a current or former intimate partner in the previous 12 months.", "data_frequency_note": "These are official survey/statistical observations, not live incident counts. The dashboard refreshes its API cache every 6 hours; the latest observation year can be older.", "retrieved_at": utc_now(), "records": []}
    errors = []
    for code in ("IND", "WLD"):
        try:
            item = fetch_indicator(code)
            if item:
                result["records"].append(item)
            else:
                errors.append(f"No published observation returned for {code}.")
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{code}: {str(exc)[:180]}")
    if result["records"]:
        STATS_CACHE.update({"fetched_at": now, "payload": result, "error": None})
        return result, None
    if STATS_CACHE["payload"]:
        return STATS_CACHE["payload"], "Live source unavailable; showing previously cached official data. " + "; ".join(errors)
    return result, "Official statistics could not be fetched. Check internet connectivity and try again. " + "; ".join(errors)


@app.get("/")
def home():
    return jsonify({"service": "Women Safety Wearable API", "status": "running", "health": "/api/health"})


@app.get("/api/health")
def health():
    try:
        with connect_db() as conn:
            conn.execute("SELECT 1").fetchone()
        return jsonify({"status": "ok", "database": "connected", "time": utc_now()})
    except sqlite3.Error as exc:
        return jsonify({"status": "error", "database": str(exc), "time": utc_now()}), 500


@app.get("/api/dashboard")
def dashboard():
    with connect_db() as conn:
        alerts = conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 20").fetchall()
        devices = conn.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
        total = conn.execute("SELECT COUNT(*) AS count FROM alerts").fetchone()["count"]
        active_today = conn.execute("SELECT COUNT(*) AS count FROM alerts WHERE created_at >= ?", (datetime.now(timezone.utc).date().isoformat(),)).fetchone()["count"]
    now = datetime.now(timezone.utc)
    device_list = []
    for row in devices:
        item = row_to_dict(row)
        try:
            last_seen = datetime.fromisoformat(item["last_seen"].replace("Z", "+00:00"))
            item["online"] = (now - last_seen).total_seconds() <= 120
        except ValueError:
            item["online"] = False
        device_list.append(item)
    return jsonify({"generated_at": utc_now(), "summary": {"total_alerts": total, "alerts_today": active_today, "known_devices": len(device_list), "online_devices": sum(1 for d in device_list if d["online"])}, "alerts": [row_to_dict(row) for row in alerts], "devices": device_list})


@app.get("/api/vawg-stats")
def vawg_stats():
    force = request.args.get("refresh", "").lower() == "true"
    payload, warning = get_vawg_stats(force=force)
    payload["warning"] = warning
    status = 200 if payload.get("records") else 503
    return jsonify(payload), status


def require_device_key():
    supplied = request.headers.get("X-API-Key", "")
    return bool(API_KEY) and supplied == API_KEY


def parse_alert_payload(data):
    device_id = str(data.get("device_id", "")).strip()[:80]
    if not device_id:
        return None, "device_id is required"
    event_type = str(data.get("event_type", "SOS")).strip().upper()[:30]
    if event_type not in {"SOS", "FALL", "TAMPER", "HEARTBEAT"}:
        return None, "event_type must be SOS, FALL, TAMPER, or HEARTBEAT"
    zone = str(data.get("zone", "Unknown zone")).strip()[:120] or "Unknown zone"
    battery = data.get("battery_percent")
    if battery is not None:
        try:
            battery = int(battery)
            if not 0 <= battery <= 100:
                return None, "battery_percent must be between 0 and 100"
        except (TypeError, ValueError):
            return None, "battery_percent must be an integer from 0 to 100"
    lat = data.get("latitude")
    lon = data.get("longitude")
    try:
        lat = float(lat) if lat is not None else None
        lon = float(lon) if lon is not None else None
        if lat is not None and not -90 <= lat <= 90: raise ValueError()
        if lon is not None and not -180 <= lon <= 180: raise ValueError()
    except (TypeError, ValueError):
        return None, "latitude/longitude must be valid coordinates"
    return {"device_id": device_id, "event_type": event_type, "zone": zone, "battery_percent": battery, "latitude": lat, "longitude": lon, "message": str(data.get("message", "Emergency alert received"))[:300]}, None


@app.post("/api/alerts")
def create_alert():
    if not require_device_key():
        return jsonify({"error": "Unauthorized. Supply the correct X-API-Key header."}), 401
    data = request.get_json(silent=True) or {}
    clean, error = parse_alert_payload(data)
    if error:
        return jsonify({"error": error}), 400
    with connect_db() as conn:
        record_device(conn, clean["device_id"], clean["battery_percent"], clean["zone"])
        if clean["event_type"] == "HEARTBEAT":
            conn.commit()
            return jsonify({"ok": True, "kind": "heartbeat", "device_id": clean["device_id"], "received_at": utc_now()}), 200
        cursor = conn.execute("""INSERT INTO alerts(device_id,event_type,message,zone,latitude,longitude,source,created_at)
            VALUES (?,?,?,?,?,?,?,?)""", (clean["device_id"], clean["event_type"], clean["message"], clean["zone"], clean["latitude"], clean["longitude"], "wearable", utc_now()))
        conn.commit()
        alert_id = cursor.lastrowid
    return jsonify({"ok": True, "alert_id": alert_id, "source": "wearable", "received_at": utc_now()}), 201


@app.post("/api/demo-alerts")
def demo_alert():
    """Explicitly labelled demo endpoint; never use it for real emergency calls."""
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "DEMO-WB-01"))[:80]
    zone = str(data.get("zone", "Demo zone"))[:120]
    now = utc_now()
    with connect_db() as conn:
        record_device(conn, device_id, 88, zone)
        cursor = conn.execute("INSERT INTO alerts(device_id,event_type,message,zone,source,created_at) VALUES (?,?,?,?,?,?)", (device_id, "SOS", "SIMULATED TEST ALERT — not a real emergency", zone, "demo", now))
        conn.commit()
    return jsonify({"ok": True, "alert_id": cursor.lastrowid, "source": "demo", "warning": "Simulated test alert only; no emergency services were contacted."}), 201


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Route not found"}), 404


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=True)

from flask import Flask, request, jsonify, g
import urllib.request, json, logging, os, re
from datetime import datetime
from functools import wraps
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_KEY    = os.environ.get("ADMIN_KEY", "neev2024admin")

# ── DATABASE ──────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g or g.db.closed:
        g.db = psycopg2.connect(DATABASE_URL)
        g.db.autocommit = False
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db and not db.closed:
        db.close()

def query(sql, args=()):
    cur = get_db().cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, args)
    return cur.fetchall()

def query_one(sql, args=()):
    cur = get_db().cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, args)
    return cur.fetchone()

def mutate(sql, args=()):
    db  = get_db()
    cur = db.cursor()
    cur.execute(sql, args)
    db.commit()
    return cur

def init_db():
    """Create table and seed if empty — runs on every startup."""
    db  = psycopg2.connect(DATABASE_URL)
    cur = db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS distributors (
            id         SERIAL PRIMARY KEY,
            name       TEXT NOT NULL,
            pincode    TEXT NOT NULL,
            mobile     TEXT NOT NULL,
            address    TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    db.commit()
    # Add updated_at to existing tables that pre-date this column
    cur.execute("""
        ALTER TABLE distributors
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()
    """)
    db.commit()
    cur.execute("SELECT COUNT(*) FROM distributors")
    count = cur.fetchone()[0]
    if count == 0:
        logging.info("Empty DB — seeding %d distributors...", len(SEED_DATA))
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO distributors (name, pincode, mobile, address) VALUES %s",
            [(d["name"], d["pincode"], d["mobile"], d["address"]) for d in SEED_DATA]
        )
        db.commit()
        logging.info("Seed complete.")
    else:
        logging.info("DB has %d distributors — skipping seed.", count)
    db.close()

# ── AUTH ──────────────────────────────────────────────────────────────────────
def require_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-Admin-Key") or request.args.get("key")
        if key != ADMIN_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ── WEATHER ───────────────────────────────────────────────────────────────────
WX_CODE = {
    0:"☀️ साफ आसमान",1:"🌤️ ज्यादातर साफ",2:"⛅ आंशिक बादल",3:"☁️ बादल छाए",
    45:"🌫️ कोहरा",48:"🌫️ कोहरा",
    51:"🌦️ हल्की बूंदाबांदी",53:"🌦️ बूंदाबांदी",55:"🌧️ तेज बूंदाबांदी",
    61:"🌧️ हल्की बारिश",63:"🌧️ बारिश",65:"🌧️ तेज बारिश",
    80:"🌦️ बौछारें",81:"🌧️ तेज बौछारें",82:"⛈️ भारी बौछारें",
    95:"⛈️ आंधी-तूफान",99:"⛈️ ओलावृष्टि"
}
DAYS_HI = ["सोमवार","मंगलवार","बुधवार","गुरुवार","शुक्रवार","शनिवार","रविवार"]

def wx_label(code):
    for c in sorted(WX_CODE.keys(), reverse=True):
        if code >= c: return WX_CODE[c]
    return "🌡️ मौसम"

def pincode_to_latlon(pincode):
    url = ("https://nominatim.openstreetmap.org/search"
           "?postalcode="+pincode+"&country=India&format=json&limit=1")
    req = urllib.request.Request(url, headers={"User-Agent":"NeevSeeds-WeatherBot/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    if not data: return None, None, None
    return float(data[0]["lat"]), float(data[0]["lon"]), data[0].get("display_name","")

def get_forecast(lat, lon):
    url = ("https://api.open-meteo.com/v1/forecast"
           "?latitude="+str(lat)+"&longitude="+str(lon)+
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
           "precipitation_sum,precipitation_probability_max,wind_speed_10m_max"
           "&timezone=Asia/Kolkata&forecast_days=7")
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())["daily"]

def format_weather_msg(pincode, daily, location_name):
    parts = [p.strip() for p in location_name.split(",")]
    city  = parts[1] if len(parts) > 1 else parts[0]
    lines = ["*"+city+" - 7 दिन का मौसम* (पिन: "+pincode+")\n"]
    for i in range(7):
        d        = datetime.strptime(daily["time"][i], "%Y-%m-%d")
        rain_mm  = daily["precipitation_sum"][i] or 0
        rain_pct = daily["precipitation_probability_max"][i] or 0
        wind     = daily["wind_speed_10m_max"][i]
        line     = ("*"+DAYS_HI[d.weekday()]+" ("+d.strftime("%d %b")+")*\n"
                    +wx_label(daily["weather_code"][i])+"\n"
                    +"🌡 "+str(daily["temperature_2m_min"][i])
                    +"°C – "+str(daily["temperature_2m_max"][i])+"°C")
        if rain_mm > 0:     line += "\n🌧 बारिश: "+str(rain_mm)+"mm ("+str(rain_pct)+"%)"
        elif rain_pct > 20: line += "\n🌧 बारिश संभव: "+str(rain_pct)+"%"
        line += "\n💨 हवा: "+str(wind)+" km/h"
        lines.append(line)
        if i < 6: lines.append("—")
    lines.append("\n_स्रोत: Open-Meteo_")
    return "\n".join(lines)

# ── DISTRIBUTOR LOOKUP ────────────────────────────────────────────────────────
def find_nearest(pincode_str):
    pin_int = int(pincode_str)
    # Tier 1: exact
    rows = query("SELECT * FROM distributors WHERE pincode = %s", (pincode_str,))
    if rows: return rows[:5], "exact"

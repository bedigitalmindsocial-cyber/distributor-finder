from flask import Flask,request,jsonify,Response
import urllib.request,json,logging,os,re
from datetime import datetime
from functools import wraps
import pg8000.dbapi
logging.basicConfig(level=logging.INFO)
app=Flask(__name__)
DATABASE_URL=os.environ.get("DATABASE_URL","")
ADMIN_KEY=os.environ.get("ADMIN_KEY","neev2024admin")

@app.after_request
def add_cors(r):
    r.headers["Access-Control-Allow-Origin"]="*"
    r.headers["Access-Control-Allow-Methods"]="GET,POST,OPTIONS"
    r.headers["Access-Control-Allow-Headers"]="Content-Type,X-Admin-Key"
    # Distributor data changes whenever the sheet is pushed, so a cached
    # reply can show a farmer stale results. Never cache API responses.
    r.headers["Cache-Control"]="no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"]="no-cache"
    r.headers["Expires"]="0"
    return r

@app.route("/find",methods=["OPTIONS"])
@app.route("/weather",methods=["OPTIONS"])
@app.route("/health",methods=["OPTIONS"])
@app.route("/admin/distributors",methods=["OPTIONS"])
@app.route("/wa",methods=["OPTIONS"])
def preflight(**k): return "",204

def parse_url(u):
    m=re.match(r"postgresql://([^:]+):([^@]+)@([^:/]+):(\d+)/(.+)",u)
    if not m: raise ValueError("Bad DATABASE_URL")
    return dict(user=m[1],password=m[2],host=m[3],port=int(m[4]),database=m[5])

def conn():
    p=parse_url(DATABASE_URL)
    c=pg8000.dbapi.connect(user=p["user"],password=p["password"],host=p["host"],port=p["port"],database=p["database"],ssl_context=True)
    c.autocommit=True; return c

def qry(sql,args=()):
    c=conn()
    try:
        cur=c.cursor(); cur.execute(sql,args)
        cols=[d[0] for d in cur.description]
        return [dict(zip(cols,r)) for r in cur.fetchall()]
    finally: c.close()

def one(sql,args=()): r=qry(sql,args); return r[0] if r else None

def run(sql,args=()):
    c=conn()
    try:
        cur=c.cursor(); cur.execute(sql,args)
        return cur.fetchone() if cur.description else None
    finally: c.close()

def init():
    c=conn()
    try:
        cur=c.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS distributors(id SERIAL PRIMARY KEY,name TEXT NOT NULL,pincode TEXT NOT NULL,mobile TEXT NOT NULL,address TEXT NOT NULL,updated_at TIMESTAMPTZ DEFAULT NOW())""")
        # ---- Sheet-sourced table: ONE ROW PER DISTRIBUTOR ----
        # Stores every column from the Google Sheet. Render reads only
        # pincode + details_hi; the rest is archived for future use.
        #
        # Uses a MIGRATION, not a plain CREATE. An earlier deploy may have
        # left an older version of this table behind, and CREATE TABLE IF
        # NOT EXISTS would silently skip it, leaving the new columns absent.
        cur.execute("""CREATE TABLE IF NOT EXISTS sheet_distributors(
            id         SERIAL PRIMARY KEY,
            pincode    TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW())""")

        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_schema='public'
                         AND table_name='sheet_distributors'""")
        have = set(r[0] for r in cur.fetchall())

        # Legacy: single "details" column becomes details_hi
        if "details" in have and "details_hi" not in have:
            cur.execute("ALTER TABLE sheet_distributors RENAME COLUMN details TO details_hi")
            have.discard("details"); have.add("details_hi")
            logging.info("migrated: details -> details_hi")

        for colname in ("s_no", "address", "name", "details_en", "details_hi", "details_pa"):
            if colname not in have:
                cur.execute("ALTER TABLE sheet_distributors ADD COLUMN " + colname + " TEXT")
                logging.info("migrated: added column %s", colname)

        # Old NOT NULL on details_hi blocks rows that legitimately lack it
        cur.execute("ALTER TABLE sheet_distributors ALTER COLUMN details_hi DROP NOT NULL")

        cur.execute("""CREATE INDEX IF NOT EXISTS idx_sheet_pin
                       ON sheet_distributors(pincode)""")

        # Staging table. Rows land here first; the live table is only
        # swapped once every batch has arrived. A failed push therefore
        # leaves the live data untouched instead of half-deleted.
        cur.execute("""CREATE TABLE IF NOT EXISTS sheet_staging(
            id         SERIAL PRIMARY KEY,
            s_no       TEXT,
            address    TEXT,
            name       TEXT,
            pincode    TEXT NOT NULL,
            details_en TEXT,
            details_hi TEXT,
            details_pa TEXT)""")

        cur.execute("DROP TABLE IF EXISTS pincode_details")
        cur.execute("SELECT COUNT(*) FROM distributors")
        n=cur.fetchone()[0]
        if n==0:
            logging.info("Seeding %d",len(SEED))
            for d in SEED:
                cur.execute("INSERT INTO distributors(name,pincode,mobile,address) VALUES(%s,%s,%s,%s)",(d["name"],d["pincode"],d["mobile"],d["address"]))
        else: logging.info("DB has %d records",n)
    finally: c.close()


def auth(f):
    @wraps(f)
    def dec(*a,**k):
        key=request.headers.get("X-Admin-Key") or request.args.get("key","")
        if key!=ADMIN_KEY: return jsonify({"error":"Unauthorized"}),401
        return f(*a,**k)
    return dec



# ---- TEXT SANITISER ----
# Google Sheets routinely carries invisible characters that survive a
# copy-paste: non-breaking spaces, zero-width joiners, BOM marks. They
# are invisible in the sheet and in Supabase, but they break exact
# matching and can render as boxes on some Android WhatsApp builds.
_INVISIBLE = {
    "\u00a0": " ",   # non-breaking space
    "\u200b": "",    # zero-width space
    "\u200c": "",    # zero-width non-joiner
    "\u200d": "",    # zero-width joiner
    "\u200e": "",    # left-to-right mark
    "\u200f": "",    # right-to-left mark
    "\ufeff": "",    # byte-order mark
    "\u2028": "\n",  # line separator
    "\u2029": "\n",  # paragraph separator
}

def clean_text(s):
    if s is None:
        return ""
    s = str(s)
    for bad, good in _INVISIBLE.items():
        s = s.replace(bad, good)
    s = re.sub(r"[ \t]+", " ", s)        # collapse runs of spaces
    s = re.sub(r"\n{3,}", "\n\n", s)     # collapse blank-line runs
    return s.strip()


def pins_in_text(s):
    """Every standalone 6-digit number inside a details string."""
    return re.findall(r"(?<!\d)\d{6}(?!\d)", str(s or ""))


# ---- SHEET-FIRST LOOKUP ----
# Google Sheet is the master source. sheet_distributors holds one row
# per distributor with the exact Hindi text the team curated.
# Same 6->5->4->3 cascade, filling up to 5 results.
def sheet_nearest(pin):
    """Return up to 5 distributor detail strings, nearest first."""
    pi = int(pin)
    seen = set()
    out  = []

    def add(rows):
        rows = sorted(rows, key=lambda r: abs(int(r["pincode"]) - pi))
        for r in rows:
            key = (r["details_hi"] or "").strip()
            if key not in seen and len(out) < 5:
                seen.add(key)
                out.append(r)

    add(qry("SELECT pincode,details_hi FROM sheet_distributors WHERE pincode=%s", (pin,)))
    if len(out) < 5:
        add(qry("SELECT pincode,details_hi FROM sheet_distributors WHERE pincode LIKE %s", (pin[:5]+"%",)))
    if len(out) < 5:
        add(qry("SELECT pincode,details_hi FROM sheet_distributors WHERE pincode LIKE %s", (pin[:4]+"%",)))
    if len(out) < 5:
        add(qry("SELECT pincode,details_hi FROM sheet_distributors WHERE pincode LIKE %s", (pin[:3]+"%",)))

    if not out:
        return [], "none"
    last = out[-1]["pincode"]
    lvl  = "exact" if last == pin else ("nearby" if last[:4] == pin[:4] else "district")
    return out, lvl


def fmt_sheet(rows, lvl):
    """Number the sheet's Hindi text blocks into one WhatsApp message."""
    L = {"exact": "आपके क्षेत्र के", "nearby": "आपके नजदीक के", "district": "आपके जिले के"}
    parts = ["✅ " + L.get(lvl, "नजदीकी") + " *Neev Seeds* के " + str(len(rows)) + " डिस्ट्रीब्यूटर:"]
    for i, r in enumerate(rows, 1):
        parts.append("*" + str(i) + ".* " + (r["details_hi"] or "").strip())
    parts.append("किसी भी जानकारी के लिए डिस्ट्रीब्यूटर से सीधे संपर्क करें। 🙏")
    return "\n\n".join(parts)


# ---- CASCADING MATCH: 6 -> 5 -> 4 -> 3 digits, fill up to 5 ----
def nearest(pin):
    pi=int(pin); seen=set(); out=[]
    def add(rows):
        rows=sorted(rows,key=lambda r:abs(int(r["pincode"])-pi))
        for r in rows:
            if r["name"] not in seen and len(out)<5:
                seen.add(r["name"]); out.append(r)
    add(qry("SELECT * FROM distributors WHERE pincode=%s",(pin,)))
    if len(out)<5: add(qry("SELECT * FROM distributors WHERE pincode LIKE %s",(pin[:5]+"%",)))
    if len(out)<5: add(qry("SELECT * FROM distributors WHERE pincode LIKE %s",(pin[:4]+"%",)))
    if len(out)<5: add(qry("SELECT * FROM distributors WHERE pincode LIKE %s",(pin[:3]+"%",)))
    if not out: return [],"none"
    last=out[-1]["pincode"]
    if last==pin: lvl="exact"
    elif last[:4]==pin[:4]: lvl="nearby"
    else: lvl="district"
    return out[:5],lvl

def fmt(m,lvl):
    L={"exact":"आपके क्षेत्र के","nearby":"आपके नजदीक के","district":"आपके जिले के"}
    ls=["✅ "+L.get(lvl,"नजदीकी")+" *Neev Seeds* के "+str(len(m))+" डिस्ट्रीब्यूटर:\n"]
    for i,d in enumerate(m,1): ls+=["*"+str(i)+". "+d["name"]+"*","📍 "+d["address"],"📞 "+d["mobile"],""]
    ls.append("किसी भी जानकारी के लिए डिस्ट्रीब्यूटर से सीधे संपर्क करें। 🙏")
    return "\n".join(ls)

def getpin():
    p=(request.get_json(silent=True) or {}).get("pincode","") if request.method=="POST" else request.args.get("pincode","")
    p=re.sub(r"\D","",str(p))
    return p if len(p)==6 else None

@app.route("/find",methods=["GET","POST"])
def find():
    pin=getpin()
    if not pin: return jsonify({"count":0,"distributors":[],"message":"कृपया सही 6 अंकों का पिनकोड भेजें।"}),400
    try:
        # 1) SHEET FIRST - it is the master source.
        #    Checking the old distributors table first would bail out on
        #    any pincode that exists only in the sheet.
        srows, slvl = sheet_nearest(pin)

        # 2) Structured table, used for the website cards and as fallback
        m, lvl = nearest(pin)

        if not srows and not m:
            return jsonify({
                "count": 0, "match_level": "none", "source": "none",
                "distributors": [],
                "message": "पिनकोड " + pin + " के पास कोई Neev Seeds डिस्ट्रीब्यूटर नहीं मिला।\nकृपया Neev Seeds से सीधे संपर्क करें। 🙏"
            })

        return jsonify({
            "count": len(srows) if srows else len(m),
            "match_level": slvl if srows else lvl,
            "source": "sheet" if srows else "database",
            "distributors": [{"Distributor Name":d["name"],"Address":d["address"],"Mobile Number":d["mobile"]} for d in m],
            "message": fmt_sheet(srows, slvl) if srows else fmt(m, lvl)
        })
    except Exception as e:
        logging.error("find: %s",e)
        return jsonify({"count":0,"distributors":[],"message":"सेवा अस्थायी रूप से उपलब्ध नहीं।"}),500

WX={0:"☀️",1:"🌤",2:"⛅",3:"☁️",45:"🌫",61:"🌧",63:"🌧",65:"🌧",80:"🌦",95:"⛈"}
DAYS=["सोमवार","मंगलवार","बुधवार","गुरुवार","शुक्रवार","शनिवार","रविवार"]
_LL={}
def wxl(c):
    for k in sorted(WX.keys(),reverse=True):
        if c>=k: return WX[k]
    return "🌡"

@app.route("/weather",methods=["GET","POST"])
def weather():
    pin=getpin()
    if not pin: return jsonify({"message":"कृपया सही 6 अंकों का पिनकोड भेजें।"}),400
    try:
        if pin not in _LL:
            u="https://nominatim.openstreetmap.org/search?postalcode="+pin+"&country=India&format=json&limit=1"
            rq=urllib.request.Request(u,headers={"User-Agent":"NeevSeeds/1.0"})
            with urllib.request.urlopen(rq,timeout=10) as r: d=json.loads(r.read())
            if not d: return jsonify({"message":"लोकेशन नहीं मिली।"})
            _LL[pin]=(float(d[0]["lat"]),float(d[0]["lon"]),d[0].get("display_name",""))
        lat,lon,loc=_LL[pin]
        u2=("https://api.open-meteo.com/v1/forecast?latitude="+str(lat)+"&longitude="+str(lon)+"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max&timezone=Asia/Kolkata&forecast_days=7")
        with urllib.request.urlopen(u2,timeout=10) as r: da=json.loads(r.read())["daily"]
        city=[p.strip() for p in loc.split(",")]; city=city[1] if len(city)>1 else city[0]
        ls=["*"+city+" - 7 दिन का मौसम* (पिन: "+pin+")\n"]
        for i in range(7):
            dt=datetime.strptime(da["time"][i],"%Y-%m-%d")
            rm=da["precipitation_sum"][i] or 0; rp=da["precipitation_probability_max"][i] or 0
            ln=("*"+DAYS[dt.weekday()]+" ("+dt.strftime("%d %b")+")*\n"+wxl(da["weather_code"][i])+"\n🌡 "+str(da["temperature_2m_min"][i])+"°C – "+str(da["temperature_2m_max"][i])+"°C")
            if rm>0: ln+="\n🌧 "+str(rm)+"mm ("+str(rp)+"%)"
            elif rp>20: ln+="\n🌧 संभव: "+str(rp)+"%"
            ln+="\n💨 "+str(da["wind_speed_10m_max"][i])+" km/h"
            ls.append(ln)
            if i<6: ls.append("—")
        ls.append("\n_Open-Meteo_")
        return jsonify({"pincode":pin,"location":loc,"message":"\n".join(ls)})
    except Exception as e:
        logging.error("weather: %s",e); return jsonify({"message":"मौसम जानकारी अभी उपलब्ध नहीं।"}),500

@app.route("/admin/ping")
@auth
def ping(): return jsonify({"ok":True})

@app.route("/admin/distributors",methods=["GET"])
@auth
def list_d():
    s=request.args.get("q","").strip()
    rows=(qry("SELECT * FROM distributors WHERE name ILIKE %s OR pincode ILIKE %s OR address ILIKE %s ORDER BY name",("%"+s+"%","%"+s+"%","%"+s+"%")) if s else qry("SELECT * FROM distributors ORDER BY name"))
    for r in rows:
        if r.get("updated_at"): r["updated_at"]=str(r["updated_at"])
    return jsonify(rows)

@app.route("/admin/distributors",methods=["POST"])
@auth
def add_d():
    d=request.get_json(silent=True) or {}
    n=d.get("name","").strip(); p=re.sub(r"\D","",str(d.get("pincode",""))); m=d.get("mobile","").strip(); a=d.get("address","").strip()
    if not all([n,p,m,a]): return jsonify({"error":"All fields required"}),400
    if len(p)!=6: return jsonify({"error":"Pincode must be 6 digits"}),400
    r=run("INSERT INTO distributors(name,pincode,mobile,address) VALUES(%s,%s,%s,%s) RETURNING id",(n,p,m,a))
    return jsonify({"id":r[0],"message":"Added"}),201

@app.route("/admin/distributors/<int:rid>",methods=["GET"])
@auth
def get_d(rid):
    r=one("SELECT * FROM distributors WHERE id=%s",(rid,))
    if not r: return jsonify({"error":"Not found"}),404
    if r.get("updated_at"): r["updated_at"]=str(r["updated_at"])
    return jsonify(r)

@app.route("/admin/distributors/<int:rid>",methods=["PUT"])
@auth
def upd_d(rid):
    row=one("SELECT * FROM distributors WHERE id=%s",(rid,))
    if not row: return jsonify({"error":"Not found"}),404
    d=request.get_json(silent=True) or {}
    n=d.get("name",row["name"]).strip(); p=re.sub(r"\D","",str(d.get("pincode",row["pincode"]))); m=d.get("mobile",row["mobile"]).strip(); a=d.get("address",row["address"]).strip()
    if len(p)!=6: return jsonify({"error":"Pincode must be 6 digits"}),400
    run("UPDATE distributors SET name=%s,pincode=%s,mobile=%s,address=%s,updated_at=NOW() WHERE id=%s",(n,p,m,a,rid))
    return jsonify({"message":"Updated"})

@app.route("/admin/distributors/<int:rid>",methods=["DELETE"])
@auth
def del_d(rid):
    if not one("SELECT id FROM distributors WHERE id=%s",(rid,)): return jsonify({"error":"Not found"}),404
    run("DELETE FROM distributors WHERE id=%s",(rid,))
    return jsonify({"message":"Deleted"})


# ---- SHEET SYNC ENDPOINTS ----
SHEET_FIELDS = ["s_no","address","name","pincode","details_en","details_hi","details_pa"]

@app.route("/admin/sheet", methods=["GET"])
@auth
def sheet_list():
    total = one("SELECT COUNT(*) as c FROM sheet_distributors")["c"]
    uniq  = one("SELECT COUNT(DISTINCT pincode) as c FROM sheet_distributors")["c"]
    return jsonify({"total_rows": total, "unique_pincodes": uniq})

@app.route("/admin/sheet/schema", methods=["GET"])
@auth
def sheet_schema():
    cols = qry("""SELECT column_name, data_type
                  FROM information_schema.columns
                  WHERE table_schema='public' AND table_name='sheet_distributors'
                  ORDER BY ordinal_position""")
    names   = [c["column_name"] for c in cols]
    missing = [f for f in SHEET_FIELDS if f not in names]
    staging = one("""SELECT COUNT(*) as c FROM information_schema.tables
                     WHERE table_schema='public' AND table_name='sheet_staging'""")["c"]
    return jsonify({"columns": cols, "missing": missing,
                    "staging_exists": staging > 0,
                    "ready": (not missing) and staging > 0})

# ---- ATOMIC PUSH: begin -> load batches -> commit ----
@app.route("/admin/sheet/begin", methods=["POST"])
@auth
def sheet_begin():
    """Empty the staging table. Live data is NOT touched."""
    run("DELETE FROM sheet_staging")
    live = one("SELECT COUNT(*) as c FROM sheet_distributors")["c"]
    return jsonify({"message": "staging cleared", "live_rows_untouched": live})

@app.route("/admin/sheet", methods=["POST"])
@auth
def sheet_load():
    """Load one batch into STAGING. Nothing goes live until /commit."""
    body = request.get_json(silent=True) or {}
    rows = body.get("rows", [])
    if not isinstance(rows, list):
        return jsonify({"error": "rows must be a list"}), 400

    clean, skipped = [], []
    for i, r in enumerate(rows):
        pin = re.sub(r"\D", "", str(r.get("pincode", "")))
        hi  = clean_text(r.get("details_hi"))
        if len(pin) != 6:
            skipped.append({"index": i, "reason": "pincode not 6 digits"}); continue
        if not hi:
            skipped.append({"index": i, "reason": "Hindi details empty"}); continue
        clean.append((
            clean_text(r.get("s_no"))       or None,
            clean_text(r.get("address"))    or None,
            clean_text(r.get("name"))       or None,
            pin,
            clean_text(r.get("details_en")) or None,
            hi,
            clean_text(r.get("details_pa")) or None,
        ))

    if not clean:
        return jsonify({"loaded": 0, "skipped": len(skipped),
                        "skipped_detail": skipped[:20], "staged_total": 0})

    c = conn()
    try:
        cur = c.cursor()
        ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s)"] * len(clean))
        params = []
        for row in clean:
            params.extend(row)
        cur.execute("INSERT INTO sheet_staging(" + ",".join(SHEET_FIELDS) + ") VALUES " + ph, params)
        cur.execute("SELECT COUNT(*) FROM sheet_staging")
        staged = cur.fetchone()[0]
    except Exception as e:
        logging.error("sheet_load: %s", e)
        try: c.close()
        except Exception: pass
        return jsonify({"error": str(e)[:200]}), 500
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({"loaded": len(clean), "skipped": len(skipped),
                    "skipped_detail": skipped[:20], "staged_total": staged})

@app.route("/admin/sheet/commit", methods=["POST"])
@auth
def sheet_commit():
    """Swap staging into live inside ONE transaction.
       Refuses to commit an empty staging table - that would wipe
       everything if an earlier batch silently failed."""
    body     = request.get_json(silent=True) or {}
    expected = body.get("expected_rows")

    staged = one("SELECT COUNT(*) as c FROM sheet_staging")["c"]
    live   = one("SELECT COUNT(*) as c FROM sheet_distributors")["c"]

    if staged == 0:
        return jsonify({"error": "staging is empty - refusing to wipe live data",
                        "staged": 0, "live_rows": live}), 400

    if expected is not None and int(expected) != staged:
        return jsonify({"error": "row count mismatch - some batches did not arrive",
                        "expected": int(expected), "staged": staged,
                        "live_rows": live, "committed": False}), 409

    c = conn()
    try:
        c.autocommit = False
        cur = c.cursor()
        cur.execute("DELETE FROM sheet_distributors")
        cur.execute("""INSERT INTO sheet_distributors(""" + ",".join(SHEET_FIELDS) + """,updated_at)
                       SELECT """ + ",".join(SHEET_FIELDS) + """, NOW() FROM sheet_staging""")
        cur.execute("SELECT COUNT(*) FROM sheet_distributors")
        final = cur.fetchone()[0]
        c.commit()
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logging.error("sheet_commit: %s", e)
        try: c.close()
        except Exception: pass
        return jsonify({"error": str(e)[:200], "committed": False,
                        "live_rows": live}), 500
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({"committed": True, "live_rows": final,
                    "previous_rows": live, "staged": staged})

# ---- DATA QUALITY AUDIT ----
@app.route("/admin/sheet/audit", methods=["GET"])
@auth
def sheet_audit():
    """Inspect LIVE data for problems the pipeline cannot see by itself."""
    rows = qry("SELECT id,s_no,name,pincode,details_hi FROM sheet_distributors ORDER BY pincode")

    pin_mismatch, no_pin_in_text, no_mobile, very_long, dupes = [], [], [], [], []
    seen = {}

    for r in rows:
        hi   = r["details_hi"] or ""
        pins = pins_in_text(hi)

        if not pins:
            no_pin_in_text.append({"id": r["id"], "pincode": r["pincode"],
                                   "name": (r["name"] or hi[:45])})
        elif r["pincode"] not in pins:
            pin_mismatch.append({"id": r["id"], "column_pincode": r["pincode"],
                                 "text_pincode": pins[0],
                                 "name": (r["name"] or hi[:45])})

        if not re.search(r"(?<!\d)[6-9]\d{9}(?!\d)", re.sub(r"[\s-]", "", hi)):
            no_mobile.append({"id": r["id"], "pincode": r["pincode"],
                              "name": (r["name"] or hi[:45])})

        if len(hi) > 400:
            very_long.append({"id": r["id"], "pincode": r["pincode"], "chars": len(hi)})

        key = (r["pincode"], hi.strip())
        if key in seen:
            dupes.append({"id": r["id"], "duplicate_of": seen[key], "pincode": r["pincode"]})
        else:
            seen[key] = r["id"]

    # Worst-case WhatsApp message size per pincode
    over_limit = []
    bypin = {}
    for r in rows:
        bypin.setdefault(r["pincode"], []).append(len(r["details_hi"] or ""))
    for pin, lens in bypin.items():
        top5 = sorted(lens, reverse=True)[:5]
        approx = sum(top5) + 60 * len(top5) + 120
        if approx > 4096:
            over_limit.append({"pincode": pin, "approx_chars": approx})

    issues = (len(pin_mismatch) + len(no_pin_in_text) + len(no_mobile)
              + len(dupes) + len(over_limit))

    return jsonify({
        "total_rows": len(rows),
        "unique_pincodes": len(bypin),
        "issue_count": issues,
        "clean": issues == 0,
        "pincode_mismatch": {"count": len(pin_mismatch), "rows": pin_mismatch[:25]},
        "no_pincode_in_text": {"count": len(no_pin_in_text), "rows": no_pin_in_text[:25]},
        "no_mobile_found":    {"count": len(no_mobile),    "rows": no_mobile[:25]},
        "duplicate_rows":     {"count": len(dupes),        "rows": dupes[:25]},
        "very_long_text":     {"count": len(very_long),    "rows": very_long[:25]},
        "whatsapp_over_4096": {"count": len(over_limit),   "rows": over_limit[:25]},
    })

@app.route("/admin/sheet/reset", methods=["POST"])
@auth
def sheet_reset():
    c = conn()
    try:
        cur = c.cursor()
        for tbl in ("sheet_distributors", "sheet_staging"):
            cur.execute("DROP TABLE IF EXISTS " + tbl)
        cur.execute("""CREATE TABLE sheet_distributors(
            id SERIAL PRIMARY KEY, s_no TEXT, address TEXT, name TEXT,
            pincode TEXT NOT NULL, details_en TEXT, details_hi TEXT,
            details_pa TEXT, updated_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("CREATE INDEX idx_sheet_pin ON sheet_distributors(pincode)")
        cur.execute("""CREATE TABLE sheet_staging(
            id SERIAL PRIMARY KEY, s_no TEXT, address TEXT, name TEXT,
            pincode TEXT NOT NULL, details_en TEXT, details_hi TEXT,
            details_pa TEXT)""")
    except Exception as e:
        logging.error("sheet_reset: %s", e)
        try: c.close()
        except Exception: pass
        return jsonify({"error": str(e)[:200]}), 500
    finally:
        try: c.close()
        except Exception: pass
    return jsonify({"message": "tables rebuilt", "total_rows": 0})

@app.route("/admin")
def admin_ui():
    p=os.path.join(os.path.dirname(__file__),"admin.html")
    with open(p) as f: return f.read(),200,{"Content-Type":"text/html;charset=utf-8"}

@app.route("/init")
def manual_init():
    try: init(); c=one("SELECT COUNT(*) as c FROM distributors")["c"]; return jsonify({"status":"ok","distributors":c})
    except Exception as e: return jsonify({"status":"error","message":str(e)}),500



# ---- PLAIN TEXT ENDPOINT FOR WATI ----
# Returns the message as raw text (not JSON) so WATI stores the whole
# reply directly in the response variable. Use {variableName} in the
# Send Message node - no nested .message access needed.
@app.route("/wa", methods=["GET","POST"])
def wa_plain():
    pin = getpin()
    if not pin:
        return Response("कृपया सही 6 अंकों का पिनकोड भेजें।\nजैसे: 152116",
                        mimetype="text/plain; charset=utf-8")
    try:
        # 1) Sheet is master
        srows, slvl = sheet_nearest(pin)
        if srows:
            return Response(fmt_sheet(srows, slvl), mimetype="text/plain; charset=utf-8")
        # 2) Fallback to structured table
        m, lvl = nearest(pin)
        if not m:
            return Response("पिनकोड " + pin + " के पास कोई Neev Seeds डिस्ट्रीब्यूटर नहीं मिला।\nकृपया Neev Seeds से सीधे संपर्क करें। 🙏",
                            mimetype="text/plain; charset=utf-8")
        return Response(fmt(m, lvl), mimetype="text/plain; charset=utf-8")
    except Exception as e:
        logging.error("wa: %s", e)
        return Response("⚠️ जानकारी अभी उपलब्ध नहीं है। कृपया थोड़ी देर बाद प्रयास करें।",
                        mimetype="text/plain; charset=utf-8")

@app.route("/wa/weather", methods=["GET","POST"])
def wa_weather_plain():
    pin = getpin()
    if not pin:
        return Response("कृपया सही 6 अंकों का पिनकोड भेजें।",
                        mimetype="text/plain; charset=utf-8")
    try:
        if pin not in _LL:
            u = "https://nominatim.openstreetmap.org/search?postalcode=" + pin + "&country=India&format=json&limit=1"
            rq = urllib.request.Request(u, headers={"User-Agent":"NeevSeeds/1.0"})
            with urllib.request.urlopen(rq, timeout=10) as r:
                d = json.loads(r.read())
            if not d:
                return Response("पिनकोड " + pin + " की लोकेशन नहीं मिली।",
                                mimetype="text/plain; charset=utf-8")
            _LL[pin] = (float(d[0]["lat"]), float(d[0]["lon"]), d[0].get("display_name",""))
        lat, lon, loc = _LL[pin]
        u2 = ("https://api.open-meteo.com/v1/forecast?latitude=" + str(lat) + "&longitude=" + str(lon) +
              "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
              "precipitation_probability_max,wind_speed_10m_max&timezone=Asia/Kolkata&forecast_days=7")
        with urllib.request.urlopen(u2, timeout=10) as r:
            da = json.loads(r.read())["daily"]
        city = [p.strip() for p in loc.split(",")]
        city = city[1] if len(city) > 1 else city[0]
        ls = ["*" + city + " - 7 दिन का मौसम* (पिन: " + pin + ")\n"]
        for i in range(7):
            dt = datetime.strptime(da["time"][i], "%Y-%m-%d")
            rm = da["precipitation_sum"][i] or 0
            rp = da["precipitation_probability_max"][i] or 0
            ln = ("*" + DAYS[dt.weekday()] + " (" + dt.strftime("%d %b") + ")*\n" +
                  wxl(da["weather_code"][i]) + "\n🌡 " + str(da["temperature_2m_min"][i]) +
                  "°C – " + str(da["temperature_2m_max"][i]) + "°C")
            if rm > 0:    ln += "\n🌧 " + str(rm) + "mm (" + str(rp) + "%)"
            elif rp > 20: ln += "\n🌧 संभव: " + str(rp) + "%"
            ln += "\n💨 " + str(da["wind_speed_10m_max"][i]) + " km/h"
            ls.append(ln)
            if i < 6: ls.append("—")
        ls.append("\n_Open-Meteo_")
        return Response("\n".join(ls), mimetype="text/plain; charset=utf-8")
    except Exception as e:
        logging.error("wa weather: %s", e)
        return Response("मौसम जानकारी अभी उपलब्ध नहीं है।",
                        mimetype="text/plain; charset=utf-8")

# ---- DIAGNOSTIC TEST PAGE ----
TEST_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Neev Seeds API - System Test</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:#F6F5F2;color:#040D1E;padding:24px 16px;line-height:1.5}
.wrap{max-width:820px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px;color:#134094}
.sub{font-size:13px;color:#737373;margin-bottom:24px}
.card{background:#fff;border:1px solid #E0DCD4;border-radius:10px;padding:18px 20px;margin-bottom:12px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.8px;color:#737373;margin-bottom:12px;font-weight:700}
.row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #F0EEE9;font-size:14px}
.row:last-child{border-bottom:none}
.dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;background:#CCC}
.dot.ok{background:#16A34A}.dot.bad{background:#DC2626}.dot.warn{background:#F7A600}
.lbl{flex:1;min-width:0}
.val{font-weight:600;font-size:13px;text-align:right;word-break:break-word;max-width:55%}
.val.ok{color:#16A34A}.val.bad{color:#DC2626}.val.warn{color:#C97C00}
pre{background:#F6F5F2;border:1px solid #E0DCD4;border-radius:8px;padding:12px;font-size:11.5px;white-space:pre-wrap;word-break:break-word;margin-top:10px;max-height:280px;overflow:auto;font-family:ui-monospace,Menlo,monospace}
.pill{display:inline-block;background:#134094;color:#fff;border-radius:20px;padding:2px 9px;font-size:10.5px;font-weight:600;margin-left:6px}
.pill.g{background:#16A34A}.pill.r{background:#DC2626}.pill.o{background:#F7A600;color:#040D1E}
.verdict{border-radius:10px;padding:16px 20px;margin-bottom:16px;font-weight:600;font-size:15px}
.verdict.ok{background:#DCFCE7;border:1px solid #86EFAC;color:#15803D}
.verdict.bad{background:#FEE2E2;border:1px solid #FCA5A5;color:#B91C1C}
.verdict.wait{background:#FEF3C7;border:1px solid #FCD34D;color:#92400E}
.tier{font-size:11px;color:#737373;margin-left:6px}
button{background:#134094;color:#fff;border:none;border-radius:8px;padding:10px 20px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;margin-top:8px}
button:hover{background:#0e2f7a}
.foot{font-size:11.5px;color:#999;margin-top:20px;text-align:center}
</style></head><body><div class="wrap">
<h1>Neev Seeds API - System Test</h1>
<div class="sub">Live diagnostic. Refresh to re-run all checks.</div>
<div id="verdict" class="verdict wait">Running checks...</div>

<div class="card"><h2>1. Server &amp; Database</h2><div id="c1"></div></div>
<div class="card"><h2>2. CORS (needed by website)</h2><div id="c2"></div></div>
<div class="card"><h2>3. Cascading Match 6&rarr;5&rarr;4&rarr;3</h2><div id="c3"></div></div>
<div class="card"><h2>4. WhatsApp Message Preview</h2><div id="c4"></div>
<pre id="wa">loading...</pre></div>
<div style="text-align:center"><button onclick="location.reload()">Re-run all tests</button></div>
<div class="foot">Endpoints: /health &middot; /find &middot; /weather &middot; /admin</div>
</div>
<script>
var B=location.origin;
function row(el,label,val,state,extra){
  document.getElementById(el).insertAdjacentHTML('beforeend',
    '<div class="row"><div class="dot '+state+'"></div><div class="lbl">'+label+
    (extra?'<span class="tier">'+extra+'</span>':'')+
    '</div><div class="val '+state+'">'+val+'</div></div>');
}
var score={pass:0,fail:0};
function mark(ok){ok?score.pass++:score.fail++;}

function verdict(){
  var v=document.getElementById('verdict');
  if(score.fail===0){v.className='verdict ok';v.textContent='All systems operational. WhatsApp, website and Google Sheet should all work.';}
  else{v.className='verdict bad';v.textContent=score.fail+' check(s) failed. See details below.';}
}

fetch(B+'/health').then(function(r){
  var cors=r.headers.get('access-control-allow-origin');
  return r.json().then(function(d){return {d:d,cors:cors,status:r.status}});
}).then(function(o){
  var ok=o.d.status==='ok';
  row('c1','Server responding','HTTP '+o.status,ok?'ok':'bad');mark(ok);
  row('c1','Database connection',ok?'connected':(o.d.message||'error').substring(0,40),ok?'ok':'bad');mark(ok);
  row('c1','Distributors loaded',(o.d.distributors||0)+' records',o.d.distributors>0?'ok':'bad');mark(o.d.distributors>0);
  var sr=o.d.sheet_rows||0, sp=o.d.sheet_pincodes||0;
  row('c1','Sheet rows synced',sr+' rows',sr>0?'ok':'warn',sp+' unique PINs');

  var chas=!!o.cors;
  row('c2','Access-Control-Allow-Origin',chas?o.cors:'MISSING - website will fail',chas?'ok':'bad');mark(chas);

  return fetch(B+'/find?pincode=152116');
}).then(function(r){return r.json()}).then(function(d){
  var arr=d.distributors||[];
  var hasArr=arr.length>0;
  row('c3','distributors[] array present',hasArr?'yes':'MISSING - website/sheet will fail',hasArr?'ok':'bad');mark(hasArr);
  row('c3','PIN 152116 (Abohar)',d.count+' found',d.count>=5?'ok':'warn','tier: '+(d.match_level||'?'));
  mark(d.count>=1);
  for(var i=0;i<arr.length;i++){
    row('c3','&nbsp;&nbsp;'+(i+1)+'. '+arr[i]['Distributor Name'],arr[i]['Mobile Number'],'ok');
  }
  document.getElementById('wa').textContent=d.message||'(no message field)';
  var wok=!!d.message;
  row('c4','message field for WATI',wok?'present':'MISSING - WhatsApp will fail',wok?'ok':'bad');mark(wok);
  row('c4','Character count',(d.message||'').length+' / 4096',(d.message||'').length<4096?'ok':'bad');
  mark((d.message||'').length<4096);
  return fetch(B+'/find?pincode=124401').then(function(r){return r.json()}).then(function(d){
    row('c3','PIN 124401 (Rohtak)',d.count+' found',d.count>=1?'ok':'warn','tier: '+(d.match_level||'?'));
    mark(d.count>=1);
  }).catch(function(){});
}).then(function(){
  return fetch(B+'/wa?pincode=152116').then(function(r){
    var ct=r.headers.get('content-type')||'';
    return r.text().then(function(t){return {ct:ct,t:t}});
  }).then(function(o){
    var isText=o.ct.indexOf('text/plain')>-1;
    row('c4','/wa plain-text endpoint',isText?'working ('+o.t.length+' chars)':'not deployed',isText?'ok':'bad');
    mark(isText);
  }).catch(function(){
    row('c4','/wa plain-text endpoint','not reachable','bad');mark(false);
  });
}).then(verdict).catch(function(e){
  row('c1','Fatal error',String(e).substring(0,60),'bad');mark(false);verdict();
});
</script></body></html>"""

@app.route("/test")
def test_page():
    return TEST_HTML, 200, {"Content-Type":"text/html; charset=utf-8"}

@app.route("/health")
def health():
    try:
        c = one("SELECT COUNT(*) as c FROM distributors")["c"]
        try:
            s = one("SELECT COUNT(*) as c FROM sheet_distributors")["c"]
            u = one("SELECT COUNT(DISTINCT pincode) as c FROM sheet_distributors")["c"]
        except Exception:
            s, u = 0, 0
        return jsonify({"status":"ok","distributors":c,"sheet_rows":s,"sheet_pincodes":u})
    except Exception as e:
        return jsonify({"status":"db_error","message":str(e)}),500

@app.route("/")
def home(): return "<h2>Neev Seeds API</h2><p><a href='/test'>System Test</a> | <a href='/health'>Health</a> | <a href='/admin'>Admin</a></p>"

SEED=[{"name": "Asia Agro Centre (Malout)", "pincode": "152107", "mobile": "9815212128", "address": "#148, New Grain Market, Malout, Sri Muktsar Sahib (Punjab)"}, {"name": "Bathinda Agro Centre (Malout)", "pincode": "152107", "mobile": "9872477601", "address": "New Grain Market, Shop Cum Flat 134, Malout, Muktsar (Punjab)"}, {"name": "Beniwal Pesticides & Seeds Store (Kabarwala)", "pincode": "151210", "mobile": "6280130878", "address": "G.T. Road, Vill. Kabarwala, Malout, Muktsar (Punjab)"}, {"name": "Brar Pesticides (Bazidpur)", "pincode": "152116", "mobile": "9463172845, 9464461796", "address": "Vpo. Bazidpur Bhoma, Abohar, Fazilka (Punjab)"}, {"name": "Chhabra Pesticides (Killianwali)", "pincode": "151211", "mobile": "7988105537", "address": "OPP. G.N. College , Malout Road, Mandi Killianwali, Mukstar (Punjab)"}, {"name": "Dahuja Khad Centre (Fazilka)", "pincode": "152123", "mobile": "9417204406", "address": "100-101, New Grain Market,Fazilka, Fazilka (Punjab)"}, {"name": "Jindal Fertilizers (Gidderbaha)", "pincode": "152101", "mobile": "9056100077", "address": "Husner Gate, Jindal Market, Near Jangir Jewellers, Gidderbaha, Muktsar (Punjab)"}, {"name": "Kisan Agro Centre (Malarampura)", "pincode": "335063", "mobile": "9781776187", "address": "Ward No. 1 Malarampura, Tehsil- Sangaria, District- Hanumangarh, State- Rajasthan, Pincode"}, {"name": "Malwa Seed And Pesticide (Alamwala)", "pincode": "151210", "mobile": "9915252440", "address": "Malout- Fazilka Road, Vill. Alamwala, Tehsil- Malout, District- Sri Muktsar Sahib, State- Punjab, Pincode"}, {"name": "Punjab Pesticides And Fertilizers (Panniwala Fatta)", "pincode": "151210", "mobile": "7355157413", "address": "Abohar Road, Panniwala Fatta, Tehsil- Malout, District- Sri Muktsar Shib, State- Punjab, Pincode"}, {"name": "Sewak Beej Bhandar (Lambi)", "pincode": "152107", "mobile": "9417066191", "address": "G.T. Road, Village-Lambi, Mukatsar (Punjab)"}, {"name": "Shiv Shakti Chemicals & Fertilizers (Fazilka)", "pincode": "152123", "mobile": "9855794542", "address": "#35, Additional New Anaz Mandi, Fazilka (Punjab)"}, {"name": "Shri Ram Beej Store (Abohar)", "pincode": "152116", "mobile": "7508149791", "address": "Booth No 8,Mandi No.1,Behind S.B.I,Abohar, Fazilka (Punjab)"}, {"name": "Zimidara Pesticides (Kandu Khera)", "pincode": "152113", "mobile": "9914481443", "address": "Near Balaji Filling Station, Malout, Kandu Khera, Haripura Road, Opp. Petrol Pump, Sri Muktsar Sahib (Punjab)"}, {"name": "Balaji Agriculture Store (Bandaheri)", "pincode": "125004", "mobile": "9306401471", "address": "Near Bus Stand, Bandaheri, Hisar (Haryana)"}, {"name": "Balaji Trading Company (Agroha)", "pincode": "125047", "mobile": "9991431600", "address": "Bhuna Road, Opp. HP Petrol Pump, Agroha, Hisar (Haryana)"}, {"name": "Balwant Beej Bhandar (Hisar)", "pincode": "125001", "mobile": "9034920665", "address": "OPPOSITE BUS STAND, SIRSA ROAD, HISAR, HARYANA"}, {"name": "Bharat Seed Centre (Hisar)", "pincode": "125001", "mobile": "9812341306", "address": "#4, Sirsa Road, Near Sewak Sabha Hospital, Hisar (Haryana)"}, {"name": "Choudhary Beej Bhandar (Bhadra)", "pincode": "335501", "mobile": "9414875347", "address": "Near New Bustand,Bhadra"}, {"name": "Dadi Sati Bhadal Kissan Sewa Kender (Miran)", "pincode": "127040", "mobile": "9812736366", "address": "Bhiwani (Haryana)"}, {"name": "Garg Khad Bhandar (Bhattu)", "pincode": "125053", "mobile": "9416726574", "address": "#71A,Locality Anaj Mandi, Bhattu (Fatehabad)"}, {"name": "Jagdamba Agro Sales (Bhattu Kalan)", "pincode": "125053", "mobile": "9416084321", "address": "#114, Anaj Mandi, Bhattu Kalan, Fatehabad (Haryana)"}, {"name": "Jakhar Beej Bhandar (Adampur Mandi)", "pincode": "125052", "mobile": "9812146421", "address": "Shop No. 11-A, Mandi Adampur, Hisar (Haryana)"}, {"name": "Kisan Seva Kender (Gopalwas)", "pincode": "127043", "mobile": "9812907020", "address": "Near Bus Stand Main Road , Gopalwas, Tehsil- Loharu, District- Bhiwani, State- Haryana, Pincode"}, {"name": "Kisan Sewa Kender (Mohabatpur Dhani)", "pincode": "125052", "mobile": "9468213515", "address": "Near Bus Stand,Mohabatpur Dhani"}, {"name": "Rahul Traders (Hisar)", "pincode": "125001", "mobile": "9034843248", "address": "#230-A, New Grain Markert, Hisar"}, {"name": "Saini Beej Bhandar (Neoli Kalan)", "pincode": "125001", "mobile": "9812865711", "address": "Goswami Misthan Bhandar, Neoli Kalan, Hisar (Haryana)"}, {"name": "Sangwan Beej Bhandar (Bhera)", "pincode": "127043", "mobile": "9992583647", "address": "Main Bus Stand, Bhera, Tosham, Bhiwani (Haryana)"}, {"name": "Sharma Beej Bhandar (Khabra Kalan)", "pincode": "125053", "mobile": "9991306084", "address": "Villege Khabra Kalan, Bhattu, 99913 06084"}, {"name": "Shree Ganesh Trading Company (Siwani)", "pincode": "127046", "mobile": "9813259477", "address": "Tosham Road,Siwani Mandi, Siwani (Bhiwani)"}, {"name": "Shree Namdev Sales Corporation (Gandhi Bari)", "pincode": "335501", "mobile": "9462935201", "address": "sahid bhagat singh chok gandhi bari bhadra  Hanumangarh"}, {"name": "Shri Krishna Beej Bhandar (Adampur Mandi)", "pincode": "125052", "mobile": "9996996990", "address": "Buth No. 17, New Anaj Mandi, Mandi Adampur, Hisar (Haryana)"}, {"name": "Verma Sales Corporation (Bhadra)", "pincode": "335501", "mobile": "9783448280", "address": "Near Gandhi Park Bhadra, District- Hanumangarh, State- Rajasthan, Pincode"}, {"name": "Agri Junction (One Stop Shop) (Mengalpura)", "pincode": "283112", "mobile": "9870926041", "address": "Rajakhera Road, Mengalpura , Iradatnagar, Tehsil: Kharagrh, District: Agra, State-Uttar Pradesh, Pincode"}, {"name": "Agri Junction Kendra (One Stop Shop) (Devi Atas)", "pincode": "281406", "mobile": "7535856666", "address": "Devi Atas, Parkham Road, Chhatikara, Mathura (Uttar Pradesh)"}, {"name": "Ankush Khad Beej Bhandar (Sonkh)", "pincode": "281123", "mobile": "8392826803", "address": "Tiraha Sonkh Goverdhan, Sonkh, Mathura (Uttar Pradesh)"}, {"name": "Arya Enterprises (Okhra)", "pincode": "283206", "mobile": "9458766909", "address": "Thana Narkhi Post Okhra, District- Firozabad, State-Uttar Pradesh, Pincode"}, {"name": "Chaudhary Kisan Seva Kendra (Iglas)", "pincode": "202124", "mobile": "7505559682", "address": "Vishnu Complex, Hathras Road, Iglas, Aligarh (Uttar Pradesh)"}, {"name": "Fauji Krishi Sewa Kendra (Kosi Khurd)", "pincode": "281003", "mobile": "9914405687", "address": "Near Shri Kunj Bihari Filling Station, Bharatpur Road, Kosi Khurd, Mathura (Uttar Pradesh)"}, {"name": "Jadaun Traders (Purdilnagar)", "pincode": "204214", "mobile": "9997990513", "address": "Jaleshar Road, Purdil Nagar, Hathras (Uttar Pradesh)"}, {"name": "Jain Agri Co. (Rajakhera)", "pincode": "328025", "mobile": "8739997335", "address": "Main Market,Near Jain Dharmshala, Rajakhera,Dholpur, Rajasthan"}, {"name": "Jay Shree Buldev Traders (Buldev)", "pincode": "281301", "mobile": "9411442114", "address": "Buldev, Baroli Road, Mathura (Uttar Pradesh)"}, {"name": "Khandelwal Khad Beej Bhandar (Goverdhan)", "pincode": "281502", "mobile": "8923978636", "address": "Bada Bazar, Goverdhan,Mathura"}, {"name": "Kisan Khad Bhandar (Sikandra Rao)", "pincode": "204215", "mobile": "9719716215", "address": "Lashkar Ganj, Hathras Road, Sikandra Rao, Hathras (Uttar Pradesh)"}, {"name": "Laxmi Khad Bhandar (Khair)", "pincode": "202138", "mobile": "9758757222", "address": "Tantigaon Road, Khair, District- Aligarh, State-Uttar Pradesh, Pincode"}, {"name": "Maa Kaila Devi Khad Beej Bhandar (Khediyacha)", "pincode": "281122", "mobile": "8433496442", "address": "pilua sadiqpur 1, farah, Mathura, Uttar Pradesh"}, {"name": "Mangal Trading Agenncy (Bari)", "pincode": "328021", "mobile": "8949767644", "address": "Bhardwaj Market, Bari, District- Dholpur, State-Rajasthan, Pincode"}, {"name": "Radhey Khad Beej Bhandar (Nagariya)", "pincode": "281403", "mobile": "9997562006", "address": "Guheta 10- Bisa, Nagariya, Kosikalan Rural, Tehsil- Chata, District- Mathura, State-Uttar Pradesh, Pincode"}, {"name": "S.P. Singh Agro Traders (Jattau)", "pincode": "283203", "mobile": "9897361858", "address": "Narkhi Road Jattau, District- Firozabad, State-Uttar Pradesh, Pincode"}, {"name": "Shri Ganesh Khad Beej Bhandar (Farah)", "pincode": "281122", "mobile": "9557584830", "address": "Deen Dayal Dham, Parkham Tiraha Achnera Road Farah, Mathura (Uttar Pradesh)"}, {"name": "Shri Krishna Agro Agency (Mathura)", "pincode": "281406", "mobile": "9808006390", "address": "Sonkh Road Mandi Chauraha, Shivdai Market, Mathura (Uttar Pradesh)"}, {"name": "Shri Nagaji Maharaj Beej Bhandar (Paigaon)", "pincode": "281401", "mobile": "9718640982", "address": "Khasra No.279, Near Rawat Marriage Home, VPO-Paigaon Chhata, Mathura (Uttar Pradesh)"}, {"name": "Shri Prabhu Kripa Agro Agency (Haveli Raya)", "pincode": "281204", "mobile": "9760022120", "address": "Fauji Market, Nagla Dakhni Mod, Raya Baldev Road, Haveli Raya, Tehsil- Mahavan, District- Mathura"}, {"name": "Balaji Khad Beej Bhandar (Akola)", "pincode": "283102", "mobile": "9634868316", "address": "00, Gharra Kalan, Akola, Tehsil-Kirawoli, District- Agra, State-Uttar Pradesh, Pincode"}, {"name": "Ganesh Agro Agency (Jeoni Mandi)", "pincode": "282003", "mobile": "9410636660", "address": "Vishal Cool Point Shop No- 90 New Market Jeoni, Mandi, Tehsil- Sadar, District-Agra"}, {"name": "Jai Shri Balaji Beej Bhandar (Dhumari)", "pincode": "207248", "mobile": "9927239828", "address": "Faghol Road, Dhumari, Tehsil- Aliganj, District- Etah, State-Uttar Pradesh, Pincode"}, {"name": "Laxmi Beej Bhandar (Aliganj)", "pincode": "207247", "mobile": "9759595251", "address": "Mainpuri Road, Moh Mevatiyan, Aliganj, District- Etah, State-Uttar Pradesh, Pincode"}, {"name": "Santosh Khad Beej Bhandar (Kuberpur)", "pincode": "283202", "mobile": "8755678088", "address": "Bhagupur Chauraha,Kuberpur, Etmadpur,Agra,Uttar Pradesh"}, {"name": "Bansal Khad Beej Bhandar (Reni)", "pincode": "301409", "mobile": "9694000892", "address": "Bus Stand Road, Reni, District -Alwar, State- Rajasthan, Pincode"}, {"name": "Chadot Khad Beej Bhandar (Lilee)", "pincode": "301406", "mobile": "9057245747", "address": "Vill. Lilee, Post. Malakhera, Alwar (Rajasthan)"}, {"name": "Chandralok Khad Beej Bhandar (Thanagaji)", "pincode": "301022", "mobile": "9660046437", "address": "23-16, Thanagaji, Alwar (Rajasthan)"}, {"name": "Chawla Beej Bhandar (Laxmangarh)", "pincode": "321607", "mobile": "9414856236", "address": "Bus Stand, Indra  Market, Laxmangarh (Alwar)"}, {"name": "Ganesh Beej Agency (Narayanpur)", "pincode": "301024", "mobile": "8003262513", "address": "Near Central Bank, Purushottam Market, Narayanpur, Thanagazi, Alwar (Rajasthan)"}, {"name": "Goyal Khad Beej Bhandar (Mubarikpur)", "pincode": "301025", "mobile": "9413065282", "address": "Main Market, Mubarikpur, Alwar (Rajasthan)"}, {"name": "Jai Kishan Khad Beej Bhandar (Bansur)", "pincode": "301402", "mobile": "7597852525", "address": "Kotputly Road, Subhash Road, Bansur (Alwar)"}, {"name": "Kanha Khad Beej Bhandar (Kathumar)", "pincode": "321605", "mobile": "9252364835", "address": "Main Bus Stand, W.No.16, Ahinsa Circle, Kathumar, Alwar (Rajasthan)"}, {"name": "Kisan Bazar (Kishangarh Bas)", "pincode": "301405", "mobile": "9414791715", "address": "Khasra No. 2240/946,  Alwar Road, Dist. Alwar (Rajasthan)"}, {"name": "Shri Balaji Khad Beej Bhandar (Baberi)", "pincode": "301412", "mobile": "9460800773", "address": "Sub Health Center Baberi,3, Unnaamed Road, Baberi Harsora, Dist. Alwar, Rajasthan, Pin Code"}, {"name": "Shubham Traders (Alwar)", "pincode": "301001", "mobile": "9414017423", "address": "Near Gau Shala,Station Road,Alwar"}, {"name": "Sushma Traders (Bibirani)", "pincode": "301702", "mobile": "8104077777", "address": "Ikrotiya, Bibirani, Alwar (Rajasthan)"}, {"name": "Uttam Beej Bhandar (Kotkasim)", "pincode": "301702", "mobile": "9461139610", "address": "Near Bus Stand,Kishangarh Road,Kotkasim,Alwar"}, {"name": "Haryali Pesticides & Fertilizer (Rawla)", "pincode": "335707", "mobile": "9782197116", "address": "SHOP NO. 48, SH 94, Punjab National Bank, Dhan Mandi, Rawla Mandi, Ganganagar"}, {"name": "Kisan Beej Bhandar (Nahrawali)", "pincode": "335703", "mobile": "8696250452", "address": "Shop No 03, Land No.34, E Block , Nahrawali, 11 ND, Anupgarh,  District- Sri Ganganagar, State - Rajasthan, Pincode"}, {"name": "Shanker Beej Bhandar (Khajuwala)", "pincode": "334023", "mobile": "8740994403", "address": "Urmul Cirrcle, Shop No.107, Tehbazari, Khajuwala, Bikaner (Rajasthan)"}, {"name": "Vadhera Fertilizers (Gharsana)", "pincode": "335707", "mobile": "9929231763", "address": "#172, New Dhan Mandi,  Gharsana, Sri Ganganagar (Rajasthan)"}, {"name": "Adarsh Trading Co. (Muradnagar)", "pincode": "201206", "mobile": "9837534426", "address": "Rawli Road, Chungi No-3, Muradnagar, District- Ghaziabad, State -Uttar Pradesh ,Pincode"}, {"name": "Agastya Krishi Seva Kendra (Sarai Aghat)", "pincode": "205302", "mobile": "8707693634", "address": "Sarai Aghat, Kaimganj Road, Merapur, Farrukhabad (Uttar Pradesh)"}, {"name": "Agrahari Agro Agency (Baberu)", "pincode": "210121", "mobile": "9506517118", "address": "148, Sunder Kuwan, Augashi Raod, Baberu, Banda, (Uttar Pradesh)"}, {"name": "Ahinsa Khad Beej Bhandar (Kurawali)", "pincode": "205265", "mobile": "7060188425", "address": "GT Road,Kurawali, Mainpuri,Uttar Pradesh"}, {"name": "Akash Khad Beej Bhandar (Mustafabad)", "pincode": "283207", "mobile": "9058226500", "address": "#2, Radhey Radhey Market, Eka Road, Mustafabad, Firozabad (Uttar Pradesh)"}, {"name": "Akhand Pratap Traders (Sarauth)", "pincode": "207247", "mobile": "8273448782", "address": "Sarauth Puwayan, Sarauth, Aliganj, Etah (Uttar Pradesh)"}, {"name": "Awadesh Beej Bhandar (Bhognipur)", "pincode": "209111", "mobile": "9307457227", "address": "H.No.242, Subhash Nagar, Near Nagar Palika Mod, Pukhrayan, Kanpur Dehat (Uttar Pradesh)"}, {"name": "Jay Kisan Beej Agancy (Pukhrayan)", "pincode": "209111", "mobile": "7897907194", "address": "Rajendra Nagar, Mandi Mod, Pukhrayan, Tehsil-Bhognipur, District-Kanpur Dehat, State-Uttar Pradesh, Pincode"}, {"name": "Kali Seeds and Fertilizers (Rampura)", "pincode": "285127", "mobile": "8883188850", "address": "Near Kali Filling Station, Rampura, Madhogarh, Jalaun (Uttar Pradesh)"}, {"name": "Neeraj Khad Evam Keetnasak Bhandar (Chitsona)", "pincode": "245402", "mobile": "9675892277", "address": "Village Chitsona, Tehsil Siana, Bulandshahr"}, {"name": "New Gupta Beej Bhandar (Hardoi)", "pincode": "241001", "mobile": "7355146035", "address": "Mohalla Sercular Road, Mandi Gate Ke Samne, Hardoi (Uttar Pradesh)"}, {"name": "Om Beej Bhandar (Allahaganj)", "pincode": "242220", "mobile": "8808847286", "address": "Near Bus Stand, Allahaganj, Shahjahanpur (Uttar Pradesh)"}, {"name": "Parihar Beej Bhandar (Mahatauli)", "pincode": "209715", "mobile": "7895936632", "address": "Mahatauli, NH-19, Sikandra, Kanpur Dehat (Uttar Pradesh)"}, {"name": "Shri Balaji Pesticide (Amroha)", "pincode": "244221", "mobile": "9410867377", "address": "Mohalla Kishan Ghar, Amroha (Uttar Pradesh)"}, {"name": "Anil Kumar Mittal & Sons (Uklana)", "pincode": "125113", "mobile": "8307840805", "address": "#168, New Anaj Mandi Suri Wala Chowk, Uklana Mandi, Hisar (Haryana)"}, {"name": "Baba Jeetgar Beej Bhandar (Sandlana)", "pincode": "125121", "mobile": "9416303009", "address": "VPO. Sandlana, Tehsil- Barwala, District-Hisar, State- Haryana, Pincode"}, {"name": "Baba Lal Dass Trading Co. (Data)", "pincode": "125049", "mobile": "9991181999", "address": "Village-Data, Hansi (Hisar)"}, {"name": "Baba Shri Ram Enterprises (Petwar)", "pincode": "125033", "mobile": "9992915457", "address": "Village -Petwar, Tehsil- Narnaund, District- Hisar, State- Haryana, Pincode"}, {"name": "Boora Pesiticides (Khanpur)", "pincode": "125033", "mobile": "7015911210", "address": "V.P.O Khanpur, Tehsil-Hansi, District- Hisar, State- Haryana, Pincode"}, {"name": "Durga Seed Store (Hansi)", "pincode": "125033", "mobile": "9813823313", "address": "New Bus Stand Road, Hansi (Hisar)"}, {"name": "Jamawari Beej Bhandar (Hansi)", "pincode": "125033", "mobile": "9466736052", "address": "Shop No. 37, Devi Lal Market, Jind Chowk, Hansi, District- Hansi, State- Haryana, Pincode"}, {"name": "New Chaudhary Khad Beej Bhandar (Tosham)", "pincode": "127040", "mobile": "9466567099", "address": "Siwani Road, Tosham, Tosham"}, {"name": "New Haryana Beej Farm (Hansi)", "pincode": "125033", "mobile": "9992354111", "address": "182/18, Ambedkar Chowk To Barsi Gate, Hansi"}, {"name": "Shree Ram Beej Bhandar (Narnaund)", "pincode": "125039", "mobile": "9991104039", "address": "Groun Floor, Opp. Anaj Mandi, Jind Hansi Road, Om Ortho Clinic and X Ray, Narnaund, Hisar (Haryana)"}, {"name": "Shri Ram Trading Company (Kheri Jalab)", "pincode": "125039", "mobile": "9416363987", "address": "Barwala Road, Kheri Jalab, Barwala (Hisar)"}, {"name": "Tara Chand And Sons (Barwala)", "pincode": "125121", "mobile": "8059298881", "address": "81, New Anaj Mandi, Barwala, Hisar (Haryana)"}, {"name": "Thakral Beej Bhandar (Jamalpur)", "pincode": "127035", "mobile": "9896731211", "address": "Near Bus Stand Road, Jamalpur (Bhiwani)"}, {"name": "Vishant Trading Co. (Dhamana)", "pincode": "125037", "mobile": "9812551866", "address": "VPO Dhamana, Hansi (Hisar)"}, {"name": "BM Trading Co. (Rampura Phul)", "pincode": "151103", "mobile": "9417078357", "address": "Rampura Phul, Bathinda (Punjab)"}, {"name": "Deepak Pesticides (Bathinda)", "pincode": "151001", "mobile": "9915142489", "address": "#42-A, Grain Market, Bathinda (Punjab)"}, {"name": "Ekaum Trading Co. (Rampura)", "pincode": "151103", "mobile": "8847557983", "address": "Old Anaj Mandi,  Near Civil Hospital, Old Cinema, Road,  Rampura Phul, District- Bathinda, State- Punjab, Pincode"}, {"name": "Kashmiri Lal Mohan Lal (Bhucho)", "pincode": "151101", "mobile": "9463027007", "address": "226, Main Bazar, Bhucho Mandi, Bathinda (Punjab)"}, {"name": "Kisan Pesticides (Natheha)", "pincode": "151302", "mobile": "8728093162", "address": "Near Gurudwara, Golewala Road, Natheha, Talwandi Sabo, Bathinda (Punjab)"}, {"name": "Nahil Trading Company (Maur Mandi)", "pincode": "151509", "mobile": "9041809993", "address": "New Krishna Mandir, Old Grain Market Maur, Bathinda , Punjab"}, {"name": "Naunihal Beej Bhandar (Talwandi Sabo)", "pincode": "151302", "mobile": "9815431162", "address": "Near Canara Bank, College Road, Talwandi Sabo, District- Bathinda, State- Punjab, Pincode"}, {"name": "New Sidhu Pesticides (Chak Hira Singh Wala)", "pincode": "151301", "mobile": "9779311018", "address": "Chak Hira Singh Wala Urf Pakka Khurd, Tehsil- Talwandi Sabo, District- Bathinda, State- Punjab, Pincode"}, {"name": "Puri Pesticides Store (Bathinda)", "pincode": "151001", "mobile": "8557811573", "address": "92C, Grain Market, Bathinda (Punjab)"}, {"name": "Sadhu Ram Vijay Kumar And Co. (Goniana Mandi)", "pincode": "151001", "mobile": "9417565311", "address": "442, M.C No. B-VI, Goniana Mandi, Bathinda (Punjab)"}, {"name": "Jai Dada Bhaiya Agriculture Store (Badhra)", "pincode": "127308", "mobile": "9813466232", "address": "Charkhi Dadri Main Road, Badhra, Charkhi Dadri (Haryana)"}, {"name": "Naresh Beej Bhandar (Dhigawa)", "pincode": "127201", "mobile": "9416296942", "address": "Loharu Road, Dhigawa, Bhiwani"}, {"name": "Punia Agriculture Store (Loharu)", "pincode": "127201", "mobile": "9416543415", "address": "Shop No 56 Surajgarh Road,Loharu, Loharu (Bhiwani)"}, {"name": "Laxmi Khad Beej Bhandar (Uchchain)", "pincode": "321302", "mobile": "7340491691", "address": "Bhayana Road Uchchain District- Bharatpur, State-Rajasthan, Pincode"}, {"name": "Pitaliya Krishi Seva Kendra (Kumher)", "pincode": "321201", "mobile": "7378104147", "address": "Deeg Road, Kumher, District- Bharatpur, State- Rajasthan, Pincode"}, {"name": "Rohit Agrotech (Bharatpur)", "pincode": "321001", "mobile": "8387805366", "address": "Sonkh Road, Ekta Vihar Bharatpur, District-Bharatpur, State- Rajasthan, Pincode"}, {"name": "Saini Hybrid Seeds (Nadbai)", "pincode": "321602", "mobile": "8529305007", "address": "Hdfc Bank , Dahara Road, Nadbai, District- Bharatpur, State- Rajasthan, Pincode"}, {"name": "Singhal Khad Veej Bhandar (Weir)", "pincode": "321408", "mobile": "9509098933", "address": "Purani Sabzi, Mandi, Weir, Tehsil- Weir, District- Bharatpur, State- Rajasthan, Pincode"}, {"name": "Balaji Beej Bhandar (Beerpur)", "pincode": "476337", "mobile": "7879558602", "address": "State- Madhya Pradesh, Pincode"}, {"name": "Jagdamba Traders (Pichhore)", "pincode": "475115", "mobile": "8460306347", "address": "Ward No.15 Kalindri Ke Pass Pichhore, District- Gwalior, State-Madhya Pradesh, Pincode"}, {"name": "Jai Gurudev Beej Agency (Joura)", "pincode": "476221", "mobile": "8770209137", "address": "Old Sabzi Mandi Joura, District- Morena, State-Madhya Pradesh"}, {"name": "Khemariya Agro Agencies (Bhind)", "pincode": "477001", "mobile": "9826372760", "address": "Near Veersen Dak Bangla Road, Bhind,Madhya Pradesh"}, {"name": "Maa Durga Krishi Seva Kendra (Ambah)", "pincode": "476111", "mobile": "9329370279", "address": "Near D.A.V. School, Porsa Road, Ambah, District- Morena, State-Madhya Pradesh, Pincode"}, {"name": "Chaudhary Agriculture Store (Bhiwani)", "pincode": "127021", "mobile": "9416230500", "address": "Booth No.22, New Anaj Mandi, Huda Towan Centre, Bhiwani, District- Bhiwani, State- Haryana, Pincode"}, {"name": "Chetan Ram Om Parkash (Charkhi Dadri)", "pincode": "127306", "mobile": "9416077021", "address": "Old Anaj Mandi, Charkhi Dadri"}, {"name": "Dariya Singh Pawan Kumar (Charkhi Dadri)", "pincode": "127306", "mobile": "7015268661", "address": "Old Anaj Mandi,  Charkhi Dadri, Bhiwani (Haryana)"}, {"name": "Maha Laxmi Trading Company (Bhiwani)", "pincode": "127021", "mobile": "9812467668", "address": "#122, New Grain Market, Bhiwani (Haryana)"}, {"name": "Sangwan Agriculture Store (Leghan)", "pincode": "127029", "mobile": "9991288300", "address": "Bus Stand Near Gramin Bank, Leghan, District- Bhiwani, State- Haryana, Pincode"}, {"name": "Tanwar Agriculture Store (Bhiwani)", "pincode": "127021", "mobile": "9050036008", "address": "Naya Bazar, Bhiwani, Bhiwani"}, {"name": "Hanuman Kisan Seva Kendra (Khorabisal)", "pincode": "302012", "mobile": "9928372015", "address": "Village- Khora Bisal, Tehsil- Amer, District- Jaipur, State- Rajasthan, Pincode"}, {"name": "Krishi Sahayata Kendra (Chomu)", "pincode": "303702", "mobile": "9829437617", "address": "Bus Stand , Morija Road Tehsil Chomu Distt. Jaipur, Rajasthan"}, {"name": "Sharma Agro Sales (Dolatpura)", "pincode": "303805", "mobile": "9828586681", "address": "Shop No 02, Surya Market, Dolatpura Bus Stand, Delhi Road, Tehasil Amer, District Jaipur, State- Rajasthan, Pincode"}, {"name": "Shri Balram Agri Clinic Centre (Chomu)", "pincode": "303702", "mobile": "9785549266", "address": "Ward No. 17, Radhabag, Mandi Road, Chomu, District- Jaipur, State- Rajasthan, Pincode"}, {"name": "Shri Ram Agri Clinic And Business Centre (Rampura)", "pincode": "303802", "mobile": "9414821584", "address": "Jahota Road, Rampura,Tehsil- Amer, District- Jaipur, State -Rajasthan, Pincode"}, {"name": "Sumant Agro Inputs (Nangal Purohit)", "pincode": "302013", "mobile": "9828568899", "address": "Hanuman Vaitka-3, B-31, Nangal Purohit, Near Rajawas Road, District- Jaipur, State- Rajasthan, Pin Code"}, {"name": "Balaji Khad Beej Bhandar (Mandawari)", "pincode": "303504", "mobile": "9929024915", "address": "Main Bus Stand, Mandawariteh, Tehsil- Lalsot, District- Dausa, State- Rajasthan, Pincode"}, {"name": "Kisaan Enterprises (Kaluwas)", "pincode": "303505", "mobile": "9950540820", "address": "Ward No.3, Near Gopal Ji Mandir, Main Road, Kaluwas, District- Dausa, State- Rajasthan, Pincode"}, {"name": "Mamodia Brothers (Dausa)", "pincode": "303303", "mobile": "9414271563", "address": "29, Manganj, Station Road, Dausa, District- Dausa, State- Rajasthan, Pincode"}, {"name": "Nai Nath Agro Center (Bassi)", "pincode": "303301", "mobile": "9828385155", "address": "Toonga Road, Fruit Market, Bassi, District- Jaipur, State- Rajsthan, Pincode"}, {"name": "Pawan Trading Company (Bharoti)", "pincode": "322024", "mobile": "9636286802", "address": "SH-1, Near Punjab National Bank, Bharoti, District-Sawal Madhopur, State- Rajasthan, Pincode"}, {"name": "Shree Ram Khad Beej Bhandar (Baijwadi)", "pincode": "303505", "mobile": "8387060733", "address": "Shop No. 01, Ward No.05, Gram Baijwadi, Tehsil- Nangal Rajawatan, District- Dausa"}, {"name": "Shri Krishan Krishi Kendra (Mahwa)", "pincode": "321608", "mobile": "9413489596", "address": "NH-11, Jaipur Agra Road, Mahwa, Tehsil-Mahwa, District- Dausa, State-Rajasthan, Pincode"}, {"name": "Bidhauliya Khad Beej Bhandar (Bah)", "pincode": "283104", "mobile": "9897234887", "address": "Etwah Road,Bah, Agra Uttar Pradesh"}, {"name": "Hari Khad Bhandar (Bharthana)", "pincode": "206242", "mobile": "8650770851", "address": "Motiganj, Bharthana, Etawah (Uttar Pradesh)"}, {"name": "Kisan Khad Bhandar (Bewar)", "pincode": "206301", "mobile": "9457882836", "address": "Bewar, G.T. Road, Mainpuri (Uttar Pradesh)"}, {"name": "Kisan Khad Bhandar (Ekdil)", "pincode": "206126", "mobile": "8474987557", "address": "Kumharan Sarai, Ekdil, Etawah (Uttar Pradesh)"}, {"name": "Om Khad Beej Bhandar (Manikpur)", "pincode": "202564", "mobile": "9760111141", "address": "Village Manikpur, Tehsil-Karhal, District-Mainpuri, State-Uttar Pradesh, Pincode"}, {"name": "Poonam Beej Bhandar (Etawah)", "pincode": "206001", "mobile": "8006037966", "address": "Kachora Road, Etawah, Uttar Pradesh"}, {"name": "Rajput Beej Bhandar (Babarpur)", "pincode": "206247", "mobile": "9997588914", "address": "Near Trivedi Guest House, Babarpur, Auraiya (Uttar Pradesh)"}, {"name": "Shiv Khad Bhandar (UDI Mod Etawah)", "pincode": "206131", "mobile": "8979832980", "address": "UDI Mod  Etawah , District- Etawah, State-Uttar Pradesh, Pincode"}, {"name": "Shree Shyam Agro Agency (Bhikhepur)", "pincode": "206129", "mobile": "8395845186", "address": "Near Bhikhepur By  Pass  Chauraha, Jua Road, Ajitmal, District- Auraiya, State- Uttar Pradesh, Pincode"}, {"name": "Shri Ganesh Fertilizers (Etawah)", "pincode": "206001", "mobile": "9758677311", "address": "Farukhabad Road , Near Gate No. 2 , New Mandi, District- Etawah, State-Uttar Pradesh, Pincode"}, {"name": "Tripathi Beej Bhandar (Phaphund)", "pincode": "206247", "mobile": "8865068897", "address": "Phaphund Sub Post Office, Dibiyapur Road, Chamanganj, Phaphund, Auraiya (Uttar Pradesh)"}, {"name": "Unnat Krishi Seva Kendra (Lahar)", "pincode": "477445", "mobile": "9897361858", "address": "New Subzi Mandi Lahar, District- Bhind, State-Madhya Pradesh, Pincode"}, {"name": "Vedant Agro Agency (Bakewar)", "pincode": "206124", "mobile": "9413376351", "address": "Auraiya Road Bakewar, District- Etawah, State -Uttar Pradesh , Pincode"}, {"name": "Ajay Beej Bhandar (Ratia)", "pincode": "125051", "mobile": "9416568620", "address": "Bhagat Singh Chowk, Tohana Road, Ratia, Fatehabad (Haryana)"}, {"name": "Bajrangi Pesticides (Tohana)", "pincode": "125120", "mobile": "9812756372", "address": "Near Under Railway Bridge, Tohana, Fatehabad (Haryana)"}, {"name": "Jai Balaji Seeds (Fatehabad)", "pincode": "125050", "mobile": "9812202333", "address": "16-A Anaj Mandi Fatehabad, Vil. Fatehabad , Distt. Fatehabad State Haryana"}, {"name": "Jakhar Beej Bhandar (Chandrawal)", "pincode": "125111", "mobile": "8607283109", "address": "Fatehabad Road, Chandrawal, Bhuna, Fatehabad (Haryana)"}, {"name": "Jambha Trading Company (Dhangar)", "pincode": "125048", "mobile": "9991618950", "address": "Dhani Majra Road, Village Dhangar (Fatehabad), Haryana, Pincode"}, {"name": "Jannat Pesticides (Fatehabad)", "pincode": "125050", "mobile": "9416127322", "address": "#76B, Anaj Mandi, Fatehabad (Haryana)"}, {"name": "Kissan Agriculture Store (Bhuna)", "pincode": "125111", "mobile": "9812520736", "address": "Uklana Road, Bhuna, Fatehabad, Haryana"}, {"name": "Kissan Trading Company (Kamana)", "pincode": "125051", "mobile": "9356246193", "address": "Near Bus Stand Vill. Kamana, Tehsil- Ratia, District- Fatehabad, State- Haryana,  Pincode"}, {"name": "New Farmer Trading Company (Fatehabad)", "pincode": "125050", "mobile": "9812741400", "address": "Shop No - 85 B , Old Anaj Mandi, Fatehabad, District- Fatehabad, State- Haryana, Pincode"}, {"name": "RK Agricultire Store (Tohana)", "pincode": "125120", "mobile": "9416239005", "address": "Shop No. 18 Opp. Aggarwal Bhawan, New Anaj Mandi, Tohana, District- Fatehabad, State - Haryana, Pincode"}, {"name": "Ravi Beej Bhandar (Mochiwali)", "pincode": "125111", "mobile": "9416805819", "address": "Main Chowk, Vill. Mochiwali, Bhuna, Fatehabad (Haryana)"}, {"name": "Shree Balaji Beej Bhandar (Jandli Kalan)", "pincode": "125111", "mobile": "9812250050", "address": "Opp. GSS School, Jandli Kalan, Bhuna, Fatehabad (Haryana)"}, {"name": "Suresh & Co. (Gorakhpur)", "pincode": "125047", "mobile": "9813444266", "address": "Village Gorakhpur, Fatehabad"}, {"name": "Anndata Kishan Seva Kender (Nohar)", "pincode": "335523", "mobile": "6350471043", "address": "#11B, New Anaj Mandi, Nohar, Hanumangarh (Rajasthan)"}, {"name": "Bhadu Agro Centre (Hanumangarh)", "pincode": "335512", "mobile": "9461394112", "address": "Shop No.221- B , New Dhan Mandi, Hanumangarh, District- Hanumangarh, State- Rajasthan, Pincode"}, {"name": "Bhanwar Lal Jagdish Prasad (Pilibanga)", "pincode": "335803", "mobile": "9686960128", "address": "Sector No.01,Shop No.309-310, New Dhan Mandi Ke, Samne,  Tehsil- Pilibanga, District- Hanumangarh"}, {"name": "Chhabra Seeds & Pesticides (Hanumangarh Town)", "pincode": "335513", "mobile": "9829075736", "address": "#88 B, New Anaj Mandi, Hanumangarh Town, Hanumangarh (Rajasthan)"}, {"name": "Deepak Pesticides (Chautala)", "pincode": "125101", "mobile": "9166166661", "address": "Near Main Bazar, Mandi Dabwali, Chautala, Sirsa (Haryana)"}, {"name": "Fagodia Pesticides And Fertilizers (Rawatsar)", "pincode": "335524", "mobile": "8239473515", "address": "Shop No.24, General Market, Rawatsar, District- Hanumangarh, State- Rajasthan, Pincode"}, {"name": "Guru Kirpa Sales Agencies (Masitawali Head)", "pincode": "335526", "mobile": "9649371963", "address": "Nera Gurudwara, Masita Wali Head,Tibbi, Hanumangarh, (Rajasthan)"}, {"name": "Guru Nanak Dev Pesticides (Jandawali)", "pincode": "335512", "mobile": "9887707542", "address": "Sgnr Road,Near Bus Stand, Jandawali,Hanumangarh, Rajasthan"}, {"name": "Haldhar Sewa Kendra (Ridmalsar)", "pincode": "335041", "mobile": "9413777105", "address": "Ridmalsar,59 LNP,Padampur Gajsinghpur Road, Padampur Sub Post Office,Tehsil Padampur, Sri Ganganagar,Rajasthan"}, {"name": "Jai Shree Hanuman Pesticides (Fatehgarh)", "pincode": "335801", "mobile": "9829469351", "address": "W.NO.5, Khelari Bass, Fatehgarh, Hanumangarh (Rajasthan)"}, {"name": "Karni Pesticides (Saliwala)", "pincode": "335526", "mobile": "9649875166", "address": "Main Road Saliwala, Sangria, Hanumangarh (Rajasthan)"}, {"name": "Khadria Pesticides (Sangaria)", "pincode": "335063", "mobile": "9828187733", "address": "Shop No. 88, Old Grain Market, Sangaria, District- Hanumangarh, State-Rajasthan, Pincode"}, {"name": "Kisan Agro Clinic (Dabli Khurd)", "pincode": "335526", "mobile": "9982792774", "address": "Ward No.4, Dabli Khurd, Tibbi, Hanumangarh (Rajasthan)"}, {"name": "Lal Chand Sohan Lal (Padampur)", "pincode": "335041", "mobile": "8094434006", "address": "Shop No. 9 Purani Dhan Mandi Padampur, District- Ganganagar, State- Rajasthan, Pincode"}, {"name": "Laxmi Beej Bhandar (Sadulshahar)", "pincode": "335062", "mobile": "9783700070", "address": "New Dhan Mandi, #131, Sadul Shahar, Sri Ganganagar (Rajasthan)"}, {"name": "Luxmi Om Pesticides (Goluwala)", "pincode": "335802", "mobile": "8790183035", "address": "#124, Dhan Mandi, Goluwala, Hanumangarh (Rajasthan)"}, {"name": "Mahala Pesticides (Pallu)", "pincode": "335524", "mobile": "8955757338", "address": "#04, Near Sadula Ji Mandir, Pallu, Rawatsar, Hanumangarh (Rajasthan)"}, {"name": "Middha Pesticides & Fertilizers (Kesrisinghpur)", "pincode": "335027", "mobile": "8078603212", "address": "148,Teh Bazari,Kesrisinghpur,Sri Ganganagar, Rajasthan"}, {"name": "Nandan Fertilizers (Hanumangarh Town)", "pincode": "335513", "mobile": "9414332625", "address": "Shop No. 8, Nai Dhan Mandi, Hanumangarh Town-335513, Rajasthan"}, {"name": "Ruby Seeds (Sri Ganganagar)", "pincode": "335001", "mobile": "9784057700", "address": "Chak 7e Chhoti, Hanumangarh Road, Sri Ganganagar (Rajasthan)"}, {"name": "Shishan Kumar Karan Kumar (Sadulshahar)", "pincode": "335062", "mobile": "9460563418", "address": "#12, Bhagat Singh Market, Sadul Shahar, Sri Ganganagar (Rajasthan)"}, {"name": "Shree Balaji Beej Bhandar (Tibbi)", "pincode": "335526", "mobile": "7597645009", "address": "Main Bazar,Tibbi,Hanumangarh"}, {"name": "Shree Ganesh Agro Chemical (Sri Karanpur)", "pincode": "335073", "mobile": "9414432354", "address": "Sh. No.10-Old Dhan Mandi, Srikaranpur, Sri Ganganagar (Rajasthan)"}, {"name": "Shree Ram Beej Bhandar (Rawatsar)", "pincode": "335524", "mobile": "9636729200", "address": "Shop No.10 General Market, Rawatsar,Hanumangarh, Rajasthan"}, {"name": "Shri Ganesh Seeds & Pesticides (Sangaria)", "pincode": "335063", "mobile": "9461295608", "address": "#10, New Dhan Mandi, Near PNB Bank, Sangaria, Hanumangarh (Rajasthan)"}, {"name": "Shri Ganpati Agro (Sri Ganganagar)", "pincode": "335001", "mobile": "8559831401", "address": "Sh.No.08, Udyog Market, New Dhan Mandi Road, Gate No.2, Near Canera Bank, Sri Ganganagar (Rajasthan)"}, {"name": "Singh Agri Business (Hanumangarh Town)", "pincode": "335513", "mobile": "9928057418", "address": "#143, Genral Market, Dhan Mandi, Hanumangarh (Rajasthan)"}, {"name": "Vijay Trading Company (Sangaria)", "pincode": "335063", "mobile": "9414380744", "address": "New Dhan Mandi, Sangaria, Hanumangarh, Rajasthan"}, {"name": "Zamidara Agrico (Hanumangarh Town)", "pincode": "335513", "mobile": "9461313031", "address": "#146, New Dhan Mandi Road, Hanumangarh Town, Hanumangarh (Rajasthan)"}, {"name": "Zamindara Pesticides (Nohar)", "pincode": "335523", "mobile": "9521252730", "address": "Shop No.05-A,  Anaj Mandi Nohar, District- Hanumangarh, State- Rajasthan, Pincode"}, {"name": "Annapoorna Sales (Moga)", "pincode": "142001", "mobile": "9298300006", "address": "615, Old Grain Market, Moga (Punjab)"}, {"name": "Arora Seed Store (Patti)", "pincode": "143416", "mobile": "9872683794", "address": "H.No.654/13, Nikki Mandi, Patti, Tarn Taran (Punjab)"}, {"name": "Bansal Seeds (Mullanpur)", "pincode": "141101", "mobile": "7508260067", "address": "Shop No. 30, Grain Market, Mullanpur, District- Ludhiana, State- Punjab, Pincode"}, {"name": "Chaman Lal Arora Seed Store (Patti)", "pincode": "143416", "mobile": "9888722299", "address": "Court Road Nikki Mandi Patti, Tarn Taran, Punjab"}, {"name": "Guru Ram Dass Seed Store (Talwandi Chaudhrian)", "pincode": "144606", "mobile": "6280870547", "address": "OP.Bank Talwandi Chaudhrian, (Kapurthala), Punjab"}, {"name": "Jagan Nath Beej Bhandar (Nihal Singh Wala)", "pincode": "142055", "mobile": "9855388751", "address": "Grain Market Mandi Nihal Singh Wala, District- Moga, State- Punjab, Pincode"}, {"name": "Kahlon Pesticides (Batala)", "pincode": "143505", "mobile": "9023360006", "address": "Dera Baba Nanak Road,Batala, District-Gurdaspur, State -Punjab, Pincode"}, {"name": "Krishna Trading Co. (Adda Bassi)", "pincode": "146111", "mobile": "7973534246", "address": "Bassi Daulat Khan, Hoshiarpur (Punjab)"}, {"name": "Kulwant Rai Jai Gopal (Jagraon)", "pincode": "142026", "mobile": "9417310625", "address": "Nehru Market Jagraon, Distt. Ludhiana (Punjab)"}, {"name": "Malerkotla Seed Store (Mullanpur)", "pincode": "141101", "mobile": "9814437716", "address": "G.T. Road, Near ICICI Bank, Mandi Mullanpur, District- Ludhiana, State- Punjab, Pincode"}, {"name": "Pall Seed Store (Hoshiarpur)", "pincode": "146001", "mobile": "9815448101", "address": "Old Sabzi Mandi, Afgan Road, Hoshiarpur (Punjab)"}, {"name": "Punjab Seed Store (Hoshiarpur)", "pincode": "146001", "mobile": "9465507777", "address": "#33, New Grain Market, Hoshiarpur (Punjab)"}, {"name": "Rajesh Gupta Seed Farms (Gurdaspur)", "pincode": "143521", "mobile": "9814120350", "address": "Sadar Bazar, Main Market, Distt. Gurdaspur (Punjab)"}, {"name": "Rajvans Seeds Corporation (Amritsar)", "pincode": "143001", "mobile": "9417253500", "address": "Old Sabzi Mandi, Amritsar (Punjab)"}, {"name": "Royal Seed Store (Hoshiarpur)", "pincode": "146001", "mobile": "9872643890", "address": "Banjar Bagh Chowk Bharwain Road Salwara, Hoshiarpur, Punjab , Pincode"}, {"name": "Saini Brothers (Rajpur Bhaian)", "pincode": "146112", "mobile": "9417585764", "address": "Village Rajpur Bhaian, (Hoshiarpur), Punjab, Pin Code"}, {"name": "Amit Beej Bhandar (Murthal)", "pincode": "131027", "mobile": "7497007213", "address": "VPO- Murthal, Hassanpur Chowk, Tajpur Road, Sonepat (Haryana)"}, {"name": "Bhawani Khad Beej Bhandar (Jhajjar)", "pincode": "124103", "mobile": "9991472342", "address": "Kosli Road Jhajjar, District- Jhajjar, State - Haryana, Pincode"}, {"name": "Bhuvika Khad Beej Bhandar (Jahazgarh)", "pincode": "124103", "mobile": "9991443468", "address": "V.P.O- Jahazgarh, Tehsil- Beri, District- Jhajjar, State- Haryana, Pincode"}, {"name": "Chaudhary Beej Bhandar (Kharkhoda)", "pincode": "131402", "mobile": "9812251951", "address": "Sampla Road, Kharkhoda, Kharkhoda (Sonipat)"}, {"name": "Dulhera Javik Producer Company Limited (Dulhera)", "pincode": "124507", "mobile": "8059199620", "address": "Near Jasu Petrol Pump, Dulhera, Tehsil- Bahadurgarh,  District- Jhajjar, State- Haryana, Pincode"}, {"name": "Kisan Agro Traders (Morwala)", "pincode": "127306", "mobile": "8607272100", "address": "Shop No.:00, Near Kanheti Mor, Morwala, District- Charkhi Dadri, State- Haryana, Pincode"}, {"name": "Kisan Sewa Kender (Dighal)", "pincode": "124107", "mobile": "7404234525", "address": "Dighal Chowk , Jhajjar Road , Dhandlan, Jhajjar Haryana"}, {"name": "Kissan Beej Bhandar (Kulana)", "pincode": "124108", "mobile": "9728513250", "address": "Main Bus Stand Kulana Chowk, Kulana, District- Jhajjar, State- Haryana, Pincode"}, {"name": "Lakhdatar Khad Beej Bhandar (Akehri Madanpur)", "pincode": "124146", "mobile": "9991196985", "address": "VPO. Akehri Madanpur, Tehsil- Matanhail, District- Jhajjar, State- Haryana, Pincode"}, {"name": "New Kissan Beej Bhandar (Haily Mandi)", "pincode": "122504", "mobile": "9812605696", "address": "Near Shiv Mandir , Old Anaj Mandi, Pataudi (Gurugram)"}, {"name": "OP Traders (Mankrola)", "pincode": "122505", "mobile": "8930781630", "address": "126, Village- Mankrola, Pagan Paints, Garhi Harsaru, Dist. Gurugram, Haryana"}, {"name": "Om Sai Beej Bhandar (Pataudi)", "pincode": "122503", "mobile": "9050216791", "address": "Rewari Road, Pataudi, Distt. Gurugram, State- Haryana, Pincode"}, {"name": "Rajawat Khad & Beej Bhandar (Machhrouli)", "pincode": "124108", "mobile": "9416390108", "address": "Machhrouli, Jhajjar"}, {"name": "Rathi Khad Bhandar (Sampla)", "pincode": "124501", "mobile": "9416961950", "address": "Shop No.8 Anaj Mandi , Sampla, Tehsil- Sampla, District- Rohtak, State- Haryana, Pincode"}, {"name": "S K Khad Beej Bhandar (Jasrana)", "pincode": "131408", "mobile": "8708888456", "address": "V.P.O. Jasrana, Tehsil- Gohana, District-Sonipat, State- Haryana, Pincode"}, {"name": "Shiv Khad Bhandar (Jakhauli)", "pincode": "131023", "mobile": "8178732993", "address": "257 Village Jakhauli ,Sonipat, Haryana"}, {"name": "Balaji Agro Agency (Jalaun)", "pincode": "285123", "mobile": "9839144548", "address": "Kotwali Road Jalaun, District-Jalaun, State-Uttar Pradesh, Pincode"}, {"name": "Harshika Beej Bhandar (Prithvipur)", "pincode": "472336", "mobile": "8223878516", "address": "Orchha Road, Prithvipur, District- Niwari, State-Madhya Pradesh, Pincode"}, {"name": "Kamal Beej Bhandar (Mauranipur)", "pincode": "284204", "mobile": "9415944847", "address": "Old Sabji Mandi, Nazha Bazar, Mauranipur, Jhansi (Uttar Pradesh)"}, {"name": "Madhav Beej Bhandar (Tikamgarh)", "pincode": "472339", "mobile": "8821092711", "address": "A, Near State Bank of India, Digoda, Tikamgarh, District- Tikamgarh, State-Madhya Pradesh, Pincode"}, {"name": "Mata Shree Traders (Bhojla Jhansi)", "pincode": "284002", "mobile": "9651823740", "address": "Bala Ji Road Bhojla, Jhansi Uttar Pradesh"}, {"name": "Mohini Beej Bhandar (Babina)", "pincode": "284401", "mobile": "8375070147", "address": "Main Road  Babina, District- Jhansi, State-Uttar Pradesh , Pincode"}, {"name": "Mon Sadhana krishi kendra (Rath)", "pincode": "210431", "mobile": "9453405501", "address": "Charkhari  Road ,0, Sikandarpura, Rath, Hamirpur, Hamirpur, Uttar Pradesh"}, {"name": "Raghav Krishi Seva Kendra (Jalaun)", "pincode": "285123", "mobile": "9369309760", "address": "Dhanja Kyolari Jalaun, District-Jalaun, State-Uttar Pradesh, Pincode"}, {"name": "Ragni Traders (Chirgaon)", "pincode": "284301", "mobile": "9415588119", "address": "Bhander Road,Rana Ka Bagichha, Chirgaon,Jhansi,Uttar Pradesh"}, {"name": "Rajesh Agencies (Ghughua)", "pincode": "284201", "mobile": "7084515099", "address": "GHUGHUWA, BARWASAGAR, Dist. Jansi, Uttar Pradesh"}, {"name": "Rana Krishi Seva Kendra (Bhitarwar)", "pincode": "475220", "mobile": "9977041292", "address": "Ward No. 9, Karahiya Road, Bhitarwar, Gwalior, Tehsil Bhitarwar  Distt. Gwalior State Madhya Prade"}, {"name": "Rayeen Khad Bhandar (Samthar)", "pincode": "284304", "mobile": "6387163619", "address": "01, Agga Bazar, Samthar, Jhansi (Uttar Pardesh)"}, {"name": "Ruhi Agro Agency (Bangra)", "pincode": "285221", "mobile": "8299889573", "address": "Bhind Road , Bangra, District- Jalaun, State-Uttar Pradesh, Pincode"}, {"name": "Sai Astha Beej (Bhander)", "pincode": "475336", "mobile": "9340488876", "address": "Datia Bhander Road, Near Bus Stand, Bhander, District- Datia, State-Madhya Pradesh, Pincode"}, {"name": "Shri Hari Khad Beej Bhandar (Mahoba)", "pincode": "210427", "mobile": "9452122411", "address": "Laundi Tiraha Bhatipura Peeli Kothi Ke Bagal Me, District- Mahoba, State-Uttar Pradesh, Pincode"}, {"name": "Shri Vidya KIsan Bazar Ltd. (Lalitpur)", "pincode": "284403", "mobile": "9170223336", "address": "01,Kailaguwan Road Near Ambrosiya, Colony,Lalitpur, Uttar Pradesh 28443"}, {"name": "Upadhyay Khad And Beej Bhandar (Moth)", "pincode": "284303", "mobile": "8358933119", "address": "Near Bharat Petrol Pump, Akhada Pura, Shahpur Road, Moth, District- Jhansi"}, {"name": "Ashish Beej Bhandar (Manoharpur)", "pincode": "126102", "mobile": "7206805800", "address": "Old Bank Gali, Manoharpur, Jind (Haryana)"}, {"name": "Jivant Agriculture Store (Julana)", "pincode": "126101", "mobile": "9813917999", "address": "Booth No. 13 New Anaj Mandi Julana, District- Jind, State- Haryana, Pincode"}, {"name": "Khajan Chand Naveen Kumar (Jind)", "pincode": "126102", "mobile": "9416138306", "address": "Old Anaj Mandi, Jind"}, {"name": "Piyush Trading Company (Julana)", "pincode": "126101", "mobile": "9416318004", "address": "Booth No 80.Anaj Mandi , Julana"}, {"name": "Shiv Beej Bhandar (Jind)", "pincode": "126102", "mobile": "9416558553", "address": "Old Sabji Mandi,Jind, Distt. Jind (Haryana)"}, {"name": "Shree Vishnu Fertilizer (Jind)", "pincode": "126102", "mobile": "9416108979", "address": "#28, New Anaj Mandi, Jind (Haryana)"}, {"name": "Sindhu Beej Bhandar (Julana)", "pincode": "126101", "mobile": "9255853000", "address": "Shop No.62, New Anaj Mandi, Julana, District- Jind, State- Haryana, Pincode"}, {"name": "AGR Seeds (Sardarshahar)", "pincode": "331403", "mobile": "8078671299", "address": "Shop No. 1 Near Power House, Railway Station Road, Sardarshahar, District -Churu, State- Rajasthan"}, {"name": "Balaji Beej Bhandar (Khoor)", "pincode": "332023", "mobile": "7976892559", "address": "Bus Stand, Sikar Road, V.P.O. Khoor, Tehsil- Dataramgarh, District- Sikar, State -Rajasthan, Pincod"}, {"name": "Gora Seeds (Dudu)", "pincode": "303008", "mobile": "8003224109", "address": "Malpura Road, Dudu, Tehsil-Mozmabad, District- Jaipur, State- Rajasthan, Pincode"}, {"name": "Gupta Traders (Jhalrapatan)", "pincode": "326023", "mobile": "9414194049", "address": "Khasra no 2320, Sunel Road, Near Petrol Pump, Jhalrapatan, District- Jhalrapatan, State- Rajasthan  Pincode"}, {"name": "Jai Shree Balaji Beej Bhandar (Beri)", "pincode": "332031", "mobile": "9610008143", "address": "Kisan Market , Dharmshala Beri, District- Sikar, State- Rajasthan, Pincode"}, {"name": "Khandelwal Krishi Beej Bhandar (Kishangarh)", "pincode": "305801", "mobile": "9610413611", "address": "Plot No-145, B-4, Vyapari Mohalla Ke Gali Ka Pass, Kishangarh, District- Ajmer, State- Rajasthan, Pincode"}, {"name": "Kisan Krishi Seva Kendra (Khatushyam Ji)", "pincode": "332404", "mobile": "7062777747", "address": "Manda Choraha, Khatushyam Ji, District- Sikar, State- Rajasthan, Pincode"}, {"name": "Malav Krishi Seva Kendra (Chhabra)", "pincode": "325220", "mobile": "9414808000", "address": "Mama Hotel Ki Gali , Chhabra, District-Baran, State- Rajasthan, Pincode"}, {"name": "Mittal Khad Beej Bhandar (Deoli)", "pincode": "304804", "mobile": "9414029014", "address": "Near Ambedkar Circle, Nehru Market, Deoli, Tonk, Rajasthan"}, {"name": "Pride Agro (V.K.I Area Jaipur)", "pincode": "302013", "mobile": "9413063878", "address": "SP-818-C, OPP. Rochees Watch, Road No.14, VKI. Jaipur District -Jaipur, State- Rajasthan Pincode"}, {"name": "Rohit Agro Agencies (Palunda)", "pincode": "303505", "mobile": "7014996276", "address": "Vill. Palunda, Tehsil- Rahuwas, District- Dausa, State- Rajasthan, Pincode"}, {"name": "Sharma Khad Beej Bhandar (Khawaraoji)", "pincode": "303506", "mobile": "9828269907", "address": "Vill. Khawaraoji, Tehsil- Paparda, District- Dausa, State- Rajasthan, Pincode"}, {"name": "Shree Mahaveer Enterprises (Bundi)", "pincode": "323001", "mobile": "9928581050", "address": "Lanka Gate, Chittor Road, Bundi, District- Bundi, State- Rajasthan, Pincode"}, {"name": "Shri Chouhan Krishi Seva Kendra (Sarwar)", "pincode": "305403", "mobile": "9828545229", "address": "Kota Road, Sarwar, District-Ajmer, State- Rajasthan, Pincode"}, {"name": "Shri Krishna Enterprises (Jaipur)", "pincode": "302016", "mobile": "9413901504", "address": "B-3/107, Vijay Luxmi Tower, Cantrail  Spine, Vidhyadhar Nagar, District- Jaipur, State-Rajasthan, Pincode"}, {"name": "Shubham Agro Seeds (Marwa)", "pincode": "303348", "mobile": "9602410037", "address": "Shop No. 6, Bus Stand Marwa, Tehsil- Dudu, District- Jaipur"}, {"name": "Sonal Krishi Seva Kendra (Banswara)", "pincode": "327001", "mobile": "9166949403", "address": "Pratap Circle, Shiv Colony, Banswara, Rajasthan"}, {"name": "Tak Traders (Beejwad)", "pincode": "304507", "mobile": "8003009730", "address": "Vill. Beejwad, Tehsil- Deoli, District- Tonk, State-Rajasthan, Pincode"}, {"name": "Veer Teja Agro (Sahnali Chhoti)", "pincode": "331022", "mobile": "8005604268", "address": "Shop No.20, Main Bus Stand, Sahnali Chhoti, District- Churu, State- Rajasthan, Pincode"}, {"name": "Veni Gopal Trading Company (Bahrawada Khurd)", "pincode": "322001", "mobile": "9462119377", "address": "Main Road, Shop No- 10, Bahrawada Khurd, Tehsil-Khandar, District- Sawai Madhopur, State- Rajasthan, Pincode"}, {"name": "Vinayak Provision Khad Beej Bhandar (Nayan)", "pincode": "303601", "mobile": "8233216670", "address": "Bus Stand, Nayan, Tehsil- Shahpura, District- Jaipur, State- Rajasthan, Pincode"}, {"name": "Vinod Agriculture (Rajgarh)", "pincode": "331023", "mobile": "9982487521", "address": "Shop No. 26 , Krishi Upaj Mandi Samiti, Rajgarh, District- Churu, State- Rajasthan, Pincode"}, {"name": "Kisan Krishi Seva Kendra (Tirwa)", "pincode": "209732", "mobile": "9651803540", "address": "Vill. Tirwa, District- Kannauj, State-Uttar Pradesh, Pincode"}, {"name": "Krishna Beej Agency (Uttripura)", "pincode": "209210", "mobile": "9450605816", "address": "Chauhan Market, Station Road, Uttripura, District- Kanpur Nagar, State-Uttar Pradesh, Pincode"}, {"name": "New Katiyar Beej Bhandar (Kannauj)", "pincode": "209728", "mobile": "8858886002", "address": "Tirwa Crossing, Saraimira, Kannauj, District- Kannauj, State-Uttar Pradesh, Pincode"}, {"name": "New Shri Ram Beej Agency (Chhibramau)", "pincode": "209721", "mobile": "7398751291", "address": "Saurikh Road, Chhibramau, District- Kannauj, State-Uttar Pradesh, Pincode"}, {"name": "Prachi Beej Bhandar (Hansapur)", "pincode": "209732", "mobile": "9839411931", "address": "Vill. Hansapur, Tehsil-Tirwa, District- Kannauj, State-Uttar Pradesh, Pincode"}, {"name": "Thakur Sahab Kisan Sewa Kendra (Gazipur)", "pincode": "212621", "mobile": "6387972604", "address": "Bahuwa Road, Gazipur, Next to Jio Office, District- Fatehpur, State-Uttar Pradesh, Pincode"}, {"name": "Amrat Agro Center (Thariyaw)", "pincode": "212622", "mobile": "9956321339", "address": "H.No.304, Bahrampur, Sakhiyanwa, Fatehpur (Uttar Pradesh)"}, {"name": "Maurya Beej Bhandar (Bidhnoo)", "pincode": "209214", "mobile": "9935040994", "address": "Araji No. 780, A1, Hamidpur Road, Bidhnoo, Dist. Kanpur Nagar, (Uttar Pradesh) Pin Code"}, {"name": "Neha Seeds Trading Company (Pawa)", "pincode": "209801", "mobile": "8756015194", "address": "Vill- Pawa, Tehsil- Safipur, District- Unnao, State-Uttar Pradesh, Pincode"}, {"name": "New Maya Beej Bhandar (Fatehpur)", "pincode": "212601", "mobile": "9335330723", "address": "Lucknow Bypass Chauraha, District- Fatehpur, Uttar Pradesh"}, {"name": "Tiwari Beej Bhandar (Jhinjhak)", "pincode": "209302", "mobile": "9839945215", "address": "Main Road Jhinjhak Tehsil- Derapur, District- Kanpur Dehat, State-Uttar Pradesh , Pincode"}, {"name": "Upkar Beej Bhandar (Kanpur)", "pincode": "208001", "mobile": "9839043994", "address": "75/232, Subzi Mandi, (Uttar Pradesh), Kanpur, Uttar Pradesh"}, {"name": "Bala Ji Trading Co. (Talheri Gujran)", "pincode": "133203", "mobile": "9992719735", "address": "Vill. Talheri Gujran, Tehsil-  Barara, District- Ambala, State- Haryana, Pincode"}, {"name": "Bala Sundri Trading Company (Jundla)", "pincode": "132001", "mobile": "9354499633", "address": "Vill. Jundla, Assandh Road, Near P.N.Bank, District- Karnal, State- Haryana, Pinode"}, {"name": "Bhola Shankar Trad. Com. Fertilizer (Badona Khurd )", "pincode": "134204", "mobile": "9467081470", "address": "Village- Badona Khurd, Tehsil- Raipur Rani, District- Panchkula, State-Haryana, Pincode"}, {"name": "Deepak Traders (Shahabad Markanda)", "pincode": "136135", "mobile": "9416872591", "address": "Gur Mandi, Shahabad Markanda, District- Kurukshetra, State- Haryana, Pincode"}, {"name": "Dhanoura Seeds Store (Ladwa)", "pincode": "136132", "mobile": "9467580630", "address": "S.K. Road, Near Devi Mandir, Ladwa (Kurukshetra)"}, {"name": "Duli Chand Mohinder Kumar (Gharaunda)", "pincode": "132114", "mobile": "8708625704", "address": "Village: Gharaunda, District- Karnal, State- Haryana, Pincode"}, {"name": "Gupta Trading Co. (Nilokheri)", "pincode": "132117", "mobile": "8398881515", "address": "Village- Nilokheri, District-Karnal, State- Haryana, Pincode"}, {"name": "Hindustan Seeds & Pesticides (Ismailabad)", "pincode": "136129", "mobile": "9416304547", "address": "Thaska Road, Ismailabad, Kurukshetra (Haryana)"}, {"name": "Hindustan Trading Company (Panipat)", "pincode": "132103", "mobile": "9812127609", "address": "Shop No-107, New Sabzi Mandi Panipat, District- Panipat, State- Haryana, Pincode"}, {"name": "India Seeds and Pesticides (Karnal)", "pincode": "132001", "mobile": "9215622803", "address": "Old Sabji Mandi Gate, Karnal, District- Karnal, State- Haryana, Pincode"}, {"name": "Indian Sales Corporation (Indri)", "pincode": "132041", "mobile": "9053600000", "address": "Shop No. 103, New Anaj Mandi, Indri, District- Karnal, State- Haryana, Pincode"}, {"name": "Jai Bharat Seed Store (Karnal)", "pincode": "132001", "mobile": "9050705010", "address": "#7, Old Sabji Mandi, Karnal (Haryana)"}, {"name": "Narender Seed Company (Madlauda)", "pincode": "132113", "mobile": "9466344401", "address": "Booth No- 36, New Grain Market, VPO- Madlauda, District- Panipat, State- Haryana Pin Code"}, {"name": "New Guru Nanak Trading Co. (Ladwa)", "pincode": "136132", "mobile": "9812331906", "address": "Opp. PNB, Radour Road, Ladwa, Kurukshetra (Haryana)"}, {"name": "Om Seed Store (Yamunanagar)", "pincode": "135001", "mobile": "8307745683", "address": "Mahaveer Chowk , Yamunanagar, District- Yamunanagar, State- Haryana, Pincode"}, {"name": "Saini Beej Bhandar (Damla)", "pincode": "135001", "mobile": "9466326578", "address": "Radour Road, Village - Damla, Tehsil- Radour, District-Yamunanagar, State- Haryana, Pincode"}, {"name": "Sharma Trading Co. (Paharpur)", "pincode": "136033", "mobile": "9813294777", "address": "Village- Paharpur, Tehsil- Siwan, District- Kaithal, State- Haryana, Pincode"}, {"name": "Shree Balaji Khal Feed & Cement Store(Chhachhrouli)", "pincode": "135103", "mobile": "8398804777", "address": "Deep Market, Near Punjab National Bank, Chhachhrouli, District- Yamunanagar, State- Haryana, Pincode"}, {"name": "Shri Maha Luxmi Pesticides & Seed Store (Gheer)", "pincode": "132001", "mobile": "9354198826", "address": "Near Bus Stand Main Road Gheer, District-Karnal, State - Haryana, Pincode"}, {"name": "Alok Khad Bhandar (Kanina)", "pincode": "123027", "mobile": "9416089660", "address": "Anaj Mandi, Kanina, District- Mahendragarh, State- Haryana, Pincode"}, {"name": "Gupta Traders (Satnali)", "pincode": "123024", "mobile": "9416370948", "address": "Railway Station Road,Near Dharamshala,Main Market, Satnali,Mahendragarh"}, {"name": "Narsingh Dass Bajrang Lal (Narnaul)", "pincode": "123001", "mobile": "9416064128", "address": "New Mandi, Narnaul, Mahendragarh, (Haryana)"}, {"name": "New Krishna Bij Bhandar (Mahendergarh)", "pincode": "123029", "mobile": "9315832131", "address": "Railway Road, Mahendergarh"}, {"name": "Pawan Khad Bhandar (Kosli)", "pincode": "123302", "mobile": "8053106161", "address": "Near Co-Op Bank, Kosli (Rewari)"}, {"name": "Rakesh & Company (Ateli Mandi)", "pincode": "123021", "mobile": "9416417537", "address": "Rewari Road, Ateli Mandi"}, {"name": "Shree Yadav Beej Bhandar (Kanina)", "pincode": "123027", "mobile": "9416348919", "address": "Mahendragarh Road, Kanina (Mahendragarh)"}, {"name": "Shri Khatu Shyam Beej Bhandar (Nichi Bhandor)", "pincode": "123029", "mobile": "7027030666", "address": "Nichi Bandor , Mahendragarh, Haryana"}, {"name": "Bansal Pesticides (Cheema)", "pincode": "148028", "mobile": "9872729673", "address": "Sunam Road, Cheema, District- Sangrur, State- Punjab, Pincode"}, {"name": "DNB Punjab Pesticides (Kauhrian)", "pincode": "148030", "mobile": "9463129494", "address": "Near Bus Stand V.P.O Kauhrian, Tehsil- Sunam, District- Sangrur, State- Punjab, Pincode"}, {"name": "Dhan Dhan Baba Bhane Wale Pesticides (Longowal)", "pincode": "148106", "mobile": "8288988830", "address": "Near Drain Da Pull, Sangrur Road, Logowal, District- Sangrur, State- Punjab, Pincode"}, {"name": "Genius Farmer Producer Company Limited (Lehragaga)", "pincode": "148031", "mobile": "9876112133", "address": "Rajbaha Road, Near Stadium, Lehragaga, District-Sangrur, State- Punjab, Pincode"}, {"name": "Jagdish Oil Company (Tapa)", "pincode": "148108", "mobile": "9872781910", "address": "Old Grain Market, Tapa-148108 (Barnala) Pb."}, {"name": "K.K. Goyal & Co. (Budhlada)", "pincode": "151502", "mobile": "9815095111", "address": "Pnb Road, Budhlada, District - Mansa, State- Punjab, Pincode"}, {"name": "Khalsa Pesticides (Bareta)", "pincode": "151501", "mobile": "9465294376", "address": "Near Krishna Mandir, Bareta, Budhlada, Mansa (Punjab)"}, {"name": "Kissan Beej Bhandar (Tapa)", "pincode": "148108", "mobile": "9417773557", "address": "Near Bus Stand, Tajoke Road, Tapa, Barnala (Punjab)"}, {"name": "Malwa Beej Store (Boha)", "pincode": "151503", "mobile": "9876189086", "address": "Ratia Road Boha, Tehsil- Budhlada, District- Mansa, State- Punjab, Pincode"}, {"name": "Malwa Kheti Store (Budhlada)", "pincode": "151502", "mobile": "9417476312", "address": "Bus Stand Road , Near Guru Nanak College, Budhlada, District- Mansa, State- Punjab, Pincode"}, {"name": "Moom Pesticides (Moom)", "pincode": "148100", "mobile": "9915396031", "address": "VPO. Moom, District- Barnala, State- Punjab, Pincode"}, {"name": "Nav Durga Trading Company (Mansa)", "pincode": "151505", "mobile": "9872416172", "address": "Shop No. 83,  Opp. Near Ram Natic Club, Mansa, District- Mansa, State- Punjab, Pincode"}, {"name": "New Kissan Sewa Center (Bareh)", "pincode": "151502", "mobile": "9815502811", "address": "OPP. Guru Ghar, New Bus Stand. Bareh, Tehsil- Budhlada, District- Mansa, State- Punjab, Pincode"}, {"name": "New Kissan Sewa Centre (Lehragaga)", "pincode": "148031", "mobile": "9815386977", "address": "Anaj Mandi, Lehragaga, Sangrur, Punjab"}, {"name": "Singla Fertilizers (Mansa)", "pincode": "151505", "mobile": "9815566365", "address": "#221, Old Grain Market, Mansa (Punjab)"}, {"name": "Vishal Trading Co. (Lehragaga)", "pincode": "148033", "mobile": "8528687000", "address": "Anaj Mandi, Lehragaga, Sangrur (Punjab)"}, {"name": "Jai Ram Khad Beej Bhandar (Farmana)", "pincode": "124112", "mobile": "8307996245", "address": "Shop No.02,Near Nindana Road,Farmana Khas,Rohtak"}, {"name": "Lakshdeep Khad Beej Bhandar (Dhanana)", "pincode": "127031", "mobile": "7015603078", "address": "Near Govt. Girl School, Dhanana, Bhiwani (Haryana)"}, {"name": "New Kissan Sewa Center (Bawani Khera)", "pincode": "127032", "mobile": "9813938626", "address": "Shop No.35, Grain Market, Tehsil- Bawani Khera, District- Bhiwani, State- Haryana, Pincode"}, {"name": "Rajender Singh and Company (Meham)", "pincode": "124112", "mobile": "9812774051", "address": "Bhiwani Road,Opp.Civil Hoshpital, Meham , Rohtak, Haryana"}, {"name": "Ravinder Agriculture Store (Chang)", "pincode": "127027", "mobile": "9992976006", "address": "VPO. Chang, Near Kali Mata Mandir,Main Bus. Road, District- Bhiwani, State- Haryana"}, {"name": "Shri Ram Beej Bhandar (Meham)", "pincode": "124112", "mobile": "9992240924", "address": "Near New Bus Stand, Meham, District- Rohtak, State- Haryana, Pincode"}, {"name": "Brar Pesticides (Bhalaiana)", "pincode": "152101", "mobile": "9988910053", "address": "Near Govt sen Sec School. Vill. Bhalaiana, Gidderbaha, Muktsar (Punjab)"}, {"name": "Divansh Hybrid Seeds Agency (Sri Muktsar Sahib)", "pincode": "152026", "mobile": "9855460328", "address": "S.C.F-52, New Grain Market, Sri Muktsar Sahib, Punjab"}, {"name": "Guru Nanak Feed & Seed Store (Golu Ka Mour)", "pincode": "152022", "mobile": "9872065135", "address": "Golu Ke Mour,Guruharsahai, Ferozpur,Punjab"}, {"name": "Kamal Kheti Store (Manna Singh Wala)", "pincode": "142052", "mobile": "9915664614", "address": "Manna Singh Wala, Ferozepur,Punjab"}, {"name": "Kissan Seed Store (Jalalabad)", "pincode": "152024", "mobile": "9888555275", "address": "New Grain Market, Jalalabad, District - Fazilka, State- Punjab, Pincode"}, {"name": "New Bansal Seed Store (Mandi Bariwala)", "pincode": "152026", "mobile": "9855249516", "address": "Marar Kalan, Mandi Bariwala, Sri Muktsar Sahib (Punjab)"}, {"name": "PR Beej Store (Jandwala Bhimeshah)", "pincode": "152124", "mobile": "9872242749", "address": "Jandwala Bhimeshah, Fazilka (Punjab)"}, {"name": "Pee Jay Seed Store (Kotkapura)", "pincode": "151203", "mobile": "9915631245", "address": "Old Grain Market, Shop No-2, Kotkapura, District- Faridkot, State- Punjab, Pincode"}, {"name": "Punjab Pesticides (Kotkapura)", "pincode": "151204", "mobile": "9356870947", "address": "Shop No.11, Old Grain Market, Kotkapura, District- Faridkot, State- Punjab, Pincode"}, {"name": "S K Pesticides (Muktsar)", "pincode": "152026", "mobile": "9914700187", "address": "Shop No. 84-A, New Grain Market, Muktsar, District-Muktsar, State- Punjab, Pincode"}, {"name": "Singla Agencies (Jaitu)", "pincode": "151202", "mobile": "8307600009", "address": "Bajakhana Road, Jaitu, Faridkot (Punjab)"}, {"name": "Adarsh Krishi Kendra (Itarsi)", "pincode": "461111", "mobile": "9827542435", "address": "13th Line, Near Talab Mohalla, Itarsi, Dist. Narmadapuram, (M.P) Pin Code"}, {"name": "Ajay Krishi Sewa Kendra (Amon)", "pincode": "466448", "mobile": "7470829869", "address": "Main Road Amon, Tehsil- Budni, District- Sehore, State-Madhya Pradesh, Pincode"}, {"name": "Aruja Agro (Chhindwara)", "pincode": "480001", "mobile": "7000532175", "address": "New Subzi Mandi, Guraiya Road, Ev Master India, Sarra, Chhindwara, Madhya Pradesh"}, {"name": "Dhruvil Trading (Shuajlpur)", "pincode": "465333", "mobile": "9926491009", "address": "Lohiya Marg, Shuajlpur, State-Madhya Pradesh, Pincode"}, {"name": "Ganesh Agro (Khandwa)", "pincode": "450001", "mobile": "9425950550", "address": "Bhawani Mata Road, Khandwa, Lakkad Bazar, District -Khandwa, State- Madhya Pradesh Pincode"}, {"name": "Janta Beej Bhandar (Vidisha)", "pincode": "464001", "mobile": "9406910041", "address": "1, Marketing Society Gali, Vidisha, District-Vidisha, State-Madhya Pradesh, Pincode"}, {"name": "Jaya Kisan Krishi Seva Kendra (Shahpur)", "pincode": "460440", "mobile": "9893874106", "address": "Tehsil Road Shahpur, District-Betul, State-Madhya Pradesh, Pincode"}, {"name": "Kisan Agri Mart (Dhamnod)", "pincode": "454552", "mobile": "9630529696", "address": "Old A.B. Road Dhamnod, Tehsil- Dharampuri, District- Dhar, State-Madhya Pradesh, Pincode"}, {"name": "Krishak Krishi Sewa Kenddra (Ratua)", "pincode": "462031", "mobile": "9691585405", "address": "Village Ratua, Berasia Road, District- Bhopal, State-Madhya Pradesh, Pincode"}, {"name": "Kshipra Krishi Sewa Kendra (Ujjain)", "pincode": "456006", "mobile": "9926914720", "address": "01, Ankpat Marg,  Ujjain, District- Ujjain, State- Madhya Pradesh, Pincode"}, {"name": "Kundan Trading Company (Guna)", "pincode": "473001", "mobile": "9425131157", "address": "Umri Road, Infront of Central School, Nanakhedi Mandi, Guna, District- Guna, State-Madhya Pradesh"}, {"name": "Laxmi Enterprises (Segaon)", "pincode": "451442", "mobile": "7247671950", "address": "Golwadi Road, Segaon, District- Khargone, State-Madhya Pradesh, Pincode"}, {"name": "Maadhav Krisi Sewa Kendra (Gourjhamar)", "pincode": "470223", "mobile": "9893458044", "address": "Main Road, Gourjhamar, Tehsil- Deori, District-Sagar, State-Madhya Pradesh, Pincode"}, {"name": "Manthan Traders (Nasrullaganj)", "pincode": "466331", "mobile": "9926377887", "address": "Ward 15, In Front of New Bus Stand Nasrullaganj, District- Sehore, State- Madhya Pradesh, Pincode"}, {"name": "Pragati Beej Bhandar (Chopna)", "pincode": "460440", "mobile": "9981283081", "address": "Main Road, Chopna Tehsil Ghodadongri, Distt. Betul  Madhyya Pradesh"}, {"name": "Pratap Krishi Kendra (Jabalpur)", "pincode": "482004", "mobile": "9300882007", "address": "Nai Basti, Amkhera Road, Gohalpur, District- Jabalpur, State-Madhya Pradesh, Pincode"}, {"name": "Rahul Beej Bhandar (Betul)", "pincode": "460001", "mobile": "9424422953", "address": "Kothi Bazar Betul, District-Betul, State-Madhya Pradesh Pincode"}, {"name": "Riddhi Siddhi Traders (Sehore)", "pincode": "466001", "mobile": "9589050552", "address": "01, Balaji Colony, Police Line, Jimoniya Road,Sehore, (MP) Pin Code"}, {"name": "Sagar Krishi Sewa Kendra (Sagar)", "pincode": "470002", "mobile": "8103903288", "address": "Ambedkar Ward,  Dharmashri, Sagar, District- Sagar, State-Madhya Pradesh, Pincode"}, {"name": "Shree Balaji Krashi Kendra (Sausar)", "pincode": "480106", "mobile": "9329214005", "address": "Berdi Road, Sausar, District-Chhindwara, State-Madhya Pradesh, Pincode"}, {"name": "Shri Govind Enterprises (Seoni)", "pincode": "480661", "mobile": "7987972141", "address": "Chhindwara Road , Near Maharaja Transport, District-Seoni, State-Madhya Pradesh , Pincode"}, {"name": "Shrinath Krishi Sadhan Kendra (Khargone)", "pincode": "451001", "mobile": "9425415969", "address": "23. Khandwa Road , Near Bus Stand, Khargone, District- Khargone, State-Madhya Pradesh, Pincode"}, {"name": "Shukla Krashi Sewa Kendra (Ratanpur)", "pincode": "460330", "mobile": "9377662511", "address": "Vill. Ratanpur, Tehsil-Bhimpur, District- Betul, State- Madhya Pradesh, Pincode"}, {"name": "Shyam Krishi Sewa Kendra (Deori)", "pincode": "470226", "mobile": "9993516954", "address": "Main Road Chhir, Tehsil- Deori, District- Sagar, State- Madhya Pradesh, Pincode"}, {"name": "Sisodhiya Krishi Vikas Kendra (Khajuri Sadak)", "pincode": "462030", "mobile": "9926969369", "address": "Main Road , Khajuri Sadak, Tehsil-Huzur, District- Bhopal"}, {"name": "Taj Krishi Sewa Kendra (Prabhat)", "pincode": "460001", "mobile": "9893920243", "address": "Shop No-3 Bus Stand Complex Prabhat, Tehsil- Multai, District- Betul, State- Madhya Pradesh, Pincode"}, {"name": "Tulsi Beej Bhandar (Barwaha)", "pincode": "451115", "mobile": "9826505632", "address": "116/1/2, Mahavir Colony, Maruti Market, District- Khargone, State-Madhya Pradesh, Pincode"}, {"name": "Uttamshree Krishi Seva Kendra & Hardware (Pansemal)", "pincode": "451770", "mobile": "9201991955", "address": "Jalgone Road, Pansemal, District- Barwani, State-Madhya Pradesh, Pincode"}, {"name": "Vindhya Steel Centre (Satna)", "pincode": "485001", "mobile": "9731953333", "address": "Plot No. 6/70, Transport Nagar,  Satna, Tehsil-Raghurajnagar, District- Satna, State-Madhya Pradesh, Pincode"}, {"name": "Virat Krishi Seva Kendra (Kukshi)", "pincode": "454331", "mobile": "9981939194", "address": "22, Hospital Complex, Alirajpur Road, Kukshi, District- Dhar, State- Madhya Pradesh, Pincode"}, {"name": "Ajay & Company (Hassanpur)", "pincode": "121107", "mobile": "7015161419", "address": "Sahnoli Road, Village- Hassanpur, District- Palwal, State- Haryana, Pincode"}, {"name": "Arun Beej Bhandar (Bhiduki)", "pincode": "121107", "mobile": "9991369958", "address": "Near Sidh Baba Rice Mill, Banswan Road, Bhiduki, Hodal, Palwal (Haryana)"}, {"name": "Choudhary Beej Bhandar (Jaindapur)", "pincode": "121102", "mobile": "9813764118", "address": "01, Palwal Sohna Road, Jaindapur, Palwal (Haryana)"}, {"name": "Ganpati Beej Bhandar (Hodal)", "pincode": "121106", "mobile": "9416390480", "address": "Opp. Rest House, Old GT Road, Hodal (Palwal)"}, {"name": "Garg Khad Beej Avem Davaiyan (Ferozpur Jhirka)", "pincode": "122104", "mobile": "9802920693", "address": "Biwan Road, Ferozpur Jhirka, District- Nuh, State - Haryana, Pincode"}, {"name": "Garg Khad Bhandar (Manjhawali)", "pincode": "121101", "mobile": "9990201684", "address": "Vill. Manjhawali, Near Bus Stand, Tehsil- Ballabgarh, District- Faridabad, State- Haryana, Pincode"}, {"name": "Goyal Khad Beej Bhandar (Sohna)", "pincode": "122103", "mobile": "8683069926", "address": "Palwal Road Ward No 5, Sohna (Gurgaon)"}, {"name": "Harsh Khad Beej Bhandar (Bagpur)", "pincode": "121102", "mobile": "9991530356", "address": "Village Bagpur,Palwal"}, {"name": "Jai Kishan Beej Bhandar (Mohna)", "pincode": "121004", "mobile": "8708036283", "address": "Baghpur Road, Bus Stand, Near Anaj Mandi Mohna, Tehsil-Ballabgarh, District- Faridabad, State- Haryana, Pincode"}, {"name": "Mohan Khaad Beej Bhandar (Atali-Faridabad)", "pincode": "121004", "mobile": "9310557371", "address": "Near Gas Agency, 02, Mohna Road, Atali, Faridabad (Haryana)"}, {"name": "Pandit Ji Khad Beej Bhandar (Sikrona)", "pincode": "121004", "mobile": "9818421950", "address": "Ground Floor, Village Sikrona , Old Sohna Road, Ballabgarh, Faridabad ( Haryana)"}, {"name": "RK Seeds & Pesticides (Palwal)", "pincode": "121102", "mobile": "8950221835", "address": "Booth No.164, Near Railway Station, New Grain Market, Palwal (Haryana)"}, {"name": "Rattan Khad Bhandar (Mandkola)", "pincode": "121103", "mobile": "9812774084", "address": "Vpo- Mandkola, Hathin, Palwal (Haryana)"}, {"name": "Saini Beej Bhandar (Sarmathla)", "pincode": "122103", "mobile": "9992380805", "address": "47, Saini Market, Old Sohna Road, Sarmathla, Gurgaon (Haryana)"}, {"name": "Saini Khad Beej Bhandar (Seoli)", "pincode": "121105", "mobile": "9466526846", "address": "Village- Seoli, Palwal, Haryana"}, {"name": "Subhash Beej Bhandar (Ballabhgarh)", "pincode": "121004", "mobile": "9811379990", "address": "106 A, Gully 3, Ballabgarh, District- Faridabad, State- Haryana, Pincode"}, {"name": "Subhash Fertilizer (Ballabgarh)", "pincode": "121004", "mobile": "9811314435", "address": "Mohna Road, Ballabgarh, Faridabad (Haryana)"}, {"name": "Ankit Khad Beej Bhandar (Kosli)", "pincode": "123302", "mobile": "9467461024", "address": "V.P.O -Kosli, Near Govt School, Tehsil Road, District- Rewari, State- Haryana, Pincode"}, {"name": "Durga Trading Company (Rewari)", "pincode": "123401", "mobile": "9812454503", "address": "Katla Bazar, Rewari (123401), Haryana"}, {"name": "Chandra Agriculture Store (Rohtak)", "pincode": "124001", "mobile": "8053368084", "address": "#126, Circular Road, New Grain Market, Rohtak (Haryana)"}, {"name": "Durga Khad Bhandar (Israna)", "pincode": "132107", "mobile": "8059000092", "address": "Mandi Moad Ishrana, Distt Panipat, State :- Haryana  Pincode"}, {"name": "Guru Kirpa Trading Co. (Rohtak)", "pincode": "124001", "mobile": "9416333700", "address": "Under Elevated Bridge, Piller No 69, Hissar, Road, Near Old Bus Stand , Rohtak, District- Rohtak, Pincode"}, {"name": "Kanhaiya Khad Beej Bhandar (Puthi)", "pincode": "131301", "mobile": "8930320235", "address": "Near Shiv Mandir, And Govt School, Puthi, District- Sonipat, State- Haryana, Pincode"}, {"name": "Nanda Khad Beej Bhandar (Rohtak)", "pincode": "124001", "mobile": "7404115563", "address": "Jind Bye Pass, Rohtak, District- Rohtak, State- Haryana, Pincode"}, {"name": "Ramesh Khad Beej Bhandar (Gohana)", "pincode": "131301", "mobile": "9518609800", "address": "Jind Road, New Anaj Mandi, Shop No.22, Gohana, District- Sonipat, State- Haryana, Pincode"}, {"name": "Rose Khad Beej Bhandar (Kalanaur)", "pincode": "124113", "mobile": "7988817966", "address": "Shop No- 72, New Anaj Mandi, Kalanaur, District- Rohtak, State- Haryana, Pincode"}, {"name": "Shiv Shakti Fertilizer (Madina)", "pincode": "124111", "mobile": "9416512000", "address": "Bus Stand, Madina, Tehsil- Meham, District- Rohtak, State- Haryana, Pincode"}, {"name": "Shri Shyam Trading Company (Lakhan Majra)", "pincode": "124514", "mobile": "7015283606", "address": "Near Police Station, Ground Floor, 1302, Jind- Rohtak Road, Lakhan Majra, Rohtak (Haryana)"}, {"name": "Bhagwati Pesticides (Nanauta)", "pincode": "247452", "mobile": "8802608095", "address": "Near O B C Bank Mohalla Shekhjadgan, Deoband Road, Nanauta, Tehsil- Rampur, District- Saharanpur, State-Uttar Pradesh, Pincode"}, {"name": "Bharat Beej Store (Deoband)", "pincode": "247452", "mobile": "9456836489", "address": "#03, Vill. Maheshpur, Shimlana Road, Badgao, Saharanpur (Uttar Pradesh)"}, {"name": "Ganpati Trading Co. (Shimbhaoli)", "pincode": "245207", "mobile": "7351669000", "address": "Saifi Colony, Mohammadpur Khudalia, Shimbhaoli, Hapur (Uttar Pradesh)"}, {"name": "Khurana Khad Bhandar (Ambehta Peer)", "pincode": "247340", "mobile": "9557535328", "address": "Near Kirtan Bhawan , Mohalla Gujran, Ambehta, District-  Saharanpur, State-Uttar Pradesh, Pincode"}, {"name": "Kisan Aggrijunction (Khera Mugal)", "pincode": "247551", "mobile": "7500525120", "address": "Petrol Pump, Khera Mugal, Deoband, Saharanpur (Uttar Pradesh)"}, {"name": "Krishi Kisan Sewa Kendra (Lahboli)", "pincode": "247670", "mobile": "9719492821", "address": "Vill. Lahboli, Pargana Manglore, Roorkee, Haridwar (Uttarakhand)"}, {"name": "Krishna Khad Bhandar (Talheri)", "pincode": "247551", "mobile": "8279633315", "address": "Talheri Bujurg, Block Nagal, Devband, Saharanpur (Uttar Pradesh)"}, {"name": "Mittal Pestiside (Nanauta)", "pincode": "247452", "mobile": "9411272609", "address": "Neeraj Mittal Near Old Paniki Tanki ,Kanoon Goyan, Saharanpur (UP)"}, {"name": "S.K. Enterprises (Roorkee)", "pincode": "247667", "mobile": "9851005101", "address": "00, Village Talhedi, Roorkee, District- Haridwar, State- Uttarakhand, Pincode"}, {"name": "Sanjay Traders (Gangoh)", "pincode": "247341", "mobile": "9045651501", "address": "Shiv Chowk, Gangoh, Tehsil- Nakur, District- Saharanpur, State-Uttar Pradesh, Pincode"}, {"name": "Vijay Beej Bhandar (Biharigarh)", "pincode": "247662", "mobile": "9719654259", "address": "Baggu Wala Road,Biharigarh,Chutmalpu,Saharnpur"}, {"name": "Bhardwaj Pesticide (Sarsawa)", "pincode": "247232", "mobile": "9758974770", "address": "D C Jain Market, Ambala Road, Sarsawa, Saharanpur (Uttar Pradesh)"}, {"name": "Hari Om Beej Bhandar (Saharanpur)", "pincode": "247001", "mobile": "7017069423", "address": "Rakesh Cenema Road, Saharanpur, Tehsil- Nakur, District- Saharanpur, State-Uttar Pradesh, Pincode"}, {"name": "Mahadev New Kisan Agrijunction One Stop Shop (Rampur Maniharan)", "pincode": "247451", "mobile": "9675343744", "address": "H.No.346, Mohalla Ikram, Rampur Maniharan, Saharanpur (Uttar Pradesh)"}, {"name": "Agri Junction One Stop Shop (Jalalabad)", "pincode": "247777", "mobile": "9149069325", "address": "Jalalabad, Tehsil- Thana Bhawan, District-Shamli, State-Uttar Pradesh, Pincode"}, {"name": "Farm Villa (Titarwara)", "pincode": "247774", "mobile": "9760029800", "address": "Main Road, Titarwara,Tehsil-Kairana, District-Shamli, State-Uttar Pradesh , Pincode"}, {"name": "Goel Khad Bhandar (Thana Bhawan)", "pincode": "247777", "mobile": "9319974907", "address": "Charthawal Road, Thana Bhawan, Shamali (Uttar Pradesh)"}, {"name": "Punjab Traders (Baraut)", "pincode": "250611", "mobile": "9837090448", "address": "Near Police Choki Baraut Disst Baghpat, Uttar Pradesh Pincode"}, {"name": "Ram Traders (Kandhla)", "pincode": "247776", "mobile": "9761170571", "address": "Near TVS Agency, Delhi Road, Raj Gardden, Town-, Kandhla, District- Shamli, State-Uttar Pradesh, Pincode"}, {"name": "Yash Pesticide (Garhi Hasanpur)", "pincode": "247778", "mobile": "6397426635", "address": "H.No.100, Garhi Hasanpur, Unn, Shamli (Uttar Pradesh)"}, {"name": "Balaji Pesticides (Rupawas)", "pincode": "125055", "mobile": "9813510590", "address": "VPO- Rupawas, Sirsa- 125055, Haryana"}, {"name": "Battan Seed & Pesticide (Peer Khera)", "pincode": "125076", "mobile": "9050888895", "address": "Jodhpuria Road, Near HR Agriculture Works, Peer Khera, Rania, Sirsa (Haryana)"}, {"name": "Bhuratwala Khad Pesticides and Beej Bhandar (Bhuratwala)", "pincode": "125055", "mobile": "8930734626", "address": "Flat No.1, Chilkani Dhab Road, Bhurat Wala, Sirsa (Haryana)"}, {"name": "Ganpati Beej Bhandar (Ellenabad)", "pincode": "125102", "mobile": "9315392150", "address": "#27A, New Anaj Mandi, Ellenabad, Sirsa (Haryana)"}, {"name": "Gupta Beej Bhandar (Goriwala)", "pincode": "125103", "mobile": "7015844646", "address": "Sh.No.01, Goriwala, Sangaria Road, Mandi Dabwali, Sirsa (Haryana)"}, {"name": "Kiroriwal Beej Bhandar (Sadewala)", "pincode": "125076", "mobile": "9729432164", "address": "khewat no 436 murabba no 103, shop no 1,2, killa No 16 ,near krishna Mandir,VPO Sadewala"}, {"name": "Kissan Pesticides & Beej Bhandar (Kalanwali)", "pincode": "125201", "mobile": "9467302203", "address": "Shop No.29 Anaj Mandi Kalanwali, Tehsil- Kalanwali, Dist.- Sirsa"}, {"name": "Malwa Agri Mart (Rania)", "pincode": "125076", "mobile": "9728700063", "address": "#0, Near Rice Mill, Opp. Anaj Mandi, Rania, Sirsa (Haryana)"}, {"name": "Murli Trading Company (Dabwali)", "pincode": "125104", "mobile": "7015530436", "address": "#118, Nai Anaj Mandi, Mandi Dabwali (Sirsa)"}, {"name": "Nokhwal Trading Co. (Chaka Bhuna)", "pincode": "125076", "mobile": "8813860009", "address": "Chakkan Road, Bhuna, Rania (Sirsa)"}, {"name": "Pankaj Seeds & Fertilizers (Kharian)", "pincode": "125077", "mobile": "9355010995", "address": "VPO- Kharian, Rania, Sirsa (Haryana)"}, {"name": "Sad Bhawana Beej Farm (Ellenabad)", "pincode": "125102", "mobile": "9812188876", "address": "#73A,New Anaj Mandi, Ellenabad (Sirsa)"}, {"name": "Saharan Pesticides (Ludesar)", "pincode": "125110", "mobile": "9813063037", "address": "Opp. Govt. School, Vill. Ludesar, Sirsa (Haryana)"}, {"name": "Shri Shyam Pesticides & Beej Bhandar (Tajia Khara)", "pincode": "125055", "mobile": "9812802429", "address": "VPO-Tajia Khara, Sirsa (Haryana)"}, {"name": "Sidhu Farm Solutions (Kalanwali)", "pincode": "125201", "mobile": "7015315066", "address": "#42-B, New Anaj Mandi, Kalanwali, Sirsa (Haryana)"}, {"name": "Sihag Pesticides (Randhawa)", "pincode": "125055", "mobile": "9466040958", "address": "4#, Vpo- Randhawa, Nathusari Chopta (Sirsa)"}, {"name": "Sirsa Pesticides (Sirsa)", "pincode": "125055", "mobile": "9896211151", "address": "#58-A, Back Side, New Anaj Mandi, Sirsa (Haryana)"}, {"name": "The Kagdana Shri Bala Ji Cooperative (Kagdana)", "pincode": "125055", "mobile": "9466033468", "address": "The Kagdana Shri Bala Ji Cooperative Multi, Purpose Society,VPO Kagdana,Sirsa"}, {"name": "Yogita Trading Company (Farwain Kalan)", "pincode": "125060", "mobile": "8529526218", "address": "Barnala Road, Near Induslnd Bank, Farwain Kalan, Sirsa (Haryana)"}, {"name": "Zimidara Pesticides (Sirsa)", "pincode": "125055", "mobile": "9215955110", "address": "Sh.No.11, Sirsa Club Shop, Janta Bhawan Road, Sirsa (Haryana)"}, {"name": "Agri Junction One Stop Shop (Nawabganj)", "pincode": "262406", "mobile": "9759415888", "address": "Pirthbipur Nawadia, Tehsil- Nawabganj, District- Bareilly, State-Uttar Pradesh, Pincode"}, {"name": "Bhawani Beej Bhandar (Shahjahanpur)", "pincode": "242001", "mobile": "9335511406", "address": "Mohalla Bajeerganj, Shahjahanpur, State-Uttar Pradesh, Pincode"}, {"name": "Farmers Traders (Jaspur)", "pincode": "244712", "mobile": "9870703350", "address": "Kaliwala Road Jaspur,District: Udham Singh Nagar, State-Uttarakhand"}, {"name": "Garg Seeds & Pesticides (Sitarganj)", "pincode": "262405", "mobile": "9837191275", "address": "Village Malpuri, Pilibhit Road, Tehsil Sitarganj, Dist. U.S. Nagar (Uttarakhand)"}, {"name": "Guru Teg Bahadur Pesticides & Fertilizer (Bilaspur)", "pincode": "244921", "mobile": "9837322400", "address": "Begmabad  Bilaspur, Tehsil-Bilaspur, District-Rampur, State-Uttar Pradesh Pincode"}, {"name": "Hari Om Gupta and Sons (Sambhal)", "pincode": "244303", "mobile": "9411909640", "address": "Shri Ram Kunj, Kirshana Market, District-Sambhal, State-Uttar Pradesh, Pincode"}, {"name": "Jai Durga Beej Bhandar (Baheri)", "pincode": "243201", "mobile": "9389443726", "address": "Akabarabad Naher Ke Pass Mudiya Road Baheri, District- Bareilly, State-Uttar Pradesh, Pincode"}, {"name": "Kamal Beej Bhandar (Baheri)", "pincode": "243201", "mobile": "7017527249", "address": "Naintal Road Baheri, Bareilly (UP)"}, {"name": "Kisan Beej Bhandaar (Kichha)", "pincode": "263148", "mobile": "7088759962", "address": "Ward No 4 Bareilly Road,Oppo. Bansal Paint, Kichha-Tehsil, Udham Singh Nagar- District, Uttarakhand- State"}, {"name": "Kissan Seed Store (Rudrapur)", "pincode": "263153", "mobile": "9837791203", "address": "Shivaji Chowk, Galla Mandi Road, Rudrapur, Udham Singh Nagar (Uttarakhand)"}, {"name": "Krishi Kendra Agri Junction One Stop Shop (Seohara)", "pincode": "246746", "mobile": "8218673817", "address": "Manoj Kumar Sharma, Village- Bamnoli, Post-Seohara Dist Bijnor (UP), Uttar Pradesh, Pincode"}, {"name": "Master Beej Bhandar (Pilibhit)", "pincode": "262001", "mobile": "9927507584", "address": "Mandi Samiti,Block Lalauri Khera Pilibhit, District- Pilibhit, State-Uttar Pradesh , Pincode"}, {"name": "New Punjab Beej Bhandar (Powayan)", "pincode": "242401", "mobile": "9793820911", "address": "Nigohi Road, Powayan, District- Shahjahanpur, State-Uttar Pradesh, Pincode"}, {"name": "Saini Khad Bhandar (Rampur)", "pincode": "244901", "mobile": "8909330019", "address": "Near Choki Hazyani Patti Tola, Rampur, District- Rampur, State-Uttar Pradesh, Pincode"}, {"name": "Sanveer Beej Bhandar (Puranpur)", "pincode": "262122", "mobile": "8218447984", "address": "Near Ghungchihai Tiraha, Puranpur, District- Pilibhit, State-Uttar Pradesh, Pincode"}, {"name": "Saroha Trading Company (Najibabad)", "pincode": "246763", "mobile": "8077778304", "address": "O, Tehsil Road Najibabad, District- Bijnor, State-Uttar Pradesh, Pincode"}, {"name": "Shri Organic Krishi Sewa Kendra (Biswan)", "pincode": "261201", "mobile": "9454031578", "address": "Sri Bachchulal Ramdulari Mahavidyalaya, Master Colony, Biswan, District-Sitapur, State-Uttar Pradesh, Pincode"}, {"name": "Tarai Traders (Afzalgarh)", "pincode": "246722", "mobile": "9012700127, 9760979380", "address": "Abdul Razaq Market, Afzalgarh, Bijnor (Uttar Pradesh)"}, {"name": "Tyagi Pesticides (Chandpur)", "pincode": "246725", "mobile": "9837184026", "address": "Pheena Road,Chandpur, District- Bijnor, State-Uttar Pradesh , Pincode"}, {"name": "Umesh Khad Bhandar (Budaun)", "pincode": "243601", "mobile": "8923516592", "address": "Bus Stand, Gulariya, Budaun (Uttar Pradesh)"}, {"name": "Uttaranchal Agro Seeds (Kashipur)", "pincode": "244713", "mobile": "9837776271", "address": "Moradabad Road, Nera Swastik Hospital, Kashipur, District-Udham Singh Nagar, State-Uttarakhand, Pincode"}, {"name": "Verma Krishi Kendra (Tikuniya)", "pincode": "242405", "mobile": "7459831120", "address": "Gola Road Tikuniya, Khutar, Tehsil-Powayan, District- Shahjahanpur, State-Uttar Pradesh , Pincode"}, {"name": "Bahuuddeshiy Agro Sales (Jaitsar)", "pincode": "335702", "mobile": "8104687949", "address": "#14, Dhan Mandi, PNB Bank Road, Opp PNB Bank, Jaitsar, Anoopgarh (Rajasthan)"}, {"name": "Bihani Pesticides (Jaitsar)", "pincode": "335702", "mobile": "9460393257", "address": "#25, W.No16, Gb-A, Dhan Mandi, Jaitsar, Sri Ganganagar (Rajasthan)"}, {"name": "Godara Pesticides (Sri Vijaynagar)", "pincode": "335704", "mobile": "9982039814", "address": "Shop No. 428 Chander Nagar, Opp. New Dhan Mandi, Sri Vjay Nagar, Dist. Sri Ganga Nagar, Rajasthan, Pin Code"}, {"name": "Kisan Pesticide And Seeds (Kaluwala Dhaba)", "pincode": "335051", "mobile": "9587668238", "address": "Kaluwala Dhabha Gram Panchayat 8 STB,0, Tehsil- Sri Vijayanagar, District- Sri Ganganagar, Pincode"}, {"name": "Kisan Sales Corporation (Birmana Barani)", "pincode": "335704", "mobile": "9001283073", "address": "Near PNB Bank, Main Road, Birmana Barani, Sri Ganganagar (Rajasthan)"}, {"name": "Shree Shyam Pesticides (Suratgarh)", "pincode": "335804", "mobile": "8432403960", "address": "Shop No. 70B, New Dhan Mandi, Suratgarh, Distt. Sri Ganganagar, Rajasthan"}, {"name": "Shri Karni Fertilizers (Rangmahal Suratgarh)", "pincode": "335803", "mobile": "9660500510", "address": "Near RMGB Bank, W.No.07, #180, Rampura, Suratgarh Hanumangarh Road, Rangmahal, Pilibanga, Hanumangarh (Rajasthan)"}, {"name": "Shri Shyam Kisan Seva Kender (Badopal)", "pincode": "335804", "mobile": "9309420001", "address": "Ward No-4 Badopal, Birani, Tehsil-Pilibanga, District- Hanumangarh, State- Rajasthan, Pincode"}, {"name": "Shri Shyam Pesticide (Paliwala)", "pincode": "335804", "mobile": "7849840008", "address": "Gram Panchayat Sanghar, 17 S T B, Paliwala, Suratgarh, District- Ganganagar, State- Rajasthan, Pincode"}, {"name": "Sri Balaji Seeds & Fertilizers (Raisinghnagar)", "pincode": "335051", "mobile": "9785425509", "address": "P.No. 150, M-Block, W.No.17, Raisinghnagar, Sri Ganganagar (Rajasthan)"}, {"name": "Thalod Pesticide & Fertilizers (Arjunsar)", "pincode": "334604", "mobile": "7976241015", "address": "Main Market Arjunsar, Lunkaransar, Bikaner (Rajasthan)"}, {"name": "Aggarwal Trading Company (Safidon)", "pincode": "126112", "mobile": "7015917196", "address": "Old Grain Market,Safidon, Jind (Haryana)"}, {"name": "Bajrang Beej Bhandar (Kalayat)", "pincode": "136117", "mobile": "9812790914", "address": "Buth No.64, Anaj Mandi, Kalayat, Kaithal (Haryana)"}, {"name": "Bala Ji Khad Bhandar (Alewa)", "pincode": "126111", "mobile": "9996525028", "address": "Main Bus Stand, Alewa, Jind (Haryana)"}, {"name": "Bala Sundri Trading Co. (Kaithal)", "pincode": "136043", "mobile": "8607000668", "address": "S.C.F No.83, Sbi Road, New Grain Market, District- Kaithal, State- Haryana, Pincode"}, {"name": "Boora Seed House (Kaithal)", "pincode": "136027", "mobile": "7056471905", "address": "New Grain Market, Near Gaushala, Kaithal, District- Kaithal, State- Haryana, Pincode"}, {"name": "Chahal Beej Bhandar (Uchana)", "pincode": "126116", "mobile": "9416701633", "address": "Near Axis Bank Railway Road Uchana, District- Jind, State- Haryana, Pincode"}, {"name": "Goyal Seed & Pesticide (Kaithal)", "pincode": "136027", "mobile": "8708840819", "address": "House No.252/1, 16, Ram Nagar, Kaithal, District- Kaithal, State- Haryana, Pincode"}, {"name": "Hafed Products Bikri Kender (Dhamtan Sahib)", "pincode": "126116", "mobile": "9034494344", "address": "SHOP NO 26 ANAJ MANDI VILLAGE DHANTAN SAHIB, DIST. JIND (HARYANA)"}, {"name": "Haryana Beej Bhandar (Harsola)", "pincode": "136027", "mobile": "9034338111", "address": "Near Drain, Unnamed Road, Prachin Shiv Mandir, Village - Harsola, District- Kaithal"}, {"name": "Jai Baba Trading Co. (Balu)", "pincode": "136027", "mobile": "9991302021", "address": "Rapriya Patti, Village- Ballu, Tehsil- Kalayat, District- Kaithal, State- Haryana, Pincode"}, {"name": "Khatkar Beej Bhandar (Naguran)", "pincode": "126125", "mobile": "9416607656", "address": "Kaithal Road , Naguran (Jind), Naguran , Alwar (Jind)"}, {"name": "Kisan Khad Bhandar (Kaithal)", "pincode": "136024", "mobile": "9416766239", "address": "New Grain Market , Shop No-71  Kaithal, District- Kaithal, State- Haryana,  Pincode"}, {"name": "Kisan Trading Company (Nepewala)", "pincode": "126116", "mobile": "9466822049", "address": "Khata No.16, Khevat No.183, Unnamed Road, Nepewala, Tehsil- Narwana, District- Jind, State- Haryana, Pincode"}, {"name": "Kundu Trading Company (Kailram)", "pincode": "136117", "mobile": "8397939411", "address": "Near Govt School, Kailram, Tehsil-Kalayat, District-Kaithal, State- Haryana, Pincode"}, {"name": "Malik Beej Bhandar (Rohera)", "pincode": "136027", "mobile": "9992762830", "address": "Near Main Bus Stand Main Road, Rohera, District-Kaithal, State- Haryana, Pincode"}, {"name": "Nav Bharat Seeds (Kaithal)", "pincode": "136027", "mobile": "9354280008", "address": "Shopno,77A New Grain Market Kaithal"}, {"name": "Navdeep Beej Bhandar (Garhwali)", "pincode": "126114", "mobile": "9817137038", "address": "#62, Garhwali Branch Post Office, Julana Rajgarh Road, Garhwali, Jind (Haryana)"}, {"name": "New Guru Brahmanand Trading Co (Assandh)", "pincode": "132039", "mobile": "9896003388", "address": "Near New Grain Market Assandh, District- Karnal, State- Haryana, Pincode"}, {"name": "New Shiv Shankar Trading Company (Pundri)", "pincode": "136026", "mobile": "9992376703", "address": "Shop No.87, Anaj Mandi, Pundri, District- Kaithal, State- Haryana, Pincode"}, {"name": "Rajesh Agro Centre (Baba Ladana)", "pincode": "136027", "mobile": "9416172065", "address": "Main Road, Village- Baba Ladana, District- Kathal, State- Haryana, Pincode"}, {"name": "Ram Dhari and Sons (Safidon)", "pincode": "126102", "mobile": "9996741119", "address": "127/11, Old Anaj Mandi, Near Satnarayan Mandir, Tehsil- Safidon, District- Jind, State- Haryana, Pincode"}, {"name": "Redhu Beej Bhandar (Dalamwala)", "pincode": "126102", "mobile": "9466847094", "address": "Near Bus Stand, VPO Dalamwala, Tehsil- Alewa, District- Jind, State- Haryana, Pincode"}, {"name": "Shiv Shakti Pesticides And Seeds (Fulian Khurd)", "pincode": "126116", "mobile": "8295104121", "address": "Phulian Kalan Near Bus Stand, Fulian Khurd Bus Stop, Village -Fulian Khurd, Tehsil-Narwana, District- Jind"}, {"name": "Shivalik Beej Company (Assandh)", "pincode": "132039", "mobile": "9034047020", "address": "Assandh Thana Road Gali Main, District-Karnal, State - Haryana, Pincode"}, {"name": "Shree Balaji Fertilizers (Kalayat)", "pincode": "136117", "mobile": "9068676919", "address": "Ward No.13, Anaj Mandi, Kalayat"}, {"name": "Sidharath Trading Company (Dhabi Teksingh)", "pincode": "126116", "mobile": "8708266650", "address": "Near Bus Stop, Dhabi Teksingh, Narwana, Jind (Haryana)"}, {"name": "Sudershan And Company (Kaithal)", "pincode": "136027", "mobile": "9034123822", "address": "#5, Super Market, Kaithal (Haryana)"}, {"name": "Yash Traders (Safidon)", "pincode": "126112", "mobile": "9996833473", "address": "Old Anaj Mandi Ward No-13, Safidon, District- Jind, State- Haryana, Pincode"}, {"name": "Aarush Beej Bhandar (Uchana)", "pincode": "126115", "mobile": "9518408793", "address": "#31, Anaj Mandi, Uchana, Jind (Haryana)"}, {"name": "Abhay Ram Trading Company (Narwana)", "pincode": "126116", "mobile": "9812340980", "address": "New Anaj Mandi, Shop No.92 Mela Mandi, Narwana, Dist. Jind, (Haryana)"}, {"name": "Bhai Chara Beej Bhandar (Thua)", "pincode": "126125", "mobile": "9068688968", "address": "Near Baba Aasan Dera, Unnamed Road, Panchayat Bhawan, Thua, Jind (Haryana)"}, {"name": "Gill Trading Company (Lodhar)", "pincode": "126115", "mobile": "9996617083", "address": "VPO- Lodhar, Sher Khan Kheri Road, Tehsil- Uchana, District- Jind, State- Haryana, Pincode"}, {"name": "Gupta Trading Co. (Narwana)", "pincode": "126116", "mobile": "9416660777", "address": "Anaj Mandi, Narwana, Jind (Haryana)"}, {"name": "Haryana Seed & Pesticide (Uchana Mandi)", "pincode": "126115", "mobile": "8708774252", "address": "40 Foota Road, Near Old Anaj Mandi, Tehsil- Uchana Mandi, Dist.- Jind, Haryana"}, {"name": "J.L. Trading Company (Narwana)", "pincode": "126116", "mobile": "9896134778", "address": "Near SBI Bank, Dhakal Road, Vishwakarma Chowk, Narwana, Jind (Haryana)"}, {"name": "Jai Shri Ram Beej Bhandar (Uchana Mandi)", "pincode": "126115", "mobile": "9467496956", "address": "40 Foot Road, Uchana Mandi, Jind (Haryana)"}, {"name": "Karam Beej Bhandhar (Baroda)", "pincode": "126115", "mobile": "8708984319", "address": "Rohtak Road, Balwam Cotton Cloth House, Baroda, Jind (Haryana)"}, {"name": "Kaushal Beej Bhandar (Koth Kalan)", "pincode": "125039", "mobile": "9802921448", "address": "VPO. Koth Kalan, Narnaund (Hisar)"}, {"name": "Kehar Singh Balwan Singh (Uchana)", "pincode": "126115", "mobile": "9416263889", "address": "#151, Old Anaj Mandi, Uchana, Jind (Haryana)"}, {"name": "Raghav Beej Bhandar (Uchana)", "pincode": "126115", "mobile": "9416485709", "address": "Litani Road, Uchana Mandi, Uchana, Jind (Haryana)"}, {"name": "Ram Dhan Deepak Kumar (Narwana)", "pincode": "126116", "mobile": "9996270229", "address": "Mall Godam Road, Narwana"}, {"name": "Rohit Fertilizers (Danoda)", "pincode": "126152", "mobile": "9812055391", "address": "Bus Stand Danoda, Jind (Haryana)"}, {"name": "Satyam Trading Company (Narwana)", "pincode": "126116", "mobile": "9896677618", "address": "Krishna Gali, Narwana, District: Jind, State- Haryana, Pincode"}, {"name": "Sheokand Fertilizers (Narwana)", "pincode": "126116", "mobile": "9728651731", "address": "Near Mela Mandi, Gandhi Nagar, Narwana, Jind (Haryana)"}, {"name": "Shree Salasar Beej Bhandar (Chhattar)", "pincode": "126125", "mobile": "8053664700", "address": "Singara Patti, Chhattar, Tehsil- Uchana, District- Jind, State- Haryana, Pincode"}, {"name": "Shri Ram Trading Company (Uchana)", "pincode": "126115", "mobile": "9416664799", "address": "Anaj Mandi, Uchana Mandi, Teh - Uchana, Distt. Jind, (Haryana)"}, {"name": "Sumit Seed Center (Durjanpur)", "pincode": "126152", "mobile": "9466986291", "address": "Near Modern School, #01, Uchana, Uklana Road, Durjanpur, Jind (Haryana)"}, {"name": "Garg Traders (Sadhaura)", "pincode": "133206", "mobile": "9416376200", "address": "Near Bijli Board, Sainiyon Wali Gali, Sadhaura, Yamunanagar (Haryana)"}, {"name": "Gujjar Pesticides & Seeds (Bhilpura)", "pincode": "135106", "mobile": "9813640550", "address": "Village Bhilpura, Chhachhrauli, Yamunanagar (Haryana)"}, {"name": "Kissan Traders (Chhachhrauli)", "pincode": "135103", "mobile": "8053835201", "address": "Near Bus Stand, Chhachhrauli, Yamunanagar (Haryana)"}, {"name": "Mohan Agrotech (Haibatpur)", "pincode": "133206", "mobile": "9896242533", "address": "Pabni Sadhaura Road, Common Service Centre Haibatpur, Yamunanagar (Haryana)"}, {"name": "Shree Ram Agri (Badhauli)", "pincode": "134203", "mobile": "7082348062", "address": "Vpo. Badhauli, Naraingarh, Ambala (Haryana)"}]

try: init()
except Exception as e: logging.error("Startup init: %s",e)

if __name__=="__main__": app.run(host="0.0.0.0",port=8080)
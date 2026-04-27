from flask import Flask, render_template, request, send_from_directory, jsonify
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from geopy.extra.rate_limiter import RateLimiter
import osmnx as ox
import networkx as nx
import csv, os, pickle, functools, math, random, threading, tempfile, shutil
from datetime import datetime
from pyngrok import ngrok

# ═══════════════════════════════════════════
#  Ngrok
# ═══════════════════════════════════════════
ngrok.set_auth_token(os.environ.get("NGROK_TOKEN", "3CvB1dM0FSwRo44S7Z85N1YGwX9_47sWZ8uQbn6TgTa1T3kX7"))

# ═══════════════════════════════════════════
#  OSMnx
# ═══════════════════════════════════════════
ox.settings.use_cache   = True
ox.settings.log_console = False
GRAPH_CACHE = "hcm_graph.pkl"

def load_graph():
    if os.path.exists(GRAPH_CACHE):
        print("⚡ Loading graph từ cache...")
        with open(GRAPH_CACHE, "rb") as f:
            return pickle.load(f)
    print("🔄 Downloading graph...")
    G = ox.graph_from_place("Ho Chi Minh City, Vietnam",
                            network_type="drive", simplify=True)
    with open(GRAPH_CACHE, "wb") as f:
        pickle.dump(G, f)
    print("✅ Cached!")
    return G

print("🔄 Loading map...")
G = load_graph()
print("✅ Ready!")

app = Flask(__name__)

# ─── Geocoder ───
geolocator = Nominatim(user_agent="goride_v5", timeout=10)
geocode    = RateLimiter(geolocator.geocode, min_delay_seconds=1)
_geo_cache = {}

def cached_geocode(addr):
    if addr in _geo_cache: return _geo_cache[addr]
    r = geocode(addr)
    if r: _geo_cache[addr] = r
    return r

# ═══════════════════════════════════════════
#  Ride state + stats (in-memory)
# ═══════════════════════════════════════════
ride_state = {
    "status"     : "idle",   # idle | waiting | completed
    "current"    : None,     # dict with current ride info
    "total_rides": 0,
    "total_revenue": 0,
}

# ═══════════════════════════════════════════
#  Files
# ═══════════════════════════════════════════
REVIEWS_FILE = "reviews.csv"
DRIVERS_FILE = "drivers.csv"

# ═══════════════════════════════════════════
#  Driver data
# ═══════════════════════════════════════════
VEHICLES = {
    "bike": [("Honda Wave Alpha","Xanh dương"),("Honda Air Blade","Đen nhám"),
             ("Yamaha Exciter","Đỏ đen"),("Honda Vision","Trắng"),
             ("Yamaha Janus","Xám bạc"),("Honda Lead","Nâu vàng")],
    "car4": [("Toyota Vios","Trắng ngọc trai"),("Hyundai Accent","Đen"),
             ("Kia Morning","Xám bạc"),("Mazda 3","Đỏ ruby")],
    "car7": [("Toyota Innova","Bạc"),("Mitsubishi Xpander","Trắng"),("Kia Carnival","Đen")],
}
VEHICLE_ICON = {"bike":"🛵","car4":"🚗","car7":"🚐"}
PROVINCES    = ["51","59","41","43","61","72","50"]

def random_plate():
    return (f"{random.choice(PROVINCES)}"
            f"{random.choice('ABCDEFGHKLMNPSTUVX')}"
            f"{random.randint(1,9)} - {random.randint(1000,9999)}")

def load_drivers():
    drivers = []
    with open(DRIVERS_FILE, newline='', encoding="utf-8") as f:
        for row in csv.DictReader(f):
            drivers.append({
                "name"      : row["name"],
                "lat"       : float(row["lat"]),
                "lon"       : float(row["lon"]),
                "rating"    : float(row["rating"]),
                "preference": row["preference"],
            })
    return drivers

DRIVERS = load_drivers()

def update_driver_rating(driver_name: str, new_star: float):
    """Average new_star into the driver's existing rating in drivers.csv."""
    rows = []
    with open(DRIVERS_FILE, newline='', encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if row["name"] == driver_name:
                old = float(row["rating"])
                # Simple running average: (old + new) / 2
                row["rating"] = f"{(old + new_star) / 2:.2f}"
            rows.append(row)

    # Write back atomically
    tmp = DRIVERS_FILE + ".tmp"
    with open(tmp, "w", newline='', encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    shutil.move(tmp, DRIVERS_FILE)

    # Reload in-memory list
    global DRIVERS
    DRIVERS = load_drivers()

# ═══════════════════════════════════════════
#  Fuzzy Logic — 3 hệ thống độc lập
#  ① Driver Scoring   (5 inputs, 27 rules)
#  ② Fare Multiplier  (4 inputs, 24 rules)
#  ③ ETA Confidence   (3 inputs, 18 rules)
# ═══════════════════════════════════════════

# ── Membership function primitives ──────────
def tri(x, a, b, c):
    """Triangular MF"""
    if x <= a or x >= c: return 0.0
    return (x-a)/(b-a) if x <= b else (c-x)/(c-b)

def trap(x, a, b, c, d):
    """Trapezoidal MF"""
    if x <= a or x >= d: return 0.0
    if x <= b: return (x-a)/(b-a)
    if x <= c: return 1.0
    return (d-x)/(d-c)

def gauss(x, c, sigma):
    """Gaussian MF"""
    import math as _m
    return _m.exp(-0.5*((x-c)/sigma)**2)

def defuzz_centroid(rules):
    """Weighted average defuzzification (centroid method)"""
    tw = sum(w for w, _ in rules)
    return sum(w*v for w, v in rules) / tw if tw else 0.0

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HỆ THỐNG 1: DRIVER SCORING
#  Inputs : rating(0-5), dist_km, trips,
#            accept_rate(0-1), cancel_rate(0-1)
#  Output : score (0-100)
#  Method : Mamdani + centroid defuzz
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fuzzy_driver_score(rating, dist_km, trips,
                       accept_rate=0.85, cancel_rate=0.05):
    # — Fuzzify rating —
    r = {
        "lo": trap(rating, 0, 0,   3.0, 3.8),
        "md": tri (rating, 3.2, 3.9, 4.5),
        "hi": trap(rating, 4.2, 4.6, 5,   5),
    }
    # — Fuzzify distance —
    d = {
        "near": trap(dist_km, 0,   0,   1.5, 3.0),
        "md"  : tri (dist_km, 1.5, 3.5, 6.0),
        "far" : trap(dist_km, 4.5, 7.0, 20,  20),
    }
    # — Fuzzify experience (trips) —
    t = {
        "new": trap(trips, 0,    0,    200,  600),
        "mid": tri (trips, 300,  800,  1500),
        "pro": trap(trips, 1000, 1800, 3000, 3000),
    }
    # — Fuzzify accept rate —
    a = {
        "lo": trap(accept_rate, 0,    0,    0.60, 0.75),
        "md": tri (accept_rate, 0.65, 0.80, 0.92),
        "hi": trap(accept_rate, 0.88, 0.95, 1.0,  1.0),
    }
    # — Fuzzify cancel rate —
    k = {
        "lo": trap(cancel_rate, 0,    0,    0.03, 0.08),
        "md": tri (cancel_rate, 0.05, 0.12, 0.20),
        "hi": trap(cancel_rate, 0.15, 0.25, 1.0,  1.0),
    }

    # — Rules (strength, crisp_output) — 27 rules
    rules = [
        # ── Tier 1: Xuất sắc (85-100) ──
        (min(r["hi"], d["near"], t["pro"],  a["hi"], k["lo"]), 98),
        (min(r["hi"], d["near"], t["mid"],  a["hi"], k["lo"]), 90),
        (min(r["hi"], d["md"],   t["pro"],  a["hi"], k["lo"]), 87),
        (min(r["hi"], d["near"], t["pro"],  a["md"], k["lo"]), 85),
        (min(r["hi"], d["near"], t["new"],  a["hi"], k["lo"]), 80),

        # ── Tier 2: Tốt (65-84) ──
        (min(r["hi"], d["far"],  t["pro"],  a["hi"], k["lo"]), 78),
        (min(r["md"], d["near"], t["pro"],  a["hi"], k["lo"]), 75),
        (min(r["hi"], d["md"],   t["mid"],  a["hi"], k["lo"]), 74),
        (min(r["hi"], d["near"], t["pro"],  a["md"], k["md"]), 72),
        (min(r["md"], d["near"], t["mid"],  a["hi"], k["lo"]), 70),
        (min(r["hi"], d["md"],   t["pro"],  a["md"], k["md"]), 68),
        (min(r["hi"], d["near"], t["new"],  a["md"], k["lo"]), 67),
        (min(r["md"], d["md"],   t["pro"],  a["hi"], k["lo"]), 65),

        # ── Tier 3: Trung bình (40-64) ──
        (min(r["md"], d["near"], t["new"],  a["md"], k["lo"]), 58),
        (min(r["md"], d["md"],   t["mid"],  a["md"], k["lo"]), 55),
        (min(r["hi"], d["far"],  t["new"],  a["md"], k["md"]), 52),
        (min(r["md"], d["far"],  t["pro"],  a["md"], k["lo"]), 50),
        (min(r["lo"], d["near"], t["pro"],  a["hi"], k["lo"]), 48),
        (min(r["md"], d["md"],   t["new"],  a["md"], k["md"]), 45),
        (min(r["md"], d["near"], t["new"],  a["lo"], k["md"]), 42),

        # ── Tier 4: Kém (0-39) ──
        (min(r["lo"], d["near"], t["mid"],  a["md"], k["md"]), 35),
        (min(r["lo"], d["md"],   t["pro"],  a["md"], k["md"]), 30),
        (min(r["lo"], d["near"], t["new"],  a["lo"], k["hi"]), 22),
        (min(r["lo"], d["md"],   t["mid"],  a["lo"], k["hi"]), 18),
        (min(r["lo"], d["far"],  t["new"],  a["lo"], k["hi"]), 10),
        (min(r["md"], d["far"],  t["new"],  a["lo"], k["hi"]), 15),
        (min(r["lo"], d["far"],  t["mid"],  a["lo"], k["hi"]),  8),
    ]
    return defuzz_centroid(rules)

# Alias giữ tương thích với code cũ
def fuzzy_score(rating, dist_km, trips):
    return fuzzy_driver_score(rating, dist_km, trips)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HỆ THỐNG 2: FARE MULTIPLIER (giá động)
#  Inputs : hour(0-24), dow(0-6),
#           dist_km, weather_score(0-1)
#  Output : multiplier (1.0 – 2.0)
#  Method : Sugeno + weighted average
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fuzzy_fare_multiplier(hour, dow, dist_km, weather_score=0.0):
    # — Fuzzify giờ trong ngày —
    h = {
        "night"    : max(trap(hour, 0,0,5,7),     trap(hour, 22,23,24,24)),
        "morning"  : tri (hour, 5,   7,   9),
        "rush_am"  : tri (hour, 7,   8.5, 10),
        "midday"   : trap(hour, 9.5, 11,  14, 15.5),
        "rush_pm"  : tri (hour, 15,  17.5, 20),
        "evening"  : tri (hour, 19,  21,  23),
    }
    # — Fuzzify ngày —
    day = {
        "weekday" : trap(dow, 0, 0, 4, 4.5),
        "weekend" : trap(dow, 4.5, 5, 6, 6),
    }
    # — Fuzzify khoảng cách (ảnh hưởng phụ phí ngắn/dài) —
    dist = {
        "short": trap(dist_km, 0,  0,  3,  5),
        "med"  : tri (dist_km, 3,  8,  15),
        "long" : trap(dist_km, 12, 18, 60, 60),
    }
    # — Fuzzify thời tiết (0=nắng, 1=mưa to) —
    wx = {
        "clear": trap(weather_score, 0,   0,   0.2,  0.4),
        "light": tri (weather_score, 0.2, 0.4, 0.65),
        "heavy": trap(weather_score, 0.55,0.75, 1.0,  1.0),
    }

    # — Rules → (strength, multiplier_output) — 24 rules
    rules = [
        # Rush hour + rain = giá cao nhất
        (min(h["rush_am"],  day["weekday"], dist["short"], wx["heavy"]), 2.00),
        (min(h["rush_pm"],  day["weekday"], dist["short"], wx["heavy"]), 1.95),
        (min(h["rush_am"],  day["weekday"], dist["med"],   wx["heavy"]), 1.85),
        (min(h["rush_pm"],  day["weekday"], dist["med"],   wx["heavy"]), 1.80),

        # Rush hour không mưa
        (min(h["rush_am"],  day["weekday"], dist["short"], wx["clear"]), 1.65),
        (min(h["rush_pm"],  day["weekday"], dist["short"], wx["clear"]), 1.60),
        (min(h["rush_am"],  day["weekday"], dist["med"],   wx["clear"]), 1.50),
        (min(h["rush_pm"],  day["weekday"], dist["med"],   wx["clear"]), 1.48),

        # Weekend tất cả giờ
        (min(h["rush_pm"],  day["weekend"], dist["short"], wx["clear"]), 1.55),
        (min(h["rush_pm"],  day["weekend"], dist["short"], wx["heavy"]), 1.75),
        (min(h["evening"],  day["weekend"], dist["med"],   wx["light"]), 1.45),
        (min(h["night"],    day["weekend"], dist["short"], wx["clear"]), 1.40),

        # Buổi tối thường
        (min(h["evening"],  day["weekday"], dist["med"],   wx["clear"]), 1.30),
        (min(h["evening"],  day["weekday"], dist["short"], wx["light"]), 1.35),
        (min(h["night"],    day["weekday"], dist["short"], wx["clear"]), 1.25),
        (min(h["night"],    day["weekday"], dist["short"], wx["heavy"]), 1.50),

        # Giữa ngày bình thường
        (min(h["midday"],   day["weekday"], dist["med"],   wx["clear"]), 1.10),
        (min(h["midday"],   day["weekday"], dist["long"],  wx["clear"]), 1.05),
        (min(h["morning"],  day["weekday"], dist["med"],   wx["clear"]), 1.15),
        (min(h["midday"],   day["weekday"], dist["short"], wx["light"]), 1.20),

        # Đêm khuya đường dài
        (min(h["night"],    day["weekday"], dist["long"],  wx["clear"]), 1.35),
        (min(h["night"],    day["weekend"], dist["long"],  wx["clear"]), 1.40),

        # Mưa giữa ngày
        (min(h["midday"],   day["weekday"], dist["short"], wx["heavy"]), 1.45),
        (min(h["midday"],   day["weekend"], dist["med"],   wx["heavy"]), 1.50),
    ]
    raw = defuzz_centroid(rules)
    return max(1.0, min(2.0, raw))   # clamp [1.0, 2.0]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HỆ THỐNG 3: ETA CONFIDENCE
#  Inputs : dist_km, hour, route_nodes
#  Output : confidence (0-100%) + label
#  Method : Mamdani + centroid defuzz
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fuzzy_eta_confidence(dist_km, hour, route_nodes):
    # — Fuzzify khoảng cách —
    d = {
        "short": trap(dist_km, 0,  0,  2,  4),
        "med"  : tri (dist_km, 2,  6,  12),
        "long" : trap(dist_km, 10, 15, 60, 60),
    }
    # — Fuzzify giờ (tắc đường) —
    h = {
        "free"  : max(trap(hour, 0, 0, 5, 7),
                      tri (hour, 9.5, 12, 15.5)),
        "medium": max(tri (hour, 5, 8, 10),
                      tri (hour, 15, 18, 21)),
        "heavy" : max(tri (hour, 7, 8.5, 10),
                      tri (hour, 16.5, 18, 20)),
    }
    # — Fuzzify số node route (độ phức tạp lộ trình) —
    n = {
        "simple"  : trap(route_nodes, 0,   0,   20,  50),
        "moderate": tri (route_nodes, 30,  80,  150),
        "complex" : trap(route_nodes, 120, 200, 999, 999),
    }

    # — Rules — 18 rules
    rules = [
        # Ngắn + thông thoáng + đơn giản = rất tin cậy
        (min(d["short"], h["free"],   n["simple"]),   95),
        (min(d["short"], h["free"],   n["moderate"]), 88),
        (min(d["short"], h["medium"], n["simple"]),   80),
        (min(d["med"],   h["free"],   n["simple"]),   82),
        (min(d["med"],   h["free"],   n["moderate"]), 75),

        # Tắc đường vừa
        (min(d["short"], h["heavy"],  n["simple"]),   65),
        (min(d["short"], h["heavy"],  n["moderate"]), 55),
        (min(d["med"],   h["medium"], n["moderate"]), 60),
        (min(d["med"],   h["heavy"],  n["simple"]),   50),
        (min(d["med"],   h["heavy"],  n["moderate"]), 42),

        # Đường dài
        (min(d["long"],  h["free"],   n["moderate"]), 70),
        (min(d["long"],  h["free"],   n["complex"]),  60),
        (min(d["long"],  h["medium"], n["moderate"]), 50),
        (min(d["long"],  h["medium"], n["complex"]),  40),

        # Tắc nặng + phức tạp = kém tin cậy
        (min(d["med"],   h["heavy"],  n["complex"]),  30),
        (min(d["long"],  h["heavy"],  n["moderate"]), 28),
        (min(d["long"],  h["heavy"],  n["complex"]),  18),
        (min(d["short"], h["heavy"],  n["complex"]),  45),
    ]
    conf = defuzz_centroid(rules)
    conf = max(0.0, min(100.0, conf))
    if conf >= 80: label = "Rất chính xác ✅"
    elif conf >= 60: label = "Khá chính xác 🟡"
    elif conf >= 40: label = "Tương đối ⚠️"
    else:            label = "Không chắc 🔴"
    return round(conf, 1), label


# ═══════════════════════════════════════════
#  Fare — dùng fuzzy_fare_multiplier
# ═══════════════════════════════════════════
BASE = {
    "bike":{"base":15_000,"perKm":9_500, "min":15_000},
    "car4":{"base":25_000,"perKm":16_500,"min":30_000},
    "car7":{"base":35_000,"perKm":20_000,"min":45_000},
}

def calc_fare(km, vtype, weather_score=0.0):
    now  = datetime.now()
    hour = now.hour + now.minute / 60
    dow  = now.weekday()          # 0=Thứ 2 … 6=CN
    r    = BASE.get(vtype, BASE["bike"])

    # ── Fuzzy multiplier (Hệ thống 2) ──
    mult = fuzzy_fare_multiplier(hour, dow, km, weather_score)

    base  = r["base"] + km * r["perKm"]
    sur   = base * (mult - 1.0)           # phụ phí = phần tăng thêm
    disc  = base * 0.08 if km > 15 else base * 0.04 if km > 8 else 0
    total = max(r["min"], round((base + sur - disc) / 1000) * 1000)

    # Label nhu cầu
    if mult >= 1.7:   demand_label = "Rất cao 🔴"
    elif mult >= 1.35: demand_label = "Cao 🟠"
    else:              demand_label = "Bình thường 🟢"

    return {
        "fare"         : int(total),
        "surcharge"    : int(round(sur / 1000) * 1000),
        "demand_label" : demand_label,
        "demand_low"   : mult < 1.2,
        "multiplier"   : round(mult, 2),
    }

# ═══════════════════════════════════════════
#  Graph helpers
# ═══════════════════════════════════════════
@functools.lru_cache(maxsize=512)
def get_route(orig, dest):
    route = nx.shortest_path(G, orig, dest, weight='length')
    km    = sum(G[u][v][0].get("length",0) for u,v in zip(route[:-1],route[1:])) / 1000
    return route, km

def route_coords(node_list):
    return [[G.nodes[n]['y'], G.nodes[n]['x']] for n in node_list]

def nearest_node(lat, lon):
    return ox.distance.nearest_nodes(G, lon, lat)

def get_initials(name):
    p = name.split()
    return (p[0][0]+p[-1][0]).upper() if len(p)>=2 else name[:2].upper()

# ═══════════════════════════════════════════
#  Reviews helpers
# ═══════════════════════════════════════════
def save_review(data: dict):
    exists = os.path.exists(REVIEWS_FILE)
    with open(REVIEWS_FILE, "a", newline='', encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp","driver_name","plate","stars","tags","comment"])
        if not exists: w.writeheader()
        w.writerow({
            "timestamp"  : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "driver_name": data.get("driver_name",""),
            "plate"      : data.get("plate",""),
            "stars"      : data.get("stars",0),
            "tags"       : data.get("tags",""),
            "comment"    : data.get("comment",""),
        })

def read_reviews():
    if not os.path.exists(REVIEWS_FILE):
        return []
    with open(REVIEWS_FILE, newline='', encoding="utf-8") as f:
        return list(csv.DictReader(f))

# ═══════════════════════════════════════════
#  Flask routes
# ═══════════════════════════════════════════
@app.route("/map-inner")
def map_inner():
    return send_from_directory("templates", "_map_inner.html")

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

# ── Ride state API ──
@app.route("/ride_status")
def ride_status():
    return jsonify({
        "status" : ride_state["status"],
        "current": ride_state["current"],
    })

@app.route("/complete_ride", methods=["POST"])
def complete_ride():
    if ride_state["status"] != "waiting":
        return jsonify({"ok": False, "error": "No active ride"}), 400
    ride_state["status"] = "completed"
    print("\n✅ Ride COMPLETED — review screen will pop up in browser.\n")
    return jsonify({"ok": True})

@app.route("/submit_review", methods=["POST"])
def submit_review():
    data = request.get_json(force=True)
    save_review(data)

    # Update driver rating: average of old CSV rating + customer stars
    stars = int(data.get("stars", 0))
    if stars > 0:
        update_driver_rating(data.get("driver_name",""), float(stars))

    ride_state["status"]  = "idle"
    ride_state["current"] = None
    print(f'⭐ Review saved: {stars}★  {data.get("driver_name")}  "{data.get("comment","")}"')
    return jsonify({"ok": True})

# ── Stats API ──
@app.route("/api/stats")
def api_stats():
    reviews = read_reviews()
    stars   = [float(r["stars"]) for r in reviews if r.get("stars")]
    return jsonify({
        "total_rides"   : ride_state["total_rides"],
        "total_reviews" : len(reviews),
        "avg_stars"     : sum(stars)/len(stars) if stars else 0,
        "total_revenue" : ride_state["total_revenue"],
    })

# ── Reviews API ──
@app.route("/api/reviews")
def api_reviews():
    return jsonify({"reviews": read_reviews()})

# ── Main booking ──
@app.route("/", methods=["GET","POST"])
def index():
    if request.method == "POST":
        pickup = request.form["pickup"]
        destination = request.form["destination"]
        vtype  = request.form.get("vehicle_type","bike")

        loc1 = cached_geocode(pickup)
        loc2 = cached_geocode(destination)
        if not loc1 or not loc2:
            return "❌ Không tìm thấy địa chỉ.", 400

        try:
            # Trip route
            pn = nearest_node(loc1.latitude, loc1.longitude)
            dn = nearest_node(loc2.latitude, loc2.longitude)
            trip_route, trip_km = get_route(pn, dn)
            trip_coords = route_coords(trip_route)

            # Driver matching
            pickup_coords = (loc1.latitude, loc1.longitude)
            scored = []
            for d in DRIVERS:
                dist = geodesic(pickup_coords,(d["lat"],d["lon"])).km
                if dist > 15: continue
                sc = fuzzy_score(d["rating"], dist, random.randint(120,3500))
                scored.append((sc, dist, d))

            if not scored:
                scored = [(fuzzy_score(d["rating"],geodesic(pickup_coords,(d["lat"],d["lon"])).km,500),
                           geodesic(pickup_coords,(d["lat"],d["lon"])).km, d) for d in DRIVERS]

            scored.sort(key=lambda x:(-x[0],x[1]))
            sc, dist_km, best = random.choice(scored[:min(3,len(scored))])

            # Approach route: driver → pickup (real roads)
            driver_node = nearest_node(best["lat"], best["lon"])
            try:
                approach_route, _ = get_route(driver_node, pn)
                approach_coords   = route_coords(approach_route)
            except Exception:
                approach_coords = []

            # Random vehicle
            veh_name, veh_color = random.choice(VEHICLES.get(vtype, VEHICLES["bike"]))
            plate = random_plate()
            trips = random.randint(120, 3500)
            eta   = max(1, math.ceil(dist_km / 25 * 60))

            # Fare
            fi = calc_fare(trip_km, vtype)

            # Update state
            ride_state["status"]       = "waiting"
            ride_state["total_rides"] += 1
            ride_state["total_revenue"] += fi["fare"]
            ride_state["current"] = {
                "driver_name": best["name"],
                "plate"      : plate,
                "road_km"    : round(trip_km, 2),
                "fare"       : f"{fi['fare']:,}",
                "eta"        : eta,
            }

            driver_info = {
                "initials"      : get_initials(best["name"]),
                "name"          : best["name"],
                "rating"        : best["rating"],
                "pref"          : best["preference"],
                "prefClass"     : "badge-quiet" if best["preference"].lower() in ("quiet","yên tĩnh") else "badge-chat",
                "plate"         : plate,
                "vehicle"       : f"{veh_name} · {veh_color}",
                "vehicle_icon"  : VEHICLE_ICON.get(vtype,"🛵"),
                "trips"         : f"{trips:,}",
                "eta"           : eta,
                "fare"          : f"{fi['fare']:,}",
                "surcharge"     : f"{fi['surcharge']:,}",
                "road_km"       : round(trip_km,2),
                "demand_label"  : fi["demand_label"],
                "demand_low"    : fi["demand_low"],
                "lat"           : best["lat"],
                "lon"           : best["lon"],
                "pickup_lat"    : loc1.latitude,
                "pickup_lon"    : loc1.longitude,
                "dest_lat"      : loc2.latitude,
                "dest_lon"      : loc2.longitude,
                "route_coords"  : trip_coords,
                "approach_route": approach_coords,
            }

            print(f"\n🛵 Driver: {best['name']} | ETA {eta}min | Score {sc:.1f}")
            print(f"   Plate: {plate} | {veh_name} {veh_color}")
            print(f"   Fare: {fi['fare']:,}₫ | {trip_km:.1f}km")
            print(f"   Approach route: {len(approach_coords)} nodes")
            print(f"\n   👉 Dashboard: http://localhost:5000/dashboard")
            print(f"   👉 Complete:  curl -X POST http://localhost:5000/complete_ride\n")

            return render_template("result.html", driver=driver_info)

        except nx.NetworkXNoPath:
            return "❌ Không tìm được đường đi.", 400
        except Exception as e:
            import traceback; traceback.print_exc()
            return f"❌ Lỗi: {e}", 500

    return render_template("index.html")

# ═══════════════════════════════════════════
#  Ngrok + run
# ═══════════════════════════════════════════
def start_ngrok():
    try: ngrok.kill()
    except: pass
    tunnel = ngrok.connect(5000)
    url = tunnel.public_url
    print(f"\n{'='*54}")
    print(f"  🌍 Public URL  : {url}")
    print(f"  📊 Dashboard   : {url}/dashboard")
    print(f"  Share the public URL with friends!")
    print(f"{'='*54}\n")

if __name__ == "__main__":
    threading.Thread(target=start_ngrok, daemon=True).start()
    app.run(port=5000, debug=False, use_reloader=False)
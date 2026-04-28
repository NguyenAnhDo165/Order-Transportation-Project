from flask import Flask, render_template, request, send_from_directory, jsonify
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from geopy.extra.rate_limiter import RateLimiter
import osmnx as ox
import networkx as nx
import csv, os, pickle, functools, math, random, threading, shutil
from datetime import datetime
from pyngrok import ngrok

# ════════════════════════════════════════════════
#  Ngrok
# ════════════════════════════════════════════════
ngrok.set_auth_token("3CvB1dM0FSwRo44S7Z85N1YGwX9_47sWZ8uQbn6TgTa1T3kX7")

# ════════════════════════════════════════════════
#  OSMnx
# ════════════════════════════════════════════════
ox.settings.use_cache   = True
ox.settings.log_console = False
GRAPH_CACHE = "hcm_graph.pkl"

def load_graph():
    if os.path.exists(GRAPH_CACHE):
        print("⚡ Loading graph từ cache...")
        with open(GRAPH_CACHE,"rb") as f: return pickle.load(f)
    print("🔄 Downloading graph...")
    G = ox.graph_from_place("Ho Chi Minh City, Vietnam", network_type="drive", simplify=True)
    with open(GRAPH_CACHE,"wb") as f: pickle.dump(G,f)
    print("✅ Cached!"); return G

print("🔄 Loading map..."); G = load_graph(); print("✅ Ready!")

app = Flask(__name__)

geolocator = Nominatim(user_agent="goride_v6", timeout=10)
geocode    = RateLimiter(geolocator.geocode, min_delay_seconds=1)
_geo_cache = {}

def cached_geocode(addr):
    if addr in _geo_cache: return _geo_cache[addr]
    r = geocode(addr)
    if r: _geo_cache[addr] = r
    return r

# ════════════════════════════════════════════════
#  Multi-ride queue  (nhiều cuốc đồng thời)
# ════════════════════════════════════════════════
import uuid as _uuid

rides       = {}   # ride_id → ride_dict
stats_store = {"total_rides":0, "total_revenue":0, "driver_earnings":0}

def new_ride(info: dict) -> str:
    rid = str(_uuid.uuid4())[:8]
    rides[rid] = {**info, "id": rid, "status": "waiting",
                  "created": datetime.now().strftime("%H:%M:%S")}
    stats_store["total_rides"] += 1
    stats_store["total_revenue"]  += info.get("fare_raw", 0)
    stats_store["driver_earnings"] += info.get("driver_earn_raw", 0)
    return rid

# ════════════════════════════════════════════════
#  Files
# ════════════════════════════════════════════════
REVIEWS_FILE = "reviews.csv"
DRIVERS_FILE = "drivers.csv"
VOUCHERS_FILE = "vouchers.csv"

# ════════════════════════════════════════════════
#  Vouchers  (load from CSV)
# ════════════════════════════════════════════════
def load_vouchers() -> dict:
    v = {}
    if not os.path.exists(VOUCHERS_FILE):
        # Seed sample vouchers
        with open(VOUCHERS_FILE,"w",newline='',encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["code","discount_pct","max_discount","description","expires","active"])
            w.writeheader()
            rows = [
                {"code":"GORIDE20","discount_pct":20,"max_discount":30000,"description":"Giảm 20% tối đa 30k","expires":"2025-12-31","active":"1"},
                {"code":"TETHOLIDAY","discount_pct":25,"max_discount":50000,"description":"Mừng Tết 2025","expires":"2025-02-28","active":"1"},
                {"code":"SUMMER15","discount_pct":15,"max_discount":20000,"description":"Hè sôi động giảm 15%","expires":"2025-08-31","active":"1"},
                {"code":"NEWUSER","discount_pct":30,"max_discount":40000,"description":"Khách mới giảm 30%","expires":"2026-12-31","active":"1"},
                {"code":"RAINY10","discount_pct":10,"max_discount":15000,"description":"Mùa mưa giảm 10%","expires":"2025-11-30","active":"1"},
            ]
            w.writerows(rows)
    with open(VOUCHERS_FILE,newline='',encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("active","1") == "1":
                v[row["code"].strip().upper()] = {
                    "discount_pct" : float(row["discount_pct"]),
                    "max_discount" : float(row["max_discount"]),
                    "description"  : row["description"],
                    "expires"      : row.get("expires",""),
                }
    return v

VOUCHERS = load_vouchers()

def apply_voucher(code: str, fare: float):
    code = code.strip().upper()
    if code not in VOUCHERS: return 0, None
    v = VOUCHERS[code]
    # Check expiry
    try:
        exp = datetime.strptime(v["expires"], "%Y-%m-%d")
        if datetime.now() > exp: return 0, None
    except: pass
    disc = min(fare * v["discount_pct"]/100, v["max_discount"])
    disc = round(disc/1000)*1000
    return disc, v

# ════════════════════════════════════════════════
#  Vehicle data
# ════════════════════════════════════════════════
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
    rows=[]
    with open(DRIVERS_FILE,newline='',encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({"name":row["name"],"lat":float(row["lat"]),"lon":float(row["lon"]),
                         "rating":float(row["rating"]),"preference":row["preference"]})
    return rows

DRIVERS = load_drivers()

def update_driver_rating(name: str, new_star: float):
    rows=[]
    with open(DRIVERS_FILE,newline='',encoding="utf-8") as f:
        rd = csv.DictReader(f); fn = rd.fieldnames
        for row in rd:
            if row["name"]==name:
                row["rating"] = f"{(float(row['rating'])+new_star)/2:.2f}"
            rows.append(row)
    tmp = DRIVERS_FILE+".tmp"
    with open(tmp,"w",newline='',encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fn); w.writeheader(); w.writerows(rows)
    shutil.move(tmp,DRIVERS_FILE)
    global DRIVERS; DRIVERS = load_drivers()

# ════════════════════════════════════════════════
#  Fuzzy Logic helpers
# ════════════════════════════════════════════════
def tri(x,a,b,c):
    if x<=a or x>=c: return 0.0
    return (x-a)/(b-a) if x<=b else (c-x)/(c-b)

def trap(x,a,b,c,d):
    if x<=a or x>=d: return 0.0
    if x<=b: return (x-a)/(b-a)
    if x<=c: return 1.0
    return (d-x)/(d-c)

def fuzzy_score(rating, dist_km, trips):
    r={"lo":trap(rating,0,0,3.0,3.8),"md":tri(rating,3.2,3.9,4.5),"hi":trap(rating,4.2,4.6,5,5)}
    d={"near":trap(dist_km,0,0,1.5,3),"md":tri(dist_km,1.5,3.5,6),"far":trap(dist_km,4.5,7,20,20)}
    t={"new":trap(trips,0,0,200,600),"mid":tri(trips,300,800,1500),"pro":trap(trips,1000,1800,3000,3000)}
    rules=[(min(r["hi"],d["near"],t["pro"]),95),(min(r["hi"],d["near"],t["mid"]),85),
           (min(r["hi"],d["md"],t["pro"]),80),(min(r["hi"],d["near"],t["new"]),72),
           (min(r["hi"],d["far"],t["pro"]),70),(min(r["md"],d["near"],t["pro"]),68),
           (min(r["hi"],d["md"],t["mid"]),66),(min(r["md"],d["near"],t["mid"]),58),
           (min(r["md"],d["md"],t["pro"]),55),(min(r["md"],d["near"],t["new"]),45),
           (min(r["lo"],d["near"],t["pro"]),38),(min(r["lo"],d["md"],t["mid"]),25),
           (min(r["lo"],d["far"],t["new"]),10),(min(r["hi"],d["far"],t["new"]),40),
           (min(r["md"],d["far"],t["new"]),20)]
    tw=sum(w for w,_ in rules)
    return sum(w*c for w,c in rules)/tw if tw else 0.0

# ════════════════════════════════════════════════
#  Fuzzy Fare Engine
# ════════════════════════════════════════════════
# Base pricing  (opening fee covers first 2 km)
BASE = {
    "bike": {"open":13_000,"per_km":4_500,"wait_min":500, "min":13_000},
    "car4": {"open":22_000,"per_km":9_000,"wait_min":800, "min":22_000},
    "car7": {"open":32_000,"per_km":12_000,"wait_min":1_000,"min":32_000},
}
DRIVER_SHARE = 0.80   # 80% doanh thu về tài xế
REGION_DISC  = 0.07   # 7% giảm cho tỉnh (Ninh Thuận style)

def fuzzy_surge(hour: float, dow: int) -> float:
    """Surge multiplier 1.0–1.8, Mamdani centroid defuzz."""
    morning  = tri(hour, 6.5, 8.0, 9.5)
    evening  = tri(hour, 16.5,18.0,20.0)
    night    = min(1.0, trap(hour,22,23,24,24)+trap(hour,0,0,1,2))
    weekend  = 0.25 if dow>=5 else 0.0

    # Membership → output center pairs
    rules = [
        (morning,   1.55),
        (evening,   1.60),
        (night*0.8, 1.25),
        (weekend,   1.20),
        (max(morning,evening)*weekend, 1.80),  # peak + weekend
    ]
    base_load = max(morning, evening, night*0.7)
    rules.append((1.0 - base_load, 1.0))   # off-peak → no surge

    tw = sum(w for w,_ in rules)
    surge = sum(w*c for w,c in rules)/tw if tw else 1.0
    return round(min(1.80, max(1.0, surge)), 3)

def fuzzy_weather_fee(is_rain: bool, is_storm: bool) -> float:
    """Returns additive fee multiplier 0–0.50."""
    if is_storm: return 0.50
    if is_rain:  return random.uniform(0.10, 0.30)   # simulate
    return 0.0

def pickup_fee(dist_km: float) -> int:
    """Extra fee if driver is far from pickup."""
    if dist_km > 6:  return 15_000
    if dist_km > 3:  return 10_000
    return 0

def calc_fare(trip_km: float, vtype: str, driver_dist_km: float = 0,
              wait_min: float = 0, voucher_code: str = "") -> dict:
    now  = datetime.now()
    h    = now.hour + now.minute/60
    dow  = now.weekday()
    r    = BASE.get(vtype, BASE["bike"])

    # ── Base fare ──
    extra_km  = max(0.0, trip_km - 2.0)          # first 2 km in opening fee
    base_fare = r["open"] + extra_km * r["per_km"] + wait_min * r["wait_min"]
    base_fare = max(base_fare, r["min"])

    # ── Surge (fuzzy) ──
    surge_mult = fuzzy_surge(h, dow)
    surge_fee  = base_fare * (surge_mult - 1.0)

    # ── Night premium (already partially captured by surge, add small top-up) ──
    night_fee  = base_fare * 0.12 if (h >= 22 or h < 5) else 0

    # ── Weather (random demo) ──
    is_rain    = random.random() < 0.25   # 25% chance rain in demo
    weather_fee= base_fare * fuzzy_weather_fee(is_rain, False)

    # ── Pickup fee ──
    pick_fee   = pickup_fee(driver_dist_km)

    # ── Subtotal before discount ──
    subtotal   = base_fare + surge_fee + night_fee + weather_fee + pick_fee

    # ── Regional discount ──
    region_disc = subtotal * REGION_DISC

    # ── Raw fare ──
    raw_fare   = subtotal - region_disc
    raw_fare   = round(raw_fare / 1000) * 1000
    raw_fare   = max(r["min"], int(raw_fare))

    # ── Voucher ──
    voucher_disc = 0
    voucher_info = None
    if voucher_code:
        voucher_disc, voucher_info = apply_voucher(voucher_code, raw_fare)

    final_fare = max(r["min"], raw_fare - voucher_disc)

    # ── Driver earnings ──
    driver_earn = round(final_fare * DRIVER_SHARE / 1000) * 1000

    # ── Surge label ──
    if surge_mult >= 1.6:  surge_label = f"Rất cao ×{surge_mult:.2f} 🔴"
    elif surge_mult >= 1.3: surge_label = f"Cao ×{surge_mult:.2f} 🟠"
    else:                   surge_label = "Bình thường 🟢"

    return {
        "fare_raw"      : raw_fare,
        "fare"          : final_fare,
        "driver_earn"   : driver_earn,
        "base_fare"     : int(base_fare),
        "surge_fee"     : int(surge_fee),
        "night_fee"     : int(night_fee),
        "weather_fee"   : int(weather_fee),
        "pick_fee"      : pick_fee,
        "region_disc"   : int(region_disc),
        "voucher_disc"  : int(voucher_disc),
        "voucher_info"  : voucher_info,
        "surge_mult"    : surge_mult,
        "surge_label"   : surge_label,
        "is_rain"       : is_rain,
        "demand_low"    : surge_mult < 1.2,
    }

# ════════════════════════════════════════════════
#  Graph helpers
# ════════════════════════════════════════════════
@functools.lru_cache(maxsize=512)
def get_route(orig, dest):
    route = nx.shortest_path(G, orig, dest, weight='length')
    km    = sum(G[u][v][0].get("length",0) for u,v in zip(route[:-1],route[1:])) / 1000
    return route, km

def route_coords(nl): return [[G.nodes[n]['y'],G.nodes[n]['x']] for n in nl]
def nearest_node(lat,lon): return ox.distance.nearest_nodes(G, lon, lat)
def get_initials(name):
    p=name.split(); return (p[0][0]+p[-1][0]).upper() if len(p)>=2 else name[:2].upper()

# ════════════════════════════════════════════════
#  Reviews
# ════════════════════════════════════════════════
def save_review(data):
    ex = os.path.exists(REVIEWS_FILE)
    with open(REVIEWS_FILE,"a",newline='',encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=["timestamp","ride_id","driver_name","plate","stars","tags","comment"])
        if not ex: w.writeheader()
        w.writerow({"timestamp":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ride_id":data.get("ride_id",""),"driver_name":data.get("driver_name",""),
                    "plate":data.get("plate",""),"stars":data.get("stars",0),
                    "tags":data.get("tags",""),"comment":data.get("comment","")})

def read_reviews():
    if not os.path.exists(REVIEWS_FILE): return []
    with open(REVIEWS_FILE,newline='',encoding="utf-8") as f: return list(csv.DictReader(f))

# ════════════════════════════════════════════════
#  Voucher API endpoint
# ════════════════════════════════════════════════
@app.route("/api/check_voucher", methods=["POST"])
def check_voucher():
    data = request.get_json(force=True)
    code = data.get("code","").strip().upper()
    fare = float(data.get("fare", 0))
    disc, info = apply_voucher(code, fare)
    if info:
        return jsonify({"valid":True,"discount":int(disc),"description":info["description"],
                        "pct":info["discount_pct"]})
    return jsonify({"valid":False,"discount":0,"description":"Mã không hợp lệ hoặc hết hạn"})

# ════════════════════════════════════════════════
#  Dashboard API
# ════════════════════════════════════════════════
@app.route("/dashboard")
def dashboard(): return render_template("dashboard.html")

@app.route("/api/rides")
def api_rides():
    return jsonify({"rides": list(rides.values())})

@app.route("/api/stats")
def api_stats():
    reviews = read_reviews()
    stars   = [float(r["stars"]) for r in reviews if r.get("stars")]
    return jsonify({
        "total_rides"    : stats_store["total_rides"],
        "total_reviews"  : len(reviews),
        "avg_stars"      : sum(stars)/len(stars) if stars else 0,
        "total_revenue"  : stats_store["total_revenue"],
        "driver_earnings": stats_store["driver_earnings"],
    })

@app.route("/api/reviews")
def api_reviews(): return jsonify({"reviews": read_reviews()})

@app.route("/ride_status")
def ride_status():
    ride_id = request.args.get("id","")
    if ride_id and ride_id in rides:
        return jsonify({"status": rides[ride_id]["status"], "current": rides[ride_id]})
    # Legacy: return any waiting ride
    waiting = [r for r in rides.values() if r["status"]=="waiting"]
    if waiting: return jsonify({"status":"waiting","current":waiting[0]})
    return jsonify({"status":"idle","current":None})

@app.route("/complete_ride", methods=["POST"])
def complete_ride():
    ride_id = request.get_json(force=True).get("ride_id","") if request.data else ""
    if ride_id and ride_id in rides:
        rides[ride_id]["status"] = "completed"
    else:
        # complete oldest waiting ride
        for r in rides.values():
            if r["status"]=="waiting": r["status"]="completed"; break
    print("\n✅ Ride COMPLETED\n")
    return jsonify({"ok":True})

@app.route("/submit_review", methods=["POST"])
def submit_review():
    data = request.get_json(force=True)
    save_review(data)
    stars = int(data.get("stars",0))
    if stars>0: update_driver_rating(data.get("driver_name",""), float(stars))
    ride_id = data.get("ride_id","")
    if ride_id in rides: del rides[ride_id]
    print(f'"⭐ {stars}★ — {data.get("driver_name")} — "{data.get("comment","")}"')
    return jsonify({"ok":True})

# ════════════════════════════════════════════════
#  Main booking route
# ════════════════════════════════════════════════

@app.route("/api/drivers")
def api_drivers():
    """Return all drivers with aggregated earnings from reviews."""
    drivers = load_drivers()

    # Build earnings map from reviews
    reviews = read_reviews()
    earnings = {}
    for r in reviews:
        name = r.get("driver_name","")
        if not name: continue
        if name not in earnings:
            earnings[name] = {"driver_name":name,"rating":0,"trip_count":0,
                               "total_revenue":0,"total_earn":0}
        try:
            stars = float(r.get("stars",0))
            # Approximate revenue: total_earn / 0.8
            # We don't store per-review revenue so we estimate from known fare data
            earnings[name]["trip_count"] += 1
            earnings[name]["rating"] = (earnings[name]["rating"] * (earnings[name]["trip_count"]-1) + stars) / earnings[name]["trip_count"]
        except: pass

    # Merge driver earnings from rides dict
    for ride in rides.values():
        name = ride.get("driver_name","")
        if not name: continue
        if name not in earnings:
            earnings[name] = {"driver_name":name,"rating":0,"trip_count":0,"total_revenue":0,"total_earn":0}
        earnings[name]["total_revenue"] = earnings[name].get("total_revenue",0) + (ride.get("fare_raw") or ride.get("fare",0) or 0)
        earnings[name]["total_earn"]    = earnings[name].get("total_earn",0)    + (ride.get("driver_earn_raw",0) or 0)

    # Attach earnings to driver list
    total_driver_earnings = sum(e["total_earn"] for e in earnings.values())
    driver_list = []
    for d in drivers:
        e = earnings.get(d["name"], {})
        driver_list.append({**d,
            "total_earn"   : e.get("total_earn", 0),
            "total_revenue": e.get("total_revenue", 0),
            "total_trips"  : e.get("trip_count", 0),
        })

    return jsonify({
        "drivers"              : driver_list,
        "earnings"             : earnings,
        "total_driver_earnings": total_driver_earnings,
    })

@app.route("/map-inner")
def map_inner(): return send_from_directory("templates","_map_inner.html")

@app.route("/", methods=["GET","POST"])
def index():
    if request.method == "POST":
        pickup       = request.form["pickup"]
        destination  = request.form["destination"]
        vtype        = request.form.get("vehicle_type","bike")
        voucher_code = request.form.get("voucher_code","").strip().upper()

        loc1 = cached_geocode(pickup)
        loc2 = cached_geocode(destination)
        if not loc1 or not loc2: return "❌ Không tìm thấy địa chỉ.", 400

        try:
            pn = nearest_node(loc1.latitude, loc1.longitude)
            dn = nearest_node(loc2.latitude, loc2.longitude)
            trip_route, trip_km = get_route(pn, dn)
            trip_coords = route_coords(trip_route)

            # Driver matching
            pc = (loc1.latitude, loc1.longitude)
            scored=[]
            for d in DRIVERS:
                dist=geodesic(pc,(d["lat"],d["lon"])).km
                if dist>15: continue
                sc=fuzzy_score(d["rating"],dist,random.randint(120,3500))
                scored.append((sc,dist,d))
            if not scored:
                scored=[(fuzzy_score(d["rating"],geodesic(pc,(d["lat"],d["lon"])).km,500),
                         geodesic(pc,(d["lat"],d["lon"])).km,d) for d in DRIVERS]
            scored.sort(key=lambda x:(-x[0],x[1]))
            sc,dist_km,best=random.choice(scored[:min(3,len(scored))])

            # Approach route
            dnode=nearest_node(best["lat"],best["lon"])
            try: appr,_=get_route(dnode,pn); appr_c=route_coords(appr)
            except: appr_c=[]

            veh_name,veh_color=random.choice(VEHICLES.get(vtype,VEHICLES["bike"]))
            plate=random_plate(); trips=random.randint(120,3500)
            eta=max(1,math.ceil(dist_km/25*60))

            fi = calc_fare(trip_km, vtype, dist_km, 0, voucher_code)
            ride_id = new_ride({
                "driver_name"  : best["name"],
                "plate"        : plate,
                "road_km"      : round(trip_km,2),
                "fare_raw"     : fi["fare_raw"],
                "fare"         : fi["fare"],
                "driver_earn_raw": fi["driver_earn"],
                "eta"          : eta,
                "vtype"        : vtype,
            })

            d_info = {
                "ride_id"       : ride_id,
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
                # Fare breakdown
                "fare"          : f"{fi['fare']:,}",
                "fare_raw"      : f"{fi['fare_raw']:,}",
                "base_fare"     : f"{fi['base_fare']:,}",
                "surge_fee"     : f"{fi['surge_fee']:,}",
                "night_fee"     : f"{fi['night_fee']:,}",
                "weather_fee"   : f"{fi['weather_fee']:,}",
                "pick_fee"      : f"{fi['pick_fee']:,}",
                "region_disc"   : f"{fi['region_disc']:,}",
                "voucher_disc"  : f"{fi['voucher_disc']:,}",
                "voucher_info"  : fi["voucher_info"],
                "driver_earn"   : f"{fi['driver_earn']:,}",
                "road_km"       : round(trip_km,2),
                "surge_label"   : fi["surge_label"],
                "surge_mult"    : fi["surge_mult"],
                "is_rain"       : fi["is_rain"],
                "demand_low"    : fi["demand_low"],
                # Coords
                "lat"           : best["lat"], "lon": best["lon"],
                "pickup_lat"    : loc1.latitude, "pickup_lon": loc1.longitude,
                "dest_lat"      : loc2.latitude, "dest_lon"  : loc2.longitude,
                "route_coords"  : trip_coords,
                "approach_route": appr_c,
            }

            print(f"\n🛵 [{ride_id}] {best['name']} | ETA {eta}min | {fi['fare']:,}₫ → Driver {fi['driver_earn']:,}₫")
            if fi["voucher_info"]: print(f"   🎟  Voucher {voucher_code}: -{fi['voucher_disc']:,}₫")
            print(f"   Surge ×{fi['surge_mult']} | Rain: {fi['is_rain']}")
            print(f"   👉 Dashboard: http://localhost:5000/dashboard\n")

            return render_template("result.html", driver=d_info)

        except nx.NetworkXNoPath: return "❌ Không tìm được đường đi.", 400
        except Exception as e:
            import traceback; traceback.print_exc(); return f"❌ Lỗi: {e}", 500

    return render_template("index.html")

# ════════════════════════════════════════════════
#  Ngrok + run
# ════════════════════════════════════════════════
def start_ngrok():
    try: ngrok.kill()
    except: pass
    t = ngrok.connect(5000)
    url = t.public_url
    print(f"\n{'='*54}\n  🌍 {url}\n  📊 {url}/dashboard\n{'='*54}\n")

if __name__=="__main__":
    threading.Thread(target=start_ngrok,daemon=True).start()
    app.run(port=5000,debug=False,use_reloader=False)

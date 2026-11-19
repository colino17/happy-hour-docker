#!/usr/bin/env python3

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo
from math import asin, cos, radians, sin, sqrt
import osmium
from opening_hours import OpeningHours
from rapidfuzz import fuzz

DATA_DIR = os.environ.get("DATA_DIR", "/data")
TZ_NAME = os.environ.get("TZ", "America/Moncton")
LOCAL_TZ = ZoneInfo(TZ_NAME)
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", str(24 * 3600)))
PORT = int(os.environ.get("PORT", "8090"))

def _default_origin():
    lat, lon = os.environ.get("DEFAULT_LAT", "").strip(), os.environ.get("DEFAULT_LON", "").strip()
    if not lat and not lon:
        return None
    if not (lat and lon):
        raise ValueError("DEFAULT_LAT and DEFAULT_LON must be set together")
    try:
        lat_f, lon_f = float(lat), float(lon)
    except ValueError:
        raise ValueError("DEFAULT_LAT / DEFAULT_LON must be numbers")
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        raise ValueError("DEFAULT_LAT / DEFAULT_LON out of range")
    return (lat_f, lon_f)

DEFAULT_ORIGIN = _default_origin()

_GEOFABRIK_CA = "https://download.geofabrik.de/north-america/canada"
KNOWN_LOCATIONS = {
    "new-brunswick": ("New Brunswick", f"{_GEOFABRIK_CA}/new-brunswick-latest.osm.pbf"),
    "nova-scotia": ("Nova Scotia", f"{_GEOFABRIK_CA}/nova-scotia-latest.osm.pbf"),
    "prince-edward-island": ("Prince Edward Island", f"{_GEOFABRIK_CA}/prince-edward-island-latest.osm.pbf"),
    "newfoundland-and-labrador": ("Newfoundland and Labrador", f"{_GEOFABRIK_CA}/newfoundland-and-labrador-latest.osm.pbf"),
    "quebec": ("Quebec", f"{_GEOFABRIK_CA}/quebec-latest.osm.pbf"),
    "ontario": ("Ontario", f"{_GEOFABRIK_CA}/ontario-latest.osm.pbf"),
    "manitoba": ("Manitoba", f"{_GEOFABRIK_CA}/manitoba-latest.osm.pbf"),
    "saskatchewan": ("Saskatchewan", f"{_GEOFABRIK_CA}/saskatchewan-latest.osm.pbf"),
    "alberta": ("Alberta", f"{_GEOFABRIK_CA}/alberta-latest.osm.pbf"),
    "british-columbia": ("British Columbia", f"{_GEOFABRIK_CA}/british-columbia-latest.osm.pbf"),
    "yukon": ("Yukon", f"{_GEOFABRIK_CA}/yukon-latest.osm.pbf"),
    "northwest-territories": ("Northwest Territories", f"{_GEOFABRIK_CA}/northwest-territories-latest.osm.pbf"),
    "nunavut": ("Nunavut", f"{_GEOFABRIK_CA}/nunavut-latest.osm.pbf"),
    "canada": ("Canada", f"{_GEOFABRIK_CA}-latest.osm.pbf"),
    "north-america": ("North America", "https://download.geofabrik.de/north-america-latest.osm.pbf"),
}

def resolve_sources():
    names = [loc.strip().lower().replace(" ", "-").replace("_", "-")
             for loc in os.environ.get("LOCATIONS", "").split(",")]
    names = [n for n in names if n] or ["new-brunswick"]
    sources = []
    for name in names:
        if name not in KNOWN_LOCATIONS:
            raise ValueError(
                f"Unknown LOCATIONS entry {name!r}. Known names: "
                + ", ".join(sorted(KNOWN_LOCATIONS)))
        if name not in [s["slug"] for s in sources]:
            label, url = KNOWN_LOCATIONS[name]
            sources.append({"slug": name, "label": label, "url": url,
                            "path": os.path.join(DATA_DIR, f"{name}.osm.pbf")})
    return sources

def coverage_label(sources):
    override = os.environ.get("COVERAGE_LABEL", "").strip()
    if override:
        return override
    labels = [s["label"] for s in sources]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f" and {labels[-1]}"

SOURCES = resolve_sources()
COVERAGE = coverage_label(SOURCES)

INDEX = []
INDEX_LOCK = threading.Lock()
INDEX_META = {"count": 0, "built_at": None, "coverage": COVERAGE,
              "locations": [s["slug"] for s in SOURCES],
              "sources": [s["url"] for s in SOURCES]}

class HoursHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.places = []

    def _add(self, o, kind):
        t = o.tags
        name, hours = t.get("name"), t.get("opening_hours")
        if not name or not hours:
            return
        street = " ".join(x for x in [t.get("addr:housenumber"), t.get("addr:street")] if x)
        lat = lon = None
        try:
            if kind == "node":
                lat, lon = o.location.lat, o.location.lon
            else:
                pts = [(n.location.lat, n.location.lon) for n in o.nodes
                       if n.location.valid()]
                if pts:
                    lat = sum(p[0] for p in pts) / len(pts)
                    lon = sum(p[1] for p in pts) / len(pts)
        except Exception:
            pass
        self.places.append({
            "name": name,
            "opening_hours": hours,
            "city": t.get("addr:city") or t.get("addr:town") or t.get("addr:village"),
            "street": street or None,
            "postcode": t.get("addr:postcode"),
            "phone": t.get("phone") or t.get("contact:phone"),
            "website": t.get("website") or t.get("contact:website"),
            "lat": lat, "lon": lon,
            "osm": f"{kind}/{o.id}",
        })

    def node(self, o):
        self._add(o, "node")

    def way(self, o):
        self._add(o, "way")

    def relation(self, o):
        self._add(o, "relation")

def download_extract(url, dest):
    tmp = dest + ".tmp"
    req = urllib.request.Request(url, headers={"User-Agent": "nb-hours/1.0"})
    with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    os.replace(tmp, dest)

def _dedupe(places):
    seen, out = set(), []
    for p in places:
        key = (p["name"].lower(), (p["street"] or "").lower(),
               (p["city"] or "").lower(), p["opening_hours"])
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out

def build_index(path):
    h = HoursHandler()
    h.apply_file(path, locations=True)
    return _dedupe(h.places)

def build_combined():
    all_places, per_source = [], {}
    for s in SOURCES:
        places = build_index(s["path"])
        for p in places:
            p["region"] = s["slug"]
        per_source[s["slug"]] = len(places)
        all_places.extend(places)
        print(f"[nb-hours] {s['slug']}: {len(places)} places", flush=True)
    return _dedupe(all_places), per_source

def refresh():
    os.makedirs(DATA_DIR, exist_ok=True)
    legacy = os.path.join(DATA_DIR, "extract.osm.pbf")
    if os.path.exists(legacy) and len(SOURCES) == 1 and not os.path.exists(SOURCES[0]["path"]):
        os.replace(legacy, SOURCES[0]["path"])
    for s in SOURCES:
        if not os.path.exists(s["path"]):
            print(f"[nb-hours] downloading {s['slug']} extract...", flush=True)
            download_extract(s["url"], s["path"])
    print(f"[nb-hours] parsing {len(SOURCES)} extract(s) for {COVERAGE}...", flush=True)
    places, per_source = build_combined()
    with INDEX_LOCK:
        INDEX.clear()
        INDEX.extend(places)
        INDEX_META.update(count=len(places), per_location=per_source,
                          built_at=datetime.now(LOCAL_TZ).isoformat())
    print(f"[nb-hours] indexed {len(places)} places with opening_hours", flush=True)

def refresh_loop():
    while True:
        time.sleep(REFRESH_SECONDS)
        try:
            print("[nb-hours] daily refresh: downloading fresh extract(s)...", flush=True)
            for s in SOURCES:
                download_extract(s["url"], s["path"])
            refresh()
        except Exception as e:  # keep serving the old index
            print(f"[nb-hours] refresh failed, keeping old index: {e}", flush=True)

def fmt_time(dt):
    """21:00 -> '9 PM', 07:30 -> '7:30 AM' (spoken form)."""
    s = dt.strftime("%I:%M %p").lstrip("0")
    return s.replace(":00 ", " ")

def now_local(at=None):
    if at:
        dt = datetime.fromisoformat(at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ)
    return datetime.now(LOCAL_TZ)

def evaluate(place, when):
    raw = place["opening_hours"]
    naive = when.replace(tzinfo=None)
    try:
        oh = OpeningHours(raw)
    except Exception:
        return {"is_open": None, "today": [], "next_change": None, "raw": raw}
    try:
        is_open = bool(oh.is_open(naive))
    except Exception:
        is_open = None
    today = []
    try:
        start = naive.replace(hour=0, minute=0, second=0, microsecond=0)
        for a, b, state, _ in oh.intervals(start, start + timedelta(days=1)):
            if getattr(state, "name", str(state)).upper() == "OPEN":
                today.append((a, b))
    except Exception:
        pass
    nxt = None
    try:
        nxt = oh.next_change(naive)
    except Exception:
        pass
    return {"is_open": is_open, "today": today,
            "next_change": nxt.isoformat() if nxt else None, "raw": raw}

def api_eval(place, when):
    ev = evaluate(place, when)
    ev["today"] = [(a.isoformat(), b.isoformat()) for a, b in ev["today"]]
    return ev

def distance_km(origin, place):
    if origin is None or place.get("lat") is None or place.get("lon") is None:
        return None
    lat1, lon1 = radians(origin[0]), radians(origin[1])
    lat2, lon2 = radians(place["lat"]), radians(place["lon"])
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(h))

def _dist_phrase(place):
    d = place.get("distance_km")
    if d is None:
        return ""
    if d < 1:
        metres = max(10, int(round(d * 100)) * 10)
        return f" It's {metres} metres away."
    if d < 10:
        return f" It's {d:.1f} kilometres away."
    return f" It's {d:.0f} kilometres away."

def describe(place, when):
    ev = evaluate(place, when)
    label = place["name"]
    where = place["street"] or place["city"] or ""
    if where and where.lower() not in label.lower():
        label = f"{label} on {where}" if place["street"] else f"{label} in {where}"
    if ev["is_open"] is None:
        text = f"I have hours listed for {label} as {ev['raw']}, but I can't work out if it's open right now."
    elif ev["is_open"]:
        spans = " and ".join(f"{fmt_time(a)} to {fmt_time(b)}" for a, b in ev["today"]) if ev["today"] else None
        closes = fmt_time(datetime.fromisoformat(ev["next_change"])) if ev["next_change"] else None
        text = (f"Yes, {label} is open today, {spans}." if spans else f"Yes, {label} is open now.") + \
               (f" It closes at {closes}." if closes else "")
    elif ev["today"]:
        spans = " and ".join(f"{fmt_time(a)} to {fmt_time(b)}" for a, b in ev["today"])
        opens = ev["today"][0][0]
        if when.replace(tzinfo=None) < opens:
            text = f"No, {label} is closed right now. It opens today at {fmt_time(opens)} and closes at {fmt_time(ev['today'][0][1])}."
        else:
            text = f"No, {label} was open today, {spans}, but it's closed now."
    else:
        text = f"No, {label} is closed today."
    return text + _dist_phrase(place)

def search(q, city=None, limit=5, origin=None):
    qn = q.strip().lower()
    scored = []
    with INDEX_LOCK:
        places = list(INDEX)
    for p in places:
        hay = f"{p['name']} {p['city'] or ''} {p['street'] or ''}".lower()
        score = fuzz.WRatio(qn, p["name"].lower())
        if qn in hay:
            score = max(score, 92)
        if city:
            if p["city"] and city.lower() == p["city"].lower():
                score += 5
            elif p["city"]:
                continue
        if score >= 78:
            scored.append((score, p))
    if origin is not None:
        ranked = []
        for score, p in scored:
            d = distance_km(origin, p)
            ranked.append((d, score, {**p,
                                      "distance_km": round(d, 3) if d is not None else None,
                                      "score": round(score, 1)}))
        ranked.sort(key=lambda x: (x[0] is None, x[0] if x[0] is not None else 0.0, -x[1]))
        return [p for _, _, p in ranked[:limit]]
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:limit]]

def answer(q, city=None, at=None, origin=None):
    when = now_local(at)
    matches = search(q, city=city, limit=25 if origin is not None else 5, origin=origin)
    if not matches:
        return {"found": False,
                "speech": f"I couldn't find any business called {q} with hours listed in {COVERAGE}."}
    if origin is not None:
        best = max(m["score"] for m in matches)
        eligible = [m for m in matches if m["score"] >= best - 7]
        chosen = eligible[:1]
        return {"found": True, "speech": describe(chosen[0], when),
                "when": when.isoformat(), "origin": list(origin),
                "candidates": len(matches),
                "matches": [{**chosen[0], **api_eval(chosen[0], when)}]}
    top_name = matches[0]["name"].lower()
    same = [m for m in matches if m["name"].lower() == top_name]
    chosen = same[:3] if len(same) > 1 else matches[:1]
    if len(chosen) > 1:
        parts = [describe(m, when) for m in chosen]
        speech = f"I found {len(same)} places called {matches[0]['name']}" + \
                 (f" matching {city}" if city else f" in {COVERAGE}") + \
                 (f". Here are the first {len(chosen)}: " if len(same) > len(chosen) else ". ") + \
                 " ".join(parts)
    else:
        speech = describe(chosen[0], when)
    return {"found": True, "speech": speech, "when": when.isoformat(),
            "matches": [{**m, **api_eval(m, when)} for m in chosen]}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def origin_from(self, qs):
        lat, lon = qs.get("lat"), qs.get("lon")
        if lat is None and lon is None:
            return DEFAULT_ORIGIN
        if lat is None or lon is None:
            raise ValueError("lat and lon must be given together")
        try:
            lat_f, lon_f = float(lat), float(lon)
        except ValueError:
            raise ValueError("lat and lon must be numbers")
        if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
            raise ValueError("lat/lon out of range")
        return (lat_f, lon_f)

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/health":
            self.send_json({"ok": True, "default_origin": DEFAULT_ORIGIN, **INDEX_META})
        elif u.path in ("/search", "/open", "/answer"):
            q = qs.get("q", "").strip()
            if not q:
                self.send_json({"error": "missing ?q="}, 400)
                return
            try:
                origin = self.origin_from(qs)
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
                return
            if u.path == "/search":
                self.send_json({"results": search(q, qs.get("city"), origin=origin)})
            elif u.path == "/open":
                when = now_local(qs.get("at"))
                self.send_json({"results": [
                    {**m, **api_eval(m, when)} for m in search(q, qs.get("city"), origin=origin)]})
            else:
                self.send_json(answer(q, qs.get("city"), qs.get("at"), origin=origin))
        else:
            self.send_json({"error": "not found"}, 404)

if __name__ == "__main__":
    refresh()
    threading.Thread(target=refresh_loop, daemon=True).start()
    print(f"[nb-hours] listening on :{PORT} (tz {TZ_NAME}, coverage {COVERAGE})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

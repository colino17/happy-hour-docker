#!/usr/bin/env python3

import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
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

DDG_FALLBACK = os.environ.get("DDG_FALLBACK", "1").strip().lower() not in ("0", "no", "false", "off")
DDG_RADIUS_KM = float(os.environ.get("DDG_RADIUS_KM", "30"))
DDG_CACHE_TTL = 6 * 3600
DDG_CACHE_NEG_TTL = 30 * 60
_DDG_CACHE = {}
_DDG_GEOCODE = {}
_DDG_LOCK = threading.Lock()

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
    req = urllib.request.Request(url, headers={"User-Agent": "happy-hour/1.0"})
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
        print(f"[happy-hour] {s['slug']}: {len(places)} places", flush=True)
    return _dedupe(all_places), per_source

def refresh():
    os.makedirs(DATA_DIR, exist_ok=True)
    legacy = os.path.join(DATA_DIR, "extract.osm.pbf")
    if os.path.exists(legacy) and len(SOURCES) == 1 and not os.path.exists(SOURCES[0]["path"]):
        os.replace(legacy, SOURCES[0]["path"])
    for s in SOURCES:
        if not os.path.exists(s["path"]):
            print(f"[happy-hour] downloading {s['slug']} extract...", flush=True)
            download_extract(s["url"], s["path"])
    print(f"[happy-hour] parsing {len(SOURCES)} extract(s) for {COVERAGE}...", flush=True)
    places, per_source = build_combined()
    with INDEX_LOCK:
        INDEX.clear()
        INDEX.extend(places)
        INDEX_META.update(count=len(places), per_location=per_source,
                          built_at=datetime.now(LOCAL_TZ).isoformat())
    print(f"[happy-hour] indexed {len(places)} places with opening_hours", flush=True)

def refresh_loop():
    while True:
        time.sleep(REFRESH_SECONDS)
        try:
            print("[happy-hour] daily refresh: downloading fresh extract(s)...", flush=True)
            for s in SOURCES:
                download_extract(s["url"], s["path"])
            refresh()
        except Exception as e:  # keep serving the old index
            print(f"[happy-hour] refresh failed, keeping old index: {e}", flush=True)

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

_DDG_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_OSM_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_PROVINCES = {"NB", "NS", "PE", "NL", "QC", "ON", "MB", "SK", "AB", "BC", "YT", "NT", "NU"}

def _ddg_hours_to_osm(hours):
    if not isinstance(hours, dict):
        return None
    spans = []
    for day in _DDG_DAYS:
        raw = hours.get(day)
        if not raw or not isinstance(raw, str):
            spans.append(None)
            continue
        parts, ok = [], True
        for chunk in raw.split(","):
            chunk = chunk.strip().replace("\u2013", "-").replace("\u2014", "-")
            m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?-(\d{1,2}):(\d{2})(?::\d{2})?$", chunk)
            if not m:
                ok = False
                break
            parts.append(f"{int(m.group(1)):02d}:{m.group(2)}-{int(m.group(3)):02d}:{m.group(4)}")
        spans.append(",".join(parts) if ok and parts else None)
    if all(s is None for s in spans):
        return None
    out, i = [], 0
    while i < 7:
        if spans[i] is None:
            i += 1
            continue
        j = i
        while j + 1 < 7 and spans[j + 1] == spans[i]:
            j += 1
        label = _OSM_DAYS[i] if i == j else f"{_OSM_DAYS[i]}-{_OSM_DAYS[j]}"
        out.append(f"{label} {spans[i]}")
        i = j + 1
    return "; ".join(out)

def _ddg_place(res):
    hours = _ddg_hours_to_osm(res.get("hours"))
    if not hours:
        return None
    parts = [p.strip() for p in (res.get("address") or "").split(",")]
    street = parts[0] if parts and parts[0] else None
    city = None
    if len(parts) > 1:
        toks = [t for t in parts[1].split()
                if t not in _PROVINCES and not re.match(r"^[A-Z]\d[A-Z]\s?\d[A-Z]\d$", t)]
        city = " ".join(toks) or None
    coords = res.get("coordinates") or {}
    return {"name": res["name"], "opening_hours": hours, "city": city,
            "street": street, "postcode": None,
            "phone": res.get("phone") or None, "website": res.get("website") or None,
            "lat": coords.get("latitude"), "lon": coords.get("longitude"),
            "osm": None, "source": "duckduckgo", "ddg_source": res.get("url"),
            "score": 90}

def _ddg_relevant(q, title):
    qn, tn = q.strip().lower(), title.strip().lower()
    if not tn:
        return False
    if qn in tn or tn in qn:
        return True
    if fuzz.WRatio(qn, tn) >= 62:
        return True
    qtoks = {t for t in re.split(r"[^a-z0-9]+", qn) if len(t) > 2}
    ttoks = {t for t in re.split(r"[^a-z0-9]+", tn) if len(t) > 2}
    return bool(qtoks & ttoks) and fuzz.token_set_ratio(qn, tn) >= 55

def _ddg_bbox(city, origin):
    if origin is not None:
        dlat = DDG_RADIUS_KM * 0.008983
        dlon = dlat / max(0.2, cos(radians(origin[0])))
        return (origin[0] + dlat, origin[1] - dlon, origin[0] - dlat, origin[1] + dlon)
    place = city or COVERAGE
    with _DDG_LOCK:
        if place in _DDG_GEOCODE:
            return _DDG_GEOCODE[place]
    bbox = None
    try:
        url = "https://nominatim.openstreetmap.org/search?" + \
              urlencode({"q": place, "format": "jsonv2", "limit": "1"})
        req = urllib.request.Request(url, headers={"User-Agent": "happy-hour/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data and data[0].get("boundingbox"):
            bb = [float(x) for x in data[0]["boundingbox"]]
            bbox = (bb[1], bb[2], bb[0], bb[3])
    except Exception as e:
        print(f"[ddg] geocode failed for {place!r}: {e}", flush=True)
    with _DDG_LOCK:
        _DDG_GEOCODE[place] = bbox
    return bbox

def _ddg_query(q, city, origin):
    import primp
    bbox = _ddg_bbox(city, origin)
    if not bbox:
        return []
    lat_t, lon_l, lat_b, lon_r = bbox
    verify = os.environ.get("DDG_TLS_VERIFY", "1").strip().lower() not in ("0", "no", "false")
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
               "Referer": "https://duckduckgo.com/"}
    last_err = None
    for _attempt in range(2):
        try:
            try:
                client = primp.Client(headers=headers, verify=verify, timeout=12,
                                      impersonate=os.environ.get("DDG_IMPERSONATE", "chrome_146"))
            except Exception:
                client = primp.Client(headers=headers, verify=verify, timeout=12)
            r = client.get("https://duckduckgo.com/", params={"q": q})
            m = re.search(r"vqd=([\d-]+)", r.text) or re.search(r'"vqd":"([\d-]+)"', r.text)
            if not m:
                return []
            params = {"q": q, "vqd": m.group(1), "tg": "maps_places", "rt": "D",
                      "mkexp": "b", "wiki_info": "1", "is_requery": "1",
                      "bbox_tl": f"{lat_t},{lon_l}", "bbox_br": f"{lat_b},{lon_r}",
                      "strict_bbox": "1"}
            r = client.get("https://duckduckgo.com/local.js", params=params)
            data = json.loads(r.text)
            out, seen = [], set()
            for res in data.get("results", []):
                if not _ddg_relevant(q, res.get("name", "")):
                    continue
                p = _ddg_place(res)
                if p is None:
                    continue
                k = (p["name"].lower(), (p["street"] or "").lower())
                if k in seen:
                    continue
                seen.add(k)
                out.append(p)
            if origin is not None:
                for p in out:
                    d = distance_km(origin, p)
                    p["distance_km"] = round(d, 3) if d is not None else None
                out.sort(key=lambda p: (p["distance_km"] is None, p["distance_km"] or 0))
            return out
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise last_err

def ddg_search(q, city=None, origin=None, limit=5):
    if not DDG_FALLBACK:
        return []
    key = (q.strip().lower(), (city or "").lower(),
           tuple(round(c, 2) for c in origin) if origin else None)
    now = time.time()
    with _DDG_LOCK:
        hit = _DDG_CACHE.get(key)
        if hit and now - hit[0] < (DDG_CACHE_TTL if hit[1] else DDG_CACHE_NEG_TTL):
            return list(hit[1])
    try:
        places = _ddg_query(q, city, origin)[:limit]
    except Exception as e:
        print(f"[ddg] fallback failed for {q!r}: {e}", flush=True)
        places = []
    with _DDG_LOCK:
        _DDG_CACHE[key] = (now, places)
    if places:
        print(f"[ddg] fallback supplied {len(places)} place(s) for {q!r}", flush=True)
    return list(places)

def _strong_name(q, name):
    qn, nn = q.strip().lower(), name.strip().lower()
    return (qn in nn or nn in qn or fuzz.WRatio(qn, nn) >= 90
            or fuzz.token_set_ratio(qn, nn) >= 92)

def _ddg_needed(q, matches, origin):
    if not DDG_FALLBACK:
        return False
    if not matches:
        return True
    if not any(_strong_name(q, m["name"]) for m in matches):
        return True
    if origin is not None:
        dists = [m["distance_km"] for m in matches if m.get("distance_km") is not None]
        if dists and min(dists) > DDG_RADIUS_KM:
            return True
    return False

def _ddg_better(q, osm_matches, fb, origin):
    if not osm_matches:
        return True
    if not any(_strong_name(q, m["name"]) for m in osm_matches):
        return any(_strong_name(q, p["name"]) for p in fb)
    if origin is not None:
        fd = [p["distance_km"] for p in fb if p.get("distance_km") is not None]
        return (bool(fd) and min(fd) <= DDG_RADIUS_KM
                and any(_strong_name(q, p["name"]) for p in fb))
    return False

def answer(q, city=None, at=None, origin=None):
    when = now_local(at)
    matches = search(q, city=city, limit=25 if origin is not None else 5, origin=origin)
    source = "osm"
    if _ddg_needed(q, matches, origin):
        fb = ddg_search(q, city=city, origin=origin)
        if fb and _ddg_better(q, matches, fb, origin):
            matches, source = fb, "duckduckgo"
    if not matches:
        return {"found": False,
                "speech": f"I couldn't find any business called {q} with hours listed in {COVERAGE}."}
    if origin is not None:
        best = max(m["score"] for m in matches)
        eligible = [m for m in matches if m["score"] >= best - 7]
        chosen = eligible[:1]
        return {"found": True, "speech": describe(chosen[0], when), "source": source,
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
    return {"found": True, "speech": speech, "when": when.isoformat(), "source": source,
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
            self.send_json({"ok": True, "default_origin": DEFAULT_ORIGIN,
                            "ddg_fallback": DDG_FALLBACK, **INDEX_META})
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
    print(f"[happy-hour] listening on :{PORT} (tz {TZ_NAME}, coverage {COVERAGE})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

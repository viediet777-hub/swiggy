#!/usr/bin/env python3
"""
Swiggy Offer Finder - shared core.

Used by BOTH:
    server.py          -> local dev server (python server.py)
    api/*.py           -> Vercel serverless functions (same logic)

Endpoints (all GET):
    /                                -> public/index.html   (local only; Vercel serves it)
    /api/health                      -> {ok, time}          (no access key needed)
    /api/geocode?q=                  -> OSM Nominatim search
    /api/reverse?lat=&lng=           -> OSM reverse label
    /api/iplocate                    -> IP based city/coords
    /api/offers?lat=&lng=&ring=0..3  -> restaurants + biggest deals
    /api/offers?lat=&lng=&fetch=N    -> one-shot scan

Access key (optional):
    Set env var OFFER_KEY. When set, every endpoint except /api/health
    requires ?key=<value> (or header x-offer-key).
    Local dev without OFFER_KEY = no gate.
"""

import gzip
import hmac
import io
import json
import math
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
SWIGGY_LISTING = "https://www.swiggy.com/dapi/restaurants/list/v5"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REV = "https://nominatim.openstreetmap.org/reverse"
IP_API = "http://ip-api.com/json/"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
UA_OSM = "swiggy-offer-finder/2.0 (local dev tool)"

RINGS = [
    [(0.0, 1), (0.007, 4)],
    [(0.016, 8)],
    [(0.027, 12)],
    [(0.040, 14)],
]

_geo_cache = {}
_rev_cache = {}
_ring_cache = {}
_cache_lock = threading.Lock()
RING_TTL = 600


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- Swiggy

def _app_headers(lat, lng):
    return {
        "pl-version": "138",
        "version-code": "1795",
        "app-version": "4.113.0",
        "os-version": "11",
        "latitude": str(lat),
        "longitude": str(lng),
        "current-latitude": str(lat),
        "current-longitude": str(lng),
        "accessibility_enabled": "false",
        "x-network-quality": "GOOD",
        "faw-flags": "1354",
        "accept": "application/json; charset=utf-8",
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "user-agent": "Swiggy-Android",
        "deviceid": str(uuid.uuid4()).upper(),
        "swuid": "SW-" + uuid.uuid4().hex[:12].upper(),
    }


def _browser_headers():
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-IN,en;q=0.9",
        "referer": "https://www.swiggy.com/",
        "user-agent": UA_BROWSER,
    }


def swiggy_fetch(lat, lng, mode="app", retries=3):
    url = "%s?lat=%s&lng=%s" % (SWIGGY_LISTING, lat, lng)
    if mode == "web":
        url += "&is-seo-homepage-enabled=true&page_type=DESKTOP_WEB_LISTING"
    headers = _app_headers(lat, lng) if mode == "app" else _browser_headers()
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=25)
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            if r.status == 200 and raw:
                return json.loads(raw.decode("utf-8", "replace"))
            last = "HTTP %s" % r.status
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        time.sleep(1.2 * (attempt + 1) + random.uniform(0, 0.6))
    log("  swiggy %s fetch failed: %s" % (mode, last))
    return None


def iter_restaurants(node, depth=0):
    if depth > 14:
        return
    if isinstance(node, dict):
        info = node.get("info")
        if (isinstance(info, dict) and "name" in info and "id" in info
                and ("cloudinaryImageId" in info or "cuisines" in info)):
            yield info, node.get("cta")
        for v in node.values():
            for r in iter_restaurants(v, depth + 1):
                yield r
    elif isinstance(node, list):
        for v in node:
            for r in iter_restaurants(v, depth + 1):
                yield r


PCT_RE = re.compile(r"(\d{1,3})\s*%", re.I)
MAXOFF_RE = re.compile(r"(?:upto|up to|max)\s*₹?\s*(\d{1,5})", re.I)
CODE_RE = re.compile(r"use\s+([A-Za-z0-9_\-]{3,})\b", re.I)
FLAT_RE = re.compile(r"₹?\s*(\d{2,4})\s*(?:store|only)", re.I)
UNDER_RE = re.compile(r"(?:under|items at|at)\s+₹?\s*(\d{2,4})", re.I)
FLATOFF_RE = re.compile(r"(?:flat\s*)?₹\s*(\d{2,4})\s*off", re.I)
ABOVE_RE = re.compile(r"above\s*₹?\s*(\d{2,5})", re.I)
NUM_RE = re.compile(r"(\d{2,5})")


def parse_offer(info):
    agg = info.get("aggregatedDiscountInfoV2") or {}
    if not isinstance(agg, dict) or not agg:
        agg = info.get("aggregatedDiscountInfoV3") or {}
    if not isinstance(agg, dict) or not agg:
        return None

    metas = []
    for key in ("shortDescriptionList", "descriptionList"):
        for item in (agg.get(key) or []):
            if isinstance(item, dict) and item.get("meta"):
                metas.append((item["meta"], item.get("discountType") or ""))

    header = (agg.get("header") or "").strip()
    sub = (agg.get("subHeader") or "").strip()
    texts = ([header] if header else []) + [m[0] for m in metas]

    pres = info.get("restaurantOfferPresentationInfo")
    if isinstance(pres, dict):
        for item in (pres.get("offerStrings") or []):
            if isinstance(item, str) and item.strip():
                texts.append(item.strip())

    if not texts:
        return None

    pct = 0
    for t in texts:
        for m in PCT_RE.finditer(t):
            pct = max(pct, min(int(m.group(1)), 90))
        if pct:
            break

    flat_off = 0
    if not pct:
        for t in texts + ([sub] if sub else []):
            m = FLATOFF_RE.search(t)
            if m:
                flat_off = int(m.group(1))
                break

    price = 0
    if not pct and not flat_off:
        for t in texts:
            m = FLAT_RE.search(t) or UNDER_RE.search(t)
            if m:
                price = int(m.group(1))
                break

    cap = 0
    for t in texts:
        m = MAXOFF_RE.search(t)
        if m:
            cap = max(cap, int(m.group(1)))
    no_cap = any("no upper limit" in t.lower() for t in texts)

    min_order = 0
    for t in texts + ([sub] if sub else []):
        m = ABOVE_RE.search(t)
        if m:
            min_order = int(m.group(1))
            break

    code = ""
    for t in texts:
        m = CODE_RE.search(t)
        if m:
            cand = m.group(1).upper()
            if cand not in ("CODE", "PROMO", "COUPON", "COUPONS"):
                code = cand
                break

    display = header or texts[0]
    if pct:
        kind = "percent"
    elif flat_off:
        kind = "flatoff"
    elif price:
        kind = "flat"
    else:
        kind = "other"
    return {
        "discount_pct": pct,
        "flat_value": price,
        "flat_off": flat_off,
        "kind": kind,
        "max_off": cap,
        "no_cap": no_cap,
        "min_order": min_order,
        "code": code,
        "text": display,
        "sub": sub,
        "all": list(dict.fromkeys(texts))[:4],
    }


def image_url(cloud_id):
    if not cloud_id:
        return ""
    cid = str(cloud_id)
    if cid.startswith("http"):
        return cid
    return ("https://media-assets.swiggy.com/swiggy/image/upload/"
            "fl_lossy,f_auto,q_auto,w_480,h_300,c_fill/" + cid)


def _num(s, default=0):
    m = NUM_RE.search(str(s or ""))
    return int(m.group(1)) if m else default


def _rating_val(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def deal_metrics(offer, cost_for_two, rating):
    order = cost_for_two or 300
    save = 0
    if offer:
        k = offer["kind"]
        if k == "percent":
            save = offer["discount_pct"] / 100.0 * order
            if offer["max_off"] and not offer["no_cap"]:
                save = min(save, offer["max_off"])
        elif k == "flatoff":
            save = offer["flat_off"] if order >= offer.get("min_order", 0) else offer["flat_off"] * 0.6
        elif k == "flat":
            save = max(order * 0.22, 40)
        else:
            save = 25
    save = int(round(save))
    pct = (offer or {}).get("discount_pct", 0)
    score = 0.0
    if offer:
        score = 100 + save * 1.0 + pct * 1.3 + max(0.0, rating - 3.6) * 25
    else:
        score = max(0.0, rating - 3.0) * 5
    return save, round(score, 2)


def _build_row(info, offer, cta=None):
    rid = str(info.get("id"))
    cost = _num(info.get("costForTwo") or info.get("costForTwoMessage"))
    if cost > 5000:
        cost = cost // 100
    rating = _rating_val(info.get("avgRatingString") or info.get("avgRating"))
    save, score = deal_metrics(offer, cost, rating)
    sla = info.get("sla") or {}
    link = ""
    if isinstance(cta, dict) and str(cta.get("link") or "").startswith("https://www.swiggy.com/"):
        link = cta["link"]
    avail = info.get("availability") or {}
    return {
        "id": rid,
        "name": info.get("name") or "",
        "area": info.get("areaName") or info.get("locality") or "",
        "locality": info.get("locality") or "",
        "rating": info.get("avgRatingString") or info.get("avgRating") or "",
        "rating_num": rating,
        "ratings_total": info.get("totalRatingsString") or "",
        "delivery_mins": sla.get("deliveryTime") or "",
        "distance": sla.get("lastMileTravelString") or "",
        "cost_for_two": info.get("costForTwo") or "",
        "cost_num": cost,
        "cuisines": ", ".join((info.get("cuisines") or [])[:4]),
        "cuisine_list": (info.get("cuisines") or [])[:6],
        "veg": bool(info.get("veg")),
        "open": avail.get("opened", True) is not False,
        "image": image_url(info.get("cloudinaryImageId")),
        "menu_url": link or ("https://www.swiggy.com/menu/%s" % rid),
        "offer": offer,
        "save": save,
        "score": score,
    }


def _add(rows_by_id, row):
    prev = rows_by_id.get(row["id"])
    if prev is None:
        rows_by_id[row["id"]] = row
    elif row["offer"] and (prev["offer"] is None or _better(row["offer"], prev["offer"])):
        if prev["menu_url"] and "/restaurants/" in prev["menu_url"]:
            row["menu_url"] = prev["menu_url"]
        rows_by_id[row["id"]] = row
    elif "/restaurants/" in row["menu_url"] and "/restaurants/" not in prev["menu_url"]:
        prev["menu_url"] = row["menu_url"]


def _score(r):
    o = r.get("offer") or {}
    return (1 if o else 0, r.get("score", 0), o.get("discount_pct", 0),
            o.get("max_off", 0), _rating(r))


def ring_points(lat, lng, ring):
    pts = []
    coslat = max(0.2, math.cos(math.radians(lat)))
    for radius, n in RINGS[ring]:
        if radius == 0:
            pts.append((round(lat, 6), round(lng, 6)))
            continue
        off = random.uniform(0, math.pi)
        for k in range(n):
            a = off + 2 * math.pi * k / n
            pts.append((round(lat + radius * math.cos(a), 6),
                        round(lng + radius * math.sin(a) / coslat, 6)))
    return pts


def _scan(points, web_points=None):
    web_points = points if web_points is None else web_points
    tasks = [(a, b, "web") for a, b in web_points] + [(a, b, "app") for a, b in points]
    random.shuffle(tasks)
    rows_by_id = {}
    ok_web = ok_app = fail = 0

    def _one(t):
        return t[2], swiggy_fetch(t[0], t[1], mode=t[2], retries=2)

    with ThreadPoolExecutor(max_workers=5) as pool:
        for mode, data in pool.map(_one, tasks):
            if not data:
                fail += 1
                continue
            if mode == "web":
                ok_web += 1
                for info, cta in iter_restaurants(data):
                    _add(rows_by_id, _build_row(info, None, cta))
            else:
                ok_app += 1
                for info, cta in iter_restaurants(data):
                    _add(rows_by_id, _build_row(info, parse_offer(info), cta))
    return rows_by_id, ok_web, ok_app, fail


def _finish(rows_by_id, ok_web, ok_app, fail):
    by_place = {}
    for row in rows_by_id.values():
        key = (row["name"].strip().lower(), row["area"].strip().lower())
        prev = by_place.get(key)
        if prev is None or _score(row) > _score(prev):
            by_place[key] = row

    rows = list(by_place.values())
    rows.sort(key=_score, reverse=True)
    stats = {
        "total": len(rows),
        "with_offer": sum(1 for r in rows if r["offer"]),
        "with_pct": sum(1 for r in rows if r["offer"] and r["offer"]["discount_pct"]),
        "web_rounds": ok_web,
        "app_rounds": ok_app,
        "failed": fail,
    }
    return rows, stats


def collect(lat, lng, fetch=3):
    fetch = max(1, min(int(fetch), 6))
    clat, clng = float(lat), float(lng)
    points = [(clat, clng)]
    for _ in range(fetch - 1):
        points.append((round(clat + random.uniform(-0.015, 0.015), 6),
                       round(clng + random.uniform(-0.015, 0.015), 6)))
    return _finish(*_scan(points, points[:2]))


def collect_ring(lat, lng, ring):
    ring = max(0, min(int(ring), len(RINGS) - 1))
    key = (round(lat, 3), round(lng, 3), ring)
    now = time.time()
    with _cache_lock:
        hit = _ring_cache.get(key)
        if hit and now - hit[0] < RING_TTL:
            return hit[1], dict(hit[2], cached=True)
    rows, stats = _finish(*_scan(ring_points(lat, lng, ring)))
    if rows:
        with _cache_lock:
            _ring_cache[key] = (now, rows, stats)
            if len(_ring_cache) > 300:
                for k in sorted(_ring_cache, key=lambda k: _ring_cache[k][0])[:100]:
                    _ring_cache.pop(k, None)
    return rows, stats


def _rating(r):
    try:
        return float(r.get("rating") or 0)
    except (TypeError, ValueError):
        return 0.0


def _better(a, b):
    return (a.get("discount_pct", 0), a.get("flat_off", 0), a.get("max_off", 0)) > \
           (b.get("discount_pct", 0), b.get("flat_off", 0), b.get("max_off", 0))


# ---------------------------------------------------------------- Geocode

def _osm_get(url):
    req = urllib.request.Request(url, headers={
        "user-agent": UA_OSM,
        "accept": "application/json",
        "accept-language": "en-IN,en;q=0.9",
    })
    r = urllib.request.urlopen(req, timeout=20)
    return json.loads(r.read().decode("utf-8", "replace"))


def geocode(query, limit=6):
    key = query.strip().lower()
    with _cache_lock:
        if key in _geo_cache:
            return _geo_cache[key]

    qs = urllib.parse.urlencode({
        "format": "jsonv2", "limit": limit, "countrycodes": "in", "q": query,
    })
    out = []
    try:
        for item in _osm_get(NOMINATIM + "?" + qs):
            full = item.get("display_name") or ""
            parts = [p.strip() for p in full.split(",")]
            out.append({
                "lat": item.get("lat"),
                "lon": item.get("lon"),
                "name": full[:140],
                "short": ", ".join(parts[:2]),
                "sub": ", ".join(parts[2:5]),
            })
    except Exception as e:
        log("  geocode error: %s" % e)

    if out:
        with _cache_lock:
            _geo_cache[key] = out
    return out


def reverse(lat, lng):
    key = (round(lat, 3), round(lng, 3))
    with _cache_lock:
        if key in _rev_cache:
            return _rev_cache[key]
    qs = urllib.parse.urlencode({"format": "jsonv2", "lat": lat, "lon": lng, "zoom": 16})
    out = {"label": "", "city": ""}
    try:
        d = _osm_get(NOMINATIM_REV + "?" + qs)
        a = d.get("address") or {}
        area = (a.get("suburb") or a.get("neighbourhood") or a.get("quarter")
                or a.get("residential") or a.get("road") or "")
        city = (a.get("city") or a.get("town") or a.get("state_district")
                or a.get("county") or a.get("village") or "")
        out = {"label": ", ".join([x for x in (area, city) if x]) or
               (d.get("display_name") or "")[:60], "city": city}
    except Exception as e:
        log("  reverse error: %s" % e)
    if out["label"]:
        with _cache_lock:
            _rev_cache[key] = out
    return out


def ip_locate(client_ip=""):
    ip = client_ip
    if not ip or ip.startswith(("127.", "10.", "192.168.", "172.", "::1", "localhost")):
        ip = ""
    url = IP_API + ip + "?fields=status,lat,lon,city,regionName,country"
    try:
        req = urllib.request.Request(url, headers={"user-agent": UA_OSM})
        d = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        if d.get("status") == "success":
            return {"ok": True, "lat": d["lat"], "lng": d["lon"],
                    "city": d.get("city") or "", "region": d.get("regionName") or ""}
    except Exception as e:
        log("  iplocate error: %s" % e)
    return {"ok": False}


# ---------------------------------------------------------------- Access key

def required_key():
    return (os.environ.get("OFFER_KEY") or "").strip()


def find_index():
    for cand in (os.path.join(HERE, "public", "index.html"),
                 os.path.join(HERE, "index.html"),
                 os.path.join(os.getcwd(), "public", "index.html"),
                 os.path.join(os.getcwd(), "index.html")):
        if os.path.isfile(cand):
            return cand
    return ""


def _pq(raw):
    p = urllib.parse.urlparse(raw)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
    return p.path, q


def _endpoint(path):
    p = path.strip("/")
    if p in ("", "index.html", "index"):
        return ""
    if p.startswith("api/"):
        p = p[4:]
    return p.split("/")[0].lower()


class BaseHandler(BaseHTTPRequestHandler):
    server_version = "SwiggyOfferFinder/2.1"
    endpoint = None          # set by Vercel function subclasses

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _latlng(self, q):
        try:
            flat, flng = float(q.get("lat")), float(q.get("lng"))
        except (TypeError, ValueError):
            self._json(400, {"error": "lat and lng (numbers) required"})
            return None
        if not (-90 <= flat <= 90 and -180 <= flng <= 180):
            self._json(400, {"error": "lat/lng out of range"})
            return None
        return flat, flng

    def _allowed(self, q):
        need = required_key()
        if not need:
            return True
        got = (q.get("key") or "").strip() or (self.headers.get("x-offer-key") or "").strip()
        if got and hmac.compare_digest(got, need):
            return True
        self._json(403, {
            "ok": False,
            "need_key": True,
            "error": "Access key chahiye. Link ke saath ?key=YOUR_KEY lagao.",
        })
        return False

    def _index(self):
        fpath = find_index()
        if not fpath:
            self._send(404, b"index.html not found", "text/plain; charset=utf-8")
            return
        with open(fpath, "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def _ep(self, path):
        return self.endpoint or _endpoint(path)

    def do_GET(self):
        try:
            path, q = _pq(self.path)
            ep = self._ep(path)

            if ep == "":
                self._index()
                return

            if ep == "health":
                self._json(200, {"ok": True, "time": int(time.time())})
                return

            if not self._allowed(q):
                return

            if ep == "geocode":
                text = (q.get("q") or "").strip()
                if len(text) < 2:
                    self._json(400, {"error": "q (min 2 chars) required"})
                    return
                self._json(200, {"query": text, "results": geocode(text)})
                return

            if ep == "reverse":
                ll = self._latlng(q)
                if ll:
                    self._json(200, reverse(*ll))
                return

            if ep == "iplocate":
                fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                self._json(200, ip_locate(fwd or self.client_address[0]))
                return

            if ep == "offers":
                self._offers(q)
                return

            self._json(404, {"error": "not found", "path": path})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            log("handler error: %r" % e)
            try:
                self._json(500, {"error": str(e)[:200]})
            except Exception:
                pass

    def _offers(self, q):
        ll = self._latlng(q)
        if not ll:
            return
        flat, flng = ll
        t0 = time.time()
        ring = q.get("ring")
        if ring is not None and ring != "":
            try:
                ring = int(ring)
            except ValueError:
                ring = 0
            rows, stats = collect_ring(flat, flng, ring)
        else:
            try:
                fetch = int(q.get("fetch") or 3)
            except ValueError:
                fetch = 3
            rows, stats = collect(flat, flng, fetch)
        rounds = stats["web_rounds"] + stats["app_rounds"]
        if not rows and rounds == 0 and not stats.get("cached"):
            self._json(503, {
                "ok": False,
                "error": "Swiggy se data nahi mila (rate limit / block ho gaya). "
                         "10 second baad dobara try karo.",
                "took_ms": int((time.time() - t0) * 1000),
                "stats": stats,
            })
            return
        best = next((r["offer"] for r in rows
                     if r["offer"] and r["offer"]["discount_pct"]), None)
        if best is None:
            best = next((r["offer"] for r in rows if r["offer"]), None)
        self._json(200, {
            "ok": True,
            "source": "swiggy",
            "lat": flat,
            "lng": flng,
            "ring": ring,
            "rings_total": len(RINGS),
            "label": q.get("label") or "",
            "rounds_ok": rounds,
            "count": len(rows),
            "with_offer": stats["with_offer"],
            "with_pct": stats["with_pct"],
            "stats": stats,
            "best": best,
            "took_ms": int((time.time() - t0) * 1000),
            "offers": rows,
        })


def _selftest():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    assert _endpoint("/api/offers") == "offers"
    assert _endpoint("/") == ""
    assert _endpoint("/api/") == ""
    rows, stats = collect(22.7196, 75.8577, 1)
    log("collect -> %d rows, %d offers, stats=%s" % (len(rows), stats["with_offer"], stats))
    log("geocode -> %d" % len(geocode("Indore")))
    log("index   -> %s" % (find_index() or "MISSING"))


if __name__ == "__main__":
    _selftest()

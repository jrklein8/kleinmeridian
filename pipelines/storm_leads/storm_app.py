"""
Roofing Storm-Lead Map - backend.

Pulls NWS Local Storm Reports (LSR) from the Iowa Environmental Mesonet (IEM),
keeps only roof-relevant events (hail / damaging wind / tornado) within a drive
radius of Wilmington, NC, scores each report by how much roof damage it implies,
spreads that score over a small influence radius to build graded "damage zones,"
and returns everything the front-end needs to draw a marketing-target map.

Data source (free, no API key):
  https://mesonet.agron.iastate.edu/geojson/lsr.geojson
"""

import os
import json
import math
import time
import datetime as dt
from typing import Optional

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from shapely.geometry import shape, Point, mapping
from shapely.strtree import STRtree

import mesh as mesh_mod

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Wilmington, NC city center
CENTER_LAT = 34.2257
CENTER_LON = -77.9447

IEM_URL = "https://mesonet.agron.iastate.edu/geojson/lsr.geojson"
IEM_SBW_URL = "https://mesonet.agron.iastate.edu/geojson/sbw.geojson"

# Census TIGERweb boundary layers used as the "neighborhood" grading unit.
TIGERWEB_BASE = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
BOUNDARY_SOURCES = {
    "zip": {
        "label": "ZIP code",
        "url": f"{TIGERWEB_BASE}/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/11/query",
        "offset": "0.004",      # generalize ~400m
    },
    "bg": {
        "label": "block group",
        "url": f"{TIGERWEB_BASE}/tigerWMS_Current/MapServer/10/query",
        "offset": "0.0018",     # block groups are small; keep more detail
    },
}

# NWS offices whose warnings can reach the Wilmington drive radius
WARN_WFOS = "ILM,MHX,RAH,CAE"
# Storm-based warning types worth chasing for roof leads (severe tstorm + tornado)
WARN_PHENOMENA = {"SV", "TO"}

MAX_RADIUS_MI = 100          # largest radius the UI can request
BOX_PAD_MI = 10              # pad the IEM bounding box beyond the radius
CACHE_TTL_SECONDS = 900      # re-pull IEM at most every 15 minutes
FULL_WINDOW_DAYS = 185       # we always pull ~6 months and filter down in memory

# Damage-zone kernel (how a single report's score spreads over the map)
KERNEL_BANDWIDTH_MI = 5.0
KERNEL_CUTOFF_MI = 11.0
GRID_CELL_MI = 2.0

# Grade thresholds applied to a grid cell's blended score (point reports only).
# (color, label) ordered HIGH -> LOW; first threshold that the score clears wins.
GRADE_BANDS = [
    (14.0, "High",     "#bd0026"),
    (9.0,  "Med-High", "#f03b20"),
    (5.0,  "Medium",   "#fd8d3c"),
    (2.5,  "Low-Med",  "#feb24c"),
    (1.0,  "Low",      "#fed976"),
]

# Higher thresholds for ZIP-area scores, which accumulate many warning swaths over
# the window (so "High" stays meaningful = repeatedly or severely hit).
ZCTA_GRADE_BANDS = [
    (35.0, "High",     "#bd0026"),
    (20.0, "Med-High", "#f03b20"),
    (10.0, "Medium",   "#fd8d3c"),
    (4.0,  "Low-Med",  "#feb24c"),
    (1.5,  "Low",      "#fed976"),
]

# Time windows the UI exposes (label key -> days back from now)
WINDOWS = {
    "yesterday": 2,    # last ~48h ("yesterday" / freshest leads)
    "1w": 7,
    "1m": 30,
    "6m": 180,
}
WINDOW_LABELS = {
    "yesterday": "Yesterday (last 48h)",
    "1w": "Past 1 week",
    "1m": "Past 1 month",
    "6m": "Past 6 months",
}

app = FastAPI(title="Roofing Storm-Lead Map")


@app.middleware("http")
async def no_store(request, call_next):
    """Keep the browser from caching the static front-end during iteration."""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

# --------------------------------------------------------------------------- #
# Geo helpers
# --------------------------------------------------------------------------- #

def haversine_mi(lat1, lon1, lat2, lon2):
    """Great-circle distance in statute miles."""
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def mi_to_lat_deg(mi):
    return mi / 69.0


def mi_to_lon_deg(mi, lat):
    return mi / (69.0 * max(0.1, math.cos(math.radians(lat))))


# --------------------------------------------------------------------------- #
# Report classification & damage weighting
# --------------------------------------------------------------------------- #

def classify(typetext: str) -> Optional[str]:
    """Reduce an LSR typetext to a roof-relevant category, or None to drop it."""
    t = (typetext or "").upper()
    if "MARINE" in t:          # offshore / over-water -> no roofs to market to
        return None
    if "HAIL" in t:
        return "hail"
    if "TORNADO" in t:
        return "tornado"
    if any(k in t for k in ("WND", "WIND", "GUST", "DOWNBURST", "MICROBURST")):
        return "wind"
    return None                # rain, snow, flood, freezing rain, etc.


def to_mph(magf: Optional[float], unit: str) -> Optional[float]:
    if magf is None:
        return None
    u = (unit or "").upper()
    if "K" in u and "MPH" not in u:    # knots
        return magf * 1.15078
    return magf                         # already mph (LSR wind is usually mph)


def hail_size_weight(s: Optional[float]) -> float:
    """Roof-damage weight from hail diameter in inches (1"+ is the claim threshold)."""
    s = s or 0.0
    if s >= 2.0:
        return 11.0
    if s >= 1.5:
        return 7.0
    if s >= 1.0:        # quarter-size: classic roof-damage threshold
        return 4.0
    if s >= 0.75:
        return 2.0
    if s > 0:
        return 1.0
    return 0.0


def damage_weight(cat: str, hail_in: Optional[float], wind_mph: Optional[float], typetext: str) -> float:
    """Score a single report by how much roof damage / claim potential it implies."""
    t = (typetext or "").upper()
    if cat == "tornado":
        return 12.0
    if cat == "hail":
        return max(1.0, hail_size_weight(hail_in))
    if cat == "wind":
        damage_report = any(k in t for k in ("DMG", "DAMAGE", "TREES", "ROOF"))
        m = wind_mph or 0.0
        if m >= 85:
            w = 9.0
        elif m >= 70:
            w = 6.0
        elif m >= 58:       # NWS severe-wind threshold
            w = 4.0
        elif m >= 50:
            w = 2.0
        elif m > 0:
            w = 1.0
        else:
            w = 0.0
        if damage_report:
            w = max(w, 5.0)
        if w == 0.0:        # reported wind w/ no measure but flagged worth noting
            w = 3.0
        return w
    return 0.0


def warning_intensity(ph, hail_in, wind_mph, tornadotag, damagetag):
    """Roof-damage weight for a severe/tornado warning polygon, from its IBW tags."""
    dmg = (damagetag or "").upper()
    if ph == "TO":                      # tornado warning
        w = 8.0
        if (tornadotag or "").upper() in ("OBSERVED", "RADAR INDICATED"):
            w = 10.0 if "OBSERVED" not in (tornadotag or "").upper() else 14.0
        if dmg == "CONSIDERABLE":
            w = max(w, 14.0)
        if dmg == "CATASTROPHIC":
            w = 20.0
        return w
    # severe thunderstorm warning: base "this area saw a severe storm" + tag bumps
    w = 1.5
    h = hail_in or 0.0
    if h >= 2.0:
        w += 8.0
    elif h >= 1.5:
        w += 5.0
    elif h >= 1.0:
        w += 3.0
    elif h >= 0.75:
        w += 1.0
    m = wind_mph or 0.0
    if m >= 80:
        w += 5.0
    elif m >= 70:
        w += 3.0
    elif m >= 60:
        w += 1.5
    if dmg == "CONSIDERABLE":
        w += 3.0
    elif dmg == "DESTRUCTIVE":
        w += 6.0
    return w


# --------------------------------------------------------------------------- #
# IEM fetch + cache
# --------------------------------------------------------------------------- #

_cache = {"ts": 0.0, "reports": None, "error": None}
_warn_cache = {"ts": 0.0, "warnings": None, "error": None}
_boundary_cache = {}   # unit -> {"features": [...], "error": str|None}


def _box():
    pad = MAX_RADIUS_MI + BOX_PAD_MI
    dlat = mi_to_lat_deg(pad)
    dlon = mi_to_lon_deg(pad, CENTER_LAT)
    return (CENTER_LON - dlon, CENTER_LON + dlon, CENTER_LAT + dlat, CENTER_LAT - dlat)


def fetch_reports(force: bool = False):
    """Pull ~6 months of roof-relevant LSRs in the max bounding box (cached)."""
    now = time.time()
    if not force and _cache["reports"] is not None and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
        return _cache["reports"]

    west, east, north, south = _box()
    ets = dt.datetime.now(dt.timezone.utc)
    sts = ets - dt.timedelta(days=FULL_WINDOW_DAYS)
    params = {
        "sts": sts.strftime("%Y-%m-%dT%H:%MZ"),
        "ets": ets.strftime("%Y-%m-%dT%H:%MZ"),
        "west": west, "east": east, "north": north, "south": south,
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(IEM_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # network / parse failure -> keep stale cache if any
        _cache["error"] = str(exc)
        if _cache["reports"] is not None:
            return _cache["reports"]
        return []

    reports = []
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        cat = classify(p.get("typetext"))
        if cat is None:
            continue
        lat = p.get("lat")
        lon = p.get("lon")
        if lat is None or lon is None:
            coords = (feat.get("geometry") or {}).get("coordinates") or [None, None]
            lon, lat = coords[0], coords[1]
        if lat is None or lon is None:
            continue

        magf = p.get("magf")
        if magf is None and p.get("magnitude") not in (None, "", "M"):
            try:
                magf = float(p.get("magnitude"))
            except (TypeError, ValueError):
                magf = None
        unit = p.get("unit") or ""
        hail_in = magf if cat == "hail" else None
        wind_mph = to_mph(magf, unit) if cat == "wind" else None

        reports.append({
            "lat": float(lat),
            "lon": float(lon),
            "cat": cat,
            "typetext": p.get("typetext") or cat.upper(),
            "hail_in": round(hail_in, 2) if hail_in else None,
            "wind_mph": round(wind_mph) if wind_mph else None,
            "city": p.get("city") or "",
            "county": p.get("county") or "",
            "state": p.get("st") or p.get("state") or "",
            "valid": p.get("valid") or "",
            "remark": p.get("remark") or "",
            "weight": damage_weight(cat, hail_in, wind_mph, p.get("typetext") or ""),
        })

    _cache.update(ts=now, reports=reports, error=None)
    return reports


def fetch_warnings(force: bool = False):
    """Pull ~6 months of severe/tornado warning polygons (cached) with IBW tags."""
    now = time.time()
    if not force and _warn_cache["warnings"] is not None and (now - _warn_cache["ts"]) < CACHE_TTL_SECONDS:
        return _warn_cache["warnings"]

    ets = dt.datetime.now(dt.timezone.utc)
    sts = ets - dt.timedelta(days=FULL_WINDOW_DAYS)
    params = {
        "sts": sts.strftime("%Y-%m-%dT%H:%MZ"),
        "ets": ets.strftime("%Y-%m-%dT%H:%MZ"),
        "wfos": WARN_WFOS,
    }
    try:
        with httpx.Client(timeout=45.0) as client:
            resp = client.get(IEM_SBW_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        _warn_cache["error"] = str(exc)
        if _warn_cache["warnings"] is not None:
            return _warn_cache["warnings"]
        return []

    warnings = []
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        ph = p.get("phenomena")
        if ph not in WARN_PHENOMENA:        # drop marine, flood, etc.
            continue
        try:
            geom = shape(feat.get("geometry"))
            if not geom.is_valid:
                geom = geom.buffer(0)
            if geom.is_empty:
                continue
        except Exception:
            continue
        hail_in = p.get("hailtag")
        wind_mph = p.get("windtag")       # land SV/TO tags are MPH
        try:
            hail_in = float(hail_in) if hail_in not in (None, "") else None
        except (TypeError, ValueError):
            hail_in = None
        try:
            wind_mph = float(wind_mph) if wind_mph not in (None, "") else None
        except (TypeError, ValueError):
            wind_mph = None
        warnings.append({
            "geom": geom,
            "bounds": geom.bounds,           # (minx, miny, maxx, maxy)
            "area": geom.area,
            "ph": ph,
            "ps": p.get("ps") or ("Tornado Warning" if ph == "TO" else "Severe Thunderstorm Warning"),
            "hail_in": hail_in,
            "wind_mph": wind_mph,
            "tornadotag": p.get("tornadotag"),
            "damagetag": p.get("damagetag"),
            "issue": p.get("issue") or "",
            "expire": p.get("expire") or "",
            "intensity": warning_intensity(ph, hail_in, wind_mph, p.get("tornadotag"), p.get("damagetag")),
        })

    _warn_cache.update(ts=now, warnings=warnings, error=None)
    return warnings


def fetch_boundaries(unit: str = "zip", force: bool = False):
    """Pull neighborhood boundaries (ZIP ZCTAs or Census block groups) from TIGERweb.

    Cached for the process lifetime (boundaries change rarely). Paginates so the
    block-group layer (hundreds of polygons) comes back complete.
    """
    src = BOUNDARY_SOURCES.get(unit, BOUNDARY_SOURCES["zip"])
    cached = _boundary_cache.get(unit)
    if not force and cached is not None and cached.get("features") is not None:
        return cached["features"]

    west, east, north, south = _box()
    base_params = {
        "where": "1=1",
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "BASENAME,GEOID",
        "returnGeometry": "true",
        "maxAllowableOffset": src["offset"],
        "geometryPrecision": "4",
        "f": "geojson",
        "resultRecordCount": "1000",
    }

    features = []
    offset = 0
    try:
        with httpx.Client(timeout=60.0) as client:
            while True:
                params = dict(base_params, resultOffset=str(offset))
                resp = client.get(src["url"], params=params)
                resp.raise_for_status()
                data = resp.json()
                page = data.get("features", [])
                for feat in page:
                    p = feat.get("properties", {})
                    try:
                        geom = shape(feat.get("geometry"))
                        if not geom.is_valid:
                            geom = geom.buffer(0)
                        if geom.is_empty:
                            continue
                    except Exception:
                        continue
                    features.append({
                        "id": str(p.get("GEOID") or p.get("BASENAME") or "").strip(),
                        "short": str(p.get("BASENAME") or "").strip(),
                        "geom": geom,
                        "area": geom.area,
                    })
                if len(page) < 1000:
                    break
                offset += 1000
                if offset > 20000:       # safety stop
                    break
    except Exception as exc:
        _boundary_cache[unit] = {"features": features or None, "error": str(exc)}
        return features or (cached or {}).get("features") or []

    _boundary_cache[unit] = {"features": features, "error": None}
    return features


# --------------------------------------------------------------------------- #
# Scoring / aggregation
# --------------------------------------------------------------------------- #

def grade_for(score: float, bands=GRADE_BANDS):
    for thresh, label, color in bands:
        if score >= thresh:
            return label, color
    return None, None


def recency_multiplier(valid_iso: str, window_days: int) -> float:
    """Fresher reports rank a bit higher (warmer leads). Range ~1.0 - 1.5."""
    try:
        v = dt.datetime.fromisoformat(valid_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 1.0
    age_days = (dt.datetime.now(dt.timezone.utc) - v).total_seconds() / 86400.0
    frac = max(0.0, 1.0 - age_days / max(1.0, window_days))
    return 1.0 + 0.5 * frac


def build_grid(reports, radius_mi):
    """Blend report scores onto a grid and return only cells above the Low band."""
    if not reports:
        return []
    dlat = mi_to_lat_deg(GRID_CELL_MI)
    dlon = mi_to_lon_deg(GRID_CELL_MI, CENTER_LAT)
    rlat = mi_to_lat_deg(radius_mi + GRID_CELL_MI)
    rlon = mi_to_lon_deg(radius_mi + GRID_CELL_MI, CENTER_LAT)
    south = CENTER_LAT - rlat
    west = CENTER_LON - rlon
    n_rows = int((2 * rlat) / dlat) + 1
    n_cols = int((2 * rlon) / dlon) + 1

    cutoff = KERNEL_CUTOFF_MI
    bw2 = KERNEL_BANDWIDTH_MI ** 2
    cells = {}

    for rep in reports:
        score = rep["weight"] * rep["_recency"]
        if score <= 0:
            continue
        # cell index of the report
        cr = int((rep["lat"] - south) / dlat)
        cc = int((rep["lon"] - west) / dlon)
        span_r = int(cutoff / GRID_CELL_MI) + 1
        span_c = span_r
        for r in range(max(0, cr - span_r), min(n_rows, cr + span_r + 1)):
            clat = south + (r + 0.5) * dlat
            for c in range(max(0, cc - span_c), min(n_cols, cc + span_c + 1)):
                clon = west + (c + 0.5) * dlon
                d = haversine_mi(rep["lat"], rep["lon"], clat, clon)
                if d > cutoff:
                    continue
                contrib = score * math.exp(-(d * d) / bw2)
                key = (r, c)
                cells[key] = cells.get(key, 0.0) + contrib

    out = []
    for (r, c), score in cells.items():
        clat = south + (r + 0.5) * dlat
        clon = west + (c + 0.5) * dlon
        if haversine_mi(CENTER_LAT, CENTER_LON, clat, clon) > radius_mi:
            continue
        label, color = grade_for(score)
        if label is None:
            continue
        out.append({
            "lat": round(clat, 4),
            "lon": round(clon, 4),
            "score": round(score, 2),
            "grade": label,
            "color": color,
            "bounds": [
                round(clat - dlat / 2, 4), round(clon - dlon / 2, 4),
                round(clat + dlat / 2, 4), round(clon + dlon / 2, 4),
            ],
        })
    return out


def top_areas(reports):
    """Aggregate by town into a ranked canvassing list."""
    agg = {}
    for rep in reports:
        city = rep["city"].strip() or "Unknown area"
        key = (city, rep["state"])
        a = agg.setdefault(key, {
            "city": city, "state": rep["state"], "count": 0, "score": 0.0,
            "max_hail_in": 0.0, "max_wind_mph": 0, "last": "",
            "lat": rep["lat"], "lon": rep["lon"],
            "hail": 0, "wind": 0, "tornado": 0,
        })
        a["count"] += 1
        a["score"] += rep["weight"] * rep["_recency"]
        a[rep["cat"]] += 1
        if rep["hail_in"]:
            a["max_hail_in"] = max(a["max_hail_in"], rep["hail_in"])
        if rep["wind_mph"]:
            a["max_wind_mph"] = max(a["max_wind_mph"], rep["wind_mph"])
        if rep["valid"] > a["last"]:
            a["last"] = rep["valid"]
            a["lat"], a["lon"] = rep["lat"], rep["lon"]

    rows = []
    for a in agg.values():
        label, color = grade_for(a["score"])
        if label is None:
            label, color = "Low", GRADE_BANDS[-1][2]
        rows.append({
            **a,
            "score": round(a["score"], 1),
            "max_hail_in": round(a["max_hail_in"], 2),
            "grade": label,
            "color": color,
        })
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:20]


def score_boundaries(reports, warnings, radius_mi, unit="zip", mesh=None):
    """Grade each neighborhood (ZIP or block group) by reports inside + swaths over it.

    If `mesh` (a MeshGrid) is given, the max radar hail size sampled in each area is
    blended in too, so areas light up from radar even with no spotter/warning.
    """
    bounds = fetch_boundaries(unit)
    if not bounds:
        return [], []
    geoms = [b["geom"] for b in bounds]
    tree = STRtree(geoms)
    kind_label = BOUNDARY_SOURCES.get(unit, BOUNDARY_SOURCES["zip"])["label"]

    # for block groups, look up the containing ZIP so each area has a recognizable label
    zip_bounds, zip_tree = None, None
    if unit == "bg":
        zip_bounds = fetch_boundaries("zip")
        if zip_bounds:
            zip_tree = STRtree([z["geom"] for z in zip_bounds])

    def containing_zip(centroid):
        if not zip_tree:
            return ""
        for zi in zip_tree.query(centroid):
            if zip_bounds[zi]["geom"].contains(centroid):
                return zip_bounds[zi]["short"]
        return ""

    acc = [{
        "score": 0.0, "nl": 0, "nw": 0, "mh": 0.0, "mw": 0,
        "radar_hail": 0.0, "last": "", "town": "",
    } for _ in bounds]

    # storm reports falling inside an area
    for rep in reports:
        pt = Point(rep["lon"], rep["lat"])
        for i in tree.query(pt):
            if geoms[i].contains(pt):
                a = acc[i]
                a["score"] += rep["weight"] * rep["_recency"]
                a["nl"] += 1
                if rep["hail_in"]:
                    a["mh"] = max(a["mh"], rep["hail_in"])
                if rep["wind_mph"]:
                    a["mw"] = max(a["mw"], rep["wind_mph"])
                if rep["valid"] > a["last"]:
                    a["last"] = rep["valid"]
                if rep["city"]:
                    a["town"] = rep["city"]
                break

    # warning swaths overlapping an area (area-weighted so an edge clip counts less)
    for w in warnings:
        wg = w["geom"]
        for i in tree.query(wg):
            inter = geoms[i].intersection(wg)
            if inter.is_empty:
                continue
            frac = min(1.0, inter.area / max(1e-12, bounds[i]["area"]))
            if frac < 0.03:
                continue
            a = acc[i]
            a["score"] += w["intensity"] * w["_recency"] * frac
            a["nw"] += 1
            if w["hail_in"]:
                a["mh"] = max(a["mh"], w["hail_in"])
            if w["wind_mph"]:
                a["mw"] = max(a["mw"], w["wind_mph"])
            if w["issue"] > a["last"]:
                a["last"] = w["issue"]

    # radar MESH hail sampled per area
    if mesh is not None:
        for b, a in zip(bounds, acc):
            rh = mesh.max_in(b["geom"])
            if rh and rh > 0:
                a["radar_hail"] = round(rh, 2)
                a["mh"] = max(a["mh"], rh)
                a["score"] += hail_size_weight(rh)   # radar hail contributes like a report

    features, rows = [], []
    low_thresh = ZCTA_GRADE_BANDS[-1][0]
    for b, a in zip(bounds, acc):
        if a["score"] < low_thresh:          # below "Low" -> not storm-affected
            continue
        c = b["geom"].centroid
        if haversine_mi(CENTER_LAT, CENTER_LON, c.y, c.x) > radius_mi + 3:
            continue
        label, color = grade_for(a["score"], ZCTA_GRADE_BANDS)
        if unit == "zip":
            name, town = f"ZIP {b['short']}", a["town"]
        else:
            cz = containing_zip(c)
            name = f"{kind_label.title()} {b['short']}" + (f" (ZIP {cz})" if cz else "")
            town = a["town"] or (f"ZIP {cz}" if cz else "")
        props = {
            "id": b["id"], "name": name, "unit": unit, "town": town,
            "score": round(a["score"], 1), "grade": label, "color": color,
            "reports": a["nl"], "warnings": a["nw"], "radar_hail_in": a["radar_hail"],
            "max_hail_in": round(a["mh"], 2), "max_wind_mph": a["mw"],
            "last": a["last"], "lat": round(c.y, 4), "lon": round(c.x, 4),
        }
        features.append({"type": "Feature", "properties": props, "geometry": mapping(b["geom"])})
        rows.append(props)

    rows.sort(key=lambda r: r["score"], reverse=True)
    return features, rows[:25]


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def compute_storms(window="6m", radius=75.0, types="hail,wind,tornado", unit="zip", radar=0):
    window = window if window in WINDOWS else "6m"
    window_days = WINDOWS[window]
    unit = unit if unit in BOUNDARY_SOURCES else "zip"
    radius = max(10.0, min(float(radius), MAX_RADIUS_MI))
    selected = {t.strip() for t in types.split(",") if t.strip()}

    all_reports = fetch_reports()
    cutoff_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)

    filtered = []
    for rep in all_reports:
        if rep["cat"] not in selected:
            continue
        if haversine_mi(CENTER_LAT, CENTER_LON, rep["lat"], rep["lon"]) > radius:
            continue
        try:
            v = dt.datetime.fromisoformat(rep["valid"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if v < cutoff_dt:
            continue
        rep = dict(rep)
        rep["_recency"] = recency_multiplier(rep["valid"], window_days)
        filtered.append(rep)

    # --- severe / tornado warning swaths within window + radius + selected types ---
    want_sv = ("hail" in selected) or ("wind" in selected)
    want_to = "tornado" in selected
    circle = Point(CENTER_LON, CENTER_LAT).buffer(radius / 69.0, quad_segs=24)
    warn_filtered = []
    for w in fetch_warnings():
        if w["ph"] == "SV" and not want_sv:
            continue
        if w["ph"] == "TO" and not want_to:
            continue
        try:
            iv = dt.datetime.fromisoformat(w["issue"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if iv < cutoff_dt:
            continue
        if not w["geom"].intersects(circle):
            continue
        w = dict(w)
        w["_recency"] = recency_multiplier(w["issue"], window_days)
        warn_filtered.append(w)

    # radar MESH hail (optional, heavier) — aggregated max hail grid over the window
    mesh = None
    mesh_error = None
    if radar:
        # only fetch radar for days that actually had storms (reports or warnings)
        storm_days = {r["valid"][:10].replace("-", "") for r in filtered if r.get("valid")}
        storm_days |= {w["issue"][:10].replace("-", "") for w in warn_filtered if w.get("issue")}
        try:
            mesh = mesh_mod.get_mesh(storm_days, radius)
        except Exception as exc:
            mesh_error = str(exc)

    grid = build_grid(filtered, radius)
    areas = top_areas(filtered)
    nb_features, top_neighborhoods = score_boundaries(filtered, warn_filtered, radius, unit, mesh)
    mesh_features = mesh.to_features(radius) if mesh is not None else []

    # warning swaths as GeoJSON for the map (colored by tagged intensity)
    warn_features = []
    for w in warn_filtered:
        label, color = grade_for(w["intensity"])
        if label is None:
            label, color = "Low", GRADE_BANDS[-1][2]
        warn_features.append({
            "type": "Feature",
            "geometry": mapping(w["geom"]),
            "properties": {
                "ps": w["ps"], "ph": w["ph"], "grade": label, "color": color,
                "intensity": round(w["intensity"], 1),
                "hail_in": w["hail_in"], "wind_mph": w["wind_mph"],
                "damagetag": w["damagetag"], "issue": w["issue"], "expire": w["expire"],
            },
        })

    hail_n = sum(1 for r in filtered if r["cat"] == "hail")
    wind_n = sum(1 for r in filtered if r["cat"] == "wind")
    torn_n = sum(1 for r in filtered if r["cat"] == "tornado")
    max_hail = max([r["hail_in"] or 0 for r in filtered] + [w["hail_in"] or 0 for w in warn_filtered], default=0)
    max_wind = max([r["wind_mph"] or 0 for r in filtered] + [w["wind_mph"] or 0 for w in warn_filtered], default=0)

    public_reports = [{k: v for k, v in r.items() if not k.startswith("_")} for r in filtered]

    return {
        "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
        "radius_mi": radius,
        "window": window,
        "window_label": WINDOW_LABELS[window],
        "unit": unit,
        "unit_label": BOUNDARY_SOURCES[unit]["label"],
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_error": (_cache.get("error") or _warn_cache.get("error")
                       or _boundary_cache.get(unit, {}).get("error") or mesh_error),
        "stats": {
            "total": len(filtered),
            "hail": hail_n,
            "wind": wind_n,
            "tornado": torn_n,
            "warnings": len(warn_filtered),
            "neighborhoods": len(nb_features),
            "max_hail_in": round(max_hail, 2),
            "max_wind_mph": int(max_wind),
            "radar_max_hail_in": round(mesh.max_hail, 2) if mesh is not None else 0,
            "graded_cells": len(grid),
            "areas": len(areas),
        },
        "reports": public_reports,
        "grid": {"cell_mi": GRID_CELL_MI, "cells": grid},
        "warnings": {"type": "FeatureCollection", "features": warn_features},
        "neighborhoods": {"type": "FeatureCollection", "features": nb_features},
        "radar_hail": {"type": "FeatureCollection", "features": mesh_features},
        "top_areas": areas,
        "top_neighborhoods": top_neighborhoods,
        "grade_bands": [{"label": l, "color": c, "min": t} for t, l, c in GRADE_BANDS],
        "radar_legend": mesh_mod.HAIL_LEGEND,
    }


@app.get("/api/storms")
def storms(
    window: str = Query("6m"),
    radius: float = Query(75.0, ge=10, le=MAX_RADIUS_MI),
    types: str = Query("hail,wind,tornado"),
    unit: str = Query("zip"),
    radar: int = Query(0),
):
    return JSONResponse(compute_storms(window, radius, types, unit, radar))


# --------------------------------------------------------------------------- #
# Exports (door-knocking / direct-mail lists)
# --------------------------------------------------------------------------- #

def _neighborhood_rows(window, radius, types, unit, radar):
    data = compute_storms(window, radius, types, unit, radar)
    return data["neighborhoods"]["features"], data


@app.get("/api/export/neighborhoods.csv")
def export_neighborhoods_csv(
    window: str = "6m", radius: float = 75.0,
    types: str = "hail,wind,tornado", unit: str = "zip", radar: int = 0,
):
    import csv
    import io
    feats, _ = _neighborhood_rows(window, radius, types, unit, radar)
    cols = ["name", "town", "grade", "score", "reports", "warnings",
            "max_hail_in", "max_wind_mph", "radar_hail_in", "last", "lat", "lon", "id"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for f in sorted(feats, key=lambda f: f["properties"]["score"], reverse=True):
        p = f["properties"]
        w.writerow([p.get(c, "") for c in cols])
    fname = f"jem_targets_{unit}_{window}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/export/neighborhoods.geojson")
def export_neighborhoods_geojson(
    window: str = "6m", radius: float = 75.0,
    types: str = "hail,wind,tornado", unit: str = "zip", radar: int = 0,
):
    feats, _ = _neighborhood_rows(window, radius, types, unit, radar)
    fc = {"type": "FeatureCollection", "features": feats}
    fname = f"jem_targets_{unit}_{window}.geojson"
    return Response(json.dumps(fc), media_type="application/geo+json",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/export/addresses.csv")
def export_addresses_csv(id: str, unit: str = "zip"):
    """Street addresses inside one graded neighborhood (OSM) — a door-knocking list."""
    import csv
    import io
    bounds = fetch_boundaries(unit)
    target = next((b for b in bounds if b["id"] == id), None)
    if target is None:
        return JSONResponse({"error": "neighborhood not found"}, status_code=404)
    geom = target["geom"]
    minx, miny, maxx, maxy = geom.bounds
    query = (
        "[out:json][timeout:60];"
        f'(node["addr:housenumber"]({miny},{minx},{maxy},{maxx});'
        f' way["addr:housenumber"]({miny},{minx},{maxy},{maxx}););'
        "out center tags;"
    )
    mirrors = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]
    headers = {"User-Agent": "JEM-Roofing-Lead-Dashboard/1.0", "Accept": "application/json"}
    elements, last_err = None, None
    with httpx.Client(timeout=90.0, headers=headers) as client:
        for url in mirrors:
            try:
                resp = client.post(url, content=f"data={query}".encode("utf-8"),
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
                resp.raise_for_status()
                elements = resp.json().get("elements", [])
                break
            except Exception as exc:
                last_err = exc
    if elements is None:
        return JSONResponse({"error": f"address lookup failed: {last_err}"}, status_code=502)

    rows = []
    for el in elements:
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        if not geom.contains(Point(lon, lat)):
            continue
        t = el.get("tags", {})
        street = " ".join(x for x in [t.get("addr:housenumber"), t.get("addr:street")] if x)
        rows.append([street, t.get("addr:city", ""), t.get("addr:postcode", ""),
                     round(lat, 6), round(lon, 6)])
        if len(rows) >= 6000:
            break

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["address", "city", "postcode", "lat", "lon"])
    w.writerows(sorted(rows))
    fname = f"jem_addresses_{id}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/health")
def health():
    reps = fetch_reports()
    warns = fetch_warnings()
    zips = fetch_boundaries("zip")
    return {
        "ok": True,
        "cached_reports": len(reps),
        "cached_warnings": len(warns),
        "cached_zips": len(zips),
        "errors": {
            "lsr": _cache.get("error"),
            "sbw": _warn_cache.get("error"),
            "zip": _boundary_cache.get("zip", {}).get("error"),
        },
    }


# Serve the static front-end (mounted last so /api/* wins).
# Guarded so this module can also be imported as a plain compute library (e.g. by
# the static-site build pipeline) where no static/ dir is present.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

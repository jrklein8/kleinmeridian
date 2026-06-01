"""
MRMS MESH (Maximum Estimated Size of Hail) — radar-derived hail swaths.

Pulls NOAA MRMS `MESH_Max_1440min` GRIB2 grids (free, public S3 bucket, Nov 2020+),
decodes the ~1 km CONUS hail grid, crops to the Wilmington region, and aggregates the
per-pixel maximum hail size across a set of storm days. Used both as a map overlay
(true radar-pixel hail bands) and to credit hail to neighborhoods that had no spotter.

Only days that actually had storms are fetched (the caller derives them from the
reports + warnings it already has), so even a 6-month build is a few dozen files.
Each day's cropped grid is cached to disk so overlapping windows are instant.
"""
import gzip
import math
import os
import re
import tempfile

import httpx
import numpy as np
import xarray as xr


def _haversine_mi(la1, lo1, la2, lo2):
    r = 3958.76
    a = (math.sin(math.radians(la2 - la1) / 2) ** 2
         + math.cos(math.radians(la1)) * math.cos(math.radians(la2))
         * math.sin(math.radians(lo2 - lo1) / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))

BUCKET = "https://noaa-mrms-pds.s3.amazonaws.com"
PRODUCT = "CONUS/MESH_Max_1440min_00.50"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mesh_cache")

# Fixed MRMS CONUS grid geometry (0.01-degree spacing)
G_LAT0 = 54.995      # northern-most row center
G_LON0 = 230.005     # western-most col center, in 0-360 longitude
G_STEP = 0.01

# Regional crop (Wilmington + ~1.5 hr radius, generous)
BOX_N, BOX_S = 35.7, 32.8
BOX_W, BOX_E = -79.9, -76.0          # in -180..180

MAX_MESH_DAYS = 60                    # safety cap on how many days we'll fetch

# Hail-size color bands for the radar overlay (inches -> color)
HAIL_BANDS = [
    (2.0, "2\"+ hail", "#7a0177"),
    (1.5, "1.5-2\" hail", "#bd0026"),
    (1.0, "1-1.5\" hail", "#f03b20"),
    (0.75, "0.75-1\" hail", "#fd8d3c"),
]
HAIL_MIN = 0.75                       # don't draw/credit below this (roof-damage floor)


def _lat_index(lat):
    return int(round((G_LAT0 - lat) / G_STEP))


def _lon_index(lon):
    return int(round(((lon % 360) - G_LON0) / G_STEP))


# Regional index window into the full CONUS grid (computed once)
R_I0, R_I1 = _lat_index(BOX_N), _lat_index(BOX_S)      # rows, north->south
R_J0, R_J1 = _lon_index(BOX_W), _lon_index(BOX_E)      # cols, west->east
REG_LATS = G_LAT0 - np.arange(R_I0, R_I1 + 1) * G_STEP
_reg_lon360 = G_LON0 + np.arange(R_J0, R_J1 + 1) * G_STEP
REG_LONS = np.where(_reg_lon360 > 180, _reg_lon360 - 360, _reg_lon360)
REG_SHAPE = (len(REG_LATS), len(REG_LONS))


def _list_day_key(client, datestr):
    """Return the latest MESH_Max_1440min key for a day (its 24h max ~ that day's max)."""
    url = f"{BUCKET}/?list-type=2&prefix={PRODUCT}/{datestr}/"
    r = client.get(url, timeout=30)
    r.raise_for_status()
    keys = re.findall(r"<Key>([^<]+)</Key>", r.text)
    return keys[-1] if keys else None


def _load_day(client, datestr):
    """Cropped regional hail-size grid (inches) for one day, cached to disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"mesh_{datestr}.npz")
    if os.path.exists(cache):
        return np.load(cache)["g"]

    grid = np.zeros(REG_SHAPE, dtype=np.float32)
    key = _list_day_key(client, datestr)
    if key:
        r = client.get(f"{BUCKET}/{key}", timeout=120)
        r.raise_for_status()
        raw = gzip.decompress(r.content) if key.endswith(".gz") else r.content
        tf = tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
        try:
            tf.write(raw)
            tf.close()
            ds = xr.open_dataset(tf.name, engine="cfgrib", backend_kwargs={"indexpath": ""})
            var = list(ds.data_vars)[0]
            vals = ds[var].values            # (3500, 7000), hail size in mm
            ds.close()
            crop = vals[R_I0:R_I1 + 1, R_J0:R_J1 + 1].astype(np.float32)
            crop = np.where(crop < 0, 0.0, crop) / 25.4    # mm -> inches, missing -> 0
            grid = crop
        finally:
            try:
                os.unlink(tf.name)
            except OSError:
                pass

    np.savez_compressed(cache, g=grid)
    return grid


def get_mesh(storm_days, radius_mi=100):
    """Aggregate per-pixel max hail (inches) across the given storm days -> MeshGrid."""
    days = sorted(set(storm_days))[-MAX_MESH_DAYS:]
    grid = np.zeros(REG_SHAPE, dtype=np.float32)
    fetched = 0
    with httpx.Client() as client:
        for d in days:
            try:
                np.maximum(grid, _load_day(client, d), out=grid)
                fetched += 1
            except Exception:
                continue
    return MeshGrid(grid, fetched)


class MeshGrid:
    """Holds the regional max-hail grid and samples / renders it."""

    def __init__(self, grid, days=0):
        self.grid = grid
        self.days = days
        self.max_hail = float(grid.max()) if grid.size else 0.0

    def _lat_idx(self, lat):
        return int(round((REG_LATS[0] - lat) / G_STEP))

    def _lon_idx(self, lon):
        return int(round(((lon % 360) - (REG_LONS[0] % 360)) / G_STEP))

    def max_in(self, geom):
        """Max hail size (inches) among pixels within a polygon's bounding box."""
        minx, miny, maxx, maxy = geom.bounds
        i0 = max(0, self._lat_idx(maxy))
        i1 = min(REG_SHAPE[0] - 1, self._lat_idx(miny))
        j0 = max(0, self._lon_idx(minx))
        j1 = min(REG_SHAPE[1] - 1, self._lon_idx(maxx))
        if i1 < i0 or j1 < j0:
            return 0.0
        sub = self.grid[i0:i1 + 1, j0:j1 + 1]
        return float(sub.max()) if sub.size else 0.0

    def to_features(self, radius_mi, center=(34.2257, -77.9447), stride=2):
        """Radar hail swath as GeoJSON cells (coarsened to ~2 km), colored by hail size."""
        feats = []
        cstep = G_STEP * stride
        ni, nj = REG_SHAPE
        for i in range(0, ni - 1, stride):
            for j in range(0, nj - 1, stride):
                v = float(self.grid[i:i + stride, j:j + stride].max())
                if v < HAIL_MIN:
                    continue
                lat = float(REG_LATS[i])
                lon = float(REG_LONS[j])
                if _haversine_mi(center[0], center[1], lat - cstep / 2, lon + cstep / 2) > radius_mi:
                    continue
                color = next(c for thr, _, c in HAIL_BANDS if v >= thr)
                feats.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [round(lon, 4), round(lat, 4)],
                            [round(lon + cstep, 4), round(lat, 4)],
                            [round(lon + cstep, 4), round(lat - cstep, 4)],
                            [round(lon, 4), round(lat - cstep, 4)],
                            [round(lon, 4), round(lat, 4)],
                        ]],
                    },
                    "properties": {"hail_in": round(v, 2), "color": color},
                })
        return feats


HAIL_LEGEND = [{"label": lbl, "color": c} for _, lbl, c in HAIL_BANDS]

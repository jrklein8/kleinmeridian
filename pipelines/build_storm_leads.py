"""
Bake the Storm Lead Dashboard data into static JSON for GitHub Pages.

Reuses the dashboard's compute logic (storm_leads/storm_app.py) to pre-compute the
graded neighborhoods + reports + warnings + radar hail for every time window and
both granularities, and writes them under leads/data/. The static front-end reads
these files and does the radius/type filtering client-side.

Run locally:   python pipelines/build_storm_leads.py
In CI:         scheduled GitHub Action (.github/workflows/leads-data.yml)
"""
import os
import sys
import json
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "storm_leads"))
import storm_app as S   # noqa: E402  (the copied FastAPI-free compute module)

OUT = os.path.join(os.path.dirname(HERE), "leads", "data")
os.makedirs(OUT, exist_ok=True)

WINDOWS = ["yesterday", "1w", "1m", "6m"]
UNITS = ["zip", "bg"]
RADIUS = S.MAX_RADIUS_MI                       # bake the full area; client filters by radius
TYPES = "hail,wind,tornado"
# Radar (MRMS) is OFF for the hosted page to keep the scheduled Action light + reliable
# (no GRIB/numpy/xarray deps, no NOAA fetch). Set LEADS_RADAR=1 to re-enable.
RADAR = int(os.environ.get("LEADS_RADAR", "0"))


def compact(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))


def main():
    manifest = {"windows": WINDOWS, "units": UNITS, "radar": bool(RADAR), "generated": None}
    for window in WINDOWS:
        base = None
        for unit in UNITS:
            data = S.compute_storms(window=window, radius=RADIUS, types=TYPES, unit=unit, radar=RADAR)
            if base is None:
                base = {
                    "center": data["center"], "radius_mi": data["radius_mi"],
                    "window": data["window"], "window_label": data["window_label"],
                    "generated": data["generated"], "data_error": data.get("data_error"),
                    "stats": data["stats"], "reports": data["reports"], "grid": data["grid"],
                    "warnings": data["warnings"], "radar_hail": data["radar_hail"],
                    "top_areas": data["top_areas"], "grade_bands": data["grade_bands"],
                    "radar_legend": data["radar_legend"],
                }
                compact(os.path.join(OUT, f"{window}.base.json"), base)
            compact(os.path.join(OUT, f"{window}.{unit}.json"), {
                "unit": data["unit"], "unit_label": data["unit_label"],
                "neighborhoods": data["neighborhoods"],
                "top_neighborhoods": data["top_neighborhoods"],
                "neighborhoods_count": data["stats"]["neighborhoods"],
            })
            print(f"  {window}/{unit}: {data['stats']['neighborhoods']} areas, "
                  f"{len(data['reports'])} reports, {data['stats']['warnings']} warnings, "
                  f"radar max {data['stats'].get('radar_max_hail_in')}\"")
        manifest["generated"] = base["generated"]

    compact(os.path.join(OUT, "manifest.json"), manifest)
    print(f"Wrote data for {len(WINDOWS)} windows x {len(UNITS)} units -> {OUT}")


if __name__ == "__main__":
    main()

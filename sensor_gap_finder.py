#!/usr/bin/env python3
"""
Sensor Gap Finder
=================

Given a river point, find the nearest *upstream* sensor gap and suggest the
optimal placement for a new monitoring station.

Live data sources (no mocks):
  * USGS NWIS site service  -> nearby streamgages
        https://waterservices.usgs.gov/nwis/site/
  * USGS NWIS iv service    -> latest gauge readings
        https://waterservices.usgs.gov/nwis/iv/
  * EPA Water Quality Portal -> water-quality monitoring sites
        https://www.waterqualitydata.us/data/Result/search

Output conforms to the #450 Ecological Sensor Data JSON Schema Standard
(see sensor_schema.json shipped alongside this file). Every record carries the
six required fields (timestamp, location{lat,lng}, unit, value, sensor_id,
source) plus parameter / parameter_code / quality.

Usage
-----
  # Auto-discover nearby gages around a point (production):
  python sensor_gap_finder.py --lat 36.95 --lon -111.50 --radius-km 60

  # Explicit site list (handy when the site service is firewalled; still live):
  python sensor_gap_finder.py --sites 09380000,09421500,09402500

  # Emit raw #450 records only:
  python sensor_gap_finder.py --lat 36.95 --lon -111.50 --json
"""
import argparse
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

USGS_SITE = "https://waterservices.usgs.gov/nwis/site/"
USGS_IV = "https://waterservices.usgs.gov/nwis/iv/"
WQP = "https://www.waterqualitydata.us/data/Result/search"

# #450 parameter codes we care about
DISCHARGE_CD = "00060"   # streamflow, ft3/s
GAGE_HT_CD = "00065"     # gage height, ft


def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "sensor-gap-finder/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _geo(site):
    loc = site.get("location", {}).get("geogLocation", {})
    return float(loc.get("latitude")), float(loc.get("longitude"))


def discover_sites(lat, lon, radius_km):
    """Bounding-box search via USGS site service (production path)."""
    # ~1 deg latitude = 111 km; longitude compressed by cos(lat)
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.2, abs(math.cos(math.radians(lat)))))
    bbox = f"{lon - dlon:.5f},{lat - dlat:.5f},{lon + dlon:.5f},{lat + dlat:.5f}"
    q = urllib.parse.urlencode({
        "format": "json",
        "bBox": bbox,
        "siteType": "ST",
        "hasDataTypeCd": "iv",
        "parameterCd": DISCHARGE_CD,
    })
    try:
        d = _get_json(f"{USGS_SITE}?{q}")
        return d.get("value", {}).get("timeSeries", []) or \
               d.get("value", {}).get("site", [])
    except Exception as e:
        sys.stderr.write(f"[warn] USGS site discovery failed: {e}\n")
        return []


def latest_value(site_no, parameter_cd):
    """Fetch latest instantaneous value (+coords) for one site via the iv service.
    Returns (value, timestamp, lat, lon). The iv response carries geoLocation."""
    q = urllib.parse.urlencode({
        "format": "json",
        "sites": site_no,
        "parameterCd": parameter_cd,
        "period": "P1D",
    })
    try:
        d = _get_json(f"{USGS_IV}?{q}")
        for ts in d.get("value", {}).get("timeSeries", []):
            gi = ts.get("sourceInfo", {}).get("geoLocation", {}).get("geogLocation", {})
            lat = gi.get("latitude")
            lon = gi.get("longitude")
            for v in ts.get("values", [{}])[0].get("value", []):
                if v.get("value") not in (None, ""):
                    return float(v["value"]), v.get("dateTime"), lat, lon
    except Exception:
        pass
    return None, None, None, None


def wqp_sites(lat, lon, radius_km):
    """Nearby EPA water-quality monitoring sites (production path)."""
    q = urllib.parse.urlencode({
        "format": "json",
        "latitude": f"{lat}",
        "longitude": f"{lon}",
        "within": str(radius_km),
    })
    try:
        d = _get_json(f"{WQP}?{q}")
        return d.get("Results", [])
    except Exception as e:
        sys.stderr.write(f"[warn] EPA WQP discovery failed: {e}\n")
        return []


def haversine(a_lat, a_lon, b_lat, b_lon):
    R = 6371.0
    p = math.radians
    dphi = p(b_lat - a_lat)
    dlam = p(b_lon - a_lon)
    x = math.sin(dphi / 2) ** 2 + math.cos(p(a_lat)) * math.cos(p(b_lat)) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def upstream_bearing(point_lat, point_lon, site_lat, site_lon):
    """Signed "upstream-ness": +1 straight upstream (north), -1 downstream."""
    dlon = site_lon - point_lon
    dlat = site_lat - point_lat
    if abs(dlat) < 1e-9 and abs(dlon) < 1e-9:
        return 0.0
    ang = math.degrees(math.atan2(dlon, dlat))  # 0 = north, 90 = east
    # Project bearing onto the "north = upstream" axis.
    return math.cos(math.radians(ang))


def find_gap(point_lat, point_lon, sensors):
    """sensors: list of dicts {id, name, lat, lon, value, unit}.
    Returns the largest gap between consecutive upstream sensors and the
    recommended midpoint for a new station."""
    upstream = [s for s in sensors if upstream_bearing(point_lat, point_lon, s["lat"], s["lon"]) > 0]
    if len(upstream) < 2:
        # Fall back to all sensors by distance if no clear upstream cluster.
        upstream = sorted(sensors, key=lambda s: haversine(point_lat, point_lon, s["lat"], s["lon"]))
    else:
        upstream.sort(key=lambda s: haversine(point_lat, point_lon, s["lat"], s["lon"]))

    best = None
    for a, b in zip(upstream, upstream[1:]):
        gap_km = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        if best is None or gap_km > best["gap_km"]:
            best = {
                "gap_km": gap_km,
                "a": a, "b": b,
                "mid_lat": (a["lat"] + b["lat"]) / 2,
                "mid_lon": (a["lon"] + b["lon"]) / 2,
            }
    return best


def to_450(record):
    """Wrap a reading in the #450 schema shape."""
    return {
        "timestamp": record["timestamp"],
        "location": {"lat": record["lat"], "lng": record["lon"]},
        "unit": record["unit"],
        "value": record["value"],
        "sensor_id": record["sensor_id"],
        "source": record["source"],
        "parameter": record["parameter"],
        "parameter_code": record["parameter_code"],
        "quality": record.get("quality", "calculated"),
    }


def main():
    ap = argparse.ArgumentParser(description="Find nearest upstream sensor gap.")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--radius-km", type=float, default=60)
    ap.add_argument("--sites", help="comma-separated USGS site numbers")
    ap.add_argument("--json", action="store_true", help="emit #450 records only")
    args = ap.parse_args()

    sensors = []
    if args.sites:
        for sid in args.sites.split(","):
            sid = sid.strip()
            if not sid:
                continue
            val, ts, lat, lon = latest_value(sid, DISCHARGE_CD)
            sensors.append({
                "id": sid, "name": f"USGS {sid}", "lat": lat, "lon": lon,
                "value": val, "unit": "ft3/s", "ts": ts,
            })

    if args.lat is not None and args.lon is not None:
        ts_list = discover_sites(args.lat, args.lon, args.radius_km)
        for s in ts_list:
            try:
                info = s["sourceInfo"]
                sid = info["siteCode"][0]["value"]
                lat, lon = _geo(info)
                val, ts, _, _ = latest_value(sid, DISCHARGE_CD)
                sensors.append({
                    "id": sid, "name": info.get("siteName", sid),
                    "lat": lat, "lon": lon, "value": val, "unit": "ft3/s", "ts": ts,
                })
            except Exception:
                continue
        wq = wqp_sites(args.lat, args.lon, args.radius_km)
        for r in wq[:50]:
            try:
                loc = r.get("MonitoringLocation", {})
                lat = float(loc.get("LatitudeMeasure"))
                lon = float(loc.get("LongitudeMeasure"))
                sensors.append({
                    "id": loc.get("MonitoringLocationIdentifier", "?"),
                    "name": loc.get("MonitoringLocationName", "?"),
                    "lat": lat, "lon": lon, "value": None, "unit": "n/a", "ts": None,
                })
            except Exception:
                continue

    if not sensors:
        print(json.dumps({"error": "no sensors discovered", "hint": "check network egress to USGS/EPA"}, indent=2))
        return

    # Always emit per-sensor #450 readings (the live values we fetched).
    recs = []
    for s in sensors:
        if s["value"] is not None:
            recs.append(to_450({
                "timestamp": s["ts"] or datetime.now(timezone.utc).isoformat(),
                "lat": s["lat"] or 0.0, "lon": s["lon"] or 0.0,
                "unit": "ft3/s", "value": s["value"],
                "sensor_id": s["id"], "source": "USGS_NWIS",
                "parameter": "streamflow", "parameter_code": DISCHARGE_CD,
            }))

    gap = find_gap(args.lat or 0.0, args.lon or 0.0, sensors)
    result = {"sensor_count": len(sensors), "readings": recs}
    if gap:
        now = datetime.now(timezone.utc).isoformat()
        gap_rec = to_450({
            "timestamp": now, "lat": gap["mid_lat"], "lon": gap["mid_lon"],
            "unit": "km", "value": round(gap["gap_km"], 3),
            "sensor_id": f"GAP:{gap['a']['id']}->{gap['b']['id']}",
            "source": "sensor_gap_finder",
            "parameter": "upstream_sensor_gap", "parameter_code": "GAP_KM",
        })
        result["recommended_placement"] = {"lat": gap["mid_lat"], "lon": gap["mid_lon"]}
        result["gap_km"] = round(gap["gap_km"], 3)
        result["between"] = [gap["a"]["id"], gap["b"]["id"]]
        result["readings"] = recs + [gap_rec]
    else:
        result["note"] = "insufficient upstream sensors to compute a gap (need >=2 with coordinates)"

    if args.json:
        print(json.dumps(result["readings"], indent=2))
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

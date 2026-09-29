# Sensor Gap Finder

> **owocki bounty #478** — Given a river point, find the nearest *upstream* sensor
> gap and suggest the optimal placement for a new monitoring station.
> Conforms to the **#450 Ecological Sensor Data JSON Schema Standard**
> (see `sensor_schema.json`).

## What it does

1. Discovers streamgages and water-quality monitoring sites near a river point
   (USGS NWIS + EPA Water Quality Portal).
2. Orders them by upstream direction from the reference point.
3. Finds the **largest gap** between consecutive upstream sensors.
4. Recommends a new-station placement at the gap midpoint.
5. Emits a **#450-conformant** reading (`upstream_sensor_gap` / `GAP_KM`) plus a
   full result object.

All readings are **live** (USGS NWIS instantaneous values, EPA WQP site metadata) —
no mocks.

## Data sources

| Source | Endpoint | Used for |
|---|---|---|
| USGS NWIS site service | `https://waterservices.usgs.gov/nwis/site/` | nearby gage discovery |
| USGS NWIS iv service | `https://waterservices.usgs.gov/nwis/iv/` | latest gauge readings + coords |
| EPA Water Quality Portal | `https://www.waterqualitydata.us/data/Result/search` | water-quality sites |

## Usage

```bash
# Auto-discover nearby gages around a point (production):
python sensor_gap_finder.py --lat 36.95 --lon -111.50 --radius-km 60

# Explicit site list (handy when the site service is firewalled; still live):
python sensor_gap_finder.py --sites 09380000,09421500,09402500

# Emit raw #450 records only:
python sensor_gap_finder.py --lat 36.95 --lon -111.50 --json
```

### Output (point mode)

```json
{
  "recommended_placement": { "lat": 36.91, "lon": -111.52 },
  "gap_km": 18.4,
  "between": ["09380000", "09421500"],
  "sensor_count": 7,
  "readings": [
    {
      "timestamp": "2026-09-27T21:15:00.000-07:00",
      "location": { "lat": 36.91, "lon": -111.52 },
      "unit": "km",
      "value": 18.4,
      "sensor_id": "GAP:09380000->09421500",
      "source": "sensor_gap_finder",
      "parameter": "upstream_sensor_gap",
      "parameter_code": "GAP_KM",
      "quality": "calculated"
    }
  ]
}
```

Every `readings[]` entry satisfies the six required #450 fields
(`timestamp`, `location{lat,lng}`, `unit`, `value`, `sensor_id`, `source`)
plus `parameter` / `parameter_code` / `quality`.

## Notes

- "Upstream" is approximated by the signed north-axis projection of the bearing
  from the reference point; for divergent tributaries the tool falls back to
  ordering all discovered sensors by straight-line distance.
- Live runs require outbound network egress to `waterservices.usgs.gov` and
  `waterqualitydata.us` (the sandbox used for CI may rate-limit bulk queries).

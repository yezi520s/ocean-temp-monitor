"""Full CRW-pipeline integration test."""
import json
from app import app

with app.test_client() as c:
    # 1. CRW clim endpoint
    r = c.get("/api/crw/clim")
    d = json.loads(r.data)
    print("1. /api/crw/clim:", len(d.get("stations", [])), "stations")
    for s in d.get("stations", []):
        print("   " + s["name"] + ": " + str(s.get("anomaly")))

    # 2. Heatmap endpoint
    r = c.get("/api/crw/heatmap")
    d = json.loads(r.data)
    if "error" in d:
        print("2. /api/crw/heatmap: ERROR -", d["error"])
    else:
        print("2. /api/crw/heatmap: OK, grid", len(d["lats"]), "x", len(d["lons"]))

    # 3. Anomaly endpoint
    r = c.get("/api/crw/anomaly")
    d = json.loads(r.data)
    if "error" in d:
        print("3. /api/crw/anomaly: ERROR -", d["error"])
    else:
        print("3. /api/crw/anomaly: OK, grid", len(d["lats"]), "x", len(d["lons"]))

    # 4. Station detail with CRW anomaly
    r = c.get("/api/station/1/detail")
    d = json.loads(r.data)
    print("4. /api/station/1/detail: score=" + str(d.get("score")),
          "clim_anomaly=" + str(d.get("clim_anomaly")),
          "baseline=" + str(d.get("baseline_type")))

    # 5. Overview with CRW
    r = c.get("/api/overview")
    d = json.loads(r.data)
    print("5. /api/overview: stations=" + str(len(d.get("stations", []))),
          "score=" + str(d.get("score")))

print("\n=== ALL PASSED ===")
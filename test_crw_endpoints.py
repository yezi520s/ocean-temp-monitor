"""Test CRW endpoints via Flask test client."""
import json
from app import app

with app.test_client() as c:
    # /api/crw/clim
    r = c.get("/api/crw/clim")
    data = json.loads(r.data)
    print("CLIM stations:", len(data.get("stations", [])))
    for s in data.get("stations", []):
        anom = s.get("anomaly")
        label = s.get("label")
        print(f"  {s['name']}: {anom} -> {label}")
    
    # /api/crw/heatmap
    r2 = c.get("/api/crw/heatmap")
    data2 = json.loads(r2.data)
    print()
    if "error" in data2:
        print(f"HEATMAP error: {data2['error']}")
    else:
        print(f"HEATMAP variable={data2.get('variable')} time={data2.get('data_time')}")
        print(f"  grid lats={len(data2.get('lats',[]))} lons={len(data2.get('lons',[]))}")
        n_valid = sum(1 for row in data2.get("grid", []) for v in row if v is not None)
        n_total = len(data2.get("grid", [])) * len(data2.get("grid", [])[0] if data2.get("grid") else [])
        print(f"  valid points: {n_valid}/{n_total}")
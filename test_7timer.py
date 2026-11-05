import sys
sys.path.insert(0, '.')
from app import _fetch_7timer_weather, _wind_dir_to_deg

# Unit test wind dir
for inp, exp in [("W", 270), ("NE", 45), ("255", 255), (None, None), (123, 123)]:
    r = _wind_dir_to_deg(inp)
    print(f"  wind_dir({inp!r}) => {r} {'PASS' if r == exp else f'FAIL expected {exp}'}")

payload, err = _fetch_7timer_weather(38.9, 121.6, 8)
if payload:
    h = payload['hourly']
    print(f"OK | times={len(h['time'])} | first temp={h['temperature_2m'][0]}C "
          f"wind={h['wind_speed_10m'][0]}m/s dir={h['wind_direction_10m'][0]}\u00b0")
    print(f"  press={h['surface_pressure'][0]}hPa precip={h['precipitation'][0]}mm "
          f"cloud={h['cloud_cover'][0]}% rh={h['relative_humidity_2m'][0]}%")
else:
    print(f"FAIL: {err}")
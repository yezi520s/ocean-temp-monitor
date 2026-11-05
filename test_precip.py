import sys, json
sys.path.insert(0, '.')
from app import _pick_current_index, _series_stats

times = [f"2026-11-05T{i:02d}:00" for i in range(24)]
precip_vals = [0.5]*5 + [1.3, 0.8, 1.3, 0.8, 0.1, 0.3] + [0.0]*13

idx = _pick_current_index(times, 8*3600)
print(f"idx (now) = {idx}, time = {times[idx]}")
old_stat = _series_stats(precip_vals, idx, 24)
print(f"OLD (idx..idx+24): now={old_stat['now']} avg={old_stat['avg']} max={old_stat['max']} min={old_stat['min']}")

# New: all data for stats, idx for now
all_non_none = [v for v in precip_vals if v is not None]
new_stat = {
    "now": precip_vals[idx] if idx < len(precip_vals) else None,
    "avg": round(sum(all_non_none) / len(all_non_none), 2) if all_non_none else None,
    "max": max(precip_vals),
    "min": min(precip_vals),
}
print(f"NEW (full range): now={new_stat['now']} avg={new_stat['avg']} max={new_stat['max']} min={new_stat['min']}")
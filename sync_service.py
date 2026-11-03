# -*- coding: utf-8 -*-
"""
数据同步服务 —— 从 Open-Meteo Marine API 拉取海表温度并落库。

- 首次同步：拉取 HISTORY_DAYS 历史（覆盖完整去年同期）+ 未来 7 天预报，
  为「同比异常」提供真实基线。
- 增量同步：只拉近 3 天 + 未来 7 天，避免每次全量、刷爆 API 配额。
"""
import time
from datetime import datetime, timedelta

import requests

import config
from database import get_conn


def _build_params(lat, lon, past_days, forecast_days):
    return {
        "latitude": lat,
        "longitude": lon,
        "hourly": "sea_surface_temperature",
        "timezone": config.TIMEZONE,
        "past_days": past_days,
        "forecast_days": forecast_days,
    }


def fetch_hourly(lat, lon, past_days=0, forecast_days=config.FORECAST_DAYS, timeout=30):
    """请求一次 Marine API，返回 {"time": [...], "sst": [...]}；失败返回 None。"""
    params = _build_params(lat, lon, past_days, forecast_days)
    try:
        resp = requests.get(config.MARINE_API_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        hourly = data.get("hourly")
        if not hourly or "sea_surface_temperature" not in hourly:
            return None
        return {
            "time": hourly["time"],
            "sst": hourly["sea_surface_temperature"],
        }
    except Exception as exc:  # noqa: BLE001 —— 记录后由调用方决定重试策略
        print(f"[sync] API 请求失败 ({lat},{lon}): {exc}")
        return None


def _upsert(station, times, ssts):
    """批量写入一个站点的逐时数据（幂等：同站+时间戳去重）。"""
    now = datetime.now().isoformat()
    rows = []
    for t, temp in zip(times, ssts):
        if temp is None:
            continue
        rows.append(
            (station["id"], station["name"], station["lat"], station["lon"],
             t, round(float(temp), 2), now)
        )
    if not rows:
        return 0
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO sst_data
                (station_id, station_name, lat, lon, timestamp, temperature, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def sync_station(station, historical=False):
    """同步单个站点。historical=True 时做历史基线同步，否则做增量同步。"""
    if historical:
        past_days = config.HISTORY_DAYS
    else:
        past_days = config.INCREMENTAL_PAST_DAYS

    hourly = fetch_hourly(station["lat"], station["lon"], past_days=past_days)
    if not hourly:
        return False
    count = _upsert(station, hourly["time"], hourly["sst"])
    return count > 0


def sync_all(historical=False, stations=None):
    """同步全部站点，返回 [{"station": name, "success": bool, "records": n}]。"""
    stations = stations or config.MONITOR_STATIONS
    results = []
    for station in stations:
        t0 = time.time()
        ok = sync_station(station, historical=historical)
        # 记录落库条数
        if ok:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM sst_data WHERE station_id = ?",
                    (station["id"],),
                ).fetchone()
                records = row["c"] if row else 0
        else:
            records = 0
        results.append({
            "station": station["name"],
            "station_id": station["id"],
            "success": ok,
            "records": records,
            "elapsed_ms": int((time.time() - t0) * 1000),
        })
    return results


def has_historical_baseline():
    """是否已具备历史基线（任一站点的数据覆盖去年同期的同比窗口）。"""
    cutoff = (datetime.now() - timedelta(days=config.YOY_OFFSET_DAYS + 7)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM sst_data WHERE timestamp <= ?", (cutoff,)
        ).fetchone()
    return (row["c"] if row else 0) > 0


def latest_data_span():
    """数据库中数据的时间跨度（最早/最晚时间戳），用于前端展示数据新鲜度。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MIN(timestamp) AS lo, MAX(timestamp) AS hi FROM sst_data"
        ).fetchone()
    return {"min": row["lo"] if row else None, "max": row["hi"] if row else None}

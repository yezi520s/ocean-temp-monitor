# -*- coding: utf-8 -*-
"""
NOAA Coral Reef Watch 数据服务 —— ERDDAP 栅格数据获取与预处理。

数据来源：
  NOAA CRW Daily Global 5km Satellite Coral Bleaching Heat Stress Monitoring
  ERDDAP: https://coastwatch.pfeg.noaa.gov/erddap/griddap/NOAA_DHW

变量：
  CRW_DHW         — 度周 (Degree Heating Weeks, °C-weeks)
  CRW_SSTANOMALY  — 海表温度距平 (相对于 1985–2012 气候态, °C)
  CRW_HOTSPOT     — 热点 (当前 SST 超出 MMM 的值, °C)
  CRW_SST         — CoralTemp 海表温度 (°C)
  CRW_BAA         — 白化预警等级 (1–4)

中国近海范围: lat 15°–42°N, lon 108°–124°E (约 0.05° 分辨率原数据)
为控制传输量，前端用 stride=4 (≈0.2°) 输出网格 JSON。
"""
import json
import threading
import time

import requests

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
CRW_ERDDAP = "https://coastwatch.pfeg.noaa.gov/erddap/griddap/NOAA_DHW.csv"
REQUEST_TIMEOUT = 25          # ERDDAP 提取超时
GRID_STRIDE = 4               # 原 0.05° → 约 0.2° 网格
TTL_SECONDS = 3600 * 6        # 缓存 6 小时（CRW 日产日更）

# 中国近海范围
REGION = {
    "north": 42.0,
    "south": 15.0,
    "east": 124.0,
    "west": 108.0,
}

# DHW 色阶阈值（NOAA 标准）
DHW_LEVELS = [
    (0,   "#2166ac"),   # 无热应力
    (1,   "#92c5de"),   # 轻度
    (2,   "#f4a582"),   # 中度
    (4,   "#d6604d"),   # 预警
    (8,   "#b2182b"),   # 严重
    (12,  "#67001f"),   # 极严重
]

# SST 距平色阶
ANOMALY_LEVELS = [
    (-5.0, "#053061"),
    (-2.0, "#2166ac"),
    (-1.0, "#4393c3"),
    (0.0,  "#d1e5f0"),
    (1.0,  "#fddbc7"),
    (2.0,  "#f4a582"),
    (3.0,  "#d6604d"),
    (5.0,  "#b2182b"),
]

# 线程安全缓存
_lock = threading.Lock()
_cache = {}  # key → {"ts": float, "data": ...}


def _now():
    return time.time()


# ---------------------------------------------------------------------------
# ERDDAP 查询
# ---------------------------------------------------------------------------
def _fetch_crw_csv(variable, time_str=None):
    """从 NOAA ERDDAP 拉取指定变量在中国近海范围的 CSV。

    Args:
        variable: CRW_DHW | CRW_SSTANOMALY | CRW_HOTSPOT | CRW_SST | CRW_BAA
        time_str: 时间点 (如 "2026-11-04T00:00:00Z")，默认最新可用

    Returns:
        CSV 文本，或 None（失败时）
    """
    if time_str is None:
        time_str = "last"

    # ERDDAP CSV 查询语法:
    #   ?variable[(time)][(latMin):stride:(latMax)][(lonMin):stride:(lonMax)]
    url = (
        f"{CRW_ERDDAP}?"
        f"{variable}[({time_str})]"
        f"[({REGION['north']}):{GRID_STRIDE}:({REGION['south']})]"
        f"[({REGION['west']}):{GRID_STRIDE}:({REGION['east']})]"
    )
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception:
        return None


def _parse_csv_to_grid(csv_text):
    """把 ERDDAP CSV 解析为便于前端绘制的紧凑网格结构。

    CSV 列: time, latitude (degrees_north), longitude (degrees_east), value

    Returns:
        {
            "bounds": {"north": float, "south": float, "east": float, "west": float},
            "stride": int,    # 经纬度方向提取跨步（数据点间距）
            "lats": [float],  # 纬度数组 (从北到南)
            "lons": [float],  # 经度数组 (从西到东)
            "grid": [[float|null, ...], ...],  # 2D 值数组 [lat_idx][lon_idx]
            "variable": str,
            "time": str,
            "river_mask": bool  # 是否为陆地点 (NaN → 后处理为 null)
        }

    NaN 原值替换为 Python None → JSON null，前端跳过不画。
    """
    lines = csv_text.strip().split("\n")
    if len(lines) < 2:
        return None

    header = lines[0]
    # 确定值列名（最后一列）
    cols = header.split(",")
    value_col = cols[-1].strip()
    time_col = cols[0].strip()

    points = []
    data_time = None
    lat_set = set()
    lon_set = set()

    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            t_str = parts[0].strip()
            lat = round(float(parts[1].strip()), 4)
            lon = round(float(parts[2].strip()), 4)
            val_str = parts[3].strip()
            val = None if val_str == "NaN" else round(float(val_str), 4)
        except (ValueError, IndexError):
            continue

        if data_time is None:
            data_time = t_str
        points.append((lat, lon, val))
        lat_set.add(lat)
        lon_set.add(lon)

    if not points:
        return None

    # 排序坐标轴
    lats = sorted(lat_set, reverse=True)  # 北→南
    lons = sorted(lon_set)                # 西→东
    lat_idx = {v: i for i, v in enumerate(lats)}
    lon_idx = {v: i for i, v in enumerate(lons)}

    # 构建 2D 网格
    nlat, nlon = len(lats), len(lons)
    grid = [[None] * nlon for _ in range(nlat)]
    for lat, lon, val in points:
        grid[lat_idx[lat]][lon_idx[lon]] = val

    return {
        "bounds": {
            "north": lats[0] if lats else REGION["north"],
            "south": lats[-1] if lats else REGION["south"],
            "east": lons[-1] if lons else REGION["east"],
            "west": lons[0] if lons else REGION["west"],
        },
        "stride": GRID_STRIDE,
        "lats": lats,
        "lons": lons,
        "grid": grid,
        "variable": value_col,
        "time": data_time,
    }


# ---------------------------------------------------------------------------
# 公开接口（带 TTL 缓存）
# ---------------------------------------------------------------------------
def get_dhw_grid():
    """获取最新的 DHW 网格数据。

    Returns:
        dict (见 _parse_csv_to_grid 返回值) 或 None
    """
    key = "dhw_grid"
    with _lock:
        entry = _cache.get(key)
        if entry and (_now() - entry["ts"]) < TTL_SECONDS:
            return entry["data"]

    csv_text = _fetch_crw_csv("CRW_DHW")
    if csv_text is None:
        # 返回过期缓存（如果有）
        with _lock:
            stale = _cache.get(key)
            if stale:
                return stale["data"]
        return None

    data = _parse_csv_to_grid(csv_text)
    with _lock:
        _cache[key] = {"ts": _now(), "data": data}
    return data


def get_sstanomaly_grid():
    """获取最新的 SST 距平网格数据（相对 1985–2012 气候态）。

    Returns:
        dict 或 None
    """
    key = "ssta_grid"
    with _lock:
        entry = _cache.get(key)
        if entry and (_now() - entry["ts"]) < TTL_SECONDS:
            return entry["data"]

    csv_text = _fetch_crw_csv("CRW_SSTANOMALY")
    if csv_text is None:
        with _lock:
            stale = _cache.get(key)
            if stale:
                return stale["data"]
        return None

    data = _parse_csv_to_grid(csv_text)
    with _lock:
        _cache[key] = {"ts": _now(), "data": data}
    return data


def get_hotspot_grid():
    """获取最新的 HotSpot 网格数据（当前 SST 超出 MMM 的值）。

    Returns:
        dict 或 None
    """
    key = "hs_grid"
    with _lock:
        entry = _cache.get(key)
        if entry and (_now() - entry["ts"]) < TTL_SECONDS:
            return entry["data"]

    csv_text = _fetch_crw_csv("CRW_HOTSPOT")
    if csv_text is None:
        with _lock:
            stale = _cache.get(key)
            if stale:
                return stale["data"]
        return None

    data = _parse_csv_to_grid(csv_text)
    with _lock:
        _cache[key] = {"ts": _now(), "data": data}
    return data


# ---------------------------------------------------------------------------
# 站点级别的气候态 SST 对比（替换旧的一年同比）
# ---------------------------------------------------------------------------
def get_station_clim_anomaly(lat, lon):
    """获取指定坐标的 SST 距平值（相对于 1985–2012 气候态）。

    Args:
        lat: 纬度
        lon: 经度

    Returns:
        float SST 距平（°C），或 None
    """
    # 从已缓存的格网中查找最近点
    grid = get_sstanomaly_grid()
    if not grid or not grid.get("lats") or not grid.get("lons"):
        return None

    lats, lons = grid["lats"], grid["lons"]
    nlat, nlon = len(lats), len(lons)

    # 找到最近的网格点索引
    best_i, best_j = 0, 0
    best_dist = float("inf")
    for i, la in enumerate(lats):
        dlat = abs(la - lat)
        for j, lo in enumerate(lons):
            dlon = abs(lo - lon)
            d = dlat * dlat + dlon * dlon
            if d < best_dist:
                best_dist = d
                best_i, best_j = i, j

    if best_i < nlat and best_j < nlon:
        val = grid["grid"][best_i][best_j]
        return val

    return None


def get_all_station_anomalies():
    """批量获取所有 8 个站点的 SST 距平（一次网格查询，避免重复 HTTP）。

    Returns:
        dict: {station_id: anomaly_float, ...}
    """
    import config

    grid = get_sstanomaly_grid()
    if not grid:
        return {}

    lats, lons = grid["lats"], grid["lons"]
    nlat, nlon = len(lats), len(lons)
    if nlat == 0 or nlon == 0:
        return {}

    result = {}
    for s in config.MONITOR_STATIONS:
        best_val = None
        best_dist = float("inf")
        for i, la in enumerate(lats):
            dlat = abs(la - s["lat"])
            if dlat > 1.0:  # 提早剪枝
                continue
            for j, lo in enumerate(lons):
                dlon = abs(lo - s["lon"])
                d = dlat * dlat + dlon * dlon
                if d < best_dist:
                    best_dist = d
                    best_val = grid["grid"][i][j]
        result[s["id"]] = best_val

    return result
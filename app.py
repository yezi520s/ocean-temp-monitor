# -*- coding: utf-8 -*-
"""
海洋温度异常预警监测平台 —— 应用组装层。

职责：Flask 应用创建、路由注册、后台任务编排。
业务逻辑已下沉到 sync_service / alert_engine / report_service。
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request, render_template

import config
from database import init_db, query_all, get_conn, query_one
from sync_service import sync_all, has_historical_baseline, latest_data_span
from alert_engine import evaluate_all, evaluate_station
from report_service import generate_report, latest_report
import crw_service


# ======================================================================
# 多源外部数据接入（带 TTL 内存缓存，避免频繁打到第三方接口）
# ======================================================================
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_WEATHER_429_UNTIL = 0.0  # global weather 429 cooldown (monotonic timestamp)
_WEATHER_429_LOCK = threading.Lock()


def _cache_get(key, ttl):
    """读取缓存；过期返回 None。

    条目自带 ttl 时优先用它（失败结果需要更短的 TTL），
    否则沿用调用方传入的 ttl —— 保持 fishery 等旧调用方行为不变。
    """
    with _CACHE_LOCK:
        item = _CACHE.get(key)
    if not item:
        return None
    effective_ttl = item.get("ttl") if item.get("ttl") is not None else ttl
    if time.time() - item["ts"] > effective_ttl:
        return None
    return item["data"]


def _cache_set(key, data, ttl=None):
    """写入缓存；ttl=None 表示由读取方的 ttl 决定（旧行为）。"""
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": time.time(), "data": data, "ttl": ttl}


def _load_external(kind, station_id, lat, lon, fetch_fn):
    """读取外部实时源，带负缓存与陈旧回退。

    返回 (payload, meta)：
      - payload：fetch 结果或最近一次成功结果（可能 hourly 为空）
      - meta.stale=True 表示上游当前失败、正在回退旧数据
      - meta.error  上游失败原因（成功时为 None）

    负缓存（EXTERNAL_TTL_FAIL）让失败结果也占住缓存位，避免每个请求
    都重新打上游、持续撞限流；成功结果同时写入长 TTL 的 *-ok 键，
    供短时上游故障时回退。
    """
    key = f"{kind}:{station_id}"
    ok_key = f"{kind}-ok:{station_id}"
    cached = _cache_get(key, config.EXTERNAL_TTL_OK)
    if cached is not None:
        if cached.get("ok"):
            return cached, {"stale": False, "error": None}
        stale = _cache_get(ok_key, config.EXTERNAL_STALE_TTL)
        if stale is not None:
            return stale, {"stale": True, "error": cached.get("error")}
        return cached, {"stale": False, "error": cached.get("error")}

    result = fetch_fn(lat, lon)
    if result.get("ok"):
        _cache_set(key, result, ttl=config.EXTERNAL_TTL_OK)
        _cache_set(ok_key, result, ttl=config.EXTERNAL_STALE_TTL)
        return result, {"stale": False, "error": None}

    _cache_set(key, result, ttl=config.EXTERNAL_TTL_FAIL)
    stale = _cache_get(ok_key, config.EXTERNAL_STALE_TTL)
    if stale is not None:
        return stale, {"stale": True, "error": result.get("error")}
    return result, {"stale": False, "error": result.get("error")}


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 源 1
def _fetch_hourly_api(url, variables, lat, lon, forecast_days, timeout):
    """请求 Open-Meteo 类接口，返回 (payload, error)。

    429 专用指数退避 + jitter，尊重 Retry-After 头；
    5xx 仍用短退避重试。总耗时远小于 gunicorn --timeout 120。
    """
    import random as _random
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(variables),
        "timezone": config.TIMEZONE,
        "forecast_days": forecast_days,
    }
    headers = {
        "User-Agent": "OceanTempMonitor/1.0 (https://github.com/yezi520s/ocean-temp-monitor)",
        "Accept": "application/json",
    }
    last_error = None
    for attempt in range(config.EXTERNAL_RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code in config.EXTERNAL_RETRY_STATUS:
                last_error = f"{resp.status_code}"
                if attempt < config.EXTERNAL_RETRY_ATTEMPTS - 1:
                    if resp.status_code == 429:
                        # 429：指数退避 2^attempt s + jitter ±30%，尊重 Retry-After
                        retry = resp.headers.get("Retry-After")
                        if retry is not None:
                            try:
                                delay = float(retry)
                            except (ValueError, TypeError):
                                delay = 2.0 ** attempt
                        else:
                            delay = 2.0 ** attempt
                        delay += _random.uniform(-0.3 * delay, 0.3 * delay)
                        delay = max(1.0, min(delay, 12.0))
                    else:
                        delay = min(config.EXTERNAL_BACKOFF_MAX, 0.5 * (2 ** attempt))
                    time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json(), None
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < config.EXTERNAL_RETRY_ATTEMPTS - 1:
                time.sleep(min(config.EXTERNAL_BACKOFF_MAX,
                               0.5 * (2 ** attempt)))
    return None, last_error


def fetch_marine_live(lat, lon, forecast_days=2, timeout=None):
    """Open-Meteo Marine API —— 海浪 / 涌浪 / 海流 / 海表温度。"""
    timeout = config.EXTERNAL_TIMEOUT if timeout is None else timeout
    variables = [
        "sea_surface_temperature",
        "wave_height", "wave_direction", "wave_period",
        "swell_wave_height", "swell_wave_period",
        "ocean_current_velocity", "ocean_current_direction",
    ]
    payload, error = _fetch_hourly_api(
        config.MARINE_API_URL, variables, lat, lon, forecast_days, timeout)
    if payload is None:
        return {"ok": False, "error": error}
    hourly = payload.get("hourly") or {}
    return {"ok": True, "hourly": hourly,
            "utc_offset_seconds": payload.get("utc_offset_seconds")}


# ---------------------------------------------------------------- 源 2
_WIND_DIR = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5,
    "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
    "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}


def _wind_dir_to_deg(v):
    """风向字符串 → 度数（7Timer civil 产品用文字风向）。"""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return _WIND_DIR.get(str(v).upper(), None)


def _fetch_7timer_weather(lat, lon, timeout):
    """7Timer! Astro meteo — 免费无密钥 GFS 天气 API（Open-Meteo 429 回退）。
    
    meteo 产品包含 msl_pressure，风向为数字。
    返回与 Open-Meteo hourly 格式兼容的字典。
    """
    try:
        url = f"http://www.7timer.info/bin/api.pl?lon={lon}&lat={lat}&product=meteo&output=json"
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return None, f"7Timer: {exc}"

    series = data.get("dataseries") or []
    if not series:
        return None, "7Timer: 无数据"

    init_str = data.get("init", "")
    if len(init_str) >= 10:
        try:
            base = datetime(int(init_str[:4]), int(init_str[4:6]), int(init_str[6:8]),
                            int(init_str[8:10]), tzinfo=timezone.utc)
        except (ValueError, TypeError):
            base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    else:
        base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    times, temps, wind_s, wind_d, press, precip, cloud, rh = [], [], [], [], [], [], [], []
    for row in series:
        step_h = row.get("timepoint", 0)
        t = base + timedelta(hours=step_h)
        times.append(t.isoformat())
        temps.append(row.get("temp2m"))
        w10 = row.get("wind10m") or {}
        ws = w10.get("speed")
        wind_s.append(None if ws == -9999 else ws)
        wd = _wind_dir_to_deg(w10.get("direction"))
        wind_d.append(wd)
        mp = row.get("msl_pressure")
        press.append(None if mp == -9999 else mp)
        pt = row.get("prec_type", "")
        pa = row.get("prec_amount", 0)
        precip.append(pa if pt == "rain" else 0)
        cc = row.get("cloudcover")
        cloud.append(None if cc is None or cc == -9999 else cc)
        rh_val = row.get("rh2m")
        if isinstance(rh_val, str):
            rh_val = rh_val.replace("%", "")
        try:
            rh_val = float(rh_val)
        except (ValueError, TypeError):
            rh_val = None
        rh.append(None if rh_val == -9999 else rh_val)

    return {"ok": True, "hourly": {
        "time": times,
        "temperature_2m": temps,
        "wind_speed_10m": wind_s,
        "wind_direction_10m": wind_d,
        "surface_pressure": press,
        "precipitation": precip,
        "cloud_cover": cloud,
        "relative_humidity_2m": rh,
    }, "utc_offset_seconds": 8 * 3600}, None


def fetch_weather_live(lat, lon, forecast_days=2, timeout=None):
    """气象要素 —— 优先 Open-Meteo，被限 429 时回退 7Timer! GFS。"""
    global _WEATHER_429_UNTIL
    with _WEATHER_429_LOCK:
        if time.monotonic() < _WEATHER_429_UNTIL:
            return {"ok": False, "error": "429 上游限流（全局冷却中）"}
    timeout = config.EXTERNAL_TIMEOUT if timeout is None else timeout
    variables = [
        "temperature_2m", "wind_speed_10m", "wind_direction_10m",
        "surface_pressure", "precipitation", "cloud_cover", "relative_humidity_2m",
    ]
    payload, error = _fetch_hourly_api(
        "https://api.open-meteo.com/v1/forecast",
        variables, lat, lon, forecast_days, timeout)
    if payload is None:
        if error and "429" in str(error):
            with _WEATHER_429_LOCK:
                _WEATHER_429_UNTIL = time.monotonic() + 120.0
        # 回退到 7Timer! GFS
        payload2, err2 = _fetch_7timer_weather(lat, lon, timeout)
        if payload2 is not None:
            payload2["source"] = "7Timer! GFS (Open-Meteo 限流回退)"
            return payload2
        return {"ok": False, "error": f"Open-Meteo: {error} | 7Timer: {err2}"}
    hourly = payload.get("hourly") or {}
    payload["source"] = "Open-Meteo"
    return {"ok": True, "hourly": hourly,
            "utc_offset_seconds": payload.get("utc_offset_seconds")}


# ---------------------------------------------------------------- 源 3
FISHERY_INDICATORS = [
    ("ER.FSH.PROD.MT", "渔业总产量", "吨"),
    ("ER.FSH.CAPT.MT", "海洋捕捞产量", "吨"),
    ("ER.FSH.AQUA.MT", "水产养殖产量", "吨"),
]


def fetch_fishery_stats(timeout=25):
    """World Bank Open Data —— 中国渔业产量（年度）。"""
    out = {"ok": True, "country": "CHN", "series": [], "updated_at": _now_iso()}
    for code, label, unit in FISHERY_INDICATORS:
        points_rows = None
        last_error = None
        for attempt in range(2):
            try:
                resp = requests.get(
                    f"https://api.worldbank.org/v2/country/CHN/indicator/{code}",
                    params={"format": "json", "per_page": 80, "date": "2000:2024"},
                    timeout=timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
                points_rows = []
                for rec in (payload[1] or []):
                    if rec.get("value") is not None:
                        points_rows.append({"year": rec["date"], "value": round(float(rec["value"]), 2)})
                points_rows.sort(key=lambda r: r["year"])
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == 0:
                    time.sleep(0.8)
        if points_rows is None:
            out["ok"] = False
            out.setdefault("errors", []).append(f"{code}: {last_error}")
            continue
        out["series"].append({
            "code": code, "label": label, "unit": unit,
            "points": points_rows,
            "latest": points_rows[-1] if points_rows else None,
        })
    return out


# ---------------------------------------------------------------- 渔业适宜度
def _sea_state_level(wave_h):
    """按浪高判定海况等级与作业适宜度。"""
    if wave_h is None:
        return {"level": "未知", "suitability": None, "color": "muted"}
    if wave_h < 0.5:
        return {"level": "0–1 级（微浪）", "suitability": "优", "color": "good"}
    if wave_h < 1.25:
        return {"level": "2–3 级（轻浪）", "suitability": "良", "color": "good"}
    if wave_h < 2.5:
        return {"level": "4 级（中浪）", "suitability": "一般", "color": "warn"}
    if wave_h < 4.0:
        return {"level": "5 级（大浪）", "suitability": "较差", "color": "bad"}
    return {"level": "6 级以上（巨浪）", "suitability": "恶劣", "color": "bad"}


def _wind_level(speed_kmh):
    if speed_kmh is None:
        return {"level": "未知", "suitability": None, "color": "muted"}
    if speed_kmh < 20:
        return {"level": "和风", "suitability": "优", "color": "good"}
    if speed_kmh < 40:
        return {"level": "清劲风", "suitability": "良", "color": "good"}
    if speed_kmh < 62:
        return {"level": "强风", "suitability": "较差", "color": "warn"}
    return {"level": "疾风以上", "suitability": "恶劣", "color": "bad"}


_SUIT_RANK = {"优": 4, "良": 3, "一般": 2, "较差": 1, "恶劣": 0}


def _pick_current_index(times, utc_offset_seconds=None):
    """在逐小时序列中定位『当前时刻』的下标（找不到则用 0）。

    API 返回的 time 是按 config.TIMEZONE 标注的本地时刻（如 Asia/Shanghai），
    而服务器（Render 容器）进程时区是 UTC。直接比较 datetime.now() 会取到
    偏移 8 小时前的槽位，因此这里必须换算到数据自身所用的时区。

    utc_offset_seconds 取自 API 响应的 utc_offset_seconds 字段，缺失时回退到
    config.TIMEZONE_UTC_OFFSET_SECONDS。
    """
    if not times:
        return 0
    if utc_offset_seconds is None:
        utc_offset_seconds = config.TIMEZONE_UTC_OFFSET_SECONDS
    now = datetime.now(timezone(timedelta(seconds=int(utc_offset_seconds)))).strftime("%Y-%m-%dT%H")
    for i, t in enumerate(times):
        if str(t).startswith(now):
            return i
    return 0


def _series_stats(values, start_idx, span=24):
    """取 start_idx 起 span 个点的统计量。"""
    chunk = [v for v in values[start_idx:start_idx + span] if v is not None]
    if not chunk:
        return {"now": None, "avg": None, "max": None, "min": None}
    return {
        "now": values[start_idx] if start_idx < len(values) else None,
        "avg": round(sum(chunk) / len(chunk), 2),
        "max": max(chunk),
        "min": min(chunk),
    }


def create_app():
    app = Flask(__name__)
    # 开发期模板改动即时生效（生产部署时由 gunicorn 的 workers 数控制，影响可忽略）
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # 禁止缓存，确保前端始终加载最新版
    @app.after_request
    def no_cache(response):
        response.cache_control.no_cache = True
        response.cache_control.no_store = True
        response.cache_control.must_revalidate = True
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    # 后台刷新任务状态（单 worker 进程内共享）
    app.config["REFRESH_STATUS"] = {
        "running": False, "results": None, "time": None, "error": None,
    }
    app.config["REFRESH_LOCK"] = threading.Lock()

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------
    @app.route("/")
    def index():
        return render_template("index.html")

    # ------------------------------------------------------------------
    # 站点
    # ------------------------------------------------------------------
    @app.route("/api/stations", methods=["GET"])
    def stations():
        return jsonify(config.MONITOR_STATIONS)

    # ------------------------------------------------------------------
    # 数据同步
    # ------------------------------------------------------------------
    @app.route("/api/refresh", methods=["POST"])
    def refresh():
        lock = app.config["REFRESH_LOCK"]
        status = app.config["REFRESH_STATUS"]
        with lock:
            if status["running"]:
                return jsonify({"status": "running", "message": "刷新任务已在执行中"})
            status.update({"running": True, "results": None, "time": None, "error": None})

        historical = request.args.get("historical", "auto")
        if historical == "auto":
            historical = not has_historical_baseline()

        def _bg():
            status = app.config["REFRESH_STATUS"]
            lock = app.config["REFRESH_LOCK"]
            try:
                results = sync_all(historical=historical)
                with lock:
                    status.update({
                        "running": False, "results": results,
                        "time": datetime.now().isoformat(), "error": None,
                        "historical": historical,
                    })
            except Exception as exc:  # noqa: BLE001
                with lock:
                    status.update({
                        "running": False, "results": None,
                        "time": datetime.now().isoformat(), "error": str(exc),
                        "historical": historical,
                    })

        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({"status": "started", "historical": historical,
                        "message": "后台同步已启动"})

    @app.route("/api/refresh_status", methods=["GET"])
    def refresh_status():
        with app.config["REFRESH_LOCK"]:
            return jsonify(dict(app.config["REFRESH_STATUS"]))

    # ------------------------------------------------------------------
    # 单站点时序
    # ------------------------------------------------------------------
    @app.route("/api/sst/<int:station_id>", methods=["GET"])
    def sst(station_id):
        hours = request.args.get("hours", 168, type=int)
        # 只取「过去 N 小时」的实测/历史数据，不含未来预报（趋势图应展示真实历史）
        rows = query_all(
            """
            SELECT timestamp, temperature FROM sst_data
            WHERE station_id = ?
              AND timestamp <= datetime('now')
              AND timestamp >= datetime('now', ?)
            ORDER BY timestamp
            """,
            (station_id, f"-{hours} hours"),
        )
        if not rows:
            return jsonify({"error": "暂无数据，请先同步", "times": [], "temps": []})

        times = [r["timestamp"] for r in rows]
        temps = [r["temperature"] for r in rows]
        return jsonify({
            "times": times,
            "temps": temps,
            "current": temps[-1],
            "avg": round(sum(temps) / len(temps), 2),
            "max": max(temps),
            "min": min(temps),
            "trend": round(temps[-1] - temps[0], 2) if len(temps) >= 2 else 0,
            "count": len(temps),
        })

    # ------------------------------------------------------------------
    # 总览（地图 + 卡片）
    # ------------------------------------------------------------------
    @app.route("/api/overview", methods=["GET"])
    def overview():
        from datetime import timedelta
        result = []
        for station in config.MONITOR_STATIONS:
            alerts, score, level, label, metrics = evaluate_station(station, save=False)
            result.append({
                "id": station["id"],
                "name": station["name"],
                "region": station["region"],
                "lat": station["lat"],
                "lon": station["lon"],
                "current_temp": metrics.get("current"),
                "avg_24h": metrics.get("prev_24h"),
                "score": score,
                "level": level,
                "label": label,
                "alert_count": len(alerts),
            })
        return jsonify(result)

    # ------------------------------------------------------------------
    # 告警列表
    # ------------------------------------------------------------------
    @app.route("/api/alerts", methods=["GET"])
    def alerts():
        limit = request.args.get("limit", 50, type=int)
        rows = query_all(
            """
            SELECT station_name, alert_type, dimension, alert_level, message,
                   temperature, reference_value, target_time, created_at
            FROM alert_log ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        return jsonify([dict(r) for r in rows])

    # ------------------------------------------------------------------
    # 智能分析 + 预警报告
    # ------------------------------------------------------------------
    @app.route("/api/run_analysis", methods=["POST"])
    def run_analysis():
        evaluation = evaluate_all(save=True)
        report = generate_report(evaluation)
        return jsonify({
            "status": "ok",
            "report": report,
            "time": report["generated_at"],
        })

    @app.route("/api/report", methods=["GET"])
    def get_report():
        report = latest_report()
        if report is None:
            return jsonify({"status": "empty", "message": "暂无报告，请先执行智能分析"})
        return jsonify({"status": "ok", "report": report})

    # ------------------------------------------------------------------
    # 数据新鲜度
    # ------------------------------------------------------------------
    @app.route("/api/station/<int:station_id>/detail", methods=["GET"])
    def station_detail(station_id):
        """返回单个站点详细信息（统计指标 + 海洋知识卡片）。"""
        station = config.STATION_BY_ID.get(station_id)
        if not station:
            return jsonify({"error": "未知站点"}), 404

        # 当前与近期统计
        current_row = query_one(
            """SELECT temperature FROM sst_data
               WHERE station_id = ? AND timestamp <= datetime('now')
               ORDER BY timestamp DESC LIMIT 1""",
            (station_id,),
        )
        avg_24h = query_one(
            """SELECT ROUND(AVG(temperature), 2) as v FROM sst_data
               WHERE station_id = ? AND timestamp >= datetime('now', '-24 hours')""",
            (station_id,),
        )
        avg_7d = query_one(
            """SELECT ROUND(AVG(temperature), 2) as v FROM sst_data
               WHERE station_id = ? AND timestamp >= datetime('now', '-7 days')""",
            (station_id,),
        )
        max_7d = query_one(
            """SELECT MAX(temperature) as v FROM sst_data
               WHERE station_id = ? AND timestamp >= datetime('now', '-7 days')""",
            (station_id,),
        )
        min_7d = query_one(
            """SELECT MIN(temperature) as v FROM sst_data
               WHERE station_id = ? AND timestamp >= datetime('now', '-7 days')""",
            (station_id,),
        )
        yoy = query_one(
            """SELECT ROUND(AVG(temperature), 2) as v FROM sst_data
               WHERE station_id = ? AND timestamp >= datetime('now', '-380 days')
                 AND timestamp <= datetime('now', '-350 days')""",
            (station_id,),
        )
        # 警报统计
        alert_count = query_one(
            "SELECT COUNT(*) as v FROM alert_log WHERE station_name = ?",
            (station["name"],),
        )

        # 告警与评分 — 优先使用 CRW 气候态基线
        clim_anomaly = None
        try:
            clim_anomaly = crw_service.get_station_clim_anomaly(
                station["lat"], station["lon"])
        except Exception:
            pass
        alerts, score, level, label, metrics = evaluate_station(
            station, save=False, clim_anomaly=clim_anomaly)

        # 海洋知识卡片
        knowledge = OCEAN_KNOWLEDGE.get(station["name"], "")

        return jsonify({
            "id": station["id"],
            "name": station["name"],
            "region": station["region"],
            "lat": station["lat"],
            "lon": station["lon"],
            "current_temp": current_row["temperature"] if current_row else None,
            "avg_24h": avg_24h["v"] if avg_24h else None,
            "avg_7d": avg_7d["v"] if avg_7d else None,
            "max_7d": max_7d["v"] if max_7d else None,
            "min_7d": min_7d["v"] if min_7d else None,
            "yoy": yoy["v"] if yoy else None,
            "score": score,
            "level": level,
            "label": label,
            "alert_count": alert_count["v"] if alert_count else 0,
            "active_alerts": [a["alert_type"] for a in alerts],
            "clim_anomaly": metrics.get("clim_anomaly"),
            "baseline_type": metrics.get("baseline_type", "YoY"),
            "knowledge": knowledge,
        })

    # ------------------------------------------------------------------
    # 数据链路（采集 / 处理 / 分析 / 预警预报）运行统计
    # ------------------------------------------------------------------
    @app.route("/api/pipeline", methods=["GET"])
    def pipeline():
        total = query_one("SELECT COUNT(*) AS v FROM sst_data")
        hist = query_one("SELECT COUNT(*) AS v FROM sst_data WHERE timestamp <= datetime('now')")
        fut = query_one("SELECT COUNT(*) AS v FROM sst_data WHERE timestamp > datetime('now')")
        span = query_one("SELECT MIN(timestamp) AS lo, MAX(timestamp) AS hi FROM sst_data")
        fetched = query_one("SELECT MAX(fetched_at) AS v FROM sst_data")
        alerts = query_one("SELECT COUNT(*) AS v FROM alert_log")
        stations = query_one("SELECT COUNT(DISTINCT station_id) AS v FROM sst_data")

        # 日均数据点
        days = 1
        if span and span["lo"] and span["hi"]:
            from datetime import datetime as _dt
            try:
                lo = _dt.fromisoformat(span["lo"])
                hi = _dt.fromisoformat(span["hi"])
                days = max(1, (hi - lo).days + 1)
            except ValueError:
                pass

        return jsonify({
            "total_records": total["v"] if total else 0,
            "history_records": hist["v"] if hist else 0,
            "forecast_records": fut["v"] if fut else 0,
            "stations": stations["v"] if stations else 0,
            "span_days": days,
            "points_per_day": round((total["v"] if total else 0) / days, 1),
            "alert_count": alerts["v"] if alerts else 0,
            "last_fetched": fetched["v"] if fetched else None,
            "span": {"min": span["lo"] if span else None, "max": span["hi"] if span else None},
            "source": {
                "name": "Open-Meteo Marine API",
                "url": "https://marine-api.open-meteo.com/v1/marine",
                "variable": "sea_surface_temperature（海表温度 SST）",
                "frequency": "逐小时",
                "timezone": "Asia/Shanghai",
            },
        })

    # ------------------------------------------------------------------
    # 预警预报：未来 7 天逐日海温预测
    # ------------------------------------------------------------------
    @app.route("/api/forecast", methods=["GET"])
    def forecast():
        station_id = request.args.get("station_id", type=int)
        rows = query_all(
            """
            SELECT station_id, station_name, substr(timestamp, 1, 10) AS day,
                   ROUND(AVG(temperature), 2) AS avg_t,
                   ROUND(MAX(temperature), 2) AS max_t,
                   ROUND(MIN(temperature), 2) AS min_t
            FROM sst_data
            WHERE timestamp > datetime('now')
              AND timestamp <= datetime('now', '+7 days')
              AND (? IS NULL OR station_id = ?)
            GROUP BY station_id, day
            ORDER BY station_id, day
            """,
            (station_id, station_id),
        )

        by_station = {}
        for r in rows:
            sid = r["station_id"]
            by_station.setdefault(sid, {
                "id": sid,
                "name": r["station_name"],
                "days": [],
            })["days"].append({
                "day": r["day"],
                "avg": r["avg_t"],
                "max": r["max_t"],
                "min": r["min_t"],
            })

        # 预报风险提示：均温 >= 30 或 <= 15 标记
        result = []
        for item in by_station.values():
            item["days"].sort(key=lambda d: d["day"])
            highs = [d["max"] for d in item["days"] if d["max"] is not None]
            lows = [d["min"] for d in item["days"] if d["min"] is not None]
            item["peak_max"] = max(highs) if highs else None
            item["low_min"] = min(lows) if lows else None
            if item["peak_max"] is not None and item["peak_max"] >= 30:
                item["risk"] = "danger"
                item["risk_label"] = "高温风险"
            elif item["low_min"] is not None and item["low_min"] <= 15:
                item["risk"] = "warning"
                item["risk_label"] = "低温风险"
            else:
                item["risk"] = "normal"
                item["risk_label"] = "平稳"
            result.append(item)

        result.sort(key=lambda x: x["id"])
        return jsonify(result)

    # ------------------------------------------------------------------
    # 多源数据：海浪 / 气象 / 渔业适宜度（实时调取外部接口）
    # ------------------------------------------------------------------
    @app.route("/api/sources/<int:station_id>", methods=["GET"])
    def sources(station_id):
        station = config.STATION_BY_ID.get(station_id)
        if not station:
            return jsonify({"error": "未知站点"}), 404

        lat, lon = station["lat"], station["lon"]

        # ?preload=1：后台预拉取模式，只请求 Marine API（Weather 在 Render 上被主站限流）
        preload_only = request.args.get("preload", "") == "1"

        # --- 源 1 / 源 2：海洋要素与气象要素
        # 失败也进缓存（短 TTL 负缓存），避免每个请求都去撞限流；
        # 上游持续失败时回退最近一次成功数据，并标记 stale，避免整页空白。
        # 预拉取模式跳过 Weather（Render 共享 IP 被 Open-Meteo 主站限流）；
        # 用户实际查看某站时才按需拉取气象数据。
        marine, marine_meta = _load_external("marine", station_id, lat, lon, fetch_marine_live)
        if preload_only:
            weather = {"ok": False, "error": "后台预拉取，气象数据按需加载"}
            weather_meta = {"stale": False}
        else:
            weather, weather_meta = _load_external("weather", station_id, lat, lon, fetch_weather_live)

        hourly_m = marine.get("hourly") or {}
        hourly_w = weather.get("hourly") or {}
        times = hourly_m.get("time") or hourly_w.get("time") or []
        # 优先采用数据自身返回的时区偏移，缺失时回退配置值
        utc_offset = marine.get("utc_offset_seconds")
        if utc_offset is None:
            utc_offset = weather.get("utc_offset_seconds")
        idx = _pick_current_index(times, utc_offset)

        def stat(key, src="marine"):
            src_hourly = hourly_m if src == "marine" else hourly_w
            return _series_stats(src_hourly.get(key) or [], idx)

        sst = stat("sea_surface_temperature")
        wave_h = stat("wave_height")
        wave_p = stat("wave_period")
        swell_h = stat("swell_wave_height")
        current_v = stat("ocean_current_velocity")
        current_d = stat("ocean_current_direction")
        wave_d = stat("wave_direction")

        air_t = stat("temperature_2m", "weather")
        wind_s = stat("wind_speed_10m", "weather")
        wind_d = stat("wind_direction_10m", "weather")
        pressure = stat("surface_pressure", "weather")
        precip = stat("precipitation", "weather")
        cloud = stat("cloud_cover", "weather")
        humidity = stat("relative_humidity_2m", "weather")

        sea_state = _sea_state_level(wave_h["now"])
        wind_state = _wind_level(wind_s["now"])

        # 综合渔业作业适宜度：取海况与风的较差者
        ranks = [r for r in (_SUIT_RANK.get(sea_state["suitability"]),
                             _SUIT_RANK.get(wind_state["suitability"])) if r is not None]
        if ranks:
            worst = min(ranks)
            overall = {v: k for k, v in _SUIT_RANK.items()}.get(worst, "未知")
            overall_color = "good" if worst >= 3 else ("warn" if worst == 2 else "bad")
        else:
            overall, overall_color = "未知", "muted"

        advice = {
            "优": "海况平稳，适宜出海作业与捕捞。",
            "良": "海况较好，可正常作业，注意常规安全。",
            "一般": "海况转差，中小型渔船应谨慎出海。",
            "较差": "风浪较大，建议中小型渔船回港避风。",
            "恶劣": "海况恶劣，所有渔船应停止作业并回港避风。",
        }.get(overall, "暂无评估。")

        return jsonify({
            "station": {
                "id": station["id"], "name": station["name"],
                "region": station["region"], "lat": lat, "lon": lon,
            },
            "fetched_at": _now_iso(),
            "marine": {
                "ok": marine.get("ok", False),
                "source": "Open-Meteo Marine API",
                "url": config.MARINE_API_URL,
                "sst": sst, "wave_height": wave_h, "wave_period": wave_p,
                "wave_direction": wave_d, "swell_wave_height": swell_h,
                "current_velocity": current_v, "current_direction": current_d,
                "error": marine.get("error"),
            },
            "weather": {
                "ok": weather.get("ok", False),
                "source": "Open-Meteo Weather API",
                "url": "https://api.open-meteo.com/v1/forecast",
                "air_temp": air_t, "wind_speed": wind_s, "wind_direction": wind_d,
                "pressure": pressure, "precipitation": precip,
                "cloud_cover": cloud, "humidity": humidity,
                "error": weather.get("error"),
                "stale": weather_meta.get("stale", False),
            },
            "suitability": {
                "sea_state": sea_state,
                "wind_state": wind_state,
                "overall": overall,
                "overall_color": overall_color,
                "advice": advice,
            },
            "timeline": {
                "times": times[idx:idx + 24],
                "wave_height": (hourly_m.get("wave_height") or [])[idx:idx + 24],
                "wind_speed": (hourly_w.get("wind_speed_10m") or [])[idx:idx + 24],
                "sst": (hourly_m.get("sea_surface_temperature") or [])[idx:idx + 24],
            },
        })

    # ------------------------------------------------------------------
    # 渔业产量数据（World Bank Open Data）
    # ------------------------------------------------------------------
    @app.route("/api/fishery", methods=["GET"])
    def fishery():
        cached = _cache_get("fishery", 86400)  # 年度数据，缓存 1 天
        if cached is not None:
            return jsonify(cached)
        data = fetch_fishery_stats()
        if data.get("ok"):
            _cache_set("fishery", data)
        return jsonify(data)

    # ── NOAA CRW 热力图 / 气候态距平 ──
    @app.route("/api/crw/heatmap", methods=["GET"])
    def crw_heatmap():
        """DHW 度周热力图网格数据。
        前端用 Canvas 绘制空间热力覆盖层，替代固定标记。
        """
        dhw = crw_service.get_dhw_grid()
        if dhw is None:
            return jsonify({"error": "CRW data unavailable", "cached": False})
        # 不反序列化全部 grid — 直接返序列化后走 Flask jsonify
        return jsonify({
            "bounds": dhw["bounds"],
            "lats": dhw["lats"],
            "lons": dhw["lons"],
            "grid": dhw["grid"],
            "stride": dhw["stride"],
            "variable": dhw["variable"],
            "data_time": dhw["time"],
            "unit": "°C-weeks",
        })

    @app.route("/api/crw/anomaly", methods=["GET"])
    def crw_anomaly():
        """SST 距平网格（相对 1985–2012 气候态）。
        负值=偏冷，正=偏暖。"""
        ssta = crw_service.get_sstanomaly_grid()
        if ssta is None:
            return jsonify({"error": "CRW SST Anomaly unavailable", "cached": False})
        return jsonify({
            "bounds": ssta["bounds"],
            "lats": ssta["lats"],
            "lons": ssta["lons"],
            "grid": ssta["grid"],
            "stride": ssta["stride"],
            "variable": ssta["variable"],
            "data_time": ssta["time"],
            "unit": "°C (vs 1985–2012)",
        })

    @app.route("/api/crw/clim", methods=["GET"])
    def crw_clim():
        """8 站点的 CRW 气候态 SST 距平摘要。
        提供气候态基线数据给前端详情面板的「气候态距平」字段。
        """
        anoms = crw_service.get_all_station_anomalies()
        import config as cfg
        result = []
        for s in cfg.MONITOR_STATIONS:
            a = anoms.get(s["id"])
            result.append({
                "id": s["id"],
                "name": s["name"],
                "region": s["region"],
                "anomaly": round(a, 2) if a is not None else None,
                "label": _anomaly_label(a) if a is not None else None,
            })
        return jsonify({"stations": result, "data_time": _crw_data_time()})

    @app.route("/api/status", methods=["GET"])
    def status():
        return jsonify({
            "span": latest_data_span(),
            "has_historical_baseline": has_historical_baseline(),
        })

    return app


# ======================================================================
# 海洋知识卡片
# ======================================================================
OCEAN_KNOWLEDGE = {
    "大连海域": "渤海是中国的半封闭内海，平均水深仅 18 米，水温受大陆气候影响显著，冬季近岸可出现海冰。大连海域是北黄海-渤海交汇处，夏季 SST 通常 22–26℃，冬季可降至 0–4℃，年较差达 25℃ 以上，为我国近海水温变幅最大区域。",
    "青岛海域": "黄海西部受黄海暖流与沿岸流共同影响，青岛海域夏季表层水温约 23–27℃，冬季 5–8℃。该区域是黄海冷水团南缘，底层终年维持低温（<10℃），对渔业资源分布有重要影响。",
    "连云港海域": "连云港地处海州湾顶，水深较浅（<20 米），海水对流混合充分。该海域水温季节性强，易受陆地径流及苏北沿岸流影响，夏季高温可达 28–30℃，是赤潮高发区之一。",
    "舟山海域": "舟山群岛位于长江口南翼，受长江冲淡水、台湾暖流及浙闽沿岸流三重影响，水文环境极其复杂。舟山渔场是中国最大近海渔场，海温异常直接影响带鱼、大黄鱼等经济鱼类的洄游路线与产卵时间。",
    "厦门海域": "台湾海峡是东海与南海的水交换通道，厦门海域受黑潮分支（台湾暖流）影响，冬季水温仍可维持在 16–19℃。该海域潮差大、潮流强，是研究陆架-深海交换的热点区域。",
    "珠江口海域": "珠江口是南海北部最大河口湾，冲淡水向外扩散形成低盐羽状锋。夏季 SST 常达 29–31℃，是全球海洋热浪的高风险区之一。海温升高会加剧珊瑚白化风险，影响大亚湾及香港周边海域生态系统。",
    "三亚海域": "三亚海域属热带南海，终年高温，SST 年均约 26–28℃，季节变幅小于 6℃。该区域拥有丰富的珊瑚礁生态系统，对热应力极为敏感——当 SST 连续超过 30℃ 超过 4 周，珊瑚白化概率大幅上升。",
    "西沙海域": "西沙群岛位于南海中部深水区，水深 1000–3000 米，是典型的大洋性岛礁海域。该区域 SST 年际波动与 ENSO（厄尔尼诺-南方涛动）密切相关，是监测热带海洋对全球气候响应的重要前哨。",
}


def _anomaly_label(anomaly):
    """SST 距平 → 中文标签"""
    if anomaly is None:
        return None
    a = abs(anomaly)
    direction = "偏暖" if anomaly > 0 else "偏冷"
    if a < 0.5:
        return "正常"
    if a < 1.0:
        return f"略{direction}"
    if a < 2.0:
        return direction
    return f"显著{direction}"


def _crw_data_time():
    """获取 CRW 缓存的第一个可用数据时间"""
    dhw = crw_service.get_dhw_grid()
    if dhw and dhw.get("time"):
        return dhw["time"]
    ssta = crw_service.get_sstanomaly_grid()
    if ssta and ssta.get("time"):
        return ssta["time"]
    return None


app = create_app()
init_db()


if __name__ == "__main__":
    print("=" * 60)
    print("  海洋温度异常预警监测平台")
    print("  Ocean Temperature Anomaly Warning System")
    print("=" * 60)
    print(f"  监测站点: {len(config.MONITOR_STATIONS)} 个")
    print(f"  数据源: Open-Meteo Marine API")
    print(f"  启动地址: http://127.0.0.1:{config.PORT}")
    print("=" * 60)
    app.run(debug=False, host=config.HOST, port=config.PORT)

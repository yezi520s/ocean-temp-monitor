# -*- coding: utf-8 -*-
"""
海洋温度异常预警监测平台 - 后端服务
使用 Open-Meteo Marine API 获取实时海表温度数据
"""
import json
import os
import sqlite3
import time
import statistics
import threading
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, render_template
import requests

app = Flask(__name__)

# 禁止缓存 - 确保前端始终加载最新版
@app.after_request
def no_cache(response):
    response.cache_control.no_cache = True
    response.cache_control.no_store = True
    response.cache_control.must_revalidate = True
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# 刷新任务状态
_refresh_status = {"running": False, "results": None, "time": None, "error": None}
_refresh_lock = threading.Lock()

# ============================================================
# 监测站点配置 - 中国沿海关键海域
# ============================================================
MONITOR_STATIONS = [
    {"id": 1, "name": "大连海域", "lat": 38.90, "lon": 121.60, "region": "渤海"},
    {"id": 2, "name": "青岛海域", "lat": 36.10, "lon": 120.40, "region": "黄海"},
    {"id": 3, "name": "舟山海域", "lat": 30.00, "lon": 122.10, "region": "东海"},
    {"id": 4, "name": "厦门海域", "lat": 24.50, "lon": 118.10, "region": "台湾海峡"},
    {"id": 5, "name": "珠江口海域", "lat": 22.20, "lon": 113.60, "region": "南海北部"},
    {"id": 6, "name": "三亚海域", "lat": 18.20, "lon": 109.50, "region": "南海"},
    {"id": 7, "name": "西沙海域", "lat": 16.80, "lon": 112.30, "region": "南海"},
    {"id": 8, "name": "连云港海域", "lat": 34.60, "lon": 119.50, "region": "黄海"},
]

# ============================================================
# 预警阈值配置
# ============================================================
ALERT_CONFIG = {
    "temp_high": 30.0,         # 高温预警阈值 (℃)
    "temp_low": 15.0,          # 低温预警阈值 (℃) - 冬季
    "anomaly_threshold": 2.0,  # 异常偏离阈值 (℃) - 与历史均值偏差超过此值触发预警
    "trend_window": 24,        # 趋势分析窗口 (小时)
}

# ============================================================
# 数据库初始化 - SQLite 缓存
# ============================================================
DB_PATH = os.environ.get("DB_PATH", "ocean_data.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sst_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        station_id INTEGER,
        station_name TEXT,
        lat REAL,
        lon REAL,
        timestamp TEXT,
        temperature REAL,
        fetched_at TEXT,
        UNIQUE(station_id, timestamp)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS alert_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        station_id INTEGER,
        station_name TEXT,
        alert_type TEXT,
        alert_level TEXT,
        message TEXT,
        temperature REAL,
        created_at TEXT
    )''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_sst_station 
        ON sst_data(station_id, timestamp)''')
    conn.commit()
    conn.close()

init_db()

# ============================================================
# 数据获取 - Open-Meteo Marine API
# ============================================================
def fetch_sst_from_api(lat, lon, forecast_days=7):
    """从 Open-Meteo Marine API 获取海表温度数据"""
    url = "https://marine-api.open-meteo.com/v1/marine"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "sea_surface_temperature",
        "forecast_days": forecast_days,
        "timezone": "Asia/Shanghai",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "hourly" not in data or "sea_surface_temperature" not in data["hourly"]:
            return None
        return data["hourly"]
    except Exception as e:
        print(f"API 请求失败 ({lat},{lon}): {e}")
        return None

def cache_sst_data(station_id, station_name, lat, lon):
    """获取并缓存单个站点的 SST 数据"""
    hourly = fetch_sst_from_api(lat, lon)
    if not hourly:
        return False
    
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    for t, temp in zip(hourly["time"], hourly["sea_surface_temperature"]):
        if temp is None:
            continue
        try:
            c.execute(
                "INSERT OR REPLACE INTO sst_data (station_id, station_name, lat, lon, timestamp, temperature, fetched_at) VALUES (?,?,?,?,?,?,?)",
                (station_id, station_name, lat, lon, t, round(temp, 2), now)
            )
        except:
            pass
    
    conn.commit()
    conn.close()
    return True

# ============================================================
# 预警检测引擎
# ============================================================
def check_anomaly(station_id, station_name, current_temp, temp_history, target_time):
    """检测温度异常并生成预警"""
    alerts = []
    
    # 1. 高温预警
    if current_temp >= ALERT_CONFIG["temp_high"]:
        alerts.append({
            "station_id": station_id,
            "station_name": station_name,
            "alert_type": "高温预警",
            "alert_level": "danger" if current_temp >= 32 else "warning",
            "message": f"海表温度达到 {current_temp}℃，超过高温阈值 {ALERT_CONFIG['temp_high']}℃，可能导致珊瑚白化、海洋生态受损",
            "temperature": current_temp,
            "time": target_time,
        })
    
    # 2. 低温预警（冬季）
    if current_temp <= ALERT_CONFIG["temp_low"]:
        alerts.append({
            "station_id": station_id,
            "station_name": station_name,
            "alert_type": "低温预警",
            "alert_level": "warning",
            "message": f"海表温度降至 {current_temp}℃，低于正常范围，需关注海洋生物受冻风险",
            "temperature": current_temp,
            "time": target_time,
        })
    
    # 3. 异常偏离检测（与近期均值对比）
    if temp_history and len(temp_history) >= 12:
        avg = statistics.mean(temp_history)
        deviation = abs(current_temp - avg)
        if deviation >= ALERT_CONFIG["anomaly_threshold"]:
            direction = "偏高" if current_temp > avg else "偏低"
            alerts.append({
                "station_id": station_id,
                "station_name": station_name,
                "alert_type": f"温度异常{direction}",
                "alert_level": "warning",
                "message": f"当前温度 {current_temp}℃ 较近期均值 {avg:.1f}℃ {direction} {deviation:.1f}℃，可能存在海洋温度异常事件",
                "temperature": current_temp,
                "time": target_time,
            })
    
    # 4. 快速升温/降温趋势检测
    if temp_history and len(temp_history) >= 6:
        recent_avg = statistics.mean(temp_history[-3:])
        earlier_avg = statistics.mean(temp_history[-6:-3])
        trend = recent_avg - earlier_avg
        if abs(trend) >= 1.5:
            direction = "快速升温" if trend > 0 else "快速降温"
            alerts.append({
                "station_id": station_id,
                "station_name": station_name,
                "alert_type": f"{direction}趋势",
                "alert_level": "info",
                "message": f"过去6小时内温度{direction} {abs(trend):.1f}℃，请持续关注",
                "temperature": current_temp,
                "time": target_time,
            })
    
    # 保存告警
    if alerts:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for a in alerts:
            try:
                c.execute(
                    "INSERT INTO alert_log (station_id, station_name, alert_type, alert_level, message, temperature, created_at) VALUES (?,?,?,?,?,?,?)",
                    (a["station_id"], a["station_name"], a["alert_type"], 
                     a["alert_level"], a["message"], a["temperature"], datetime.now().isoformat())
                )
            except:
                pass
        conn.commit()
        conn.close()
    
    return alerts

# ============================================================
# API 路由
# ============================================================

@app.route('/')
def index():
    """主页面"""
    return render_template('index.html')

@app.route('/api/refresh', methods=['POST'])
def refresh_data():
    """刷新所有站点数据 - 后台异步执行（单 worker 安全）"""
    global _refresh_status, _refresh_lock
    with _refresh_lock:
        if _refresh_status["running"]:
            return jsonify({"status": "running", "message": "刷新任务已在执行中"})
        _refresh_status = {"running": True, "results": None, "time": None, "error": None}
    
    def _bg_refresh():
        global _refresh_status, _refresh_lock
        try:
            results = []
            for station in MONITOR_STATIONS:
                ok = cache_sst_data(station["id"], station["name"], station["lat"], station["lon"])
                results.append({"station": station["name"], "success": ok})
            with _refresh_lock:
                _refresh_status = {"running": False, "results": results, "time": datetime.now().isoformat(), "error": None}
        except Exception as e:
            with _refresh_lock:
                _refresh_status = {"running": False, "results": None, "time": datetime.now().isoformat(), "error": str(e)}
    
    threading.Thread(target=_bg_refresh, daemon=True).start()
    return jsonify({"status": "started", "message": "后台刷新已启动"})

@app.route('/api/refresh_status', methods=['GET'])
def refresh_status():
    """查询刷新任务状态"""
    global _refresh_status, _refresh_lock
    with _refresh_lock:
        s = dict(_refresh_status)
    return jsonify(s)

@app.route('/api/stations', methods=['GET'])
def get_stations():
    """获取所有监测站点信息"""
    return jsonify(MONITOR_STATIONS)

@app.route('/api/sst/<int:station_id>', methods=['GET'])
def get_sst_data(station_id):
    """获取指定站点的 SST 数据"""
    hours = request.args.get('hours', 168, type=int)  # 默认7天
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    c.execute(
        "SELECT timestamp, temperature FROM sst_data WHERE station_id=? AND timestamp >= ? ORDER BY timestamp",
        (station_id, cutoff)
    )
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return jsonify({"error": "暂无数据，请先刷新", "times": [], "temps": []})
    
    times = [r[0] for r in rows]
    temps = [r[1] for r in rows]
    
    # 计算统计信息
    avg_temp = round(statistics.mean(temps), 2)
    max_temp = round(max(temps), 2)
    min_temp = round(min(temps), 2)
    current = temps[-1] if temps else None
    trend = round(temps[-1] - temps[0], 2) if len(temps) >= 2 else 0
    
    return jsonify({
        "times": times,
        "temps": temps,
        "current": current,
        "avg": avg_temp,
        "max": max_temp,
        "min": min_temp,
        "trend": trend,
        "count": len(temps),
    })

@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    """获取预警列表"""
    limit = request.args.get('limit', 50, type=int)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT station_name, alert_type, alert_level, message, temperature, created_at FROM alert_log ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    rows = c.fetchall()
    conn.close()
    
    alerts = []
    for r in rows:
        alerts.append({
            "station": r[0],
            "type": r[1],
            "level": r[2],
            "message": r[3],
            "temperature": r[4],
            "time": r[5],
        })
    return jsonify(alerts)

@app.route('/api/overview', methods=['GET'])
def get_overview():
    """获取总览数据 - 所有站点最新状态"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    stations_data = []
    for station in MONITOR_STATIONS:
        c.execute(
            "SELECT temperature, timestamp FROM sst_data WHERE station_id=? ORDER BY timestamp DESC LIMIT 24",
            (station["id"],)
        )
        rows = c.fetchall()
        if rows:
            temps_24h = [r[0] for r in rows]
            current = temps_24h[0]
            avg_24h = round(statistics.mean(temps_24h), 2)
            
            # 获取告警数
            c.execute(
                "SELECT COUNT(*) FROM alert_log WHERE station_id=? AND created_at > ?",
                (station["id"], (datetime.now() - timedelta(hours=24)).isoformat())
            )
            alert_count = c.fetchone()[0]
            
            stations_data.append({
                "id": station["id"],
                "name": station["name"],
                "region": station["region"],
                "lat": station["lat"],
                "lon": station["lon"],
                "current_temp": current,
                "avg_24h": avg_24h,
                "alert_count": alert_count,
                "status": "danger" if current >= ALERT_CONFIG["temp_high"] else 
                          "warning" if current <= ALERT_CONFIG["temp_low"] else "normal",
            })
        else:
            stations_data.append({
                "id": station["id"],
                "name": station["name"],
                "region": station["region"],
                "lat": station["lat"],
                "lon": station["lon"],
                "current_temp": None,
                "avg_24h": None,
                "alert_count": 0,
                "status": "nodata",
            })
    
    conn.close()
    return jsonify(stations_data)

@app.route('/api/run_analysis', methods=['POST'])
def run_analysis():
    """运行全量预警分析"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    all_alerts = []
    
    for station in MONITOR_STATIONS:
        # 获取该站点最近 72 小时数据
        cutoff = (datetime.now() - timedelta(hours=72)).isoformat()
        c.execute(
            "SELECT temperature, timestamp FROM sst_data WHERE station_id=? AND timestamp >= ? ORDER BY timestamp",
            (station["id"], cutoff)
        )
        rows = c.fetchall()
        
        if len(rows) < 6:
            continue
        
        temps = [r[0] for r in rows]
        times = [r[1] for r in rows]
        
        # 对每个小时的数据点做检测
        for i in range(6, len(temps)):
            history = temps[max(0, i-24):i]
            alerts = check_anomaly(
                station["id"], station["name"], 
                temps[i], history, times[i]
            )
            all_alerts.extend(alerts)
    
    conn.close()
    return jsonify({
        "status": "ok",
        "alerts_generated": len(all_alerts),
        "alerts": all_alerts[-20:],
        "time": datetime.now().isoformat()
    })

if __name__ == '__main__':
    print("=" * 60)
    print("  海洋温度异常预警监测平台")
    print("  Ocean Temperature Anomaly Warning System")
    print("=" * 60)
    print(f"  监测站点: {len(MONITOR_STATIONS)} 个")
    print(f"  API 数据源: Open-Meteo Marine API (免费, 无需Key)")
    port = int(os.environ.get("PORT", 5000))
    print(f"  启动地址: http://127.0.0.1:{port}")
    print("=" * 60)
    app.run(debug=False, host='0.0.0.0', port=port)
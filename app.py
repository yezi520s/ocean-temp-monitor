# -*- coding: utf-8 -*-
"""
娴锋磱娓╁害寮傚父棰勮鐩戞祴骞冲彴 - 鍚庣鏈嶅姟
浣跨敤 Open-Meteo Marine API 鑾峰彇瀹炴椂娴疯〃娓╁害鏁版嵁
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

# 绂佹缂撳瓨 - 纭繚鍓嶇濮嬬粓鍔犺浇鏈€鏂扮増
@app.after_request
def no_cache(response):
    response.cache_control.no_cache = True
    response.cache_control.no_store = True
    response.cache_control.must_revalidate = True
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# 鍒锋柊浠诲姟鐘舵€?_refresh_status = {"running": False, "results": None, "time": None, "error": None}

# ============================================================
# 鐩戞祴绔欑偣閰嶇疆 - 涓浗娌挎捣鍏抽敭娴峰煙
# ============================================================
MONITOR_STATIONS = [
    {"id": 1, "name": "澶ц繛娴峰煙", "lat": 38.90, "lon": 121.60, "region": "娓ゆ捣"},
    {"id": 2, "name": "闈掑矝娴峰煙", "lat": 36.10, "lon": 120.40, "region": "榛勬捣"},
    {"id": 3, "name": "鑸熷北娴峰煙", "lat": 30.00, "lon": 122.10, "region": "涓滄捣"},
    {"id": 4, "name": "鍘﹂棬娴峰煙", "lat": 24.50, "lon": 118.10, "region": "鍙版咕娴峰场"},
    {"id": 5, "name": "鐝犳睙鍙ｆ捣鍩?, "lat": 22.20, "lon": 113.60, "region": "鍗楁捣鍖楅儴"},
    {"id": 6, "name": "涓変簹娴峰煙", "lat": 18.20, "lon": 109.50, "region": "鍗楁捣"},
    {"id": 7, "name": "瑗挎矙娴峰煙", "lat": 16.80, "lon": 112.30, "region": "鍗楁捣"},
    {"id": 8, "name": "杩炰簯娓捣鍩?, "lat": 34.60, "lon": 119.50, "region": "榛勬捣"},
]

# ============================================================
# 棰勮闃堝€奸厤缃?# ============================================================
ALERT_CONFIG = {
    "temp_high": 30.0,         # 楂樻俯棰勮闃堝€?(鈩?
    "temp_low": 15.0,          # 浣庢俯棰勮闃堝€?(鈩? - 鍐
    "anomaly_threshold": 2.0,  # 寮傚父鍋忕闃堝€?(鈩? - 涓庡巻鍙插潎鍊煎亸宸秴杩囨鍊艰Е鍙戦璀?    "trend_window": 24,        # 瓒嬪娍鍒嗘瀽绐楀彛 (灏忔椂)
}

# ============================================================
# 鏁版嵁搴撳垵濮嬪寲 - SQLite 缂撳瓨
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
# 鏁版嵁鑾峰彇 - Open-Meteo Marine API
# ============================================================
def fetch_sst_from_api(lat, lon, forecast_days=7):
    """浠?Open-Meteo Marine API 鑾峰彇娴疯〃娓╁害鏁版嵁"""
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
        print(f"API 璇锋眰澶辫触 ({lat},{lon}): {e}")
        return None

def cache_sst_data(station_id, station_name, lat, lon):
    """鑾峰彇骞剁紦瀛樺崟涓珯鐐圭殑 SST 鏁版嵁"""
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
# 棰勮妫€娴嬪紩鎿?# ============================================================
def check_anomaly(station_id, station_name, current_temp, temp_history, target_time):
    """妫€娴嬫俯搴﹀紓甯稿苟鐢熸垚棰勮"""
    alerts = []
    
    # 1. 楂樻俯棰勮
    if current_temp >= ALERT_CONFIG["temp_high"]:
        alerts.append({
            "station_id": station_id,
            "station_name": station_name,
            "alert_type": "楂樻俯棰勮",
            "alert_level": "danger" if current_temp >= 32 else "warning",
            "message": f"娴疯〃娓╁害杈惧埌 {current_temp}鈩冿紝瓒呰繃楂樻俯闃堝€?{ALERT_CONFIG['temp_high']}鈩冿紝鍙兘瀵艰嚧鐝婄憵鐧藉寲銆佹捣娲嬬敓鎬佸彈鎹?,
            "temperature": current_temp,
            "time": target_time,
        })
    
    # 2. 浣庢俯棰勮锛堝啲瀛ｏ級
    if current_temp <= ALERT_CONFIG["temp_low"]:
        alerts.append({
            "station_id": station_id,
            "station_name": station_name,
            "alert_type": "浣庢俯棰勮",
            "alert_level": "warning",
            "message": f"娴疯〃娓╁害闄嶈嚦 {current_temp}鈩冿紝浣庝簬姝ｅ父鑼冨洿锛岄渶鍏虫敞娴锋磱鐢熺墿鍙楀喕椋庨櫓",
            "temperature": current_temp,
            "time": target_time,
        })
    
    # 3. 寮傚父鍋忕妫€娴嬶紙涓庤繎鏈熷潎鍊煎姣旓級
    if temp_history and len(temp_history) >= 12:
        avg = statistics.mean(temp_history)
        deviation = abs(current_temp - avg)
        if deviation >= ALERT_CONFIG["anomaly_threshold"]:
            direction = "鍋忛珮" if current_temp > avg else "鍋忎綆"
            alerts.append({
                "station_id": station_id,
                "station_name": station_name,
                "alert_type": f"娓╁害寮傚父{direction}",
                "alert_level": "warning",
                "message": f"褰撳墠娓╁害 {current_temp}鈩?杈冭繎鏈熷潎鍊?{avg:.1f}鈩?{direction} {deviation:.1f}鈩冿紝鍙兘瀛樺湪娴锋磱娓╁害寮傚父浜嬩欢",
                "temperature": current_temp,
                "time": target_time,
            })
    
    # 4. 蹇€熷崌娓?闄嶆俯瓒嬪娍妫€娴?    if temp_history and len(temp_history) >= 6:
        recent_avg = statistics.mean(temp_history[-3:])
        earlier_avg = statistics.mean(temp_history[-6:-3])
        trend = recent_avg - earlier_avg
        if abs(trend) >= 1.5:
            direction = "蹇€熷崌娓? if trend > 0 else "蹇€熼檷娓?
            alerts.append({
                "station_id": station_id,
                "station_name": station_name,
                "alert_type": f"{direction}瓒嬪娍",
                "alert_level": "info",
                "message": f"杩囧幓6灏忔椂鍐呮俯搴direction} {abs(trend):.1f}鈩冿紝璇锋寔缁叧娉?,
                "temperature": current_temp,
                "time": target_time,
            })
    
    # 淇濆瓨鍛婅
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
# API 璺敱
# ============================================================

@app.route('/')
def index():
    """涓婚〉闈?""
    return render_template('index.html')

@app.route('/api/refresh', methods=['POST'])
def refresh_data():
    """鍒锋柊鎵€鏈夌珯鐐规暟鎹?- 鍚庡彴寮傛鎵ц"""
    global _refresh_status
    if _refresh_status["running"]:
        return jsonify({"status": "running", "message": "鍒锋柊浠诲姟宸插湪鎵ц涓?})
    
    def _bg_refresh():
        global _refresh_status
        _refresh_status = {"running": True, "results": None, "time": None, "error": None}
        try:
            results = []
            for station in MONITOR_STATIONS:
                ok = cache_sst_data(station["id"], station["name"], station["lat"], station["lon"])
                results.append({"station": station["name"], "success": ok})
            _refresh_status = {"running": False, "results": results, "time": datetime.now().isoformat(), "error": None}
        except Exception as e:
            _refresh_status = {"running": False, "results": None, "time": datetime.now().isoformat(), "error": str(e)}
    
    threading.Thread(target=_bg_refresh, daemon=True).start()
    return jsonify({"status": "started", "message": "鍚庡彴鍒锋柊宸插惎鍔?})

@app.route('/api/refresh_status', methods=['GET'])
def refresh_status():
    """鏌ヨ鍒锋柊浠诲姟鐘舵€?""
    global _refresh_status
    return jsonify(_refresh_status)

@app.route('/api/stations', methods=['GET'])
def get_stations():
    """鑾峰彇鎵€鏈夌洃娴嬬珯鐐逛俊鎭?""
    return jsonify(MONITOR_STATIONS)

@app.route('/api/sst/<int:station_id>', methods=['GET'])
def get_sst_data(station_id):
    """鑾峰彇鎸囧畾绔欑偣鐨?SST 鏁版嵁"""
    hours = request.args.get('hours', 168, type=int)  # 榛樿7澶?    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    c.execute(
        "SELECT timestamp, temperature FROM sst_data WHERE station_id=? AND timestamp >= ? ORDER BY timestamp",
        (station_id, cutoff)
    )
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return jsonify({"error": "鏆傛棤鏁版嵁锛岃鍏堝埛鏂?, "times": [], "temps": []})
    
    times = [r[0] for r in rows]
    temps = [r[1] for r in rows]
    
    # 璁＄畻缁熻淇℃伅
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
    """鑾峰彇棰勮鍒楄〃"""
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
    """鑾峰彇鎬昏鏁版嵁 - 鎵€鏈夌珯鐐规渶鏂扮姸鎬?""
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
            
            # 鑾峰彇鍛婅鏁?            c.execute(
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
    """杩愯鍏ㄩ噺棰勮鍒嗘瀽"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    all_alerts = []
    
    for station in MONITOR_STATIONS:
        # 鑾峰彇璇ョ珯鐐规渶杩?72 灏忔椂鏁版嵁
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
        
        # 瀵规瘡涓皬鏃剁殑鏁版嵁鐐瑰仛妫€娴?        for i in range(6, len(temps)):
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
    print("  娴锋磱娓╁害寮傚父棰勮鐩戞祴骞冲彴")
    print("  Ocean Temperature Anomaly Warning System")
    print("=" * 60)
    print(f"  鐩戞祴绔欑偣: {len(MONITOR_STATIONS)} 涓?)
    print(f"  API 鏁版嵁婧? Open-Meteo Marine API (鍏嶈垂, 鏃犻渶Key)")
    port = int(os.environ.get("PORT", 5000))
    print(f"  鍚姩鍦板潃: http://127.0.0.1:{port}")
    print("=" * 60)
    app.run(debug=False, host='0.0.0.0', port=port)
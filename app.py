# -*- coding: utf-8 -*-
"""
海洋温度异常预警监测平台 —— 应用组装层。

职责：Flask 应用创建、路由注册、后台任务编排。
业务逻辑已下沉到 sync_service / alert_engine / report_service。
"""
import threading
from datetime import datetime

from flask import Flask, jsonify, request, render_template

import config
from database import init_db, query_all, get_conn, query_one
from sync_service import sync_all, has_historical_baseline, latest_data_span
from alert_engine import evaluate_all, evaluate_station
from report_service import generate_report, latest_report


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

        # 告警与评分
        alerts, score, level, label, metrics = evaluate_station(station, save=False)

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

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
from database import init_db, query_all, get_conn
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
    @app.route("/api/status", methods=["GET"])
    def status():
        return jsonify({
            "span": latest_data_span(),
            "has_historical_baseline": has_historical_baseline(),
        })

    return app


# 模块级应用实例（gunicorn 需要 app:app）
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

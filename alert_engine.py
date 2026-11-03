# -*- coding: utf-8 -*-
"""
预警引擎 —— 五维检测 + 综合评分。

五个维度：
  ① 高温异常  ② 低温异常  ③ 日升幅  ④ 周升幅  ⑤ 同比异常
每个维度独立判断是否触发，再按权重合成 0–100 的综合评分与风险等级。
"""
from datetime import datetime, timedelta

import config
from database import get_conn


def _iso(dt):
    return dt.isoformat()


def _level_for_score(score):
    """总分 → (level, 中文标签)。level 取值 normal/watch/warning/danger。"""
    for threshold, level, label in config.RISK_LEVELS:
        if score >= threshold:
            return level, label
    return "danger", "严重"


# ---------------------------------------------------------------------------
# 五维检测（纯函数，便于测试）
# ---------------------------------------------------------------------------
def _check_high_temp(station, current, target_time, score_cfg):
    """① 高温异常：超过阈值。"""
    th = config.ALERT_CONFIG["temp_high"]
    if current >= th:
        excess = round(current - th, 2)
        # 分档：越热越危险
        if current >= th + 2:
            level = "danger"
        else:
            level = "warning"
        score = min(score_cfg, round(score_cfg * min(1.0, 0.5 + excess / 2)))
        return {
            "station_id": station["id"],
            "station_name": station["name"],
            "alert_type": "高温异常",
            "dimension": "high_temp",
            "alert_level": level,
            "message": f"海表温度 {current:.1f}℃，超高温阈值 {th}℃ {excess:.1f}℃，"
                       f"需警惕珊瑚白化与海洋生态热应激",
            "temperature": current,
            "reference_value": th,
            "target_time": target_time,
            "score": score,
        }
    return None


def _check_low_temp(station, current, target_time, score_cfg):
    """② 低温异常：低于阈值。"""
    th = config.ALERT_CONFIG["temp_low"]
    if current <= th:
        deficit = round(th - current, 2)
        score = min(score_cfg, round(score_cfg * min(1.0, 0.5 + deficit / 2)))
        return {
            "station_id": station["id"],
            "station_name": station["name"],
            "alert_type": "低温异常",
            "dimension": "low_temp",
            "alert_level": "warning",
            "message": f"海表温度 {current:.1f}℃，低于阈值 {th}℃ {deficit:.1f}℃，"
                       f"需关注海洋生物受冻风险",
            "temperature": current,
            "reference_value": th,
            "target_time": target_time,
            "score": score,
        }
    return None


def _check_daily_rise(station, current, prev_24h, target_time, score_cfg):
    """③ 日升幅：较 24 小时前升温幅度。"""
    th = config.ALERT_CONFIG["daily_rise"]
    if prev_24h is None:
        return None
    rise = round(current - prev_24h, 2)
    if rise >= th:
        level = "danger" if rise >= th * 1.5 else "warning"
        score = min(score_cfg, round(score_cfg * min(1.0, 0.5 + (rise - th) / th)))
        return {
            "station_id": station["id"],
            "station_name": station["name"],
            "alert_type": "日升幅异常",
            "dimension": "daily_rise",
            "alert_level": level,
            "message": f"24 小时升温 {rise:.1f}℃（{prev_24h:.1f}℃→{current:.1f}℃），"
                       f"超过日升幅阈值 {th}℃",
            "temperature": current,
            "reference_value": prev_24h,
            "target_time": target_time,
            "score": score,
        }
    return None


def _check_weekly_rise(station, current, prev_7d, target_time, score_cfg):
    """④ 周升幅：较 7 天前升温幅度。"""
    th = config.ALERT_CONFIG["weekly_rise"]
    if prev_7d is None:
        return None
    rise = round(current - prev_7d, 2)
    if rise >= th:
        level = "danger" if rise >= th * 1.5 else "warning"
        score = min(score_cfg, round(score_cfg * min(1.0, 0.5 + (rise - th) / th)))
        return {
            "station_id": station["id"],
            "station_name": station["name"],
            "alert_type": "周升幅异常",
            "dimension": "weekly_rise",
            "alert_level": level,
            "message": f"近 7 天升温 {rise:.1f}℃（{prev_7d:.1f}℃→{current:.1f}℃），"
                       f"超过周升幅阈值 {th}℃，可能预示持续升温事件",
            "temperature": current,
            "reference_value": prev_7d,
            "target_time": target_time,
            "score": score,
        }
    return None


def _check_yoy(station, current, yoy_value, target_time, score_cfg):
    """⑤ 同比异常：与去年同期温差绝对值超过阈值。"""
    th = config.ALERT_CONFIG["yoy_anomaly"]
    if yoy_value is None:
        return None
    diff = abs(round(current - yoy_value, 2))
    if diff >= th:
        direction = "偏高" if current > yoy_value else "偏低"
        level = "danger" if diff >= th * 1.5 else "warning"
        score = min(score_cfg, round(score_cfg * min(1.0, 0.5 + (diff - th) / th)))
        return {
            "station_id": station["id"],
            "station_name": station["name"],
            "alert_type": "同比异常",
            "dimension": "yoy_anomaly",
            "alert_level": level,
            "message": f"较去年同期 {direction} {diff:.1f}℃"
                       f"（今年 {current:.1f}℃ / 去年 {yoy_value:.1f}℃），"
                       f"超过同比阈值 {th}℃",
            "temperature": current,
            "reference_value": yoy_value,
            "target_time": target_time,
            "score": score,
        }
    return None


# ---------------------------------------------------------------------------
# 数据查询辅助
# ---------------------------------------------------------------------------
def _station_series(station_id, limit=None):
    """站点全部时序数据（升序），返回 [(timestamp, temp), ...]。"""
    sql = "SELECT timestamp, temperature FROM sst_data WHERE station_id = ? ORDER BY timestamp"
    params = [station_id]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [(r["timestamp"], r["temperature"]) for r in rows]


def _value_at_or_before(series, dt_target, tolerance_hours=6):
    """在时序里找最接近且不超过 dt_target 的值；找不到返回 None。"""
    best = None
    best_ts = None
    for ts, temp in series:
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if t <= dt_target:
            if best_ts is None or t > best_ts:
                best = temp
                best_ts = t
    # 允许目标点在前 6 小时窗口内
    if best_ts is not None and dt_target - best_ts <= timedelta(hours=tolerance_hours):
        return best
    return None


def _latest_point(series):
    if not series:
        return None, None
    ts, temp = series[-1]
    return datetime.fromisoformat(ts), temp


# ---------------------------------------------------------------------------
# 引擎主入口
# ---------------------------------------------------------------------------
def evaluate_station(station, save=True):
    """对单个站点做五维检测，返回 (alerts, score, level, label, metrics)。

    以该站点最新数据点为「当前」，回溯 24h / 7d / 去年同期作为对比基准。
    """
    series = _station_series(station["id"])
    if len(series) < 2:
        return [], 0, "normal", "正常", {"current": None}

    current_dt, current = _latest_point(series)
    target_time = _iso(current_dt)

    prev_24h = _value_at_or_before(series, current_dt - timedelta(hours=24))
    prev_7d = _value_at_or_before(series, current_dt - timedelta(days=7))
    yoy_dt = current_dt - timedelta(days=config.YOY_OFFSET_DAYS)
    yoy_value = _value_at_or_before(series, yoy_dt, tolerance_hours=48)

    score_cfg = config.SCORE_WEIGHTS
    checks = [
        _check_high_temp(station, current, target_time, score_cfg["high_temp"]),
        _check_low_temp(station, current, target_time, score_cfg["low_temp"]),
        _check_daily_rise(station, current, prev_24h, target_time, score_cfg["daily_rise"]),
        _check_weekly_rise(station, current, prev_7d, target_time, score_cfg["weekly_rise"]),
        _check_yoy(station, current, yoy_value, target_time, score_cfg["yoy_anomaly"]),
    ]
    alerts = [c for c in checks if c is not None]

    # 综合评分：满分 100，未触发维度的分数保留，触发维度按严重度打折。
    total = 0
    for dim, full in score_cfg.items():
        hit = next((a for a in alerts if a["dimension"] == dim), None)
        if hit:
            total += hit["score"]
        else:
            total += full
    total = round(total, 1)

    level, label = _level_for_score(total)

    if save and alerts:
        _save_alerts(alerts)

    metrics = {
        "current": round(current, 2),
        "current_time": target_time,
        "prev_24h": round(prev_24h, 2) if prev_24h is not None else None,
        "prev_7d": round(prev_7d, 2) if prev_7d is not None else None,
        "yoy": round(yoy_value, 2) if yoy_value is not None else None,
        "yoy_time": _iso(yoy_dt),
    }
    return alerts, total, level, label, metrics


def _save_alerts(alerts):
    """告警入库（同一站+维度+时间点去重，避免重复分析刷屏）。"""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        for a in alerts:
            exists = conn.execute(
                """
                SELECT 1 FROM alert_log
                WHERE station_id = ? AND dimension = ? AND target_time = ?
                """,
                (a["station_id"], a["dimension"], a["target_time"]),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """
                INSERT INTO alert_log
                    (station_id, station_name, alert_type, dimension, alert_level,
                     message, temperature, reference_value, target_time, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    a["station_id"], a["station_name"], a["alert_type"], a["dimension"],
                    a["alert_level"], a["message"], a["temperature"],
                    a["reference_value"], a["target_time"], now,
                ),
            )


def evaluate_all(save=True):
    """全站点五维评估，返回汇总 dict（供报告与 API 使用）。"""
    results = []
    for station in config.MONITOR_STATIONS:
        alerts, score, level, label, metrics = evaluate_station(station, save=save)
        results.append({
            "station": station,
            "alerts": alerts,
            "score": score,
            "level": level,
            "label": label,
            "metrics": metrics,
        })
    return results

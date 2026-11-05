# -*- coding: utf-8 -*-
"""
预警引擎 —— 六维检测 + 综合评分。

六个维度：
  ① 高温异常  ② 低温异常  ③ 日升幅  ④ 周升幅  ⑤ CRW 气候态距平  ⑥ 同比异常（回退）
其中 ⑤ / ⑥ 互斥：CRW 可用时用 30 年气候态基线，不可用时回退去年同日。
"""
from datetime import datetime, timedelta, timezone

import config
from database import get_conn

# 惰性导入 CRW 服务（避免循环导入，仅评估时加载）
_crw = None


def _iso(dt):
    return dt.isoformat()


def _data_now():
    """数据所用时区（config.TIMEZONE）的当前时刻，naive 形式。

    库中 timestamp 由 Open-Meteo 按 Asia/Shanghai 标注（见 sync_service），
    而服务器（Render 容器）进程时区是 UTC。若直接用 naive datetime.now()
    与这些时间戳比较，会落后 8 小时，把 8 小时前的数据当成「当前温度」。
    """
    return datetime.now(
        timezone(timedelta(seconds=config.TIMEZONE_UTC_OFFSET_SECONDS))
    ).replace(tzinfo=None)


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


def _check_clim_anomaly(station, clim_anomaly, target_time, score_cfg):
    """⑥ 气候态距平异常：基于 NOAA CRW 1985–2012 30 年气候态基线。

    距平 ≥ ±2℃ 触发预警——30 年统计基线远比一年同日更可靠。
    """
    th = config.ALERT_CONFIG.get("clim_anomaly", 2.0)
    if clim_anomaly is None:
        return None
    diff = abs(clim_anomaly)
    if diff >= th:
        direction = "偏暖" if clim_anomaly > 0 else "偏冷"
        level = "danger" if diff >= th * 1.5 else "warning"
        score = min(score_cfg, round(score_cfg * min(1.0, 0.5 + (diff - th) / th)))
        return {
            "station_id": station["id"],
            "station_name": station["name"],
            "alert_type": "气候态异常",
            "dimension": "clim_anomaly",
            "alert_level": level,
            "message": f"SST 距平 {clim_anomaly:+.1f}℃ (相对 1985–2012 气候态)，"
                       f"{direction}幅度超过阈值 {th}℃，"
                       f"属 30 年统计基线下的异常信号",
            "temperature": None,
            "reference_value": clim_anomaly,
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


def _latest_point(series, as_of=None):
    """取「不超过 as_of（默认当前时刻）」的最新数据点。

    关键：Open-Meteo 返回「历史 + 未来预报」，序列最后一条是未来预报值。
    预警检测必须锚定「现在及过去」，否则会把未来预报当成当前温度。
    """
    if not series:
        return None, None
    as_of = as_of or _data_now()
    best_ts = None
    best_temp = None
    for ts, temp in series:
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if t <= as_of:
            if best_ts is None or t > best_ts:
                best_ts = t
                best_temp = temp
    return (best_ts, best_temp) if best_ts is not None else (None, None)


# ---------------------------------------------------------------------------
# 引擎主入口
# ---------------------------------------------------------------------------
def evaluate_station(station, save=True, clim_anomaly=None):
    """对单个站点做六维检测，返回 (alerts, score, level, label, metrics)。

    以该站点最新数据点为「当前」，回溯 24h / 7d / 去年同期作为对比基准。
    优先使用 CRW 30 年气候态基线（clim_anomaly），不可用时回退到去年同日。
    """
    series = _station_series(station["id"])
    if len(series) < 2:
        return [], 0, "normal", "正常", {"current": None}

    # 锚定「现在及过去」的最新点，而非未来预报
    current_dt, current = _latest_point(series)
    if current_dt is None:
        return [], 0, "normal", "正常", {"current": None}
    target_time = _iso(current_dt)

    prev_24h = _value_at_or_before(series, current_dt - timedelta(hours=24))
    prev_7d = _value_at_or_before(series, current_dt - timedelta(days=7))
    yoy_dt = current_dt - timedelta(days=config.YOY_OFFSET_DAYS)
    yoy_value = _value_at_or_before(series, yoy_dt, tolerance_hours=48)

    score_cfg = config.SCORE_WEIGHTS
    # ①–④ 传统维度  +  ⑤/⑥：气候态基线优先，回退去年同日
    checks = [
        _check_high_temp(station, current, target_time, score_cfg["high_temp"]),
        _check_low_temp(station, current, target_time, score_cfg["low_temp"]),
        _check_daily_rise(station, current, prev_24h, target_time, score_cfg["daily_rise"]),
        _check_weekly_rise(station, current, prev_7d, target_time, score_cfg["weekly_rise"]),
        (_check_clim_anomaly(station, clim_anomaly, target_time, score_cfg["clim_anomaly"])
         if clim_anomaly is not None
         else _check_yoy(station, current, yoy_value, target_time, score_cfg["yoy_anomaly"])),
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

    baseline_type = "YoY"
    if clim_anomaly is not None:
        baseline_type = "CRW 30y Climatology (1985-2012)"

    metrics = {
        "current": round(current, 2),
        "current_time": target_time,
        "prev_24h": round(prev_24h, 2) if prev_24h is not None else None,
        "prev_7d": round(prev_7d, 2) if prev_7d is not None else None,
        "yoy": round(yoy_value, 2) if yoy_value is not None else None,
        "yoy_time": _iso(yoy_dt),
        "clim_anomaly": round(clim_anomaly, 2) if clim_anomaly is not None else None,
        "baseline_type": baseline_type,
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
    """全站点六维评估，返回汇总 dict（供报告与 API 使用）。

    CRW 气候态基线优先——从 crw_service 惰性加载距平数据；
    若网络不可达则以 None 传入 evaluate_station，自动回退去年同日。
    """
    # 惰性加载 CRW 气候态距平（避免循环导入）
    clim_map = {}
    try:
        global _crw
        if _crw is None:
            import importlib
            _crw = importlib.import_module("crw_service")
        clim_map = _crw.get_all_station_anomalies()
    except Exception:
        pass  # 网络不可达时全部回退 YoY

    results = []
    for station in config.MONITOR_STATIONS:
        ca = clim_map.get(station["id"])
        alerts, score, level, label, metrics = evaluate_station(
            station, save=save, clim_anomaly=ca)
        results.append({
            "station": station,
            "alerts": alerts,
            "score": score,
            "level": level,
            "label": label,
            "metrics": metrics,
        })
    return results

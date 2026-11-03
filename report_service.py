# -*- coding: utf-8 -*-
"""
预警报告生成 —— 汇总全站点评估结果，产出综合评分与文字报告。
"""
from datetime import datetime

import config
from database import get_conn


def generate_report(evaluation_results):
    """根据 alert_engine.evaluate_all() 的结果生成结构化报告。

    返回 dict，包含总分、等级、各站点明细与整体文字结论。
    """
    now = datetime.now().isoformat()
    stations_detail = []
    danger_count = watch_count = 0

    for item in evaluation_results:
        station = item["station"]
        metrics = item["metrics"]
        triggered_dims = [a["dimension"] for a in item["alerts"]]
        stations_detail.append({
            "id": station["id"],
            "name": station["name"],
            "region": station["region"],
            "score": item["score"],
            "level": item["level"],
            "label": item["label"],
            "current": metrics.get("current"),
            "prev_24h": metrics.get("prev_24h"),
            "prev_7d": metrics.get("prev_7d"),
            "yoy": metrics.get("yoy"),
            "triggered_dimensions": triggered_dims,
            "alerts": item["alerts"],
        })
        if item["level"] == "danger":
            danger_count += 1
        elif item["level"] == "warning":
            watch_count += 1

    # 平台整体评分：各站点平均分
    scores = [s["score"] for s in stations_detail]
    overall_score = round(sum(scores) / len(scores), 1) if scores else 0.0

    overall_level, overall_label = _level_for_score(overall_score)

    summary = _compose_summary(overall_score, overall_label, danger_count, watch_count, stations_detail)

    report = {
        "generated_at": now,
        "overall_score": overall_score,
        "overall_level": overall_level,
        "overall_label": overall_label,
        "danger_count": danger_count,
        "warning_count": watch_count,
        "summary": summary,
        "stations": stations_detail,
    }
    _persist_report(report)
    return report


def _level_for_score(score):
    for threshold, level, label in config.RISK_LEVELS:
        if score >= threshold:
            return level, label
    return "danger", "严重"


def _compose_summary(score, label, danger_count, watch_count, stations_detail):
    """生成文字结论。"""
    hottest = max(stations_detail, key=lambda s: s["current"] or -999, default=None)
    coldest = min(stations_detail, key=lambda s: s["current"] if s["current"] is not None else 999, default=None)

    lines = []
    lines.append(f"平台综合评分为 {score} 分，整体风险等级为「{label}」。")
    if danger_count:
        lines.append(f"共 {danger_count} 个站点处于「严重」状态，需立即关注。")
    if watch_count:
        lines.append(f"共 {watch_count} 个站点处于「预警」状态，建议持续跟踪。")
    if not danger_count and not watch_count:
        lines.append("所有站点温度均在正常区间，暂无显著异常。")

    if hottest and hottest["current"] is not None:
        lines.append(f"当前海温最高为 {hottest['name']}（{hottest['current']}℃）。")
    if coldest and coldest["current"] is not None:
        lines.append(f"当前海温最低为 {coldest['name']}（{coldest['current']}℃）。")

    # 同比维度提示
    yoy_hits = [s for s in stations_detail if "yoy_anomaly" in s["triggered_dimensions"]]
    if yoy_hits:
        names = "、".join(s["name"] for s in yoy_hits[:3])
        lines.append(f"{names} 较去年同期温差超过阈值，可能存在气候异常信号。")

    return "".join(lines)


def _persist_report(report):
    """报告落库（保留历史）。"""
    import json
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO report_log
                (generated_at, overall_score, overall_level, overall_label, summary_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                report["generated_at"],
                report["overall_score"],
                report["overall_level"],
                report["overall_label"],
                json.dumps(report, ensure_ascii=False),
            ),
        )


def latest_report():
    """读取最近一次报告（无则返回 None）。"""
    import json
    with get_conn() as conn:
        row = conn.execute(
            "SELECT summary_json FROM report_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["summary_json"])
    except (ValueError, TypeError):
        return None

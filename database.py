# -*- coding: utf-8 -*-
"""
数据库访问层 —— SQLite 连接、建表与轻量迁移。

统一提供连接工厂与查询辅助，业务模块不直接拼 SQLite 连接。
"""
import sqlite3
from contextlib import contextmanager

from config import DB_PATH


@contextmanager
def get_conn():
    """连接上下文管理器：自动提交、异常回滚、确保关闭。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(conn, table, column, ddl):
    """若列不存在则补充（幂等轻量迁移）。"""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    """建表 + 补齐历史版本缺失的列。"""
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sst_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id INTEGER,
                station_name TEXT,
                lat REAL,
                lon REAL,
                timestamp TEXT,
                temperature REAL,
                fetched_at TEXT,
                UNIQUE(station_id, timestamp)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id INTEGER,
                station_name TEXT,
                alert_type TEXT,
                dimension TEXT,
                alert_level TEXT,
                message TEXT,
                temperature REAL,
                reference_value REAL,
                target_time TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS report_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_at TEXT,
                overall_score REAL,
                overall_level TEXT,
                overall_label TEXT,
                summary_json TEXT
            )
            """
        )
        # 旧版表结构补齐
        _ensure_column(conn, "alert_log", "dimension", "dimension TEXT")
        _ensure_column(conn, "alert_log", "reference_value", "reference_value REAL")
        _ensure_column(conn, "alert_log", "target_time", "target_time TEXT")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sst_station ON sst_data(station_id, timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alert_station_time ON alert_log(station_id, target_time)"
        )


def query_one(sql, params=()):
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()


def query_all(sql, params=()):
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()

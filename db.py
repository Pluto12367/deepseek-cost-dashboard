"""SQLite 存储层:调用日志、聚合查询与本地设置(预算、API Key)。"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_DIR, "data.db")

# 与前端图表/CSV 导出一致的列顺序
COST_COLS = ("cost_tokens", "cost_search", "cost_fetch",
             "cost_ocr", "cost_asr", "cost_tts", "cost_total")

_COLS = ("ts", "endpoint", "protocol", "model", "status_code", "latency_ms", "stream",
         "input_cache_hit", "input_cache_miss", "output_tokens",
         "search_calls", "image_search_calls", "fetch_calls",
         "ocr_pages", "asr_seconds", "tts_chars",
         "cost_tokens", "cost_search", "cost_fetch", "cost_ocr", "cost_asr", "cost_tts",
         "cost_total", "source", "error")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            endpoint TEXT NOT NULL DEFAULT '',
            protocol TEXT DEFAULT '',
            model TEXT DEFAULT '',
            status_code INTEGER DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            stream INTEGER DEFAULT 0,
            input_cache_hit REAL DEFAULT 0,
            input_cache_miss REAL DEFAULT 0,
            output_tokens REAL DEFAULT 0,
            search_calls REAL DEFAULT 0,
            image_search_calls REAL DEFAULT 0,
            fetch_calls REAL DEFAULT 0,
            ocr_pages REAL DEFAULT 0,
            asr_seconds REAL DEFAULT 0,
            tts_chars REAL DEFAULT 0,
            cost_tokens REAL DEFAULT 0,
            cost_search REAL DEFAULT 0,
            cost_fetch REAL DEFAULT 0,
            cost_ocr REAL DEFAULT 0,
            cost_asr REAL DEFAULT 0,
            cost_tts REAL DEFAULT 0,
            cost_total REAL DEFAULT 0,
            source TEXT DEFAULT 'real',
            error TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts)")
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")


def insert_call(row: dict) -> None:
    data = {k: row.get(k) for k in _COLS}
    placeholders = ",".join("?" for _ in _COLS)
    with _conn() as c:
        c.execute(f"INSERT INTO calls ({','.join(_COLS)}) VALUES ({placeholders})",
                  [data[k] for k in _COLS])


# ---------- 过滤条件 ----------

def _filter_sql(days=None, model=None, endpoint=None, source=None):
    conds, params = [], []
    if days:
        cutoff = (datetime.now() - timedelta(days=int(days) - 1)).strftime("%Y-%m-%d")
        conds.append("ts >= ?")
        params.append(cutoff)
    if model:
        conds.append("model LIKE ?")
        params.append(f"%{model}%")
    if endpoint:
        conds.append("endpoint LIKE ?")
        params.append(f"%{endpoint}%")
    if source:
        conds.append("source = ?")
        params.append(source)
    if conds:
        return " WHERE " + " AND ".join(conds), params
    return "", []


# ---------- 聚合查询 ----------

def summary() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")
    sql = "SELECT COUNT(*) n, COALESCE(SUM(cost_total),0) s FROM calls WHERE {}"
    with _conn() as c:
        t = c.execute(sql.format("ts LIKE ?"), (today + "%",)).fetchone()
        m = c.execute(sql.format("ts LIKE ?"), (month + "%",)).fetchone()
        a = c.execute(sql.format("1=1")).fetchone()
    return {"today_cost": t["s"], "today_calls": t["n"],
            "month_cost": m["s"], "month_calls": m["n"],
            "total_cost": a["s"], "total_calls": a["n"]}


def month_spend() -> float:
    month = datetime.now().strftime("%Y-%m")
    with _conn() as c:
        r = c.execute("SELECT COALESCE(SUM(cost_total),0) s FROM calls WHERE ts LIKE ?",
                      (month + "%",)).fetchone()
    return r["s"]


def daily_series(days=30, **f) -> list[dict]:
    w, p = _filter_sql(days=days, **f)
    cols = ",".join(f"COALESCE(SUM({c}),0) {c}" for c in COST_COLS)
    with _conn() as c:
        rows = c.execute(
            f"SELECT substr(ts,1,10) d, {cols}, COUNT(*) calls "
            f"FROM calls{w} GROUP BY d ORDER BY d", p).fetchall()
    return [dict(r) for r in rows]


def breakdown(days=30, **f) -> list[dict]:
    w, p = _filter_sql(days=days, **f)
    cols = ",".join(f"COALESCE(SUM({c}),0) {c}" for c in COST_COLS[:-1])
    with _conn() as c:
        row = c.execute(f"SELECT {cols} FROM calls{w}", p).fetchone()
    return [{"k": col, "v": row[col]} for col in COST_COLS[:-1]]


def by_model(days=30, **f) -> list[dict]:
    w, p = _filter_sql(days=days, **f)
    with _conn() as c:
        rows = c.execute(
            "SELECT COALESCE(NULLIF(model,''),'(未知)') model, "
            "COALESCE(SUM(cost_total),0) cost, COUNT(*) calls "
            f"FROM calls{w} GROUP BY model ORDER BY cost DESC LIMIT 10", p).fetchall()
    return [dict(r) for r in rows]


def list_calls(limit=20, offset=0, **f) -> list[dict]:
    w, p = _filter_sql(**f)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM calls{w} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*p, limit, offset]).fetchall()
    return [dict(r) for r in rows]


def count_calls(**f) -> int:
    w, p = _filter_sql(**f)
    with _conn() as c:
        r = c.execute(f"SELECT COUNT(*) n FROM calls{w}", p).fetchone()
    return r["n"]


def export_rows(**f) -> list[dict]:
    w, p = _filter_sql(**f)
    with _conn() as c:
        rows = c.execute(f"SELECT * FROM calls{w} ORDER BY id", p).fetchall()
    return [dict(r) for r in rows]


# ---------- 本地设置 ----------

def get_setting(key: str) -> str | None:
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def set_setting(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def delete_setting(key: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM settings WHERE key = ?", (key,))

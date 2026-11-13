"""DeepSeek 成本监控面板 —— 入口。

启动:python main.py
- 面板:      http://127.0.0.1:8787/ui
- 代理地址:   http://127.0.0.1:8787(把应用的 base_url 指向这里,API Key 照常透传)
"""
from __future__ import annotations

import csv
import io
import os
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import db
import estimator
import notify
import proxy

_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(_DIR, "static")


@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    yield


app = FastAPI(title="DeepSeek Cost Dashboard", lifespan=lifespan,
              docs_url=None, redoc_url=None)


# ---------- REST API(注意:必须注册在兜底代理路由之前) ----------

@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/favicon.ico")
def favicon():
    # 避免浏览器自动请求被兜底代理转发并在日志里产生噪音
    return Response(status_code=204)


@app.get("/ui")
def ui():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/summary")
def api_summary():
    s = db.summary()
    raw = db.get_setting("monthly_budget")
    budget = float(raw) if raw else 0.0
    progress, warn = None, None
    if budget > 0:
        progress = min(1.0, s["month_cost"] / budget)
        if s["month_cost"] >= budget:
            warn = f"本月已花费 ¥{s['month_cost']:.2f},已超出预算 ¥{budget:.2f}"
        elif s["month_cost"] >= budget * 0.8:
            warn = (f"本月已花费 ¥{s['month_cost']:.2f},"
                    f"已用预算的 {s['month_cost'] / budget * 100:.0f}%")
    return {**s, "budget": budget, "budget_progress": progress, "budget_warning": warn}


def _filters(days: int, model: str, endpoint: str, source: str, key_id: str = "") -> dict:
    d = max(1, min(int(days), 365)) if int(days) else None
    return {"days": d, "model": model or None,
            "endpoint": endpoint or None, "source": source or None,
            "key_id": key_id or None}


@app.get("/api/daily")
def api_daily(days: int = 30, model: str = "", endpoint: str = "", source: str = "",
              key_id: str = ""):
    return db.daily_series(**_filters(days, model, endpoint, source, key_id))


@app.get("/api/breakdown")
def api_breakdown(days: int = 30, model: str = "", endpoint: str = "", source: str = "",
                  key_id: str = ""):
    names = {"cost_tokens": "模型 Token", "cost_search": "联网搜索",
             "cost_fetch": "网页抓取", "cost_ocr": "OCR",
             "cost_asr": "ASR", "cost_tts": "TTS"}
    rows = db.breakdown(**_filters(days, model, endpoint, source, key_id))
    return [{"category": names[r["k"]], "key": r["k"], "cost": r["v"]}
            for r in rows if r["v"] > 0]


@app.get("/api/models")
def api_models(days: int = 30, model: str = "", endpoint: str = "", source: str = "",
               key_id: str = ""):
    return db.by_model(**_filters(days, model, endpoint, source, key_id))


@app.get("/api/keys")
def api_keys(days: int = 30, model: str = "", endpoint: str = "", source: str = ""):
    return db.by_key(**_filters(days, model, endpoint, source))


@app.post("/api/keys/label")
async def api_keys_label(request: Request):
    body = await request.json()
    key_id = (body.get("key_id") or "").strip()
    if not key_id:
        return JSONResponse(status_code=400, content={"error": "缺少 key_id"})
    db.set_key_label(key_id, (body.get("label") or "").strip())
    return {"ok": True}


@app.get("/api/calls")
def api_calls(days: int = 7, model: str = "", endpoint: str = "", source: str = "",
              key_id: str = "", limit: int = 20, offset: int = 0):
    f = _filters(days, model, endpoint, source, key_id)
    return {"rows": db.list_calls(limit=min(max(limit, 1), 200),
                                  offset=max(offset, 0), **f),
            "total": db.count_calls(**f)}


@app.get("/api/calls/export.csv")
def api_calls_csv(days: int = 7, model: str = "", endpoint: str = "", source: str = "",
                  key_id: str = ""):
    rows = db.export_rows(**_filters(days, model, endpoint, source, key_id))
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM:Excel 打开中文不乱码
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    fname = f"deepseek-calls-{datetime.now():%Y%m%d-%H%M%S}.csv"
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.get("/api/settings")
def api_settings():
    key = db.get_setting("api_key") or ""
    masked = f"sk-***{key[-4:]}" if len(key) >= 8 else ("已设置" if key else "")
    raw = db.get_setting("monthly_budget")
    return {"monthly_budget": float(raw) if raw else None,
            "has_key": bool(key), "api_key_masked": masked,
            "inject_usage": db.get_setting("inject_usage") != "0",  # 默认开启
            "webhook_url": db.get_setting("webhook_url") or "",
            "webhook_last_result": db.get_setting("webhook_last_result") or ""}


@app.post("/api/settings")
async def api_settings_post(request: Request):
    body = await request.json()
    if "monthly_budget" in body:
        v = body["monthly_budget"]
        if v is None or float(v) <= 0:
            db.delete_setting("monthly_budget")
        else:
            db.set_setting("monthly_budget", str(float(v)))
    if "api_key" in body:
        v = (body["api_key"] or "").strip()
        if v:
            db.set_setting("api_key", v)
        else:
            db.delete_setting("api_key")
    if "inject_usage" in body:
        db.set_setting("inject_usage", "1" if body["inject_usage"] else "0")
    if "webhook_url" in body:
        v = (body["webhook_url"] or "").strip()
        if v:
            db.set_setting("webhook_url", v)
        else:
            db.delete_setting("webhook_url")
    return {"ok": True}


@app.post("/api/webhook/test")
async def api_webhook_test():
    result = await notify.send_test()
    return result


@app.get("/api/prices")
def api_prices():
    return estimator.load_prices()


@app.post("/api/prices")
async def api_prices_post(request: Request):
    data = await request.json()
    if not isinstance(data, dict) or "models" not in data or "tools_per_1k" not in data:
        return JSONResponse(status_code=400,
                            content={"error": "价格表格式不完整(缺少 models/tools_per_1k)"})
    estimator.save_prices(data)
    return {"ok": True}


@app.get("/api/balance")
async def api_balance():
    key = db.get_setting("api_key")
    if not key:
        return {"available": False,
                "reason": "未设置 API Key(在页面底部「设置」中保存)"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://api.deepseek.com/user/balance",
                                 headers={"Authorization": f"Bearer {key}"})
    except httpx.RequestError as e:
        return {"available": False, "reason": f"网络错误:{e.__class__.__name__}"}
    if r.status_code != 200:
        return {"available": False, "reason": f"官方接口返回 HTTP {r.status_code}"}
    data = r.json()
    return {"available": bool(data.get("is_available")),
            "balance_infos": data.get("balance_infos") or []}


# ---------- 静态资源 ----------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------- 兜底:其余路径全部代理到 api.deepseek.com ----------

@app.api_route("/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def passthrough(path: str, request: Request):
    return await proxy.forward(request, path)


if __name__ == "__main__":
    import uvicorn

    print("面板:      http://127.0.0.1:8787/ui")
    print("代理地址:   http://127.0.0.1:8787  (base_url 指向这里即可记账)")
    uvicorn.run(app, host="127.0.0.1", port=8787)

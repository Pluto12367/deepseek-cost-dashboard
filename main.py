"""DeepSeek 成本监控面板 —— 入口。

启动:python main.py
- 面板:      http://127.0.0.1:8787/ui
- 代理地址:   http://127.0.0.1:8787(把应用的 base_url 指向这里,API Key 照常透传)
"""
from __future__ import annotations

import csv
import hmac
import io
import os
import calendar
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import db
import estimator
import notify
import proxy

_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(_DIR, "static")
COOKIE_NAME = "dscd_token"


@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    retention = db.get_setting("retention_days")
    if retention and int(retention) > 0:  # 数据保留策略:启动时清理过期记录
        removed = db.cleanup(int(retention))
        if removed:
            print(f"[数据保留] 已清理 {removed} 条 {retention} 天前的记录")
    yield


app = FastAPI(title="DeepSeek Cost Dashboard", lifespan=lifespan,
              docs_url=None, redoc_url=None)


# ---------- 访问令牌鉴权(可选;公网部署必须开启) ----------

def _configured_token() -> str:
    """优先环境变量 DSCD_TOKEN,其次面板里保存的令牌。未配置 = 鉴权关闭。"""
    return os.environ.get("DSCD_TOKEN") or (db.get_setting("access_token") or "")


def _provided_token(request) -> str:
    return (request.headers.get("x-dscd-token")
            or request.query_params.get("token")
            or request.cookies.get(COOKIE_NAME)
            or "").strip()


def _authed(request) -> bool:
    cfg = _configured_token()
    if not cfg:
        return True
    return hmac.compare_digest(_provided_token(request), cfg)


class AuthMiddleware:
    """纯 ASGI 中间件(不用 BaseHTTPMiddleware,避免缓冲破坏 SSE 流式)。

    未配置令牌时完全放行(本地默认形态);配置后,除登录页外全部校验。
    """

    EXEMPT = {"/login", "/api/login", "/favicon.ico"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _configured_token():
            path = scope.get("path", "")
            if path not in self.EXEMPT:
                request = Request(scope)
                if not _authed(request):
                    if path.startswith("/ui") or path == "/" or path.startswith("/static"):
                        await RedirectResponse("/login", 302)(scope, receive, send)
                        return
                    resp = JSONResponse(status_code=401, content={
                        "error": {"message": "未授权:请携带访问令牌"
                                  "(X-DSCD-Token 头 / ?token= / 登录 Cookie)",
                                  "type": "auth_error"}})
                    await resp(scope, receive, send)
                    return
        await self.app(scope, receive, send)


app.add_middleware(AuthMiddleware)


@app.get("/login")
def login_page():
    return HTMLResponse(LOGIN_HTML)


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    tok = (body.get("token") or "").strip()
    cfg = _configured_token()
    if not cfg:
        return JSONResponse(status_code=400,
                            content={"error": "未启用访问令牌,无需登录"})
    if not tok or not hmac.compare_digest(tok, cfg):
        return JSONResponse(status_code=401, content={"error": "令牌错误"})
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE_NAME, tok, httponly=True, samesite="lax",
                    max_age=30 * 24 * 3600, path="/")
    return resp


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · DeepSeek 成本监控面板</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%23637bff'/%3E%3Cstop offset='1' stop-color='%2322d3ee'/%3E%3C/linearGradient%3E%3C/defs%3E%3Ccircle cx='16' cy='16' r='13' fill='url(%23g)'/%3E%3C/svg%3E">
<style>
  :root { --bg:#0f1115; --panel:#161a22; --border:#262c38; --text:#e8eaf0;
          --muted:#8b93a7; --grad:linear-gradient(135deg,#637bff,#22d3ee); }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:var(--bg); color:var(--text);
         font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  .box { width:340px; background:var(--panel); border:1px solid var(--border);
         border-radius:14px; padding:28px; }
  .brand { display:flex; align-items:center; gap:10px; margin-bottom:6px; }
  .dot { width:12px; height:12px; border-radius:50%; background:var(--grad);
         box-shadow:0 0 10px rgba(99,123,255,.8); }
  h1 { font-size:16px; margin:0; }
  p { color:var(--muted); font-size:12px; margin:4px 0 18px; }
  input { width:100%; padding:9px 11px; border:1px solid var(--border); border-radius:8px;
          background:#1c212b; color:var(--text); font-size:13px; margin-bottom:12px; }
  button { width:100%; padding:9px; border:none; border-radius:8px; background:var(--grad);
           color:#fff; font-size:13px; cursor:pointer; }
  .err { color:#f87171; font-size:12px; min-height:16px; margin-top:10px; }
</style>
</head>
<body>
  <div class="box">
    <div class="brand"><span class="dot"></span><h1>DeepSeek 成本监控面板</h1></div>
    <p>本站已启用访问令牌保护,请输入令牌登录</p>
    <input id="token" type="password" placeholder="访问令牌" autofocus>
    <button onclick="go()">登 录</button>
    <div class="err" id="err"></div>
  </div>
  <script>
    async function go() {
      const t = document.getElementById("token").value.trim();
      if (!t) return;
      const r = await fetch("/api/login", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: t }) });
      if (r.ok) { location.href = "/ui"; return; }
      const d = await r.json().catch(() => ({}));
      document.getElementById("err").textContent = d.error || "登录失败";
    }
    document.getElementById("token").addEventListener("keydown",
      e => { if (e.key === "Enter") go(); });
  </script>
</body>
</html>"""


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

    # 月底费用预测:近 7 天日均 × 剩余天数
    now = datetime.now()
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_remaining = days_in_month - now.day
    avg7 = db.range_spend((now - timedelta(days=6)).strftime("%Y-%m-%d")) / 7
    forecast = s["month_cost"] + avg7 * days_remaining

    return {**s, "budget": budget, "budget_progress": progress, "budget_warning": warn,
            "days_remaining": days_remaining, "forecast_daily_avg": round(avg7, 4),
            "forecast_month": round(forecast, 2)}


def _filters(days: int, model: str, endpoint: str, source: str, key_id: str = "",
             date: str = "") -> dict:
    d = max(1, min(int(days), 365)) if int(days) else None
    return {"days": d, "model": model or None,
            "endpoint": endpoint or None, "source": source or None,
            "key_id": key_id or None, "date": date or None}


@app.get("/api/daily")
def api_daily(days: int = 30, model: str = "", endpoint: str = "", source: str = "",
              key_id: str = "", date: str = ""):
    return db.daily_series(**_filters(days, model, endpoint, source, key_id, date))


@app.get("/api/breakdown")
def api_breakdown(days: int = 30, model: str = "", endpoint: str = "", source: str = "",
                  key_id: str = "", date: str = ""):
    names = {"cost_tokens": "模型 Token", "cost_search": "联网搜索",
             "cost_fetch": "网页抓取", "cost_ocr": "OCR",
             "cost_asr": "ASR", "cost_tts": "TTS"}
    rows = db.breakdown(**_filters(days, model, endpoint, source, key_id, date))
    return [{"category": names[r["k"]], "key": r["k"], "cost": r["v"]}
            for r in rows if r["v"] > 0]


@app.get("/api/models")
def api_models(days: int = 30, model: str = "", endpoint: str = "", source: str = "",
               key_id: str = "", date: str = ""):
    return db.by_model(**_filters(days, model, endpoint, source, key_id, date))


@app.get("/api/keys")
def api_keys(days: int = 30, model: str = "", endpoint: str = "", source: str = "",
             date: str = ""):
    return db.by_key(**_filters(days, model, endpoint, source, "", date))


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
              key_id: str = "", date: str = "", limit: int = 20, offset: int = 0):
    f = _filters(days, model, endpoint, source, key_id, date)
    return {"rows": db.list_calls(limit=min(max(limit, 1), 200),
                                  offset=max(offset, 0), **f),
            "total": db.count_calls(**f)}


@app.get("/api/calls/export.csv")
def api_calls_csv(days: int = 7, model: str = "", endpoint: str = "", source: str = "",
                  key_id: str = "", date: str = ""):
    rows = db.export_rows(**_filters(days, model, endpoint, source, key_id, date))
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
    thr = db.get_setting("cost_alert_threshold")
    ret = db.get_setting("retention_days")
    return {"monthly_budget": float(raw) if raw else None,
            "has_key": bool(key), "api_key_masked": masked,
            "inject_usage": db.get_setting("inject_usage") != "0",  # 默认开启
            "webhook_url": db.get_setting("webhook_url") or "",
            "webhook_type": db.get_setting("webhook_type") or "generic",
            "webhook_last_result": db.get_setting("webhook_last_result") or "",
            "cost_alert_threshold": float(thr) if thr else 0.5,
            "retention_days": int(ret) if ret else 90,
            "has_access_token": bool(_configured_token()),
            "token_from_env": bool(os.environ.get("DSCD_TOKEN"))}


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
    if "webhook_type" in body:
        db.set_setting("webhook_type", body["webhook_type"] or "generic")
    if "cost_alert_threshold" in body:
        v = body["cost_alert_threshold"]
        db.set_setting("cost_alert_threshold", str(float(v)) if v and float(v) > 0 else "0.5")
    if "retention_days" in body:
        v = body["retention_days"]
        db.set_setting("retention_days", str(int(v)) if v and int(v) > 0 else "90")
    if "access_token" in body:  # 设置/清除访问令牌;保存者自动登录(证明已知晓令牌)
        v = (body["access_token"] or "").strip()
        resp = JSONResponse({"ok": True})
        if v:
            db.set_setting("access_token", v)
            resp.set_cookie(COOKIE_NAME, v, httponly=True, samesite="lax",
                            max_age=30 * 24 * 3600, path="/")
        else:
            db.delete_setting("access_token")
            resp.delete_cookie(COOKIE_NAME, path="/")
        return resp
    return {"ok": True}


@app.post("/api/webhook/test")
async def api_webhook_test():
    result = await notify.send_test()
    return result


@app.post("/api/demo/seed")
def api_demo_seed():
    """无 Key 时的演示模式:一键生成 30 天模拟数据(def 端点自动在线程池执行)。"""
    import demo_seed
    demo_seed.main()
    return {"ok": True}


@app.post("/api/demo/wipe")
def api_demo_wipe():
    import demo_seed
    demo_seed.main(wipe=True)
    return {"ok": True}


@app.post("/api/cleanup")
def api_cleanup():
    ret = db.get_setting("retention_days")
    days = int(ret) if ret and int(ret) > 0 else 90
    removed = db.cleanup(days)
    return {"ok": True, "removed": removed, "days": days}


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
    import threading
    import webbrowser

    import uvicorn

    port = int(os.environ.get("PORT", "8787"))
    print(f"面板:      http://127.0.0.1:{port}/ui")
    print(f"代理地址:   http://127.0.0.1:{port}  (base_url 指向这里即可记账)")
    if _configured_token():
        print("鉴权:      已启用访问令牌(面板登录 / 代理需 X-DSCD-Token 头)")
    if os.environ.get("DSCD_NO_BROWSER") != "1":  # 启动后自动打开面板
        threading.Timer(1.5, webbrowser.open, [f"http://127.0.0.1:{port}/ui"]).start()
    uvicorn.run(app, host="0.0.0.0", port=port)

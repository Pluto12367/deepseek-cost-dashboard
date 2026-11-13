"""预算 Webhook 告警:月花费达到阈值时向配置的 URL 推送 JSON 通知。

- 阈值:80% 预警(event=budget_warning)、100% 告警(event=budget_exceeded)
- 去重:同一级别每个月只推送一次(状态存 settings.webhook_state),花费回落到 80% 以下自动复位
- 推送结果记录在 settings.webhook_last_result,面板可见
"""
from __future__ import annotations

import json
from datetime import datetime

import httpx

import db
from estimator import BJT


def _now() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")


def _level(spend: float, budget: float) -> int:
    if budget <= 0:
        return 0
    if spend >= budget:
        return 2
    if spend >= budget * 0.8:
        return 1
    return 0


async def _post(url: str, payload: dict) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload)
        return f"{_now()} HTTP {r.status_code}"
    except httpx.RequestError as e:
        return f"{_now()} 发送失败 {e.__class__.__name__}"


async def check_and_notify() -> None:
    """每次真实调用入账后调用(异步,不阻塞代理转发)。"""
    url = db.get_setting("webhook_url")
    budget_raw = db.get_setting("monthly_budget")
    budget = float(budget_raw) if budget_raw else 0.0
    if not url or budget <= 0:
        return
    spend = db.month_spend()
    level = _level(spend, budget)
    month = datetime.now().strftime("%Y-%m")

    state_raw = db.get_setting("webhook_state")
    try:
        state = json.loads(state_raw) if state_raw else {}
    except ValueError:
        state = {}

    if level == 0:
        if state.get("month") == month and state.get("level"):
            db.set_setting("webhook_state", json.dumps({"month": month, "level": 0}))
        return
    if state.get("month") == month and (state.get("level") or 0) >= level:
        return  # 本月已推送过同级或更高级别,防骚扰

    payload = {
        "event": "budget_exceeded" if level == 2 else "budget_warning",
        "month": month,
        "month_cost": round(spend, 4),
        "budget": budget,
        "percent": round(spend / budget * 100, 1),
        "source": "deepseek-cost-dashboard",
        "ts": _now(),
    }
    result = await _post(url, payload)
    db.set_setting("webhook_state", json.dumps({"month": month, "level": level}))
    db.set_setting("webhook_last_result", result)


async def send_test() -> dict:
    """面板「发送测试」按钮:立即推送一条 test 事件。"""
    url = db.get_setting("webhook_url")
    if not url:
        return {"ok": False, "reason": "未配置 Webhook URL"}
    payload = {"event": "test", "message": "DeepSeek 成本监控面板:这是一条测试通知",
               "ts": _now()}
    result = await _post(url, payload)
    db.set_setting("webhook_last_result", result)
    return {"ok": " HTTP 2" in result, "result": result}

"""反向代理:把请求透传到 api.deepseek.com,同时记账。

- 非流式:等完整响应,解析 usage 精确计费,响应头附带本次估算费用(X-DSCD-*)
- 流式(SSE):边转发边缓冲原始字节,结束后解析事件里的 usage 再入账
- 上游不可达/报错同样入账(便于排查),状态码原样透传
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse

import db
from estimator import BJT, estimate_call, find_usage, merge_usage

UPSTREAM = "https://api.deepseek.com"

HOP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-connection", "expect",
}

_client = httpx.AsyncClient(
    base_url=UPSTREAM,
    timeout=httpx.Timeout(connect=10.0, read=600.0, write=120.0, pool=10.0),
)


def _parse_sse_usage(text: str):
    """从 SSE 流缓冲里合并出 usage(各协议事件中 usage 可能分片出现)。"""
    acc = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        u = find_usage(obj)
        if u:
            acc = merge_usage(acc, u)
    return acc


def _passthrough_headers(src) -> dict:
    # content-encoding 必须剔除:httpx 已把正文解压,透传该头会让客户端二次解压失败
    skip = HOP_HEADERS | {"content-type", "content-encoding"}
    out = {}
    for k, v in src.items():
        if k.lower() in skip:
            continue
        try:  # HTTP 头必须是 latin-1 可编码,跳过上游返回的不合规头
            k.encode("latin-1")
            v.encode("latin-1")
        except UnicodeEncodeError:
            continue
        out[k] = v
    return out


def _budget_warning() -> str | None:
    raw = db.get_setting("monthly_budget")
    budget = float(raw) if raw else 0.0
    if budget <= 0:
        return None
    spend = db.month_spend()
    if spend >= budget:
        return f"budget_exceeded CNY {spend:.2f}/{budget:.2f}"
    if spend >= budget * 0.8:
        return f"budget_near CNY {spend:.2f}/{budget:.2f}"
    return None


def _record(ts: datetime, endpoint, protocol, model, status_code, latency_ms,
            stream, breakdown, usage, error=None) -> None:
    u = usage or {}
    db.insert_call({
        "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "endpoint": endpoint,
        "protocol": protocol,
        "model": model or "",
        "status_code": status_code,
        "latency_ms": latency_ms,
        "stream": 1 if stream else 0,
        "input_cache_hit": u.get("cache_hit", 0),
        "input_cache_miss": u.get("cache_miss", 0),
        "output_tokens": u.get("output", 0),
        "search_calls": u.get("search", 0),
        "image_search_calls": u.get("image_search", 0),
        "fetch_calls": u.get("fetch", 0),
        "ocr_pages": breakdown.get("pages", 0),
        "asr_seconds": breakdown.get("asr_seconds", 0),
        "tts_chars": breakdown.get("tts_chars", 0),
        "cost_tokens": breakdown["cost_tokens"],
        "cost_search": breakdown["cost_search"],
        "cost_fetch": breakdown["cost_fetch"],
        "cost_ocr": breakdown["cost_ocr"],
        "cost_asr": breakdown["cost_asr"],
        "cost_tts": breakdown["cost_tts"],
        "cost_total": breakdown["cost_total"],
        "source": "real",
        "error": error,
    })


async def forward(request, path: str):
    ts = datetime.now(BJT)
    body = await request.body()
    req_json = None
    if "json" in (request.headers.get("content-type") or "").lower() and body:
        try:
            req_json = json.loads(body)
        except ValueError:
            req_json = None
    is_stream = bool(isinstance(req_json, dict) and req_json.get("stream"))
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    url = "/" + path + (f"?{request.url.query}" if request.url.query else "")
    start = time.perf_counter()
    model = req_json.get("model") if isinstance(req_json, dict) else None

    try:
        if is_stream:
            return await _forward_stream(request.method, url, headers, body,
                                         req_json, model, ts, start, path)
        resp = await _client.request(request.method, url, headers=headers, content=body)
        latency = int((time.perf_counter() - start) * 1000)
        resp_json = None
        if "json" in (resp.headers.get("content-type") or "").lower():
            try:
                resp_json = resp.json()
            except ValueError:
                resp_json = None
        protocol, b = estimate_call(path, request.method, req_json, resp_json, ts)
        ok = resp.status_code < 400
        _record(ts, "/" + path, protocol, model, resp.status_code, latency, False, b,
                b.get("usage") if ok else None,
                None if ok else (resp.text or "")[:500])
        out_headers = _passthrough_headers(resp.headers)
        out_headers["X-DSCD-Cost-Total"] = f"{b['cost_total']:.6f}"
        warn = _budget_warning()
        if warn:
            out_headers["X-DSCD-Budget-Warning"] = warn
        return Response(content=resp.content, status_code=resp.status_code,
                        headers=out_headers,
                        media_type=resp.headers.get("content-type", "application/json"))
    except httpx.RequestError as e:
        latency = int((time.perf_counter() - start) * 1000)
        b = estimate_call(path, request.method, req_json, None, ts)[1]
        _record(ts, "/" + path, "error", model, 502, latency, False, b, None,
                f"upstream unreachable: {e.__class__.__name__}")
        return JSONResponse(status_code=502, content={
            "error": {"message": f"无法连接上游 {UPSTREAM}: {e}", "type": "proxy_error"}})


async def _forward_stream(method, url, headers, body, req_json, model, ts, start, path):
    req = _client.build_request(method, url, headers=headers, content=body)
    try:
        resp = await _client.send(req, stream=True)
    except httpx.RequestError as e:
        latency = int((time.perf_counter() - start) * 1000)
        b = estimate_call(path, method, req_json, None, ts)[1]
        _record(ts, "/" + path, "error", model, 502, latency, True, b, None,
                f"upstream unreachable: {e.__class__.__name__}")
        return JSONResponse(status_code=502, content={
            "error": {"message": f"无法连接上游 {UPSTREAM}: {e}", "type": "proxy_error"}})

    status = resp.status_code
    ctype = resp.headers.get("content-type", "text/event-stream")

    async def gen():
        buf = bytearray()
        err_text = ""
        try:
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                yield chunk
        except httpx.RequestError as e:
            err_text = str(e)
        finally:
            await resp.aclose()
            latency = int((time.perf_counter() - start) * 1000)
            text = bytes(buf).decode("utf-8", "ignore")
            usage = _parse_sse_usage(text) if status < 400 else None
            protocol, b = estimate_call(path, method, req_json,
                                        {"usage": usage} if usage else None, ts)
            _record(ts, "/" + path, protocol, model, status, latency, True, b, usage,
                    None if status < 400 else (text[-500:] or err_text[:500]))

    return StreamingResponse(gen(), status_code=status,
                             headers=_passthrough_headers(resp.headers), media_type=ctype)

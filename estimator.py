"""计费引擎 —— 按价格表把每次 API 调用折算成费用。

计费口径(与官方定价页对齐):
- 模型 token:元/百万 tokens,分「缓存命中/未命中 × 高峰/空闲」四档
- 联网搜索/图片搜索/网页抓取:元/千次
- OCR:元/千页;ASR:元/小时;TTS:元/万字符
高峰时段:北京时间周一至周五 9:00-12:00、14:00-18:00。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
PRICES_FILE = os.path.join(_DIR, "prices.json")
BJT = timezone(timedelta(hours=8))  # 北京时间

_lock = threading.Lock()
_cache: dict = {"mtime": 0.0, "data": None}

# 费用分类键(与数据库列、前端图表一致)
COST_KEYS = ("cost_tokens", "cost_search", "cost_fetch", "cost_ocr", "cost_asr", "cost_tts")


def load_prices(force: bool = False) -> dict:
    """读取价格表(带 mtime 缓存;面板改价保存后自动生效)。"""
    with _lock:
        mtime = os.path.getmtime(PRICES_FILE)
        if force or _cache["data"] is None or mtime != _cache["mtime"]:
            with open(PRICES_FILE, "r", encoding="utf-8") as f:
                _cache["data"] = json.load(f)
            _cache["mtime"] = mtime
        return _cache["data"]


def save_prices(data: dict) -> None:
    with _lock:
        with open(PRICES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _cache["data"] = data
        _cache["mtime"] = os.path.getmtime(PRICES_FILE)


def is_peak(ts: datetime, prices: dict | None = None) -> bool:
    prices = prices or load_prices()
    bj = ts.astimezone(BJT)
    if prices.get("peak_weekdays_only", True) and bj.weekday() >= 5:
        return False
    hour = bj.hour + bj.minute / 60.0
    for lo, hi in prices.get("peak_hours", [[9, 12], [14, 18]]):
        if lo <= hour < hi:
            return True
    return False


# ---------- usage 解析(兼容 Chat Completions / Responses / Messages 三种协议) ----------

def _num(d, *keys) -> float:
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return 0.0


def find_usage(obj, depth: int = 0):
    """递归在响应对象里找 usage 字段(兼容各协议嵌套差异)。"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        u = obj.get("usage")
        if isinstance(u, dict):
            return u
        for v in obj.values():
            r = find_usage(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_usage(v, depth + 1)
            if r:
                return r
    return None


def parse_usage(usage: dict) -> dict:
    """把不同协议的 usage 归一化为统一的计数结构。"""
    u = dict(cache_hit=0.0, cache_miss=0.0, output=0.0,
             search=0.0, image_search=0.0, fetch=0.0, found=False)
    if not isinstance(usage, dict):
        return u
    hit = _num(usage, "prompt_cache_hit_tokens", "cache_read_input_tokens")
    if hit == 0:
        det = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        hit = _num(det, "cached_tokens", "cache_read_input_tokens")
    out = _num(usage, "completion_tokens", "output_tokens")
    if "prompt_tokens" in usage:
        # OpenAI/DeepSeek 风格:prompt_tokens = 命中 + 未命中
        miss = max(0.0, _num(usage, "prompt_tokens") - hit)
    elif "input_tokens" in usage:
        # Anthropic/Messages 风格:input_tokens 不含缓存命中部分
        miss = _num(usage, "input_tokens") + _num(usage, "cache_creation_input_tokens")
    else:
        miss = _num(usage, "prompt_cache_miss_tokens")

    st = usage.get("server_tool_use") or {}
    search = _num(st, "web_search_requests", "tool_search")
    image = _num(st, "tool_image_search")
    fetch = _num(st, "web_fetch_requests", "tool_fetch")
    if search == image == fetch == 0:  # 兜底:有的协议把计数平铺在 usage 顶层
        search = _num(usage, "web_search_requests", "tool_search")
        image = _num(usage, "tool_image_search")
        fetch = _num(usage, "web_fetch_requests", "tool_fetch")

    u.update(cache_hit=hit, cache_miss=miss, output=out,
             search=search, image_search=image, fetch=fetch)
    u["found"] = any(u[k] > 0 for k in ("cache_hit", "cache_miss", "output",
                                        "search", "image_search", "fetch"))
    return u


def merge_usage(a: dict | None, b: dict) -> dict:
    """合并流式响应中多个 usage 片段(计数按累计最大值取)。"""
    if not isinstance(a, dict):
        return dict(b)
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            cur = out.get(k)
            out[k] = v if not isinstance(cur, (int, float)) else max(cur, v)
        elif isinstance(v, dict):
            cur = out.get(k) if isinstance(out.get(k), dict) else {}
            out[k] = merge_usage(cur, v)
    return out


# ---------- 计费 ----------

def _find_first_number(obj, fields, depth: int = 0) -> float:
    if depth > 6 or not fields:
        return 0.0
    if isinstance(obj, dict):
        for f in fields:
            v = obj.get(f)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return float(v)
        for v in obj.values():
            r = _find_first_number(v, fields, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_first_number(v, fields, depth + 1)
            if r:
                return r
    return 0.0


def _resolve_model(model: str, prices: dict) -> str:
    for name, cfg in prices["models"].items():
        if model == name or model in cfg.get("alias", []):
            return name
    return prices.get("default_model") or next(iter(prices["models"]))


def _zero_breakdown() -> dict:
    b = {k: 0.0 for k in COST_KEYS}
    b["cost_total"] = 0.0
    return b


def estimate_chat(model: str, usage: dict | None, ts: datetime, prices: dict | None = None) -> dict:
    """按对话接口的 usage 估算费用,返回分类费用明细。"""
    prices = prices or load_prices()
    u = parse_usage(usage)
    slot = "peak" if is_peak(ts, prices) else "off_peak"
    name = _resolve_model(model or "", prices)
    p = prices["models"][name]

    cost_tokens = (
        u["cache_hit"] * p["input_cache_hit"][slot]
        + u["cache_miss"] * p["input_cache_miss"][slot]
        + u["output"] * p["output"][slot]
    ) / 1e6

    t1k = prices.get("tools_per_1k", {})
    search_price = t1k.get("web_search_requests", 0.0)
    cost_search = (u["search"] * search_price
                   + u["image_search"] * t1k.get("tool_image_search", search_price)) / 1000.0
    cost_fetch = u["fetch"] * t1k.get("web_fetch_requests", t1k.get("tool_fetch", 0.0)) / 1000.0

    b = _zero_breakdown()
    b.update(cost_tokens=cost_tokens, cost_search=cost_search, cost_fetch=cost_fetch)
    b["cost_total"] = sum(b[k] for k in COST_KEYS)
    b["usage"] = u
    b["model"] = name
    b["peak"] = slot == "peak"
    return b


def estimate_standalone(endpoint: str, req_body: dict | None, resp_body, ts: datetime,
                        prices: dict | None = None) -> dict:
    """独立接口(Search/Fetch/OCR/ASR/TTS)的费用估算(防御性解析)。"""
    prices = prices or load_prices()
    ep = endpoint.lower()
    b = _zero_breakdown()
    sa = prices.get("standalone", {})
    t1k = prices.get("tools_per_1k", {})
    req = req_body if isinstance(req_body, dict) else {}
    resp = resp_body if isinstance(resp_body, dict) else {}

    if "ocr" in ep:
        fields = sa.get("ocr", {}).get("page_fields", ["pages", "page_count", "num_pages"])
        pages = _find_first_number(resp, fields) or _find_first_number(req, fields) or 1.0
        b["cost_ocr"] = pages * sa.get("ocr", {}).get("price_per_1k_pages", 0.0) / 1000.0
        b["pages"] = pages
    elif any(k in ep for k in ("asr", "transcri", "audio-to-text")):
        fields = sa.get("asr", {}).get("duration_fields", ["duration", "duration_seconds"])
        seconds = _find_first_number(resp, fields) or _find_first_number(req, fields)
        if seconds == 0:
            ms = _find_first_number(resp, ["duration_ms"]) or _find_first_number(req, ["duration_ms"])
            seconds = ms / 1000.0
        price_key = "discount_price_per_hour" if prices.get("use_discount") else "price_per_hour"
        price = sa.get("asr", {}).get(price_key, sa.get("asr", {}).get("price_per_hour", 0.0))
        b["cost_asr"] = seconds / 3600.0 * price
        b["asr_seconds"] = seconds
    elif any(k in ep for k in ("tts", "speech", "synthes")):
        fields = sa.get("tts", {}).get("char_fields", ["chars", "characters", "char_count"])
        chars = (_find_first_number(resp, fields) or _find_first_number(req, fields)
                 or float(len(str(req.get("text") or req.get("input") or ""))))
        price_key = "discount_price_per_10k_chars" if prices.get("use_discount") else "price_per_10k_chars"
        price = sa.get("tts", {}).get(price_key, sa.get("tts", {}).get("price_per_10k_chars", 0.0))
        b["cost_tts"] = chars * price / 10000.0
        b["tts_chars"] = chars
    elif "fetch" in ep:
        b["cost_fetch"] = t1k.get("web_fetch_requests", t1k.get("tool_fetch", 0.0)) / 1000.0
    elif "search" in ep:
        b["cost_search"] = t1k.get("web_search_requests", 0.0) / 1000.0

    b["cost_total"] = sum(b[k] for k in COST_KEYS)
    return b


# 端点里出现这些片段即按对话接口计费
_CHAT_MARKERS = ("chat/completions", "completions", "/responses", "/messages")


def estimate_call(endpoint: str, method: str, req_body: dict | None, resp_body, ts: datetime):
    """入口:根据端点判断走对话计费还是独立接口计费。

    返回 (protocol, breakdown);breakdown 中对话接口附带归一化 usage。
    """
    ep = (endpoint or "").lower()
    if method.upper() == "POST" and any(m in ep for m in _CHAT_MARKERS):
        model = (req_body or {}).get("model") or "unknown"
        usage = find_usage(resp_body) if resp_body is not None else None
        b = estimate_chat(model, usage, ts)
        if "/responses" in ep:
            protocol = "responses"
        elif "/messages" in ep:
            protocol = "messages"
        else:
            protocol = "chat_completions"
        return protocol, b
    return "standalone", estimate_standalone(endpoint, req_body, resp_body, ts)

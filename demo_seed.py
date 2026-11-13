"""生成 30 天模拟调用数据(零费用、无需 API Key),便于演示面板效果。

用法:
    python demo_seed.py          # 灌入模拟数据(保留真实记录)
    python demo_seed.py --wipe   # 清除已有演示数据
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta

import db
from estimator import BJT, estimate_call, parse_usage

random.seed(42)

# 偏向业务时段的抽样,让"高峰/空闲"价差看得出来
HOURS = [8, 9, 9, 10, 10, 11, 11, 13, 14, 14, 15, 15, 16, 16, 17, 19, 20, 21, 22]


def _rand_ts(base_day: datetime) -> datetime:
    h = random.choice(HOURS)
    return base_day.replace(hour=h, minute=random.randint(0, 59),
                            second=random.randint(0, 59), microsecond=0)


def _row(ts, endpoint, protocol, model, b, usage=None, status=200, stream=0,
         latency=None, error=None) -> dict:
    u = parse_usage(usage) if usage else {}
    return {
        "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "endpoint": endpoint, "protocol": protocol, "model": model,
        "status_code": status, "latency_ms": latency or random.randint(400, 9000),
        "stream": stream,
        "input_cache_hit": u.get("cache_hit", 0),
        "input_cache_miss": u.get("cache_miss", 0),
        "output_tokens": u.get("output", 0),
        "search_calls": u.get("search", 0),
        "image_search_calls": u.get("image_search", 0),
        "fetch_calls": u.get("fetch", 0),
        "ocr_pages": b.get("pages", 0),
        "asr_seconds": b.get("asr_seconds", 0),
        "tts_chars": b.get("tts_chars", 0),
        "cost_tokens": b["cost_tokens"], "cost_search": b["cost_search"],
        "cost_fetch": b["cost_fetch"], "cost_ocr": b["cost_ocr"],
        "cost_asr": b["cost_asr"], "cost_tts": b["cost_tts"],
        "cost_total": b["cost_total"],
        "source": "demo", "error": error,
    }


def _chat_row(ts: datetime) -> dict:
    model = random.choices(["deepseek-flash", "deepseek-v4-pro"], weights=[0.88, 0.12])[0]
    prompt = random.randint(600, 15000)
    usage = {
        "prompt_tokens": prompt,
        "prompt_cache_hit_tokens": int(prompt * random.uniform(0.15, 0.75)),
        "completion_tokens": random.randint(150, 7000),
    }
    if random.random() < 0.45:  # 带联网工具的对话
        usage["server_tool_use"] = {
            "web_search_requests": random.choice([1, 1, 2, 3]),
            "tool_image_search": random.choice([0, 0, 0, 1, 2]),
            "web_fetch_requests": random.choice([0, 1, 1, 2, 4]),
        }
    protocol, b = estimate_call("/chat/completions", "POST", {"model": model},
                                {"usage": usage}, ts)
    return _row(ts, "/chat/completions", protocol, model, b, usage,
                stream=random.choice([0, 1]))


def _standalone_row(ts: datetime) -> dict:
    kind = random.choices(
        ["search", "fetch", "ocr", "asr", "tts"], weights=[0.3, 0.2, 0.2, 0.15, 0.15])[0]
    req, resp = {}, {}
    if kind == "search":
        ep, req, resp = "/v1/search", {"query": "近期行业动态"}, {"results": []}
    elif kind == "fetch":
        ep, req, resp = "/v1/fetch", {"url": "https://example.com/article"}, {"content": "..."}
    elif kind == "ocr":
        ep, req, resp = "/v1/ocr", {"file": "scan.pdf"}, {"pages": random.randint(1, 40), "content": "..."}
    elif kind == "asr":
        ep, req, resp = "/v1/asr", {}, {"duration": random.randint(120, 7200), "text": "..."}
    else:
        ep, req, resp = "/v1/tts", {"text": "内容" * random.randint(100, 3000)}, {"audio_url": "..."}
    protocol, b = estimate_call(ep, "POST", req, resp, ts)
    return _row(ts, ep, protocol, "", b)


def main(wipe: bool = False) -> None:
    db.init_db()
    if wipe:
        with db._conn() as c:
            c.execute("DELETE FROM calls WHERE source = 'demo'")
        print("已清除全部演示数据(真实调用记录保留)")
        return

    now = datetime.now(BJT)
    total, cost = 0, 0.0
    for d in range(30, -1, -1):  # 含今天,让「今日花费」卡片也有数据
        day = now - timedelta(days=d)
        n = random.randint(3, 10) if d == 0 else (random.randint(5, 15) if d == 1 else random.randint(18, 60))
        for _ in range(n):
            ts = _rand_ts(day)
            row = _chat_row(ts) if random.random() < 0.72 else _standalone_row(ts)
            db.insert_call(row)
            total += 1
            cost += row["cost_total"]
    print(f"已生成 {total} 条模拟调用记录,合计估算费用 ¥{cost:.2f}")
    print("启动 python main.py 后打开 http://127.0.0.1:8787/ui 查看")


if __name__ == "__main__":
    main(wipe="--wipe" in sys.argv)

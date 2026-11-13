"""本地 mock 服务:开发与端到端测试用,不消耗真实费用。

用法:
    python tests/mock_upstream.py upstream  # 127.0.0.1:9999,模拟 api.deepseek.com
    python tests/mock_upstream.py webhook   # 127.0.0.1:9998,接收告警 POST

配合代理测试:
    DEEPSEEK_UPSTREAM=http://127.0.0.1:9999 python main.py
之后所有经代理的请求都会打到 mock,可用 GET /last 查看代理实际转发的请求体。
"""
import json
import sys
import threading

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

store = {"last_request_body": None, "last_hook": None}
lock = threading.Lock()

upstream = FastAPI()
hooker = FastAPI()


@upstream.post("/chat/completions")
async def chat(request: Request):
    body = await request.json()
    with lock:
        store["last_request_body"] = body
    if not body.get("stream"):
        return {"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 800, "prompt_cache_hit_tokens": 300,
                          "completion_tokens": 40}}
    async def gen():
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":800,'
        yield b'"prompt_cache_hit_tokens":300,"completion_tokens":40,'
        yield b'"server_tool_use":{"web_search_requests":2,"web_fetch_requests":1}}}\n\n'
        yield b"data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@upstream.get("/last")
def last_body():
    return store["last_request_body"] or {}


@upstream.api_route("/{path:path}", methods=["GET", "POST"])
async def echo(path: str):
    return {"mock": True, "path": path}


@hooker.post("/hook")
async def hook(request: Request):
    payload = await request.json()
    with lock:
        store["last_hook"] = payload
    print("收到 Webhook:", json.dumps(payload, ensure_ascii=False))
    return {"ok": True}


@hooker.get("/last")
def last_hook():
    return store["last_hook"] or {}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "upstream"
    if mode == "upstream":
        uvicorn.run(upstream, host="127.0.0.1", port=9999, log_level="warning")
    else:
        uvicorn.run(hooker, host="127.0.0.1", port=9998, log_level="warning")

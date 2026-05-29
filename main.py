"""
Claude Code -> DeepSeek 兼容代理

背景:
    Claude Code v2.1.154+ 会在 messages 数组中间插入 role="system" 的消息,
    而 DeepSeek 的 Anthropic 兼容层只接受 role 为 user / assistant 的消息,
    因此会报错:
        messages[i].role: unknown variant `system`, expected `user` or `assistant`

本代理做的事:
    1. 监听本地端口,接收 Claude Code 发来的 Anthropic 格式请求。
    2. 把 messages 里所有 role="system" 的消息内容合并进顶层 system 字段,
       并从 messages 中移除这些消息。
    3. 把改写后的请求转发给真正的 DeepSeek Anthropic 端点。
    4. 原样回传响应,流式 (SSE) 与非流式都支持。

使用:
    1. uv run main.py
    2. 在 ~/.claude/settings.json 里把 ANTHROPIC_BASE_URL 改成本代理地址,
       例如 http://127.0.0.1:8787 ,其余配置不变。
"""

import json
import os

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# 真正的上游:DeepSeek 的 Anthropic 兼容端点。
UPSTREAM_BASE_URL = os.environ.get(
    "UPSTREAM_BASE_URL", "https://api.deepseek.com/anthropic"
).rstrip("/")

# 本代理监听地址。
PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8787"))

# 转发时不应直接透传的逐跳头 (hop-by-hop) 以及会被重新计算的头。
_SKIP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "accept-encoding",  # 让 httpx 自行协商,避免拿到压缩体却按原样转发
}
_SKIP_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
}


def _normalize_content_to_system(content):
    """把一条 system 消息的 content 规整成可追加到顶层 system 的文本块列表。

    Anthropic 的 content 可能是字符串,也可能是内容块数组。这里统一返回
    内容块 (dict) 列表,以便和顶层 system 的结构兼容。
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        blocks = []
        for block in content:
            if isinstance(block, dict):
                blocks.append(block)
            elif isinstance(block, str):
                blocks.append({"type": "text", "text": block})
        return blocks
    return []


def rewrite_body(body: dict) -> dict:
    """把 messages 中的 system 消息合并进顶层 system 字段。

    顶层 system 在 Anthropic 规范里可以是字符串或内容块数组。为了能稳妥地
    追加,这里统一成内容块数组形式。
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body

    # 找出所有 role=system 的消息,顺序保留。
    system_blocks = []
    kept_messages = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            system_blocks.extend(_normalize_content_to_system(msg.get("content")))
        else:
            kept_messages.append(msg)

    if not system_blocks:
        # 没有中间 system 消息,无需改写。
        return body

    # 把已有的顶层 system 规整成内容块数组,再把抽出来的块追加在后面。
    existing_system = body.get("system")
    merged_system = []
    if isinstance(existing_system, str):
        if existing_system:
            merged_system.append({"type": "text", "text": existing_system})
    elif isinstance(existing_system, list):
        merged_system.extend(existing_system)

    merged_system.extend(system_blocks)

    body["system"] = merged_system
    body["messages"] = kept_messages
    return body


async def proxy(request: Request) -> Response:
    raw = await request.body()

    # 仅对带 JSON body 的请求尝试改写;其余原样转发。
    rewritten = raw
    if raw:
        try:
            body = json.loads(raw)
            if isinstance(body, dict):
                body = rewrite_body(body)
                rewritten = json.dumps(body).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 不是 JSON 就别动它。
            rewritten = raw

    # 透传请求头(去掉逐跳头与会被重算的头)。
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _SKIP_REQUEST_HEADERS
    }

    url = f"{UPSTREAM_BASE_URL}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))
    upstream_req = client.build_request(
        request.method,
        url,
        headers=fwd_headers,
        content=rewritten,
    )
    upstream_resp = await client.send(upstream_req, stream=True)

    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _SKIP_RESPONSE_HEADERS
    }

    async def body_iter():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "upstream": UPSTREAM_BASE_URL})


app = Starlette(
    routes=[
        Route("/__health", health, methods=["GET"]),
        Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]),
    ]
)


if __name__ == "__main__":
    import uvicorn

    print(f"DeepSeek-CC proxy listening on http://{PROXY_HOST}:{PROXY_PORT}")
    print(f"Forwarding to {UPSTREAM_BASE_URL}")
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="info")

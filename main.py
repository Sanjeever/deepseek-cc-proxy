"""
Claude Code -> DeepSeek 兼容代理

本代理解决 Claude Code 对接 DeepSeek Anthropic API 的四个兼容性问题:

1. System 消息清洗 — 把 messages 中 role="system" 的消息合并进顶层 system
2. Thinking 块注入 — 缓存真实 thinking 块,自动注入缺失的 assistant 消息
3. Streaming 响应缓存 — 在流式响应(SSE)中实时捕获 thinking 内容
4. Adaptive Thinking 修正 — 将 thinking.type="adaptive" 转为 "enabled"

使用:
    1. uv run main.py
    2. 在 ~/.claude/settings.json 里把 ANTHROPIC_BASE_URL 改成本代理地址
"""

import json
import logging
import os
import time
import uuid

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("proxy")

UPSTREAM_BASE_URL = os.environ.get(
    "UPSTREAM_BASE_URL", "https://api.deepseek.com/anthropic"
).rstrip("/")

PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8787"))

_SKIP_REQUEST_HEADERS = {"host", "content-length", "connection", "accept-encoding"}
_SKIP_RESPONSE_HEADERS = {
    "content-length", "content-encoding", "transfer-encoding", "connection",
}

# ── Thinking 缓存 ──────────────────────────────────────────────

# 缓存最后一次由 DeepSeek 返回的真实 thinking 块
_last_thinking_block: dict | None = None

# 用于历史 assistant 消息的占位符 thinking (含固定 base64 signature)
_PLACEHOLDER_THINKING = {
    "type": "thinking",
    "signature": (
        "Y2xhdWRlLWRzLXByb3h5LXBsYWNlaG9sZGVyLXNpZ25hdHVyZS1mb3ItZGVlcHNlZWstdjQt"
        "Y29tcGF0aWJpbGl0eS1hbnRocm9waWMtcHJvdG9jb2wtZml4ZWQtNDAwLWJhZC1yZXF1ZXN0"
        "LWVycm9y"
    ),
    "thinking": "",
}


# ── 辅助函数 ────────────────────────────────────────────────────

def _normalize_content_to_system(content):
    """把 system 消息的 content 统一成内容块列表。"""
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


def _inject_thinking(msg: dict, block: dict) -> None:
    """将 thinking 块注入到 assistant 消息 content 的最前面。"""
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = [block, {"type": "text", "text": content}]
    elif isinstance(content, list):
        content.insert(0, block)


def _has_thinking(msg: dict) -> bool:
    """检查 assistant 消息是否已包含 thinking 块。"""
    content = msg.get("content")
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "thinking" for b in content
        )
    return False


def _fix_missing_thinking(messages: list) -> None:
    """为缺失 thinking 的 assistant 消息注入 thinking 块。

    从后往前遍历:
    - 最近一条缺失 thinking 的 assistant → 注入真实缓存的 thinking
    - 更早缺失 thinking 的 assistant → 注入占位符
    """
    global _last_thinking_block
    has_injected_latest = False

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue

        if _has_thinking(msg):
            if not has_injected_latest:
                content = msg["content"]
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "thinking":
                            _last_thinking_block = block
                            log.info("更新 thinking 缓存 (来自请求中的 assistant 消息)")
                            break
                has_injected_latest = True
            continue

        # 缺失 thinking
        if not has_injected_latest and _last_thinking_block:
            _inject_thinking(msg, dict(_last_thinking_block))
            log.info("注入缓存的真实 thinking 块到最新 assistant 消息")
        else:
            _inject_thinking(msg, dict(_PLACEHOLDER_THINKING))
            log.info("注入占位符 thinking 块到历史 assistant 消息")
        has_injected_latest = True


def _capture_thinking_from_response(data: dict) -> None:
    """从非流式 (JSON) 响应中捕获 thinking 块并缓存。"""
    global _last_thinking_block
    content = data.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                _last_thinking_block = block
                log.info("缓存真实 thinking 块 (非流式)")
                return


def _build_sse_parser():
    """返回 (parse, finalize) 用于流式 SSE 响应中捕获 thinking。

    parse(chunk: bytes)  — 对每个 SSE chunk 调用
    finalize()           — 响应结束后调用,缓存捕获到的 thinking
    """
    state = {"buffer": "", "signature": "", "thinking": ""}

    def parse(chunk: bytes):
        state["buffer"] += chunk.decode("utf-8", errors="ignore")
        while "\n\n" in state["buffer"]:
            event, state["buffer"] = state["buffer"].split("\n\n", 1)
            for line in event.split("\n"):
                if not line.startswith("data:"):
                    continue
                json_str = line[5:].strip()
                if not json_str or json_str == "[DONE]":
                    continue
                try:
                    data = json.loads(json_str)
                except json.JSONDecodeError:
                    continue
                typ = data.get("type")
                if typ == "content_block_start":
                    cb = data.get("content_block", {})
                    if cb.get("type") == "thinking":
                        state["signature"] = cb.get("signature", "")
                elif typ == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "thinking_delta":
                        state["thinking"] += delta.get("thinking", "")

    def finalize():
        global _last_thinking_block
        if state["signature"] or state["thinking"]:
            _last_thinking_block = {
                "type": "thinking",
                "signature": state["signature"],
                "thinking": state["thinking"],
            }
            log.info("缓存真实 thinking 块 (流式, %d 字符)", len(state["thinking"]))

    return parse, finalize


# ── 请求改写 ────────────────────────────────────────────────────

def rewrite_body(body: dict) -> dict:
    """请求体改写: system 清洗 + thinking 注入 + adaptive 修正。"""

    messages = body.get("messages")

    # 1. System 清洗 — 合并进顶层 system
    if isinstance(messages, list):
        system_blocks = []
        kept_messages = []
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                system_blocks.extend(_normalize_content_to_system(msg.get("content")))
            else:
                kept_messages.append(msg)

        if system_blocks:
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
            messages = kept_messages

    # 2. Thinking 注入
    if isinstance(messages, list):
        _fix_missing_thinking(messages)

    # 3. Adaptive Thinking → enabled
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "adaptive":
        thinking["type"] = "enabled"
        log.info("修正 thinking.type: adaptive → enabled")

    return body


# ── 代理入口 ────────────────────────────────────────────────────

async def proxy(request: Request) -> StreamingResponse:
    raw = await request.body()

    # 仅对 JSON body 改写;其余原样转发
    rewritten = raw
    if raw:
        try:
            body = json.loads(raw)
            if isinstance(body, dict):
                body = rewrite_body(body)
                rewritten = json.dumps(body).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            rewritten = raw

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
        request.method, url, headers=fwd_headers, content=rewritten,
    )

    trace_id = uuid.uuid4().hex[:8]
    path = request.url.path + ("?" + request.url.query if request.url.query else "")

    log.info(
        "%s | %s %s → ... | %sB",
        trace_id, request.method, path, f"{len(rewritten):,}",
    )

    t0 = time.monotonic()
    upstream_resp = await client.send(upstream_req, stream=True)

    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _SKIP_RESPONSE_HEADERS
    }

    content_type = upstream_resp.headers.get("content-type", "")
    is_stream = "event-stream" in content_type

    sse_parse, sse_finalize = _build_sse_parser() if is_stream else (None, None)
    raw_chunks: list[bytes] = [] if not is_stream else []  # only used for non-stream

    async def body_iter():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
                if sse_parse:
                    sse_parse(chunk)
                else:
                    raw_chunks.append(chunk)
        finally:
            await upstream_resp.aclose()
            await client.aclose()
            elapsed = time.monotonic() - t0
            log.info(
                "%s | %s %s ← %s | %.2fs",
                trace_id, request.method, path, upstream_resp.status_code, elapsed,
            )

            if sse_finalize:
                sse_finalize()
            elif raw_chunks:
                full = b"".join(raw_chunks).decode("utf-8", errors="ignore")
                try:
                    data = json.loads(full)
                    if upstream_resp.status_code == 400:
                        log.info(
                            "DeepSeek 返回 400: %s",
                            json.dumps(data, ensure_ascii=False),
                        )
                    _capture_thinking_from_response(data)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

    return StreamingResponse(
        body_iter(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=content_type,
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

    print(f"DeepSeek-CC proxy -> http://{PROXY_HOST}:{PROXY_PORT}")
    print(f"Upstream             {UPSTREAM_BASE_URL}")
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="warning")

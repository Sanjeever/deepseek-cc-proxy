# deepseek-cc-proxy（已归档 / Archived）

> **本项目已完成历史使命，不再维护。**
>
> DeepSeek API 现已原生兼容 Claude Code 的 Anthropic 协议，不再需要中间代理来修正差异。直接使用 DeepSeek 的 `/anthropic` 端点配置 Claude Code 即可，`400` 等问题已不存在。
>
> 下文为原始文档，仅供参考。

---

让 [Claude Code](https://claude.com/claude-code) 能继续对接 DeepSeek 的本地中间代理。

## 解决什么问题

DeepSeek 的 Anthropic 兼容层 (`/anthropic`) 与标准 Claude API 协议存在四个差异，直接使用会导致 `400 Bad Request` 等错误。本代理在本地逐一修正：

1. **System 消息清洗** — DeepSeek 不接受 `messages` 中的 `role: "system"`，代理自动将其合并到顶层 `system` 字段
2. **Thinking 块注入** — DeepSeek 要求每条 assistant 消息都包含 `thinking` 块（含合法 `signature`），代理缓存真实 thinking 并自动注入缺失的消息
3. **Streaming 响应缓存** — 在流式 (SSE) 响应中实时解析并缓存 thinking 内容，供下一轮请求复用
4. **Adaptive Thinking 修正** — 将 `thinking.type = "adaptive"` 转为 `"enabled"`，因为 DeepSeek 不支持 adaptive 模式

## 快速开始

### 1. 启动代理

```powershell
uv run main.py
```

启动后监听 `http://127.0.0.1:8787`。

### 2. 配置 Claude Code

编辑 `~/.claude/settings.json`，把 `ANTHROPIC_BASE_URL` 指向本代理，其余配置（`ANTHROPIC_AUTH_TOKEN`、模型映射等）保持不变：

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "sk-...",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
    "ANTHROPIC_MODEL": "deepseek-v4-pro[1m]"
  }
}
```

鉴权由代理透传，token 仍写在 Claude Code 配置里即可。

### 3. 验证

```powershell
curl http://127.0.0.1:8787/__health
# {"ok":true,"upstream":"https://api.deepseek.com/anthropic"}
```

之后正常使用 Claude Code，`400` 错误会消失。

## 工作原理

```
客户端 (Claude API 格式)
    │  POST /v1/messages → 请求体含 system / adaptive / 缺失 thinking
    ▼
┌───────────────────┐     ┌───────────────────┐
│  本代理 :8787      │ →  │  DeepSeek API      │
│  · system 合并     │ ←  │  /anthropic         │
│  · thinking 注入   │     └───────────────────┘
│  · adaptive 修正   │
│  · thinking 缓存   │
└───────────────────┘
```

## 请求改写示意

改写前 (Claude Code 发出):

```json
{
  "system": "You are Claude Code.",
  "messages": [
    { "role": "user", "content": "hi" },
    { "role": "system", "content": "mid-conversation note" },
    { "role": "assistant", "content": "hello" }
  ],
  "thinking": { "type": "adaptive" }
}
```

改写后 (转发给 DeepSeek):

```json
{
  "system": [
    { "type": "text", "text": "You are Claude Code." },
    { "type": "text", "text": "mid-conversation note" }
  ],
  "messages": [
    { "role": "user", "content": "hi" },
    {
      "role": "assistant",
      "content": [
        { "type": "thinking", "signature": "...", "thinking": "..." },
        { "type": "text", "text": "hello" }
      ]
    }
  ],
  "thinking": { "type": "enabled" }
}
```

## 配置项 (环境变量)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `UPSTREAM_BASE_URL` | `https://api.deepseek.com/anthropic` | 真正的上游 Anthropic 兼容端点 |
| `PROXY_HOST` | `127.0.0.1` | 代理监听地址 |
| `PROXY_PORT` | `8787` | 代理监听端口 |

例如换端口启动:

```powershell
$env:PROXY_PORT = "8800"; uv run main.py
```

## 设计取舍

- **中间 system 消息**采用合并进顶层 `system` 的策略：语义最贴近原意，但多个中间 system 的**位置信息**会丢失（全部拼接到 `system` 末尾）。对 Claude Code 的实际用法通常无影响。
- **Thinking 注入**对最新一轮对话使用 DeepSeek 返回的真实签名，对更早的历史消息使用固定占位符签名。这能满足 Anthropic 协议的签名校验，同时不影响回答质量。
- **Adaptive thinking** 直接转为 `enabled`，DeepSeek 会根据自身策略决定思考长度。

## 依赖

- Python ≥ 3.10
- `starlette`、`uvicorn`、`httpx`
- 使用 [uv](https://docs.astral.sh/uv/) 管理：`uv sync` 安装依赖

## 致谢

Thinking 注入、Streaming 缓存、Adaptive Thinking 修正等思路来自 [deepseek-claude-proxy](https://github.com/GuanLuoFu/deepseek-claude-proxy)，感谢作者的探索和开源。

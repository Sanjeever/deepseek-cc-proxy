# deepseek-cc-proxy

让 [Claude Code](https://claude.com/claude-code) 能继续对接 DeepSeek 的本地中间代理。

## 解决什么问题

Claude Code **v2.1.154+** 改了对话 API 的请求结构,会在 `messages` 数组中间插入 `role: "system"` 的消息。而 DeepSeek 的 Anthropic 兼容层只接受 `user` / `assistant`,于是报错:

```
API Error: 400 Failed to deserialize the JSON body into the target type:
messages[1].role: unknown variant `system`, expected `user` or `assistant`
```

本代理夹在 Claude Code 与 DeepSeek 之间,**把 `messages` 里所有 `role: "system"` 的消息剥离、合并进顶层 `system` 字段**,再转发给 DeepSeek。这样既不用降级 Claude Code,也不用等 DeepSeek 兼容新结构。

### 请求改写示意

改写前(Claude Code v2.1.154+ 发出):

```json
{
  "system": "You are Claude Code.",
  "messages": [
    { "role": "user", "content": "hi" },
    { "role": "system", "content": "mid-conversation note" },
    { "role": "assistant", "content": "hello" }
  ]
}
```

改写后(转发给 DeepSeek):

```json
{
  "system": [
    { "type": "text", "text": "You are Claude Code." },
    { "type": "text", "text": "mid-conversation note" }
  ],
  "messages": [
    { "role": "user", "content": "hi" },
    { "role": "assistant", "content": "hello" }
  ]
}
```

## 特性

- 仅改写 `messages` 中的 `system` 消息,其余请求体原样保留;没有中间 `system` 消息时零改动。
- 流式(SSE)与非流式响应都通过 `aiter_raw` 原样透传。
- 鉴权头(`x-api-key` / `authorization`)等完整透传给上游。
- 单文件实现(`main.py`),依赖仅 `starlette` + `uvicorn` + `httpx`。

## 使用

### 1. 启动代理

需要一个常驻终端运行:

```powershell
cd D:\code\python\deepseek-cc-proxy
uv run main.py
```

启动后监听 `http://127.0.0.1:8787`。

### 2. 配置 Claude Code

编辑 `~/.claude/settings.json`,把 `ANTHROPIC_BASE_URL` 指向本代理,其余配置(`ANTHROPIC_AUTH_TOKEN`、各模型映射等)保持不变:

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

鉴权由代理透传,所以 token 仍写在 Claude Code 配置里即可。

### 3. 验证

```powershell
curl http://127.0.0.1:8787/__health
# {"ok":true,"upstream":"https://api.deepseek.com/anthropic"}
```

之后正常使用 Claude Code,400 报错会消失。

## 配置项(环境变量)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `UPSTREAM_BASE_URL` | `https://api.deepseek.com/anthropic` | 真正的上游 Anthropic 兼容端点 |
| `PROXY_HOST` | `127.0.0.1` | 代理监听地址 |
| `PROXY_PORT` | `8787` | 代理监听端口(被占用时改这里,并同步改 settings 里的端口) |

例如换端口启动:

```powershell
$env:PROXY_PORT = "8800"; uv run main.py
```

## 设计取舍

中间 `system` 消息采用**合并进顶层 `system`** 的策略:语义最贴近原意,但多个中间 `system` 消息的**位置信息**会丢失(全部按序拼接到 `system` 末尾)。对 Claude Code 的实际用法通常无影响,因为它插入的多是上下文提示而非位置敏感指令。若日后发现回答异常,可改为把 `role: "system"` 转成 `role: "user"` 的策略。

## 依赖

- Python ≥ 3.10
- `starlette`、`uvicorn`、`httpx`
- 使用 [uv](https://docs.astral.sh/uv/) 管理:`uv sync` 安装依赖

# gemini-web2api

<p align="center">
  <img src="logo.png" width="200" alt="gemini-web2api logo">
</p>

[English](README.md)

将 Google Gemini 网页端转换为 OpenAI 兼容 API. 零成本, 跨平台, 单文件.

## 特性

- **可选密钥**: `api_keys` 为空时免密, 填入密钥后按 OpenAI Bearer Key 校验
- **OpenAI 兼容**: 直接替换 `/v1/chat/completions` 和 `/v1/models`
- **工具调用**: 完整的 Function Calling 支持 (OpenAI 格式)
- **多模型**: Flash (3.6), 扩展思考 (2万字+输出), Pro, Auto, Lite
- **思考深度**: 通过 `@think=N` 后缀调节 (0=最深, 4=最浅)
- **联网搜索**: 内置互联网访问 (Gemini 原生搜索能力)
- **跨平台**: 纯 Python, 仅一个可选依赖 (`httpx` 用于流式输出)
- **流式输出**: 基于 `httpx` 的 SSE Streaming 支持
- **Codex CLI**: Responses API (`/v1/responses`) 兼容 OpenAI Codex
- **Gemini CLI**: Google 原生 API (`/v1beta/models`) 兼容 Gemini CLI

## 快速开始

```bash
pip install httpx
python gemini_web2api.py
```

服务启动在 `http://localhost:8081/v1`.

## 双端口链路 (匿名 + Cookie)

可以同时跑两个实例, 各占一个端口: 一条匿名路, 一条带 cookie 路.

建两个配置文件 (参考 `.env.example`),

`.env.anon` (无鉴权):

```
PORT=8081
```

`.env.cookie` (带 cookie):

```
PORT=8082
COOKIE_FILE=cookie.txt
```

一条命令启动两个端口:

```bash
python start_all.py
```

| 端口 | 配置文件 | 路由行为 |
|------|---------|---------|
| 8081 | `.env.anon` | 匿名, 所有模型路由到 Flash-Lite |
| 8082 | `.env.cookie` | cookie 认证, 模型类别真实生效 |

客户端指向 8081 走匿名, 指向 8082 走认证, 互不干扰. 启动 banner 会显示 `Env file:` 和 `Cookie: yes/none (anonymous)`, 可确认实例身份.

也可以分别启动:

```bash
python -m gemini_web2api --env-file .env.anon
python -m gemini_web2api --env-file .env.cookie
```

> 注意: `COOKIE_FILE` 指向的文件不存在时, 实例会静默以匿名模式运行, 不会报错. Cookie 配置见下文.

## 客户端配置

### Cherry Studio / ChatBox / 任何 OpenAI 兼容客户端

| 字段 | 值 |
|------|-----|
| Base URL | `http://localhost:8081/v1` |
| API Key | `config.json` 中的任意 `api_keys`；未配置时随便填 |
| Model | `gemini-3.5-flash-thinking` |

### curl

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"你好!"}]}'
```

### OpenAI Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-your-key")
resp = client.chat.completions.create(
    model="gemini-3.5-flash-thinking",
    messages=[{"role": "user", "content": "解释量子计算"}]
)
print(resp.choices[0].message.content)
```

### Gemini CLI

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://localhost:8081
gemini
```

支持 Google 原生 API 端点:
- `GET /v1beta/models` — 模型列表
- `POST /v1beta/models/{model}:generateContent` — 非流式生成
- `POST /v1beta/models/{model}:streamGenerateContent` — 流式生成 (SSE)

## 可用模型

| 模型 | 说明 | 输出量 |
|------|------|--------|
| `gemini-3.8-flash` | 全能模型 (最新) | ~1.2万字 |
| `gemini-3.6-flash` | 全能模型 | ~1.2万字 |
| `gemini-3.5-flash` | gemini-3.6-flash 别名 | ~1.2万字 |
| `gemini-3.5-flash-thinking` | 扩展思考, 最长输出 | **~2万字** |
| `gemini-3.5-flash-thinking-lite` | 自适应思考深度 | ~1.5万字 |
| `gemini-3.1-pro` | 高级数学与代码 (需 cookie) | ~1.2万字 |
| `gemini-auto` | 自动选择模型 | 不定 |
| `gemini-flash-lite` | 最快响应, 轻量 | ~1万字 |

### 思考深度

在模型名后追加 `@think=N`:

```
gemini-3.5-flash-thinking@think=0   # 最深 (默认)
gemini-3.5-flash-thinking@think=2   # 中等
gemini-3.5-flash-thinking@think=4   # 最浅
```

## Cookie 配置 (详细)

### 为什么要配 Cookie

模型选择通过请求 payload 的 `[79]` 字段把"模型类别"发给 Gemini 服务端, 但**匿名请求下服务端会无视类别号, 所有模型统一路由到 Flash-Lite** (实测确认). 配置 cookie 后类别号才被服务端采用:

- `gemini-3.8-flash` / `gemini-3.6-flash` (FAST 类别) → 路由到账号 FAST 档位当前挂载的模型
- `gemini-3.1-pro` (PRO 类别) → 免费 Google 账号会静默回退到 Flash, 需要 **Gemini Advanced (付费订阅)** 才有真 Pro 路由
- 没有官方的无鉴权 API: Gemini API 免费层需要 Google 账号 + AI Studio 的 key, 匿名只有网页端的基础档

### 方式一: 仓库自带浏览器扩展 (推荐)

扩展一次性导出 cookie + SAPISID + XSRF token + gemini_bl, 比手动全:

1. Chrome 打开 `chrome://extensions` → 开启右上角**开发者模式** → **加载已解压的扩展程序** → 选择本仓库的 `gemini-cookie-sync-extension` 目录
2. 打开 [gemini.google.com/app](https://gemini.google.com/app), 登录 Google 账号, 刷新页面
3. 点扩展图标 → **Inspect session**, 确认显示:

   ```
   XSRF / SNlM0e: present
   gemini_bl / cfb2h: present
   ```

4. 点 **Export gemini-auth.json**, 得到包含 cookie、`sapisid`、`xsrf_token`、`gemini_bl`、`auth_user` 的完整认证文件
5. `config.json` 中设置 `"cookie_file": "gemini-auth.json"

> `gemini.google.com` 的 `bl` 版本号会随部署更新, 扩展导出的 `gemini_bl` 是当前值, 比手填更可靠.

### 方式二: 手动从 DevTools 提取

1. Chrome 访问 [gemini.google.com](https://gemini.google.com) 并登录, 按 **F12** 打开开发者工具
2. **Application (应用)** 标签 → 左侧 **Cookies** → `https://gemini.google.com`
3. 复制以下 cookie 的值:

   | Cookie | 作用 |
   |--------|------|
   | `SID` / `HSID` / `SSID` | Google 登录态 |
   | `APISID` / `SAPISID` | API 鉴权 (SAPISID 同时用于 sapisidhash) |
   | `__Secure-1PSID` | 会话凭证 |

4. 项目根目录创建 `cookie.txt`, JSON 格式:

```json
{"cookie": "SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx", "sapisid": "SAPISID的值"}
```

或纯文本单行格式:

```
SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx
```

启动方式:

```bash
python gemini_web2api.py --cookie-file cookie.txt
```

### 登录账号路径与 XSRF Token

如果已登录的 Gemini 页面 URL 带账号序号, 例如:

```
https://gemini.google.com/u/1/app/...
```

请把 `auth_user` 设置为该序号。登录态的 Gemini Web 请求还可能需要页面里的 XSRF token。该 token 在渲染后的 Gemini 页面源码中名为 `SNlM0e`; 在 `config.json` 中填入 `xsrf_token` 后, 服务会把它作为 `at` 表单字段提交。

示例:

```json
{
  "cookie_file": "/app/cookie.txt",
  "auth_user": "1",
  "xsrf_token": "AOOh0P...",
  "gemini_bl": "boq_assistant-bard-web-server_YYYYMMDD.xx_p0"
}
```

如果登录态请求返回 HTTP 400 且错误中包含 `xsrf`, 请刷新 Gemini Web 后更新 `xsrf_token`, 并确认 `auth_user` 与浏览器 URL 中的 `/u/<序号>/` 一致.

Pro 路由需要 **Gemini Advanced** (付费订阅). 免费 Google 账号的 cookie 可以登录认证, 但会静默回退到 Flash.

### 验证路由是否生效

启动带 cookie 的实例后发一条请求, 看服务日志:

```bash
curl http://localhost:8082/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hi"}]}'
```

服务日志会输出后端实际路由的模型名:

```
[xx:xx:xx] actual model from response: 3.X Flash
```

显示的就是 FAST 档位真实挂载的模型. 如果仍是 `3.5 Flash-Lite`, 检查 cookie 文件路径与登录态是否有效.

## 配置文件

在同目录创建 `config.json`:

```json
{
  "port": 8081,
  "host": "0.0.0.0",
  "retry_attempts": 3,
  "retry_delay_sec": 2,
  "request_timeout_sec": 180,
  "image_request_budget_sec": 900,
  "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
  "auth_user": null,
  "xsrf_token": null,
  "api_keys": ["sk-your-key"],
  "cookie_file": null,
  "proxy": null,
  "log_requests": true,
  "temporary_chats": false
}
```

将 `temporary_chats` 设置为 `true` 后，请求会使用 Gemini 网页版的临时聊天，
不会将对话保存在账号历史记录中。

`api_keys` 为空数组 `[]` 时不校验密钥；填入一个或多个密钥后, `/v1/*` 接口需要 `Authorization: Bearer <key>` 或 `x-api-key: <key>`.

### 模型变动检测

模块版服务（`python -m gemini_web2api`）支持定期读取登录账号的模型列表。
在带 Cookie 的实例配置中增加：

```json
{
  "model_check_interval_sec": 86400,
  "model_snapshot_file": "./gemini-models.json",
  "log_file": "logs/service.log"
}
```

配置认证导出后，默认每 86400 秒（一天）检查一次，设置为 `0` 可关闭。
首次启动且没有已保存的检查记录时，会立即读取一次模型列表，以该次检查作为每日周期起点。
后续重启继续遵守保存的检查时间，不会强制增加请求。每次到期检查仅发送一次
`otAQ7b` 模型列表请求，不发送生成请求、不跟随重定向、不在单次检查中重试。
最短检查间隔为 3600 秒。失败也遵守检查间隔，检查时间会保存到磁盘。
配置了 `proxy` 时使用该代理，否则模型检查直接连接上游。

需要包含有效会话 Cookie 和 XSRF token 的认证导出。首次成功读取建立基线，
随后按稳定模型 ID 比较，记录新增、移除、改名和元数据更新。请求失败、空列表、
不完整数据或未登录响应都保留上次有效快照。更换账号或会话会重新建立基线，
常规 TS/CC Cookie 续期不会重置基线。模块版 `/v1/models` 和 `/v1beta/models`
会公开快照中的当前模型，例如 `gemini-4-argon`；生成请求使用快照中的模型 ID、
容量参数和模型编号构造完整选择标头。读取列表和解析模型只访问本地快照，不会增加
模型列表请求；快照变化无需重启即可生效。旧模型别名映射到账号当前可用的同类别模型，
不可用的 Gemini 模型名称会明确报错，不会静默选择其他模型。
更新后在 New API 等下游服务中重新获取渠道模型列表即可。没有当前会话的有效快照时，
接口沿用旧列表，并拒绝未发现的 Gemini 模型名称。

快照中的 `snapshot.models` 保存模型信息，`snapshot.checked_at` 保存成功读取时间；
`last_attempt_at`、`last_status` 保存最近检查状态，`last_change` 保留最近一次变动。
文件只保存会话范围的哈希，不保存 Cookie、token 或原始 RPC 响应。
Unix 上的快照和锁文件权限为 `0600`，快照采用原子写入。
即使 `log_requests` 为 `false`，仍会输出 `model_check` 和 `model_change` 日志。
状态文件损坏或无法写入时停止检查，修复文件后恢复。共享快照文件的进程通过系统锁
串行检查；不同账号实例应使用不同快照路径，自定义路径也应加入 Git 忽略规则。

对应的 `.env` 配置项为 `MODEL_CHECK_INTERVAL_SEC` 和 `MODEL_SNAPSHOT_FILE`。
单文件 Python 版与 Cloudflare Worker 不启动此检测任务。

### 上下文缓存

服务提供基于 SQLite 的本地上下文缓存。通过 `POST /v1/caches` 创建缓存，
然后在 Chat Completions 或 Responses 请求中传入 `cache_id`。开启
`auto_cache` 后，稳定的系统上下文和多轮请求中的历史对话会自动缓存。
缓存 Token 通过 `usage.prompt_tokens_details.cached_tokens` 估算返回；这是代理
侧统计，不是 Gemini 上游的计费数据。

## Docker 部署

```bash
cp config.example.json config.json
docker build -t gemini-web2api .
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json gemini-web2api
```

或使用 Docker Compose:

```bash
cp config.example.json config.json
docker compose up -d
```

如需挂载 Cookie 文件:

```bash
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json -v ./cookie.txt:/app/cookie.txt gemini-web2api
```

此时 `config.json` 中设置 `"cookie_file": "/app/cookie.txt"`.

> **注意**: 如果 Docker 默认 bridge 网络下出现空回复 (`content: null`), 请切换到 host 网络: `docker run --network host ...` 或在 compose 文件中添加 `network_mode: host`. 这是 Gemini 上游拒绝来自 Docker NAT IP 段的请求导致的.

## 代理配置

如果无法直接访问 `gemini.google.com` (连接超时), 需要配置代理:

**方式 1: 命令行参数**
```bash
python gemini_web2api.py --proxy http://127.0.0.1:7890
```

**方式 2: config.json**
```json
{"proxy": "http://127.0.0.1:7890"}
```

**方式 3: 环境变量** (自动检测)
```bash
set HTTPS_PROXY=http://127.0.0.1:7890
python gemini_web2api.py
```

支持 Clash, V2Ray, Shadowsocks 等任何 HTTP 代理.

## 图片输入

模块版服务（`python -m gemini_web2api`，包括 Docker 镜像）的 Chat Completions、Responses API
和 Google `inlineData` 支持图片输入。需要有效的 Gemini 登录 Cookie 和 `curl_cffi`
（安装 `requirements.txt` 时会在受支持的平台上安装）。可使用公网 HTTPS 图片 URL
或 base64 data URL；每张图片最多 10 MiB。旧单文件入口和 Cloudflare Worker 尚未移植此修复。

图片请求采用与 [上游 PR #80](https://github.com/Sophomoresty/gemini-web2api/pull/80)
相同的附件结构、页面会话 token 和 Chrome 传输方式。`stream=true` 时返回一个完整内容块，
等待生成期间仍保留 SSE 心跳。远程图片下载为固定已校验的 DNS 地址而直连，
不会使用代理；Gemini 页面、上传和生成请求仍使用配置的代理。

隔离部署和限次验收见 [图片测试部署说明](deploy/image-test/README.md)。

```python
resp = client.chat.completions.create(
    model="gemini-3.6-flash",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
        ]
    }]
)
```

## 图片输出

模块版服务支持 `POST /v1/images/generations`，接受 `prompt`、`model`（必填）、`n`（1-10）和
`response_format`（仅 `b64_json`）。图片请求使用 Gemini Web 专用的浏览器请求格式。
`size` 会被校验并转换成明确的宽高比提示词；当主机安装了 ImageMagick 时，还会把结果
缩放/裁切到请求的尺寸。响应会返回可确定的实际 `size`（包括省略 `size` 或 `auto` 的情况）
和实际的 `output_format`。如果 ImageMagick 不可用，响应返回真实尺寸，而不是虚报请求尺寸。`n > 1` 时，只有所有输出
的实际尺寸一致才返回响应级 `size`；尺寸不一致时省略该字段。`n` 会执行 n 次独立的上游生成，
延迟和 Gemini 配额消耗近似线性增长；没有 seed 或 batch 模式，因此不保证多张图视觉一致。
`image_request_budget_sec`（默认 900，设为 `0` 关闭）限制整个 Images 请求的总时间，包括单图
重试和生成图下载；超时返回 HTTP 504，`type=server_error, code=request_timeout`。
响应还带一个代理侧估算的 `usage`：文本 token 为 `ceil(UTF-8 字节数 / 4)`，图片 token 为每个
输入附件或输出图片的 `ceil(宽/32) * ceil(高/32)`。这是确定性估算，不是 OpenAI 或 Gemini
分词器的真实计数，请勿用于计费。`model` 在 generations 和 multipart edits 中必填。OpenAI 图片模型名（`gpt-image-*`、
`chatgpt-image-latest`、旧版 `dall-e-*`）会被接受并映射到同一个 Gemini 图片管线；
Gemini 模型名和 16 位 upstream ID 也接受，但不改变图片后端；其他名称返回 `model_not_found`。
JSON edits 省略 `model` 时默认使用 `gpt-image-2.5-sunburst`。
`quality`、`style`、`moderation`、
`input_fidelity`、`safety_identifier` 等参数会被接受并校验，但 Gemini 后端不会应用它们。
`background=opaque/auto` 接受但不处理；`background=transparent` 会返回 `unsupported_value`，
因为服务无法保证输出带 alpha 通道；
`output_compression` 会在通过 ImageMagick 输出 JPEG/WebP 时生效。不支持 `stream`，会返回带 `param: "stream"` 的标准 OpenAI 错误。
`partial_images` 会接受并校验 0–3，但不生效，因为后端只返回最终图片。
所有 OpenAI 兼容的 `/v1` 接口都使用 `ErrorResponse` 结构（`type`、`message`、`param`、`code`），
所有响应都带 `x-request-id`。错误类型按 HTTP 状态映射：400/422 为
`invalid_request_error`，401 为 `authentication_error`，403 为 `permission_error`，404 为
`invalid_request_error`，429 为 `rate_limit_error`，5xx 为 `server_error`。代理特有的原因
（如 `tool_call_parse_error`、`upstream_temporary_refusal`）放在 `code`；`retryable`
保留为扩展字段。`/v1beta/*` 继续返回 Google 风格错误对象，以兼容 Gemini CLI。

模块版服务还支持 OpenAI 兼容的 `POST /v1/images/edits`，可使用 multipart 或 JSON 请求体。
multipart 模式下必须提供 `image`、`prompt` 和 `model`，`mask` 可选；只支持一张 `image`，
多图请求返回标准 `unsupported_value` 错误。JSON 模式接受单元素
`images: [{"image_url": ...}]`，支持 HTTPS 或 base64 data URL；
`file_id` 引用会拒绝，因为本服务没有实现 OpenAI Files API。提供 mask 时，mask 必须是
带 alpha 通道的 PNG，并且和源图尺寸一致。
目前支持从 PNG、JPEG 或 WebP 源图读取尺寸。不透明区域会尽量保留。Gemini Web
没有单独的 mask 参数，因此服务会把 mask 作为附件上传，并在提示词中说明 alpha 区域语义。
multipart 每张图片限制 50 MiB，mask 限制 4 MiB；JSON 的远程 HTTPS 图片仍受 10 MiB 下载上限约束。
`prompt` 限制 32000 字符。`response_format` 仅支持 `b64_json`。传 `url` 会返回标准的 `unsupported_value` 错误，
因为本服务不托管生成图片。如果确实需要公网链接，请自行解码 `b64_json` 后保存到你的存储或 CDN。
`n`（1-10）、`size`、`output_format`
会被执行（与 generations 相同，依赖 ImageMagick 可用）。`quality`、`moderation`、
`input_fidelity` 为兼容客户端而接受，但会被忽略；`background=transparent` 会返回
`unsupported_value`，`background=opaque/auto` 接受但不处理；`output_compression` 会应用到
JPEG/WebP 输出。`markup` 不支持，会返回标准的 `unsupported_value` 错误。不支持 `stream`；
`partial_images` 接受但不生效。

```bash
curl http://localhost:8081/v1/images/edits \\
  -H "Authorization: Bearer sk-your-key" \\
  -F "model=gpt-image-1" \\
  -F "image=@source.png" \\
  -F "mask=@mask.png" \\
  -F "prompt=把天空替换成日落" \\
  -F "response_format=b64_json"
```

Responses API 的 `tools` 中也支持 `{"type": "image_generation"}`，返回包含 base64 结果的
`image_generation_call`。Chat Completions 会识别类似 `generate an image of ...` 的明确请求，
并返回 Markdown 图片 URL。

生成图片的下载地址会限制在经过校验的 Google 图片域名，并限制重定向次数、文件大小和图片文件头。
服务不托管生成图片，因此不支持 `response_format=url`；请自行解码 `b64_json` 后保存或提供下载。

## 已知限制

- **图片输入需要登录和 Chrome 传输**: 请使用模块版服务、有效 Cookie 和 `curl_cffi`。Serv00/FreeBSD 可使用附带原生运行库的[图片部署包](deploy/serv00/README.md)，无需 Docker。
- **Pro/Ultra 非真实路由**: 无付费订阅 cookie 时, `gemini-3.1-pro` 实际路由到 Flash 模型. "Pro" 只是 UI 偏好标签.
- **单轮对话**: 每次请求是独立对话, 多轮上下文通过在 prompt 中包含历史消息模拟.
- **频率限制**: Google 可能限制高频请求, server 会自动重试但持续高负载可能被封.

## 系统要求

- Python 3.8+
- `httpx` (`pip install httpx`) — 用于流式请求
- 需要能访问 `gemini.google.com` (部分地区需代理)

## 工作原理

逆向 Google Gemini 网页端的 StreamGenerate 协议, 将 OpenAI API 格式与 Gemini 内部 protobuf-like 格式互转. 模型选择通过请求 payload 的 `[79]` 字段控制, 映射自 Gemini 前端 JS 源码中的 `MODE_CATEGORY` 枚举.

## 致谢

- [linux.do](https://linux.do) 社区
- 开源 API 代理生态

## License

MIT

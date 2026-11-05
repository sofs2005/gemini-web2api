# OpenAI Images 接口一致性检查报告

- **检查对象**：`POST /v1/images/generations`、`POST /v1/images/edits`
- **实现位置**：`gemini_web2api/server.py`
  - generations：`_handle_image_generation`（L1144–L1193）
  - edits：`_handle_image_edit`（L838–L1142）
- **对照基准**：OpenAI 官方 OpenAPI `openai/openai-openapi` master，`info.version: 2.3.0`
  - `CreateImageRequest` / `CreateImageEditRequest` / `EditImageBodyJsonParam`
  - `ImagesResponse` / `Image` / `ErrorResponse`
  - 拉取时间：2026-11-03
- **验证方式**：源码逐项比对 + 启动真实 HTTP handler、mock 上游做黑盒探测 + 现有单测（`tests/test_modular_sync.py`，76 项全部通过）

> 注：第一至七节保留修复前对照；最新修复状态见第九节。

---

## 一、结论

**没有做到“完全一致”。**

两个接口在**常见 b64_json 调用路径上形状兼容**，OpenAI 官方 SDK 的常规 `images.generate` / `images.edit` 调用可以通。但按 OpenAPI 严格对照，存在 **3 类硬性不一致**：

1. **所有错误响应都不符合 `ErrorResponse` schema**：缺少 `type`、`param`、`code` 三个必填字段；部分 404 还是字符串。
2. **若干 2.3.0 已定义的标准参数被直接 400 拒绝**：`safety_identifier`、`markup`、`partial_images`，以及 `stream: false`、`n: null`、`response_format: null`。
3. **标准能力缺口**：edits 不支持多图（`image` 数组，最多 16 张）、不支持 JSON 请求体、`n` 只允许 1、多个生成参数被静默忽略；其中 **`size`（宽高比）被完全忽略**，请求 16:9 也会返回 Gemini 的默认比例，且响应里不回报实际 `size`。

按模块打个分（主观，按 schema + 行为综合）：

| 维度 | 一致度 | 说明 |
|---|---:|---|
| 路由 / 方法 / 认证 | 95% | 路径、POST、Bearer 均正确；缺 `x-request-id` |
| generations 请求 schema | 65% | 参数名齐，但多个枚举未校验、若干字段被拒/忽略 |
| edits 请求 schema | 55% | 单图 multipart 可用；多图、JSON body、新参数缺失 |
| 成功响应 schema | 90% | `created` + `data[].b64_json` 合法；可选字段缺失、`url` 语义偏离 |
| 错误响应 schema | 15% | 仅 `message`，`type/param/code` 全缺，严格校验必失败 |
| 状态码语义 | 80% | 主要用 400；上游错误用 502，不在官方枚举内 |

---

## 二、两个接口的逐项对照

### 2.1 `POST /v1/images/generations`

请求体：`application/json` → `CreateImageRequest`。

| 参数 | OpenAI 2.3.0 | 当前实现 | 判定 |
|---|---|---|---|
| `prompt` | string，必填，最长 32000 | 必填、非空，但**不校验 32000** | ⚠️ 偏宽松 |
| `model` | **必填**，string/enum | 可省略，默认 `CONFIG["default_model"]` | ❌ 比标准宽松 |
| `n` | 1–10，可 null，默认 1 | 只允许 1；`null` 直接 400 | ❌ 不支持 1–10 / null |
| `quality` | 枚举 `low/medium/high/xhigh/max/auto/standard/hd` | 任意字符串都接受并忽略；`quality:"ultra"` 也返回 200 | ❌ 无校验、无语义 |
| `size` | 枚举 + `auto`（GPT-Image-2 起支持任意 `WxH`） | 接受任意值并忽略 | ❌ 无语义 |
| `response_format` | `url` / `b64_json` / `null`（已 deprecated） | 默认 `b64_json`；显式 `null` 返回 400 | ⚠️ null 不兼容 |
| `output_format` | `png`/`jpeg`/`webp`/null，默认 png | 接受并忽略，实际返回 Gemini 原始 mime | ❌ 无语义 |
| `output_compression` | 0–100 | 接受并忽略，不校验范围 | ❌ 无语义 |
| `background` | `transparent`/`opaque`/`auto`/null | 接受并忽略 | ❌ 无语义 |
| `moderation` | `low`/`auto`/null | 接受并忽略 | ❌ 无语义 |
| `style` | `vivid`/`natural`/null（已 deprecated） | 接受并忽略 | ❌ 无语义 |
| `user` | string（已 deprecated） | 接受并忽略 | ✅ schema 兼容 |
| `safety_identifier` | string/null，最长 128，当前标准字段 | 视为未知字段 → 400 `invalid image generation request` | ❌ 标准字段被拒 |
| `stream` | boolean/null，默认 false | **只要出现该 key 就 400**，`stream:false` 也拒绝 | ❌ |
| `partial_images` | 0–3/null | 未知字段 → 400 | ❌ |

成功响应实测（`/v1/images/generations`，mock 上游）：

```json
{"created": 1793671989, "data": [{"b64_json": "iVBORw0KGgo..."}]}
```

- `created` 为 Unix 秒整数 ✅
- `data` 数组、单个 `b64_json` ✅
- `ImagesResponse` 中 `background`、`output_format`、`size`、`quality`、`usage`、`safety_identifier` 均未返回。schema 里只有 `created` 必填，因此**结构合法**，但和真实 OpenAI GPT-Image 响应不一致，尤其是 `usage`（token 计量）完全缺失。⚠️
- `response_format:"url"` 时返回 `data:image/png;base64,...`。`Image.url` 的 `format: uri` 允许 data URI，**schema 合法**，但 OpenAI 返回的是 HTTPS 托管 URL；且 data URL 体积会随图片线性膨胀。⚠️

### 2.2 `POST /v1/images/edits`

请求体：标准支持 `multipart/form-data`（`CreateImageEditRequest`）**以及** `application/json`（`EditImageBodyJsonParam`，用 `images[]` + URL/file_id）。

| 参数 | OpenAI 2.3.0 | 当前实现 | 判定 |
|---|---|---|---|
| 请求体类型 | multipart **或** JSON | 只接受 multipart；JSON 直接 400 `Content-Type must be multipart/form-data` | ❌ 缺 JSON 模式 |
| `image` | 单文件或**数组，最多 16 张** | 只允许 1 个 part，重复 `image` → 400 `image must be provided at most once` | ❌ 多图不支持 |
| `prompt` | 必填，最长 32000 | 必填非空，不校验长度 | ⚠️ 偏宽松 |
| `mask` | PNG、透明区为编辑区、与图同尺寸、<4MB | PNG + alpha + 同尺寸 + 至少 1 个全透明像素；上限 10MiB | ⚠️ 更严格（透明像素）/ 更宽松（4→10MiB） |
| `model` | **必填**，枚举 | 可省略；只校验“非空”，任意字符串都接受 | ❌ 比标准宽松、无枚举校验 |
| `n` | 1–10，默认 1 | 只允许 1 | ❌ |
| `size` | 枚举/`WxH`/null | 接受并忽略 | ❌ 无语义 |
| `quality` | `low/medium/high/xhigh/max/auto` | 接受并忽略，任意值 | ❌ |
| `background` | `transparent/opaque/auto` | 接受并忽略 | ❌ |
| `moderation` | `low/auto` | 接受并忽略 | ❌ |
| `input_fidelity` | `high/low`（GPT-Image 模型） | 接受并忽略 | ❌ |
| `output_format` | `png/jpeg/webp` | 接受并忽略 | ❌ |
| `output_compression` | 0–100 | 接受并忽略 | ❌ |
| `response_format` | `url`/`b64_json`/null（deprecated） | 默认 `b64_json`，支持 `url`（返回 data URL） | ✅/⚠️ |
| `user` | string（deprecated） | 接受并忽略 | ✅ schema 兼容 |
| `safety_identifier` | string/null，当前标准字段 | 未知字段 → 400 `unexpected field: safety_identifier` | ❌ |
| `markup` | 透明 PNG 引导图（GPT-Image-2.5） | 未知字段 → 400 `unexpected field: markup` | ❌ |
| `stream` | bool/null，默认 false | 只要出现就 400；`stream=false` 也拒绝 | ❌ |
| `partial_images` | 0–3/null | 未知字段 → 400 | ❌ |
| `style` | **不在 edits schema 中** | 被允许并忽略 | ⚠️ 多余字段 |

成功响应与 generations 相同：`{"created": ..., "data": [{"b64_json"|"url": ...}]}`，结构合法，缺顶层的 `background/output_format/size/quality/usage/safety_identifier`。

---

## 三、错误响应：最严重的不一致

OpenAI `ErrorResponse` 的 `Error` **四个字段全部必填**：

```yaml
Error:
  required: [type, message, param, code]
  properties:
    type: string
    message: string
    param: string | null
    code: string | null
```

当前实现所有错误都只有 `message`，例如：

```json
{"error": {"message": "only n=1 is supported"}}
{"error": {"message": "unexpected field: markup"}}
{"error": {"message": "invalid api key"}}
```

未知路由甚至不是对象：

```json
{"error": "not found"}
```

实测（mock 上游，真实 HTTP handler）：

| 场景 | 状态码 | 响应体 |
|---|---:|---|
| 缺 prompt | 400 | `{"error":{"message":"invalid image generation request"}}` |
| 未知字段 | 400 | 同上（无 `param` 指明是哪个字段） |
| `stream:false` | 400 | 同上 |
| 非法 API key | 401 | `{"error":{"message":"invalid api key"}}` |
| 上游失败 | 502 | `{"error":{"message":"upstream error: ..."}}` |
| 未知路径 | 404 | `{"error":"not found"}` |

影响：

- 严格按 OpenAPI 校验的客户端/网关会判定响应非法。
- OpenAI SDK 通常仍能 raise 出 message，但拿不到 `type`/`code`/`param`，无法做细粒度重试或参数定位。
- 502 不在 `/images/*` 文档列出的 400/403/410/429/500/503 中。

---

## 四、已确认符合标准的部分

以下行为与 OpenAI 一致或兼容，不需要改：

1. 路由与 HTTP 方法：`POST /v1/images/generations`、`POST /v1/images/edits`。
2. 认证：支持 `Authorization: Bearer <key>`；额外支持 `x-api-key` / `x-goog-api-key` / `?key=` 属于超集。
3. generations 使用 JSON body；edits 使用 multipart，字段名 `image` / `mask` / `prompt` 正确。
4. 成功响应核心结构：`created`（Unix 秒 int）+ `data` 数组 + `data[0].b64_json` 标准 base64。
5. `Content-Type: application/json`、`Content-Length` 正确；额外 CORS 头不影响兼容。
6. 主要状态码：成功 200、参数错误 400、认证失败 401。
7. mask 契约（PNG、同尺寸、透明区语义）与标准一致。
8. `response_format` 的 `b64_json` / `url` 两种取值都被接受；默认 b64_json 符合当前 GPT-Image “总是返回 base64” 的行为（注：旧版 DALL·E spec 默认是 `url`）。
9. `n=1` 的响应确实只返回一个元素，不伪造多图。
10. 单图编辑的完整链路（上传 → 生成 → 合成 → base64）有单测覆盖，76 项全绿。

---

## 五、问题分级与修复建议

### P0：会让严格客户端判错

1. **统一错误响应 helper**

```python
def _send_error(self, message, status, *, param=None, code=None,
                type_="invalid_request_error"):
    self.send_json({"error": {
        "message": message,
        "type": type_,
        "param": param,
        "code": code,
    }}, status)
```

- 401：`type="invalid_request_error"`, `code="invalid_api_key"`
- 400 参数错误：`type="invalid_request_error"`, `param=<字段名>`, `code="invalid_value"` / `"missing_required_parameter"`
- 500/503：`type="server_error"`, `code=None`
- 404 未知路径也改成对象形式。

2. **不要把 `stream: false` 当作错误**

```python
stream = req.get("stream")
if stream is True:
    # 返回明确的 400/501，而不是把 false 也拒掉
```

edits 的 multipart 同理：只有 `stream` 解析为 true 才拒绝。

3. **接受 `n: null`、`response_format: null`**（null 等价于默认值）。

### P1：标准参数/能力缺失

4. **`safety_identifier`**：两个接口都应接受（可以忽略），不要 400。
5. **edits 的 `markup`、`partial_images`**：接受并忽略，或明确返回 `param` 指向该字段的 400，而不是 “unexpected field”。
6. **edits 多图**：支持 `image` 重复 part（最多 16）；至少不要用 “must be provided at most once” 拒绝 SDK 的标准多图调用。
7. **edits JSON body**：支持 `application/json` 的 `images[]` + `image_url`/`data URL`/`file_id`；若暂不实现，返回带 `param` 的明确 400，而不是 Content-Type 错误。
8. **`n` 1–10**：如果 Gemini 侧确实只能生成 1 张，建议显式返回 `code="unsupported_value"` 并说明；完全一致则需要循环生成或拒绝 >1 时给出标准错误结构。
9. **参数校验/语义**：`size`、`quality`、`background`、`output_format` 等目前“接受但静默忽略”。二选一：
   - 真正实现（例如按 `output_format` 转码、按 `size` 缩放）；
   - 或返回 `400 + param + unsupported_value`，不要静默忽略。
   静默忽略会让客户端以为参数生效（README 已声明，但 API 层面仍不一致）。
10. **`model` 必填**：按 2.3.0，两个接口的 multipart/JSON 请求都要求 `model`（JSON edits 有默认值）。当前允许省略，属于偏宽松；若要严格对齐应补 `required` 校验。

### P2：响应完整度与运维细节

11. **补充顶层响应字段**：`output_format`（实际 png/jpeg/webp）、`size`、`quality`、`background`；能拿到 token 用量时补 `usage`。
12. **`url` 语义**：如要完全一致，应返回可下载的 HTTPS URL（带短期签名）；当前 data URL 是 schema 合法但行为不同，建议在响应/文档中显式标注为扩展。
13. **`x-request-id` 响应头**：OpenAI 每个响应都有；建议生成并回传，便于排障。
14. **状态码映射**：上游生成失败优先 500 或 503；502 不是 `/images/*` 文档中的状态码。
15. **文件限制对齐**：图片 10MiB vs 标准 50MB（偏严）；mask 10MiB vs 标准 4MB（偏松）；`prompt` 32000 未校验；`image` 格式接受 gif/bmp（标准 GPT-Image 只列 png/webp/jpg）。
16. **edits 的 `style`**：标准 edits 没有此字段；保留会形成超集，删除或继续忽略均可，但建议与 generations 的白名单分开维护。

---

## 六、复现证据

### 6.1 请求探测（mock 上游，真实 handler）

```text
gen missing model        : 200  （标准要求 model 必填）
gen stream false         : 400  {"error":{"message":"invalid image generation request"}}
gen n null               : 400  {"error":{"message":"only n=1 is supported"}}
gen safety_identifier    : 400  {"error":{"message":"invalid image generation request"}}
gen invalid quality      : 200  （quality:"ultra" 被接受）
gen response_format null : 400  {"error":{"message":"response_format must be b64_json or url"}}
gen url                  : 200  data:image/png;base64,...
edit basic no model      : 200  （标准要求 model 必填）
edit stream=false        : 400  {"error":{"message":"stream is not supported"}}
edit markup              : 400  {"error":{"message":"unexpected field: markup"}}
edit safety_identifier   : 400  {"error":{"message":"unexpected field: safety_identifier"}}
edit partial_images      : 400  {"error":{"message":"unexpected field: partial_images"}}
edit style               : 200  （edits 标准无 style 字段）
edit multi-image         : 400  {"error":{"message":"image must be provided at most once"}}
edit JSON body           : 400  {"error":{"message":"Content-Type must be multipart/form-data"}}
```

### 6.2 成功响应头

```text
200 {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*',
     'Content-Length': '63'}
# 无 x-request-id / openai-processing-ms
```

### 6.3 现有测试

```text
python -m unittest tests.test_modular_sync -q
Ran 76 tests in 0.684s
OK
```

现有测试验证的是实现自身的行为与安全边界（mask 校验、重试、合成、fallback），**没有**按 OpenAI OpenAPI 做 schema 校验，因此上述不一致不会被现有测试发现。

---

## 七、建议的验收清单

- [ ] 所有错误响应通过 `ErrorResponse` JSON Schema 校验（`type/message/param/code` 必填）
- [ ] `stream:false`、`n:null`、`response_format:null` 返回 200 或标准错误结构
- [ ] `safety_identifier`、`markup`、`partial_images` 不再返回 “unexpected field”
- [x] edits 多图最终决定不做；重复 `image` part 返回标准 `unsupported_value` 错误
- [ ] edits 支持 JSON body（`images[]`）或返回标准 `param` 错误
- [ ] `n` 的 1–10 语义有明确结论（支持或标准错误）
- [ ] `size/quality/background/output_format` 有实际效果或明确报错，不再静默忽略
- [ ] `size` 的每个合法值（含 `auto`、`1536x1024`、`1024x1536`）都能控制输出宽高比，或在无法支持时返回标准 `param=size` 错误
- [ ] edits 的 `size` 与源图比例不同时，输出真正按请求尺寸生成/缩放，或明确报错
- [ ] 成功响应补 `size`（实际输出宽高）
- [ ] 成功响应补 `output_format`/`size`/`quality`/`background`，有用量时补 `usage`
- [ ] 响应头包含 `x-request-id`
- [ ] 上游失败映射到 500/503，而不是 502
- [ ] 增加基于 OpenAI OpenAPI 的 contract tests（请求 + 响应 + 错误三套 schema）

---

## 八、专项：`size` 与宽高比检查

### 8.1 OpenAI 标准里没有独立的 `aspect_ratio` 参数

OpenAI Images API 用 **`size`** 表达尺寸和比例，没有 `aspect_ratio` 字段：

- generations：`size` ∈ `auto` / `1024x1024` / `1536x1024` / `1024x1536` / `256x256` / `512x512` / `1792x1024` / `1024x1792`，默认 `auto`；GPT-Image-2 起还支持任意 `WIDTHxHEIGHT`（16 的倍数，比例 1:3–3:1）。
- edits：`size` ∈ `auto` / `1024x1024` / `1536x1024` / `1024x1536`（JSON 变体）；multipart schema 还保留 `256x256` / `512x512` 兼容枚举。
- 响应顶层还有 `size` 字段回报实际尺寸。

因此：客户端发 `aspect_ratio` / `image_size` 本身就不是 OpenAI 标准参数；当前实现返回 400 是“符合 OpenAI 对未知字段的处理”，但很多聚合客户端会这么发。

### 8.2 当前实现：`size` 被接受但完全忽略

- generations：`size` 在 `ignored_compat_fields` 中（`server.py` L1154–L1156），只记录日志；实际调用是 `generate_image_structured(prompt)`（L1185），**没有任何尺寸/比例参数**。
- Gemini 请求体 `_build_image_payload`（`gemini.py` L427–L446）只有 prompt 和附件，没有 size/ratio 字段。
- edits：`size` 在 `allowed_fields` 中（L841–L844），随后进入 `ignored_fields` 被记录（L874–L877）；有 mask 时输出尺寸由 `composite_crop_edit` 按源图/crop 尺寸决定，不可能按请求的 `size` 输出。
- `image_ops._SUPPORTED_ASPECTS`（L34–L44，1:1、4:3、3:4、3:2、2:3、5:4、4:5、16:9、9:16、21:9、9:21）**只用于 edits 的 crop box 扩张**，没有接到生成请求或 `size` 映射上。

黑盒实测（mock 上游，真实 handler）：

```text
size='1024x1024'  -> 200 calls=call('a red fox')
size='1536x1024'  -> 200 calls=call('a red fox')
size='1792x1024'  -> 200 calls=call('a red fox')
size='not-a-size' -> 200 calls=call('a red fox')   # 非法值也通过
size=None         -> 200 calls=call('a red fox')
aspect_ratio 16:9 -> 400 {"error":{"message":"invalid image generation request"}}
image_size portrait -> 400 {"error":{"message":"invalid image generation request"}}
```

也就是说：

1. `size` 合法值、非法值、缺省值走的是**完全相同的上游调用**，宽高比不可控。
2. 客户端请求 16:9 或竖图不会报错，会静默拿到 Gemini 的默认比例（通常是方图），这是“看起来成功、结果不对”的兼容问题。
3. 响应没有 `size` 字段，客户端无法发现实际比例和请求不一致。
4. README 已声明该限制（L440、L454），但 API 行为上仍是静默忽略，不是标准行为。

### 8.3 建议

1. **先做严格校验**：按 2.3.0 的 enum 校验 `size`；非法值返回标准错误 `param=size`，不要再 200。
2. **把 `size` 映射到 Gemini 的比例能力**：
   - 最小实现：在 prompt 里显式追加比例约束（例如 `Output aspect ratio: 16:9`），但这是概率性的，不能保证；
   - 正确实现：抓/复用 Gemini Web UI 的比例选择字段（RPC 里应有对应位置），把 `size` 映射到最接近的受支持比例（可复用 `_SUPPORTED_ASPECTS`），生成后按 `size` 缩放/裁切；
   - edits：`size=auto` 保持源图尺寸；显式 `size` 时先按目标比例构建 crop，再合成/缩放到目标尺寸。
3. **响应回报实际 `size`**，并在无法满足请求比例时返回明确错误，而不是静默返回方图。
4. **`aspect_ratio`/`image_size` 作为扩展参数**：如果目标是兼容第三方客户端，可以额外接受并映射到 `size`；但不要把它算作 OpenAI 标准字段。
5. **增加 contract test**：同一 prompt、不同 `size` 必须产生不同的上游参数/输出尺寸；非法 `size` 必须 400 且带 `param=size`。

---

**总体判定：当前两个接口属于“OpenAI 形状兼容、常用路径可用”，但未达到“与标准完全一致”。优先修错误 schema、`stream:false`/新字段拒绝，以及 `size`/宽高比静默失效这三类问题，可以用较小改动消除大部分严格客户端的兼容风险。**


---

## 九、修复记录（2026-11-03）

本轮已按上面清单完成协议层修复，全部测试通过（`python -m unittest discover -s tests`，167 项）。

### 9.1 已修复

| # | 问题 | 修复结果 |
|---|---|---|
| 1 | 错误响应缺 `type/param/code` | 新增 `send_openai_error()`；images 及全部 OpenAI 兼容 `/v1` 接口统一使用 `ErrorResponse` 四字段，404 也改为对象 |
| 2 | `stream:false` 被拒 | 现在只拒绝 `stream:true`，返回 `param=stream, code=unsupported_value`；`false/null` 正常通过 |
| 3 | `n:null` / `response_format:null` 被拒 | null 视为默认值，正常通过 |
| 4 | `safety_identifier` 被拒 | 两个接口均接受并忽略，最长 128 字符校验 |
| 5 | `markup`/`partial_images` 被拒 | `partial_images` 接受 0–3；`markup` 返回明确的 `unsupported_value`，不再是 `unexpected field` |
| 6 | edits 多图 | 最终决定不做多图：重复 `image` 或多元素 `images` 返回标准 400（`param=image/images, code=unsupported_value`）；单图 + mask 继续支持 |
| 7 | edits JSON body 不支持 | 支持 `application/json` + `images:[{image_url}]`，支持 HTTPS/data URL；`file_id` 返回标准 `unsupported_value`，错误信息提示改用 `image_url` |
| 8 | `n` 只允许 1 | generations 和 edits 均支持 1–10，返回对应数量的 `data` 条目 |
| 9 | `size` 被静默忽略 | 校验 `auto`/`WxH`（16 倍数、1:3–3:1）；写入宽高比提示词；ImageMagick 可用时缩放/裁切到目标尺寸 |
| 10 | 非法 `size/quality/...` 返回 200 | `size`、`quality`、`background`、`moderation`、`output_format`、`output_compression`、`input_fidelity` 及 generations 的 `style` 均按 enum/范围校验，错误带 `param` |
| 11 | 响应缺 `size/output_format` | 成功响应在可获得时返回实际 `size` 和 `output_format` |
| 12 | 缺 `x-request-id` | 普通 JSON 响应和 SSE 响应均带 `x-request-id: req_...` |
| 13 | 图片上游错误用 502 | images 接口上游错误改为 500 + `type=server_error, code=upstream_error` |
| 14 | 文件限制不一致 | 图片上限 50 MiB、mask 4 MiB、prompt 32000 字符；图片必须是 PNG/JPEG/WebP 且有可识别签名 |
| 15 | 404 返回字符串 | 统一为 `{"error": {"message": "not found", "type": ..., "param": null, "code": "not_found"}}` |
| 16 | `url` 返回 data URL / 托管 URL | 明确放弃服务端托管 URL；`response_format=url` 返回 `unsupported_value`，只支持 `b64_json` |

### 9.2 全接口错误 schema 统一（todo #23）

所有 OpenAI 兼容 `/v1` 接口（chat、responses、caches、images 及通用 404）已统一为
`{"error": {"type", "message", "param", "code"}}`，SSE 中途错误也使用同一结构。错误类型按
HTTP 状态映射：

| HTTP 状态 | `type` |
|---|---|
| 400 / 409 / 422 | `invalid_request_error` |
| 401 | `authentication_error` |
| 403 | `permission_error` |
| 404 | `invalid_request_error` |
| 429 | `rate_limit_error` |
| 5xx | `server_error` |

代理自定义原因放在 `code`，例如 `tool_call_parse_error`、`upstream_temporary_refusal`、
`upstream_stream_error`；需要客户端重试时保留扩展字段 `retryable`。`/v1beta/*` 的鉴权、
404 和通用异常继续返回 Google 风格 `{"error": {"code", "message", "status"}}`，不套用
OpenAI schema。

### 9.3 `n>1` 成本、预算与尺寸回显（todo #24）

- `n` 继续按顺序执行 n 次独立上游生成；没有 seed/batch，不承诺多张图的视觉一致性。
- 新增 `image_request_budget_sec`（默认 900，`0` 关闭）作为整个 Images 请求的总时间预算。
- 预算传入每次图片生成（包括 RPC 重试），并传入生成图下载；超时返回
  HTTP 504、`type=server_error`、`code=request_timeout`，不再受 `n × retry × timeout`
  的无上限叠加影响。
- 多图响应只有在所有输出实际尺寸一致时才返回响应级 `size`；混合尺寸或任一尺寸无法
  确定时省略，避免虚报。
- 测试覆盖：预算到期返回 504、预算 helper、预算为 0 时不发上游请求、`n=2` 尺寸不一致时
  省略 `size`。

### 9.4 保留的差异（有意或后端限制）

| 差异 | 原因 / 处理 |
|---|---|
| edits 多图不做 | 需求确认单图与单图 + mask 已足够；多图请求返回标准 `unsupported_value` 错误，不再规划 |
| `url` 不支持 | 服务不托管图片；`response_format=url` 返回标准 `unsupported_value`，客户端应使用 `b64_json` 自行保存/托管 |
| `quality/background/moderation/input_fidelity` 不生效 | 已接受并校验，但 Gemini Web 无对应控制能力；`background=transparent` 明确返回 `unsupported_value`，其余值文档标注为 no-op |
| `output_compression` | 在 `output_format=jpeg/webp` 且 ImageMagick 可用时应用；其他情况忽略 |
| `usage` 为代理侧估算 | Gemini Web 不提供 OpenAI 风格 token 计量；使用 `ceil(UTF-8 字节/4)` 估算文本，`ceil(宽/32)*ceil(高/32)` 估算输入/输出图片；文档明确不可用于计费 |
| `markup` 不支持 | 返回标准 `unsupported_value`，避免静默忽略 |
| `stream`/`partial_images` | `stream=true` 返回 `unsupported_value`；`partial_images` 0–3 接受但无效果，后端只有最终图片 |
| `model` 仅做入口校验 | OpenAI 图片别名、Gemini 名前缀、16 位 upstream ID 接受并共用同一 Gemini 图片管线；其他名称返回 `model_not_found`；JSON edits 缺省为 `gpt-image-2.5-sunburst` |
| `size` 回显 | 使用纯 Python 读取 PNG/JPEG/WebP 实际尺寸；省略/`auto` 也回显；ImageMagick 不可用时回显真实尺寸而非请求值 |
| 精确尺寸依赖 ImageMagick | 不可用时退化为比例提示词，响应不虚报 `size`（拿不到实际尺寸就不返回） |

### 9.5 新增测试

- 错误 schema + `x-request-id`
- 全部状态码到 OpenAI `type` 的映射，以及 401/404 端点的四字段断言
- `/v1beta` 鉴权和 404 保持 Google 错误对象的断言
- `stream:false`、`n:null`、`response_format:null`、`safety_identifier`
- 多输出 `n=2` + `size` 提示词 + 响应 `size/output_format`
- `n>1` 实际尺寸不一致时省略响应 `size`；预算耗尽返回 504 `request_timeout`
- `partial_images`、`safety_identifier` 单图兼容
- JSON edits（data URL）
- `n=2` edits
- 非法 `n/size` 的 `param`/`code`
- `response_format=url` 在 generations、multipart edits、JSON edits 中均返回 `unsupported_value`
- `background=transparent` 在 generations 和 edits 中返回 `unsupported_value`
- `markup` 在 multipart 和 JSON edits 中返回 `unsupported_value`
- `stream=true` 拒绝；`stream=false/null` 与 `partial_images=0..3` 正常通过
- 图片模型命名空间：OpenAI 别名 / Gemini 名 / 16 位 ID 通过，未知名称返回 `model_not_found`
- 默认/`auto` 尺寸回显；ImageMagick 不可用时回显真实尺寸
- JSON edits 的 `images[0].file_id` 和 `mask.file_id` 返回 `unsupported_value`
- 图片 `usage` 估算：文本 UTF-8 字节/4、图片 32×32 patch，并覆盖 generations/edits
- `model` 必填、edits `style` 拒绝、GIF/BMP 拒绝
- edits 多图拒绝（multipart 与 JSON）

**本轮变更纳入正式版本 `v1.3.0`。**

**更新后判定：两个 images 接口的核心请求/响应/错误 schema 已与 OpenAI 2.3.0 对齐；
全部 OpenAI 兼容 `/v1` 接口的边缘错误也已统一为 `ErrorResponse`。`url` 明确不支持，
剩余差异均为已声明的后端能力限制或兼容超集。**

## 10. `v1.3.1` 部署补丁

Serv00 实际部署验证发现两个与图片协议无关、但会阻断生产使用的问题：

- 文本 `/v1/chat/completions` 的 buffered 路径使用了过期的页面 XSRF token，上游返回 HTTP 400；同时 urllib 请求声明了 gzip/br/zstd，却不会自动解压响应。
- 离线包内的 `curl_cffi` wheel 使用 `freebsd_14_3_release_amd64` 标签，而 Serv00 主机报告 `freebsd_14_3_release_p19_amd64`；pip 因此拒绝安装。旧包还缺少 `cffi` 运行时 wheel。

`v1.3.1` 的处理：

- buffered 文本请求改用 `Accept-Encoding: identity`；遇到 400 时刷新页面 token 并重试一次，streaming 路径同样处理。
- 模型发现也改用 fresh page token + `identity`，避免首次启动因旧 token/压缩响应拿不到模型快照而退回 legacy 路由。
- 离线安装器在校验全部 wheel 哈希后，直接安全解压已验证 wheel 到 venv，绕过 pip 对 patch-level 平台标签的精确匹配；安装后实际导入 `cffi`、`curl_cffi` 并执行 Chrome impersonation 探测。
- 构建 wheelhouse 时加入 `cffi` 及 `pycparser`，确保全新安装不需要复用旧 venv。

新增测试覆盖 buffered/streaming 的 fresh token 重试和 `identity` 编码。`v1.3.0` 的 OpenAI Images 行为保持不变。

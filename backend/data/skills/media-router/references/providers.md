# 各平台接入参数对照

配置写在 `config/models.yaml`。本文件是**查表用**的：接某个平台时，照抄对应的 `provider` 段落。

> 不想手写 YAML 的话，运行 `media_router.py web` 打开网页配置界面：所有平台的中文说明、
> 默认接口地址、密钥获取入口、只读校验端点都已经内置在 `scripts/mrouter/catalog.py` 里，
> 页面直接调它们。本文件仍然适合需要精确控制参数（`params`、`options`、自定义 JSON 路径）的场景。

## provider 取值总表

| `provider` | 平台 / 协议 | 图片 | 视频 | 需要密钥 |
|---|---|---|---|---|
| `native` | 内置工具（ImageGen / VideoGen 等） | ✓ | ✓ | 否 |
| `openai` | OpenAI 及任何兼容 `/v1/images/*` 的网关 | ✓ | — | 是 |
| `dashscope` | 阿里云百炼 · 通义万相 | ✓ | ✓ | 是 |
| `volcengine` | 火山方舟 · 即梦 / Seedance | ✓ | ✓ | 是 |
| `kling` | 可灵（快手） | ✓ | ✓ | 是（AK + SK） |
| `replicate` | Replicate | ✓ | ✓ | 是 |
| `fal` | fal.ai | ✓ | ✓ | 是 |
| `generic_http` | 任意 REST 接口，配置驱动 | ✓ | ✓ | 可配 |

别名（写哪个都行）：`openai_image`≡`openai`、`aliyun`/`bailian`≡`dashscope`、
`ark`/`doubao`≡`volcengine`、`keling`≡`kling`、`falai`≡`fal`、`generic`/`http`≡`generic_http`。

查看当前版本实际支持的列表：`python3 scripts/media_router.py providers`

---

## native — 内置工具通道

不调任何 HTTP。返回一条"请你去调 ImageGen / VideoGen"的指令（`status: delegate`，退出码 3），
由 agent 用它自己环境里的内置能力完成。

适合当兜底：放在最高的 priority 上，保证没配 Key 时任务也不会彻底断掉。

```yaml
- id: builtin-imagen
  provider: native
  priority: 9          # 数值大 = 最后才轮到
  weight: 100
  enabled: true
  supports: [text2img, img2img]
```

不需要 `model` / `api_key_env` / `endpoint`。

---

## openai — OpenAI 及兼容网关

`/v1/images/generations`（文生图）与 `/v1/images/edits`（图生图，multipart 上传）。
只要网关兼容这两个端点，改 `endpoint` 就能复用 —— SiliconFlow、Together、各类自建网关都算。

| 字段 | 说明 |
|---|---|
| `api_key_env` | 密钥环境变量名 |
| `endpoint` | 默认 `https://api.openai.com/v1/images/generations`；图生图时自动把 `/generations` 换成 `/edits` |
| `params` | 透传字段，如 `size`、`quality`、`style`、`background` |

```yaml
# OpenAI 官方
- id: gpt-image
  provider: openai
  model: gpt-image-1
  priority: 1
  weight: 20
  supports: [text2img, img2img]
  api_key_env: OPENAI_API_KEY
  params:
    size: 1024x1024

# SiliconFlow（同一套协议，改 endpoint）
- id: siliconflow-kolors
  provider: openai
  model: Kwai-Kolors/Kolors
  priority: 2
  weight: 15
  supports: [text2img]
  api_key_env: SILICONFLOW_API_KEY
  endpoint: https://api.siliconflow.cn/v1/images/generations
  params:
    image_size: 1024x1024
```

**注意**：`gpt-image-1` 默认就返回 `b64_json` 且不接受 `response_format` 参数，
所以**不要**往 `params` 里加 `response_format`。`dall-e-3` 则需要显式写
`response_format: url`。脚本对两种返回形态都做了处理。

---

## dashscope — 阿里云百炼（通义万相）

异步任务：提交拿 `task_id`，再轮询 `/api/v1/tasks/{id}`。脚本内部已实现轮询，
并对 429/5xx 自动重试。

| 字段 | 说明 |
|---|---|
| `api_key_env` | 建议 `DASHSCOPE_API_KEY` |
| `endpoint` | 图片默认 `.../services/aigc/text2image/image-synthesis`；视频默认 `.../services/aigc/video-generation/video-synthesis` |
| `params` | 图片：`size`（如 `1024*1024`）、`style`、`prompt_extend`、`watermark`；视频：`size`、`duration` |

```yaml
- id: wanx-2.5
  provider: dashscope
  model: wanx2.5-t2i-turbo
  priority: 1
  weight: 30
  supports: [text2img]
  api_key_env: DASHSCOPE_API_KEY
  params:
    size: 1024*1024

- id: wanx-video
  provider: dashscope
  model: wanx2.1-t2v-turbo
  priority: 2
  weight: 40
  supports: [text2video, img2video]
  api_key_env: DASHSCOPE_API_KEY
  params:
    size: 1280*720
    duration: 5
```

注意万相的尺寸用 `*` 分隔（`1024*1024`），不是 `x`。

---

## volcengine — 火山方舟（即梦 / Seedance）

**图片走同步接口，视频走异步任务。**

| 字段 | 说明 |
|---|---|
| `api_key_env` | 建议 `ARK_API_KEY`（方舟的 API Key） |
| `endpoint` | 图片默认 `https://ark.cn-beijing.volces.com/api/v3/images/generations`；视频默认 `.../contents/generations/tasks` |
| `params` | 图片：`size`、`response_format`、`watermark`、`guidance_scale`、`seed`；视频：`ratio`、`duration` |

```yaml
- id: jimeng-seedream
  provider: volcengine
  model: doubao-seedream-3-0-t2i-250415
  priority: 1
  weight: 50
  supports: [text2img]
  api_key_env: ARK_API_KEY
  params:
    size: 1024x1024
    response_format: url
    watermark: false

- id: seedance
  provider: volcengine
  model: doubao-seedance-1-0-pro-250528
  priority: 1
  weight: 60
  supports: [text2video, img2video]
  api_key_env: ARK_API_KEY
  params:
    ratio: "16:9"
    duration: 5
```

视频的 `ratio` / `duration` 会被自动拼进提示词（` --ratio 16:9 --dur 5`），
你也可以用 `--aspect-ratio` / `--duration` 在命令行覆盖。

`model` 填的是方舟上的**模型 ID 或推理接入点 ID（ep-xxx）**，按你控制台里的实际值填。

---

## kling — 可灵

用 AK/SK 签 HS256 JWT 鉴权（脚本内纯标准库实现，不需要 PyJWT）。
图片与视频都是异步任务。

**需要两个值**，三种给法任选：

1. 一个环境变量里用冒号分隔：`KLING_KEYS="你的AK:你的SK"`（推荐，配置最简单）
2. 分开两个环境变量 + `options.access_key_env` / `options.secret_key_env`
3. `options.access_key` / `options.secret_key` 直接内联（不推荐）

```yaml
- id: kling-v2
  provider: kling
  model: kling-v2-master
  priority: 3
  weight: 100
  supports: [text2video, img2video]
  api_key_env: KLING_KEYS
  params:
    mode: std            # std | pro
    cfg_scale: 0.5
```

图片走 `.../v1/images/generations`，视频走 `.../v1/videos/text2video` 或
`.../v1/videos/image2video`（给了 `--image` 就自动走 image2video）。

---

## replicate — Replicate

两种提交方式，脚本按你有没有给 `version` 自动选：

- 给了 `options.version` → `POST /v1/predictions`，body 带 `version`
- 没给 → `POST /v1/models/{model}/predictions`，`model` 写 `owner/name`

```yaml
# 方式一：按 owner/name（推荐，不用查 version hash）
- id: flux-schnell
  provider: replicate
  model: black-forest-labs/flux-schnell
  priority: 2
  weight: 20
  supports: [text2img]
  api_key_env: REPLICATE_API_TOKEN
  params:
    aspect_ratio: "1:1"

# 方式二：锁定某个 version hash
- id: flux-dev-pinned
  provider: replicate
  model: black-forest-labs/flux-dev
  priority: 1
  weight: 40
  supports: [text2img]
  api_key_env: REPLICATE_API_TOKEN
  options:
    version: "你的-version-hash"
```

想精确控制模型入参，用 `options.inputs` 或命令行 `--param key=value`，
它们会合并进 `input` 对象。

---

## fal — fal.ai

队列接口：提交 → 轮询 `status_url` → 取 `response_url`。鉴权头是 `Key <token>`（不是 Bearer）。

```yaml
- id: fal-flux
  provider: fal
  model: fal-ai/flux/schnell
  priority: 2
  weight: 30
  supports: [text2img]
  api_key_env: FAL_KEY
  params:
    image_size: square_hd
```

图生视频时把输入图放 `--image`，脚本会填到 `image_url` 字段。

---

## generic_http — 任意 REST 接口（不用写代码）

最灵活的一个。只要能描述清"怎么提交"和"怎么查结果"，就能接上。

### 顶层开关

| 字段 | 默认 | 说明 |
|---|---|---|
| `options.auth.type` | `bearer` | `bearer` / `key` / `token` / `header` / `query` / `none` |
| `options.auth.header` | `X-API-Key` | `type: header` 时的头名 |
| `options.auth.query` | `key` | `type: query` 时的参数名 |
| `options.auth.type: none` | — | 公开接口，不需要密钥（此时 `api_key_env` 可省略） |
| `options.sync` | `false` | `true` = 提交响应里直接就是结果，跳过轮询 |
| `options.keyless` | `false` | 强制声明不需要密钥 |

### `options.submit`

| 字段 | 说明 |
|---|---|
| `url` | 提交地址，支持 `{{占位符}}` |
| `method` | 默认 `POST` |
| `headers` | 额外请求头 |
| `params` | 额外查询参数 |
| `body` | 请求体模板（嵌套结构里的占位符也会被替换） |
| `task_id_path` | 从提交响应里取任务 id 的点号路径，如 `data.task_id` |

### `options.poll`

| 字段 | 说明 |
|---|---|
| `url` | 查询地址，通常含 `{{task_id}}` |
| `method` | 默认 `GET` |
| `interval` | 轮询间隔秒数，默认取全局 `defaults.poll_interval_seconds` |
| `headers` / `params` | 额外请求头 / 查询参数 |
| `status_path` | 状态字段的点号路径，如 `data.status` |
| `success` | 视为成功的状态值（小写比较），默认 `[succeeded, success, done, completed]` |
| `failure` | 视为失败的状态值（小写比较），默认 `[failed, error, canceled]` |
| `result_path` | 产物数组/对象的点号路径，如 `data.images` |
| `result_url_field` | 当产物是对象数组时，取哪个字段当下载地址，默认按 `url` → `video_url` → `image_url` → `file_url` → `download_url` 依次尝试 |

> 如果**不写** `status_path`，脚本会认为"只要 `result_path` 能取到东西就算完成" ——
> 适合那些提交后直接返回结果的简单接口。

### 可用占位符

`{{prompt}}`、`{{negative_prompt}}`、`{{model}}`、`{{size}}`、`{{aspect_ratio}}`、
`{{duration}}`、`{{count}}`、`{{image}}`、`{{image_url}}`、`{{image1}}`~`{{image3}}`、`{{task_id}}`（仅 poll 段）

如果某个值是纯占位符（如 `size: "{{duration}}"`），会保留原始类型（数字/布尔）而不是转成字符串。

### 完整示例

```yaml
- id: my-custom-image-api
  provider: generic_http
  model: your-model-name
  priority: 3
  weight: 10
  supports: [text2img]
  api_key_env: MY_API_KEY
  options:
    auth:
      type: header
      header: X-API-Key
    sync: false
    submit:
      url: https://api.example.com/v1/images
      method: POST
      body:
        model: "{{model}}"
        prompt: "{{prompt}}"
        width: 1024
        height: 1024
      task_id_path: data.task_id
    poll:
      url: https://api.example.com/v1/tasks/{{task_id}}
      method: GET
      interval: 3
      status_path: data.status
      success: [succeeded]
      failure: [failed, canceled]
      result_path: data.images
      result_url_field: url
```

调试技巧：先用 `resolve` 确认这个模型被选中，再 `generate`；
失败时 `fallback_trail[].error` 会带上 HTTP 状态码和响应片段，照着改 YAML 就行。

---

## 排查清单

| 现象 | 原因与处理 |
|---|---|
| `status: error`，`fallback_trail[].kind: config` | 缺 API Key。核对 `api_key_env` 的名字与实际环境变量是否一致 |
| `will_attempt` 里只有 `builtin-*` | 所有第三方模型都缺 Key，被跳过了 |
| `HTTP 401` / `403` | Key 无效、过期，或鉴权方式写错（可灵/ fal 的格式和其他家不一样） |
| `HTTP 404` | `endpoint` 或 `model` 名写错。方舟的 `model` 必须是控制台里的模型 ID 或 `ep-xxx` |
| `HTTP 429` | 限流。脚本已自动重试 2 次；仍失败说明额度用尽 |
| `未拿到 task_id` | `task_id_path` 写错了，或接口返回结构变了 |
| `任务成功但没解析出产物 URL` | `result_path` 或 `result_url_field` 写错了 |
| 轮询超时 | 调大 `defaults.max_poll_seconds`（视频建议 900 以上） |
| 某模型总被跳过 | 它进了熔断冷却。`health` 看状态，`health --reset` 清空 |

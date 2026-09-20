---
name: media-router
description: >-
  当任务中出现"生成图片 / 生成视频"步骤、而当前模型只能处理文本导致任务被搁置时，
  用用户自己配置好的多模型池完成生成，并把产物回传给文本模型继续推进任务。
  支持多个图像/视频模型共存：按 priority 分优先级档，同档内按 weight 加权随机选模型；
  某个模型失败自动按候选序列降级，连续失败达到阈值自动熔断冷却。
  触发词：生成图片、画一张图、文生图、图生图、改图、生成视频、做个视频、文生视频、图生视频、
  首尾帧、多模型池、模型权重、模型优先级、生成失败降级、media router。
agent_created: true
---

# 媒体生成调度（media-router）

## 这个 skill 解决什么问题

文本模型在多步任务里遇到"生成一张图""做个视频"这类步骤时，因为自己不能出图，
整个任务就被搁置了 —— 用户拿到的是"我无法生成图片"，而不是成品。

这个 skill 让文本模型**自己把这一步补上**：从一个用户维护好的模型池里挑一个模型、
调它的接口、把产物下载到本地、再把结果和文字描述交回给文本模型，任务继续往下走。

模型池、权重、优先级在 `config/models.yaml` 里手改，改完即生效；不想碰 YAML 的用户
可以运行 `media_router.py web` 打开本机网页，点点点配完（见下文「用户不会配配置怎么办」）。

## 什么时候用它

满足**全部**两条件时才用：

1. 当前任务的推进**卡在一个媒体生成步骤**上 —— 需要一张图、一段视频才能继续；
2. 你自己没有直接可用的出图能力（或已有的内置工具不好用，用户明确配了第三方模型池）。

**不要**在这些情况下用它：
- 用户只是随口问"能不能生成图片" —— 先回答，不要真的调接口花钱；
- 用户明确要求用某个具体平台/工具的界面；
- 纯文本任务里没有任何媒体需求。

## 执行流程

### 第 0 步：定位脚本

记 `<SKILL_DIR>` 为**本 SKILL.md 所在目录**。所有命令都基于它，不要硬编码任何机器的绝对路径。

Python 解释器：优先 `python3`，没有就试 `python`。

```bash
# 通用写法
python3 "<SKILL_DIR>/scripts/media_router.py" <子命令> ...
```

Windows 上优先用 Bash 工具执行；若只有 PowerShell，直接调 python，**不要**用
`Start-Process` / `start` / `cmd /c`，也**不要**用 WindowsApps 下的 `python.exe` 存根路径。

### 第 1 步：先确认有没有可用的模型

**这一步不能跳。** 先看路由结果，避免白跑一次长任务：

```bash
python3 "<SKILL_DIR>/scripts/media_router.py" resolve --kind image --supports text2img --pretty
```

看返回里的 `will_attempt` 和 `skipped_missing_key`：

- `will_attempt` 里有**非 native** 的模型 → 有第三方模型可用，直接进第 2 步；
- `will_attempt` 只有 `builtin-*`（native）→ 说明还没配好 API Key。
  这时**不要**去改用户的配置。告诉他两件事：要么按下面的「用户不会配配置怎么办」
  开网页配，要么在 `config/models.yaml` 配 Key / 设环境变量；
  然后问他是否要改用内置工具。**缺 Key 时不要硬试，也不要假装生成了。**

### 第 2 步：执行生成

```bash
# 文生图
python3 "<SKILL_DIR>/scripts/media_router.py" generate --kind image --prompt "一只戴圆框眼镜的橘猫，扁平插画风"

# 图生图（把本地图片或 URL 作为输入）
python3 "<SKILL_DIR>/scripts/media_router.py" generate --kind image --prompt "改成水彩风格" --image "/abs/path/in.png"

# 文生视频
python3 "<SKILL_DIR>/scripts/media_router.py" generate --kind video --prompt "樱花飘落的慢镜头，浅景深" --aspect-ratio 16:9 --duration 5

# 图生视频
python3 "<SKILL_DIR>/scripts/media_router.py" generate --kind video --prompt "镜头缓慢推进" --image "/abs/path/frame.png"
```

常用可选参数：`--size`（如图 `1024x1024`）、`--negative-prompt`、`--count`、
`--output-dir`、`--max-attempts`、`--param key=value`（透传给 provider 的自定义参数）。

**产物默认落盘到当前工作目录的 `./outputs/`**（沙箱友好：很多 agent 把 skill 目录设为只读，
cwd 才是可写的工作区）。想放别处再传 `--output-dir`。stdout JSON 的 `files[].path`
是绝对路径，直接用它引用产物。

**stdout 是单个 JSON 对象，日志在 stderr。** 不要解析 stderr。

命令可能跑较久（视频 1~3 分钟，异步任务要轮询）。直接等它返回，
**不要**在外面套 `sleep` 循环重新调用 —— 脚本内部已经处理了提交与轮询。

### 第 3 步：按退出码和 `status` 分支处理

| 退出码 | `status` | 含义 | 你要做什么 |
|---|---|---|---|
| 0 | `ok` | 生成成功，产物已落盘 | 用 `files[].path` 继续原任务 |
| 3 | `delegate` | 该走内置工具，脚本调不了 | **由你自己调用内置出图工具**，见下 |
| 1 | `error` | 所有候选模型都失败 | 如实报告错误，见下 |

#### 情况 A：`status = "ok"`

返回结构（关键字段）：

```json
{
  "status": "ok",
  "model":  {"id": "jimeng-seedream", "provider": "volcengine", "model": "doubao-seedream-3-0-t2i-250415"},
  "files":  [{"path": "/abs/out/image_jimeng-seedream_20260916_221630412_01.png",
              "bytes": 148213, "size_human": "144.7KB", "format": "png",
              "width": 1024, "height": 1024}],
  "caption": "一只戴圆框眼镜的橘猫，扁平插画风格，暖色背景……",
  "fallback_trail": [],
  "notes": []
}
```

处理要点：
- **`files[].path` 是绝对路径**，后续步骤直接引用它，不要再去找。
- **`caption` 是给纯文本模型用的画面描述。** 因为你可能看不见图像内容，
  汇报或继续推理时应当用它，而不是凭空猜测画面。
- `fallback_trail` 非空说明前面有模型失败了 —— 汇报时可以提一句"某模型不可用已自动切换"，
  但不要把它当成错误。
- `notes` 里有需要让用户知道的信息（比如缺 Key、某模型被熔断）。

#### 情况 B：`status = "delegate"`（退出码 3）—— 最容易做错的一步

这表示配置里选中的是 `provider: native` 的条目，**脚本没有真正生成任何东西**，
它只是把参数和调用指令交回给你：

```json
{
  "status": "delegate",
  "delegate": {
    "tool": "ImageGen",
    "arguments": {"prompt": "……", "size": "1024x1024"},
    "note": "请用 ToolSearch 查找 ImageGen，再用 DeferExecuteTool 以上述 arguments 调用……"
  }
}
```

看到这个，你必须：
1. 用**当前环境里**可用的内置出图/出视频能力，拿 `delegate.arguments` 去调用。
   （在 WorkBuddy 里就是 `ImageGen` / `VideoGen`：先用 ToolSearch 找到工具，再用 DeferExecuteTool 传入参数。）
2. 调用成功后，把**内置工具返回的本地文件路径**继续用于原任务。
3. 想把这个结果反馈给调度器（用于权重统计）时，可以执行：
   ```bash
   python3 "<SKILL_DIR>/scripts/media_router.py" report --model <delegate 里的模型 id> --ok
   ```
   失败了就用 `--fail --error "原因"`。

**绝对不要把 `delegate` 当成"生成成功"。** 它还没有图。

#### 情况 C：`status = "error"`（退出码 1）

`fallback_trail` 里会逐条列出每个模型失败的原因和 `kind`：
- `kind: "config"` → 配置问题（多半是缺 API Key），去提示用户配 Key；
- `kind: "runtime"` → 接口调用失败（限流、额度、网络、参数不合法），
  把 `error` 原文一并告诉用户，不要含糊成"生成失败了"。

然后**如实**告诉用户哪一步没走通、需要他做什么。不要编造图片描述，不要伪造文件路径。

### 第 4 步：继续原任务

拿到产物后，回到用户原本的请求上把它用完 —— 插进文档、作为 PPT 配图、
写进 HTML、拼进视频时间轴……**这才是这个 skill 存在的意义**：
把被搁置的任务接着做完，而不是交一个文件路径了事。

## 命令参考

| 命令 | 用途 |
|---|---|
| `config` | 看配置解析结果、路径、可用 provider |
| `providers` | 看内置支持哪些 provider 及其接入方式 |
| `list [--kind image\|video]` | 看模型池、优先级、权重、Key 是否就绪、是否被熔断 |
| `resolve --kind ... ` | **只看会选谁**，不真调用。排查首选/降级序列用 |
| `generate --kind ... --prompt "..."` | 执行生成 |
| `report --model ID --ok\|--fail [--error MSG]` | 回写某个模型的成败（外部调用后同步健康度） |
| `health [--reset [--model ID]]` | 看/清熔断状态 |
| `web [--port 8760] [--no-browser]` | 打开本机网页配置界面（给不写配置文件的用户用） |

## 用户不会配配置怎么办：让他开网页配

这个 skill 的使用者可能完全不懂 YAML。**缺 Key 或没有可用模型时，
不要丢一段 YAML 让他改**，直接给他一条命令：

```bash
python scripts/media_router.py web
```

会在本机起一个只监听 `127.0.0.1` 的配置页面并自动打开浏览器：

1. **① 厂商配置**：从 9 个内置平台里点一个（带中文说明和"去哪拿 Key"的提示），
   粘贴 API Key，保存。密钥写进 `config/secrets.web.yaml`（已在 `.gitignore` 里）。
2. **② 模型配置**：下拉选刚配好的厂商 → 填模型名称 → 打上"图像/视频"标签 →
   **点「测试连通性」** → 配权重 → 保存。

要点：

- **连通性测试默认不花钱、不生成任何东西** —— 只调各平台的只读接口
  （模型列表 / 任务列表 / 账号信息）来验密钥。拿不到只读接口的平台（如 fal）
  会如实标注"无法免生成校验"，由页面引导用户点「试生成」做深度验证（会消耗额度，页面会先弹确认框）。
- 页面写的是 `config/models.web.yaml`（厂商+模型，不含密钥）和 `config/secrets.web.yaml`。
  这两个文件**叠加**在用户手写的 `models.yaml` / `secrets.yaml` 之上，同 id 以页面为准，
  **绝不会覆盖用户手写的内容和注释**。
- 只用 `MEDIA_ROUTER_CONFIG` / `MEDIA_ROUTER_SECRETS` 指向别的文件时进入只读模式，
  写接口返回 409，页面会提示原因。
- 页面是给人在终端里用的交互命令，会一直阻塞到 Ctrl-C。**你自己不要用 Bash 阻塞调用它。**

## 配置怎么改（用户的事，不要擅自改）

改 `config/models.yaml`：

```yaml
image:
  strategy: priority_then_weight   # 默认：先分档，同档按权重加权随机
  models:
    - id: jimeng-seedream
      provider: volcengine
      model: doubao-seedream-3-0-t2i-250415
      priority: 1                  # 数值越小越优先，priority=1 永远压过 priority=2
      weight: 50                   # 同档内的权重，50 和 30 就是 50:30 的出手比
      enabled: true
      supports: [text2img]
      api_key_env: ARK_API_KEY     # 密钥从哪个环境变量读
```

- 三种 `strategy`：`priority_then_weight`（默认）、`weight_only`、`fallback_chain`。
  视频贵且要可预测时用 `fallback_chain`。
- 密钥放环境变量，或 `config/secrets.yaml` 的 `keys` 下。**不要**写进 `models.yaml` 的 `api_key` 字段。
- 接入文档里没有的平台时，用 `provider: generic_http` 填 YAML 即可，不用改代码。
  各家参数对照见 `references/providers.md`。
- **⚠️ 比值/日期/占位符这类值必须加引号**：裸写的 `1:1` 会被 YAML 当成六十进制
  整数 **61**（`16:9` 是 969）。这个坑不报错，只是悄悄把 61 发给接口。
  正确写法 `aspect_ratio: "1:1"`。同理 `"{{prompt}}"`、`"2026-01-01"`、
  `"重要: 别删"`（值里不允许出现未加引号的「冒号+空格」）。
  加载时会对原文做体检，这类问题会出现在 `config` / `list` 命令的 `warnings` 里 ——
  **看到 warnings 要转告用户**，别默默忽略。

## 硬性约束

- **缺 Key 不要硬试，也不要假装成功。** 明确告诉用户缺什么。
- **`delegate` 不等于成功**，必须由你补上内置工具的调用。
- **不要编造产物。** 没有 `files[].path` 就没有图；不要凭 prompt 想象一张图然后描述它。
- **不要擅自改动用户的模型池配置或权重。** 需要调整时先问。
- **不要在外面套 sleep 重试循环。** 内部已处理提交与轮询。
- **费用提醒**：第三方生图/生视频是计费的。用户只是"问问能不能"时不要真跑；
  批量生成多个产物前先跟用户确认。

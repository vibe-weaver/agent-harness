# media-router · 媒体生成调度

<p>
  <a href="https://github.com/vibe-weaver">
    <img src="https://github.com/vibe-weaver.png?size=72" width="72" height="72" alt="vibe-weaver" style="border-radius:14px; vertical-align:middle"/>
  </a>
  <br/>
  <a href="https://github.com/vibe-weaver"><strong>vibe-weaver</strong></a>
</p>

给文本模型补上"出图 / 出视频"这条腿。

文本模型遇到"生成一张图""做个视频"这类步骤时只会说"我做不到"，任务就断了。
这个 skill 用一个**你自己维护的多模型池**把这一步接上：按优先级分档、同档内按权重
加权随机挑模型，失败自动降级，连续失败自动熔断，最后把产物和文字描述交回给文本模型，
让原任务继续往下走。

**零依赖**：只用 Python 3.8+ 标准库，不需要 `pip install` 任何东西。
**可移植**：所有路径相对 skill 自身解析，放哪个 agent 的 skills 目录都能跑。

---

## 安装

把这个目录整个复制到目标 agent 的 skills 目录下即可。

| 环境 | 用户级（所有项目可用） | 项目级（只在此仓库生效） |
|---|---|---|
| WorkBuddy | `~/.workbuddy/skills/media-router/` | `<项目>/.workbuddy/skills/media-router/` |
| Claude Code | `~/.claude/skills/media-router/` | `<项目>/.claude/skills/media-router/` |
| 其他 agent | 遵循它自己的 skills 目录约定 | 同左 |

Windows 下 `~` 指 `C:\Users\<你>`。

`SKILL.md` 是给 agent 读的；`README.md`（本文件）是给你读的。

## 不想碰配置文件？开网页点就行

```bash
cd <skill目录>
python3 scripts/media_router.py web
```

终端会打印一个地址（形如 `http://127.0.0.1:8760/?t=...`）并自动打开浏览器。
整个流程就两步：

**① 厂商配置** — 点「添加厂商」，从 9 个内置平台里挑一个（每个都有中文说明和
「去控制台哪里拿 Key」的提示），把 API Key 粘进去，保存。

**② 模型配置** — 点「添加模型」，下拉选刚配好的厂商 → 填模型名称
（也可以点「拉取可用模型」直接从平台拉列表）→ 打上「图像 / 视频」标签 →
**点「测试连通性」** → 调权重滑块 → 保存。

关于「测试连通性」，有件事值得说清楚：**它默认不花钱、不生成任何东西。**
它只去调各平台的**只读**接口（模型列表 / 任务列表 / 账号信息）来验密钥。
刻意没有用"提交一个空任务看报错"这种取巧办法 —— 万一平台把空提示词当合法输入，
花的就是你的钱。拿不到只读接口的平台（比如 fal.ai）会**如实标注**"无法免生成校验"，
而不是假装测过了；想真正确认就点旁边的「试生成」（会消耗额度，页面会先弹确认框）。

写完的文件：

| 文件 | 内容 | 会被提交吗 |
|---|---|---|
| `config/models.web.yaml` | 厂商 + 模型（**不含密钥**） | 会（无敏感信息） |
| `config/secrets.web.yaml` | 密钥 | 不会，已在 `.gitignore` |

这两个文件是**叠加层**：加载时先读你手写的 `models.yaml` / `secrets.yaml`，
再把这两份盖上去，同 id 以页面为准。也就是说**你在手写文件里的内容和注释一个字都不会丢**。

安全上的取舍（这是个能写配置、能读到密钥的本地服务，值得较真）：

- 只监听 `127.0.0.1`，且校验 `Host` 头（防 DNS 重绑定）；
- 校验 `Origin` / `Referer`（防别的网页跨站给你发请求）；
- 每次启动生成一次性随机 token，页面所有请求都要带上；
- 预览产物的接口只允许读 `outputs/` 目录，杜绝任意文件读取；
- 不写后台进程，Ctrl-C 就没了。

`--port` 换端口，`--no-browser` 不自动开浏览器。

### 两个容易困惑的地方

**页面按钮"点不动"时，先看页面上有没有一条红色提示。** 如果后台已经关了
（终端里按了 Ctrl-C、或进程被结束），页面上的按钮确实不会有任何反应 ——
这种情况页面顶部会明确写出"连不上配置服务"和重启命令，不会让你一头雾水。
另外表单打开时会自动滚到视野内，列表长了也不会"看不见反应"。

**"删除"手写层里的模型，实际是"隐藏"。** 页面列的是 `models.yaml`（你手写）
+ `models.web.yaml`（页面生成）两份合并后的结果。手写层里的条目页面**不会去改
你的文件**，所以点删除时它会在叠加层写一条同 id 的"墓碑"把那条盖住 ——
对路由和列表的效果等同于删除，而且随时可以重新添加同 id 的模型恢复。
所以按钮上写的是「隐藏」而不是「删除」，确认框里也会说明。想彻底删掉请直接编辑
`models.yaml`。

同理，编辑手写层里的模型时，厂商下拉框会多一个「（保持原样，不改厂商）」选项：
那种模型用的是内联 `provider`，不挂在任何厂商下，页面只能改它的名称、优先级、
权重和能力标签；接口地址和密钥要去 `models.yaml` 改。

### 想拿别处的配置跑测试

```bash
MEDIA_ROUTER_CONFIG_DIR=/tmp/mr-test python3 scripts/media_router.py web
```

把配置目录整体挪走，读写都落在那个目录里，**不会碰到你真实的 `config/`**。
`MEDIA_ROUTER_SECRETS` / `MEDIA_ROUTER_STATE_DIR` / `MEDIA_ROUTER_OUTPUT_DIR`
同理。写自检、做试验时都用得上。

## 60 秒跑通（命令行版）

```bash
cd <skill目录>

# 忘了有什么命令？光敲脚本名就会列出全部用法
python3 scripts/media_router.py

# 1. 看模型池状态（此时应该有两三个模型显示 api_key_ready: false）
python3 scripts/media_router.py list --pretty

# 2. 配一个 Key（以火山方舟为例）
export ARK_API_KEY="你的-key"          # Windows PowerShell: $env:ARK_API_KEY="你的-key"

# 3. 确认它变成可用了
python3 scripts/media_router.py resolve --kind image --pretty

# 4. 生成
python3 scripts/media_router.py generate --kind image --prompt "一只戴圆框眼镜的橘猫，扁平插画风"
```

第 4 步会返回一个 JSON，`files[0].path` 就是产物在磁盘上的绝对路径。

**忘了命令？** 光敲 `python3 scripts/media_router.py`（不带任何子命令）会列出
全部子命令和常用示例；具体的参数用 `... <子命令> --help`。
不想碰命令行就直接跑 `... web` 开网页配。

**没配任何 Key 也能跑。** 配置里默认带了一个 `provider: native` 的兜底条目，
它不会真出图，而是返回"请调用 ImageGen"的指令（`status: delegate`，退出码 3），
交给 agent 用它自己的内置工具完成。这样在没配 Key 时任务也不会彻底卡死。

## 先验证环境是否正常

```bash
python3 scripts/selftest.py      # 路由 / 生成 / 降级 / 熔断，17 项
python3 scripts/selftest_web.py  # 网页配置全链路，105 项
```

`selftest.py` 会在本地起一个模拟的异步任务接口，把「提交 → 轮询 → 下载 → 落盘 →
读元数据 → 失败降级」整条链路真跑一遍，**不需要任何 API Key**。

`selftest_web.py` 会真起一个 HTTP 服务、真发请求、真写配置文件，然后**还原现场**，
并校验你手写的 `models.yaml` 一个字节都没被动过。覆盖了鉴权、Host/Origin 校验、
厂商与模型增删改、权重、连通性探测、密钥分流、级联删除、只读模式、
以及"手写配置被改坏时页面还能不能打开"。

两个脚本全绿，说明这套代码在你的环境里是可用的
（Windows 中文环境、Python 3.13 实测 17/17 与 105/105 通过）。

## 配置：`config/models.yaml`

你只需要维护这一个文件。

```yaml
image:
  strategy: priority_then_weight
  models:
    - id: jimeng-seedream            # 唯一标识，随便起，报错时会显示
      provider: volcengine           # 用哪套协议，见 references/providers.md
      model: doubao-seedream-3-0-t2i-250415   # 平台侧的模型名
      priority: 1                    # 优先级档位，数值越小越优先
      weight: 50                     # 同档内的权重
      enabled: true
      supports: [text2img]           # 能力标签
      api_key_env: ARK_API_KEY       # 从哪个环境变量读 Key
      params:                        # 固定透传给接口的参数
        size: 1024x1024
```

### ⚠️ YAML 引号：最容易踩的坑

**比值、时间、占位符、日期这几类值必须加引号。** 最阴的是比值 —— YAML 1.1
规定 `1:1` 是**六十进制整数**，所以：

```yaml
params:
  aspect_ratio: 1:1      # ✗ 读出来是数字 61，会原样发给接口
  aspect_ratio: "1:1"    # ✓ 字符串 "1:1"
```

这条特别坏，因为**它不报错**，只是悄悄把 61 发给接口，你要等到出图比例不对
才发现。同类还有：

| 裸写 | 会被读成 | 正确写法 |
|---|---|---|
| `1:1` `16:9` | 61 / 969（六十进制） | `"1:1"` `"16:9"` |
| `2026-01-01` | `datetime.date` 对象 | `"2026-01-01"` |
| `{{prompt}}` | 行内映射（装了 PyYAML 直接报错） | `"{{prompt}}"` |
| `重要: 别删` | 报错（值里不允许出现「冒号+空格」） | `"重要: 别删"` |

拿不准就一律加引号，加了永远没错。

程序会在加载时对原文做一次**体检**，把这些写法挑出来放进 `warnings`：
`config` / `list` 命令能看到，网页配置页顶部也会有提示。比如：

```bash
$ python3 scripts/media_router.py config --pretty | grep -A3 warnings
"warnings": [
  "models.yaml：第 97 行 aspect_ratio: 1:1 没加引号，YAML 会把它当成六十进制数字读成 61。
   比值、时间这类值要写成 \"1:1\""
]
```

为什么要有这一层：同一个坑，**装了 PyYAML 和没装可能是两种结果**（PyYAML 严格按
YAML 1.1，内置的回退解析器得手工对齐）。所以本项目做了一件比较极端的事 ——
把"什么算数字"的判定直接照抄 PyYAML 的正则，并且在自检里**逐字节比对两条解析路径
解析整份配置的结果**。这个比对抓出过真 bug，详见 `selftest.py` 里的"解析器一致性"一节。

### 三个字段的实际含义

**`priority` 决定档位，`weight` 决定同档内的概率。**

优先级 1 的模型**永远**压过优先级 2 —— 只要有档位 1 的模型健康可用，就轮不到档位 2。
只有在同一个 priority 内，才按 weight 加权随机。

实例（默认配置里的图片池）：

| 模型 | priority | weight | 实际首选概率 |
|---|---|---|---|
| jimeng-seedream | 1 | 50 | 62.5% |
| wanx-2.5 | 1 | 30 | 37.5% |
| builtin-imagen | 9 | 100 | 仅当上面两个都不可用时 |

实测 4000 次抽样：jimeng 62.6% / wanx 37.4%，与理论值吻合。

所以：
- 想让 A 永远优先、B 只是备胎 → 给它们**不同**的 priority；
- 想让 A、B 按 5:3 分摊流量 → 给它们**相同**的 priority，weight 设 50 和 30。

### 三种 `strategy`

| 取值 | 行为 | 适合 |
|---|---|---|
| `priority_then_weight` | 先分档，同档内加权随机（默认） | 图片等单价低、想分摊用量的场景 |
| `weight_only` | 忽略 priority，全部按 weight 加权随机 | 就是想让多个模型纯分流 |
| `fallback_chain` | 严格按 (priority, weight) 排序，不随机 | 视频这类贵且要求结果可预测的场景 |

### 密钥放哪

优先级从高到低：

1. 环境变量 —— `api_key_env` 指定的那个变量名（**推荐**）
2. `config/secrets.yaml` 的 `keys.<环境变量名>`
3. `config/secrets.web.yaml` 的 `keys.<环境变量名>` —— 配置页面写的那份
4. `config/secrets.yaml` / `secrets.web.yaml` 的 `keys.<模型 id>`
5. `models.yaml` 条目里的 `api_key` 内联字段（不推荐，容易提交进版本库）

从 `config/secrets.example.yaml` 复制一份改成 `secrets.yaml`；`.gitignore` 已经把它和
`secrets.web.yaml` 都排除掉了。

> 两份密钥文件是叠加关系，后一份（`secrets.web.yaml`，配置页面生成）覆盖前一份。
> 里面的**空值不会覆盖非空值**，所以示例文件里的空占位不会把真实密钥抹掉。

可灵需要 AK/SK 一对密钥，用冒号写在一个变量里：`KLING_KEYS="你的AK:你的SK"`。

### 让文本模型真正"看见"产物

`defaults.caption.mode`：

| 取值 | 行为 |
|---|---|
| `off` | 只给文件路径 |
| `metadata` | 额外给宽高/时长/大小（默认，零成本） |
| `vlm` | **再调一次视觉模型产出一句话描述**，填 `caption.model` 与 `caption.api_key_env` 即可 |

为什么建议开 `vlm`：文本模型看不见图片，如果只拿到一个路径，它要么继续卡着、
要么只好瞎猜画面。给一句真实描述，它就能接着写文案、做汇报、往下推理。
视频用 ffmpeg 抽一帧再送视觉模型（环境里有 ffmpeg 才生效，没有就只给元数据）。

## 失败降级与熔断

- 一次 `generate` 会按候选序列依次尝试（默认最多 2 个，`defaults.max_attempts`）。
- **缺密钥的模型会被跳过，且不占用尝试次数** —— 否则你还没配 Key，重试预算就被空转吃光。
- `native` 兜底条目不占尝试次数，它永远是最后那个"总能落地"的出口。
- 某模型连续失败达 `failure_threshold` 次后进入 `cooldown_seconds` 冷却，
  冷却期内不再被选中；成功后失败计数清零。状态在 `state/health.json`。
- 全部模型都在冷却时不会直接失败，而是挑**最早恢复**的那个继续试（宁可慢也别断）。

想清空熔断状态：`python3 scripts/media_router.py health --reset`

## 接入一个文档里没有的平台

不用写代码。用 `provider: generic_http`，把接口的提交与轮询填成 YAML：

```yaml
- id: my-custom-api
  provider: generic_http
  model: your-model-name
  priority: 3
  weight: 10
  supports: [text2img]
  options:
    auth:
      type: bearer            # bearer | key | token | header | query | none
    submit:
      url: https://api.example.com/v1/images
      method: POST
      body:
        model: "{{model}}"
        prompt: "{{prompt}}"
      task_id_path: data.task_id     # 从提交响应里取任务 id 的路径
    poll:
      url: https://api.example.com/v1/tasks/{{task_id}}
      method: GET
      status_path: data.status
      success: [succeeded, success, done]
      failure: [failed, error]
      result_path: data.images       # 产物数组的路径
      result_url_field: url          # 数组元素里哪个字段是下载地址
```

如果是**同步**接口（提交响应里直接就是结果），加一行 `sync: true`，
再去掉 `poll` 段即可。

> ⚠️ YAML 里凡是带 `{{占位符}}` 的值都必须加引号，写成 `"{{prompt}}"`。

## 目录结构

```
media-router/
├── SKILL.md                  # 给 agent 的执行规范（核心）
├── README.md                 # 本文件
├── assets/config.html        # 网页配置界面（单文件，零依赖）
├── config/
│   ├── models.yaml           # ★ 你维护的模型池
│   ├── models.web.yaml       # 配置页面生成的厂商/模型（叠加在上面的前面那份之上）
│   └── secrets.example.yaml  # 密钥样例，复制成 secrets.yaml
├── scripts/
│   ├── media_router.py       # CLI 入口
│   ├── selftest.py           # 无需 Key 的路由/生成端到端自检
│   ├── selftest_web.py       # 无需 Key 的网页配置端到端自检
│   └── mrouter/
│       ├── config.py         # 配置加载、双层叠加与密钥解析
│       ├── selector.py       # 权重 / 优先级排序算法
│       ├── health.py         # 熔断与冷却
│       ├── transport.py      # HTTP 传输（纯标准库）
│       ├── adapters.py       # 各 provider 适配器
│       ├── caption.py        # 元数据与视觉描述
│       ├── catalog.py        # 平台目录（中文说明 + 默认地址 + 只读校验端点）
│       ├── probe.py          # 连通性探测（轻量 / 深度两级）
│       ├── store.py          # 配置页面的持久层（叠加层读写）
│       ├── webserver.py      # 本机网页配置服务
│       ├── cli.py            # 命令行
│       └── miniyaml.py       # YAML 子集解析与序列化（没装 pyyaml 时的回退）
├── state/health.json         # 运行时熔断状态（自动生成）
├── outputs/                  # 产物默认落盘位置（自动生成）
└── references/providers.md   # 各平台接入参数对照
```

## 常见问题

**Q：装了 PyYAML 和不装，行为会不一样吗？**
不会 —— 而且这一点是被自检**逐字节验证**过的。有 PyYAML 就用它，没有就用内置的
`miniyaml`（覆盖本 skill 用到的语法子集，并且带序列化，配置页面写文件也用它 ——
这样同一份配置在哪台机器上写出来格式都一样）。

为此做过两件事：

1. **"什么算数字"的判定直接照抄 PyYAML 的 resolver 正则**，而不是用 Python 的
   `int()` / `float()`。这两套规则不一样 —— 差别最大的一处就是 YAML 1.1 的
   六十进制：`1:1` 是整数 61。
2. **自检里逐字节比对两条解析路径**对整份 `models.yaml` 的解析结果，并断言
   `dumps → loads` 往返不失真。

> 这个比对抓到过一个真 bug：默认配置里的 `aspect_ratio: 1:1` 没加引号，
> 装了 PyYAML 的机器读到 **61**，没装的读到 `"1:1"` —— 同一份配置两个结果。
> 已修复，并在配置文件顶部写清了引号规则。

已知且**有意保留**的两处差异：日期形状的值，miniyaml 保持字符串、PyYAML 读成
`date` 对象；对明显写错的输入（如 `{a:1}` 少个空格），miniyaml 直接报错，
PyYAML 会静默产出一个没意义的结果 —— 报错比静默错好。这两种情况都会被上面那层
体检（lint）提前告知，不会让你自己撞上。

**Q：网页配置会不会把我的 `models.yaml` 改乱？**
不会。页面只写 `config/models.web.yaml`（厂商/模型）和 `config/secrets.web.yaml`（密钥），
这两份是**叠加层**，加载时才盖在你手写的文件之上。你写的注释、顺序、条目一个都不会动。
真的想清掉页面的改动，删掉那两个文件即可。

**Q：为什么第二个同平台厂商的密钥变量名后面多了 `_2`？**
因为一个环境变量名只能有一个值。你在页面里加第二个火山账号时，如果两家的 Key 都记到
`ARK_API_KEY`，后填的会把先填的覆盖掉。所以程序给第二个自动换成 `ARK_API_KEY_2`。
（注意分隔符必须是下划线 —— 连字符在 shell 和 `.env` 里都是非法的环境变量名。）

**Q：`generate` 卡很久是正常的吗？**
视频正常，1~3 分钟。异步任务在提交后要轮询。**不要**在外面套 sleep 重试，
脚本内部已经处理完了。

**Q：能一次生成多张吗？**
`--count N`。但生图生视频是计费的，批量前先跟用户确认。

**Q：产物存在哪？**
默认 `<skill>/outputs/`，文件名形如 `image_jimeng-seedream_20260916_221630412_01.png`
（毫秒级时间戳 + 序号，不会互相覆盖）。用 `--output-dir` 可改写；在 `models.yaml` 的
`defaults.output_dir` 改默认值。

**Q：能不能不让 agent 自己动我的配置？**
`SKILL.md` 里已经写明"不要擅自改动用户的模型池配置或权重，需要调整时先问"。
你也可以把 `models.yaml` 设成只读来硬性约束。
另外用 `MEDIA_ROUTER_CONFIG` / `MEDIA_ROUTER_SECRETS` 指向自己的配置文件时，
配置页面会自动进入只读模式，不会写入。

---

## 密钥与安全

- 密钥**只**存在 `config/secrets.yaml`（手写）或 `config/secrets.web.yaml`（网页配置写入），
  两者都已在 `.gitignore` 里，不会进入版本库。仓库里的 `config/secrets.example.yaml`
  是空白模板，复制成 `secrets.yaml` 填入真实值即可。
- `config/models.yaml` / `config/models.web.yaml` 只含环境变量名（如 `api_key_env: ARK_API_KEY`），
  不含密钥本身，可以安全提交。
- `state/`（熔断状态）和 `outputs/`（生成产物）同样不入库。
- 开发自查：改动配置相关代码后，可以 grep 一遍密钥特征确认没有误提交：
  `grep -rInE "sk-[A-Za-z0-9_-]{10,}" --include="*.yaml" config/` 应当无输出。

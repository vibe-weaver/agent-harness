# Agent Harness

> 自包含的 AI Agent 办公平台：对话、执行代码、生成文档、调用技能、积累长期记忆。
> **下载下来，配一个 LLM API Key，就能跑。** 不需要 Docker、不需要构建前端。

---

## 5 分钟上手

```
准备 MySQL → 复制 .env 填 2 行 → 启动后端 → 登录后台填 API Key → 开聊
   ①            ②              ③           ④            ⑤
```

<details>
<summary><b>① 你需要先装好这些</b>（点击展开）</summary>

| 软件 | 版本 | 用途 |
|---|---|---|
| Python | 3.11+ | 跑后端 |
| MySQL | 8.0+ | 存数据 |
| Node.js | 18+ | **可选**，只有想跑前台开发态才需要 |
| 一个 LLM API Key | — | DeepSeek / OpenAI / Anthropic / 任何兼容厂商 |

</details>

### ② 建数据库

```sql
CREATE DATABASE agent_harness DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

### ③ 配置（只需填 2 行）

```bash
cd backend
cp .env.example .env
```

打开 `.env`，填这两项：

```ini
DB_PASSWORD=你的数据库密码
JWT_SECRET=随便一串随机字符
```

> JWT_SECRET 生成：`python -c "import secrets; print(secrets.token_urlsafe(48))"`

### ④ 启动

```bash
cd backend
python -m venv env
source env/bin/activate        # Windows: env\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次启动会自动建表 + **创建管理员**，密码打印在日志里：

```
============================================================
  已创建初始管理员
  登录账号：admin@local
  初始密码：xxxxxxxxxxxx
============================================================
```

### ⑤ 登录并填 API Key（3 步）

打开 **http://localhost:8000/admin**，用上面的账号登录，然后：

| 步骤 | 页面 | 操作 |
|---|---|---|
| 1 | `/admin/ai` → 厂商管理 | 新增厂商：填 Base URL + API Key |
| 2 | `/admin/ai` → 对话模型 | 新增模型：如 `deepseek-chat`（可点「🔍 自动发现」列出全部可用模型） |

完成。在管理端就能直接测试对话；要更好的界面，启动前台（见下方折叠块）后打开 **http://localhost:5173**。

<details>
<summary><b>想跑前台对话页面？（npm run dev）</b></summary>

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

生产部署不用跑它：`npm run build` 出的 `frontend/dist/` 会被后端自动挂载到根路径。

</details>

<details>
<summary><b>卡住了？看这里</b></summary>

| 症状 | 原因与解法 |
|---|---|
| 启动报数据库连接失败 | 检查 `.env` 的 DB_PASSWORD / MySQL 是否在跑（`mysql -u root -p` 能进吗） |
| pip 装依赖很慢 | 换国内源：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 对话报「无可用模型」 | API Key 没配或模型没启用 —— 回 `/admin/ai` 检查第 ⑤ 步 |
| 登录 401 | 密码在**启动日志**里，只打印一次；忘了就删库里的 users 表重启，或设 `INITIAL_ADMIN_PASSWORD` 后重建 |
| 5173 打不开 | 前台没启动，先 `npm run dev`；或直接用 8000 的管理端 |

</details>

---

## 这个项目能干什么

| 能力 | 说明 |
|---|---|
| **AI 对话** | 流式输出、多轮工具调用（Agent 循环）、模型降级、上下文自动压缩 |
| **代码执行** | 模型写代码真的跑：算数据、画图表、读写工作区文件（共 14 个内置工具） |
| **文档生成** | PDF / Word / Excel / Markdown |
| **技能系统** | 放一个 SKILL.md 目录就能给 Agent 添能力，可热插拔 |
| **长期记忆** | 对话后自动提取你的偏好，下次会话直接生效 |
| **工作区** | 每用户独立文件空间，版本快照 + 回收站 |
| **多厂商** | OpenAI 与 Anthropic 协议都支持 |

---

## 配置速查

完整说明在 `backend/.env.example`（每项都有中文注释）。常用的：

| 变量 | 说明 |
|---|---|
| `DB_*` | MySQL 连接信息 |
| `JWT_SECRET` | **生产必改**，默认值 = 任何人可伪造登录令牌 |
| `QQ_EMAIL` / `QQ_EMAIL_AUTH_CODE` | **留空 = 关闭注册**，只有管理员能用。想开放注册才配（QQ 邮箱要申请授权码，不是登录密码） |
| `INITIAL_ADMIN_PASSWORD` | 想自己指定管理员密码就填这里，否则随机生成打印一次 |
| `SANDBOX_BACKEND` | 代码执行的隔离模式，见下节 |

---

## 代码执行的安全模式

模型写的代码本质是不可信输入。两种执行模式：

- **`local`（默认）**：本机子进程 + 模块白名单 + 路径边界。**自用够用，零配置。**
- **`docker`（可选）**：一次性容器执行 —— 断网、只读、无特权、内存/进程限额。
  要对公网开放、允许多人用时切到这个。支持单机部署，
  步骤见 [`sandbox-executor/DEPLOY.md`](sandbox-executor/DEPLOY.md)。

> 设计取舍：docker 模式下执行器挂了会**直接报错**，不会悄悄退回 local ——
> 静默降级等于故障时撤掉隔离，宁可响亮地失败。

---

## 技能系统

技能 = `backend/data/skills/<名称>/` 下放一个 `SKILL.md`：

```markdown
---
name: my-skill
description: 这个技能做什么、什么时候该用它（模型会读到这句）
---

# 给模型的操作说明写在这里
```

两种添加方式：管理端 `/admin/skills` 传 ZIP / 从 Git 导入（自动注册），
或手动拷目录后到管理端点「扫描注册」。

> ⚠️ 只拷目录不注册不生效 —— 技能需要写进数据库。

---

## 架构与技术栈

```
浏览器 ─┬─ /          前台 SPA（React）
        └─ /admin/**  管理后台（服务端渲染 HTML，免构建）
                     │
              FastAPI 后端
        Agent 引擎 / 14 个工具 / 记忆 / 技能
                     │
        ┌────────────┴────────────┐
     MySQL 8                sandbox-executor
   （17 张表）            （可选：一次性 Docker 容器）
```

- 后端：Python 3.11+ · FastAPI · SQLAlchemy · MySQL 8
- 前端：React 19 · Vite · TanStack Router · Tailwind
- 目录速览：`backend/app/services/` 是核心（Agent 引擎、工具、记忆），
  `backend/app/admin_pages/` 是管理界面，改它不用构建前端。

<details>
<summary><b>改这个项目前值得知道的取舍</b></summary>

- 管理端是服务端渲染 HTML，改完**重启后端即生效**，无前端构建
- 对话引擎**每次请求都读数据库配置**，改配置不用重启
- 工具描述与系统提示词共同定义 Agent 能力边界，要一起改
- 模型降级链：主模型失败自动换备选，避免单点中断

</details>

---

## 许可证

MIT License —— 见 [LICENSE](LICENSE)。

# sandbox-executor 部署指南

`run_python` 的隔离执行端点。收到后端（下称 A）推来的脚本文本后，`docker run`
一个一次性容器执行，stdout/stderr 以 NDJSON 流回传。

```
A（后端）──HTTP(令牌)──► sandbox-executor ──docker run──► 一次性容器
                                │
                                └── 审计日志 exec.jsonl（只记 sha256 指纹，不记脚本正文）
```

容器的硬化参数（`--network none`、`--read-only`、`--cap-drop ALL`、cgroup 内存/
进程数限制等）在 `executor.py:build_docker_cmd` 里逐条写死了，不是配置项 ——
它们是安全语义，改任何一个都应该走代码评审而不是改 `.env`。

## 两种部署形态

| | 单机模式 | 双机模式 |
|---|---|---|
| 架构 | A 和 executor 在同一台服务器 | executor 单独一台（B 机） |
| 工作区共享 | bind mount 本地目录 | NFS 导出 |
| 挂载校验 | **必须关闭**（`SANDBOX_REQUIRE_NFS=0`） | 默认开启（推荐） |
| 隔离强度 | 容器层隔离完整；宿主层共享一台机器 | 执行机被攻破也拿不到数据库和密钥 |
| 适合 | 自用 / 小规模 | 对公网开放且允许多人使用 |

单机模式下执行器仍比 `local` 后端安全得多：用户代码跑在断网、只读、无特权的
容器里，而不是后端同机的子进程。真正需要双机的是"执行机被完全攻破也不泄密"
这个级别的威胁模型。

---

## 一、构建沙箱镜像（两种模式通用）

```bash
cd sandbox-executor

# 镜像 tag 由 Dockerfile + requirements 的内容哈希决定（改任一文件 tag 即变，
# 强制镜像内容与代码一致，杜绝"跑的不是审计过的那个镜像"）
TAG=$(cat Dockerfile requirements-sandbox.txt | sha256sum | cut -c1-12)

docker build --platform linux/amd64 -t blog-sandbox:$TAG .

# 构建末尾有冒烟测试：全量 import 依赖 + 断言中文字体路径。
# 失败会在 build 阶段炸掉，不会等到线上第一次 run_python 才炸。
```

双机模式：镜像在 A 上构建，再传到 B（B 内存小，别在 B 上跑 pip）：

```bash
docker save blog-sandbox:$TAG | gzip | ssh <B> 'gunzip | docker load'
```

---

## 二、单机模式（后端与执行器同机）

### 1. 准备目录与用户

```bash
# 执行器专用用户（能读 docker.sock 即视为有 root 等效权限，所以
# User=sandboxexec 挡的是"其余一切"，docker 这块攻击面由固定镜像+固定参数的设计收缩）
useradd -r sandboxexec
usermod -aG docker sandboxexec

# 部署目录（按你的实际路径调整，同步改 service 文件里的三个路径）
install -d -o root -g sandboxexec -m 750 /opt/sandbox-executor
cp executor.py exec_metrics.py healthz-probe.sh /opt/sandbox-executor/

cd /opt/sandbox-executor
python3 -m venv env
env/bin/pip install -r requirements-executor.txt

# 工作区与技能库的宿主路径（bind mount 源）
# A 侧工作区默认在 backend/data/workspaces/，技能库在 backend/data/skills/
# 下面用 /opt/agent-harness/ 指代你的后端部署位置
```

### 2. 配置环境变量

```bash
install -d -m 750 /etc/sandbox-executor
install -m 640 /dev/null /etc/sandbox-executor/executor.env
chown root:sandboxexec /etc/sandbox-executor/executor.env

cat >> /etc/sandbox-executor/executor.env <<EOF
SANDBOX_TOKEN=$(openssl rand -hex 32)
SANDBOX_IMAGE=blog-sandbox:<上面算出的 TAG>
SANDBOX_NFS_WS_ROOT=/opt/agent-harness/backend/data/workspaces
SANDBOX_NFS_SKILLS_ROOT=/opt/agent-harness/backend/data/skills
SANDBOX_REQUIRE_NFS=0
SANDBOX_LISTEN_HOST=127.0.0.1
EOF
```

单机模式的三处关键差异：

- `SANDBOX_REQUIRE_NFS=0` —— 本地目录不是 NFS 挂载，三层挂载校验
  （mountpoint / fstype / 哨兵新鲜度）必然不过。关掉它意味着放弃"NFS 静默失效
  时拒绝服务"这道防线；bind mount 指向本地目录时该失效模式不存在，可接受。
  **executor 启动时会对着 stderr 打一条大字警告，这是刻意的，确认你懂再忽略。**
- `SANDBOX_LISTEN_HOST=127.0.0.1` —— 同机时只听回环即可，无需防火墙规则。
- 哨兵文件 cron 不需要配（那是 NFS 新鲜度校验的一部分，已关闭）。

### 3. systemd

service 文件按 `/opt/sandbox-executor` + `/opt/agent-harness` 的路径写，
路径不同就先改这几行再安装：

- `Documentation=` `WorkingDirectory=` `ExecStart=`（第 3、33、34 行）
- `RequiresMountsFor=/mnt/blog-ws /mnt/blog-skills` —— **单机模式删掉这行**
  （ fstab 里没有这些 NFS 挂载，不删会卡在等挂载）

```bash
cp sandbox-executor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sandbox-executor
journalctl -u sandbox-executor -n 20   # 应看到镜像/挂载点回显与监听端口
```

### 4. A 侧 `.env` 切换

```bash
SANDBOX_BACKEND=docker
SANDBOX_EXECUTOR_URL=http://127.0.0.1:8787
SANDBOX_EXECUTOR_TOKEN=<和 B... 和本机 executor.env 里同一个 token>
SANDBOX_MAX_CONCURRENT=2   # ≤ executor 的 SANDBOX_MAX_CONCURRENT
```

重启后端。**executor 不可达时不会回落 local**（fail closed）——配置错了
`run_python` 会明确报错，这是刻意的：回落等于故障时静默撤掉隔离。

### 5. 验证

```bash
curl -s http://127.0.0.1:8787/healthz -H "X-Sandbox-Token: <token>"
# "ready": true，且 image 与你构建的 tag 一致

# 然后在对话里让模型跑一段 print("hello")，观察 exec.jsonl 有新记录：
tail /var/log/sandbox-executor/exec.jsonl
```

---

## 三、双机模式（独立执行机）

### 1. A 侧导出 NFS

```bash
# /etc/exports —— 导出工作区(rw)与技能库(ro)，root_squash
/opt/agent-harness/backend/data/workspaces  <B内网IP>(rw,root_squash,sync,no_subtree_check)
/opt/agent-harness/backend/data/skills      <B内网IP>(ro,root_squash,sync,no_subtree_check)

exportfs -ra
```

哨兵文件 cron（A 侧）—— 挂载新鲜度校验的信号源：

```cron
* * * * * date +\%s > /opt/agent-harness/backend/data/workspaces/.sandbox_nfs_ok
```

### 2. B 机安装 executor

步骤同单机模式，差异点：

- `/etc/sandbox-executor/executor.env`：

```bash
SANDBOX_TOKEN=<openssl rand -hex 32>
SANDBOX_IMAGE=blog-sandbox:<TAG>
SANDBOX_NFS_WS_ROOT=/mnt/blog-ws       # B 侧的 NFS 挂载点
SANDBOX_NFS_SKILLS_ROOT=/mnt/blog-skills
# SANDBOX_REQUIRE_NFS 保持默认 1 —— NFS 三层校验是这套方案最重要的一道防线
# SANDBOX_LISTEN_HOST 保持默认 0.0.0.0 —— 访问控制交给防火墙：
```

- fstab 加两条 NFS hard 挂载（`/mnt/blog-ws`、`/mnt/blog-skills`）。
- **UID 对齐**：容器内固定 `1000:1000`，NFS `root_squash` 下 A 侧工作区属主
  也必须是 uid 1000，对不上就写不动（表现为"执行成功但工作区里没有文件"）。
- **防火墙**：只放行 A 的内网 IP ——

```bash
firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source \
  address=<A内网IP>/32 port port=8787 protocol=tcp accept'
firewall-cmd --reload
```

- 内存与并发按 B 的规格调：默认值（并发 2、每容器 384MB）是按 2 核 2GB 调的。

### 3. A 侧 `.env`

```bash
SANDBOX_BACKEND=docker
SANDBOX_EXECUTOR_URL=http://<B内网IP>:8787
SANDBOX_EXECUTOR_TOKEN=<同 B>
```

### 4. 验证

同单机模式第 5 步（healthz 的 `mounts` 字段应显示两个挂载点均 nfs、fresh）。
另外故意把 A 侧 cron 停 3 分钟，确认 B 的 `/execute` 开始返回 503 ——
挂载失效从"静默数据丢失"变成"响亮拒绝服务"，这正是该校验存在的意义。

---

## 四、运维配套（两种模式均建议装）

| 单元 | 作用 |
|---|---|
| `sandbox-prune.{service,timer}` | 清理退出的容器（拿完死因后 docker rm） |
| `sandbox-image-prune.{service,timer}` + `.sh` | 清理悬空镜像 |
| `sandbox-exec-metrics.{service,timer}` + `exec_metrics.py` | 从 exec.jsonl 汇总指标 |
| `sandbox-healthz-probe.{service,timer}` + `healthz-probe.sh` | 探测 executor 存活 |
| `sandbox-executor.logrotate` | 审计日志轮转 |

安装方式统一为拷到 `/etc/systemd/system/` + `daemon-reload` + `enable --now`；
timer 类先确认 service 里的路径与本机一致。

## 五、常见故障速查

| 症状 | 先看 |
|---|---|
| A 侧 run_python 报"执行器不可达" | B 起没起、防火墙、`SANDBOX_EXECUTOR_URL` 拼写 |
| 503 not_ready | healthz 的 mounts 字段：NFS 掉了还是哨兵过期（A 的 cron 停了？A 时钟慢了？） |
| 容器起不来（docker_error） | 镜像 tag 对不对（`docker images \| grep blog-sandbox`）、磁盘满 |
| 执行成功但工作区没文件 | UID 对不上（双机）/ 挂载源路径配错（单机） |
| 429 busy | B 的并发槽满，`SANDBOX_MAX_CONCURRENT` 与 A 侧 `SANDBOX_MAX_CONCURRENT` 的关系 |
| matplotlib 相关 stderr 警告进结果 | tmpfs 没挂上（docker cmd 里的 `--tmpfs /tmp` 是功能必需项） |

审计与令牌轮换的细节（无中断三步走）见 `sandbox-executor.env.example` 顶部注释 ——
那份文件本身就是完整的参数手册。

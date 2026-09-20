# Gunicorn 配置文件
# 文档: https://docs.gunicorn.org/en/stable/settings.html

import os

# ── 基本配置 ──

# 监听地址和端口
bind = "0.0.0.0:8000"

# 工作进程数
# ⚠️ 必须为 1：验证码存储在进程内存中，多 worker 会导致验证码跨进程不可见
workers = 1

# 线程数（弥补单 worker 的并发能力）
# threads = 4

# 使用 Uvicorn 的 ASGI Worker（FastAPI 必需）
worker_class = "uvicorn.workers.UvicornWorker"

# 预加载应用（节省内存，加快 worker 启动）
preload_app = True

# ── 超时与重启 ──

# Worker 超时（秒）—— 上传大文件或慢请求时适当调大
# 注意：Agent 对话是 SSE 长连接（含工具审批等待、多轮 LLM 请求），
# SSE 持续 heartbeat 输出不触发 gunicorn 超时；此处调大作为兜底
timeout = 600

# 优雅关闭超时
graceful_timeout = 30

# Worker 处理 max_requests 后重启（防止内存泄漏）
# 设为 0 禁用，避免重启时丢失内存中的验证码
max_requests = 0
max_requests_jitter = 0  # 随机抖动，避免所有 worker 同时重启

# ── 日志 ──

# 访问日志
accesslog = "logs/access.log"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

# 错误日志
errorlog = "logs/error.log"

# 日志级别
loglevel = "info"

# ── 进程命名 ──

# 进程名（方便 ps/top 识别）
proc_name = "blog"

# ── 其他 ──

# 临时文件上传目录
worker_tmp_dir = "/dev/shm"

# 文件描述符数量（并发上传图片时可能需要）
worker_connections = 1000

# 确保日志目录存在
os.makedirs("logs", exist_ok=True)

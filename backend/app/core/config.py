from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# 计算 .env 文件路径：config.py 在 app/core/ 下，向上 3 级到 backend/
_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _CONFIG_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",  # 允许 .env 中存在未定义的变量
    )

    APP_NAME: str = "Agent Harness"
    DEBUG: bool = False

    # ── 数据库（MySQL 8）──
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_NAME: str = "agent_harness"

    # ── 认证 ──
    # ⚠️ 生产环境必须改：留默认值等于任何人都能伪造登录令牌。
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    # 登录凭证有效期（分钟）。默认 7 天：太短会让用户隔几天回来就"登录过期"，
    # 表现为"点发送没反应、只弹登录窗"，很难自己判断出是凭证过期。
    JWT_EXPIRE_MINUTES: int = 10080

    # ── CORS ──
    # 前台是独立 SPA，与后端不同源时需要在这里放行（逗号分隔）。
    # 开发时默认放行 Vite 的两个常用地址；生产建议用 Nginx 同域反代，就不需要它。
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ── 邮箱注册（可选）──
    # ⚠️ 留空 = 关闭邮箱注册（注册接口会明确报"未配置邮件服务"）。
    # 此时用下面的初始管理员登录即可，不影响其他功能。
    # 填了才能发注册验证码；QQ 邮箱需要在网页版设置里开启 SMTP 并生成「授权码」，
    # 不是登录密码。
    QQ_EMAIL: str = ""
    QQ_EMAIL_AUTH_CODE: str = ""

    # ── 首次启动自动创建的管理员 ──
    # 仅在 users 表为空时生效（即全新数据库的第一次启动）。
    # 密码留空 → 随机生成一个，并打印到启动日志里（只打印这一次）。
    # 这样即使完全不配 SMTP，装完也能立刻登录进去。
    INITIAL_ADMIN_USERNAME: str = "admin"
    INITIAL_ADMIN_PASSWORD: str = ""
    INITIAL_ADMIN_EMAIL: str = ""

    # ── 沙箱执行后端 ──
    # local  = 在后端本机以子进程执行（默认，单机即可跑通）
    # docker = 交给独立的 sandbox-executor 在一次性容器里执行（见 docs/sandbox.md）
    #
    # 默认 local：切换是**显式动作**，于是"改一个环境变量 + 重启"天然就是回滚开关。
    # ⚠️ docker 后端不可达时**不回落 local**（fail closed）——回落等于在故障时把隔离
    # 静默撤掉，用户看到"功能正常"，实际代码正以高权限跑在 .env 和数据库旁边。
    # 宁可这一轮工具调用明确失败：一个响亮的失败好过一个安静的降级。
    SANDBOX_BACKEND: str = "local"
    SANDBOX_EXECUTOR_URL: str = ""
    SANDBOX_EXECUTOR_TOKEN: str = ""
    # 灰度名单，逗号分隔的 user_id。**语义与直觉相反：留空 = 全量走 docker**；
    # 非空 = 只有名单内的用户走 docker、其余仍走 local。计划的灰度步骤是
    # "先放一个测试账号 → 观察一周 → 清空名单转全量"，所以空必须表示全量，
    # 否则最后一步要改成枚举所有 user_id。仅在 SANDBOX_BACKEND=docker 时生效。
    SANDBOX_DOCKER_ALLOWLIST: str = ""
    # 0 = 按 CPU 核数自适应（max(2, min(4, 核数//2)))。
    # docker 后端下应显式设成 ≤ executor 的 SANDBOX_MAX_CONCURRENT：这个值是从**后端
    # 所在机器**的核数算出来的，恰好等于执行机容量纯属巧合，扩容后会把它打到 429。
    SANDBOX_MAX_CONCURRENT: int = 0
    # 只用于**文案**（工具描述、OOM 提示）。真实限制由容器的 --memory 决定，
    # 两边必须一致；后端不参与决策，改这里不会改变任何实际限额。
    SANDBOX_DOCKER_MEMORY_MB: int = 384

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            "?charset=utf8mb4"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 CORS_ORIGINS 拆成列表，顺带去掉空项与首尾空格。"""
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def email_registration_enabled(self) -> bool:
        """邮箱注册是否可用：两项都配齐才算，缺一不可。"""
        return bool(self.QQ_EMAIL and self.QQ_EMAIL_AUTH_CODE)


settings = Settings()

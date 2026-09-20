"""Agent Harness 后端入口 —— AI Agent 对话 / 生图 / 技能 / 记忆 / 工作区。

本项目是从原博客系统中拆出的**独立服务**：只保留 Agent 相关能力，
不含文章、分类、留言、时间线等博客业务。
"""
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .core.config import settings
from .core.database import SessionLocal, engine
from .models import Base
from .api.v1.auth import router as auth_router
from .api.v1.ai import router as ai_router
from .api.v1.admin_ai import router as admin_ai_router
from .api.v1.chat import router as chat_router
from .api.v1.admin_users import router as admin_users_router
from .api.v1.workspace import router as workspace_router
from .admin_pages.admin_ai_page import ADMIN_AI_PAGE
from .admin_pages.chat_admin_page import CHAT_ADMIN_PAGE
from .admin_pages.memory_admin_page import MEMORY_ADMIN_PAGE
from .admin_pages.skills_admin_page import SKILLS_ADMIN_PAGE
from .admin_pages.user_admin_page import USER_ADMIN_PAGE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    # 工作区目录预热：目录名从用户 id 迁移为 QQ 号（邮箱前缀）+ 填充进程内缓存
    try:
        from .core.database import SessionLocal as _WS_SessionLocal
        from .services.workspace_service import migrate_workspace_dirs
        _ws_db = _WS_SessionLocal()
        try:
            migrate_workspace_dirs(_ws_db)
        finally:
            _ws_db.close()
    except Exception as e:
        logger.warning(f"工作区目录预热失败（可忽略，回退 id 目录）: {e}")
    # 迁移：为 users 表补充 email 列和 is_admin 列
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "users" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("users")]
                if "email" not in cols:
                    conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(100) NULL"))
                    conn.commit()
                    logger.info("已迁移 users 表: 添加 email 列")
                # 补充 is_admin 列
                cols = [c["name"] for c in insp.get_columns("users")]
                if "is_admin" not in cols:
                    conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0"))
                    conn.commit()
                    logger.info("已迁移 users 表: 添加 is_admin 列")
                # 将第一个注册用户设为管理员（站主）
                admin_count = conn.execute(text("SELECT COUNT(*) FROM users WHERE is_admin = 1")).scalar()
                if admin_count == 0:
                    first_user = conn.execute(text("SELECT id FROM users ORDER BY id ASC LIMIT 1")).fetchone()
                    if first_user:
                        conn.execute(text("UPDATE users SET is_admin = 1 WHERE id = :uid"), {"uid": first_user[0]})
                        conn.commit()
                        logger.info(f"已将用户 #{first_user[0]} 设为管理员")
                # 补充 is_active 列（账号封禁，公网场景）
                cols = [c["name"] for c in insp.get_columns("users")]
                if "is_active" not in cols:
                    conn.execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1"))
                    conn.commit()
                    logger.info("已迁移 users 表: 添加 is_active 列")
    except Exception as e:
        logger.warning(f"迁移 users 列失败（可忽略）: {e}")
    # 迁移：为 workspace_files 表补充 last_accessed_at 列（TTL 基于最近访问）
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "workspace_files" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("workspace_files")]
                if "last_accessed_at" not in cols:
                    conn.execute(text("ALTER TABLE workspace_files ADD COLUMN last_accessed_at DATETIME NULL"))
                    conn.commit()
                    logger.info("已迁移 workspace_files 表: 添加 last_accessed_at 列")
    except Exception as e:
        logger.warning(f"迁移 workspace_files 列失败（可忽略）: {e}")
    # 迁移：为 chat_sessions 表补充 last_reasoning 列（模型有、旧表缺 → 创建会话 500）
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "chat_sessions" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("chat_sessions")]
                if "last_reasoning" not in cols:
                    conn.execute(text("ALTER TABLE chat_sessions ADD COLUMN last_reasoning TEXT NULL"))
                    conn.commit()
                    logger.info("已迁移 chat_sessions 表: 添加 last_reasoning 列")
    except Exception as e:
        logger.warning(f"迁移 chat_sessions 列失败（可忽略）: {e}")
    # 迁移：为 dsh_chat_models 表补充 supported_efforts 列（实测档位落库缓存）
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "dsh_chat_models" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("dsh_chat_models")]
                if "supported_efforts" not in cols:
                    # TEXT 列不能带 DEFAULT（MySQL 5.7 限制）；ORM 层 default="" 处理空值
                    conn.execute(text("ALTER TABLE dsh_chat_models ADD COLUMN supported_efforts TEXT NULL"))
                    conn.commit()
                    logger.info("已迁移 dsh_chat_models 表: 添加 supported_efforts 列")
    except Exception as e:
        logger.warning(f"迁移 dsh_chat_models 列失败（可忽略）: {e}")
    # 初始化 AI 频率限制默认行
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            tables = insp.get_table_names()

            # 确保旧表迁移（兼容历史版本）
            if "ai_providers" in tables:
                cols = [c["name"] for c in insp.get_columns("ai_providers")]
                # ── 迁移：添加 api_type 列（借鉴 DSH adapter.ts 多协议设计） ──
                if "api_type" not in cols:
                    conn.execute(text(
                        "ALTER TABLE ai_providers ADD COLUMN api_type VARCHAR(20) DEFAULT 'openai'"
                    ))
                    conn.commit()
                    logger.info("已迁移 ai_providers 表: 添加 api_type 列")
                if "chat_model" in cols and "image_model" not in cols:
                    conn.execute(text("ALTER TABLE ai_providers ADD COLUMN image_model VARCHAR(100) NULL"))
                    conn.execute(text("UPDATE ai_providers SET image_model = chat_model WHERE image_model IS NULL"))
                    conn.execute(text("ALTER TABLE ai_providers MODIFY COLUMN image_model VARCHAR(100) NOT NULL"))
                    conn.commit()
                # image_model 列从 NOT NULL 改为允许 NULL（模型池迁移后该列不再使用）
                if "image_model" in cols:
                    col_info = [c for c in insp.get_columns("ai_providers") if c["name"] == "image_model"]
                    if col_info and not col_info[0].get("nullable", True):
                        conn.execute(text("ALTER TABLE ai_providers MODIFY COLUMN image_model VARCHAR(100) NULL DEFAULT ''"))
                        conn.commit()
                if "is_default" not in cols:
                    conn.execute(text("ALTER TABLE ai_providers ADD COLUMN is_default BOOLEAN DEFAULT 0"))
                    conn.execute(text("UPDATE ai_providers SET is_default = is_default_image WHERE is_default_image = 1"))
                    conn.commit()

            if "ai_rate_limits" in tables:
                cols = [c["name"] for c in insp.get_columns("ai_rate_limits")]
                if "max_concurrent" not in cols:
                    conn.execute(text("ALTER TABLE ai_rate_limits ADD COLUMN max_concurrent INTEGER DEFAULT 3"))
                    conn.commit()
                if "chat_minute_limit" not in cols:
                    conn.execute(text("ALTER TABLE ai_rate_limits ADD COLUMN chat_minute_limit INTEGER DEFAULT 10"))
                    conn.commit()


            # 迁移：ai_image_records 补充 seed 列（生图可复现 / 审计）
            if "ai_image_records" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("ai_image_records")]
                if "seed" not in cols:
                    conn.execute(text("ALTER TABLE ai_image_records ADD COLUMN seed INTEGER NULL"))
                    conn.commit()
                    logger.info("已迁移 ai_image_records 表: 添加 seed 列")
                if "source" not in cols:
                    conn.execute(text("ALTER TABLE ai_image_records ADD COLUMN source VARCHAR(10) DEFAULT 'api'"))
                    conn.commit()
                    logger.info("已迁移 ai_image_records 表: 添加 source 列")

            # 迁移：user_memories 加 embedding 列（向量嵌入，JSON 序列化的 float 数组）
            # 用 MEDIUMTEXT(16MB)：1024 维 JSON 约 16~24KB 够，但更大模型（3072 维 ≈ 40~60KB）
            # 会逼近 TEXT(64KB) 上限。早期版本建的是 TEXT，此处升级。
            if "user_memories" in insp.get_table_names():
                emb_col = next(
                    (c for c in insp.get_columns("user_memories") if c["name"] == "embedding"),
                    None,
                )
                if emb_col is None:
                    conn.execute(text(
                        "ALTER TABLE user_memories ADD COLUMN embedding MEDIUMTEXT NULL"
                    ))
                    conn.commit()
                    logger.info("已迁移 user_memories 表: 添加 embedding 列（MEDIUMTEXT）")
                elif "MEDIUMTEXT" not in str(emb_col["type"]).upper():
                    conn.execute(text(
                        "ALTER TABLE user_memories MODIFY COLUMN embedding MEDIUMTEXT NULL"
                    ))
                    conn.commit()
                    logger.info("已迁移 user_memories 表: embedding 列升级为 MEDIUMTEXT")

            # 迁移：ai_image_tasks.image_url 从 TEXT(64KB) 升级为 MEDIUMTEXT(16MB)。
            # base64 参考图（1024px JPEG）常达 200~500KB，TEXT 会 Data too long → 创建任务失败。
            if "ai_image_tasks" in insp.get_table_names():
                img_col = next(
                    (c for c in insp.get_columns("ai_image_tasks") if c["name"] == "image_url"),
                    None,
                )
                if img_col is not None and "MEDIUMTEXT" not in str(img_col["type"]).upper():
                    conn.execute(text("ALTER TABLE ai_image_tasks MODIFY COLUMN image_url MEDIUMTEXT"))
                    conn.commit()
                    logger.info("已迁移 ai_image_tasks 表: image_url 升级为 MEDIUMTEXT")
    except Exception as e:
        logger.warning(f"迁移 AI 表失败（可忽略）: {e}")
    try:
        from .models import AIRateLimit
        db = SessionLocal()
        try:
            if not db.query(AIRateLimit).filter(AIRateLimit.id == 1).first():
                db.add(AIRateLimit(id=1))
                db.commit()
        finally:
            # 必须 close：查询异常时若不关闭，session 事务会持有表元数据锁
            # （曾导致后续 ALTER/SELECT 全部排队卡死）
            db.close()
    except Exception as e:
        logger.warning(f"初始化 AI 频率限制失败（可忽略）: {e}")
    # 清理重启前残留的「运行中/排队中」生图任务：服务重启中断了后台 worker，
    # 这些任务会永远停在 running，前端轮询会永无终态。标记为失败即可让前端正常收尾。
    try:
        import datetime as _dt
        from .models import AiImageTask
        db = SessionLocal()
        try:
            stale = (
                db.query(AiImageTask)
                .filter(AiImageTask.status.in_(["queued", "running"]))
                .update(
                    {"status": "failed", "error": "服务重启，任务中断", "progress": "失败",
                     "finished_at": _dt.datetime.utcnow()},
                    synchronize_session=False,
                )
            )
            db.commit()
            if stale:
                logger.info(f"已清理 {stale} 个中断的生图任务（重启残留）")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"清理重启残留生图任务失败（可忽略）: {e}")
    # 迁移：为 dsh_configs 表补充 max_sessions_per_user / chat_daily_limit 列
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            # 防止 ALTER 无限等待元数据锁（泄漏事务持锁时启动会卡死数分钟）
            try:
                conn.execute(text("SET SESSION lock_wait_timeout = 30"))
            except Exception:
                pass
            insp = inspect(conn)
            if "dsh_configs" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("dsh_configs")]
                if "max_sessions_per_user" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_configs ADD COLUMN max_sessions_per_user INTEGER DEFAULT 5"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_configs 表: 添加 max_sessions_per_user 列")
                # 重新获取列列表
                cols = [c["name"] for c in insp.get_columns("dsh_configs")]
                if "chat_daily_limit" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_configs ADD COLUMN chat_daily_limit INTEGER DEFAULT 50"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_configs 表: 添加 chat_daily_limit 列")
                # 重新获取列列表
                cols = [c["name"] for c in insp.get_columns("dsh_configs")]
                if "max_files_per_message" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_configs ADD COLUMN max_files_per_message INTEGER DEFAULT 5"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_configs 表: 添加 max_files_per_message 列")
                # 迁移：添加 system_memory 列（类似 CLAUDE.md 的全局系统记忆）
                cols = [c["name"] for c in insp.get_columns("dsh_configs")]
                if "system_memory" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_configs ADD COLUMN system_memory TEXT NULL"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_configs 表: 添加 system_memory 列")
                # 迁移：Agent 办公参数列（B2 轮数/token 预算、B5 并发、A3 注入防御、B3 精简开关）
                # + 图片 detail 档位（图片视觉优化5）
                _extra_cols = [
                    ("agent_max_tool_rounds", "INTEGER DEFAULT 40"),
                    ("agent_max_output_tokens", "INTEGER DEFAULT 0"),
                    ("agent_concurrent_limit", "INTEGER DEFAULT 4"),
                    ("agent_per_user_concurrent", "INTEGER DEFAULT 1"),
                    ("agent_workspace_guard", "BOOLEAN DEFAULT 1"),
                    ("agent_compact_prompt", "BOOLEAN DEFAULT 1"),
                    ("agent_stable_prefix", "BOOLEAN DEFAULT 1"),
                    ("image_detail", "VARCHAR(10) DEFAULT 'auto'"),
                    # 记忆模块：embedding 模型配置（NULL = 未配置，功能降级关闭）
                    ("embedding_model_id", "INTEGER NULL"),
                ]
                for _col, _ddl in _extra_cols:
                    # 每次用全新 inspector 取列列表，避免反射缓存导致误判跳过
                    cols = [c["name"] for c in inspect(conn).get_columns("dsh_configs")]
                    if _col not in cols:
                        conn.execute(text(
                            f"ALTER TABLE dsh_configs ADD COLUMN {_col} {_ddl}"
                        ))
                        conn.commit()
                        logger.info(f"已迁移 dsh_configs 表: 添加 {_col} 列")
                # 语义变更迁移：embedding_model_id 原先指向 dsh_chat_models（复用对话模型），
                # 现已改为指向独立的 dsh_embedding_models。两张表 id 空间无关，旧值在新表里
                # 找不到对应记录 → 表现为"管理端显示已配置、实际检索却降级"，很难排查。
                # 因此把在新表中不存在对应记录的旧值清空，让管理员重新在 /admin/memory 添加。
                # 幂等：已正确配置（id 存在于新表）的行不会被清除，重复启动无副作用。
                if ("embedding_model_id" in [c["name"] for c in inspect(conn).get_columns("dsh_configs")]
                        and "dsh_embedding_models" in inspect(conn).get_table_names()):
                    _reset = conn.execute(text(
                        "UPDATE dsh_configs SET embedding_model_id = NULL "
                        "WHERE embedding_model_id IS NOT NULL "
                        "AND embedding_model_id NOT IN (SELECT id FROM dsh_embedding_models)"
                    ))
                    conn.commit()
                    if _reset.rowcount:
                        logger.warning(
                            f"记忆模块：已重置 {_reset.rowcount} 条失效的 embedding_model_id "
                            f"（原先复用对话模型，现已改为独立嵌入模型表）。"
                            f"请到「AI 记忆」页（/admin/memory）重新添加并选择嵌入模型。"
                        )
                # 迁移：dsh_embedding_models.is_verified（维度是否来自实测探针）。
                # create_all 只会建缺失的表，不会给已存在的表加列 —— 故这里显式补。
                if "dsh_embedding_models" in inspect(conn).get_table_names():
                    _emb_cols = [c["name"] for c in inspect(conn).get_columns("dsh_embedding_models")]
                    if "is_verified" not in _emb_cols:
                        conn.execute(text(
                            "ALTER TABLE dsh_embedding_models ADD COLUMN is_verified BOOLEAN DEFAULT 0"
                        ))
                        conn.commit()
                        # 一次性回填：本列上线前没有"手动填维度"这条路径，
                        # dimensions > 0 只可能来自探针实测 → 一律视为已验证。
                        # 必须与 ADD COLUMN 同一分支：放在外面天天跑的话，
                        # 会把管理员手动填写的维度错误地"洗白"成已验证。
                        _bf = conn.execute(text(
                            "UPDATE dsh_embedding_models SET is_verified = 1 WHERE dimensions > 0"
                        ))
                        conn.commit()
                        logger.info(
                            f"已迁移 dsh_embedding_models 表: 添加 is_verified 列"
                            f"（回填 {_bf.rowcount} 条历史记录为「已验证」）"
                        )
    except Exception as e:
        logger.warning(f"迁移 dsh_configs 列失败（可忽略）: {e}")
    # 初始化 DSH 对话配置默认行
    # 注意：必须在 dsh_configs 迁移之后执行 —— 模型类包含新增的 Agent 参数字段，
    # 若在迁移前查询而列尚未添加，会报 "Unknown column"，顺序调整后该 warning 消除。
    try:
        from .models import DshConfig
        db = SessionLocal()
        try:
            if not db.query(DshConfig).filter(DshConfig.id == 1).first():
                db.add(DshConfig(id=1))
                db.commit()
        finally:
            # 必须 close：查询异常时不关闭会泄漏连接并持有元数据锁
            db.close()
    except Exception as e:
        logger.warning(f"初始化 DSH 配置失败（可忽略）: {e}")
    # 迁移：为 dsh_chat_models 表补充 supports_vision 列
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "dsh_chat_models" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("dsh_chat_models")]
                if "supports_vision" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_chat_models ADD COLUMN supports_vision BOOLEAN DEFAULT 0"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_chat_models 表: 添加 supports_vision 列")
                # 迁移：添加 context_length 列（模型上下文窗口大小）
                if "context_length" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_chat_models ADD COLUMN context_length INTEGER DEFAULT 65536"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_chat_models 表: 添加 context_length 列")
                # 迁移：添加 reasoning_effort 列（模型推理等级）
                # 借鉴 DSH adapter.ts 的 REASONING_EFFORTS：每个模型独立配置推理等级
                cols = [c["name"] for c in insp.get_columns("dsh_chat_models")]
                if "reasoning_effort" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_chat_models ADD COLUMN reasoning_effort VARCHAR(20) DEFAULT 'off'"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_chat_models 表: 添加 reasoning_effort 列")
    except Exception as e:
        logger.warning(f"迁移 dsh_chat_models 列失败（可忽略）: {e}")
    # 迁移：为 dsh_skills 表补充 dir_path 列（支持目录形式 skill）
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "dsh_skills" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("dsh_skills")]
                if "dir_path" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_skills ADD COLUMN dir_path VARCHAR(500) DEFAULT ''"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_skills 表: 添加 dir_path 列")
    except Exception as e:
        logger.warning(f"迁移 dsh_skills 列失败（可忽略）: {e}")
    # 迁移：将 dsh_skills.description 从 VARCHAR(200) 改为 TEXT（支持长描述）
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "dsh_skills" in insp.get_table_names():
                cols = {c["name"]: c for c in insp.get_columns("dsh_skills")}
                if "description" in cols:
                    col_type = str(cols["description"]["type"])
                    if "TEXT" not in col_type.upper():
                        conn.execute(text(
                            "ALTER TABLE dsh_skills MODIFY COLUMN description TEXT NOT NULL"
                        ))
                        conn.commit()
                        logger.info("已迁移 dsh_skills 表: description 列改为 TEXT")
    except Exception as e:
        logger.warning(f"迁移 dsh_skills description 列失败（可忽略）: {e}")
    # 迁移：为 chat_sessions 表补充 last_reasoning 列（持久化推理过程）
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "chat_sessions" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("chat_sessions")]
                if "last_reasoning" not in cols:
                    conn.execute(text(
                        "ALTER TABLE chat_sessions ADD COLUMN last_reasoning TEXT NULL DEFAULT ''"
                    ))
                    conn.commit()
                    logger.info("已迁移 chat_sessions 表: 添加 last_reasoning 列")
    except Exception as e:
        logger.warning(f"迁移 chat_sessions last_reasoning 列失败（可忽略）: {e}")
    # 迁移：为 dsh_agent_tasks 表补充 tool_events 列（后台任务工具调用痕迹持久化）
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "dsh_agent_tasks" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("dsh_agent_tasks")]
                if "tool_events" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_agent_tasks ADD COLUMN tool_events TEXT NULL"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_agent_tasks 表: 添加 tool_events 列")
    except Exception as e:
        logger.warning(f"迁移 dsh_agent_tasks tool_events 列失败（可忽略）: {e}")
    # 迁移：为 dsh_agent_tasks 表补充运营统计列（#9）：
    # enable_tools 区分 Agent/纯聊天；model_switches 记录降级链触发
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "dsh_agent_tasks" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("dsh_agent_tasks")]
                if "enable_tools" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_agent_tasks ADD COLUMN enable_tools TINYINT(1) DEFAULT 0"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_agent_tasks 表: 添加 enable_tools 列")
                if "model_switches" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_agent_tasks ADD COLUMN model_switches TEXT NULL"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_agent_tasks 表: 添加 model_switches 列")
    except Exception as e:
        logger.warning(f"迁移 dsh_agent_tasks 统计列失败（可忽略）: {e}")
    # 迁移：为 dsh_agent_tasks 表补充 token 用量列（性能优化1：成本可审计）
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "dsh_agent_tasks" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("dsh_agent_tasks")]
                for _col in ("prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens"):
                    if _col not in cols:
                        conn.execute(text(
                            f"ALTER TABLE dsh_agent_tasks ADD COLUMN {_col} INTEGER NULL"
                        ))
                        conn.commit()
                        logger.info(f"已迁移 dsh_agent_tasks 表: 添加 {_col} 列")
    except Exception as e:
        logger.warning(f"迁移 dsh_agent_tasks token 列失败（可忽略）: {e}")
    # 迁移：为 dsh_skills 表补充 pack 列（技能包共享资源支持：pack 技能可访问包根下的共享资源）
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "dsh_skills" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("dsh_skills")]
                if "pack" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_skills ADD COLUMN pack VARCHAR(50) NULL DEFAULT ''"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_skills 表: 添加 pack 列")
    except Exception as e:
        logger.warning(f"迁移 dsh_skills pack 列失败（可忽略）: {e}")
    # 迁移：为 dsh_skills 表补充 category 列（前端技能面板分类下拉筛选）
    try:
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(conn)
            if "dsh_skills" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("dsh_skills")]
                if "category" not in cols:
                    conn.execute(text(
                        "ALTER TABLE dsh_skills ADD COLUMN category VARCHAR(50) NULL DEFAULT ''"
                    ))
                    conn.commit()
                    logger.info("已迁移 dsh_skills 表: 添加 category 列")
    except Exception as e:
        logger.warning(f"迁移 dsh_skills category 列失败（可忽略）: {e}")
    # ── 内置技能种子：缺失的内置技能按名补插（幂等，不覆盖/不删除已有技能；管理端可编辑/禁用）──
    try:
        with engine.connect() as conn:
            from sqlalchemy import text as _text
            _seed_skills = [
                ("translate",
                 "中英互译助手：将文本在中英文之间翻译，保留原文语气与格式。",
                 "## 翻译技能\n\n当用户请求翻译（中译英/英译中/多语言）时，遵循以下步骤：\n\n"
                 "1. 先确认源语言与目标语言。\n"
                 "2. 忠实翻译，保留原文语气、术语与格式（列表/标题/代码块）。\n"
                 "3. 专业术语（技术/法律/医疗）使用标准译法，必要时括号注明原文。\n"
                 "4. 翻译后附一句说明，如\"已按商务语气翻译\"。"),
                ("weekly-report",
                 "周报生成：根据用户的工作内容描述，生成结构化周报。",
                 "## 周报技能\n\n当用户请求生成周报时：\n\n"
                 "1. 让用户提供本周完成的工作要点（或根据对话上下文提取）。\n"
                 "2. 按结构输出：本周完成 / 进行中 / 下周计划 / 问题与风险。\n"
                 "3. 每条要点用一句话概括，量化成果（如\"完成 X 功能，覆盖 3 个场景\"）。\n"
                 "4. 语气正式简洁，适合直接提交。"),
                ("ppt-outline",
                 "PPT 大纲生成：将主题或文档扩展为结构化的演示文稿大纲。",
                 "## PPT 大纲技能\n\n当用户请求制作 PPT/演示文稿/大纲时：\n\n"
                 "1. 明确主题、受众与时长（如无则合理假设并说明）。\n"
                 "2. 输出 8-12 页结构：封面 / 目录 / 3-5 个主体章节（每节 1-2 页）/ 总结 / Q&A。\n"
                 "3. 每页给出标题 + 3-5 个要点（bullet），关键数据用数字。\n"
                 "4. 提供过渡句建议，让演示更连贯。"),
                ("email-polish",
                 "邮件润色：将口语化或粗糙的草稿改写为专业商务邮件。",
                 "## 邮件润色技能\n\n当用户请求写/润色邮件时：\n\n"
                 "1. 明确收件人关系（上级/客户/同事）决定语气。\n"
                 "2. 结构：主题行 / 问候 / 正文（目的先行，背景简洁，行动项明确）/ 结尾致谢 / 署名。\n"
                 "3. 删除口头禅与冗余，控制在一屏内。\n"
                 "4. 提供中文版；用户要求时附英文版。"),
            ]
            existing_names = {
                r[0] for r in conn.execute(_text("SELECT name FROM dsh_skills"))
            }
            seeds_to_add = [
                (n, d, c) for n, d, c in _seed_skills if n not in existing_names
            ]
            if seeds_to_add:
                for _name, _desc, _content in seeds_to_add:
                    conn.execute(
                        _text(
                            "INSERT INTO dsh_skills (name, description, content, dir_path, is_active, created_at) "
                            "VALUES (:n, :d, :c, '', 1, NOW())"
                        ),
                        {"n": _name, "d": _desc, "c": _content},
                    )
                conn.commit()
                logger.info(
                    f"已补充内置技能种子（新增 {len(seeds_to_add)} 个，"
                    f"跳过已存在 {len(_seed_skills) - len(seeds_to_add)} 个）"
                )
    except Exception as e:
        logger.warning(f"内置技能种子插入失败（可忽略）: {e}")

    # ── 首次启动：users 表为空则自动创建管理员 ──
    # 目的：即使完全不配 QQ 邮箱 SMTP（邮箱注册不可用），装完也能立刻登录进去。
    # 密码留空 → 随机生成一个并打印到日志（只打印这一次）。
    try:
        _bs_db = SessionLocal()
        try:
            from .models import User as _BsUser
            from .core.security import hash_password as _bs_hash
            if _bs_db.query(_BsUser).count() == 0:
                import secrets as _secrets
                _pwd = settings.INITIAL_ADMIN_PASSWORD or _secrets.token_urlsafe(12)
                _eml = settings.INITIAL_ADMIN_EMAIL or f"{settings.INITIAL_ADMIN_USERNAME}@local"
                _bs_db.add(_BsUser(
                    username=settings.INITIAL_ADMIN_USERNAME,
                    email=_eml,
                    password_hash=_bs_hash(_pwd),
                    is_admin=True,
                    is_active=True,
                ))
                _bs_db.commit()
                if settings.INITIAL_ADMIN_PASSWORD:
                    logger.info(f"已创建初始管理员 {_eml}（密码取自 INITIAL_ADMIN_PASSWORD）")
                else:
                    logger.info(
                        "\n" + "=" * 60
                        + f"\n  已创建初始管理员"
                        + f"\n  登录账号：{_eml}"
                        + f"\n  初始密码：{_pwd}"
                        + "\n  \u26a0 这行只打印一次，请立即登录并修改密码"
                        + "\n" + "=" * 60
                    )
        finally:
            _bs_db.close()
    except Exception as e:
        logger.warning(f"初始管理员创建失败（可忽略）: {e}")

    # ── 启动后台任务：定期清理过期工作区（6 小时 TTL，公网磁盘保护）──
    import asyncio
    from .services import workspace_service
    cleanup_task = None

    async def _cleanup_expired_loop():
        while True:
            try:
                await asyncio.sleep(1800)  # 每 30 分钟扫描一次
                db = SessionLocal()
                try:
                    cleaned = workspace_service.cleanup_expired_workspaces(db)
                    if cleaned:
                        logger.info(f"后台清理过期工作区（{workspace_service.WORKSPACE_TTL_HOURS}h TTL）: {cleaned}")
                finally:
                    db.close()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("工作区过期清理任务异常")

    cleanup_task = asyncio.create_task(_cleanup_expired_loop())

    # ── 启动后台任务：每日清理 2 天前的终态 Agent 任务（防 dsh_agent_tasks 无限增长）──
    from .api.v1.chat import cleanup_old_agent_tasks
    agent_task_cleanup_task = None

    async def _agent_task_cleanup_loop():
        while True:
            try:
                db = SessionLocal()
                try:
                    deleted = cleanup_old_agent_tasks(db)
                    if deleted:
                        logger.info(f"清理 {deleted} 条 2 天前的终态 Agent 任务")
                finally:
                    db.close()
                await asyncio.sleep(24 * 3600)  # 每日一次
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Agent 任务每日清理异常，1 小时后重试")
                await asyncio.sleep(3600)

    agent_task_cleanup_task = asyncio.create_task(_agent_task_cleanup_loop())
    yield
    if cleanup_task:
        cleanup_task.cancel()
    if agent_task_cleanup_task:
        agent_task_cleanup_task.cancel()

app = FastAPI(
    title=settings.APP_NAME,
    lifespan=lifespan,
)

# ── CORS ── 前台是独立 SPA，与后端不同源时需要；生产用 Nginx 同域反代则可留空。
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ── API 路由 ──
app.include_router(auth_router, prefix="/api/v1", tags=["认证"])
app.include_router(ai_router, prefix="/api/v1", tags=["用户记忆"])
app.include_router(admin_ai_router, prefix="/api/v1", tags=["AI 管理"])
app.include_router(chat_router, prefix="/api/v1", tags=["AI 对话"])
app.include_router(admin_users_router, prefix="/api/v1", tags=["用户管理"])
app.include_router(workspace_router, prefix="/api/v1", tags=["工作区"])


@app.get("/api/health")
def health():
    """健康检查：部署脚本与探针用。"""
    return {"status": "ok", "app": settings.APP_NAME}


# ══════════════════════════════════════════════════════════════
#  管理端页面（服务端渲染 HTML —— 改界面就是改 admin_pages/*.py，
#  部署只需重启后端，不需要前端构建）
# ══════════════════════════════════════════════════════════════

ADMIN_NAV = [
    ("/admin/ai", "AI 管理"),
    ("/admin/chat", "对话配置"),
    ("/admin/memory", "AI 记忆"),
    ("/admin/skills", "Skills 技能"),
    ("/admin/users", "用户管理"),
]


def _admin_index() -> str:
    """管理端首页：一个纯导航页，列出全部子页面。"""
    items = "".join(
        f'<a href="{href}" class="card"><div class="t">{title}</div>'
        f'<div class="p">{href}</div></a>'
        for href, title in ADMIN_NAV
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{settings.APP_NAME} · 管理后台</title>
<style>
  :root {{ --bg:#FCF9F2; --card:#fff; --text:#111; --muted:#726960; --accent:#986638; --line:#EBE5DB; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:Inter,system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         background:var(--bg); color:var(--text); min-height:100vh; }}
  .container {{ max-width:900px; margin:0 auto; padding:56px 20px; }}
  h1 {{ font-size:1.4em; font-weight:600; margin-bottom:6px; }}
  .sub {{ font-size:0.85em; color:var(--muted); margin-bottom:32px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:14px; }}
  .card {{ display:block; background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:18px 20px; text-decoration:none; color:inherit; transition:border-color .15s, transform .15s; }}
  .card:hover {{ border-color:var(--accent); transform:translateY(-2px); }}
  .t {{ font-size:1em; font-weight:600; margin-bottom:6px; }}
  .p {{ font-size:0.75em; color:var(--muted); font-family:ui-monospace,Menlo,monospace; }}
</style></head>
<body><div class="container">
  <h1>{settings.APP_NAME}</h1>
  <p class="sub">管理后台 · 共 {len(ADMIN_NAV)} 个配置面板</p>
  <div class="grid">{items}</div>
</div></body></html>"""


@app.get("/admin", response_class=HTMLResponse)
def admin_index():
    return _admin_index()


@app.get("/admin/ai", response_class=HTMLResponse)
def admin_ai_page():
    return ADMIN_AI_PAGE


@app.get("/admin/chat", response_class=HTMLResponse)
def chat_admin_page():
    return CHAT_ADMIN_PAGE


@app.get("/admin/memory", response_class=HTMLResponse)
def memory_admin_page():
    return MEMORY_ADMIN_PAGE


@app.get("/admin/skills", response_class=HTMLResponse)
def skills_admin_page():
    return SKILLS_ADMIN_PAGE


@app.get("/admin/users", response_class=HTMLResponse)
def user_admin_page():
    return USER_ADMIN_PAGE


# ══════════════════════════════════════════════════════════════
#  前端静态文件（可选）：frontend/ 构建产物存在时挂到根路径。
#  必须在所有路由之后 mount，否则 "/" 会把 API 与 /admin 一起吞掉。
# ══════════════════════════════════════════════════════════════
_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
    logger.info(f"已挂载前端静态文件: {_FRONTEND_DIST}")

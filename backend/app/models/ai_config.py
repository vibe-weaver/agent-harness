import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date, ForeignKey, Text, UniqueConstraint
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from ..core.database import Base


class AIProvider(Base):
    """AI 厂商配置（API Key 池）

    借鉴 DSH adapter.ts 的多协议设计：
    - api_type = "openai"    → 调用 /chat/completions（OpenAI 兼容格式）
    - api_type = "anthropic" → 调用 /v1/messages（Anthropic Messages API 格式）

    DSH 的 LlmModelDiscoveryRequest.api 字段就是同一个概念：
    "Wire protocol the endpoint speaks, when the draft names one."
    """
    __tablename__ = "ai_providers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)
    base_url = Column(String(200), nullable=False)
    api_key = Column(String(200), nullable=False)
    # 协议类型：openai（默认）| anthropic
    # DSH 桌面端通过 base_url 路径自动判断（/anthropic → anthropic-messages），
    # 这里显式声明，管理端可自行选择，避免 URL 路径歧义
    api_type = Column(String(20), default="openai")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class AIModel(Base):
    """AI 模型池 — 每行对应一个可调用的模型实例"""
    __tablename__ = "ai_models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("ai_providers.id"), nullable=False)
    name = Column(String(100), nullable=False)            # 模型标识，如 dall-e-3
    display_name = Column(String(100), nullable=False)     # 展示名，如 "DALL·E 3"
    supported_sizes = Column(String(200), nullable=False)  # 逗号分隔，如 "1024x1024,1792x1024"
    weight = Column(Integer, default=1)                    # 权重，加权随机
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)             # 默认模型
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class AIRateLimit(Base):
    """AI 频率限制配置（单行表，id 固定为 1）"""
    __tablename__ = "ai_rate_limits"

    id = Column(Integer, primary_key=True, autoincrement=False)
    chat_minute_limit = Column(Integer, default=10)      # 对话每分钟次数上限
    max_concurrent = Column(Integer, default=3)           # 全局最大并发生成数
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class DshConfig(Base):
    """DSH 对话配置（单行表，id=1）"""
    __tablename__ = "dsh_configs"

    id = Column(Integer, primary_key=True, autoincrement=False)
    max_tokens = Column(Integer, default=8192)                      # 输出 token 上限
    # 系统记忆（类似 CLAUDE.md）— 全局持久指令，注入到每次对话的 system prompt 中
    # 支持 Markdown 格式，可包含项目规范、回答风格、约束规则、领域知识等
    system_memory = Column(Text, default="")
    # 会话
    session_root = Column(String(200), default="./.dsh-sessions")
    max_sessions_per_user = Column(Integer, default=5)               # 每个访客最大会话窗口数
    chat_daily_limit = Column(Integer, default=50)                   # 每个访客每日对话次数上限
    max_files_per_message = Column(Integer, default=5)                # 每次对话允许上传的最大文件数量
    # 图片视觉优化5：视觉模型的 image_url.detail 档位（auto | low | high）。
    # auto = 不发送该字段，交给 provider 默认（≈high），与改动前逐字一致；
    # low 在 OpenAI 口径下固定 85 token/图，比 high 省一个数量级，代价是小字/细节识别变差
    # —— 无法自动判断何时该省，所以做成管理端开关而不是写死。
    image_detail = Column(String(10), default="auto")
    # Agent 办公参数（B2/B3/A3/B5 方案，管理端 dsh 对话页面可配，改动即时生效）
    agent_max_tool_rounds = Column(Integer, default=40)              # B2: 单个任务最大工具调用轮数
    agent_max_output_tokens = Column(Integer, default=0)             # B2: 每轮最大输出 token（0=不限制）
    agent_concurrent_limit = Column(Integer, default=4)              # B5: 全局并发 Agent 任务数
    agent_per_user_concurrent = Column(Integer, default=1)           # B5: 每用户并发 Agent 任务数
    agent_workspace_guard = Column(Boolean, default=True)            # A3: 工作区内容注入防御开关
    agent_compact_prompt = Column(Boolean, default=True)             # B3: 工具轮次精简 system prompt 开关
    # 性能优化7：前缀稳定模式（默认开）。开启时跳过 B3 精简，保持 system prompt
    # 跨轮不变，让 provider 端自动 prompt cache 命中（DeepSeek 等按 ~10% 计费）；
    # 关闭时恢复 B3 精简（适合无缓存计费的端点，如智谱）。
    agent_stable_prefix = Column(Boolean, default=True)
    # 记忆模块：当前生效的 embedding 模型 id（指向 dsh_embedding_models 表）。
    # 为空 = 未配置 → 记忆语义检索/自动提取降级关闭（recall 退回 LIKE，不自动提取）。
    # 配置后：add_memory 写入时生成向量；recall 按余弦相似度排序；对话后自动提取记忆。
    # 注意：**指向 dsh_embedding_models 而非 dsh_chat_models** —— 嵌入模型不复用对话模型
    # （对话模型列表会展示给访客可选，混入 embedding 模型会污染访客可见列表）。
    embedding_model_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class ChatSession(Base):
    """访客对话会话窗口 — 每个访客可创建多个会话"""
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), nullable=False, unique=True, index=True)  # DSH session ID
    visitor_id = Column(String(128), nullable=False, index=True)              # 访客 ID
    title = Column(String(200), default="新对话")                             # 会话标题
    model_id = Column(Integer, ForeignKey("dsh_chat_models.id"), nullable=True)
    is_active = Column(Boolean, default=True)                                 # 是否在会话列表中展示
    # 最近一次 assistant 回复的推理过程（reasoning_content），用于刷新后恢复思考过程展示。
    # 每次对话结束时覆盖写入（只保留最新一条）。
    last_reasoning = Column(Text, nullable=True, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class DshChatModel(Base):
    """DSH 对话模型 — 每行对应一个可用的对话模型，关联厂商获取 API Key

    借鉴 DSH adapter.ts 的 resolveModel() 设计：
    - 每个模型独立配置推理等级（reasoning_effort）
    - 不同厂商支持的等级不同（DeepSeek: off/low/high/max, 小米: off/minimal/low/medium/high/xhigh/max）
    - reasoning_effort='off' → thinking: {type: 'disabled'}
    - reasoning_effort='low'/'high'/'max' → thinking: {type: 'enabled'} + reasoning_effort 字段
    """
    __tablename__ = "dsh_chat_models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("ai_providers.id"), nullable=False)
    name = Column(String(100), nullable=False)               # 模型标识，如 deepseek-chat
    display_name = Column(String(100), nullable=False)        # 展示名，如 "DeepSeek 通用对话"
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)                # 默认模型（仅一个）
    supports_vision = Column(Boolean, default=False)            # 是否支持多模态（图片输入）
    context_length = Column(Integer, default=1000000)           # 默认 1M（标配）；0 = 未探测
    reasoning_effort = Column(String(20), default="off")       # 推理等级: off | low | medium | high | xhigh | max
    # 实测支持的推理等级（测试连接时逐档位实测落库），JSON 数组字符串，如 ["low","high","max"]。
    # 空串 = 未实测。对话请求前 _resolve_model 据此校正 reasoning_effort，避免发非法档位触发 400。
    supported_efforts = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class DshEmbeddingModel(Base):
    """向量嵌入模型（embedding）— 供记忆模块的语义检索与自动提取使用。

    **刻意独立于 DshChatModel，不复用对话模型**：
    - 对话模型列表会通过管理端 API 暴露、并在访客对话页作为可选模型展示；
      embedding 模型不参与聊天，混进去会污染访客可见的模型列表。
    - 两类模型字段不同：对话模型要推理等级/多模态/上下文窗口；
      embedding 模型只关心维度（用于排查"换模型后旧向量不匹配"）。
    - 语义上分属两种能力：「对话能力」vs「记忆能力」，独立演进、独立排障。

    厂商（API Key / base_url）仍复用 ai_providers —— 只是模型条目独立，不必重填 key。
    调用形态：POST {base_url}/embeddings（OpenAI 兼容）。
    """
    __tablename__ = "dsh_embedding_models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("ai_providers.id"), nullable=False)
    name = Column(String(100), nullable=False)               # 模型标识，如 text-embedding-3-large
    display_name = Column(String(100), nullable=False)       # 展示名，如 "OpenAI 向量-大"
    is_active = Column(Boolean, default=True)
    # 向量维度：探针调用 /embeddings 后回填真实返回的数组长度；0 = 未探测。
    # 存下来是为了排障：换模型后若维度与已有记忆不一致，相似度恒为 0（旧的会沉底），
    # 管理端能直接看出"当前模型是 3072 维、但库里记忆是 1024 维"这类问题。
    dimensions = Column(Integer, default=0)
    # 维度是否来自实测探针：
    #   True  = 实调 POST /embeddings 测得的真实数组长度；
    #   False = 管理员手动填写（厂商暂时不可用/余额不足/限流时，也要能先把配置建起来）。
    # 刻意区分开 —— 手动填的值不能声称"已验证"，管理端据此提示"语义检索可能尚未真正生效"。
    is_verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class AgentTask(Base):
    """Agent 长任务 — 后台执行，SSE 断开（刷新/断线）任务继续，结果可查询。

    方案 B（长任务后台化）：对话请求先建任务记录（status=running），
    worker 在后台线程执行，不随 SSE 断开取消；完成后回写 result/error。
    前端刷新后可凭 task_id 轮询任务状态与结果。
    """
    __tablename__ = "dsh_agent_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(32), nullable=False, unique=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    session_id = Column(String(64), nullable=False, index=True)
    message = Column(String(2000), default="")
    status = Column(String(20), default="running")   # running / done / failed / cancelled
    result = Column(Text, default="")                 # 完成时存回复文本（截断）
    # 工具调用痕迹（JSON 数组字符串，元素 {id,tool,arguments,result,round,max_rounds}）：
    # 刷新/断线恢复时前端凭此渲染工具卡片，而不是只看到最终文本。
    # 序列化时有事件数/单条长度/总量三重截断（见 chat.py _serialize_tool_events）。
    tool_events = Column(Text, default="")
    error = Column(Text, default="")                  # 失败原因
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    # ── 运营统计（#9）──
    # enable_tools 区分 Agent 任务与纯聊天（每次发送都会插行，缺此列统计无法分开）；
    # 迁移前的旧行为 NULL，统计侧以 tool_events 非空兜底判定。
    enable_tools = Column(Boolean, default=False)
    # 模型降级链触发记录 JSON [{to, status}]，on_model_switch 回调追加，终态时落库
    model_switches = Column(Text, default="")
    # ── token 用量（性能优化1：成本可审计）──
    # provider 流式返回的真实 usage 按任务累计（多轮工具循环每轮请求都计费）。
    # NULL = provider 未返回 usage（OpenAI 兼容流默认不发 usage）或迁移前旧行。
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    # 性能优化7：任务级 prompt cache 命中 tokens（命中部分折扣计费）。
    # 0/无缓存 = NULL，仅在有命中时写入。
    cache_read_tokens = Column(Integer, nullable=True)


class DshAgentDailyStats(Base):
    """Agent 运营统计按天滚动汇总（#9）

    终态任务 2 天即清理（#7 防膨胀），每日清理前把当天指标滚动落账到本表：
    一行一天，存储量 O(天数) 不随任务量增长。metrics 为 JSON，结构与
    agent_stats_service._empty_day() 同构；查询侧与 dsh_agent_tasks 实时数据
    按日期相加——rollup 只含已删除的任务，与实时层天然不相交，相加即全量。
    """

    __tablename__ = "dsh_agent_daily_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    stat_date = Column(Date, nullable=False, unique=True, index=True)
    metrics = Column(Text, default="")


class AgentCostAlert(Base):
    """Agent 任务级成本告警（性能优化9）

    任务落终态（done/failed，cancelled 除外）时检查两个阈值：
    总 token 用量、任务耗时；超限插一行告警，供管理端统计面板展示。
    只记录不阻断——告警是运营信号，不影响任务本身。
    """

    __tablename__ = "dsh_agent_alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(32), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    alert_type = Column(String(20), nullable=False)   # tokens | duration
    value = Column(Integer, nullable=False)           # 实际值（token 数 / 秒）
    threshold = Column(Integer, nullable=False)       # 触发阈值
    message = Column(String(300), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class DshSkill(Base):
    """DSH Skill 技能定义

    借鉴 DSH skill-filesystem 的设计：
    - 每个 skill 可以是纯文本内容（content 字段），也可以是一个目录（dir_path）
    - 目录形式的 skill 包含 SKILL.md + 可选的参考文件（references/ 等）
    - agent 通过 `skill` 工具按需加载完整的 skill 内容
    - catalog（name + description）注入到 system prompt，让模型知道有哪些 skill 可用
    """
    __tablename__ = "dsh_skills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False, unique=True)
    description = Column(Text, nullable=False)  # skill 描述（可能很长，用 Text 而非 String）
    content = Column(Text, nullable=False)  # SKILL.md 正文（Markdown）
    dir_path = Column(String(500), default="")  # 目录形式 skill 的磁盘路径（空=纯文本 skill）
    pack = Column(String(50), default="")  # 所属技能包名（空=独立技能）；pack 技能可访问包根下的共享资源
    # 分类标签（管理端录入，前端技能面板下拉筛选用）。空串 = 未分类。
    # 与 pack 不同：pack 决定磁盘目录与共享资源归属，category 只是展示层的归类。
    category = Column(String(50), default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class AiDailyUsage(Base):
    """每日 AI 配额消费记录 — 持久化到数据库，重启不丢失。

    公网场景：内存限流重启即清零，可被"重启绕过"刷配额。
    每日计数以本表为准，分钟级限流与并发计数仍走内存 rate_limiter。
    """
    __tablename__ = "ai_daily_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    usage_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    chat_count = Column(Integer, default=0)          # 对话次数
    image_count = Column(Integer, default=0)         # 访客生图次数
    agent_image_count = Column(Integer, default=0)   # Agent 工具生图次数（独立分账）

    __table_args__ = (UniqueConstraint('user_id', 'usage_date', name='uq_user_usage_date'),)


class UserMemory(Base):
    """用户长期记忆 — 跨会话持久化，按用户隔离。

    来源：agent 在对话中通过 remember 工具主动写入（用户表达偏好/个人信息/长期目标时）。
    每次对话注入最近记忆到 system prompt，让 agent 记住用户。
    容量上限 MAX_MEMORIES_PER_USER（默认 100），超出按最近使用 LRU 淘汰。
    """
    __tablename__ = "user_memories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    memory_type = Column(String(20), default="fact")   # fact(事实) | preference(偏好) | context(上下文)
    content = Column(Text, nullable=False)             # 记忆内容（一句话/短句，便于注入）
    content_hash = Column(String(64), nullable=False, default="")  # SHA256(content)，唯一约束防并发重复
    source = Column(String(200), default="agent")      # 来源
    access_count = Column(Integer, default=0)          # 命中次数（recall 时 +1）
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)     # 最近使用时间（LRU/TTL 用）
    # 向量嵌入：存 JSON 序列化的 float 数组（如 "[0.012, -0.034, ...]"）。
    # 用 MEDIUMTEXT(16MB) 而非 TEXT(64KB) —— 1024 维 JSON 约 16~24KB 够，但配置更大
    # embedding 模型（如 text-embedding-3-large 3072 维 ≈ 40~60KB）会逼近 TEXT 上限。
    # NULL = 未生成向量（embedding 模型未配置或生成失败），recall 退回 LIKE。
    embedding = Column(MEDIUMTEXT, nullable=True)

    __table_args__ = (
        # 唯一约束：同一用户不允许两条内容完全相同的记忆（应用层先去重，此约束为并发兜底）。
        # 注意：TEXT 列无法直接建唯一索引，故对内容做 SHA256 摘要列再约束（MySQL 下索引小且精确）。
        UniqueConstraint('user_id', 'content_hash', name='uq_user_memory_user_content_hash'),
    )

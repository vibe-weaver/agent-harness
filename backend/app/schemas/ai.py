from pydantic import BaseModel, Field, field_validator


# ── 厂商管理 API schemas ──

class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    base_url: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=1, max_length=200)
    api_type: str = Field(default="openai", pattern="^(openai|anthropic)$")
    is_active: bool = True


class ProviderRead(BaseModel):
    id: int
    name: str
    base_url: str
    api_key_masked: str
    api_type: str
    is_active: bool
    created_at: str

    model_config = {"from_attributes": True}


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_type: str | None = Field(default=None, pattern="^(openai|anthropic)$")
    is_active: bool | None = None


# ── 模型池管理 API schemas ──






class RateLimitRead(BaseModel):
    max_concurrent: int

    model_config = {"from_attributes": True}


class RateLimitUpdate(BaseModel):
    max_concurrent: int = Field(default=3, ge=1, le=50)


# ── AI 对话 schemas ──

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    model_id: int | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    finish_reason: str | None = None
    context_used_tokens: int | None = None
    context_window: int | None = None
    # 上下文构成拆分（近似值）
    system_tokens: int | None = None
    tools_tokens: int | None = None
    message_tokens: int | None = None


# ── 对话配额（字段名 image 为历史遗留，实际承载对话日配额） ──

class QuotaItem(BaseModel):
    daily_limit: int
    used: int
    remaining: int


class QuotaResponse(BaseModel):
    image: QuotaItem


# ── 访客会话窗口 schemas ──

class ChatSessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model_id: int | None = None


class ChatSessionRead(BaseModel):
    id: int
    session_id: str
    visitor_id: str
    title: str
    model_id: int | None
    is_active: bool
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class ChatSessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None


# ── DSH 对话配置 schemas ──

class DshConfigRead(BaseModel):
    system_memory: str = ""   # 类似 CLAUDE.md 的全局系统记忆
    session_root: str
    max_sessions_per_user: int
    chat_daily_limit: int
    max_files_per_message: int
    image_detail: str = "auto"   # 图片视觉优化5：视觉模型 detail 档位 auto|low|high
    # Agent 办公参数
    agent_max_tool_rounds: int = 40
    agent_max_output_tokens: int = 0
    agent_concurrent_limit: int = 4
    agent_per_user_concurrent: int = 1
    agent_workspace_guard: bool = True
    agent_compact_prompt: bool = True
    agent_stable_prefix: bool = True
    # 记忆模块：embedding 模型 id（NULL/0 = 未配置 → 记忆语义检索与自动提取降级关闭）
    embedding_model_id: int | None = None

    @field_validator('system_memory', mode='before')
    @classmethod
    def _none_to_empty(cls, v):
        # 兼容旧数据：system_memory 可能为 None（TEXT NULL 列）
        return '' if v is None else v

    @field_validator('image_detail', mode='before')
    @classmethod
    def _normalize_image_detail(cls, v):
        # 兼容旧数据：迁移前的行可能是 NULL；非法值也归一到 auto（= 不发送该字段）
        s = str(v or "auto").strip().lower()
        return s if s in ("auto", "low", "high") else "auto"

    model_config = {"from_attributes": True}


class DshConfigUpdate(BaseModel):
    system_memory: str = Field(default="", max_length=10000)
    session_root: str = Field(min_length=1, max_length=200)
    max_sessions_per_user: int = Field(default=5, ge=1, le=50)
    chat_daily_limit: int = Field(default=50, ge=1, le=10000)
    max_files_per_message: int = Field(default=5, ge=0, le=20)
    image_detail: str = Field(default="auto", pattern="^(auto|low|high)$")
    # Agent 办公参数（B2/B5/A3/B3）
    agent_max_tool_rounds: int = Field(default=40, ge=1, le=200)
    agent_max_output_tokens: int = Field(default=0, ge=0, le=100000)  # 0 = 不限制
    agent_concurrent_limit: int = Field(default=4, ge=1, le=32)
    agent_per_user_concurrent: int = Field(default=1, ge=1, le=8)
    agent_workspace_guard: bool = True
    agent_compact_prompt: bool = True
    agent_stable_prefix: bool = True
    # 记忆模块：embedding 模型 id。与对话模型配置互相独立。
    # 语义：不传（None）= 不修改现有配置；传 0 = 清空（关闭语义检索）；传 id = 设置（含探针校验）。
    # 注意不能把 None 当作"清空"——主保存按钮的 payload 不含此字段，否则会误清向量配置。
    embedding_model_id: int | None = Field(default=None, ge=0)


# ── DSH 对话模型管理 schemas ──

class ChatModelCreate(BaseModel):
    provider_id: int
    name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    is_active: bool = True
    is_default: bool = False
    supports_vision: bool = False
    context_length: int = Field(default=1000000, ge=0, le=1048576)  # 默认 1M（标配），可手动覆盖；0 = 未探测
    reasoning_effort: str = Field(default="off", pattern="^(off|minimal|low|medium|high|xhigh|max)$")
    # 实测支持的推理等级（测试连接落库）；前端测试后提交，空串/null=未实测
    supported_efforts: str = Field(default="", max_length=500)


class ChatModelRead(BaseModel):
    id: int
    provider_id: int
    provider_name: str
    name: str
    display_name: str
    is_active: bool
    is_default: bool
    supports_vision: bool
    context_length: int
    reasoning_effort: str
    supported_efforts: str = ""
    created_at: str

    model_config = {"from_attributes": True}


class ChatModelUpdate(BaseModel):
    provider_id: int | None = None
    name: str | None = None
    display_name: str | None = None
    is_active: bool | None = None
    is_default: bool | None = None
    supports_vision: bool | None = None
    context_length: int | None = Field(default=None, ge=0, le=1048576)
    reasoning_effort: str | None = Field(default=None, pattern="^(off|minimal|low|medium|high|xhigh|max)$")
    supported_efforts: str | None = Field(default=None, max_length=500)


# ── 向量嵌入模型管理 schemas（记忆模块 · 独立于对话模型） ──

class EmbeddingModelCreate(BaseModel):
    provider_id: int
    name: str = Field(min_length=1, max_length=100)          # 模型标识，如 text-embedding-3-large
    display_name: str = Field(min_length=1, max_length=100)
    is_active: bool = True
    # 手动指定向量维度（可选）。填了(>0)就**不走探针** —— 用于厂商暂时不可用
    # （余额不足 / 限流 / 网络不通）时也能先把配置建起来；留空则实调 /embeddings 自动探测。
    # 上限 65536 是防御性取值：实际最大的 text-embedding-3-large 也只有 3072 维。
    dimensions: int | None = Field(default=None, ge=0, le=65536)


class EmbeddingModelUpdate(BaseModel):
    provider_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None
    # 同 Create：填了(>0)则跳过敏探针并标记为"未验证"；留空则维持原逻辑（改厂商/标识才重探）。
    dimensions: int | None = Field(default=None, ge=0, le=65536)


class EmbeddingModelRead(BaseModel):
    id: int
    provider_id: int
    provider_name: str
    name: str
    display_name: str
    is_active: bool
    # 向量维度（0 = 未知）。手动填写与探针实测都会落在这里。
    dimensions: int = 0
    # 该维度是否来自实测探针。False = 管理员手动填写，尚未验证 ——
    # 管理端据此显示「手动填写」，并提示语义检索可能尚未真正生效。
    is_verified: bool = False
    # 是否被 DshConfig 选为当前生效的嵌入模型
    is_current: bool = False
    created_at: str

    model_config = {"from_attributes": True}


class EmbeddingCurrentUpdate(BaseModel):
    """设置/清空「当前生效的向量检索模型」。

    只动 DshConfig.embedding_model_id 这一个字段，**不触碰对话/Agent 配置** ——
    刻意不复用 PUT /dsh-config（那是全量覆盖，
    把记忆配置和其他对话配置绑在一起没有道理）。
    语义：model_id = 0 → 清空（关闭语义检索，降级为关键词匹配）；> 0 → 设为当前（含探针校验）。
    force = True → 跳过探针，只做"存在且启用"的静态校验后直接生效。
    用在厂商当前不可用（余额不足/限流）但管理员仍要挂上已配好模型的场景；
    此时管理端会把该条目显示为「未验证」，不做任何"已验证"的伪装。
    """
    model_id: int = Field(default=0, ge=0)
    force: bool = False


class ChatModelTestRequest(BaseModel):
    """模型连通性测试 — 验证模型标识在该厂商端点上真实可用（B1）。"""
    provider_id: int | None = None
    name: str = Field(min_length=1, max_length=100)
    base_url: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=200)


class ChatModelTestResult(BaseModel):
    ok: bool = True
    message: str
    # 实测支持的推理等级（逐档位发最小请求，200 即支持）
    efforts: list[dict] = []


# ── DSH 风格模型自动发现 schemas ──
# 对应 DSH discovery.ts 的 LlmModelDiscoveryRequest / LlmDiscoveredModel

class ModelDiscoveryRequest(BaseModel):
    """探测一个 Provider 端点的可用模型列表。

    对应 DSH 的 LlmModelDiscoveryRequest:
    - provider_id: 已存在的厂商 ID（从中获取 base_url 和 api_key）
    - base_url: 可选，直接指定端点（用于尚未保存的厂商）
    - api_key: 可选，直接指定 Key（用于测试新 Key）
    优先使用 base_url/api_key，否则从 provider_id 查询。
    """
    provider_id: int | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=200)


class DiscoveredModelItem(BaseModel):
    """一个被探测到的模型 — 对应 DSH 的 LlmDiscoveredModel。"""
    id: str
    name: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None


class ModelDiscoveryResponse(BaseModel):
    """模型探测结果。"""
    models: list[DiscoveredModelItem]
    total: int
    source: str  # "endpoint" 表示从端点探测，"fallback" 表示使用默认值


# ── DSH Skill 管理 schemas ──

class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    description: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=10000)
    category: str = Field(default="", max_length=50)  # 分类标签（空=未分类）
    is_active: bool = True


class SkillRead(BaseModel):
    id: int
    name: str
    description: str
    content: str
    dir_path: str = ""
    pack: str = ""
    category: str = ""
    is_active: bool
    created_at: str
    warnings: list[str] = Field(default_factory=list)  # 引用健康检查警告（上传/编辑时填充）

    model_config = {"from_attributes": True}


class SkillUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    description: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    pack: str | None = Field(default=None, max_length=50, pattern="^[a-zA-Z0-9_-]*$")
    category: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None


class PackUploadResult(BaseModel):
    """技能包共享资源上传结果。"""
    pack: str
    files: int = 0


class RepoSkillItem(BaseModel):
    """整仓库导入中单个注册技能的结果。"""
    name: str
    description: str = ""
    dir_path: str = ""
    warnings: list[str] = Field(default_factory=list)


class RepoImportResult(BaseModel):
    """整仓库 ZIP 一键导入结果（优化6）。"""
    pack: str
    files: int = 0
    skills: list[RepoSkillItem] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)

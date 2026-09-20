"""AI 记忆模块 管理页（服务端渲染 HTML）。

刻意独立成页（GET /admin/memory），**不放进 /admin/chat**：
- /admin/chat 管的是「对话能力」（对话模型、Agent 参数、Skills）；
- 本页管的是「记忆能力」（向量检索模型、检索与生命周期策略）。
两者独立演进、独立排障：改 embedding 模型不该让人去对话配置页里找，
也不该和「对话配置」共用同一个保存按钮。

**嵌入模型是手动维护的独立实体（dsh_embedding_models），不复用对话模型**：
- 对话模型列表会暴露给访客做模型选择；embedding 模型不参与聊天，混进去会污染该列表。
- 两类模型字段语义不同：对话模型要推理等级/多模态/上下文窗口；embedding 只要维度。
厂商（API Key / base_url）仍复用 ai_providers —— 模型条目独立，不必重填 key。
"""

MEMORY_ADMIN_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 记忆模块</title>
<style>
  :root { --bg:#FCF9F2; --card:#fff; --text:#111; --muted:#726960; --accent:#986638; --line:#EBE5DB; --red:#c44; --green:#4a8; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:Inter,system-ui,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; }
  .container { max-width:1000px; margin:0 auto; padding:40px 20px; }
  h1 { font-size:1.3em; font-weight:600; margin-bottom:4px; }
  .sub { font-size:0.8em; color:var(--muted); margin-bottom:24px; }
  .nav-bar { display:flex; gap:16px; margin-bottom:24px; font-size:0.82em; flex-wrap:wrap; }
  .nav-bar a { color:var(--accent); text-decoration:none; padding:4px 12px; border-radius:6px; border:1px solid var(--line); }
  .nav-bar a.current { color:#fff; background:var(--accent); border-color:var(--accent); }
  /* toast 要能承载长文案（如厂商报错的完整诊断）：多行、可换行、限高可滚动、点击可关。
     早期版本是单行 flex + 无换行控制，长错误会横向溢出屏幕外，完全没法读。 */
  #toast { position:fixed; top:20px; left:50%; transform:translateX(-50%) translateY(-140%); padding:12px 18px; border-radius:8px; font-size:0.82em; display:flex; align-items:flex-start; gap:8px; z-index:9999; opacity:0; transition:all 0.24s ease; pointer-events:none; max-width:min(720px,92vw); max-height:52vh; overflow-y:auto; box-shadow:0 6px 24px rgba(0,0,0,0.14); text-align:left; line-height:1.65; word-break:break-word; overflow-wrap:anywhere; }
  #toast.show { transform:translateX(-50%) translateY(0); opacity:1; pointer-events:auto; cursor:pointer; }
  #toast.ok { background:rgba(152,102,56,0.12); color:var(--accent); border:1px solid rgba(152,102,56,0.2); }
  #toast.err { background:rgba(200,60,60,0.08); color:var(--red); border:1px solid rgba(200,60,60,0.22); }
  #toast .toast-icon { font-size:1.1em; line-height:1.4; flex-shrink:0; }
  #toast .toast-text { min-width:0; }
  .btn { padding:6px 16px; font-size:0.82em; font-weight:500; border:none; border-radius:6px; cursor:pointer; font-family:inherit; white-space:nowrap; }
  .btn:disabled { opacity:0.55; cursor:not-allowed; }
  .btn-primary { background:var(--accent); color:#fff; box-shadow:0 2px 0 rgba(0,0,0,0.15); }
  .btn-sm { padding:4px 10px; font-size:0.76em; }
  .btn-ghost { background:transparent; color:var(--accent); border:1px solid rgba(152,102,56,0.25); }
  .btn-ghost:hover { background:rgba(152,102,56,0.07); }
  .btn-ghost.danger { color:var(--red); border-color:rgba(200,60,60,0.25); }
  .btn-ghost.danger:hover { background:rgba(200,60,60,0.07); }
  .section-title { font-size:0.9em; font-weight:600; margin:24px 0 8px; color:var(--text); display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; }
  .card { padding:16px; margin-bottom:16px; border:1px solid var(--line); border-radius:10px; background:var(--card); }
  .form-row { display:grid; grid-template-columns:130px 1fr; gap:8px; align-items:center; margin-bottom:10px; }
  .form-row label { font-size:0.82em; color:var(--muted); }
  .form-row select, .form-row input[type=text] { padding:6px 10px; border:1px solid var(--line); border-radius:6px; font-size:0.85em; font-family:inherit; width:100%; }
  .form-row select:focus, .form-row input[type=text]:focus { outline:none; border-color:var(--accent); }
  .modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.3); display:none; align-items:center; justify-content:center; z-index:200; opacity:0; transition:opacity 0.2s ease; }
  .modal-overlay.show { display:flex; opacity:1; }
  .modal { background:var(--card); border-radius:12px; padding:24px; width:90%; max-width:460px; max-height:90vh; overflow-y:auto; box-shadow:0 8px 32px rgba(0,0,0,0.15); transform:scale(0.95); transition:transform 0.2s ease; }
  .modal-overlay.show .modal { transform:scale(1); }
  .modal h2 { font-size:1.1em; margin-bottom:14px; }
  .modal-actions { display:flex; gap:8px; justify-content:center; margin-top:20px; }
  .confirm-modal { text-align:center; }
  .confirm-modal .confirm-icon { font-size:2.4em; line-height:1; margin-bottom:12px; }
  .confirm-modal .confirm-desc { font-size:0.85em; color:var(--muted); line-height:1.6; margin-bottom:4px; white-space:pre-wrap; text-align:left; }
  .info-banner { padding:12px 16px; background:rgba(152,102,56,0.06); border:1px solid rgba(152,102,56,0.15); border-radius:8px; font-size:0.8em; color:var(--muted); margin-bottom:20px; line-height:1.7; }
  .info-banner code { background:rgba(152,102,56,0.08); padding:1px 4px; border-radius:3px; font-size:0.95em; }
  .modal .info-banner { margin-bottom:14px; }
  .hint { font-size:0.75em; color:var(--muted); line-height:1.8; margin-top:10px; }
  .checkbox-row { display:flex; align-items:center; gap:8px; font-size:0.85em; }
  .checkbox-row input { width:auto; }
  /* 当前生效状态条 */
  .current-line { display:flex; align-items:center; gap:10px; flex-wrap:wrap; font-size:0.85em; line-height:1.7; }
  .current-label { font-size:0.8em; color:var(--muted); flex-shrink:0; }
  #currentActions { margin-top:10px; }
  /* 表格 */
  .emb-table { width:100%; border-collapse:collapse; font-size:0.82em; }
  .emb-table th { text-align:left; padding:8px 10px; border-bottom:2px solid var(--line); color:var(--muted); font-weight:600; font-size:0.9em; white-space:nowrap; }
  .emb-table td { padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:middle; line-height:1.6; }
  .emb-table tr:last-child td { border-bottom:none; }
  .emb-table code { background:rgba(114,105,96,0.08); padding:1px 5px; border-radius:3px; }
  .emb-table td.ops { text-align:right; white-space:nowrap; }
  .emb-table td.ops .btn { margin-left:4px; }
  .emb-table td.empty { text-align:center; color:var(--muted); padding:24px 10px; }
  .tag { display:inline-block; padding:1px 7px; border-radius:4px; font-size:0.85em; white-space:nowrap; }
  .tag-cur { background:rgba(74,136,136,0.12); color:#2f6b6b; }
  .tag-on { background:rgba(74,136,136,0.1); color:#3d7a7a; }
  .tag-off { background:rgba(114,105,96,0.12); color:var(--muted); }
  .tag-warn { background:rgba(200,60,60,0.1); color:var(--red); }
  /* 只读策略表 */
  .policy-table { width:100%; border-collapse:collapse; font-size:0.82em; }
  .policy-table th { text-align:left; padding:8px 10px; border-bottom:2px solid var(--line); color:var(--muted); font-weight:600; font-size:0.9em; }
  .policy-table td { padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:top; line-height:1.6; }
  .policy-table tr:last-child td { border-bottom:none; }
  .policy-table td:first-child { white-space:nowrap; color:var(--muted); width:190px; }
  .readonly-tag { display:inline-block; padding:1px 7px; border-radius:4px; font-size:0.85em; background:rgba(114,105,96,0.1); color:var(--muted); font-weight:400; }
  @media(max-width:640px){ *{-webkit-tap-highlight-color:transparent} .btn{padding:10px 16px;font-size:0.9em} .btn-sm{padding:6px 10px;font-size:0.82em} .container{padding:20px 12px} .form-row{grid-template-columns:1fr} .policy-table td:first-child{width:auto} .emb-table td.ops{white-space:normal} }
</style>
</head>
<body>
<div class="container">
  <h1>AI 记忆模块</h1>
  <p class="sub">手动维护记忆的向量语义检索模型（不复用对话模型），并查看内置的检索与生命周期策略</p>
  <div class="nav-bar">
    <a href="/admin">总览</a>
    <a href="/admin/ai">AI 管理</a>
    <a href="/admin/chat">对话配置</a>
    <a href="/admin/memory" class="current">AI 记忆</a>
    <a href="/admin/skills">Skills 技能</a>
    <a href="/admin/users">用户管理</a>
  </div>
  <div id="toast"><span class="toast-icon"></span><span class="toast-text"></span></div>

  <div class="info-banner">
    💡 记忆模块让 Agent 记住用户的长期偏好与事实（跨会话生效），由 <strong>Agent 主动写入</strong> 与 <strong>对话后自动提取</strong> 两条路径产生。<br>
    本页<strong>只</strong>负责一件事：指定把记忆文本转成向量的 <strong>embedding 模型</strong>。<br>
    <strong>嵌入模型是独立实体，不复用对话模型</strong> —— 对话模型决定访客聊天用哪个模型，会暴露给访客选择；这里的模型不参与聊天，只在服务端用于记忆检索。因此本页的保存/新增/删除与对话配置互不影响，保存后即时生效。<br>
    调用形态为 OpenAI 兼容的 <code>POST {base_url}/embeddings</code>，厂商 API Key 复用「AI 管理」里的配置（本页不必重填 Key）。
  </div>

  <!-- ── 当前生效状态 ── -->
  <div class="card" style="border-left:3px solid var(--accent)">
    <div class="current-line">
      <span class="current-label">当前生效</span>
      <span id="currentStatus">加载中...</span>
    </div>
    <div id="currentActions"></div>
  </div>

  <!-- ── 嵌入模型列表（新增/编辑/删除/测试/设为当前） ── -->
  <div class="section-title">
    <span>向量语义检索模型</span>
    <span style="font-size:0.72em;color:var(--muted);font-weight:400">手动维护 · 独立保存</span>
  </div>
  <div class="card">
    <div style="display:flex;justify-content:flex-end;margin-bottom:12px">
      <button class="btn btn-primary" onclick="openAddModal()" id="addModelBtn">＋ 新增嵌入模型</button>
    </div>
    <table class="emb-table">
      <thead>
        <tr>
          <th>名称</th><th>模型标识</th><th>厂商</th><th>维度</th><th>状态</th><th style="text-align:right">操作</th>
        </tr>
      </thead>
      <tbody id="embTbody"><tr><td colspan="6" class="empty">加载中...</td></tr></tbody>
    </table>
    <div id="mem_hint" class="hint"></div>
    <div class="info-banner" style="margin:14px 0 0">
      <strong>必须是真正支持</strong> <code>POST /embeddings</code> <strong>的模型</strong>（各类 <code>text-embedding-*</code>、<code>bge-*</code>、<code>embedding-*</code> 等）。普通对话模型（如 <code>deepseek-chat</code>）<strong>不支持</strong>该端点 —— 新增/设为当前时后端会<strong>实调一次探针校验</strong>，不通过会直接拒绝并保持原配置不变，不会留下"配了却永远失败"的死条目。<br>
      <strong>维度用于排障</strong>：不同 embedding 模型的向量维度通常不同，已有记忆的旧向量与新模型不兼容（相似度按 0 计），这些旧记忆会在语义排序中沉底。表中「维度」列是探针实测值，可据此发现"当前模型 3072 维、但库里记忆是 1024 维"这类问题。若想彻底干净，可换模型后清空历史记忆让它们按新模型重建。
    </div>
  </div>

  <!-- ── 服务端内置策略（只读） ── -->
  <div class="section-title">
    <span>检索与生命周期策略</span>
    <span class="readonly-tag">服务端内置 · 只读</span>
  </div>
  <div class="card">
    <table class="policy-table">
      <thead><tr><th>项目</th><th>当前生效值</th></tr></thead>
      <tbody>
        <tr>
          <td>检索方式</td>
          <td>当前模型可生成向量 → <strong>余弦相似度排序</strong>（先关键词缩小候选，未命中则全量语义排序）<br>未配置 / 模型已停用 / 生成失败 → <strong>关键词匹配</strong>（LIKE）</td>
        </tr>
        <tr>
          <td>每用户记忆上限</td>
          <td>100 条；超出按 LRU（最近最少使用）淘汰最旧的一条</td>
        </tr>
        <tr>
          <td>每轮注入上限</td>
          <td>最多 20 条 / 4000 字（防止撑爆上下文）</td>
        </tr>
        <tr>
          <td>记忆过期（TTL）</td>
          <td>用户偏好与事实（fact / preference）：<strong>90 天</strong>未使用即淘汰<br>短期上下文（context）：<strong>7 天</strong>未使用即淘汰<br>采用「写入时懒清理」，无需定时任务</td>
        </tr>
        <tr>
          <td>单条记忆长度</td>
          <td>最长 2000 字；同一用户内容去重（相同内容只刷新时间）</td>
        </tr>
        <tr>
          <td>向量存储</td>
          <td>以 JSON 数组存在 <code>user_memories.embedding</code>（MEDIUMTEXT），单条上限 16MB，足够容纳高维向量</td>
        </tr>
      </tbody>
    </table>
  </div>
</div>

<!-- 新增 / 编辑嵌入模型 -->
<div class="modal-overlay" id="modelOverlay">
  <div class="modal">
    <h2 id="modelModalTitle">新增嵌入模型</h2>
    <div class="info-banner" id="modelHint" style="display:none"></div>
    <div class="form-row">
      <label>厂商</label>
      <select id="m_provider"></select>
    </div>
    <div class="form-row">
      <label>模型标识</label>
      <input type="text" id="m_name" list="embNameSuggestions" placeholder="如 text-embedding-3-large" autocomplete="off">
    </div>
    <div class="form-row">
      <label>展示名</label>
      <input type="text" id="m_display" placeholder="留空则用模型标识" autocomplete="off">
    </div>
    <div class="form-row">
      <label>向量维度</label>
      <div style="display:flex;flex-direction:column;gap:6px;flex:1">
        <input type="number" id="m_dim" min="0" max="65536" step="1" placeholder="留空 = 自动探测（推荐）" autocomplete="off" oninput="updateDimHint()">
        <div id="m_dim_hint" style="font-size:0.72em;color:var(--muted);line-height:1.65"></div>
      </div>
    </div>
    <div class="form-row">
      <label>启用</label>
      <div class="checkbox-row"><input type="checkbox" id="m_active" checked><span>启用后才会出现在「设为当前」的候选中</span></div>
    </div>
    <div class="modal-actions">
      <button class="btn" style="background:var(--line);color:var(--text)" onclick="closeModelModal()" id="modelCancelBtn">取消</button>
      <button class="btn btn-primary" onclick="submitModelForm()" id="modelSubmitBtn">保存</button>
    </div>
  </div>
</div>

<datalist id="embNameSuggestions">
  <option value="text-embedding-3-large"></option>
  <option value="text-embedding-3-small"></option>
  <option value="text-embedding-ada-002"></option>
  <option value="bge-m3"></option>
  <option value="bge-large-zh-v1.5"></option>
  <option value="embedding-3"></option>
  <option value="text-embedding-v3"></option>
  <option value="Qwen3-Embedding-8B"></option>
</datalist>

<!-- 确认弹窗 -->
<div class="modal-overlay" id="confirmOverlay">
  <div class="modal confirm-modal">
    <div class="confirm-icon" id="confirmIcon">🧭</div>
    <h2 id="confirmTitle">确认</h2>
    <p class="confirm-desc" id="confirmDesc"></p>
    <div class="modal-actions">
      <button class="btn" style="background:var(--line);color:var(--text)" onclick="_confirmCancel()" id="confirmCancelBtn">取消</button>
      <button class="btn btn-primary" onclick="_confirmOk()" id="confirmOkBtn">确定</button>
    </div>
  </div>
</div>

<script>
window.addEventListener('error',function(e){toast('JS错误: '+e.message,false)});
window.addEventListener('unhandledrejection',function(e){toast('请求失败: '+(e.reason&&e.reason.message||e.reason||'网络错误'),false)});
const API = '/api/v1/admin/ai';

let _toastTimer = null;
function toast(msg, ok) {
  const t = document.getElementById('toast');
  t.querySelector('.toast-icon').textContent = ok ? '✓' : '✕';
  t.querySelector('.toast-text').textContent = msg;
  t.className = 'show ' + (ok ? 'ok' : 'err');
  if (_toastTimer) clearTimeout(_toastTimer);
  // 长文案（诊断信息）需要更久的阅读时间，按长度动态延长，上限 12 秒。
  const dur = ok ? 3200 : Math.min(12000, 4500 + String(msg || '').length * 45);
  _toastTimer = setTimeout(function(){ t.className = ''; }, dur);
}
// 点一下即可关掉（错误信息常常很长，用户看完想立刻收起）
document.getElementById('toast').addEventListener('click', function(){
  this.className = '';
  if (_toastTimer) clearTimeout(_toastTimer);
});

function esc(s) { if(s===null||s===undefined) return ''; const d=document.createElement('div'); d.textContent=String(s); return d.innerHTML; }

// ═══ 自定义确认弹窗（与对话配置页同款交互） ═══

let _confirmResolve = null;
function showConfirm(title, desc, opts) {
  opts = opts || {};
  return new Promise(function(resolve) {
    _confirmResolve = resolve;
    document.getElementById('confirmTitle').textContent = title || '确认';
    document.getElementById('confirmDesc').textContent = desc || '';
    document.getElementById('confirmIcon').textContent = opts.icon || '🧭';
    document.getElementById('confirmOkBtn').textContent = opts.okText || '确定';
    document.getElementById('confirmCancelBtn').textContent = opts.cancelText || '取消';
    document.getElementById('confirmOverlay').classList.add('show');
  });
}
function _confirmOk() {
  document.getElementById('confirmOverlay').classList.remove('show');
  if (_confirmResolve) { _confirmResolve(true); _confirmResolve = null; }
}
function _confirmCancel() {
  document.getElementById('confirmOverlay').classList.remove('show');
  if (_confirmResolve) { _confirmResolve(false); _confirmResolve = null; }
}

// 统一解析后端错误：detail 可能是字符串（业务错误）或数组（422 校验错误）
async function readErr(res) {
  try {
    const b = await res.json();
    if (b && typeof b.detail === 'string') return b.detail;
    if (Array.isArray(b && b.detail) && b.detail[0] && b.detail[0].msg) return b.detail[0].msg;
  } catch(e) {}
  return 'HTTP ' + res.status;
}

// ═══ 数据层 ═══

let providersCache = [];     // GET /providers
let embModelsCache = [];     // GET /embedding-models
let editingId = null;        // null = 新增；否则为正在编辑的模型 id

async function loadProviders() {
  try {
    const res = await fetch(API + '/providers');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    providersCache = await res.json();
  } catch(e) {
    providersCache = [];
    toast('加载厂商列表失败: ' + e.message, false);
  }
}

async function loadEmbeddingModels() {
  try {
    const res = await fetch(API + '/embedding-models');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    embModelsCache = await res.json();
    renderTable();
    renderCurrentStatus();
  } catch(e) {
    document.getElementById('embTbody').innerHTML =
      '<tr><td colspan="6" class="empty" style="color:var(--red)">加载失败: ' + esc(e.message) + '</td></tr>';
    document.getElementById('currentStatus').innerHTML =
      '<span class="tag tag-warn">加载失败</span> ' + esc(e.message);
    document.getElementById('currentActions').innerHTML = '';
  }
}

// ═══ 渲染 ═══

function renderCurrentStatus() {
  const el = document.getElementById('currentStatus');
  const act = document.getElementById('currentActions');
  const cur = embModelsCache.filter(function(m){ return m.is_current; })[0];

  if (!cur) {
    el.innerHTML = '<span class="tag tag-off">未配置</span> 记忆检索走关键词匹配（LIKE），对话后不自动提取记忆。功能不报错，只是效果变弱。';
    act.innerHTML = '';
    return;
  }

  const dim = cur.dimensions
    ? (' · ' + cur.dimensions + ' 维' + (cur.is_verified ? '（实测）' : '（手动填写，未验证）'))
    : ' · 维度未知';
  if (!cur.is_active) {
    // 停用/删除后 _get_embedding_config 会过滤掉它 → 实际已降级，但配置值还指着它。
    el.innerHTML = '<span class="tag tag-warn">⚠ 已降级</span> 仍指向「' + esc(cur.display_name) +
      '」，但该模型已被停用 —— 服务端取不到它，记忆检索实际已降级为关键词匹配。请重新「设为当前」一个启用的模型，或直接关闭向量检索。';
  } else {
    el.innerHTML = '<span class="tag tag-on">已启用</span> <strong>' + esc(cur.display_name) +
      '</strong>（<code>' + esc(cur.name) + '</code> · ' + esc(cur.provider_name) + dim +
      '） —— 记忆写入时生成向量，检索按余弦相似度排序，对话后自动提取记忆。' +
      (cur.is_verified ? '' :
        '<br><span style="color:var(--red)">⚠ 该模型的维度是<strong>手动填写</strong>的，尚未经过探测校验。' +
        '若实际维度和填写值不一致，向量检索会静默失效（相似度恒为 0，记忆像"搜不到"）。' +
        '建议点该行「测试」补做一次真实校验。</span>');
  }
  act.innerHTML = '<button class="btn btn-sm btn-ghost danger" onclick="clearCurrent()">关闭向量检索</button>';
}

function renderTable() {
  const tb = document.getElementById('embTbody');
  const hint = document.getElementById('mem_hint');

  if (!embModelsCache.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">还没有嵌入模型 —— 点右上角「＋ 新增嵌入模型」手动添加一个</td></tr>';
    hint.textContent = providersCache.length
      ? '提示：同一个厂商下可添加多个嵌入模型（例如一个大维度高质量、一个小维度低成本），随时切换「设为当前」。'
      : '提示：还没有配置任何厂商 —— 请先到「AI 管理」页添加厂商并填写 API Key，再回到本页新增嵌入模型。';
    return;
  }

  tb.innerHTML = embModelsCache.map(function(m) {
    const tags = [];
    if (m.is_current) tags.push('<span class="tag tag-cur">● 当前生效</span>');
    tags.push(m.is_active ? '<span class="tag tag-on">启用</span>' : '<span class="tag tag-off">停用</span>');
    if (m.is_current && !m.is_active) tags.push('<span class="tag tag-warn">⚠ 不生效</span>');

    const dim = m.dimensions
      ? (m.dimensions + ' 维' + (m.is_verified ? '' :
          ' <span style="color:var(--muted);font-size:0.85em">· 手动未验证</span>'))
      : '<span style="color:var(--muted)">未知</span>';
    const canSet = m.is_active && !m.is_current;

    return '<tr>' +
      '<td><strong>' + esc(m.display_name) + '</strong></td>' +
      '<td><code>' + esc(m.name) + '</code></td>' +
      '<td>' + esc(m.provider_name || ('#' + m.provider_id)) + '</td>' +
      '<td>' + dim + '</td>' +
      '<td>' + tags.join(' ') + '</td>' +
      '<td class="ops">' +
        (canSet ? '<button class="btn btn-sm btn-ghost" onclick="setCurrent(' + m.id + ')">设为当前</button>' : '') +
        '<button class="btn btn-sm btn-ghost" id="test_' + m.id + '" onclick="testModel(' + m.id + ')">测试</button>' +
        '<button class="btn btn-sm btn-ghost" onclick="openEditModal(' + m.id + ')">编辑</button>' +
        '<button class="btn btn-sm btn-ghost danger" onclick="deleteModel(' + m.id + ')">删除</button>' +
      '</td>' +
    '</tr>';
  }).join('');

  const activeCount = embModelsCache.filter(function(m){ return m.is_active; }).length;
  hint.textContent = '共 ' + embModelsCache.length + ' 个嵌入模型，其中 ' + activeCount +
    ' 个启用。「测试」会实调一次 /embeddings 并刷新实测维度（手动填的维度会在此转正）；' +
    '「设为当前」会先探针校验再写入生效配置。新增时若厂商暂时不可用，可直接手动填写维度先把配置建起来。';
}

// ═══ 新增 / 编辑 ═══

// 维度输入框的实时说明 —— 明确区分「自动探测」与「手动指定」两条路径，
// 免得管理员以为填了维度也会被探针校验。
function updateDimHint() {
  const el = document.getElementById('m_dim_hint');
  if (!el) return;
  const raw = (document.getElementById('m_dim').value || '').trim();
  if (raw === '') {
    el.innerHTML = '留空 → 保存时实调 <code>POST /embeddings</code> 自动探测真实维度，标记为「实测」。';
    el.style.color = 'var(--muted)';
    return;
  }
  const d = parseInt(raw, 10);
  if (!(d > 0) || d > 65536) {
    el.innerHTML = '⚠ 需为 1 ~ 65536 的整数（常见：1024 / 1536 / 2048 / 3072）。';
    el.style.color = 'var(--red)';
    return;
  }
  el.innerHTML = '手动指定 <strong>' + d + ' 维</strong> → 将<strong>跳过探针</strong>直接保存，标记为「手动填写 · 未验证」。' +
    '适用于厂商当前不可用（余额不足 / 限流 / 网络不通）时先把配置建起来；之后可点该行的「测试」补做校验转正。';
  el.style.color = 'var(--accent)';
}

function fillProviderSelect(selectedId) {
  const sel = document.getElementById('m_provider');
  // 启用中的厂商排前面，方便优先选到可用项
  const list = providersCache.slice().sort(function(a, b){
    return (b.is_active ? 1 : 0) - (a.is_active ? 1 : 0);
  });
  sel.innerHTML = list.map(function(p) {
    return '<option value="' + p.id + '"' + (p.id === selectedId ? ' selected' : '') + '>' +
      esc(p.name) + (p.is_active ? '' : '（已停用）') + ' · ' + esc(p.api_type) + '</option>';
  }).join('');
}

function openAddModal() {
  if (!providersCache.length) {
    toast('还没有任何厂商 —— 请先到「AI 管理」页添加厂商并配置 API Key', false);
    return;
  }
  editingId = null;
  document.getElementById('modelModalTitle').textContent = '新增嵌入模型';
  document.getElementById('m_name').value = '';
  document.getElementById('m_display').value = '';
  document.getElementById('m_dim').value = '';
  document.getElementById('m_active').checked = true;
  fillProviderSelect(providersCache[0] ? providersCache[0].id : null);
  const hint = document.getElementById('modelHint');
  hint.style.display = 'block';
  hint.innerHTML = '默认留空「向量维度」→ 保存时后端实调一次 <code>POST /embeddings</code> 探针校验，' +
    '不支持会直接拒绝，不会留下"配了却永远失败"的死条目。<br>' +
    '若厂商当前不可用（余额不足 / 限流）导致探针失败，可<strong>手动填写维度</strong>先把配置建起来，' +
    '之后再点「测试」补校验。';
  updateDimHint();
  document.getElementById('modelOverlay').classList.add('show');
  document.getElementById('m_name').focus();
}

function openEditModal(id) {
  const m = embModelsCache.filter(function(x){ return x.id === id; })[0];
  if (!m) return;
  editingId = id;
  document.getElementById('modelModalTitle').textContent = '编辑嵌入模型';
  fillProviderSelect(m.provider_id);
  document.getElementById('m_name').value = m.name;
  document.getElementById('m_display').value = m.display_name;
  document.getElementById('m_active').checked = !!m.is_active;
  // 维度输入框刻意留空：留空 = 不修改当前维度。否则若把现值回填进去，
  // 一提交就会被当成"手动指定"→ 把已实测的维度降级成"未验证"。
  document.getElementById('m_dim').value = '';
  const hint = document.getElementById('modelHint');
  hint.style.display = 'block';
  const curDim = m.dimensions
    ? (m.dimensions + ' 维' + (m.is_verified ? '（实测）' : '（手动填写，未验证）'))
    : '未知';
  hint.innerHTML = '当前维度：<strong>' + curDim + '</strong>。<br>' +
    '改动「厂商」或「模型标识」会自动重新探针校验（旧维度必然失效）；' +
    '仅改展示名 / 启用状态不会触发任何网络请求。<br>' +
    '需要手动改维度时，在「向量维度」填入数字（会跳过探针校验）。';
  updateDimHint();
  document.getElementById('modelOverlay').classList.add('show');
}

function closeModelModal() {
  document.getElementById('modelOverlay').classList.remove('show');
  editingId = null;
}

async function submitModelForm() {
  const providerId = parseInt(document.getElementById('m_provider').value) || 0;
  const name = document.getElementById('m_name').value.trim();
  const display = document.getElementById('m_display').value.trim() || name;
  const active = document.getElementById('m_active').checked;

  if (!providerId) { toast('请选择厂商', false); return; }
  if (!name) { toast('请填写模型标识（如 text-embedding-3-large）', false); return; }

  // 维度：留空 = 交给后端探针自动探测；填了数字 = 手动指定（后端会跳过探针）。
  const dimRaw = (document.getElementById('m_dim').value || '').trim();
  let manualDim = null;
  if (dimRaw !== '') {
    const d = parseInt(dimRaw, 10);
    if (!(d > 0) || d > 65536) { toast('向量维度需为 1 ~ 65536 的整数；留空则自动探测', false); return; }
    manualDim = d;
  }

  const isEdit = editingId !== null;
  const url = API + '/embedding-models' + (isEdit ? ('/' + editingId) : '');
  const body = { provider_id: providerId, name: name, display_name: display, is_active: active };
  if (manualDim !== null) body.dimensions = manualDim;

  const btn = document.getElementById('modelSubmitBtn');
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = manualDim !== null ? '保存中...' : '探针校验中...';
  try {
    const res = await fetch(url, {
      method: isEdit ? 'PUT' : 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (res.ok) {
      const saved = await res.json();
      const dimTxt = saved.dimensions
        ? ((saved.is_verified ? '实测 ' : '手动指定 ') + saved.dimensions + ' 维')
        : '已保存';
      toast((isEdit ? '已更新：' : '已新增：') + dimTxt +
        (saved.is_verified ? '' : '（未验证，可点该行「测试」补做校验）'), true);
      closeModelModal();
      await loadEmbeddingModels();
    } else {
      toast((isEdit ? '更新失败: ' : '新增失败: ') + (await readErr(res)), false);
    }
  } catch(e) {
    toast('保存失败: ' + e.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

// ═══ 设为当前 / 关闭 / 测试 / 删除 ═══

async function setCurrent(id) {
  const m = embModelsCache.filter(function(x){ return x.id === id; })[0];
  const label = m ? (m.display_name + '（' + m.name + '）') : ('id=' + id);
  const ok = await showConfirm('设为当前向量检索模型',
    '将把「' + label + '」设为记忆模块的向量检索模型。\\n\\n' +
    '默认会向该厂商实调一次 /embeddings 探针校验；若该模型不支持向量生成会被拒绝，当前配置保持不变。' +
    (m && !m.is_verified ? '\\n\\n注意：该模型的维度是手动填写的，尚未经过校验。' : '') +
    '\\n\\n只改这一个字段，不会动对话模型/Agent 配置。',
    {icon: '🧭', okText: '设为当前'});
  if (!ok) return;

  const r1 = await apiJson(API + '/embedding-models/current', 'PUT', { model_id: id });
  if (r1.ok) {
    toast((r1.data && r1.data.message) || '已设为当前向量检索模型', true);
    await loadEmbeddingModels();
    return;
  }

  // 探针没过 —— 给一条"仍然启用"的退路。用于厂商当前不可用（余额不足 / 限流 /
  // 网络不通）但管理员确认配置本身没错的场景；否则手动填维度建好的条目永远无法生效。
  const force = await showConfirm('探针校验未通过',
    '该模型未通过校验：\\n' + (r1.detail || ('HTTP ' + r1.status)) + '\\n\\n' +
    '可以选择「仍然启用」跳过校验直接生效 —— 适用于厂商当前确实不可用（如余额不足、限流）的情况。\\n\\n' +
    '代价：此刻无法确认它真能产出向量。生效后状态会标为「未验证」，' +
    '向量检索可能静默失效（相似度恒为 0）；等厂商恢复后请点该行「测试」补做校验。',
    {icon: '⚠', okText: '仍然启用'});
  if (!force) return;

  const r2 = await apiJson(API + '/embedding-models/current', 'PUT', { model_id: id, force: true });
  if (r2.ok) toast((r2.data && r2.data.message) || '已设为当前（未做探针校验）', true);
  else toast('设为当前失败: ' + (r2.detail || ('HTTP ' + r2.status)), false);
  await loadEmbeddingModels();
}

async function clearCurrent() {
  const ok = await showConfirm('关闭向量检索',
    '将清空当前生效的向量检索模型：记忆检索降级为关键词匹配，对话后不再自动提取记忆。\\n\\n' +
    '模型条目本身不会被删除，已生成的向量也保留在库中，之后可随时重新「设为当前」。\\n\\n' +
    '保存后即时生效。',
    {icon: '⚠', okText: '关闭向量检索'});
  if (!ok) return;
  await callJson(API + '/embedding-models/current', 'PUT', { model_id: 0 },
    '已关闭向量检索', '关闭失败');
}

async function testModel(id) {
  const btn = document.getElementById('test_' + id);
  const original = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '测试中...'; }
  try {
    const res = await fetch(API + '/embedding-models/' + id + '/test', { method: 'POST' });
    if (res.ok) {
      const d = await res.json();
      toast('可用 · 向量维度 ' + d.dimensions, true);
      await loadEmbeddingModels();
    } else {
      toast('测试失败: ' + (await readErr(res)), false);
    }
  } catch(e) {
    toast('测试失败: ' + e.message, false);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }
}

async function deleteModel(id) {
  const m = embModelsCache.filter(function(x){ return x.id === id; })[0];
  const label = m ? (m.display_name + '（' + m.name + '）') : ('id=' + id);
  const isCur = !!(m && m.is_current);
  const ok = await showConfirm('删除嵌入模型',
    '将删除「' + label + '」。\\n\\n' +
    (isCur ? '⚠ 它正是当前生效的模型 —— 删除后会自动清空该配置，记忆检索降级为关键词匹配。\\n\\n' : '') +
    '已生成的记忆向量不受影响，但之后无法再用该模型生成新向量。',
    {icon: '🗑', okText: '删除'});
  if (!ok) return;
  try {
    const res = await fetch(API + '/embedding-models/' + id, { method: 'DELETE' });
    if (res.ok) {
      const d = await res.json();
      toast(d.cleared_current ? '已删除，并已自动关闭向量检索' : '已删除', true);
      await loadEmbeddingModels();
    } else {
      toast('删除失败: ' + (await readErr(res)), false);
    }
  } catch(e) {
    toast('删除失败: ' + e.message, false);
  }
}

// 通用：发 JSON 请求 → 提示 → 刷新列表（失败时也刷新，保证 UI 与服务端一致）
async function callJson(url, method, body, okMsg, errPrefix) {
  try {
    const res = await fetch(url, {
      method: method,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (res.ok) {
      toast(okMsg, true);
      await loadEmbeddingModels();
      return true;
    }
    toast(errPrefix + ': ' + (await readErr(res)), false);
    await loadEmbeddingModels();
    return false;
  } catch(e) {
    toast(errPrefix + ': ' + e.message, false);
    await loadEmbeddingModels();
    return false;
  }
}

// 发一个 JSON 请求并返回结构化结果，**不弹任何提示** —— 由调用方决定怎么处理
// （典型场景：「设为当前」失败后要弹二次确认让用户选择"仍然启用"）。
async function apiJson(url, method, body) {
  try {
    const res = await fetch(url, {
      method: method,
      headers: {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.ok) {
      let data = {};
      try { data = await res.json(); } catch(e) { data = {}; }
      return { ok: true, status: res.status, data: data, detail: '' };
    }
    return { ok: false, status: res.status, data: null, detail: await readErr(res) };
  } catch(e) {
    return { ok: false, status: 0, data: null, detail: e.message };
  }
}

// ═══ 初始化 ═══
loadProviders().then(function(){ return loadEmbeddingModels(); });
</script>
</body>
</html>
"""

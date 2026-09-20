ADMIN_AI_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 管理</title>
<style>
  :root { --bg:#FCF9F2; --card:#fff; --text:#111; --muted:#726960; --accent:#986638; --line:#EBE5DB; --red:#c44; --green:#4a8; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:Inter,system-ui,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; }
  .container { max-width:1000px; margin:0 auto; padding:40px 20px; }
  h1 { font-size:1.3em; font-weight:600; margin-bottom:4px; }
  .sub { font-size:0.8em; color:var(--muted); margin-bottom:24px; }
  .nav-bar { display:flex; gap:16px; margin-bottom:24px; font-size:0.82em; }
  .nav-bar a { color:var(--accent); text-decoration:none; padding:4px 12px; border-radius:6px; border:1px solid var(--line); }
  .nav-bar a.current { color:#fff; background:var(--accent); border-color:var(--accent); }
  #toast { padding:10px 16px; border-radius:8px; margin-bottom:16px; font-size:0.85em; display:none; }
  #toast.show { display:block; }
  #toast.ok { background:rgba(152,102,56,0.1); color:var(--accent); }
  #toast.err { background:rgba(200,60,60,0.08); color:var(--red); }
  .btn { padding:6px 16px; font-size:0.82em; font-weight:500; border:none; border-radius:6px; cursor:pointer; font-family:inherit; white-space:nowrap; }
  .btn-sm { padding:3px 10px; font-size:0.7em; }
  .btn-primary { background:var(--accent); color:#fff; box-shadow:0 2px 0 rgba(0,0,0,0.15); }
  .btn-danger { background:rgba(200,60,60,0.08); color:var(--red); border:1px solid rgba(200,60,60,0.2); }
  .section-title { font-size:0.9em; font-weight:600; margin:24px 0 8px; color:var(--text); display:flex; align-items:center; justify-content:space-between; }
  .card { padding:16px; margin-bottom:16px; border:1px solid var(--line); border-radius:10px; background:var(--card); }
  .row-item { display:grid; grid-template-columns:1fr auto; gap:12px; align-items:center; padding:12px 0; border-bottom:1px solid var(--line); }
  .row-item:last-child { border-bottom:none; }
  .row-info { display:grid; gap:4px; }
  .row-name { font-weight:600; font-size:0.95em; display:flex; align-items:center; flex-wrap:wrap; gap:4px; }
  .row-meta { font-size:0.78em; color:var(--muted); }
  .badge { display:inline-block; padding:1px 8px; border-radius:4px; font-size:0.7em; }
  .badge-default { background:rgba(152,102,56,0.12); color:var(--accent); }
  .badge-inactive { background:rgba(200,60,60,0.08); color:var(--red); }
  .badge-provider { background:rgba(74,136,136,0.1); color:var(--green); }
  .form-row { display:grid; grid-template-columns:120px 1fr; gap:8px; align-items:center; margin-bottom:10px; }
  .form-row label { font-size:0.82em; color:var(--muted); }
  .form-row input, .form-row select { padding:6px 10px; border:1px solid var(--line); border-radius:6px; font-size:0.85em; font-family:inherit; }
  .form-row input:focus, .form-row select:focus { outline:none; border-color:var(--accent); }
  .form-check { display:flex; align-items:center; gap:6px; font-size:0.82em; color:var(--muted); }
  .size-checkboxes { display:flex; flex-wrap:wrap; gap:8px; }
  .size-checkboxes label { display:flex; align-items:center; gap:4px; font-size:0.8em; cursor:pointer; }
  .modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.3); display:none; align-items:center; justify-content:center; z-index:100; }
  .modal-overlay.show { display:flex; }
  .modal { background:var(--card); border-radius:12px; padding:24px; width:90%; max-width:520px; max-height:90vh; overflow-y:auto; }
  .modal h2 { font-size:1.1em; margin-bottom:16px; }
  .modal-actions { display:flex; gap:8px; justify-content:flex-end; margin-top:16px; }
  .empty { text-align:center; padding:30px; color:var(--muted); font-size:0.85em; }
  .stats-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px; }
  .stat-card { padding:16px; border:1px solid var(--line); border-radius:10px; text-align:center; background:var(--card); }
  .stat-value { font-size:1.8em; font-weight:700; color:var(--accent); }
  .stat-label { font-size:0.75em; color:var(--muted); margin-top:4px; }
  .visitor-table { width:100%; border-collapse:collapse; font-size:0.82em; }
  .visitor-table th { text-align:left; padding:8px 10px; border-bottom:2px solid var(--line); color:var(--muted); font-weight:600; font-size:0.78em; }
  .visitor-table td { padding:8px 10px; border-bottom:1px solid var(--line); }
  .visitor-table tr:hover td { background:rgba(152,102,56,0.03); }
  .vid-cell { font-family:monospace; font-size:0.78em; max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .badge-active { background:rgba(74,136,136,0.1); color:var(--green); }
  .badge-idle { background:rgba(200,200,200,0.15); color:var(--muted); }
  .tab-bar { display:flex; gap:0; margin-bottom:16px; border-bottom:2px solid var(--line); }
  .tab { padding:8px 20px; font-size:0.85em; font-weight:500; cursor:pointer; border:none; background:none; color:var(--muted); border-bottom:2px solid transparent; margin-bottom:-2px; font-family:inherit; }
  .tab.active { color:var(--accent); border-bottom-color:var(--accent); }
  .tab:hover { color:var(--accent); }
  @media(max-width:640px){ *{-webkit-tap-highlight-color:transparent} .btn{padding:10px 16px;font-size:0.9em} .btn-sm{padding:8px 12px;font-size:0.82em} .container{padding:20px 12px} .form-row{grid-template-columns:1fr} .nav-bar{flex-wrap:wrap} }
</style>
</head>
<body>
<div class="container">
  <h1>AI 管理</h1>
  <p class="sub">统一管理厂商 API Key 池与对话模型；生图能力由 media-router 技能提供</p>
  <div class="nav-bar">
    <a href="/admin">总览</a>
    <a href="/admin/ai" class="current">AI 管理</a>
    <a href="/admin/chat">对话配置</a>
    <a href="/admin/memory">AI 记忆</a>
    <a href="/admin/skills">Skills 技能</a>
    <a href="/admin/users">用户管理</a>
  </div>
  <div id="toast"></div>

  <div class="tab-bar">
    <button class="tab active" onclick="switchTab('providers')">厂商管理</button>
    <button class="tab" onclick="switchTab('chatmodels')">对话模型</button>
    <button class="tab" onclick="switchTab('agentstats')">Agent 统计</button>
    <button class="tab" onclick="switchTab('mediagen')">图像/视频配置</button>
  </div>

  <!-- 厂商管理 -->
  <div id="tab-providers">
    <div class="section-title">
      <span>API Key 池</span>
      <button class="btn btn-primary" onclick="openCreateProvider()">+ 新增厂商</button>
    </div>
    <div class="card">
      <div id="providerList"></div>
    </div>
  </div>

  <!-- 对话模型管理（自 /admin/chat 迁入） -->
  <div id="tab-chatmodels" style="display:none">
    <div class="info-banner" style="padding:12px 16px;background:rgba(152,102,56,0.06);border:1px solid rgba(152,102,56,0.15);border-radius:8px;font-size:0.8em;color:var(--muted);margin-bottom:16px;line-height:1.6">
      💡 对话模型独立管理，每个模型关联一个厂商（含 API Key）。配置的模型会展示在前端对话页面，访客可自主选择。改动即时生效，下次对话自动加载新配置。
    </div>
    <div class="section-title">
      <span>对话模型</span>
      <button class="btn btn-primary" onclick="openCreateChatModel()">+ 新增模型</button>
    </div>
    <div class="card">
      <div id="chatModelList"></div>
    </div>
  </div>

  <!-- Agent 运营统计（#9） -->
  <div id="tab-agentstats" style="display:none">
    <div class="section-title">
      <span>Agent 运营统计</span>
      <div style="display:flex;gap:6px">
        <button class="btn btn-sm" onclick="setStatsDays(7)">7 天</button>
        <button class="btn btn-sm" onclick="setStatsDays(30)">30 天</button>
        <button class="btn btn-sm" onclick="setStatsDays(90)">90 天</button>
        <button class="btn btn-sm" style="background:var(--line);color:var(--text)" onclick="loadAgentStats()">刷新</button>
      </div>
    </div>
    <p style="font-size:0.75em;color:var(--muted);margin:0 0 12px">
      明细记录仅保留 2 天（防止任务表膨胀），历史指标在每日清理前按天滚动汇总落账，自功能上线日起累计。
    </p>
    <div class="stats-grid" id="agentStatsCards"></div>
    <div style="display:grid;grid-template-columns:1.2fr 1fr;gap:16px;margin-top:16px">
      <div class="card">
        <div style="font-size:0.8em;font-weight:600;margin-bottom:8px">每日任务趋势（最近 30 天）</div>
        <div id="agentTrend"></div>
      </div>
      <div class="card"><div id="agentDists"></div></div>
    </div>
    <div class="section-title"><span>工具失败率</span></div>
    <div class="card">
      <table class="visitor-table">
        <thead><tr><th>工具</th><th>调用次数</th><th>失败次数</th><th>失败率</th></tr></thead>
        <tbody id="agentToolRows">
          <tr><td colspan="4" style="text-align:center;color:var(--muted);padding:20px">加载中...</td></tr>
        </tbody>
      </table>
    </div>
    <div class="section-title"><span>模型降级链</span></div>
    <div class="card" style="font-size:0.8em"><span id="agentSwitchInfo">加载中...</span></div>
    <div class="section-title"><span>活跃用户 Top 10</span></div>
    <div class="card">
      <table class="visitor-table">
        <thead><tr><th>用户</th><th>Agent 任务</th><th>失败</th><th>工具调用</th></tr></thead>
        <tbody id="agentUserRows">
          <tr><td colspan="4" style="text-align:center;color:var(--muted);padding:20px">加载中...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 图像/视频配置（media-router 模型池） -->
  <div id="tab-mediagen" style="display:none">
    <div class="info-banner" style="padding:12px 16px;background:rgba(152,102,56,0.06);border:1px solid rgba(152,102,56,0.15);border-radius:8px;font-size:0.8em;color:var(--muted);margin-bottom:16px;line-height:1.7">
      💡 生成图片/视频的能力由 <b>media-router 技能</b>的多模型池提供。点「打开配置页」启动本机配置界面，
      在其中添加图像/视频模型（填厂商、模型名、API Key，设为启用），保存后<b>立即生效</b> ——
      用户在 Agent 对话里说"生成一张图"即可调用，产物自动落到用户工作区。
      <br>· 支持文生图 / 图生图 / 文生视频 / 图生视频
      <br>· 多模型按优先级分档，同档加权随机，失败自动降级到下一候选
      <br>· 配置页仅监听本机回环地址（127.0.0.1），带一次性访问令牌，后端重启后需重新打开
    </div>
    <div class="section-title">
      <span>模型池配置</span>
      <div style="display:flex;gap:6px">
        <span id="mrWebStatus" style="font-size:0.78em;color:var(--muted);align-self:center"></span>
        <button class="btn btn-sm" style="background:var(--line);color:var(--text)" onclick="stopMrWeb()">停止</button>
        <button class="btn btn-primary" onclick="openMrWeb()" id="mrWebBtn">打开配置页</button>
      </div>
    </div>
    <div class="card" id="mrPoolCard">
      <div id="mrPoolInfo" style="font-size:0.8em;color:var(--muted)">加载模型池状态中...</div>
    </div>
  </div>

  <!-- 对话配置内容已迁至 /admin/chat -->
</div>

<div class="modal-overlay" id="providerModal">
  <div class="modal">
    <h2 id="providerModalTitle">新增厂商</h2>
    <input type="hidden" id="p_editId">
    <div class="form-row">
      <label>名称</label>
      <input type="text" id="p_name" placeholder="如 SiliconFlow / OpenAI">
    </div>
    <div class="form-row">
      <label>Base URL</label>
      <input type="text" id="p_base_url" placeholder="https://api.siliconflow.cn/v1">
    </div>
    <div class="form-row">
      <label>API Key</label>
      <input type="text" id="p_api_key" placeholder="sk-...">
    </div>
    <div class="form-row">
      <label>协议类型</label>
      <select id="p_api_type" style="padding:6px 10px;border:1px solid var(--line);border-radius:6px;font-size:0.85em;font-family:inherit">
        <option value="openai">OpenAI 兼容 (chat/completions)</option>
        <option value="anthropic">Anthropic (v1/messages)</option>
      </select>
    </div>
    <div class="form-row">
      <label>状态</label>
      <label class="form-check"><input type="checkbox" id="p_is_active" checked> 启用</label>
    </div>
    <div class="modal-actions">
      <button class="btn" style="background:var(--line);color:var(--text)" onclick="closeModal('providerModal')">取消</button>
      <button class="btn btn-primary" onclick="saveProvider()">保存</button>
    </div>
  </div>
</div>

<!-- 对话模型 Modal（自 /admin/chat 迁入） -->
<div class="modal-overlay" id="chatModelModal">
  <div class="modal" style="max-width:520px">
    <h2 id="chatModelModalTitle">新增对话模型</h2>
    <input type="hidden" id="cm_editId">
    <div class="form-row">
      <label>所属厂商</label>
      <div style="display:flex;gap:6px;align-items:center">
        <select id="cm_provider_id" style="flex:1"></select>
        <button class="btn btn-sm" style="background:rgba(152,102,56,0.08);color:var(--accent);border:1px solid rgba(152,102,56,0.2);white-space:nowrap" onclick="discoverModels()" id="discoverBtn">🔍 自动发现</button>
      </div>
    </div>
    <div id="discoveryResult" style="display:none;margin-bottom:12px;padding:12px;border:1px solid var(--line);border-radius:8px;background:rgba(152,102,56,0.03);max-height:240px;overflow-y:auto"></div>
    <div class="form-row">
      <label>模型标识</label>
      <input type="text" id="cm_name" placeholder="如 deepseek-chat, deepseek-reasoner">
    </div>
    <div class="form-row">
      <label>展示名称</label>
      <input type="text" id="cm_display_name" placeholder="如 DeepSeek 通用对话">
    </div>
    <div class="form-row">
      <label>状态</label>
      <div style="display:flex;gap:16px;flex-wrap:wrap">
        <label class="form-check"><input type="checkbox" id="cm_is_active" checked> 启用</label>
        <label class="form-check"><input type="checkbox" id="cm_is_default"> 默认</label>
        <label class="form-check"><input type="checkbox" id="cm_supports_vision"> 多模态（支持图片）</label>
      </div>
    </div>
    <div class="form-row">
      <label>上下文窗口</label>
      <div style="display:flex;gap:6px;align-items:center">
        <input type="number" id="cm_context_length" value="0" min="0" max="1048576" step="1024" style="flex:1">
      </div>
      <small style="color:var(--muted);font-size:12px">可手动修改；点击「自动发现」可自动填充。0 表示未探测，对话时也会自动获取</small>
    </div>
    <div class="form-row">
      <label>推理等级</label>
      <select id="cm_reasoning_effort" style="padding:6px 10px;border:1px solid var(--line);border-radius:6px;font-size:0.85em;font-family:inherit"></select>
      <small id="reasoning_effort_hint" style="color:var(--muted);font-size:12px">选择厂商后自动加载可用等级</small>
    </div>
    <div class="modal-actions">
      <button class="btn" style="background:var(--line);color:var(--text)" onclick="testChatModel()" id="cm_test_btn">测试连接</button>
      <button class="btn" style="background:var(--line);color:var(--text)" onclick="closeModal('chatModelModal')">取消</button>
      <button class="btn btn-primary" onclick="saveChatModel()">保存</button>
    </div>
  </div>
</div>

<script>
window.addEventListener('error',function(e){toast('JS错误: '+e.message,false)});
window.addEventListener('unhandledrejection',function(e){toast('请求失败: '+(e.reason&&e.reason.message||e.reason||'网络错误'),false)});
const API = '/api/v1/admin/ai';
let providersCache = [];

function toast(msg, ok) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'show ' + (ok ? 'ok' : 'err');
  setTimeout(() => t.className = '', 3000);
}

// 从失败响应里取出「人话」错误信息。
// 不要直接把 res.text() 拼进提示 —— FastAPI 返回的是 {"detail": "..."} 这种 JSON，
// 原样显示会带着花括号和转义引号，管理员看不懂；极端情况下（网关错误页）还可能是整页 HTML。
async function readErr(res) {
  try {
    const b = await res.json();
    if (b && typeof b.detail === 'string') return b.detail;
    if (Array.isArray(b && b.detail) && b.detail[0] && b.detail[0].msg) return b.detail[0].msg;
  } catch(e) {}
  return 'HTTP ' + res.status;
}

function switchTab(tab) {
  const tabs = ['providers','chatmodels','agentstats','mediagen'];
  tabs.forEach(t => {
    document.getElementById('tab-'+t).style.display = (t === tab) ? '' : 'none';
  });
  document.querySelectorAll('.tab').forEach((btn, i) => {
    btn.classList.toggle('active', tabs[i] === tab);
  });
  if (tab === 'agentstats') loadAgentStats();
  if (tab === 'mediagen') refreshMrStatus();
}

// ═══ 图像/视频配置（media-router）═══
async function refreshMrStatus() {
  try {
    const [st, pool] = await Promise.all([
      fetch(API + '/skills/media-router/web-config').then(r => r.json()),
      fetch(API + '/skills/media-router/web-config/pool').then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    document.getElementById('mrWebStatus').textContent = st.running ? '配置页运行中' : '配置页未启动';
    const info = document.getElementById('mrPoolInfo');
    if (pool && pool.image !== undefined) {
      info.innerHTML = '当前模型池：<b>' + pool.image + '</b> 个图像模型，<b>' + pool.video + '</b> 个视频模型' +
        ((pool.image + pool.video) === 0 ? ' —— 还没有配置任何模型，点右上「打开配置页」添加' : '');
    } else {
      info.textContent = '模型池状态不可用（media-router 技能未安装或未启用）';
    }
  } catch(e) {
    document.getElementById('mrWebStatus').textContent = '';
    document.getElementById('mrPoolInfo').textContent = '状态查询失败: ' + e.message;
  }
}

async function openMrWeb() {
  const btn = document.getElementById('mrWebBtn');
  if (btn) { btn.disabled = true; btn.textContent = '启动中…'; }
  try {
    const res = await fetch(API + '/skills/media-router/web-config/start', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return toast('启动配置页失败: ' + (data.detail || res.status), false);
    window.open(data.url, '_blank', 'noopener');
    toast('配置页已在新窗口打开（本机回环 + 一次性令牌）', true);
    refreshMrStatus();
  } catch(e) { toast('启动配置页失败: ' + e.message, false); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '打开配置页'; } }
}

async function stopMrWeb() {
  try {
    await fetch(API + '/skills/media-router/web-config/stop', { method: 'POST' });
    toast('配置页已停止', true);
    refreshMrStatus();
  } catch(e) { toast('停止失败: ' + e.message, false); }
}

// ═══ 厂商管理 ═══

async function loadProviders() {
  try {
    const res = await fetch(API + '/providers');
    const data = await res.json();
    providersCache = data;
    document.getElementById('providerList').innerHTML = data.length ? data.map(p => `
      <div class="row-item">
        <div class="row-info">
          <span class="row-name">${p.name}${!p.is_active ? '<span class="badge badge-inactive">已禁用</span>' : ''}<span class="badge badge-provider">${p.api_type || 'openai'}</span></span>
          <span class="row-meta">${p.base_url}</span>
          <span class="row-meta">Key: ${p.api_key_masked}</span>
        </div>
        <div style="display:flex;gap:6px">
          <button class="btn btn-sm" style="background:var(--line);color:var(--text)" onclick="openEditProvider(${p.id})">编辑</button>
          <button class="btn btn-sm btn-danger" onclick="delProvider(${p.id})">删除</button>
        </div>
      </div>
    `).join('') : '<div class="empty">暂无厂商，点击「新增厂商」添加 API Key</div>';
  } catch(e) { toast('加载厂商失败: ' + e.message, false); }
}

function openCreateProvider() {
  document.getElementById('providerModalTitle').textContent = '新增厂商';
  document.getElementById('p_editId').value = '';
  document.getElementById('p_name').value = '';
  document.getElementById('p_base_url').value = '';
  document.getElementById('p_api_key').value = '';
  document.getElementById('p_api_type').value = 'openai';
  document.getElementById('p_is_active').checked = true;
  document.getElementById('providerModal').classList.add('show');
}

async function openEditProvider(id) {
  const p = providersCache.find(x => x.id === id);
  if (!p) return toast('厂商不存在', false);
  document.getElementById('providerModalTitle').textContent = '编辑厂商';
  document.getElementById('p_editId').value = p.id;
  document.getElementById('p_name').value = p.name;
  document.getElementById('p_base_url').value = p.base_url;
  document.getElementById('p_api_key').value = '';
  document.getElementById('p_api_type').value = p.api_type || 'openai';
  document.getElementById('p_is_active').checked = p.is_active;
  document.getElementById('providerModal').classList.add('show');
}

async function saveProvider() {
  const id = document.getElementById('p_editId').value;
  const payload = {
    name: document.getElementById('p_name').value.trim(),
    base_url: document.getElementById('p_base_url').value.trim(),
    api_key: document.getElementById('p_api_key').value.trim(),
    api_type: document.getElementById('p_api_type').value,
    is_active: document.getElementById('p_is_active').checked,
  };
  if (!payload.name || !payload.base_url) { toast('请填写名称和 Base URL', false); return; }
  if (id && !payload.api_key) delete payload.api_key;
  else if (!payload.api_key) { toast('请填写 API Key', false); return; }
  try {
    const method = id ? 'PUT' : 'POST';
    const url = id ? API + '/providers/' + id : API + '/providers';
    const res = await fetch(url, { method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    if (res.ok) { toast(id ? '已更新' : '已创建', true); closeModal('providerModal'); loadProviders(); }
    else { toast('失败: ' + await readErr(res), false); }
  } catch(e) { toast('保存失败: ' + e.message, false); }
}

async function delProvider(id) {
  if (!confirm('删除此厂商将同时删除其下所有对话模型，使用这些模型的历史会话将回退到默认模型，确定？')) return;
  try {
    const res = await fetch(API + '/providers/' + id, { method: 'DELETE' });
    if (res.ok) {
      const data = await res.json().catch(function() { return {}; });
      if (data.removed_chat_models > 0) toast('已删除，同时移除 ' + data.removed_chat_models + ' 个对话模型', true);
      else toast('已删除', true);
      loadProviders(); loadChatModels();
    }
    else toast('删除失败', false);
  } catch(e) { toast('删除失败: ' + e.message, false); }
}

// ═══ 对话模型管理（自 /admin/chat 迁入） ═══

let chatModelsCache = [];
// 当前编辑/测试的实测支持档位缓存（null=未实测，用规则兜底；数组=实测值）
let chatModelSupportedEffortsCache = null;

// 对话模型专用的厂商下拉填充
function populateChatProviderSelect(selectedId) {
  const sel = document.getElementById('cm_provider_id');
  sel.innerHTML = providersCache.map(p =>
    `<option value="${p.id}" ${p.id === selectedId ? 'selected' : ''}>${p.name}</option>`
  ).join('');
  // 厂商变更后自动加载对应的推理等级（带当前模型名，按模型能力过滤档位）
  const nameInput = document.getElementById('cm_name');
  loadReasoningEfforts(sel.value, '', nameInput ? nameInput.value.trim() : '');
  sel.onchange = function() {
    loadReasoningEfforts(this.value, '', nameInput ? nameInput.value.trim() : '');
  };
}

// ═══ 推理等级 — 借鉴 DSH adapter.ts 的 REASONING_EFFORTS ═══
// 下拉框按 厂商 + 模型名 动态显示合法档位：
// - 智谱 glm-5.3+（始终思考）只显示 低/高/最大（不显示 关闭/中，选了会 400）
// - 其余模型按厂商/通用列表显示

async function loadReasoningEfforts(providerId, selectedEffort, modelName) {
  const sel = document.getElementById('cm_reasoning_effort');
  const hint = document.getElementById('reasoning_effort_hint');
  if (!providerId) {
    sel.innerHTML = '<option value="off">关闭</option>';
    return;
  }
  try {
    const mn = modelName ? '&model_name=' + encodeURIComponent(modelName) : '';
    const res = await fetch(API + '/reasoning-efforts?provider_id=' + providerId + mn);
    const data = await res.json();
    const efforts = data.efforts || [];
    // 当前选中值不在新列表（如模型名变化后）→ 自动落到第一个合法档位
    const cur = selectedEffort || '';
    const hasCur = efforts.some(e => e.value === cur);
    const pick = hasCur ? cur : (efforts.length > 0 ? efforts[0].value : 'off');
    sel.innerHTML = efforts.map(e =>
      `<option value="${e.value}" ${e.value === pick ? 'selected' : ''}>${e.label}</option>`
    ).join('');
    hint.textContent = '共 ' + efforts.length + ' 个等级';
  } catch(e) {
    sel.innerHTML = '<option value="off">关闭</option>';
    hint.textContent = '加载失败: ' + e.message;
  }
}

// 模型名输入变化 → 防抖重新加载推理等级（如输入 glm-5.3 后只显示低/高/最大）
(function setupModelNameEffortReload() {
  const nameInput = document.getElementById('cm_name');
  if (!nameInput) return;
  let t = null;
  nameInput.addEventListener('input', function() {
    if (t) clearTimeout(t);
    t = setTimeout(function() {
      const providerSel = document.getElementById('cm_provider_id');
      const effortSel = document.getElementById('cm_reasoning_effort');
      // 模型名变化后旧实测档位失效，清缓存走规则兜底
      chatModelSupportedEffortsCache = null;
      loadReasoningEfforts(
        providerSel ? providerSel.value : '',
        effortSel ? effortSel.value : '',
        nameInput.value.trim()
      );
    }, 400);
  });
})();

// 用实测档位填充下拉框（选中 selectedEffort，不在则取第一个）
function renderSupportedEfforts(supported, selectedEffort) {
  const sel = document.getElementById('cm_reasoning_effort');
  const hint = document.getElementById('reasoning_effort_hint');
  if (!supported || supported.length === 0) {
    sel.innerHTML = '<option value="off">关闭</option>';
    hint.textContent = '未探测到支持的推理等级';
    return;
  }
  const labels = { off: '关闭', minimal: '极低', low: '低', medium: '中', high: '高', xhigh: '超高', max: '最大' };
  const hasCur = supported.indexOf(selectedEffort) >= 0;
  const pick = hasCur ? selectedEffort : supported[0];
  sel.innerHTML = supported.map(v =>
    `<option value="${v}" ${v === pick ? 'selected' : ''}>${labels[v] || v}</option>`).join('');
  hint.textContent = '实测支持 ' + supported.length + ' 个等级';
}

async function loadChatModels() {
  try {
    const res = await fetch(API + '/chat-models');
    const data = await res.json();
    chatModelsCache = data;
    document.getElementById('chatModelList').innerHTML = data.length ? data.map(m => `
      <div class="row-item">
        <div class="row-info">
          <span class="row-name">${esc(m.display_name)}${m.is_default ? '<span class="badge badge-default">默认</span>' : ''}${!m.is_active ? '<span class="badge badge-inactive">已禁用</span>' : ''}${m.supports_vision ? '<span class="badge" style="background:rgba(74,136,136,0.1);color:var(--green)">多模态</span>' : ''}${m.context_length > 0 ? '<span class="badge" style="background:rgba(152,102,56,0.1);color:var(--accent)">' + (m.context_length / 1024).toFixed(0) + 'K</span>' : '<span class="badge" style="background:rgba(152,102,56,0.06);color:var(--muted)">未探测</span>'}<span class="badge badge-provider">${esc(m.provider_name)}</span></span>
          <span class="row-meta">模型: ${esc(m.name)} · 推理: ${esc(m.reasoning_effort || 'off')}</span>
        </div>
        <div style="display:flex;gap:6px">
          <button class="btn btn-sm" style="background:var(--line);color:var(--text)" onclick="openEditChatModel(${m.id})">编辑</button>
          <button class="btn btn-sm btn-danger" onclick="delChatModel(${m.id})">删除</button>
        </div>
      </div>
    `).join('') : '<div class="empty">暂无对话模型，点击「新增模型」添加</div>';
  } catch(e) { toast('加载对话模型失败: ' + e.message, false); }
}

function openCreateChatModel() {
  if (providersCache.length === 0) { toast('请先在厂商管理页签添加厂商', false); return; }
  document.getElementById('chatModelModalTitle').textContent = '新增对话模型';
  document.getElementById('cm_editId').value = '';
  document.getElementById('cm_name').value = '';
  document.getElementById('cm_display_name').value = '';
  document.getElementById('cm_is_active').checked = true;
  document.getElementById('cm_is_default').checked = false;
  document.getElementById('cm_supports_vision').checked = false;
  document.getElementById('cm_context_length').value = 0;
  document.getElementById('discoveryResult').style.display = 'none';
  document.getElementById('discoveryResult').innerHTML = '';
  chatModelSupportedEffortsCache = null;
  populateChatProviderSelect();
  document.getElementById('chatModelModal').classList.add('show');
}

function openEditChatModel(id) {
  const m = chatModelsCache.find(x => x.id === id);
  if (!m) return toast('模型不存在', false);
  document.getElementById('chatModelModalTitle').textContent = '编辑对话模型';
  document.getElementById('cm_editId').value = m.id;
  document.getElementById('cm_name').value = m.name;
  document.getElementById('cm_display_name').value = m.display_name;
  document.getElementById('cm_is_active').checked = m.is_active;
  document.getElementById('cm_is_default').checked = m.is_default;
  document.getElementById('cm_supports_vision').checked = m.supports_vision || false;
  document.getElementById('cm_context_length').value = m.context_length || 0;
  document.getElementById('discoveryResult').style.display = 'none';
  document.getElementById('discoveryResult').innerHTML = '';
  populateChatProviderSelect(m.provider_id);
  // 推理等级下拉：优先用模型已落库的实测档位（supported_efforts），无则规则兜底
  try {
    const se = m.supported_efforts ? JSON.parse(m.supported_efforts) : null;
    if (se && Array.isArray(se) && se.length) {
      chatModelSupportedEffortsCache = se;
      renderSupportedEfforts(se, m.reasoning_effort || 'off');
    } else {
      chatModelSupportedEffortsCache = null;
      loadReasoningEfforts(m.provider_id, m.reasoning_effort, m.name);
    }
  } catch(e) {
    chatModelSupportedEffortsCache = null;
    loadReasoningEfforts(m.provider_id, m.reasoning_effort, m.name);
  }
  document.getElementById('chatModelModal').classList.add('show');
}

async function saveChatModel() {
  const id = document.getElementById('cm_editId').value;
  const payload = {
    provider_id: parseInt(document.getElementById('cm_provider_id').value),
    name: document.getElementById('cm_name').value.trim(),
    display_name: document.getElementById('cm_display_name').value.trim(),
    is_active: document.getElementById('cm_is_active').checked,
    is_default: document.getElementById('cm_is_default').checked,
    supports_vision: document.getElementById('cm_supports_vision').checked,
    context_length: parseInt(document.getElementById('cm_context_length').value) || 0,
    reasoning_effort: document.getElementById('cm_reasoning_effort').value,
    supported_efforts: chatModelSupportedEffortsCache ? JSON.stringify(chatModelSupportedEffortsCache) : '',
  };
  if (!payload.name || !payload.display_name) { toast('请填写模型标识和展示名称', false); return; }
  try {
    const method = id ? 'PUT' : 'POST';
    const url = id ? API + '/chat-models/' + id : API + '/chat-models';
    const res = await fetch(url, { method, headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    if (res.ok) { toast(id ? '已更新' : '已创建', true); closeModal('chatModelModal'); loadChatModels(); }
    else { const err = await res.json(); toast(err.detail || '保存失败', false); }
  } catch(e) { toast('保存失败: ' + e.message, false); }
}

// ═══ 模型连通性测试 — 验证模型标识在厂商端点上真实可用 ═══

async function testChatModel() {
  const providerId = parseInt(document.getElementById('cm_provider_id').value);
  const name = document.getElementById('cm_name').value.trim();
  if (!providerId) { toast('请先选择厂商', false); return; }
  if (!name) { toast('请先填写模型标识', false); return; }

  const btn = document.getElementById('cm_test_btn');
  const originalText = btn.textContent;
  btn.textContent = '测试中...';
  btn.disabled = true;
  try {
    const res = await fetch(API + '/chat-models/test', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ provider_id: providerId, name: name }),
    });
    const data = await res.json();
    if (res.ok) {
      toast(data.message || '连接成功', true);
      // 实测推理等级（API 实际响应）→ 更新下拉框，只显示真实支持的档位
      if (data.efforts && data.efforts.length) {
        const supported = data.efforts.filter(e => e.supported);
        const supportedVals = supported.map(e => e.value);
        if (supportedVals.length) {
          chatModelSupportedEffortsCache = supportedVals;
          // 保留当前下拉选中值（若仍在实测列表内），否则落到第一个
          const curSel = document.getElementById('cm_reasoning_effort').value;
          renderSupportedEfforts(supportedVals, curSel);
          const hint = document.getElementById('reasoning_effort_hint');
          hint.textContent = '实测支持 ' + supportedVals.length + ' 个等级（' +
            supported.map(e => e.label).join('/') + '）';
        } else {
          chatModelSupportedEffortsCache = null;
          document.getElementById('cm_reasoning_effort').innerHTML = '<option value="off">关闭</option>';
          document.getElementById('reasoning_effort_hint').textContent = '未探测到支持的推理等级';
        }
      }
    } else toast(data.detail || '测试失败', false);
  } catch(e) { toast('测试失败: ' + e.message, false); }
  finally {
    btn.textContent = originalText;
    btn.disabled = false;
  }
}

async function delChatModel(id) {
  if (!confirm('确定删除此对话模型？使用该模型的历史会话将回退到默认模型，此操作不可撤销。')) return;
  try {
    const res = await fetch(API + '/chat-models/' + id, { method: 'DELETE' });
    if (res.ok) { toast('已删除', true); loadChatModels(); }
    else toast('删除失败', false);
  } catch(e) { toast('删除失败: ' + e.message, false); }
}

// ═══ DSH 风格模型自动发现 ═══
// 调用 Provider 的 GET /models 端点，自动获取可用模型及 context_window

async function discoverModels() {
  const providerId = parseInt(document.getElementById('cm_provider_id').value);
  if (!providerId) { toast('请先选择厂商', false); return; }

  const btn = document.getElementById('discoverBtn');
  const originalText = btn.textContent;
  btn.textContent = '探测中...';
  btn.disabled = true;
  const resultDiv = document.getElementById('discoveryResult');
  resultDiv.style.display = 'none';
  resultDiv.innerHTML = '';

  try {
    const res = await fetch(API + '/chat-models/discover', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ provider_id: providerId }),
    });
    const data = await res.json();
    if (!res.ok) {
      toast(data.detail || '探测失败', false);
      return;
    }
    if (!data.models || data.models.length === 0) {
      toast('该端点未返回任何模型', false);
      return;
    }

    // 展示探测到的模型列表，点击可自动填充
    resultDiv.innerHTML = '<div style="font-size:0.75em;color:var(--muted);margin-bottom:8px">探测到 ' + data.total + ' 个模型，点击可自动填充：</div>' +
      data.models.map(function(m) {
        var cw = m.context_window ? (m.context_window / 1024).toFixed(0) + 'K' : '未知';
        var name = m.name || m.id;
        var safeId = esc(m.id).replace(/"/g, '&quot;');
        var safeName = esc(name).replace(/"/g, '&quot;');
        return '<div class="discovery-item" data-id="' + safeId + '" data-name="' + safeName + '" data-cw="' + (m.context_window || 0) + '" ' +
          'style="padding:8px 10px;margin-bottom:4px;border:1px solid var(--line);border-radius:6px;cursor:pointer;transition:all 0.15s">' +
          '<div style="display:flex;justify-content:space-between;align-items:center">' +
            '<span style="font-weight:500;font-size:0.85em">' + esc(m.id) + '</span>' +
            '<span style="font-size:0.72em;color:var(--accent);background:rgba(152,102,56,0.1);padding:1px 6px;border-radius:4px">' + cw + '</span>' +
          '</div>' +
          (m.name ? '<div style="font-size:0.72em;color:var(--muted);margin-top:2px">' + esc(m.name) + '</div>' : '') +
        '</div>';
      }).join('');
    // 用事件委托绑定点击和悬停效果，避免内联 onclick 的引号转义问题
    resultDiv.querySelectorAll('.discovery-item').forEach(function(item) {
      item.addEventListener('click', function() {
        var id = this.getAttribute('data-id');
        var name = this.getAttribute('data-name');
        var cw = parseInt(this.getAttribute('data-cw')) || 0;
        selectDiscoveredModel(id, name, cw);
      });
      item.addEventListener('mouseover', function() {
        this.style.borderColor = 'var(--accent)';
        this.style.background = 'rgba(152,102,56,0.06)';
      });
      item.addEventListener('mouseout', function() {
        this.style.borderColor = 'var(--line)';
        this.style.background = '';
      });
    });
    resultDiv.style.display = 'block';
    toast('探测到 ' + data.total + ' 个模型', true);
  } catch(e) {
    toast('探测失败: ' + e.message, false);
  } finally {
    btn.textContent = originalText;
    btn.disabled = false;
  }
}

function selectDiscoveredModel(id, name, contextWindow) {
  document.getElementById('cm_name').value = id;
  document.getElementById('cm_display_name').value = name || id;
  if (contextWindow && contextWindow > 0) {
    document.getElementById('cm_context_length').value = contextWindow;
  }
  document.getElementById('discoveryResult').style.display = 'none';
  toast('已填充: ' + id + (contextWindow ? ' (' + (contextWindow / 1024).toFixed(0) + 'K)' : ''), true);
}

function esc(s) { if(!s) return ''; const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

function closeModal(id) {
  document.getElementById(id).classList.remove('show');
}

loadProviders();
loadChatModels();

// ═══ Agent 运营统计（#9） ═══

let statsDays = 30;
function setStatsDays(d) { statsDays = d; loadAgentStats(); }

async function loadAgentStats() {
  try {
    const res = await fetch(API + '/agent-stats?days=' + statsDays);
    if (!res.ok) { toast('加载 Agent 统计失败(' + res.status + ')', false); return; }
    const s = await res.json();
    const o = s.overview;

    // 概览卡片
    document.getElementById('agentStatsCards').innerHTML = `
      <div class="stat-card"><div class="stat-value">${o.agent_tasks}</div><div class="stat-label">Agent 任务</div></div>
      <div class="stat-card"><div class="stat-value">${o.chat_tasks}</div><div class="stat-label">纯聊天（对照）</div></div>
      <div class="stat-card"><div class="stat-value">${o.success_rate}%</div><div class="stat-label">成功率${o.failed ? ' · 失败 ' + o.failed : ''}</div></div>
      <div class="stat-card"><div class="stat-value">${o.avg_duration_s}s</div><div class="stat-label">平均时长</div></div>
      <div class="stat-card"><div class="stat-value">${o.avg_rounds}</div><div class="stat-label">平均轮数${o.hit_cap ? ' · 触顶 ' + o.hit_cap : ''}</div></div>
      <div class="stat-card"><div class="stat-value">${o.switch_total}</div><div class="stat-label">模型降级次数</div></div>
    `;

    // 每日趋势（堆叠条形图，最多 30 天）
    const daily = (s.daily || []).slice(-30);
    if (!daily.length) {
      document.getElementById('agentTrend').innerHTML = '<div class="empty">窗口内暂无任务（历史数据自功能上线日起按天累计）</div>';
    } else {
      const maxV = Math.max(1, ...daily.map(d => d.agent + d.chat));
      const h = (v) => v ? Math.max(2, Math.round(v / maxV * 84)) : 0;
      document.getElementById('agentTrend').innerHTML = `
        <div style="display:flex;align-items:flex-end;gap:2px;height:96px">
          ${daily.map(d => `
            <div style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;min-width:0"
                 title="${d.date}：Agent ${d.agent} · 纯聊天 ${d.chat}${d.failed ? ' · 失败 ' + d.failed : ''}">
              <div style="width:80%;height:${h(d.agent)}px;background:var(--accent);border-radius:2px 2px 0 0"></div>
              <div style="width:80%;height:${h(d.chat)}px;background:var(--line);border-radius:0 0 2px 2px"></div>
            </div>`).join('')}
        </div>
        <div style="display:flex;justify-content:space-between;font-size:0.7em;color:var(--muted);margin-top:6px">
          <span>${daily[0].date.slice(5)}</span>
          <span><span style="display:inline-block;width:9px;height:9px;background:var(--accent);border-radius:2px;vertical-align:-1px"></span> Agent
            <span style="display:inline-block;width:9px;height:9px;background:var(--line);border-radius:2px;vertical-align:-1px"></span> 纯聊天</span>
          <span>${daily[daily.length - 1].date.slice(5)}</span>
        </div>`;
    }

    // 时长 / 轮数分布
    const dists = [
      ['时长', [['<10s', s.durations.lt10s], ['10-60s', s.durations['10to60s']], ['1-5分', s.durations['1to5m']], ['5-20分', s.durations['5to20m']], ['>20分', s.durations.gt20m]]],
      ['轮数', [['0 轮', s.rounds.r0], ['1-3 轮', s.rounds.r1_3], ['4-10 轮', s.rounds.r4_10], ['11-20 轮', s.rounds.r11_20], ['21-39 轮', s.rounds.r21_39], ['触顶 40 轮', o.hit_cap]]],
    ];
    document.getElementById('agentDists').innerHTML = dists.map(([title, items]) => {
      const total = Math.max(1, items.reduce((a, [, v]) => a + v, 0));
      return `<div style="margin-bottom:14px">
        <div style="font-size:0.8em;font-weight:600;margin-bottom:6px">${title}分布</div>
        ${items.map(([label, v]) => `
          <div style="display:flex;align-items:center;gap:8px;font-size:0.75em;margin:3px 0">
            <span style="width:66px;color:var(--muted);flex-shrink:0">${label}</span>
            <div style="flex:1;background:var(--line);border-radius:3px;overflow:hidden;opacity:0.6">
              <div style="width:${Math.round((v || 0) / total * 100)}%;height:10px;background:var(--accent);opacity:1"></div>
            </div>
            <span style="width:44px;text-align:right;flex-shrink:0">${v || 0}</span>
          </div>`).join('')}
      </div>`;
    }).join('');

    // 工具失败率
    document.getElementById('agentToolRows').innerHTML = (s.tools || []).length ? s.tools.map(t => `
      <tr>
        <td>${t.tool}</td>
        <td>${t.calls}</td>
        <td>${t.failures}</td>
        <td><span class="badge ${t.failure_rate >= 20 ? 'badge-inactive' : 'badge-default'}">${t.failure_rate}%</span></td>
      </tr>`).join('') : '<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:20px">暂无工具调用记录</td></tr>';

    // 模型降级链
    const sw = s.switches || { by_status: {}, by_target: {} };
    document.getElementById('agentSwitchInfo').innerHTML = o.switch_total === 0
      ? '<span style="color:var(--muted)">窗口内未触发模型降级</span>'
      : `共 ${o.switch_total} 次 · 按状态码：${Object.entries(sw.by_status).map(([k, v]) => `${k} × ${v}`).join('、') || '-'}
         · 按备选模型：${Object.entries(sw.by_target).map(([k, v]) => `${k} × ${v}`).join('、') || '-'}`;

    // 活跃用户
    document.getElementById('agentUserRows').innerHTML = (s.top_users || []).length ? s.top_users.map(u => `
      <tr>
        <td title="user_id=${u.user_id}">${u.email || ('用户 ' + u.user_id)}</td>
        <td>${u.agent_tasks}</td>
        <td>${u.failed}</td>
        <td>${u.tool_calls}</td>
      </tr>`).join('') : '<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:20px">暂无数据</td></tr>';
  } catch(e) { toast('加载 Agent 统计失败: ' + e.message, false); }
}
</script>
</body>
</html>
"""

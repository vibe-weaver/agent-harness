"""Skills 技能管理页面 — 独立的管理端页面。

借鉴 DSH admin page 的设计风格，提供：
- 纯文本 skill 的 CRUD（名称 + 描述 + Markdown 内容）
- ZIP 目录上传（包含 SKILL.md + 参考文件）
- 启用/禁用切换
- 查看 skill 目录路径
"""

# 必须用 raw 字符串：页面内联 JS 里有 '\n' 这类字符串字面量，
# 非 raw 时 Python 会先把 \n 变成真实换行 → JS 字符串断行 → 整个 <script> 解析失败，
# 页面看似正常渲染但所有 JS（列表加载、按钮）全部失效。
SKILLS_ADMIN_PAGE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Skills 技能管理</title>
<style>
  :root { --bg:#FCF9F2; --card:#fff; --text:#111; --muted:#726960; --accent:#986638; --line:#EBE5DB; --red:#c44; --green:#4a8; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:Inter,system-ui,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; }
  .container { max-width:1000px; margin:0 auto; padding:40px 20px; }
  h1 { font-size:1.3em; font-weight:600; margin-bottom:4px; }
  .sub { font-size:0.8em; color:var(--muted); margin-bottom:24px; }
  .nav-bar { display:flex; gap:10px; margin-bottom:24px; font-size:0.82em; flex-wrap:wrap; }
  .nav-bar a { color:var(--accent); text-decoration:none; padding:4px 12px; border-radius:6px; border:1px solid var(--line); }
  .nav-bar a.current { color:#fff; background:var(--accent); border-color:var(--accent); }
  /* toast 要能承载长文案（厂商报错/诊断信息）：多行、可换行、限高可滚动、点击可关。
     单行 flex + 无换行控制时，长错误会横向溢出屏幕外，完全没法读。 */
  #toast { position:fixed; top:20px; left:50%; transform:translateX(-50%) translateY(-140%); padding:12px 18px; border-radius:8px; font-size:0.82em; display:flex; align-items:flex-start; gap:8px; z-index:9999; opacity:0; transition:all 0.24s ease; pointer-events:none; max-width:min(720px,92vw); max-height:52vh; overflow-y:auto; box-shadow:0 6px 24px rgba(0,0,0,0.14); text-align:left; line-height:1.65; word-break:break-word; overflow-wrap:anywhere; }
  #toast.show { transform:translateX(-50%) translateY(0); opacity:1; pointer-events:auto; cursor:pointer; }
  #toast.ok { background:rgba(152,102,56,0.12); color:var(--accent); border:1px solid rgba(152,102,56,0.2); }
  #toast.err { background:rgba(200,60,60,0.08); color:var(--red); border:1px solid rgba(200,60,60,0.22); }
  #toast .toast-icon { font-size:1.1em; line-height:1.4; flex-shrink:0; }
  #toast .toast-text { min-width:0; }
  .btn { padding:6px 16px; font-size:0.82em; font-weight:500; border:none; border-radius:6px; cursor:pointer; font-family:inherit; white-space:nowrap; }
  .btn-sm { padding:3px 10px; font-size:0.7em; }
  .btn-primary { background:var(--accent); color:#fff; box-shadow:0 2px 0 rgba(0,0,0,0.15); }
  .btn-danger { background:rgba(200,60,60,0.08); color:var(--red); border:1px solid rgba(200,60,60,0.2); }
  .section-title { font-size:0.9em; font-weight:600; margin:24px 0 8px; color:var(--text); display:flex; align-items:center; justify-content:space-between; }
  .card { padding:16px; margin-bottom:16px; border:1px solid var(--line); border-radius:10px; background:var(--card); }
  .badge { display:inline-block; padding:1px 8px; border-radius:4px; font-size:0.7em; }
  .badge-inactive { background:rgba(200,60,60,0.08); color:var(--red); }
  .badge-dir { background:rgba(74,136,136,0.1); color:var(--green); }
  .badge-cat { background:rgba(70,110,200,0.1); color:#4a6ec8; }
  .form-row { display:grid; grid-template-columns:120px 1fr; gap:8px; align-items:center; margin-bottom:10px; }
  .form-row label { font-size:0.82em; color:var(--muted); }
  .form-row input, .form-row select { padding:6px 10px; border:1px solid var(--line); border-radius:6px; font-size:0.85em; font-family:inherit; }
  .form-row input:focus, .form-row select:focus { outline:none; border-color:var(--accent); }
  .form-row textarea { width:100%; padding:6px 10px; border:1px solid var(--line); border-radius:6px; font-size:0.85em; font-family:inherit; resize:vertical; min-height:80px; }
  .form-check { display:flex; align-items:center; gap:6px; font-size:0.82em; color:var(--muted); }
  .skill-card { padding:14px 16px; border:1px solid var(--line); border-radius:8px; margin-bottom:8px; display:grid; grid-template-columns:1fr auto; gap:12px; align-items:center; }
  .skill-card .skill-info { display:grid; gap:4px; }
  .skill-card .skill-name { font-weight:600; font-size:0.9em; display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
  .skill-card .skill-desc { font-size:0.78em; color:var(--muted); }
  .skill-card .skill-meta { font-size:0.7em; color:var(--muted); }
  .modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.3); display:none; align-items:center; justify-content:center; z-index:200; opacity:0; transition:opacity 0.2s ease; }
  .modal-overlay.show { display:flex; opacity:1; }
  .modal { background:var(--card); border-radius:12px; padding:24px; width:90%; max-width:640px; max-height:90vh; overflow-y:auto; box-shadow:0 8px 32px rgba(0,0,0,0.15); transform:scale(0.95); transition:transform 0.2s ease; }
  .modal-overlay.show .modal { transform:scale(1); }
  .modal h2 { font-size:1.1em; margin-bottom:16px; }
  .modal-actions { display:flex; gap:8px; justify-content:flex-end; margin-top:16px; }
  .confirm-modal { max-width:400px; text-align:center; }
  .confirm-modal .confirm-icon { font-size:2.4em; line-height:1; margin-bottom:12px; }
  .confirm-modal h2 { margin-bottom:8px; }
  .confirm-modal .confirm-desc { font-size:0.85em; color:var(--muted); line-height:1.6; margin-bottom:20px; white-space:pre-wrap; }
  .empty { text-align:center; padding:30px; color:var(--muted); font-size:0.85em; }
  .info-banner { padding:12px 16px; background:rgba(152,102,56,0.06); border:1px solid rgba(152,102,56,0.15); border-radius:8px; font-size:0.8em; color:var(--muted); margin-bottom:20px; line-height:1.6; }
  .upload-zone { border:2px dashed var(--line); border-radius:10px; padding:30px; text-align:center; cursor:pointer; transition:all 0.2s; margin-bottom:16px; }
  .upload-zone:hover { border-color:var(--accent); background:rgba(152,102,56,0.03); }
  .upload-zone.dragover { border-color:var(--accent); background:rgba(152,102,56,0.08); }
  .upload-zone .upload-icon { font-size:2em; margin-bottom:8px; }
  .upload-zone .upload-text { font-size:0.85em; color:var(--muted); }
  #fileInput { display:none; }
  @media(max-width:640px){ *{-webkit-tap-highlight-color:transparent} .btn{padding:10px 16px;font-size:0.9em} .btn-sm{padding:8px 12px;font-size:0.82em} .container{padding:20px 12px} .form-row{grid-template-columns:1fr} .nav-bar{flex-wrap:wrap} }
</style>
</head>
<body>
<div class="container">
  <h1>Skills 技能管理</h1>
  <p class="sub">管理 Agent 可用的技能指令 — 纯文本编辑或 ZIP 目录上传</p>
  <div class="nav-bar">
    <a href="/admin">总览</a>
    <a href="/admin/ai">AI 管理</a>
    <a href="/admin/chat">对话配置</a>
    <a href="/admin/memory">AI 记忆</a>
    <a href="/admin/skills" class="current">Skills 技能</a>
    <a href="/admin/users">用户管理</a>
  </div>
  <div id="toast"><span class="toast-icon"></span><span class="toast-text"></span></div>

  <div class="info-banner">
    💡 技能（Skill）是一套可复用的任务特定指令，借鉴 DSH 的 skill 系统。<br><br>
    <b>工作原理：</b>当用户提问时，Agent 会看到所有活跃技能的摘要列表（名称+描述）。如果问题匹配某个技能，Agent 会调用 <code>skill</code> 工具加载该技能的完整指令，然后按指令步骤解决问题。<br><br>
    <b>两种创建方式：</b><br>
    1. 纯文本编辑 — 直接填写名称、描述和 Markdown 内容<br>
    2. ZIP 目录上传 — 上传包含 SKILL.md 的目录（可附带参考文件），系统自动解析 frontmatter 中的 name 和 description
  </div>

  <!-- 上传区域 -->
  <div class="section-title">
    <span>上传 Skill 目录（ZIP）</span>
  </div>
  <div class="card">
    <div class="upload-zone" id="uploadZone" onclick="document.getElementById('fileInput').click()">
      <div class="upload-icon">📦</div>
      <div class="upload-text">点击或拖拽 ZIP 文件到此处上传</div>
      <div class="upload-text" style="margin-top:4px;font-size:0.75em">ZIP 应包含 SKILL.md（带 YAML frontmatter）+ 可选参考文件</div>
    </div>
    <input type="file" id="fileInput" accept=".zip">
    <div class="form-row" style="margin-top:8px">
      <label>自定义名称</label>
      <input type="text" id="upload_skill_name" placeholder="留空则从文件名推断（如 my-skill）">
    </div>
    <div class="form-row">
      <label>技能包</label>
      <input type="text" id="upload_pack" placeholder="留空=独立技能；填包名则放入技能包（如 asu）">
    </div>
    <div class="form-row">
      <label>分类</label>
      <input type="text" id="upload_category" list="sk_category_list" autocomplete="off" placeholder="留空=不改动已有分类；如 写作 / 求职 / 演示">
    </div>
    <div class="form-row">
      <label>启用状态</label>
      <label class="form-check"><input type="checkbox" id="upload_is_active" checked> 启用此技能</label>
    </div>
    <div style="margin-top:8px">
      <button class="btn btn-primary" onclick="uploadSkill()">上传并导入</button>
    </div>
    <div id="uploadWarnings" style="display:none;margin-top:12px"></div>
  </div>

  <!-- 整仓库一键导入（优化6） -->
  <div class="section-title">
    <span>整仓库一键导入（ZIP）</span>
  </div>
  <div class="card">
    <div class="upload-zone" onclick="document.getElementById('repoFileInput').click()">
      <div class="upload-icon">🚀</div>
      <div class="upload-text">点击或拖拽多技能仓库完整 ZIP（如 ASu-skills 整个目录的压缩包）</div>
      <div class="upload-text" style="margin-top:4px;font-size:0.75em">自动完成：共享资源解压到包根 + 注册全部技能（skills/&lt;name&gt;/SKILL.md），逐技能报告引用警告</div>
    </div>
    <input type="file" id="repoFileInput" accept=".zip">
    <div class="form-row" style="margin-top:8px">
      <label>技能包名</label>
      <input type="text" id="repo_upload_name" placeholder="必填，如 asu">
    </div>
    <div class="form-row">
      <label>分类</label>
      <input type="text" id="repo_upload_category" list="sk_category_list" autocomplete="off" placeholder="批量打到本次导入的全部技能；留空=不改动已有分类">
    </div>
    <div style="margin-top:8px">
      <button class="btn btn-primary" onclick="uploadRepo()">一键导入</button>
    </div>
    <div id="repoResult" style="display:none;margin-top:12px"></div>
  </div>

  <!-- 技能包共享资源上传 -->
  <div class="section-title">
    <span>上传技能包共享资源（ZIP）</span>
  </div>
  <div class="card">
    <div class="upload-zone" onclick="document.getElementById('packFileInput').click()">
      <div class="upload-icon">🗂</div>
      <div class="upload-text">点击或拖拽共享资源 ZIP（assets/ scripts/ 等公共目录）</div>
      <div class="upload-text" style="margin-top:4px;font-size:0.75em">解压到 data/skills/&lt;pack&gt;/ 包根，包内技能用 ../ 或 @pack/ 访问；支持整仓库 ZIP（带顶层目录会自动展开）</div>
    </div>
    <input type="file" id="packFileInput" accept=".zip">
    <div class="form-row" style="margin-top:8px">
      <label>技能包名</label>
      <input type="text" id="pack_upload_name" placeholder="必填，如 asu">
    </div>
    <div style="margin-top:8px">
      <button class="btn btn-primary" onclick="uploadPackResources()">上传包资源</button>
    </div>
  </div>

  <!-- Skill 列表 -->
  <div class="section-title">
    <span>已有技能</span>
    <button class="btn btn-primary" onclick="openCreateSkill()">+ 新增技能（纯文本）</button>
  </div>
  <div class="card">
    <div id="skillList"></div>
  </div>
</div>

<!-- 分类候选（三处分类输入框共用；置于顶层，避免弹窗 display:none 时取不到候选） -->
<datalist id="sk_category_list"></datalist>

<!-- 确认弹窗 -->
<div class="modal-overlay" id="confirmOverlay">
  <div class="modal confirm-modal">
    <div class="confirm-icon" id="confirmIcon">⚠</div>
    <h2 id="confirmTitle">确认</h2>
    <p class="confirm-desc" id="confirmDesc"></p>
    <div class="modal-actions" style="justify-content:center">
      <button class="btn" style="background:var(--line);color:var(--text)" onclick="_confirmCancel()" id="confirmCancelBtn">取消</button>
      <button class="btn btn-primary" onclick="_confirmOk()" id="confirmOkBtn">确定</button>
    </div>
  </div>
</div>

<!-- Skill Modal -->
<div class="modal-overlay" id="skillModal">
  <div class="modal" style="max-width:640px">
    <h2 id="skillModalTitle">新增技能</h2>
    <input type="hidden" id="sk_editId">
    <div class="form-row">
      <label>名称</label>
      <input type="text" id="sk_name" placeholder="如 translation, code-review" style="font-family:monospace">
    </div>
    <div class="form-row">
      <label>简短描述</label>
      <input type="text" id="sk_description" placeholder="一句话描述技能用途，Agent 看到后会决定是否加载">
    </div>
    <div class="form-row" style="grid-template-columns:120px 1fr">
      <label>SKILL.md 内容</label>
      <span style="font-size:0.72em;color:var(--muted)">Markdown 格式，描述技能的触发条件和执行步骤</span>
    </div>
    <div class="form-row" style="grid-template-columns:1fr">
      <textarea id="sk_content" rows="12" placeholder="## 角色与目标&#10;...&#10;&#10;## 触发条件&#10;...&#10;&#10;## 执行步骤&#10;1. ...&#10;2. ..." style="width:100%;padding:8px 12px;border:1px solid var(--line);border-radius:8px;font-size:0.82em;font-family:monospace;resize:vertical;min-height:240px"></textarea>
    </div>
    <div class="form-row">
      <label>技能包</label>
      <input type="text" id="sk_pack" placeholder="留空=独立技能；目录形式技能可填包名（如 asu）" style="font-family:monospace">
    </div>
    <div class="form-row">
      <label>分类</label>
      <input type="text" id="sk_category" list="sk_category_list" autocomplete="off" placeholder="留空=未分类；如 写作 / 求职 / 演示，前端技能面板按此下拉筛选">
    </div>
    <div class="form-row">
      <label>状态</label>
      <label class="form-check"><input type="checkbox" id="sk_is_active" checked> 启用</label>
    </div>
    <div class="modal-actions">
      <button class="btn" style="background:var(--line);color:var(--text)" onclick="closeModal('skillModal')">取消</button>
      <button class="btn btn-primary" onclick="saveSkill()">保存</button>
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
  const icon = t.querySelector('.toast-icon');
  const text = t.querySelector('.toast-text');
  icon.textContent = ok ? '✓' : '✕';
  text.textContent = msg;
  t.className = 'show ' + (ok ? 'ok' : 'err');
  if (_toastTimer) clearTimeout(_toastTimer);
  // 长文案（诊断信息）需要更久的阅读时间，按长度动态延长，上限 12 秒。
  const dur = ok ? 3000 : Math.min(12000, 4500 + String(msg || '').length * 45);
  _toastTimer = setTimeout(() => t.className = '', dur);
}
// 点一下即可关掉（错误信息常常很长，用户看完想立刻收起）
document.getElementById('toast').addEventListener('click', function(){
  this.className = '';
  if (_toastTimer) clearTimeout(_toastTimer);
});

// ═══ 自定义确认弹窗 ═══
let _confirmResolve = null;
function showConfirm(title, desc, opts) {
  opts = opts || {};
  return new Promise(function(resolve) {
    _confirmResolve = resolve;
    document.getElementById('confirmTitle').textContent = title || '确认';
    document.getElementById('confirmDesc').textContent = desc || '';
    document.getElementById('confirmIcon').textContent = opts.icon || '⚠';
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

function closeModal(id) {
  document.getElementById(id).classList.remove('show');
}

function esc(s) { if(!s) return ''; const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

// ═══ Skill 列表加载 ═══
let skillsCache = [];

async function loadSkills() {
  try {
    const res = await fetch(API + '/skills');
    const data = await res.json();
    skillsCache = data;
    // 上传卡片的分类输入框在页面加载后就可见，候选需随列表一起就绪
    refreshCategoryDatalist();
    document.getElementById('skillList').innerHTML = data.length ? data.map(s => `
      <div class="skill-card">
        <div class="skill-info">
          <span class="skill-name">${esc(s.name)}${s.is_active ? '' : '<span class="badge badge-inactive">已禁用</span>'}${s.dir_path ? '<span class="badge badge-dir">目录</span>' : ''}${s.pack ? '<span class="badge badge-dir">包: ' + esc(s.pack) + '</span>' : ''}${s.category ? '<span class="badge badge-cat">' + esc(s.category) + '</span>' : ''}</span>
          <span class="skill-desc">${esc(s.description)}</span>
          ${s.dir_path ? '<span class="skill-meta">目录: ' + esc(s.dir_path) + '</span>' : ''}
        </div>
        <div style="display:flex;gap:6px">
          ${s.dir_path ? '<button class="btn btn-sm" style="background:var(--line);color:var(--text)" onclick="lintSkillNow(' + s.id + ')">检查</button>' : ''}
          <button class="btn btn-sm" style="background:var(--line);color:var(--text)" onclick="openEditSkill(${s.id})">编辑</button>
          <button class="btn btn-sm btn-danger" onclick="delSkill(${s.id})">删除</button>
        </div>
      </div>
    `).join('') : '<div class="empty">暂无技能，点击「新增技能」或上传 ZIP 目录添加</div>';
  } catch(e) { toast('加载技能失败: ' + e.message, false); }
}

// ═══ media-router 配置入口已迁至 /admin/ai 的「图像/视频配置」tab ═══

async function lintSkillNow(id) {
  try {
    const res = await fetch(API + '/skills/' + id + '/lint');
    const warns = await res.json();
    if (!res.ok) return toast('检查失败: ' + (warns.detail || ''), false);
    if (!warns.length) return toast('引用检查通过，未发现问题', true);
    await showConfirm('引用健康检查（' + warns.length + ' 条警告）', warns.join('\n'), {icon:'🔍', okText:'知道了', cancelText:'关闭'});
  } catch(e) { toast('检查失败: ' + e.message, false); }
}

// 分类候选：从已有技能收集去重后填入 datalist，便于复用同一写法（避免"写作"/"写作类"分裂）
function refreshCategoryDatalist() {
  const dl = document.getElementById('sk_category_list');
  if (!dl) return;
  const cats = [...new Set(skillsCache.map(s => (s.category || '').trim()).filter(Boolean))].sort();
  dl.innerHTML = cats.map(c => '<option value="' + esc(c).replace(/"/g, '&quot;') + '"></option>').join('');
}

function openCreateSkill() {
  document.getElementById('skillModalTitle').textContent = '新增技能';
  document.getElementById('sk_editId').value = '';
  document.getElementById('sk_name').value = '';
  document.getElementById('sk_description').value = '';
  document.getElementById('sk_content').value = '';
  document.getElementById('sk_pack').value = '';
  document.getElementById('sk_category').value = '';
  refreshCategoryDatalist();
  document.getElementById('sk_is_active').checked = true;
  document.getElementById('skillModal').classList.add('show');
}

function openEditSkill(id) {
  const s = skillsCache.find(x => x.id === id);
  if (!s) return toast('技能不存在', false);
  document.getElementById('skillModalTitle').textContent = '编辑技能';
  document.getElementById('sk_editId').value = s.id;
  document.getElementById('sk_name').value = s.name;
  document.getElementById('sk_description').value = s.description;
  document.getElementById('sk_content').value = s.content;
  document.getElementById('sk_pack').value = s.pack || '';
  document.getElementById('sk_category').value = s.category || '';
  refreshCategoryDatalist();
  document.getElementById('sk_is_active').checked = s.is_active;
  document.getElementById('skillModal').classList.add('show');
}

async function saveSkill() {
  const id = document.getElementById('sk_editId').value;
  const payload = {
    name: document.getElementById('sk_name').value.trim(),
    description: document.getElementById('sk_description').value.trim(),
    content: document.getElementById('sk_content').value.trim(),
    pack: document.getElementById('sk_pack').value.trim(),
    category: document.getElementById('sk_category').value.trim(),
    is_active: document.getElementById('sk_is_active').checked,
  };
  if (!payload.name || !payload.description || !payload.content) {
    toast('请填写所有字段', false); return;
  }
  try {
    const method = id ? 'PUT' : 'POST';
    const url = id ? API + '/skills/' + id : API + '/skills';
    const res = await fetch(url, { method, headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    if (res.ok) {
      const data = await res.json();
      if (data.warnings && data.warnings.length) {
        toast('已保存，但发现 ' + data.warnings.length + ' 条引用警告', false);
        await showConfirm('引用健康检查（' + data.warnings.length + ' 条警告）', data.warnings.join('\n'), {icon:'🔍', okText:'知道了', cancelText:'关闭'});
      } else {
        toast(id ? '已更新' : '已创建', true);
      }
      closeModal('skillModal'); loadSkills();
    }
    else { const err = await res.json(); toast(err.detail || '保存失败', false); }
  } catch(e) { toast('保存失败: ' + e.message, false); }
}

async function delSkill(id) {
  const ok = await showConfirm('删除技能', '确定删除此技能？此操作不可撤销，目录形式的技能会同时删除磁盘文件。', {icon:'🗑', okText:'删除'});
  if (!ok) return;
  try {
    const res = await fetch(API + '/skills/' + id, { method: 'DELETE' });
    if (res.ok) { toast('已删除', true); loadSkills(); }
    else toast('删除失败', false);
  } catch(e) { toast('删除失败: ' + e.message, false); }
}

// ═══ ZIP 上传 ═══
const uploadZone = document.getElementById('uploadZone');
const fileInput = document.getElementById('fileInput');

fileInput.addEventListener('change', function() {
  if (this.files.length > 0) {
    // 不自动上传，等用户点按钮
    uploadZone.querySelector('.upload-text').textContent = '已选择: ' + this.files[0].name;
  }
});

uploadZone.addEventListener('dragover', function(e) {
  e.preventDefault();
  this.classList.add('dragover');
});
uploadZone.addEventListener('dragleave', function(e) {
  e.preventDefault();
  this.classList.remove('dragover');
});
uploadZone.addEventListener('drop', function(e) {
  e.preventDefault();
  this.classList.remove('dragover');
  if (e.dataTransfer.files.length > 0) {
    fileInput.files = e.dataTransfer.files;
    uploadZone.querySelector('.upload-text').textContent = '已选择: ' + e.dataTransfer.files[0].name;
  }
});

async function uploadSkill() {
  if (!fileInput.files || fileInput.files.length === 0) {
    toast('请先选择 ZIP 文件', false);
    return;
  }

  const file = fileInput.files[0];
  const formData = new FormData();
  formData.append('file', file);

  const skillName = document.getElementById('upload_skill_name').value.trim();
  const packName = document.getElementById('upload_pack').value.trim();
  const categoryName = document.getElementById('upload_category').value.trim();
  const isActive = document.getElementById('upload_is_active').checked;

  // 使用 URL 参数传递 skill_name / pack / category / is_active
  const params = new URLSearchParams();
  if (skillName) params.append('skill_name', skillName);
  if (packName) params.append('pack', packName);
  if (categoryName) params.append('category', categoryName);
  params.append('is_active', isActive);

  const warnBox = document.getElementById('uploadWarnings');
  warnBox.style.display = 'none';
  warnBox.innerHTML = '';

  try {
    const res = await fetch(API + '/skills/upload?' + params.toString(), {
      method: 'POST',
      body: formData,
    });
    if (res.ok) {
      const data = await res.json();
      toast('技能 "' + data.name + '" 上传成功', true);
      if (data.warnings && data.warnings.length) {
        warnBox.style.display = 'block';
        warnBox.innerHTML = '<div style="font-size:0.8em;color:var(--red);font-weight:600;margin-bottom:4px">⚠ 引用健康检查发现 ' + data.warnings.length + ' 条警告</div>'
          + '<div style="font-size:0.75em;color:var(--muted);white-space:pre-wrap">' + esc(data.warnings.join('\n')) + '</div>';
      }
      fileInput.value = '';
      uploadZone.querySelector('.upload-text').textContent = '点击或拖拽 ZIP 文件到此处上传';
      document.getElementById('upload_skill_name').value = '';
      loadSkills();
    } else {
      // 先取文本，再尝试解析 JSON
      const text = await res.text();
      let msg = '上传失败';
      try {
        const err = JSON.parse(text);
        msg = err.detail || msg;
      } catch(_) {
        msg = text || ('上传失败 (HTTP ' + res.status + ')');
      }
      toast(msg, false);
    }
  } catch(e) {
    toast('上传失败: ' + e.message, false);
  }
}

async function uploadPackResources() {
  const packInput = document.getElementById('packFileInput');
  if (!packInput.files || packInput.files.length === 0) {
    toast('请先选择 ZIP 文件', false);
    return;
  }
  const packName = document.getElementById('pack_upload_name').value.trim();
  if (!packName) {
    toast('请填写技能包名', false);
    return;
  }

  const formData = new FormData();
  formData.append('file', packInput.files[0]);

  const params = new URLSearchParams();
  params.append('pack', packName);

  try {
    const res = await fetch(API + '/skills/upload-pack?' + params.toString(), {
      method: 'POST',
      body: formData,
    });
    if (res.ok) {
      const data = await res.json();
      toast('技能包 "' + data.pack + '" 资源已导入（' + data.files + ' 个文件）', true);
      packInput.value = '';
      document.getElementById('pack_upload_name').value = '';
    } else {
      const text = await res.text();
      let msg = '上传失败';
      try {
        const err = JSON.parse(text);
        msg = err.detail || msg;
      } catch(_) {
        msg = text || ('上传失败 (HTTP ' + res.status + ')');
      }
      toast(msg, false);
    }
  } catch(e) {
    toast('上传失败: ' + e.message, false);
  }
}

async function uploadRepo() {
  const repoInput = document.getElementById('repoFileInput');
  if (!repoInput.files || repoInput.files.length === 0) {
    toast('请先选择 ZIP 文件', false);
    return;
  }
  const packName = document.getElementById('repo_upload_name').value.trim();
  if (!packName) {
    toast('请填写技能包名', false);
    return;
  }

  const formData = new FormData();
  formData.append('file', repoInput.files[0]);
  const params = new URLSearchParams();
  params.append('pack', packName);
  const repoCategory = document.getElementById('repo_upload_category').value.trim();
  if (repoCategory) params.append('category', repoCategory);

  const box = document.getElementById('repoResult');
  box.style.display = 'none';
  box.innerHTML = '';

  try {
    const res = await fetch(API + '/skills/upload-repo?' + params.toString(), {
      method: 'POST',
      body: formData,
    });
    if (res.ok) {
      const data = await res.json();
      toast('技能包 "' + data.pack + '" 导入完成：' + data.skills.length + ' 个技能，' + data.files + ' 个文件', true);
      let html = '<div style="font-size:0.8em;font-weight:600;margin-bottom:6px">导入结果（' + data.files + ' 文件）</div>';
      if (data.skills.length === 0) {
        html += '<div style="font-size:0.78em;color:var(--muted)">未检测到技能（无 skills/&lt;name&gt;/SKILL.md）——已按纯资源导入。</div>';
      }
      for (const s of data.skills) {
        const n = (s.warnings || []).length;
        html += '<div style="font-size:0.78em;margin:4px 0;padding:6px 8px;border:1px solid var(--line);border-radius:6px">'
          + '<span style="font-weight:600">' + esc(s.name) + '</span> '
          + '<span style="color:var(--muted)">' + esc((s.description || '').slice(0, 60)) + '</span> '
          + (n ? '<span style="color:var(--red)">⚠ ' + n + ' 条警告</span>' : '<span style="color:var(--green)">✓ 健康</span>');
        if (n) {
          html += '<div style="color:var(--muted);white-space:pre-wrap;margin-top:4px">' + esc(s.warnings.join('\n')) + '</div>';
        }
        html += '</div>';
      }
      if (data.skipped && data.skipped.length) {
        html += '<div style="font-size:0.75em;color:var(--muted);margin-top:6px">未注册目录：' + esc(data.skipped.join('；')) + '</div>';
      }
      box.innerHTML = html;
      box.style.display = 'block';
      repoInput.value = '';
      document.getElementById('repo_upload_name').value = '';
      loadSkills();
    } else {
      const text = await res.text();
      let msg = '上传失败';
      try {
        const err = JSON.parse(text);
        msg = err.detail || msg;
      } catch(_) {
        msg = text || ('上传失败 (HTTP ' + res.status + ')');
      }
      toast(msg, false);
    }
  } catch(e) {
    toast('上传失败: ' + e.message, false);
  }
}

// 初始化加载
loadSkills();
</script>
</body>
</html>
"""

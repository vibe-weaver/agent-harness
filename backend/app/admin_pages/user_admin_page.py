USER_ADMIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>用户管理</title>
<style>
  :root {
    --bg: #FCF9F2; --card: #fff; --text: #111; --muted: #726960;
    --accent: #986638; --line: #EBE5DB; --red: #c44; --green: #4a8;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: Inter, system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }
  .container { max-width: 800px; margin: 0 auto; padding: 40px 20px 60px; }
  h1 { font-size: 1.3em; font-weight: 600; margin-bottom: 4px; letter-spacing: 0.04em; }
  .sub { font-size: 0.8em; color: var(--muted); margin-bottom: 20px; }
  .nav-bar { display: flex; gap: 12px; margin-bottom: 24px; font-size: 0.82em; flex-wrap: wrap; }
  .nav-bar a { color: var(--accent); text-decoration: none; padding: 4px 12px; border-radius: 6px; border: 1px solid var(--line); }
  .nav-bar a.current { background: var(--accent); color: #fff; border-color: var(--accent); }
  #toast { padding: 10px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 0.85em; display: none; position: fixed; top: 20px; left: 50%; transform: translateX(-50%); z-index: 2000; }
  #toast.show { display: block; }
  #toast.ok { background: rgba(152,102,56,0.1); color: var(--accent); }
  #toast.err { background: rgba(200,60,60,0.08); color: var(--red); }
  .btn { padding: 6px 16px; font-size: 0.82em; font-weight: 500; border: none; border-radius: 6px; cursor: pointer; font-family: inherit; white-space: nowrap; }
  .btn:active { transform: translateY(1px); }
  .btn-primary { background: var(--accent); color: #fff; box-shadow: 0 2px 0 rgba(0,0,0,0.15); }
  .btn-danger { background: rgba(200,60,60,0.08); color: var(--red); border: 1px solid rgba(200,60,60,0.2); }
  .btn-sm { padding: 4px 12px; font-size: 0.75em; }
  .toolbar { display: flex; gap: 12px; margin-bottom: 16px; align-items: center; flex-wrap: wrap; }
  .search-box { flex: 1; min-width: 200px; padding: 8px 14px; font-size: 0.85em; border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--text); outline: none; }
  .search-box:focus { border-color: var(--accent); }
  .user-list { display: grid; gap: 8px; }
  .user-card { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-radius: 10px; background: var(--card); border: 1px solid var(--line); gap: 12px; flex-wrap: wrap; }
  .user-info { display: flex; align-items: center; gap: 12px; flex: 1; min-width: 0; }
  .avatar { width: 36px; height: 36px; border-radius: 50%; background: var(--accent); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 15px; flex-shrink: 0; font-family: serif; }
  .user-detail { min-width: 0; }
  .user-email { font-size: 0.9em; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .user-meta { font-size: 0.72em; color: var(--muted); margin-top: 2px; }
  .empty { text-align: center; padding: 60px 20px; color: var(--muted); font-size: 0.9em; }
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.3); z-index: 1000; align-items: center; justify-content: center; }
  .modal-overlay.show { display: flex; }
  .modal { background: var(--card); border-radius: 16px; padding: 28px; width: 90%; max-width: 400px; box-shadow: 0 8px 40px rgba(0,0,0,0.15); }
  .modal h3 { font-size: 1.05em; font-weight: 600; margin-bottom: 16px; }
  .modal .modal-row { margin-bottom: 14px; }
  .modal .modal-row label { display: block; font-size: 0.82em; color: var(--muted); margin-bottom: 4px; }
  .modal .modal-row input { width: 100%; padding: 10px 14px; font-size: 0.88em; border: 1px solid var(--line); border-radius: 8px; outline: none; font-family: inherit; }
  .modal .modal-row input:focus { border-color: var(--accent); }
  .modal .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 20px; }
  .modal .hint { font-size: 0.75em; color: var(--muted); margin-top: 4px; }
  @media(max-width:640px){*{-webkit-tap-highlight-color:transparent}.btn{padding:12px 18px;font-size:0.95em;min-height:44px}.btn-sm{padding:10px 14px;font-size:0.85em;min-height:40px}.container{padding:20px 12px}.user-card{padding:14px 12px}.toolbar{flex-direction:column;align-items:stretch}.search-box{min-width:auto}}
</style>
</head>
<body>
<div class="container">
  <h1>用户管理</h1>
  <p class="sub">查看注册用户、重置用户密码</p>

  <div class="nav-bar">
    <a href="/admin">总览</a>
    <a href="/admin/ai">AI 管理</a>
    <a href="/admin/chat">对话配置</a>
    <a href="/admin/memory">AI 记忆</a>
    <a href="/admin/skills">Skills 技能</a>
    <a href="/admin/users" class="current">用户管理</a>
  </div>

  <div id="toast"></div>

  <div id="loginHint" style="display:none;padding:40px 20px;text-align:center;color:var(--muted);font-size:0.88em">
    <p style="margin-bottom:12px">需要管理员身份才能访问此页面</p>
    <p style="font-size:0.8em">请先在前端登录管理员账号，然后从用户菜单点击「用户管理」</p>
  </div>

  <div id="mainContent">
  <div class="toolbar">
    <input type="text" class="search-box" id="searchInput" placeholder="搜索邮箱或用户名..." oninput="doFilter()">
    <button class="btn btn-primary btn-sm" onclick="load()">刷新</button>
  </div>

  <div class="user-list" id="userList"></div>
  </div>
</div>

<!-- 改密码弹窗 -->
<div class="modal-overlay" id="pwdModal">
  <div class="modal">
    <h3>重置密码</h3>
    <input type="hidden" id="editUserId">
    <div class="modal-row">
      <label>用户</label>
      <div id="editUserLabel" style="font-size:0.88em;font-weight:500;padding:10px 14px;background:var(--bg);border-radius:8px;border:1px solid var(--line)"></div>
    </div>
    <div class="modal-row">
      <label>新密码</label>
      <input type="password" id="newPassword" placeholder="输入新密码（至少6位）" autocomplete="new-password">
      <div class="hint">密码将用 bcrypt 加密存储</div>
    </div>
    <div class="modal-actions">
      <button class="btn" style="background:var(--line);color:var(--muted)" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" onclick="savePassword()">确认重置</button>
    </div>
  </div>
</div>

<script>
window.addEventListener('error',function(e){var t=document.getElementById('toast');if(t){t.textContent='JS错误: '+e.message;t.className='show err'}});
window.addEventListener('unhandledrejection',function(e){var t=document.getElementById('toast');if(t){t.textContent='请求失败: '+(e.reason&&e.reason.message||e.reason||'网络错误');t.className='show err'}});

const API = '/api/v1/admin/users';
let allUsers = [];

// 导航链接
function navHref(path) {
  return path;
}
document.querySelectorAll('.nav-bar a').forEach(a => {
  const base = a.getAttribute('href');
  if (base && base !== '#') {
    a.setAttribute('href', navHref(base));
  }
});

function toast(msg, ok) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'show ' + (ok ? 'ok' : 'err');
  setTimeout(() => t.className = '', 3000);
}

function esc(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function doFilter() {
  const q = document.getElementById('searchInput').value.trim().toLowerCase();
  if (!q) { renderList(allUsers); return; }
  renderList(allUsers.filter(u =>
    (u.email || '').toLowerCase().includes(q) ||
    (u.username || '').toLowerCase().includes(q)
  ));
}

function renderList(users) {
  const list = document.getElementById('userList');
  if (!users.length) {
    list.innerHTML = '<div class="empty">暂无用户</div>';
    return;
  }
  list.innerHTML = users.map(u => {
    const displayName = u.username || u.email || ('用户 #' + u.id);
    const initial = (displayName.charAt(0) || '?').toUpperCase();
    const meta = [];
    if (u.email) meta.push(u.email);
    if (u.is_admin) meta.push('管理员');
    meta.push('注册时间: ' + (u.created_at || '未知'));
    return '<div class="user-card">' +
      '<div class="user-info">' +
        '<div class="avatar">' + esc(initial) + '</div>' +
        '<div class="user-detail">' +
          '<div class="user-email">' + esc(displayName) + '</div>' +
          '<div class="user-meta">' + meta.map(esc).join(' · ') + '</div>' +
        '</div>' +
      '</div>' +
      '<button class="btn btn-danger btn-sm" onclick="openModal(' + u.id + ',\\x27' + esc(displayName).replace(/'/g,"\\\\'") + '\\x27)">重置密码</button>' +
    '</div>';
  }).join('');
}

async function load() {
  try {
    const res = await fetch(API);
    if (res.status === 401 || res.status === 403) {
      const d = await res.json().catch(()=>({}));
      toast(d.detail || '权限不足', false);
      return;
    }
    allUsers = await res.json();
    doFilter();
  } catch(e) { toast('加载失败: ' + e.message, false); }
}

function openModal(id, name) {
  document.getElementById('editUserId').value = id;
  document.getElementById('editUserLabel').textContent = name;
  document.getElementById('newPassword').value = '';
  document.getElementById('pwdModal').classList.add('show');
  setTimeout(() => document.getElementById('newPassword').focus(), 100);
}

function closeModal() {
  document.getElementById('pwdModal').classList.remove('show');
}

async function savePassword() {
  const id = document.getElementById('editUserId').value;
  const pwd = document.getElementById('newPassword').value;
  if (!pwd || pwd.length < 6) { toast('密码至少6位', false); return; }
  try {
    const res = await fetch(API + '/' + id + '/password', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pwd }),
    });
    if (res.ok) { toast('密码已重置', true); closeModal(); }
    else { const d = await res.json().catch(()=>({})); toast(d.detail || '重置失败', false); }
  } catch(e) { toast('网络错误: ' + e.message, false); }
}

load();
</script>
</body>
</html>
"""

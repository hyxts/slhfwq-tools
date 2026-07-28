# -*- coding: utf-8 -*-
"""
Flask 后端（主站）
适用于 PythonAnywhere 部署
"""
import os, hashlib, traceback, time as time_mod, threading, sys, sqlite3
from flask import Flask, jsonify, send_from_directory, request, session, redirect

# 启动耗时诊断
_start_ts = time_mod.time()
def _diag(msg):
    elapsed = time_mod.time() - _start_ts
    print(f'[STARTUP {elapsed:.2f}s] {msg}')

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.config['JSON_AS_ASCII'] = False  # JSON 直接输出中文，避免 \uXXXX 乱码

app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24).hex())

# Session 安全加固
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=86400 * 7,  # 7天
)

# ==================== 密码认证 ====================

AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.auth')

def _hash(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def _load_auth():
    try:
        # 优先从环境变量读取密码
        env_pw = os.environ.get('SITE_PASSWORD', '')
        if env_pw:
            return _hash(env_pw)
        # 其次从文件读取
        if os.path.exists(AUTH_FILE):
            with open(AUTH_FILE, 'r') as f:
                return f.read().strip()
    except Exception:
        pass  # 读取失败不阻塞启动，后续走 setup 流程
    # 未设置密码，返回空（需要走setup流程）
    return ''

AUTH_HASH = _load_auth()
_AUTH_LOCK = threading.Lock()

SETUP_PAGE = '''<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>设置密码</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'><stop offset='0%25' stop-color='%236366f1'/><stop offset='100%25' stop-color='%234f46e5'/></linearGradient></defs><rect width='32' height='32' rx='7' fill='url(%23g)'/><text x='16' y='23' text-anchor='middle' font-size='16' font-weight='bold' fill='white' font-family='sans-serif'>PA</text></svg>">
<style>body{display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f7fa;font-family:system-ui}
.box{background:#fff;padding:32px;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,.08);width:320px;text-align:center}
input{width:100%;padding:12px;border:1px solid #d1d5db;border-radius:10px;font-size:15px;margin:8px 0;box-sizing:border-box}
button{width:100%;padding:12px;background:#667eea;color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;margin-top:8px}
button:hover{background:#5a6fd6}.tip{color:#666;font-size:12px;margin-top:12px}</style></head>
<body><div class="box"><h2 style="margin:0 0 8px">首次设置</h2><p style="color:#666;font-size:13px">请设置系统访问密码</p>
<form method="POST"><input type="password" name="password" placeholder="设置密码" autofocus required>
<input type="password" name="password2" placeholder="确认密码" required>
<button type="submit">确认设置</button></form>
<div class="tip">设置后可通过访问 /setup 重新修改密码</div></div></body></html>'''

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if request.method == 'POST':
        pw = request.form.get('password', '')
        pw2 = request.form.get('password2', '')
        if not pw or len(pw) < 4:
            return SETUP_PAGE.replace('请设置系统访问密码', '密码至少4位')
        if pw != pw2:
            return SETUP_PAGE.replace('请设置系统访问密码', '两次密码不一致')
        global AUTH_HASH
        with _AUTH_LOCK:
            AUTH_HASH = _hash(pw)
            with open(AUTH_FILE, 'w') as f:
                f.write(AUTH_HASH)
        return redirect('/login')
    return SETUP_PAGE

LOGIN_HTML = '''<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'><stop offset='0%25' stop-color='%236366f1'/><stop offset='100%25' stop-color='%234f46e5'/></linearGradient></defs><rect width='32' height='32' rx='7' fill='url(%23g)'/><text x='16' y='23' text-anchor='middle' font-size='16' font-weight='bold' fill='white' font-family='sans-serif'>PA</text></svg>">
<style>body{display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f7fa;font-family:system-ui}
.box{background:#fff;padding:32px;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,.08);width:320px;text-align:center}
input{width:100%;padding:12px;border:1px solid #d1d5db;border-radius:10px;font-size:15px;margin:12px 0;box-sizing:border-box}
button{width:100%;padding:12px;background:#667eea;color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer}
button:hover{background:#5a6fd6}.err{color:#dc2626;font-size:13px;margin-top:8px}</style></head>
<body><div class="box"><h2 style="margin:0 0 8px">系统登录</h2><p style="color:#666;font-size:13px">请输入访问密码</p>
<form method="POST"><input type="password" name="password" placeholder="密码" autofocus>
<button type="submit">登录</button></form>__ERROR_PLACEHOLDER__</div></body></html>'''

@app.route('/login', methods=['GET', 'POST'])
def login():
    with _AUTH_LOCK:
        auth_set = bool(AUTH_HASH)
        current_hash = AUTH_HASH
    if not auth_set:
        return redirect('/setup')
    if request.method == 'POST':
        pw = request.form.get('password', '')
        if _hash(pw) == current_hash:
            session['auth'] = True
            session.permanent = True
            nxt = request.args.get('next', '/')
            if not nxt.startswith('/'):
                nxt = '/'
            return redirect(nxt)
        return LOGIN_HTML.replace('__ERROR_PLACEHOLDER__', '<div class="err">密码错误</div>')
    return LOGIN_HTML.replace('__ERROR_PLACEHOLDER__', '')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

DEPLOY_TOKEN = os.environ.get('DEPLOY_TOKEN', '')

# 请求频率限制（基于内存 + SQLite 持久化）
_rate_limits = {}
RATE_LIMIT_WINDOW = 60       # 60秒窗口
RATE_LIMIT_MAX_JSON = 30     # API最大请求数
RATE_LIMIT_MAX_HTML = 60     # 页面最大请求数
_RATE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.rate_limits.db')
_RATE_DB_LOCK = threading.Lock()

def _init_rate_db():
    """初始化速率限制 SQLite 数据库"""
    try:
        with _RATE_DB_LOCK:
            conn = sqlite3.connect(_RATE_DB_PATH)
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE IF NOT EXISTS ratelimit (ip_window TEXT PRIMARY KEY, count INTEGER, updated REAL)')
            # 加载已有数据到内存
            rows = conn.execute('SELECT ip_window, count FROM ratelimit').fetchall()
            for ip_window, count in rows:
                _rate_limits[ip_window] = count
            conn.commit()
            conn.close()
            return True
    except Exception:
        return False

def _flush_rate_db():
    """将内存中的速率限制写入 SQLite"""
    try:
        with _RATE_DB_LOCK:
            conn = sqlite3.connect(_RATE_DB_PATH)
            conn.execute('PRAGMA journal_mode=WAL')
            now = time_mod.time()
            # 只写入当前窗口的数据
            current_window = int(now // RATE_LIMIT_WINDOW)
            for key, count in list(_rate_limits.items()):
                try:
                    key_time = int(key.split(':')[-1])
                    if key_time >= current_window - 1:
                        conn.execute(
                            'INSERT OR REPLACE INTO ratelimit(ip_window, count, updated) VALUES(?,?,?)',
                            (key, count, now)
                        )
                except Exception:
                    pass
            # 清理超过 5 分钟的旧记录
            conn.execute('DELETE FROM ratelimit WHERE updated < ?', (now - 300,))
            conn.commit()
            conn.close()
    except Exception:
        pass

# 启动时加载持久化数据
_init_rate_db()

@app.before_request
def check_auth():
    # 部署健康检查：最先放行，不走频率限制、认证、任何其他逻辑
    if request.path == '/api/ping':
        return

    # 频率限制
    client_ip = request.remote_addr or 'unknown'
    path = request.path
    now_key = int(time_mod.time() // RATE_LIMIT_WINDOW)
    rate_key = f'{client_ip}:{now_key}'
    _rate_limits.setdefault(rate_key, 0)
    _rate_limits[rate_key] += 1
    limit = RATE_LIMIT_MAX_JSON if path.startswith('/api/') else RATE_LIMIT_MAX_HTML
    if _rate_limits[rate_key] > limit:
        return jsonify({'success': False, 'error': '请求过于频繁'}), 429
    # 免登录路径
    PUBLIC_PREFIXES = ('/static', '/countdown', '/accounting', '/renqing/manifest', '/renqing/icon', '/deploy/manifest', '/deploy/icon', '/nav/manifest', '/nav/icon', '/api/accounting', '/api/countdown', '/api/pa/', '/api/status')
    if request.path in ('/login', '/setup', '/api/ping') or any(request.path.startswith(p) for p in PUBLIC_PREFIXES):
        return
    if session.get('auth'):
        return
    # 部署/续期接口允许令牌认证
    if request.path in ('/api/git-pull', '/api/status', '/api/ping', '/api/restore-db',
                         '/api/backup/restore-latest', '/api/renqing/db-check') or \
       request.path.startswith('/api/backup/') or \
       request.path.startswith('/api/cleanup'):
        token = request.headers.get('X-Deploy-Token', '')
        if DEPLOY_TOKEN and token == DEPLOY_TOKEN:
            return
        return jsonify({'success': False, 'error': '未授权'}), 401
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': '未登录'}), 401
    return redirect('/login?next=' + request.path)

# ==================== 注册蓝图 ====================

# 路由模块导入 —— 单个模块出错不影响其他模块和部署 API
# deploy 先注册，确保部署 API 始终可用
def _safe_import(module_name, what='bp, init_db'):
    try:
        mod = __import__(module_name, fromlist=['bp'])
        return getattr(mod, 'bp', None), getattr(mod, 'init_db', None)
    except Exception as e:
        print(f'[WARNING] 模块 {module_name} 导入失败: {e}', flush=True)
        return None, None

def _safe_import_extra(module_name, attrs):
    """导入模块的额外属性（如 start_auto_backup）"""
    try:
        mod = __import__(module_name, fromlist=attrs)
        return tuple(getattr(mod, a, None) for a in attrs)
    except Exception as e:
        print(f'[WARNING] 模块 {module_name} 导入失败({attrs}): {e}', flush=True)
        return tuple(None for _ in attrs)

MODULES = [
    ('routes.renqing', 'renqing'),
    ('routes.gpa', 'gpa'),
    ('routes.hsgrades', 'hsgrades'),
    ('routes.backup', 'backup'),
    ('routes.deploy', 'deploy'),
    ('routes.pa', 'pa'),
    ('routes.countdown', 'countdown'),
    ('routes.accounting', 'accounting'),
]

# 先导入 deploy（独立容错），确保部署 API 最优先可用
deploy_bp, _ = _safe_import('routes.deploy')
deploy_extra = _safe_import_extra('routes.deploy', ('prebuild_status',))
prebuild_status = deploy_extra[0] if deploy_extra else None
if deploy_bp:
    app.register_blueprint(deploy_bp)

renqing_bp = gpa_bp = hsgrades_bp = None
backup_bp = pa_bp = countdown_bp = accounting_bp = None
init_renqing_db = init_gpa_db = init_hsgrades_db = None
init_pa_db = init_countdown_db = init_accounting_db = None
start_auto_backup = start_auto_clean = start_auto_renew = None

for mod_name, key in MODULES:
    if mod_name == 'routes.deploy':
        continue  # 已单独处理
    bp_obj, init_fn = _safe_import(mod_name)
    if key == 'renqing':
        renqing_bp, init_renqing_db = bp_obj, init_fn
    elif key == 'gpa':
        gpa_bp, init_gpa_db = bp_obj, init_fn
    elif key == 'hsgrades':
        hsgrades_bp, init_hsgrades_db = bp_obj, init_fn
    elif key == 'backup':
        backup_bp = bp_obj
        backup_extra = _safe_import_extra('routes.backup', ('start_auto_backup', 'start_auto_clean'))
        start_auto_backup, start_auto_clean = backup_extra
    elif key == 'pa':
        pa_bp, init_pa_db = bp_obj, init_fn
        pa_extra = _safe_import_extra('routes.pa', ('start_auto_renew',))
        start_auto_renew = pa_extra[0] if pa_extra else None
    elif key == 'countdown':
        countdown_bp, init_countdown_db = bp_obj, init_fn
    elif key == 'accounting':
        accounting_bp, init_accounting_db = bp_obj, init_fn
    if bp_obj:
        app.register_blueprint(bp_obj)

_LOADED_MODULES = [name for name, bp in [
    ('renqing', renqing_bp), ('gpa', gpa_bp), ('hsgrades', hsgrades_bp),
    ('deploy', deploy_bp), ('backup', backup_bp), ('pa', pa_bp),
    ('countdown', countdown_bp), ('accounting', accounting_bp),
] if bp]

# ==================== 全局错误处理 ====================

ERROR_LOG_MAX_LINES = 200
ERROR_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '500_error.log')
_ERROR_LOG_LOCK = threading.Lock()

@app.errorhandler(OSError)
def handle_os_error(e):
    """客户端断开静默处理：不写日志不traceback，直接丢弃"""
    err_str = str(e).lower()
    if 'write error' in err_str or isinstance(e, (BrokenPipeError, ConnectionResetError)):
        return ''  # 客户端已断开，无需响应
    return handle_500(e)


@app.errorhandler(500)
def handle_500(e):
    err_msg = str(e)
    tb = traceback.format_exc()
    app.logger.error(f"500 error: {err_msg}\n{tb}")
    # 写入错误日志文件便于远程诊断（加锁防止并发竞争）
    try:
        with _ERROR_LOG_LOCK:
            with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(f'[{time_mod.strftime("%Y-%m-%d %H:%M:%S")}] {request.method} {request.path}\n{err_msg}\n{tb}\n{"-"*60}\n')
            # 保留最近 N 行
            with open(ERROR_LOG_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            if len(lines) > ERROR_LOG_MAX_LINES:
                with open(ERROR_LOG_FILE, 'w', encoding='utf-8') as f:
                    f.writelines(lines[-ERROR_LOG_MAX_LINES:])
    except Exception:
        pass
    return jsonify({'success': False, 'error': f'服务器内部错误: {err_msg[:200]}'}), 500

@app.errorhandler(404)
def handle_404(e):
    return jsonify({'success': False, 'error': '资源不存在'}), 404

# ==================== 前端路由 ====================

@app.route('/')
def index():
    return redirect('/nav')

@app.route('/nav')
@app.route('/nav/')
def nav_index():
    return send_from_directory('导航', 'index.html')

@app.route('/renqing')
@app.route('/renqing/')
def renqing_index():
    return send_from_directory('人情', 'index.html')

@app.route('/renqing/archive')
@app.route('/renqing/archive/')
def renqing_archive():
    return send_from_directory('人情', 'archive.html')

@app.route('/renqing/common.js')
def renqing_common_js():
    return send_from_directory('人情', 'renqing-common.js')

@app.route('/renqing/manifest.json')
def renqing_manifest():
    return send_from_directory('人情', 'manifest.json')

@app.route('/renqing/icon-192.svg')
def renqing_icon_192():
    return send_from_directory('人情', 'icon-192.svg')

@app.route('/renqing/icon-512.svg')
def renqing_icon_512():
    return send_from_directory('人情', 'icon-512.svg')

@app.route('/gpa')
@app.route('/gpa/')
def gpa_index():
    return send_from_directory('绩点', 'index.html')

@app.route('/hsgrades')
@app.route('/hsgrades/')
def hsgrades_index():
    return send_from_directory('成绩', 'index.html')

@app.route('/pa')
@app.route('/pa/')
def pa_index():
    return redirect('/deploy')

@app.route('/deploy')
@app.route('/deploy/')
def deploy_index():
    return send_from_directory('部署', 'index.html')

@app.route('/nav/manifest.json')
def nav_manifest():
    return send_from_directory('导航', 'manifest.json')

@app.route('/nav/icon-192.svg')
def nav_icon_192():
    return send_from_directory('导航', 'icon-192.svg')

@app.route('/nav/icon-512.svg')
def nav_icon_512():
    return send_from_directory('导航', 'icon-512.svg')

@app.route('/deploy/manifest.json')
def deploy_manifest():
    return send_from_directory('部署', 'manifest.json')

@app.route('/deploy/icon-192.svg')
def deploy_icon_192():
    return send_from_directory('部署', 'icon-192.svg')

@app.route('/deploy/icon-512.svg')
def deploy_icon_512():
    return send_from_directory('部署', 'icon-512.svg')

@app.route('/backup')
@app.route('/backup/')
def backup_index():
    return redirect('/deploy')

@app.route('/accounting')
@app.route('/accounting/')
def accounting_index():
    return send_from_directory('记账', 'index.html')

@app.route('/accounting/manifest.json')
def accounting_manifest():
    return send_from_directory('记账', 'manifest.json')

@app.route('/accounting/icon-192.svg')
def accounting_icon_192():
    return send_from_directory('记账', 'icon-192.svg')

@app.route('/accounting/icon-512.svg')
def accounting_icon_512():
    return send_from_directory('记账', 'icon-512.svg')

# ==================== 启动 ====================

_diag('开始初始化数据库...')
_SAFE_INITS = [
    ('renqing', init_renqing_db),
    ('gpa', init_gpa_db),
    ('hsgrades', init_hsgrades_db),
    ('pa', init_pa_db),
    ('countdown', init_countdown_db),
    ('accounting', init_accounting_db),
]
for _name, _fn in _SAFE_INITS:
    if _fn:
        try:
            _fn()
            _diag(f'{_name}_db 完成')
        except Exception as e:
            _diag(f'{_name}_db 失败: {e}')
    else:
        _diag(f'{_name}_db 跳过（模块未加载）')

_SAFE_STARTS = [
    ('auto_renew', start_auto_renew),
    ('auto_backup', start_auto_backup),
    ('auto_clean', start_auto_clean),
]
_diag('启动完成 - 数据库就绪')


def _deferred_starts():
    """延迟启动后台服务和缓存：先让 worker 就绪接受请求，再异步启动"""
    import time as _time
    _time.sleep(3)  # 等待 PA worker 确认就绪
    for _name, _fn in _SAFE_STARTS:
        if _fn:
            try:
                _fn()
                _diag(f'[延迟] {_name} 启动')
            except Exception as e:
                _diag(f'[延迟] {_name} 失败: {e}')
    _diag(f'[延迟] 后台服务启动完毕')
    if prebuild_status:
        try:
            prebuild_status()
            _diag('[延迟] 状态缓存构建完成')
        except Exception as e:
            _diag(f'[延迟] 缓存构建失败: {e}')


threading.Thread(target=_deferred_starts, daemon=True).start()

@app.route('/api/version')
def api_version():
    return jsonify({
        'version': '3.8.0',
        'build': '2026-07-27',
        'python': sys.version.split()[0],
        'modules': _LOADED_MODULES
    })

# 定期清理过期速率记录和持久化
def _clean_rate_limits():
    while True:
        time_mod.sleep(60)  # 每分钟清理
        try:
            now_key = int(time_mod.time() // RATE_LIMIT_WINDOW)
            keys = list(_rate_limits.keys())
            for k in keys:
                try:
                    key_time = int(k.split(':')[-1])
                    if key_time < now_key - 1:
                        _rate_limits.pop(k, None)
                except Exception:
                    _rate_limits.pop(k, None)
            # 防止极端情况下内存暴涨（超过1000个条目时强制清掉最旧的）
            if len(_rate_limits) > 1000:
                old_keys = sorted(_rate_limits.keys(),
                    key=lambda k: int(k.split(':')[-1]) if ':' in k else 0)[:500]
                for k in old_keys:
                    _rate_limits.pop(k, None)
            # 持久化当前速率数据到 SQLite
            _flush_rate_db()
        except Exception:
            pass

_thread = threading.Thread(target=_clean_rate_limits, daemon=True)
_thread.start()

if __name__ == '__main__':
    print('礼金记录系统: http://127.0.0.1:5000')
    print('GPA系统: http://127.0.0.1:5000/gpa')
    print('高中成绩系统: http://127.0.0.1:5000/hsgrades')
    print('个人记账系统: http://127.0.0.1:5000/accounting')
    print('提示: 使用 127.0.0.1 访问比 localhost 更快（约40倍）')
    app.run(host='0.0.0.0', port=5000, debug=False)

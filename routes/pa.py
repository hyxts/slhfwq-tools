# -*- coding: utf-8 -*-
"""PA 自动续期蓝图 - SQLite + 主站统一认证 + 自动定时执行"""
from __future__ import annotations
import os, json, re, threading, sqlite3, time as time_mod
from datetime import datetime, timedelta

from .utils import TZ, now_ts, make_logger, make_db, encode_pw, decode_pw, extract_csrf
from flask import Blueprint, request, jsonify

bp = Blueprint('pa', __name__)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PA_DIR = os.path.join(BASE_DIR, '服务器')
DB_FILE = os.path.join(PA_DIR, 'pa.db')
LOG_FILE = os.path.join(PA_DIR, 'renew.log')

_log = make_logger(LOG_FILE)
_get_db = make_db(DB_FILE)

_status = {'running': False, 'last_result': None}
_status_lock = threading.Lock()

_LOG_CACHE = {'content': '', 'timestamp': 0, 'mtime': 0}
_LOG_CACHE_LOCK = threading.Lock()
_LOG_CACHE_TTL = 10  # 日志缓存10秒


def init_db():
    try:
        _init_db_impl()
    except Exception as e:
        import traceback
        print(f'[init_pa_db ERROR] {e}\n{traceback.format_exc()}', flush=True)


def _init_db_impl():
    os.makedirs(PA_DIR, exist_ok=True)
    conn = _get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS pa_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        username TEXT DEFAULT '',
        password TEXT DEFAULT '',
        interval_days INTEGER DEFAULT 7,
        expiry TEXT DEFAULT '',
        last_run TEXT DEFAULT '',
        last_result TEXT DEFAULT '{}',
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS pa_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT NOT NULL,
        expiry TEXT DEFAULT '',
        success INTEGER DEFAULT 0,
        details TEXT DEFAULT '[]'
    )''')
    # 添加 api_token 列（如果不存在）
    try:
        conn.execute("ALTER TABLE pa_config ADD COLUMN api_token TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 列已存在
    cur = conn.execute("SELECT COUNT(*) FROM pa_config")
    if cur.fetchone()[0] == 0:
        conn.execute("INSERT INTO pa_config (id) VALUES (1)")
    # 迁移旧明文密码和旧 ENC: 格式到新版 v2: 编码格式
    row = conn.execute("SELECT password FROM pa_config WHERE id = 1").fetchone()
    if row and row['password']:
        pw = row['password']
        if pw.startswith('v2:'):
            pass  # 已是最新格式
        elif pw.startswith('ENC:'):
            # 旧格式解码后重新编码
            old_pw = decode_pw(pw[4:], PA_DIR)
            if old_pw:
                conn.execute("UPDATE pa_config SET password = ? WHERE id = 1",
                           (encode_pw(old_pw, PA_DIR),))
        else:
            # 明文密码，直接编码
            conn.execute("UPDATE pa_config SET password = ? WHERE id = 1",
                       (encode_pw(pw, PA_DIR),))
    conn.commit()
    conn.close()
    try:
        conn = _get_db()
        row = conn.execute("SELECT last_result FROM pa_config WHERE id = 1").fetchone()
        if row and row['last_result']:
            _status['last_result'] = json.loads(row['last_result'])
        conn.close()
    except Exception:
        pass


def _read_log(n=100):
    global _LOG_CACHE
    try:
        if not os.path.exists(LOG_FILE):
            return []
        now = time_mod.time()
        file_mtime = os.path.getmtime(LOG_FILE)
        
        # 检查缓存是否有效（时间未过期且文件未修改）
        with _LOG_CACHE_LOCK:
            if _LOG_CACHE['content'] and \
               (now - _LOG_CACHE['timestamp']) < _LOG_CACHE_TTL and \
               _LOG_CACHE['mtime'] == file_mtime:
                lines = _LOG_CACHE['content']
            else:
                with open(LOG_FILE, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                _LOG_CACHE['content'] = lines
                _LOG_CACHE['timestamp'] = now
                _LOG_CACHE['mtime'] = file_mtime
        
        result = lines[-n:]
        result.reverse()
        return result
    except Exception:
        return []


def _load_config():
    conn = _get_db()
    row = conn.execute("SELECT * FROM pa_config WHERE id = 1").fetchone()
    conn.close()
    if row:
        d = dict(row)
        d.pop('id', None)
        d.pop('updated_at', None)
        # 解码密码
        pw = d.get('password', '')
        if pw.startswith('v2:'):
            d['password'] = decode_pw(pw, PA_DIR)
        elif pw.startswith('ENC:'):
            d['password'] = decode_pw(pw[4:], PA_DIR)
        # api_token 可能不存在（旧数据库），给默认值
        d.setdefault('api_token', '')
        try:
            d['last_result'] = json.loads(d.get('last_result', '{}'))
        except Exception:
            d['last_result'] = {}
        return d
    return {'username': '', 'password': '', 'api_token': '', 'interval_days': 7, 'expiry': '', 'last_run': '', 'last_result': {}}


def _load_history():
    conn = _get_db()
    rows = conn.execute("SELECT * FROM pa_history ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d.pop('id', None)
        try:
            d['details'] = json.loads(d.get('details', '[]'))
        except Exception:
            d['details'] = []
        result.append(d)
    return result


# ==================== API ====================

@bp.route('/api/pa/data', methods=['GET'])
def get_data():
    try:
        cfg = _load_config()
        interval = cfg.get('interval_days', 7)
        expiry = cfg.get('expiry', '')
        last_run = cfg.get('last_run', '')
        next_run = ''
        if last_run:
            try:
                next_run = (datetime.strptime(last_run, '%Y-%m-%d %H:%M:%S') + timedelta(days=interval)).strftime('%Y-%m-%d %H:%M')
            except Exception:
                next_run = '计算失败'
        else:
            next_run = '首次续期后自动计算'
        lr = _status.get('last_result') or cfg.get('last_result')
        return jsonify({'success': True, 'data': {
            'username': cfg.get('username', ''),
            'interval': interval,
            'expiry': cfg.get('expiry', ''),
            'last_run': last_run,
            'next_run': next_run,
            'running': _status['running'],
            'last_result': lr,
            'api_token': '已配置' if cfg.get('api_token') else '',
            'password_configured': bool(cfg.get('password')),
            'history': _load_history(),
            'log': ''.join(_read_log()),
        }})
    except Exception as e:
        _log(f'[错误] 读取数据异常: {e}')
        return jsonify({'success': False, 'error': '服务器错误'}), 500


@bp.route('/api/pa/data', methods=['POST'])
def save_data():
    data = request.get_json(silent=True) or {}
    conn = _get_db()
    try:
        updates = []
        params = []
        if 'username' in data:
            updates.append("username = ?")
            params.append(data['username'])
        if data.get('password'):
            updates.append("password = ?")
            params.append(encode_pw(data['password'], PA_DIR))
        if 'interval' in data:
            updates.append("interval_days = ?")
            params.append(int(data['interval']))
        if 'api_token' in data:
            updates.append("api_token = ?")
            params.append(data['api_token'])
        if updates:
            updates.append("updated_at = datetime('now','localtime')")
            # 安全的参数化SQL，不使用f-string拼接
            set_clause = ', '.join(updates)
            conn.execute(f"UPDATE pa_config SET {set_clause} WHERE id = 1", params)
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        _log(f'[错误] 保存配置异常: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()




@bp.route('/api/pa/renew', methods=['POST'])
def trigger_renew():
    """手动触发一次PA续期检查"""
    try:
        cfg = _load_config()
        username = cfg.get('username', '')
        password = cfg.get('password', '')
        if not username or not password:
            return jsonify({'success': False, 'error': '未配置账号密码'}), 400
        if _status['running']:
            return jsonify({'success': False, 'error': '续期正在进行中'}), 409
        threading.Thread(target=_renew_thread, args=(username, password, cfg.get('api_token', '')), daemon=True).start()
        return jsonify({'success': True, 'message': '续期已触发，请稍后查看结果'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/pa/status', methods=['GET'])
def status():
    try:
        cfg = _load_config()
        interval = cfg.get('interval_days', 7)
        expiry = cfg.get('expiry', '')
        last_run = cfg.get('last_run', '')
        next_run = ''
        if last_run:
            try:
                next_run = (datetime.strptime(last_run, '%Y-%m-%d %H:%M:%S') + timedelta(days=interval)).strftime('%Y-%m-%d %H:%M')
            except Exception:
                next_run = '计算失败'
        else:
            next_run = '首次续期后自动计算'
        lr = _status.get('last_result') or cfg.get('last_result')
        return jsonify({
            'success': True,
            'running': _status['running'],
            'last_result': lr,
            'last_run': last_run,
            'expiry': cfg.get('expiry', ''),
            'interval': interval,
            'next_run': next_run,
            'history': _load_history(),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/pa/log', methods=['GET'])
def log():
    try:
        return jsonify({'success': True, 'log': ''.join(_read_log())})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'log': ''}), 500


@bp.route('/api/pa/logs', methods=['GET'])
def all_logs():
    """获取所有模块的日志"""
    module = request.args.get('module', 'pa')
    logs = {}

    # PA续期日志
    if module in ('pa', 'all'):
        try:
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, 'r', encoding='utf-8') as f:
                    logs['pa'] = ''.join(f.readlines()[-100:])
            else:
                logs['pa'] = ''
        except Exception:
            logs['pa'] = ''

    # 重载日志
    if module in ('reload', 'all'):
        try:
            rl = os.path.join(BASE_DIR, 'reload.log')
            if os.path.exists(rl):
                with open(rl, 'r', encoding='utf-8') as f:
                    logs['reload'] = ''.join(f.readlines()[-100:])
            else:
                logs['reload'] = ''
        except Exception:
            logs['reload'] = ''

    # 备份日志（从backup模块的日志文件）
    if module in ('backup', 'all'):
        try:
            # 备份操作的日志记录在print输出中，这里读取最近的备份文件列表作为参考
            backup_dir = os.path.join(BASE_DIR, 'backups')
            if os.path.isdir(backup_dir):
                backups = sorted([f for f in os.listdir(backup_dir) if f.endswith('.zip')], reverse=True)[:10]
                logs['backup'] = '\n'.join([f'[备份] {b}' for b in backups]) if backups else '(无备份记录)'
            else:
                logs['backup'] = '(无备份目录)'
        except Exception:
            logs['backup'] = ''

    # 其他模块日志
    if module == 'all':
        log_files = {
            '人情': os.path.join(BASE_DIR, '人情', 'renqing.log'),
            '绩点': os.path.join(BASE_DIR, '绩点', 'gpa.log'),
            '成绩': os.path.join(BASE_DIR, '成绩', 'hsgrades.log'),
            '倒计时': os.path.join(BASE_DIR, '倒计时', 'countdown.log'),
        }
        other_logs = []
        for name, path in log_files.items():
            try:
                if os.path.exists(path):
                    with open(path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()[-20:]  # 每个模块取最近20行
                        if lines:
                            other_logs.append(f'=== {name} ===')
                            other_logs.extend(lines)
            except Exception:
                pass
        logs['other'] = '\n'.join(other_logs) if other_logs else ''

    return jsonify({'success': True, 'logs': logs})


@bp.route('/api/pa/reload-log', methods=['GET'])
def reload_log():
    """读取自动部署重载日志"""
    try:
        rl = os.path.join(BASE_DIR, 'reload.log')
        if os.path.exists(rl):
            with open(rl, 'r', encoding='utf-8') as f:
                lines = f.readlines()[-50:]
                return jsonify({'success': True, 'log': ''.join(lines)})
        return jsonify({'success': True, 'log': '(无日志)'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 续期核心 ====================

MONTHS = {'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
          'july':7,'august':8,'september':9,'october':10,'november':11,'december':12}


def _parse_date(text):
    for pat, fn in [
        (r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})',
         lambda m: f'{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}'),
        (r'(\d{4})-(\d{2})-(\d{2})', lambda m: m.group(0)),
    ]:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return fn(m)
            except Exception:
                continue
    return None


def _find_extend_button(soup):
    """查找续期按钮，返回 (button_element, form_action) 或 (None, None)"""
    btn = soup.find('input', class_='webapp_extend')
    if not btn:
        for tag in ['button', 'input', 'a']:
            btn = soup.find(tag, string=re.compile(r'Run until|month from today|extend', re.I))
            if btn:
                break
    return btn


def _pa_login(session, username, password):
    """PA 登录步骤（获取 token + 登录），返回 (success, token_or_error, details, final_url)"""
    import requests
    base = 'https://www.pythonanywhere.com'
    webapps_url = f'{base}/user/{username}/webapps/'
    details = []

    def d(msg):
        _log(msg)
        details.append(msg)

    # 步骤1: 获取初始页面和 CSRF Token
    d('[1] 获取页面...')
    try:
        r = session.get(webapps_url, timeout=15)
    except Exception as e:
        d(f'  [1失败] 网络访问异常: {e}')
        return (False, f'网络访问异常: {e}', details, '')
    token = extract_csrf(r.text)
    if not token:
        page_preview = r.text[:500].replace('\n', ' ')[:300]
        d(f'  [1失败] 未找到安全令牌，页面可能结构变更。当前地址: {r.url}，页面预览: {page_preview}')
        return (False, '未找到安全令牌，PA页面结构可能已变更', details, '')

    # 步骤2: 登录
    d('[2] 登录...')
    session.headers['Referer'] = r.url
    d(f'  Referer: {r.url}')
    try:
        r = session.post(f'{base}/login/', data={
            'csrfmiddlewaretoken': token, 'auth-username': username,
            'auth-password': password, 'next': f'/user/{username}/webapps/',
            'login_view-current_step': 'auth',
        }, allow_redirects=True, timeout=15)
    except Exception as e:
        d(f'  [2失败] 登录请求异常: {e}')
        return (False, f'登录请求异常: {e}', details, '')

    if 'login' in r.url.lower() and 'webapps' not in r.url.lower():
        try:
            from bs4 import BeautifulSoup
            soup_err = BeautifulSoup(r.text, 'html.parser')
            err_text = ''
            # 方法1: 精确查找 PA 的 errorlist
            err_el = soup_err.find('ul', class_='errorlist')
            if err_el:
                err_text = err_el.get_text(strip=True)
            if not err_text:
                # 方法2: 正则直接搜索页面文本中的错误短句（限制长度避免匹配到整个页面头）
                m = re.search(r'(?:用户名或密码错误|密码不正确|账号或密码错误|user name or password is incorrect|username or password is incorrect|incorrect username or password|password incorrect|invalid login|account locked|disabled|CAPTCHA|verification required)[^<]*', r.text, re.I)
                if m:
                    err_text = m.group(0).strip()
            if not err_text:
                # 方法3: 在标签文本中搜索关键词，但限制文本长度不超过200字符
                for tag in soup_err.find_all(['p', 'div', 'span', 'li']):
                    txt = tag.get_text(strip=True)
                    if len(txt) > 200:
                        continue  # 跳过超长文本（可能是导航栏等）
                    if txt and ('incorrect' in txt.lower() or 'wrong' in txt.lower()
                              or 'password' in txt.lower() or 'username' in txt.lower()
                              or 'locked' in txt.lower() or 'captcha' in txt.lower()):
                        err_text = txt[:200]
                        break
            if not err_text:
                # 方法4: 尝试找 class 含 error/alert 的元素
                err_el = soup_err.find(class_=re.compile(r'error|alert|message', re.I))
                if err_el:
                    err_text = err_el.get_text(strip=True)[:200]
            if err_text:
                d(f'  [2失败] 登录失败: {err_text}')
            else:
                d(f'  [2失败] 登录失败，停留在登录页面（用户名或密码错误）')
        except Exception:
            d(f'  [2失败] 登录失败，用户名或密码错误')
        return (False, '登录失败，请检查账号密码', details, '')
    d('  登录成功')
    d(f'  登录后跳转地址: {r.url}')
    return (True, token, details, r.url)


def _pa_get_status(session, webapps_url):
    """获取 PA WebApp 状态并解析到期日，返回 (pre_expiry, token, details, html)"""
    details = []
    def d(msg):
        _log(msg)
        details.append(msg)

    d('[3] 获取状态...')
    try:
        r = session.get(webapps_url, timeout=15)
    except Exception as e:
        d(f'  [3失败] 获取网站应用页面异常: {e}')
        return ('', '', details + [f'获取网站应用页面异常: {e}'], '')

    html = r.text
    if 'webapps' not in r.url.lower():
        d(f'  [3警告] 未进入网站应用页面，当前地址: {r.url}')

    token = extract_csrf(html)
    if not token:
        d('  [3警告] 未找到安全令牌（步骤3），续期接口可能失败')
    pre = _parse_date(html)
    if pre:
        d(f'  当前到期: {pre}')
    else:
        d('  [3警告] 未能解析到期日，页面结构可能变更')
    return (pre or '', token or '', details, html)


def _pa_do_extend(session, username, html, token, webapps_url):
    """执行续期操作（找按钮 + 提交），返回 (success, details)"""
    from bs4 import BeautifulSoup
    base = 'https://www.pythonanywhere.com'
    extend_url = f'{base}/user/{username}/webapps/{username}.pythonanywhere.com/extend'
    details = []
    def d(msg):
        _log(msg)
        details.append(msg)

    d('[4] 查找续期按钮...')
    soup = BeautifulSoup(html, 'html.parser')
    btn = _find_extend_button(soup)
    if btn:
        form = btn.find_parent('form')
        if form and form.get('action'):
            act = form['action']
            post_url = f'{base}{act}' if act.startswith('/') else f'{base}/user/{username}/webapps/{act}'
        else:
            post_url = extend_url
        post_data = {'csrfmiddlewaretoken': token or ''}
        val = btn.get('value', '')
        if val:
            post_data['webapp_extend'] = val
        d(f'  找到续期按钮，提交地址: {post_url}')
    else:
        d('  未找到页面按钮，使用直接续期接口')
        post_url = extend_url
        post_data = {'csrfmiddlewaretoken': token or ''}

    d('[5] 提交续期请求...')
    session.headers['Referer'] = webapps_url
    try:
        r = session.post(post_url, data=post_data, allow_redirects=True, timeout=15)
    except Exception as e:
        d(f'  [5失败] 续期提交请求异常: {e}')
        return (False, details + [f'续期提交请求异常: {e}'])
    d(f'  服务器响应状态码 {r.status_code}')
    return (True, details)


def _pa_verify_result(session, webapps_url, pre_expiry):
    """验证续期结果，返回 (post_expiry, details)"""
    import requests
    details = []
    def d(msg):
        _log(msg)
        details.append(msg)

    d('[6] 验证续期结果...')
    try:
        r = session.get(webapps_url, timeout=15)
    except Exception as e:
        d(f'  [6失败] 验证请求异常: {e}')
        return ('', details + [f'验证请求异常: {e}'])
    post_exp = _parse_date(r.text)
    if post_exp:
        if pre_expiry and post_exp == pre_expiry:
            d(f'  到期日不变: {post_exp}（已续至最长）')
        else:
            d(f'  到期日已延至: {post_exp}')
    else:
        d('  [6警告] 未能解析续期后到期日')
    return (post_exp or '', details)


def _do_renew(username, password):
    """执行 PA 续期的主协调函数"""
    import requests
    s = requests.Session()
    s.headers['User-Agent'] = 'Mozilla/5.0 Chrome/125.0.0.0 Safari/537.36'
    all_details = []
    base = 'https://www.pythonanywhere.com'
    webapps_url = f'{base}/user/{username}/webapps/'

    # 步骤1+2: 登录
    ok, token_err, details, _ = _pa_login(s, username, password)
    all_details.extend(details)
    if not ok:
        return (False, '', '', all_details)

    # 步骤3: 获取状态（同时获取 HTML 避免后续重复请求）
    pre, token, details, html = _pa_get_status(s, webapps_url)
    all_details.extend(details)

    # 如果步骤3未获取到 token，用已有 HTML 再解析一次
    if not token and html:
        token = extract_csrf(html)
        pre2 = _parse_date(html)
        if pre2:
            pre = pre2

    # 步骤4+5: 执行续期（复用步骤3的 HTML，不再重复请求）
    if not html:
        r = s.get(webapps_url, timeout=15)
        html = r.text
        if not token:
            token = extract_csrf(html)
    ok, details = _pa_do_extend(s, username, html, token, webapps_url)
    all_details.extend(details)

    # 步骤6: 验证结果
    post, details = _pa_verify_result(s, webapps_url, pre)
    all_details.extend(details)
    return (True, pre, post, all_details)


def _renew_thread(username, password, api_token=''):
    try:
        with _status_lock:
            _status['running'] = True
        ok, pre, post, details = _do_renew(username, password)
        ts = now_ts().strftime('%Y-%m-%d %H:%M:%S')

        # 汇总日志
        renewed = False  # 是否成功续期（到期日有变化）
        if post and pre and post != pre:
            _log(f'[结果] 续期成功: {pre} -> {post}')
            renewed = True
        elif post and pre and post == pre:
            _log(f'[结果] 无需续期，到期日不变: {post}（已是最长期限）')
        elif ok and not post:
            _log(f'[结果] 操作已完成但未获取到到期日（可能页面结构变更）')
        elif not ok:
            err_summary = '; '.join(details) if details else '原因未知'
            _log(f'[结果] 续期失败: {err_summary}')

        result = {'success': ok, 'pre': pre, 'post': post, 'time': ts, 'details': details}
        with _status_lock:
            _status['last_result'] = result

        conn = _get_db()
        try:
            if renewed:
                conn.execute(
                    "UPDATE pa_config SET last_run = ?, expiry = ?, last_result = ?, updated_at = datetime('now','localtime') WHERE id = 1",
                    (ts, post, json.dumps(result, ensure_ascii=False)))
            elif post:
                # 无须续期（已是最长），更新到期日和last_run，下次按正常周期检查
                conn.execute(
                    "UPDATE pa_config SET last_run = ?, expiry = ?, last_result = ?, updated_at = datetime('now','localtime') WHERE id = 1",
                    (ts, post, json.dumps(result, ensure_ascii=False)))
            else:
                # 失败，不更新 last_run 和 expiry，允许下次重试
                conn.execute(
                    "UPDATE pa_config SET last_result = ?, updated_at = datetime('now','localtime') WHERE id = 1",
                    (json.dumps(result, ensure_ascii=False),))
            conn.execute("INSERT INTO pa_history (time, expiry, success, details) VALUES (?, ?, ?, ?)",
                         (ts, post or pre or '', 1 if ok else 0, json.dumps(details, ensure_ascii=False)))
            conn.execute("DELETE FROM pa_history WHERE id NOT IN (SELECT id FROM pa_history ORDER BY id DESC LIMIT 50)")
            conn.commit()
        except Exception as e:
            _log(f'[错误] 保存续期结果异常: {e}')
        finally:
            conn.close()
    except Exception as e:
        _log(f'[错误] 续期异常: {e}')
        with _status_lock:
            _status['last_result'] = {'success': False, 'error': str(e), 'time': now_ts().strftime('%Y-%m-%d %H:%M:%S')}
    finally:
        with _status_lock:
            _status['running'] = False


def _auto_renew_thread():
    """基于上次成功续期时间 + 周期判断，自适应检查间隔"""
    # 启动后先等待60秒，让服务完全就绪，避免网页抓取影响首次请求响应
    time_mod.sleep(60)
    while True:
        try:
            cfg = _load_config()
            username = cfg.get('username', '')
            password = cfg.get('password', '')
            api_token = cfg.get('api_token', '')
            interval = cfg.get('interval_days', 7)
            expiry = cfg.get('expiry', '')
            last_run = cfg.get('last_run', '')

            sleep_sec = 86400  # 默认24小时后

            # 续期必须使用密码（PA API 不支持账号续期，需网页模拟登录）
            has_creds = username and password
            if has_creds and not _status['running']:
                should_renew = False

                if last_run:
                    try:
                        last_run_dt = datetime.strptime(last_run, '%Y-%m-%d %H:%M:%S')
                        last_run_dt = last_run_dt.replace(tzinfo=TZ)
                        next_run_dt = last_run_dt + timedelta(days=interval)
                        now = now_ts()
                        if now >= next_run_dt:
                            should_renew = True
                        else:
                            sleep_sec = max(3600, (next_run_dt - now).total_seconds())
                    except Exception:
                        should_renew = True
                else:
                    should_renew = True  # 首次运行（从未续期过）

                if not should_renew:
                    next_check = (now_ts() + timedelta(seconds=sleep_sec)).strftime('%Y-%m-%d %H:%M')
                    _log(f'[检查] 距上次续期不足 {interval} 天，下次续期时间 {next_run_dt.strftime("%Y-%m-%d %H:%M")}，下次检查 {next_check}')
                else:
                    # 防频繁：至少间隔1天
                    blocked = False
                    if last_run:
                        try:
                            last_run_dt = datetime.strptime(last_run, '%Y-%m-%d %H:%M:%S')
                            last_run_dt = last_run_dt.replace(tzinfo=TZ)
                            if (now_ts() - last_run_dt).total_seconds() < 86400:
                                blocked = True
                        except Exception:
                            pass

                    if blocked:
                        _log(f'[检查] 距上次续期不足1天，跳过')
                    else:
                        auth_mode = 'API Token' if api_token else '账号密码'
                        _log(f'[自动] 距上次续期已达 {interval} 天，开始续期')
                        print(f'[{now_ts().strftime("%Y-%m-%d %H:%M:%S")}] PA自动续期触发 ({auth_mode})' + f' (距上次续期>={interval}天)')
                        threading.Thread(target=_renew_thread, args=(username, password, api_token), daemon=True).start()
                    # 续期已触发，直接睡到下次周期，避免24h后的冗余检查
                    sleep_sec = max(86400, interval * 86400)
            else:
                # 未配置或正执行中
                if not has_creds:
                    _log('[检查] 未配置账号或认证方式')
                    sleep_sec = 86400  # 未配置，24小时后再检查

            time_mod.sleep(sleep_sec)
        except Exception as e:
            ts = now_ts().strftime('%Y-%m-%d %H:%M:%S')
            print(f'[{ts}] PA自动续期检查异常: {e}')
            _log(f'[错误] 自动续期检查异常: {e}')
            time_mod.sleep(86400)


def start_auto_renew():
    t = threading.Thread(target=_auto_renew_thread, daemon=True)
    t.start()


# ==================== 定时任务公开接口 ====================

def run_renewal() -> tuple[bool, str]:
    """公开接口：检查并执行续期（供 daily_task.py 定时脚本调用）
    返回 (success: bool, message: str)"""
    try:
        cfg = _load_config()
        username = cfg.get('username', '')
        password = cfg.get('password', '')
        if not username or not password:
            _log('[定时] 未配置账号密码，跳过')
            return (True, '未配置账号密码，跳过')

        interval = cfg.get('interval_days', 7)
        last_run = cfg.get('last_run', '')

        should_renew = False
        if last_run:
            try:
                last_run_dt = datetime.strptime(last_run, '%Y-%m-%d %H:%M:%S')
                last_run_dt = last_run_dt.replace(tzinfo=TZ)
                next_run_dt = last_run_dt + timedelta(days=interval)
                if now_ts() >= next_run_dt:
                    should_renew = True
            except Exception:
                should_renew = True
        else:
            should_renew = True

        if not should_renew:
            return (True, '未到续期时间')

        # 防频繁：至少间隔1天
        if last_run:
            try:
                last_run_dt = datetime.strptime(last_run, '%Y-%m-%d %H:%M:%S')
                last_run_dt = last_run_dt.replace(tzinfo=TZ)
                if (now_ts() - last_run_dt).total_seconds() < 86400:
                    return (True, '距上次续期不足1天，跳过')
            except Exception:
                pass

        _log(f'[定时] 开始执行续期')
        print(f'[{now_ts().strftime("%Y-%m-%d %H:%M:%S")}] 定时任务触发 PA 续期')

        # 同步执行续期（不另开线程）
        _renew_thread(username, password)

        result = _status.get('last_result', {})
        if result.get('success'):
            return (True, f'续期完成: {result.get("pre")} -> {result.get("post")}')
        else:
            return (False, f'续期失败: {result}')
    except Exception as e:
        _log(f'[定时] 续期异常: {e}')
        return (False, f'续期异常: {e}')

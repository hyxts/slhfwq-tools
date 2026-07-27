# -*- coding: utf-8 -*-
"""独立脚本：触发 PA Web 应用 Reload。
优先使用 PA API Token，备用网页模拟登录。
由 git-pull 通过 subprocess 调用。"""
import os, sys, time, json, sqlite3, base64, hashlib, threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, 'routes'))

try:
    from utils import FOLDER_MAP, db_has_data, decode_pw, make_db
except ImportError:
    FOLDER_MAP = {
        'countdown': '倒计时', 'gpa': '绩点', 'hsgrades': '成绩',
        'pa': '服务器', 'renqing': '人情', 'deploy': '部署',
    }
    def db_has_data(db_path):
        try:
            conn = sqlite3.connect(db_path)
            tables = [t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
            for t in tables:
                if conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0] > 0:
                    conn.close(); return True
            conn.close()
            return False
        except Exception:
            return False
    def decode_pw(encoded, salt_path):
        if not encoded:
            return ''
        try:
            data = base64.b64decode(encoded)
            key = hashlib.sha256(salt_path.encode()).digest()[:16]
            return bytes(b ^ key[i % len(key)] for i, b in enumerate(data)).decode('utf-8')
        except Exception:
            return ''
    def make_db(db_file):
        def _get_db():
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            return conn
        return _get_db

PA_DIR = os.path.join(BASE_DIR, '服务器')
DB_FILE = os.path.join(PA_DIR, 'pa.db')
LOG_FILE = os.path.join(BASE_DIR, 'reload.log')
PA_LOG_FILE = os.path.join(BASE_DIR, '服务器', 'renew.log')
MAX_LOG_LINES = 200
_LOG_LOCK = threading.Lock()


def log(msg):
    """写日志到文件（带自动截断，加锁防止并发竞争）"""
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    with _LOG_LOCK:
        for log_path in (LOG_FILE, PA_LOG_FILE):
            try:
                existing_lines = []
                if os.path.exists(log_path):
                    with open(log_path, 'r', encoding='utf-8') as f:
                        existing_lines = f.readlines()
                existing_lines.append(line + '\n')
                with open(log_path, 'w', encoding='utf-8') as f:
                    f.writelines(existing_lines[-MAX_LOG_LINES:])
            except Exception:
                pass


def load_credentials():
    """从 pa.db 读取 PA 账号密码和 API Token"""
    username, password, api_token = '', '', ''
    db_to_try = [DB_FILE]
    old_db = os.path.join(BASE_DIR, 'pa', 'pa.db')
    if os.path.exists(old_db) and not os.path.exists(DB_FILE):
        db_to_try.insert(0, old_db)
    for db_path in db_to_try:
        if not os.path.exists(db_path):
            continue
        conn = None
        try:
            get_db = make_db(db_path)
            conn = get_db()
            try:
                conn.execute("ALTER TABLE pa_config ADD COLUMN api_token TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            row = conn.execute("SELECT username, password, api_token FROM pa_config WHERE id = 1").fetchone()
            if row:
                if row['username'] and row['password']:
                    username = row['username']
                    pw = row['password']
                    if pw.startswith('ENC:'):
                        pw = decode_pw(pw[4:], PA_DIR)
                    elif pw.startswith('v2:'):
                        pw = decode_pw(pw, PA_DIR)
                    password = pw
                try:
                    if row['api_token']:
                        api_token = row['api_token']
                except (IndexError, KeyError):
                    pass
                break
        except Exception as e:
            log(f'读取PA凭证失败({db_path}): {e}')
        finally:
            if conn:
                conn.close()
    return username, password, api_token


def reload_via_api(username, api_token):
    """通过 PA 官方 API 触发重载（最快：1-3秒）"""
    try:
        import requests
    except ImportError:
        log('requests 未安装')
        return False

    if not api_token:
        log('未配置 PA API Token')
        return False

    domain = f'{username}.pythonanywhere.com'
    url = f'https://www.pythonanywhere.com/api/v0/user/{username}/webapps/{domain}/reload/'
    log(f'API重载: POST {url}')

    for attempt in range(2):
        try:
            r = requests.post(url, headers={
                'Authorization': f'Token {api_token}',
                'User-Agent': 'QoderDeploy/1.0',
            }, timeout=10)
            log(f'API响应[{attempt+1}/2]: HTTP {r.status_code}')
            if r.status_code in (200, 202, 204):
                log('API重载成功')
                return True
            if r.status_code in (429, 502, 503, 504) and attempt < 1:
                time.sleep(2)
                continue
            log(f'API重载失败(HTTP {r.status_code}): {r.text[:200]}')
            return False
        except Exception as e:
            log(f'API请求异常[{attempt+1}/2]: {e}')
            if attempt < 1:
                time.sleep(2)
                continue
            return False
    return False


def reload_via_web(username, password):
    """通过 PA 网页模拟登录点击 Reload 按钮"""
    try:
        import requests
        from bs4 import BeautifulSoup
        import re
    except ImportError:
        log('缺少 requests/bs4 库')
        return False

    if not username or not password:
        log('未配置 PA 账号密码')
        return False

    base = 'https://www.pythonanywhere.com'
    webapps_url = f'{base}/user/{username}/webapps/'
    domain = f'{username}.pythonanywhere.com'

    s = requests.Session()
    s.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36'

    # 1. 获取登录页
    log('网页重载 1/4: 获取登录页...')
    try:
        r = s.get(webapps_url, timeout=10)
    except Exception as e:
        log(f'获取页面失败: {e}')
        return False

    # 提取 CSRF token
    soup = BeautifulSoup(r.text, 'html.parser')
    token = None
    inp = soup.find('input', {'name': 'csrfmiddlewaretoken'})
    if inp:
        token = inp.get('value', '')
    if not token:
        for script in soup.find_all('script'):
            if script.string:
                m = re.search(r'Anywhere\.csrfToken\s*=\s*"([^"]+)"', script.string)
                if m:
                    token = m.group(1)
                    break
    if not token:
        log('未获取到 CSRF token')
        return False

    # 2. 登录
    log('网页重载 2/4: 登录...')
    s.headers['Referer'] = r.url
    try:
        r = s.post(f'{base}/login/', data={
            'csrfmiddlewaretoken': token,
            'auth-username': username,
            'auth-password': password,
            'next': f'/user/{username}/webapps/',
            'login_view-current_step': 'auth',
        }, allow_redirects=True, timeout=10)
    except Exception as e:
        log(f'登录请求失败: {e}')
        return False

    if 'login' in r.url.lower() and 'webapps' not in r.url.lower():
        log(f'PA登录失败 (URL: {r.url})')
        return False
    log('登录成功')

    # 3. 构造 Reload URL 并直接 POST（PA 多使用 POST 触发重载）
    reload_path = f'/user/{username}/webapps/{domain}/reload'
    full_reload_url = f'{base}{reload_path}'
    log(f'网页重载 3/4: 执行重载...')

    try:
        # 先访问 webapps 页面确保认证有效
        r1 = s.get(webapps_url, timeout=10)
        # 访问 reload 页面获取表单 token
        r2 = s.get(full_reload_url, allow_redirects=True, timeout=10)

        # 提取可能的 CSRF token
        token2 = None
        soup2 = BeautifulSoup(r2.text, 'html.parser')
        inp2 = soup2.find('input', {'name': 'csrfmiddlewaretoken'})
        if inp2:
            token2 = inp2.get('value', '')
        if not token2:
            for script in soup2.find_all('script'):
                if script.string:
                    m2 = re.search(r'Anywhere\.csrfToken\s*=\s*"([^"]+)"', script.string)
                    if m2:
                        token2 = m2.group(1)
                        break

        if token2:
            log('提交重载表单...')
            r3 = s.post(full_reload_url, data={'csrfmiddlewaretoken': token2},
                       allow_redirects=True, timeout=10)
            log(f'POST响应: HTTP {r3.status_code}')
        else:
            log('无表单，GET应已触发重载')

        log('网页重载完成')
        return True
    except Exception as e:
        log(f'重载请求失败: {e}')
        return False


def migrate_old_folders():
    """迁移旧的英文文件夹到新的中文文件夹（在重载前执行）"""
    for old_name, new_name in FOLDER_MAP.items():
        old_path = os.path.join(BASE_DIR, old_name)
        new_path = os.path.join(BASE_DIR, new_name)
        if not os.path.isdir(old_path):
            continue
        try:
            os.makedirs(new_path, exist_ok=True)
            import shutil
            for fname in os.listdir(old_path):
                old_fpath = os.path.join(old_path, fname)
                new_fpath = os.path.join(new_path, fname)
                if not os.path.isfile(old_fpath):
                    continue
                old_has = db_has_data(old_fpath) if fname.endswith('.db') else (os.path.getsize(old_fpath) > 0)
                new_has = db_has_data(new_fpath) if os.path.exists(new_fpath) and fname.endswith('.db') else (os.path.exists(new_fpath) and os.path.getsize(new_fpath) > 0)
                if old_has and new_has:
                    log(f'保留新版: {new_name}/{fname} (新旧都有数据)')
                    continue
                if not old_has and new_has:
                    continue
                if not old_has and not new_has:
                    continue
                shutil.copy2(old_fpath, new_fpath)
                log(f'迁移: {old_name}/{fname} -> {new_name}/{fname}')
            shutil.rmtree(old_path, ignore_errors=True)
            log(f'删除旧目录: {old_name}/')
        except Exception as e:
            log(f'迁移失败({old_name}): {e}')


def verify_service(username, max_wait=20):
    """轮询 /api/ping 直到新版本就绪，最多等 max_wait 秒"""
    try:
        import requests as _req
    except ImportError:
        return False

    url = f'https://{username}.pythonanywhere.com/api/ping'
    log(f'等待新进程就绪(最多{max_wait}秒)...')
    start = time.time()

    # PA 重载通常需要 5-10 秒启动新 worker
    # 先等 4 秒让 PA 处理重载请求
    time.sleep(4)

    for i in range(8):
        elapsed = time.time() - start
        if elapsed >= max_wait:
            break
        try:
            r = _req.get(url, timeout=5)
            if r.status_code == 200:
                data = r.json()
                log(f'服务已就绪 运行{data.get("uptime_seconds",0)}秒')
                return True
        except Exception:
            pass
        time.sleep(2)
    log(f'验证超时({max_wait}秒)，重载可能仍在进行中')
    return False  # 不报失败，PA 后台可能还在处理


def main():
    log('===== 开始重载 =====')

    username, password, api_token = load_credentials()
    log(f'用户: {username}, Token: {"有" if api_token else "无"}')

    success = False
    # 方式1: PA API（最快）
    if username and api_token:
        log('方式1: API重载...')
        success = reload_via_api(username, api_token)
        if not success:
            log('API重载失败，fallback...')

    # 方式2: 网页模拟登录
    if not success and username and password:
        log('方式2: 网页重载...')
        success = reload_via_web(username, password)

    if success:
        verify_service(username)
        log('===== 重载完成 =====')
        sys.exit(0)

    log('所有方式均失败')
    log('===== 重载失败 =====')
    sys.exit(1)


if __name__ == '__main__':
    main()

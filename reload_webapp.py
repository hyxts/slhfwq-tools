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
        'paiban': '排班', 'pa': '服务器', 'renqing': '人情', 'deploy': '部署',
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
                # 只保留最近 N 行
                with open(log_path, 'w', encoding='utf-8') as f:
                    f.writelines(existing_lines[-MAX_LOG_LINES:])
            except Exception:
                pass


def load_credentials():
    """从 pa.db 读取 PA 账号密码和 API Token（兼容旧路径，使用统一编码/解码函数）"""
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
                    # 统一使用 decode_pw 处理新旧格式
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
    """通过 PA 官方 API 触发重载（最可靠）"""
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

    for attempt in range(3):
        try:
            r = requests.post(url, headers={
                'Authorization': f'Token {api_token}',
                'User-Agent': 'QoderDeploy/1.0',
            }, timeout=15)
            log(f'API响应[尝试{attempt+1}/3]: HTTP {r.status_code}')
            if r.status_code in (200, 202, 204):
                log('API重载成功')
                return True
            # PA 部分状态码可重试（429 频率限制、502/503/504 网关错误）
            if r.status_code in (429, 502, 503, 504) and attempt < 2:
                wait = (attempt + 1) * 3
                log(f'等待{wait}秒后重试...')
                time.sleep(wait)
                continue
            log(f'API重载失败(HTTP {r.status_code}): {r.text[:200]}')
            return False
        except Exception as e:
            log(f'API请求异常[尝试{attempt+1}/3]: {e}')
            if attempt < 2:
                time.sleep(3)
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
        r = s.get(webapps_url, timeout=15)
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
        }, allow_redirects=True, timeout=15)
    except Exception as e:
        log(f'登录请求失败: {e}')
        return False

    if 'login' in r.url.lower() and 'webapps' not in r.url.lower():
        log(f'PA登录失败 (URL: {r.url})')
        return False
    log('登录成功')

    # 3. 直接构造 Reload URL（PA webapp页面上的链接格式）
    reload_path = f'/user/{username}/webapps/{domain}/reload'
    full_reload_url = f'{base}{reload_path}'
    log(f'网页重载 3/4: 直接访问 {reload_path}')

    # 4. GET reload 页面，如果有表单则 POST 提交
    log(f'网页重载 4/4: 执行重载...')
    try:
        # 先访问 webapps 页面确保已认证
        s.get(webapps_url, timeout=15)
        # 访问 reload 页面
        r = s.get(full_reload_url, allow_redirects=True, timeout=15)
        log(f'GET响应: HTTP {r.status_code}')

        # 尝试从 reload 页面提取 CSRF token 并提交表单
        token2 = None
        soup2 = BeautifulSoup(r.text, 'html.parser')
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
            log('发现CSRF表单，POST提交...')
            r = s.post(full_reload_url, data={'csrfmiddlewaretoken': token2},
                      allow_redirects=True, timeout=15)
            log(f'POST响应: HTTP {r.status_code}')

        # 检查页面是否包含成功标志
        page_text = r.text.lower()
        if 'success' in page_text or 'reloaded' in page_text or 'queued' in page_text:
            log('检测到成功标志')
        elif r.status_code in (200, 302):
            log('HTTP状态正常，重载应已触发')

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
                # 根据实际数据判断是否覆盖：旧文件有数据，新文件无数据 -> 覆盖
                old_has = db_has_data(old_fpath) if fname.endswith('.db') else (os.path.getsize(old_fpath) > 0)
                new_has = db_has_data(new_fpath) if os.path.exists(new_fpath) and fname.endswith('.db') else (os.path.exists(new_fpath) and os.path.getsize(new_fpath) > 0)
                if old_has and new_has:
                    # 两者都有数据，保留新文件的（可能已有更新），但记录一下
                    log(f'保留新版: {new_name}/{fname} (新旧都有数据)')
                    continue
                if not old_has and new_has:
                    continue
                if not old_has and not new_has:
                    continue
                # old_has=True, new_has=False -> 覆盖
                shutil.copy2(old_fpath, new_fpath)
                log(f'迁移: {old_name}/{fname} -> {new_name}/{fname}')
            shutil.rmtree(old_path, ignore_errors=True)
            log(f'删除旧目录: {old_name}/')
        except Exception as e:
            log(f'迁移失败({old_name}): {e}')


def restore_from_backup():
    """从服务器备份 zip 仅恢复丢失或为空的数据库，不覆盖已有活跃数据"""
    backup_dir = os.path.join(BASE_DIR, 'backups')
    if not os.path.isdir(backup_dir):
        log('无备份目录')
        return False
    zips = sorted([f for f in os.listdir(backup_dir) if f.endswith('.zip')], reverse=True)
    if not zips:
        log('备份目录为空')
        return False
    log(f'备份: {len(zips)}个')
    import zipfile
    db_targets = {
        'gifts.db': os.path.join(BASE_DIR, '人情', 'gifts.db'),
        'paiban.db': os.path.join(BASE_DIR, '排班', 'paiban.db'),
        'gpa.db': os.path.join(BASE_DIR, '绩点', 'gpa.db'),
        'hsgrades.db': os.path.join(BASE_DIR, '成绩', 'hsgrades.db'),
        'countdown.db': os.path.join(BASE_DIR, '倒计时', 'countdown.db'),
        'pa.db': os.path.join(BASE_DIR, '服务器', 'pa.db'),
    }
    try:
        for zname in zips:
            zp = os.path.join(backup_dir, zname)
            try:
                with zipfile.ZipFile(zp, 'r') as zf:
                    names = zf.namelist()
                    has_any = any(
                        n in names and zf.getinfo(n).file_size > 4096
                        for n in db_targets
                    )
                    if not has_any:
                        log(f'  跳过 {zname} (无有效数据)')
                        continue
                    log(f'使用备份: {zname}')
                    restored = []
                    skipped = []
                    for fname in names:
                        if fname in db_targets:
                            data = zf.read(fname)
                            if len(data) < 4096:
                                continue
                            target = db_targets[fname]
                            # 仅当目标文件不存在、为空或已损坏时恢复，不覆盖已有活跃数据
                            if os.path.exists(target) and os.path.getsize(target) >= 4096:
                                skipped.append(fname)
                                continue
                            os.makedirs(os.path.dirname(target), exist_ok=True)
                            with open(target, 'wb') as f:
                                f.write(data)
                            restored.append(f'{fname}({len(data)}B)')
                    if restored:
                        log(f'备份恢复完成(新增): {", ".join(restored)}')
                    if skipped:
                        log(f'跳过已有数据: {", ".join(skipped)}')
                    if restored or skipped:
                        return True
                    break
            except Exception as ze:
                log(f'  读取 {zname} 失败: {ze}')
                continue
        log('所有备份均无有效数据')
        return False
    except Exception as e:
        log(f'备份恢复失败: {e}')
        return False


def show_backup_summary():
    """诊断摘要：对比备份和当前数据库（放到最后执行，确保出现在日志末尾）"""
    import zipfile as _zf, tempfile as _tmp, shutil as _sh
    backup_dir = os.path.join(BASE_DIR, 'backups')
    if not os.path.isdir(backup_dir):
        log('诊断: 无备份目录')
        return
    zips = sorted([f for f in os.listdir(backup_dir) if f.endswith('.zip')], reverse=True)
    if not zips:
        log('诊断: 备份目录为空')
        return
    log(f'----- 诊断: {len(zips)}个备份 -----')
    # 列出所有备份，每个备份显示关键数据库的数据量
    for zname in zips[:5]:
        zp = os.path.join(backup_dir, zname)
        try:
            with _zf.ZipFile(zp, 'r') as zz:
                names = zz.namelist()
                parts = [f'{len(names)}个文件']
                for key in ['countdown.db', 'gpa.db', 'gifts.db', 'hsgrades.db', 'paiban.db']:
                    if key in names:
                        sz = zz.getinfo(key).file_size
                        parts.append(f'{key}={sz}B')
                log(f'  {zname}: {", ".join(parts)}')
        except Exception as e:
            log(f'  {zname}: 读取失败({e})')
    # 打开最新备份，提取 countdown 和 gpa 的实际数据内容
    if zips:
        zp = os.path.join(backup_dir, zips[0])
        tmpdir = _tmp.mkdtemp()
        try:
            with _zf.ZipFile(zp, 'r') as zz:
                zz.extractall(tmpdir)
            # 打印备份中 countdown.db 的事件
            for dbn, display in [('countdown.db', '倒计时'), ('gpa.db', '绩点')]:
                bp = os.path.join(tmpdir, dbn)
                if os.path.exists(bp):
                    try:
                        c = sqlite3.connect(bp)
                        if dbn == 'countdown.db':
                            rows = c.execute("SELECT id,name FROM events").fetchall()
                            c.close()
                            log(f'  {display}备份内容: {len(rows)}条 - {[f"{r[0]}:{r[1]}" for r in rows]}')
                        elif dbn == 'gpa.db':
                            r = c.execute("SELECT semesters,courses FROM gpa_data").fetchone()
                            c.close()
                            if r:
                                import json as _j
                                sem = _j.loads(r[0]) if r[0] else []
                                crs = _j.loads(r[1]) if r[1] else []
                                log(f'  {display}备份内容: {len(sem)}学期/{len(crs)}门课')
                            else:
                                log(f'  {display}备份内容: 无数据')
                    except Exception as e:
                        log(f'  {display}备份读取失败: {e}')
        finally:
            _sh.rmtree(tmpdir, ignore_errors=True)
    log('----- 诊断结束 -----')


def quick_diag():
    """快速诊断：打印备份和当前数据详情"""
    import zipfile as _zf, tempfile as _tmp, json as _j
    parts = []
    # 备份
    backup_dir = os.path.join(BASE_DIR, 'backups')
    if os.path.isdir(backup_dir):
        zips = sorted([f for f in os.listdir(backup_dir) if f.endswith('.zip')], reverse=True)
        if zips:
            zp = os.path.join(backup_dir, zips[0])
            with _zf.ZipFile(zp, 'r') as zz:
                info_list = [(n, zz.getinfo(n).file_size) for n in zz.namelist()]
            parts.append(f'BK:{len(zips)}[{",".join(f"{n[:4]}:{s}" for n,s in sorted(info_list))}]')
    # 当前数据
    for fname, dname in [('gpa.db','绩点'),('countdown.db','倒计时'),('gifts.db','人情')]:
        p = os.path.join(BASE_DIR, dname, fname)
        if os.path.exists(p):
            c = None
            try:
                c = sqlite3.connect(p)
                if fname == 'gpa.db':
                    r = c.execute("SELECT semesters,courses FROM gpa_data").fetchone()
                    if r:
                        sem = _j.loads(r[0]) if r[0] else []
                        crs = _j.loads(r[1]) if r[1] else []
                        parts.append(f'GPA:{len(sem)}学期{len(crs)}课')
                elif fname == 'countdown.db':
                    n = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                    parts.append(f'CD:{n}事件')
                elif fname == 'gifts.db':
                    n = c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                    parts.append(f'GIFT:{n}条')
            except Exception:
                pass
            finally:
                if c:
                    c.close()
    print('DIAG:' + ' '.join(parts), flush=True)


if __name__ == '__main__':
    log('===== 开始重载 =====')

    username, password, api_token = load_credentials()
    log(f'用户名: {username}, API Token: {"已配置" if api_token else "未配置"}')

    success = False
    # 方式1: PA API（最可靠）
    if username and api_token:
        log('尝试API重载...')
        if reload_via_api(username, api_token):
            success = True
        else:
            log('API重载失败，尝试网页方式...')

    # 方式2: 网页模拟登录
    if not success and username and password:
        log('尝试网页重载...')
        if reload_via_web(username, password):
            success = True
        else:
            log('网页重载失败')

    if success:
        # 等待并验证新服务可用
        log('等待新进程启动(最多30秒)...')
        try:
            import requests as _req
            for i in range(10):
                time.sleep(3)
                try:
                    r = _req.get(f'https://{username}.pythonanywhere.com/api/status', timeout=5)
                    if r.status_code == 200:
                        log(f'验证成功: 服务已恢复(耗时约{(i+1)*3}秒)')
                        break
                except Exception:
                    if i < 9:
                        continue
                    log(f'验证超时: 服务未在30秒内恢复')
        except Exception as e:
            log(f'验证异常: {e}')
        log('===== 重载完成 =====')
        sys.exit(0)

    # 方式3: 所有重载方式都失败
    log('所有重载方式均失败，需要手动处理')

    log('===== 重载失败: 所有方式均失败 =====')
    print('DIAG_DONE', flush=True)
    sys.exit(1)

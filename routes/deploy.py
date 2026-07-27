# -*- coding: utf-8 -*-
"""部署工具 Blueprint (git-pull, 服务器状态)"""
import os, sys, subprocess, json, base64, sqlite3, shutil, time as time_mod, threading
from datetime import datetime

from .utils import TZ, _size_str, db_has_data, FOLDER_MAP
from flask import Blueprint, jsonify, request

bp = Blueprint('deploy', __name__)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START_TIME = datetime.now(TZ)  # 服务启动时间

# 服务启动后的"就绪"时间戳（reload 后延迟记录，用于判断是否已就绪）
_READY_TS = START_TIME


@bp.route('/api/ping')
def ping():
    """极轻量健康检查：不连DB、不做任何IO，纯返回启动信息。
    部署流程用此端点快速判断服务是否已启动就绪。"""
    delta = (datetime.now(TZ) - _READY_TS).total_seconds()
    return jsonify({
        'success': True,
        'status': 'ok',
        'uptime_seconds': round(delta, 1),
        'started_at': _READY_TS.isoformat() if hasattr(_READY_TS, 'isoformat') else str(_READY_TS),
    })

_STATUS_CACHE = {'data': None, 'timestamp': 0}
_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE_TTL = 300  # 缓存5分钟，避免频繁重建状态


@bp.route('/api/git-pull', methods=['POST'])
def git_pull():
    reload_msg = ''
    try:
        force = request.args.get('force', '0') == '1'
        if not force:
            try:
                body = request.get_json(silent=True) or {}
                force = body.get('force', False)
            except Exception:
                pass

        if force:
            subprocess.run(['git', 'fetch', '--all'], cwd=BASE_DIR, capture_output=True, text=True, timeout=30)
            r = subprocess.run(['git', 'reset', '--hard', 'origin/master'], cwd=BASE_DIR, capture_output=True, text=True, timeout=30)
        else:
            r = subprocess.run(['git', 'pull'], cwd=BASE_DIR, capture_output=True, text=True, timeout=30)

        if r.returncode == 0 and 'Already up to date' not in r.stdout:
            # 代码有变更，先清理旧目录（迁移数据库到新中文目录）
            cleanup_msg = ''
            try:
                for old_n, new_n in FOLDER_MAP.items():
                    old_p = os.path.join(BASE_DIR, old_n)
                    new_p = os.path.join(BASE_DIR, new_n)
                    if not os.path.isdir(old_p):
                        continue
                    os.makedirs(new_p, exist_ok=True)
                    for fn in os.listdir(old_p):
                        ofp = os.path.join(old_p, fn)
                        nfp = os.path.join(new_p, fn)
                        if not os.path.isfile(ofp):
                            continue
                        old_has = db_has_data(ofp) if fn.endswith('.db') else (os.path.getsize(ofp) > 0)
                        new_has = db_has_data(nfp) if os.path.exists(nfp) and fn.endswith('.db') else (os.path.exists(nfp) and os.path.getsize(nfp) > 0)
                        if old_has and not new_has:
                            shutil.copy2(ofp, nfp)
                    shutil.rmtree(old_p, ignore_errors=True)
                    cleanup_msg += f'{old_n}已清理; '
                cleanup_msg = cleanup_msg.strip('; ')
            except Exception as e:
                cleanup_msg = f'清理异常: {e}'

            # 清除 .pyc 缓存，确保子进程从磁盘加载最新 .py 文件
            reload_script = os.path.join(BASE_DIR, 'reload_webapp.py')
            pycache = os.path.join(BASE_DIR, '__pycache__')
            if os.path.isdir(pycache):
                try:
                    for f in os.listdir(pycache):
                        if 'reload' in f.lower():
                            os.remove(os.path.join(pycache, f))
                except Exception:
                    pass
            if os.path.exists(reload_script):
                try:
                    # PA生产环境sys.executable指向uwsgi，需找真正的python
                    py_exe = sys.executable
                    if 'uwsgi' in py_exe.lower():
                        py_exe = os.path.join(os.path.dirname(py_exe), 'python3')
                        if not os.path.exists(py_exe):
                            py_exe = '/usr/bin/python3'
                    # fire-and-forget: 不阻塞API响应，重载脚本在后台执行
                    subprocess.Popen(
                        [py_exe, reload_script],
                        cwd=BASE_DIR,
                        stdout=open(os.path.join(BASE_DIR, 'reload_stdout.log'), 'a'),
                        stderr=open(os.path.join(BASE_DIR, 'reload_stderr.log'), 'a'),
                        start_new_session=True)
                    reload_msg = '重载已触发(后台执行)'
                except Exception as e:
                    reload_msg = f'重载启动异常: {e}'
            else:
                reload_msg = '重载脚本未找到'
        elif r.returncode == 0:
            reload_msg = '代码已是最新，无需重载'
        else:
            reload_msg = ''

        return jsonify({
            'success': r.returncode == 0,
            'stdout': r.stdout.strip(),
            'stderr': r.stderr.strip(),
            'cleanup': cleanup_msg if force else '',
            'reload': reload_msg or ('执行失败' if r.returncode != 0 else '')
        })
    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'Git pull 超时'}), 408
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/git-log')
def git_log():
    try:
        r = subprocess.run(['git', 'log', '--oneline', '-20'], cwd=BASE_DIR,
                           capture_output=True, text=True, timeout=10)
        commits = []
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = line.split(' ', 1)
            commits.append({
                'hash': parts[0],
                'message': parts[1] if len(parts) > 1 else '',
            })
        return jsonify({'success': True, 'commits': commits})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _dir_size(path):
    """递归计算目录总大小（排除 .git 和 backups 等非业务目录），使用 scandir 提速"""
    SKIP_DIRS = {'.git', 'backups', 'backup', '__pycache__'}
    total = 0
    try:
        dirs_to_visit = [path]
        while dirs_to_visit:
            current = dirs_to_visit.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.is_file(follow_symlinks=False):
                            try:
                                total += entry.stat().st_size
                            except OSError:
                                pass
                        elif entry.is_dir(follow_symlinks=False) and entry.name not in SKIP_DIRS:
                            dirs_to_visit.append(entry.path)
            except OSError:
                pass
    except Exception:
        pass
    return total


def _build_server_status():
    """构建服务器状态数据（内部函数，不使用缓存）
    优化：单次遍历收集所有目录大小，避免重复扫描"""
    QUOTA = 512 * 1024 * 1024
    SKIP_DIRS = frozenset(['.git', '__pycache__', 'backups'])

    # 一次遍历：收集总大小 + 各一级子目录大小 + 根目录文件大小
    total_used = 0
    subdir_sizes = {}  # dname -> bytes
    root_file_sz = 0
    other_dirs = {}  # 不在 STORAGE_DIRS 中的其他目录
    STORAGE_DIRS_SET = frozenset(['人情', '绩点', '成绩', '倒计时', '服务器', '记账', '部署'])
    KNOWN_DB_DIRS = ['人情', '绩点', '成绩', '倒计时', '服务器', '记账']

    try:
        for entry in os.scandir(BASE_DIR):
            try:
                if entry.is_file(follow_symlinks=False) and not entry.name.startswith('.'):
                    sz = entry.stat().st_size
                    total_used += sz
                    root_file_sz += sz
                elif entry.is_dir(follow_symlinks=False) and entry.name not in SKIP_DIRS and not entry.name.startswith('.'):
                    sz = _dir_size(entry.path)
                    total_used += sz
                    if sz > 0:
                        if entry.name in STORAGE_DIRS_SET:
                            subdir_sizes[entry.name] = sz
                        else:
                            other_dirs[entry.name] = sz
            except OSError:
                pass
    except Exception:
        # 回退到旧的 _dir_size(BASE_DIR) 方式
        total_used = _dir_size(BASE_DIR)

    free = max(0, QUOTA - total_used)
    pct = round(total_used / QUOTA * 100, 1)
    disk = {'total': _size_str(QUOTA), 'used': _size_str(total_used), 'free': _size_str(free), 'pct': pct,
            'warn': pct > 80}

    # 数据库信息（只读查询，不做 WAL 设置，数据库创建时已设好）
    KNOWN_DB_ROOTS = [(d, os.path.join(BASE_DIR, d)) for d in KNOWN_DB_DIRS]
    dbs = []
    for dirname, dirpath in KNOWN_DB_ROOTS:
        if not os.path.isdir(dirpath):
            continue
        for f in os.listdir(dirpath):
            if f.endswith('.db'):
                full_path = os.path.join(dirpath, f)
                try:
                    sz = os.path.getsize(full_path)
                    c = sqlite3.connect(full_path, timeout=2)
                    tables = [t[0] for t in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()]
                    rows = 0
                    for t in tables[:5]:
                        rows += c.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                    c.close()
                    dbs.append({'name': f'{dirname}/{f}', 'size': _size_str(sz), 'rows': rows})
                except Exception:
                    pass
    dbs.sort(key=lambda x: x['name'])

    # 日志
    logs = []
    for dirname, dirpath in KNOWN_DB_ROOTS:
        if not os.path.isdir(dirpath):
            continue
        for f in os.listdir(dirpath):
            if f.endswith('.log'):
                full_path = os.path.join(dirpath, f)
                try:
                    sz = os.path.getsize(full_path)
                    with open(full_path, 'r', encoding='utf-8') as lf:
                        lines = sum(1 for _ in lf)
                    logs.append({'name': f'{dirname}/{f}', 'size': _size_str(sz), 'lines': lines})
                except Exception:
                    pass
    for f in os.listdir(BASE_DIR):
        if f.endswith('.log'):
            full_path = os.path.join(BASE_DIR, f)
            try:
                sz = os.path.getsize(full_path)
                with open(full_path, 'r', encoding='utf-8') as lf:
                    lines = sum(1 for _ in lf)
                logs.append({'name': f, 'size': _size_str(sz), 'lines': lines})
            except Exception:
                pass
    logs.sort(key=lambda x: x['name'])

    old_dirs = []
    for d in list(FOLDER_MAP.keys()) + ['backup']:
        dpath = os.path.join(BASE_DIR, d)
        if os.path.isdir(dpath):
            old_dirs.append(d)

    # 存储分布（从已收集的单次遍历数据构建，无需再次扫描）
    dir_sizes = []
    for dname, sz in subdir_sizes.items():
        dir_sizes.append({
            'name': dname, 'bytes': sz, 'size_str': _size_str(sz),
            'pct': round(sz / QUOTA * 100, 1),
        })
    if root_file_sz > 0:
        dir_sizes.append({
            'name': '(根目录文件)', 'bytes': root_file_sz,
            'size_str': _size_str(root_file_sz), 'pct': round(root_file_sz / QUOTA * 100, 1),
        })
    for dname, sz in other_dirs.items():
        dir_sizes.append({
            'name': dname, 'bytes': sz, 'size_str': _size_str(sz),
            'pct': round(sz / QUOTA * 100, 1),
        })
    dir_sizes.sort(key=lambda x: x['bytes'], reverse=True)

    delta = datetime.now(TZ) - START_TIME
    d = delta.days
    h, m = delta.seconds // 3600, (delta.seconds % 3600) // 60
    uptime = f'{d}天{h}小时{m}分钟' if d else f'{h}小时{m}分钟'

    return {
        'disk': disk, 'dbs': dbs, 'logs': logs, 'old_dirs': old_dirs,
        'uptime': uptime, 'python': sys.version.split()[0],
        'dir_sizes': dir_sizes,
    }


def prebuild_status():
    """启动时预构建状态缓存，延迟执行避免与启动争抢 I/O"""
    try:
        time_mod.sleep(5)  # 等待服务器稳定后再开始（30s太长，PA worker可能在30s内被重启）
        with _STATUS_CACHE_LOCK:
            _STATUS_CACHE['data'] = data
            _STATUS_CACHE['timestamp'] = time_mod.time()
    except Exception:
        pass  # 预构建失败不影响正常启动


@bp.route('/api/status')
def server_status():
    """服务器概览（基于 PA 文件配额），带线程安全的缓存"""
    try:
        now = time_mod.time()
        # 检查缓存是否有效（加锁读取）
        with _STATUS_CACHE_LOCK:
            if _STATUS_CACHE['data'] and (now - _STATUS_CACHE['timestamp']) < _STATUS_CACHE_TTL:
                return jsonify(_STATUS_CACHE['data'])

        # 缓存失效，重新计算
        data = _build_server_status()
        with _STATUS_CACHE_LOCK:
            _STATUS_CACHE['data'] = data
            _STATUS_CACHE['timestamp'] = now
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/pa-summary')
def pa_summary():
    """PA续期状态摘要（轻量，供概览页快速展示）"""
    try:
        pa_db = os.path.join(BASE_DIR, '服务器', 'pa.db')
        if not os.path.exists(pa_db):
            return jsonify({'configured': False})
        conn = sqlite3.connect(pa_db, timeout=3)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT expiry, last_run, interval_days, last_result FROM pa_config WHERE id = 1").fetchone()
        conn.close()
        if not row:
            return jsonify({'configured': False})
        expiry = row['expiry'] or ''
        last_run = row['last_run'] or ''
        interval = row['interval_days'] or 7
        has_error = False
        if row['last_result']:
            try:
                last_result = json.loads(row['last_result'])
                has_error = not last_result.get('success', False)
            except Exception:
                pass
        urgent = False
        if expiry:
            try:
                exp_date = datetime.strptime(expiry[:10], '%Y-%m-%d').date()
                days_left = (exp_date - datetime.now(TZ).date()).days
                urgent = days_left <= 3
            except Exception:
                pass
        return jsonify({
            'configured': True,
            'expiry': expiry,
            'last_run': last_run,
            'interval': interval,
            'has_error': has_error,
            'urgent': urgent,
        })
    except Exception as e:
        return jsonify({'configured': False, 'error': str(e)})


@bp.route('/api/cleanup-old-folders', methods=['POST'])
def cleanup_old_folders():
    """清理服务器上的旧英文名文件夹和废弃目录"""
    results = []

    # 1. 清理废弃目录（傲视小助手等）
    ORPHAN_DIRS = ['傲视小助手', '__pycache__']
    for dname in ORPHAN_DIRS:
        dpath = os.path.join(BASE_DIR, dname)
        if os.path.isdir(dpath):
            try:
                sz = sum(os.path.getsize(os.path.join(r, f))
                         for r, _, fs in os.walk(dpath) for f in fs
                         if os.path.isfile(os.path.join(r, f)))
                shutil.rmtree(dpath)
                results.append(f'已清理废弃目录: {dname}/ ({_size_str(sz)})')
            except Exception as e:
                results.append(f'清理失败 {dname}: {e}')

    # 2. 清理旧英文名文件夹
    for old_name, new_name in FOLDER_MAP.items():
        old_path = os.path.join(BASE_DIR, old_name)
        new_path = os.path.join(BASE_DIR, new_name)
        if not os.path.isdir(old_path):
            continue

        os.makedirs(new_path, exist_ok=True)

        for fname in os.listdir(old_path):
            old_fpath = os.path.join(old_path, fname)
            new_fpath = os.path.join(new_path, fname)
            if not os.path.isfile(old_fpath):
                continue
            if os.path.exists(new_fpath) and os.path.getsize(new_fpath) >= os.path.getsize(old_fpath):
                continue
            shutil.copy2(old_fpath, new_fpath)
            results.append(f'复制: {old_name}/{fname} -> {new_name}/{fname}')

        shutil.rmtree(old_path, ignore_errors=True)
        results.append(f'删除目录: {old_name}/')

    if not results:
        results.append('没有需要清理的旧文件夹')
    return jsonify({'success': True, 'results': results})


@bp.route('/api/restore-db', methods=['POST'])
def restore_db():
    """接收并恢复数据库文件（base64编码），用于从本地上传数据到服务器"""
    try:
        data = request.get_json(silent=True) or {}
        db_name = data.get('db_name', '')  # 如 '倒计时/countdown.db'
        content_b64 = data.get('content', '')
        if not db_name or not content_b64:
            return jsonify({'success': False, 'error': '缺少 db_name 或 content'}), 400
        # 安全检查：只允许恢复已知的数据库文件
        allowed_prefixes = ['倒计时/', '绩点/', '成绩/', '服务器/', '人情/', '部署/', '记账/']
        if not any(db_name.startswith(p) for p in allowed_prefixes):
            return jsonify({'success': False, 'error': f'不允许的路径: {db_name}'}), 403
        if '..' in db_name or db_name.startswith('/'):
            return jsonify({'success': False, 'error': '非法路径'}), 403
        target_path = os.path.join(BASE_DIR, db_name)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        content = base64.b64decode(content_b64)
        with open(target_path, 'wb') as f:
            f.write(content)
        return jsonify({'success': True, 'message': f'{db_name} 已恢复 ({len(content)} bytes)'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

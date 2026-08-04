












# -*- coding: utf-8 -*-
"""数据库备份 Blueprint（含每日自动备份、每周自动清理）"""
import os, re, shutil, zipfile, threading, time as time_mod
from datetime import timedelta

from .utils import now_ts, size_str
from flask import Blueprint, jsonify, request

bp = Blueprint('backup', __name__)

# 异地备份同步密钥（供本地拉取备份时验证）
BACKUP_SYNC_TOKEN = 'ce952b9ded0733ed'

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
MAX_SERVER_BACKUPS = 7
AUTO_BACKUP_INTERVAL = 86400       # 备份间隔：24小时
AUTO_CLEAN_INTERVAL = 604800       # 清理间隔：7天
MAX_LOG_LINES = 200                # 日志最多保留行数

# 数据库路径
DB_PATHS = [
    ('gifts.db', os.path.join(BASE_DIR, '人情', 'gifts.db')),
    ('gpa.db', os.path.join(BASE_DIR, '绩点', 'gpa.db')),
    ('hsgrades.db', os.path.join(BASE_DIR, '成绩', 'hsgrades.db')),
    ('countdown.db', os.path.join(BASE_DIR, '倒计时', 'countdown.db')),
    ('accounting.db', os.path.join(BASE_DIR, '记账', 'accounting.db')),
    ('pa.db', os.path.join(BASE_DIR, '服务器', 'pa.db')),
]

_LAST_BACKUP_LOCK = threading.Lock()
_last_backup_time = None  # datetime，记录上次备份的精确时间


def _save_server_backup():
    global _last_backup_time
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = now_ts().strftime('%Y%m%d_%H%M%S')
    zip_path = os.path.join(BACKUP_DIR, f'backup_{ts}.zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, path in DB_PATHS:
            if os.path.exists(path):
                zf.write(path, name)
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.zip')])
    while len(backups) > MAX_SERVER_BACKUPS:
        os.remove(os.path.join(BACKUP_DIR, backups.pop(0)))
    with _LAST_BACKUP_LOCK:
        _last_backup_time = now_ts()
    return zip_path


def _get_last_backup_time():
    """从备份文件列表中推断上次备份时间，优先用内存记录"""
    with _LAST_BACKUP_LOCK:
        if _last_backup_time is not None:
            return _last_backup_time

    os.makedirs(BACKUP_DIR, exist_ok=True)
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.zip')], reverse=True)
    for f in backups:
        m = re.search(r'backup_(\d{8})_(\d{6})', f)
        if m:
            try:
                from datetime import datetime
                s = m.group(1) + m.group(2)
                dt = datetime.strptime(s, '%Y%m%d%H%M%S')
                from .utils import TZ
                dt = dt.replace(tzinfo=TZ)
                return dt
            except Exception:
                continue
    return None


def _auto_backup_thread():
    """每天定时自动备份一次，基于上次实际备份时间 + 间隔"""
    while True:
        try:
            now = now_ts()
            last = _get_last_backup_time()

            if last is None:
                # 从未备份过，立即备份
                zip_path = _save_server_backup()
                ts = now_ts().strftime('%Y-%m-%d %H:%M:%S')
                print(f'[{ts}] 首次自动备份完成: {os.path.basename(zip_path)}')
                sleep_sec = AUTO_BACKUP_INTERVAL
            else:
                next_time = last + timedelta(seconds=AUTO_BACKUP_INTERVAL)
                if now >= next_time:
                    zip_path = _save_server_backup()
                    ts = now_ts().strftime('%Y-%m-%d %H:%M:%S')
                    print(f'[{ts}] 自动备份完成: {os.path.basename(zip_path)}')
                    sleep_sec = AUTO_BACKUP_INTERVAL
                else:
                    sleep_sec = max(3600, (next_time - now).total_seconds())

            next_check = (now + timedelta(seconds=sleep_sec)).strftime('%Y-%m-%d %H:%M')
            ts = now.strftime('%Y-%m-%d %H:%M:%S')
            print(f'[{ts}] 备份调度: 下次备份时间 {next_check}')
            time_mod.sleep(sleep_sec)

        except Exception as e:
            ts = now_ts().strftime('%Y-%m-%d %H:%M:%S')
            print(f'[{ts}] 自动备份异常: {e}，60秒后重试')
            time_mod.sleep(60)


def start_auto_backup():
    t = threading.Thread(target=_auto_backup_thread, daemon=True)
    t.start()


# ==================== 自动清理 ====================

_last_cleanup = {'time': '-', 'freed': '-', 'results': []}
_last_cleanup_lock = threading.Lock()


def _count_files(path):  # pyright: ignore[reportMissingParameterType]
    """统计目录下文件总数"""
    try:
        return sum(1 for _ in (os.path.join(r, f)
                   for r, _, fs in os.walk(path) for f in fs))
    except Exception:
        return -1


def _do_cleanup():
    """清理日志、缓存，打包 git 对象，控制 PA 文件数配额"""
    results = []
    total_freed = 0
    files_before = _count_files(BASE_DIR)

    # 1. 截断 PA 续期日志
    log_path = os.path.join(BASE_DIR, '服务器', 'renew.log')
    if os.path.exists(log_path):
        size_before = os.path.getsize(log_path)
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        if len(lines) > MAX_LOG_LINES:
            with open(log_path, 'w', encoding='utf-8') as f:
                f.writelines(lines[-MAX_LOG_LINES:])
            size_after = os.path.getsize(log_path)
            freed = size_before - size_after
            total_freed += freed
            results.append(f'PA日志: {len(lines)}行 → {MAX_LOG_LINES}行, 释放 {size_str(freed)}')

    # 2. 清理 __pycache__ 目录
    for root, dirs, files in os.walk(BASE_DIR):
        if '__pycache__' in dirs:
            cache_path = os.path.join(root, '__pycache__')
            dirs.remove('__pycache__')  # 防止os.walk尝试进入已删除目录
            try:
                size_before = sum(os.path.getsize(os.path.join(cache_path, f))
                                  for f in os.listdir(cache_path) if os.path.isfile(os.path.join(cache_path, f)))
                shutil.rmtree(cache_path)
                total_freed += size_before
                results.append(f'已清理: {os.path.relpath(cache_path, BASE_DIR)}')
            except Exception:
                pass

    # 3. 计算目录文件总数
    # 4. 截断系统日志（500错误日志、重载日志等）
    SYSTEM_LOGS = [
        '500_error.log', 'reload_stdout.log', 'reload_stderr.log'
    ]
    for log_name in SYSTEM_LOGS:
        log_path = os.path.join(BASE_DIR, log_name)
        if os.path.exists(log_path):
            try:
                size_before = os.path.getsize(log_path)
                with open(log_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                if len(lines) > MAX_LOG_LINES:
                    with open(log_path, 'w', encoding='utf-8') as f:
                        f.writelines(lines[-MAX_LOG_LINES:])
                    size_after = os.path.getsize(log_path)
                    freed = size_before - size_after
                    total_freed += freed
                    results.append(f'{log_name}: {len(lines)}行 → {MAX_LOG_LINES}行, 释放 {size_str(freed)}')
            except Exception:
                pass

    # 5. git gc 打包松散对象（减少文件数）
    try:
        import subprocess as _sp
        r = _sp.run(
            ['git', '-C', BASE_DIR, 'gc', '--prune=now', '--quiet'],
            capture_output=True, text=True, timeout=120
        )
        if r.returncode == 0:
            results.append('git gc 完成')
        else:
            err = r.stderr.strip()[:80] if r.stderr else 'unknown'
            results.append(f'git gc: {err}')
    except Exception:
        pass  # git 不存在或不支持时不报错

    # 6. 统计清理后文件数变化
    files_after = _count_files(BASE_DIR)
    if files_before > 0 and files_after > 0 and files_before != files_after:
        delta = files_before - files_after
        if delta > 0:
            results.append(f'文件数: {files_before} → {files_after} (减少{delta})')
        else:
            results.append(f'文件数: {files_before} → {files_after}')

    # 7. 动态检测并清理无关联模块的遗留目录
    from .utils import ALL_SAFE_DIRS
    try:
        for entry in os.scandir(BASE_DIR):
            if not entry.is_dir(follow_symlinks=False):
                continue
            dname = entry.name
            if dname.startswith('.') or dname in ALL_SAFE_DIRS:
                continue
            # 遗留目录：不在已知模块列表中，也不属于系统保留目录
            try:
                sz = sum(os.path.getsize(os.path.join(r, f))
                         for r, _, fs in os.walk(entry.path) for f in fs
                         if os.path.isfile(os.path.join(r, f)))
                shutil.rmtree(entry.path)
                total_freed += sz
                results.append(f'已清理遗留目录: {dname}/ ({size_str(sz)})')
            except Exception:
                pass
    except OSError:
        pass

    # 8. 清理已知废弃文件
    ORPHAN_FILES = ['服务器/data_report.log']
    for fname in ORPHAN_FILES:
        fpath = os.path.join(BASE_DIR, fname)
        if os.path.isfile(fpath):
            try:
                sz = os.path.getsize(fpath)
                os.remove(fpath)
                total_freed += sz
                results.append(f'已清理废弃文件: {fname} ({size_str(sz)})')
            except Exception:
                pass

    if not results:
        results.append('无需清理')
    return results, total_freed


def _auto_clean_thread():
    """每7天自动清理一次，基于上次实际清理时间 + 间隔"""
    global _last_cleanup
    while True:
        try:
            now = now_ts()
            # 从 _last_cleanup 上次记录推断下次清理时间
            last_time_str = _last_cleanup.get('time', '-')
            if last_time_str and last_time_str != '-':
                try:
                    from datetime import datetime
                    last_dt = datetime.strptime(last_time_str, '%Y-%m-%d %H:%M:%S')
                    from .utils import TZ
                    last_dt = last_dt.replace(tzinfo=TZ)
                    next_clean = last_dt + timedelta(seconds=AUTO_CLEAN_INTERVAL)
                    if now < next_clean:
                        sleep_sec = max(3600, (next_clean - now).total_seconds())
                        next_check = (now + timedelta(seconds=sleep_sec)).strftime('%Y-%m-%d %H:%M')
                        ts = now.strftime('%Y-%m-%d %H:%M:%S')
                        print(f'[{ts}] 清理调度: 距上次清理不足7天，下次清理 {next_check}')
                        time_mod.sleep(sleep_sec)
                        continue
                except Exception:
                    pass  # 解析失败则直接执行

            results, freed = _do_cleanup()
            ts = now_ts().strftime('%Y-%m-%d %H:%M:%S')
            with _last_cleanup_lock:
                _last_cleanup = {'time': ts, 'freed': size_str(freed), 'results': results}
            print(f'[{ts}] 自动清理完成: 释放 {size_str(freed)}; {"; ".join(results)}')
            time_mod.sleep(AUTO_CLEAN_INTERVAL)

        except Exception as e:
            ts = now_ts().strftime('%Y-%m-%d %H:%M:%S')
            print(f'[{ts}] 自动清理异常: {e}，60秒后重试')
            time_mod.sleep(60)


def start_auto_clean():
    t = threading.Thread(target=_auto_clean_thread, daemon=True)
    t.start()


_cleanup_lock = threading.Lock()

@bp.route('/api/cleanup', methods=['POST'])
def manual_cleanup():
    global _last_cleanup
    if _cleanup_lock.locked():
        return jsonify({'success': False, 'error': '清理正在进行中'}), 409
    with _cleanup_lock:
        try:
            results, freed = _do_cleanup()
            ts = now_ts().strftime('%Y-%m-%d %H:%M:%S')
            with _last_cleanup_lock:
                _last_cleanup = {'time': ts, 'freed': size_str(freed), 'results': results}
            return jsonify({'success': True, 'results': results, 'freed': size_str(freed)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/cleanup/status', methods=['GET'])
def cleanup_status():
    with _last_cleanup_lock:
        return jsonify(dict(_last_cleanup))


@bp.route('/api/backup/list')
def list_backups():
    """列出所有备份文件"""
    if not os.path.exists(BACKUP_DIR):
        return jsonify({'success': True, 'backups': []})
    files = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.zip')], reverse=True)
    result = []
    for f in files:
        fp = os.path.join(BACKUP_DIR, f)
        sz = os.path.getsize(fp)
        result.append({'name': f, 'size': size_str(sz), 'size_bytes': sz})
    return jsonify({'success': True, 'backups': result})


# ==================== 定时任务公开接口 ====================

def run_backup():
    """公开接口：执行备份（供 daily_task.py 定时脚本调用）
    返回 (success: bool, message: str)"""
    try:
        zip_path = _save_server_backup()
        size = size_str(os.path.getsize(zip_path))
        return (True, f'备份完成: {os.path.basename(zip_path)} ({size})')
    except Exception as e:
        return (False, f'备份异常: {e}')


def run_cleanup():
    """公开接口：执行清理（供 daily_task.py 定时脚本调用）
    返回 (success: bool, message: str)"""
    try:
        results, freed = _do_cleanup()
        global _last_cleanup
        ts = now_ts().strftime('%Y-%m-%d %H:%M:%S')
        with _last_cleanup_lock:
            _last_cleanup = {'time': ts, 'freed': size_str(freed), 'results': results}
        return (True, f'清理完成: 释放 {size_str(freed)}; {"; ".join(results)}')
    except Exception as e:
        return (False, f'清理异常: {e}')


# ==================== API 端点 ====================

@bp.route('/api/backup/create', methods=['POST'])
def create_backup():
    """手动创建备份"""
    try:
        zip_path = _save_server_backup()
        return jsonify({'success': True, 'file': os.path.basename(zip_path),
                        'size': size_str(os.path.getsize(zip_path))})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/backup/restore/<filename>')
def restore_backup(filename):
    """从指定备份文件恢复所有数据库"""
    if '..' in filename or '/' in filename:
        return jsonify({'success': False, 'error': '非法文件名'}), 403
    filepath = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({'success': False, 'error': '备份文件不存在'}), 404
    results = []
    try:
        db_targets = {
            'gifts.db': os.path.join(BASE_DIR, '人情', 'gifts.db'),
            'gpa.db': os.path.join(BASE_DIR, '绩点', 'gpa.db'),
            'hsgrades.db': os.path.join(BASE_DIR, '成绩', 'hsgrades.db'),
            'countdown.db': os.path.join(BASE_DIR, '倒计时', 'countdown.db'),
            'accounting.db': os.path.join(BASE_DIR, '记账', 'accounting.db'),
            'pa.db': os.path.join(BASE_DIR, '服务器', 'pa.db'),
        }
        with zipfile.ZipFile(filepath, 'r') as zf:
            for fname in zf.namelist():
                if fname in db_targets:
                    target = db_targets[fname]
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    data = zf.read(fname)
                    with open(target, 'wb') as f:
                        f.write(data)
                    results.append(f'{fname} -> {os.path.relpath(target, BASE_DIR)} ({len(data)} bytes)')
        return jsonify({'success': True, 'backup_file': filename, 'restored': results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/backup/download/<filename>')
def download_backup(filename):
    """下载指定备份文件"""
    if '..' in filename or '/' in filename:
        return jsonify({'success': False, 'error': '非法文件名'}), 403
    filepath = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({'success': False, 'error': '备份文件不存在'}), 404
    from flask import send_file
    return send_file(filepath, as_attachment=True, download_name=filename)


@bp.route('/api/backup/sync', methods=['POST'])
def sync_backup():
    """备份同步端点：返回最新备份 zip 文件
    需要 X-Backup-Token 请求头验证，供本地拉取脚本使用"""
    token = request.headers.get('X-Backup-Token', '') or request.args.get('token', '')
    if token != BACKUP_SYNC_TOKEN:
        return jsonify({'success': False, 'error': '未授权'}), 403

    os.makedirs(BACKUP_DIR, exist_ok=True)
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.zip')], reverse=True)

    if not backups:
        # 没有现有备份，现场创建一个
        try:
            zip_path = _save_server_backup()
            backups = [os.path.basename(zip_path)]
        except Exception as e:
            return jsonify({'success': False, 'error': f'备份创建失败: {e}'}), 500

    latest = backups[0]
    filepath = os.path.join(BACKUP_DIR, latest)
    from flask import send_file
    return send_file(filepath, as_attachment=True, download_name=latest)

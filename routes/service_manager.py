# -*- coding: utf-8 -*-
"""后台服务管理器 - 支持按需启停，避免 PA 启动阻塞"""
import os
import threading
import time as time_mod

from flask import Blueprint, jsonify, request

from .utils import make_db, _now

bp = Blueprint('service_manager', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PA_DB = os.path.join(BASE_DIR, '服务器', 'pa.db')
_get_svc_db = make_db(PA_DB)

# 服务注册表
_registry = {}
_registry_lock = threading.Lock()


# ==================== 工具函数 ====================

def _init_db():
    """确保 service_config 表存在"""
    os.makedirs(os.path.dirname(PA_DB), exist_ok=True)
    db = _get_svc_db()
    db.execute('''CREATE TABLE IF NOT EXISTS service_config (
        name TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        description TEXT DEFAULT '',
        enabled INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT ''
    )''')
    db.commit()
    db.close()


def sleep_check(stop_event, seconds, check_every=5):
    """带停止检测的 sleep。
    每 check_every 秒检查 stop_event，一旦设置立即返回。
    
    Args:
        stop_event: threading.Event or None
        seconds: 总睡眠秒数
        check_every: 检查间隔（秒），默认 5
    Returns:
        True 表示被停止信号中断，False 表示正常睡完
    """
    if stop_event is None:
        time_mod.sleep(seconds)
        return False
    elapsed = 0
    while elapsed < seconds:
        if stop_event.is_set():
            return True
        chunk = min(check_every, seconds - elapsed)
        time_mod.sleep(chunk)
        elapsed += chunk
        if stop_event.is_set():
            return True
    return False


# ==================== 服务注册与状态 ====================

def register_service(name, label, description, start_fn, default_enabled=False):
    """注册一个后台服务（app.py 初始化时调用）。
    
    start_fn 签名为 start_fn(stop_event=None)，
    stop_event 是一个 threading.Event，线程应通过 sleep_check(stop_event, X) 检测停止信号。
    """
    _init_db()
    db = _get_svc_db()
    existing = db.execute('SELECT enabled FROM service_config WHERE name=?', (name,)).fetchone()
    if existing is None:
        db.execute(
            "INSERT INTO service_config (name, label, description, enabled, updated_at) VALUES (?,?,?,?,datetime('now','localtime'))",
            (name, label, description, 1 if default_enabled else 0))
        db.commit()
        enabled = default_enabled
    else:
        enabled = bool(existing[0])
    db.close()

    with _registry_lock:
        _registry[name] = {
            'label': label,
            'description': description,
            'start_fn': start_fn,
            'enabled': enabled,
            'thread': None,
            'stop_event': threading.Event(),
            'status': 'stopped',
            'error': '',
            'started_at': '',
        }


def start_service(name):
    """启动指定服务（线程安全）"""
    if name not in _registry:
        return False, '未知服务'
    
    with _registry_lock:
        info = _registry[name]
        
        if info['thread'] and info['thread'].is_alive():
            return False, '服务已在运行'
        
        # 标记启用
        db = _get_svc_db()
        db.execute("UPDATE service_config SET enabled=1, updated_at=datetime('now','localtime') WHERE name=?", (name,))
        db.commit()
        db.close()
        
        info['enabled'] = True
        info['stop_event'].clear()
        info['error'] = ''
        info['started_at'] = _now().strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            t = threading.Thread(
                target=_wrapper,
                args=(name, info['start_fn'], info['stop_event']),
                daemon=True
            )
            t.start()
            info['thread'] = t
            info['status'] = 'running'
            return True, '已启动'
        except Exception as e:
            info['error'] = str(e)
            info['status'] = 'error'
            return False, f'启动失败: {e}'


def stop_service(name):
    """停止指定服务（发送停止信号，线程下次检查 sleep_check 时退出）"""
    if name not in _registry:
        return False, '未知服务'
    
    with _registry_lock:
        info = _registry[name]
        
        # 标记禁用
        db = _get_svc_db()
        db.execute("UPDATE service_config SET enabled=0, updated_at=datetime('now','localtime') WHERE name=?", (name,))
        db.commit()
        db.close()
        
        info['enabled'] = False
        
        if not info['thread'] or not info['thread'].is_alive():
            info['status'] = 'stopped'
            return True, '服务未运行，已标记为禁用'
        
        info['stop_event'].set()
        info['status'] = 'stopping'
        return True, '已发送停止信号'


def get_enabled_services():
    """返回所有需要自动启动的服务名列表"""
    _init_db()
    db = _get_svc_db()
    rows = db.execute('SELECT name FROM service_config WHERE enabled=1').fetchall()
    db.close()
    return [row[0] for row in rows]


def auto_start_enabled():
    """启动所有已启用的服务（app.py 延迟初始化时调用）"""
    for name in get_enabled_services():
        if name in _registry:
            ok, msg = start_service(name)
            ts = _now().strftime('%Y-%m-%d %H:%M:%S')
            print(f'[{ts}] 服务 [{name}] 自动启动: {msg}')


# ==================== 线程包装器 ====================

def _wrapper(name, start_fn, stop_event):
    """包装服务线程：调用 start_fn，捕获异常"""
    try:
        start_fn(stop_event)
    except Exception as e:
        with _registry_lock:
            if name in _registry:
                _registry[name]['error'] = str(e)
                _registry[name]['status'] = 'error'
        ts = _now().strftime('%Y-%m-%d %H:%M:%S')
        print(f'[{ts}] 服务 [{name}] 异常退出: {e}')
    finally:
        with _registry_lock:
            if name in _registry:
                _registry[name]['status'] = 'stopped'
                _registry[name]['thread'] = None


# ==================== API ====================

@bp.route('/api/services', methods=['GET'])
def list_services():
    """列出所有服务及其状态"""
    result = []
    with _registry_lock:
        for name, info in _registry.items():
            # 从注册表获取运行中状态
            is_alive = info['thread'] and info['thread'].is_alive()
            if is_alive:
                status = 'running'
            elif info['stop_event'].is_set():
                status = 'stopping'
            elif info.get('error'):
                status = 'error'
            else:
                status = 'stopped'
            info['status'] = status
            result.append({
                'name': name,
                'label': info['label'],
                'description': info['description'],
                'enabled': info['enabled'],
                'status': status,
                'error': info.get('error', ''),
                'started_at': info.get('started_at', ''),
            })
    return jsonify({'services': result})


@bp.route('/api/services/<name>/start', methods=['POST'])
def api_start_service(name):
    ok, msg = start_service(name)
    return jsonify({'success': ok, 'message': msg})


@bp.route('/api/services/<name>/stop', methods=['POST'])
def api_stop_service(name):
    ok, msg = stop_service(name)
    return jsonify({'success': ok, 'message': msg})

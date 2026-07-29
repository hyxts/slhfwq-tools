# -*- coding: utf-8 -*-
"""slhfwq 异地备份接收端 Flask 应用

部署方式（在 slhfwq PA 控制台操作）：
1. 将此文件放到 /home/slhfwq/receiver_app.py
2. 修改 WSGI 配置文件 /var/www/slhfwq_pythonanywhere_com_wsgi.py 内容为：
   import sys
   sys.path.insert(0, '/home/slhfwq')
   from receiver_app import app as application
3. 在 Web 标签页点击 Reload
"""
import os
import re
import shutil
import hashlib
import hmac
from datetime import datetime

from flask import Flask, request, jsonify

app = Flask(__name__)

# 与 gjx 端共享的密钥
SYNC_TOKEN = 'ce952b9ded0733ed'

# 备份保存目录
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backups')
MAX_BACKUPS = 7


def _verify_token():
    """验证请求令牌"""
    token = request.headers.get('X-Backup-Token', '') or request.args.get('token', '')
    if not token:
        return False
    return hmac.compare_digest(token, SYNC_TOKEN)


@app.route('/api/backup/receive', methods=['POST'])
def receive_backup():
    """接收 gjx 推送的备份 zip 文件"""
    if not _verify_token():
        return jsonify({'success': False, 'error': '未授权'}), 403

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'success': False, 'error': '缺少文件'}), 400

    filename = file.filename
    if not filename.endswith('.zip'):
        return jsonify({'success': False, 'error': '仅支持 zip 文件'}), 400

    # 防止路径穿越
    safe_name = os.path.basename(filename)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    filepath = os.path.join(BACKUP_DIR, safe_name)

    file.save(filepath)
    size = os.path.getsize(filepath)

    # 清理旧备份
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.zip')], reverse=True)
    for old in backups[MAX_BACKUPS:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except Exception:
            pass

    return jsonify({
        'success': True,
        'file': safe_name,
        'size_bytes': size,
        'kept_backups': min(len(backups), MAX_BACKUPS),
    })


@app.route('/api/backup/status')
def backup_status():
    """查看备份状态"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.zip')], reverse=True)
    result = []
    for f in backups[:MAX_BACKUPS]:
        fp = os.path.join(BACKUP_DIR, f)
        sz = os.path.getsize(fp)
        result.append({'name': f, 'size_bytes': sz})
    return jsonify({'success': True, 'backups': result, 'count': len(result)})


@app.route('/')
def index():
    return jsonify({'service': 'slhfwq backup receiver', 'status': 'running'})


if __name__ == '__main__':
    app.run(port=5000)

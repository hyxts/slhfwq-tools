# -*- coding: utf-8 -*-
"""从 gjx 拉取最新备份到本地，保留最新2份"""
import os
import urllib.request
from datetime import datetime

REMOTE_URL = 'https://gjx.pythonanywhere.com/api/backup/sync'
TOKEN = 'ce952b9ded0733ed'
LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '本地备份')
MAX_KEEP = 2

def pull():
    os.makedirs(LOCAL_DIR, exist_ok=True)
    req = urllib.request.Request(
        REMOTE_URL, data=b'{}',
        headers={'X-Backup-Token': TOKEN, 'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        # 从 Content-Disposition 提取文件名
        cd = resp.headers.get('Content-Disposition', '')
        fname = None
        for part in cd.split(';'):
            part = part.strip()
            if part.startswith('filename='):
                fname = part.split('=', 1)[1].strip(' "')
        if not fname:
            fname = f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'

        data = resp.read()
        filepath = os.path.join(LOCAL_DIR, fname)
        with open(filepath, 'wb') as f:
            f.write(data)
        print(f'已保存: {fname} ({len(data)/1024:.1f}KB)')

    # 只保留最新 MAX_KEEP 份
    backups = sorted([f for f in os.listdir(LOCAL_DIR) if f.endswith('.zip')], reverse=True)
    for old in backups[MAX_KEEP:]:
        os.remove(os.path.join(LOCAL_DIR, old))
        print(f'已清理: {old}')

    print(f'本地共 {len(backups[:MAX_KEEP])} 份备份')

if __name__ == '__main__':
    pull()

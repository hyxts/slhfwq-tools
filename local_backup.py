# -*- coding: utf-8 -*-
"""从 gjx 拉取最新 2 份备份到本地，多的自动清理"""
import os
import json
import urllib.request
from datetime import datetime

BASE_URL = 'https://gjx.pythonanywhere.com'
TOKEN = 'ce952b9ded0733ed'
LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '本地备份')
MAX_KEEP = 2

def pull():
    os.makedirs(LOCAL_DIR, exist_ok=True)

    # 1. 列出远程备份
    list_req = urllib.request.Request(
        f'{BASE_URL}/api/backup/list',
        headers={'X-Backup-Token': TOKEN}
    )
    with urllib.request.urlopen(list_req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
        remote_backups = data.get('backups', [])

    if not remote_backups:
        print('远程没有备份文件')
        return

    # 2. 下载最新的 MAX_KEEP 份
    downloaded = 0
    for item in remote_backups[:MAX_KEEP]:
        fname = item['name']
        local_path = os.path.join(LOCAL_DIR, fname)
        if os.path.exists(local_path):
            print(f'跳过(已存在): {fname}')
            downloaded += 1
            continue
        print(f'下载中: {fname} ({item["size"]})...', end=' ')
        dl_req = urllib.request.Request(
            f'{BASE_URL}/api/backup/download/{fname}',
            headers={'X-Backup-Token': TOKEN}
        )
        with urllib.request.urlopen(dl_req, timeout=120) as dl_resp:
            data = dl_resp.read()
            with open(local_path, 'wb') as f:
                f.write(data)
        print(f'完成 ({len(data)/1024:.1f}KB)')
        downloaded += 1

    # 3. 清理多余文件
    local_files = sorted([f for f in os.listdir(LOCAL_DIR) if f.endswith('.zip')], reverse=True)
    for old in local_files[MAX_KEEP:]:
        os.remove(os.path.join(LOCAL_DIR, old))
        print(f'已清理: {old}')

    print(f'本地共 {min(len(local_files), MAX_KEEP)} 份备份')

if __name__ == '__main__':
    pull()

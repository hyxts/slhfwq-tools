# -*- coding: utf-8 -*-
"""slhfwq 异地备份拉取脚本
从 gjx.pythonanywhere.com 拉取最新数据库备份到本地
用法: python pull_backup.py
可配合 PA Scheduled Task 每日定时执行
"""
import os
import re
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime

# ---- 配置 ----
GJX_BACKUP_URL = 'https://gjx.pythonanywhere.com/api/backup/sync'
BACKUP_TOKEN = 'ce952b9ded0733ed'
LOCAL_BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backups')
MAX_BACKUPS = 7  # 最多保留几个备份


def pull_latest():
    os.makedirs(LOCAL_BACKUP_DIR, exist_ok=True)
    req = urllib.request.Request(
        GJX_BACKUP_URL,
        data=b'{}',
        headers={
            'X-Backup-Token': BACKUP_TOKEN,
            'Content-Type': 'application/json',
        },
        method='POST'
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status != 200:
                print(f'[错误] HTTP {resp.status}')
                return False

            cd = resp.headers.get('Content-Disposition', '')
            filename = ''
            if cd:
                m = re.search(r'filename="?(.+?)"?$', cd)
                if m:
                    filename = m.group(1)
            if not filename:
                filename = f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'

            filepath = os.path.join(LOCAL_BACKUP_DIR, filename)
            data = resp.read()
            with open(filepath, 'wb') as f:
                f.write(data)
            sz_str = f'{len(data)/1024:.1f}KB' if len(data) >= 1024 else f'{len(data)}B'
            print(f'[完成] {filename} ({sz_str})')

            # 清理旧备份
            backups = sorted(
                [f for f in os.listdir(LOCAL_BACKUP_DIR) if f.endswith('.zip')],
                reverse=True
            )
            for old in backups[MAX_BACKUPS:]:
                os.remove(os.path.join(LOCAL_BACKUP_DIR, old))
                print(f'[清理] 删除旧备份: {old}')

            return True

    except urllib.error.HTTPError as e:
        print(f'[错误] HTTP {e.code}: {e.reason}')
        try:
            body = e.read().decode('utf-8')[:300]
            print(f'[错误] 详情: {body}')
        except Exception:
            pass
        return False
    except Exception as e:
        print(f'[错误] {e}')
        return False


if __name__ == '__main__':
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] ===== 异地备份拉取开始 =====')
    ok = pull_latest()
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] ===== 异地备份拉取{"成功" if ok else "失败"} =====')

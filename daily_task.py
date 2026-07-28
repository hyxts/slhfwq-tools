# -*- coding: utf-8 -*-
"""PythonAnywhere 每日定时任务（可选）

说明：
- PA 付费账户：可在 PA Tasks 页面配置定时触发，替代 Flask 内 daemon 线程
- PA 免费账户：不支持 Scheduled/Always-on tasks，此脚本仅供在 Console 中手动执行
- 默认情况下仍使用 app.py 内的 daemon 线程，无需配置此脚本

用法: python daily_task.py
"""
import sys, os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from routes.pa import run_renewal
from routes.backup import run_backup, run_cleanup
from routes.utils import _now


def main():
    ts = _now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] ===== 每日任务开始 =====')

    # ---- 1. PA 续期检查 ----
    try:
        ok, msg = run_renewal()
        status = '成功' if ok else '失败'
        print(f'[续期] {status}: {msg}')
    except Exception as e:
        print(f'[续期] 异常: {e}')

    # ---- 2. 数据库备份 ----
    try:
        ok, msg = run_backup()
        status = '成功' if ok else '失败'
        print(f'[备份] {status}: {msg}')
    except Exception as e:
        print(f'[备份] 异常: {e}')

    # ---- 3. 清理（仅周日执行） ----
    if _now().weekday() == 6:
        try:
            ok, msg = run_cleanup()
            status = '成功' if ok else '失败'
            print(f'[清理] {status}: {msg}')
        except Exception as e:
            print(f'[清理] 异常: {e}')
    else:
        print(f'[清理] 跳过（非周日）')

    ts = _now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] ===== 每日任务完成 =====')


if __name__ == '__main__':
    main()

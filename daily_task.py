# -*- coding: utf-8 -*-
"""PythonAnywhere 每日定时任务（可选）

说明：
- PA 付费账户：可在 PA Tasks 页面配置定时触发，替代 Flask 内 daemon 线程
- PA 免费账户：不支持 Scheduled/Always-on tasks，此脚本仅供在 Console 中手动执行
- 默认情况下仍使用 app.py 内的 daemon 线程，无需配置此脚本

用法: python daily_task.py
"""
import sys, os, shutil, subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from routes.pa import run_renewal
from routes.backup import run_backup, run_cleanup
from routes.utils import now_ts


def _count_files(path):
    """统计目录下文件总数"""
    try:
        return sum(1 for _ in (os.path.join(r, f)
                   for r, _, fs in os.walk(path) for f in fs))
    except Exception:
        return -1


def size_str(b):
    """字节数转可读字符串"""
    if b < 1024:
        return f'{b}B'
    if b < 1048576:
        return f'{b/1024:.1f}KB'
    return f'{b/1048576:.1f}MB'


def run_file_cleanup():
    """每日轻量清理：删除 __pycache__ 缓存文件，控制 PA 文件数配额
    返回 (success, message, stats_dict)"""
    results = []
    removed = 0

    # 1. 清理 __pycache__ 目录
    for root, dirs, files in os.walk(BASE_DIR):
        if '__pycache__' in dirs:
            cache_path = os.path.join(root, '__pycache__')
            try:
                cnt = len(os.listdir(cache_path))
                shutil.rmtree(cache_path, ignore_errors=True)
                removed += cnt
                results.append(os.path.relpath(cache_path, BASE_DIR))
            except Exception:
                pass

    # 2. 清理散落的 .pyc 文件
    pyc_count = 0
    for root, dirs, files in os.walk(BASE_DIR):
        for f in files:
            if f.endswith('.pyc'):
                try:
                    os.remove(os.path.join(root, f))
                    pyc_count += 1
                except Exception:
                    pass
    if pyc_count:
        removed += pyc_count
        results.append(f'散落pyc: {pyc_count}个')

    # 3. 每周日执行 git gc（打包松散对象，减少文件数）
    today = now_ts().weekday()
    if today == 6:
        try:
            r = subprocess.run(
                ['git', '-C', BASE_DIR, 'gc', '--auto'],
                capture_output=True, text=True, timeout=60
            )
            if r.returncode == 0:
                results.append('git gc 完成')
            else:
                results.append(f'git gc: {r.stderr.strip()[:80]}')
        except Exception as e:
            results.append(f'git gc 跳过: {e}')

    # 4. 统计当前状态
    total_files = _count_files(BASE_DIR)
    try:
        import glob as _glob
        total_size = sum(os.path.getsize(f)
                         for r, _, fs in os.walk(BASE_DIR) for f in fs
                         if os.path.isfile(os.path.join(r, f)))
    except Exception:
        total_size = -1

    stats = {
        'removed': removed,
        'total_files': total_files,
        'total_size': size_str(total_size) if total_size > 0 else 'N/A',
        'dirs_cleaned': len([r for r in results if r.startswith('routes') or r.endswith('pyc')]),
    }

    msg_parts = []
    if removed > 0:
        msg_parts.append(f'清理 {removed} 个文件')
    else:
        msg_parts.append('无缓存需清理')
    msg_parts.append(f'当前 {total_files} 个文件/{stats["total_size"]}')
    if results:
        msg_parts.append('; '.join(results[:3]))

    return True, ' | '.join(msg_parts)


def main():
    ts = now_ts().strftime('%Y-%m-%d %H:%M:%S')
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

    # ---- 3. 每日文件缓存清理 ----
    try:
        ok, msg = run_file_cleanup()
        status = '成功' if ok else '失败'
        print(f'[文件清理] {status}: {msg}')
    except Exception as e:
        print(f'[文件清理] 异常: {e}')

    # ---- 4. 深度清理（仅周日执行） ----
    if now_ts().weekday() == 6:
        try:
            ok, msg = run_cleanup()
            status = '成功' if ok else '失败'
            print(f'[深度清理] {status}: {msg}')
        except Exception as e:
            print(f'[深度清理] 异常: {e}')
    else:
        print(f'[深度清理] 跳过（非周日）')

    ts = now_ts().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] ===== 每日任务完成 =====')


if __name__ == '__main__':
    main()

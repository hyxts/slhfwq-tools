# -*- coding: utf-8 -*-
"""
随身口袋 -- 待办清单 + 记事本 统一模块
支持待办与记事联动、微信分享
"""
import os, time as time_mod, sqlite3
from flask import Blueprint, request, jsonify
from routes.utils import make_db, _now, TZ

bp = Blueprint('pocket', __name__, url_prefix='/api/pocket')

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '待办')
DB_PATH = os.path.join(DB_DIR, 'pocket.db')


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = _get_db()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            pinned INTEGER NOT NULL DEFAULT 0
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 1,
            deadline TEXT DEFAULT NULL,
            note_id INTEGER DEFAULT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE SET NULL
        )''')
        conn.commit()
    finally:
        conn.close()


# ==================== 任务 API ====================

@bp.route('/tasks', methods=['GET'])
def list_tasks():
    """获取任务列表，支持筛选和排序"""
    filter_type = request.args.get('filter', 'all')  # all, active, done
    sort_by = request.args.get('sort', 'created')     # created, priority, deadline
    note_id = request.args.get('note_id', None)

    conn = _get_db()
    try:
        where = []
        params = []

        if note_id is not None:
            where.append('note_id = ?')
            params.append(int(note_id))

        if filter_type == 'active':
            where.append('done = 0')
        elif filter_type == 'done':
            where.append('done = 1')

        where_clause = ' AND '.join(where) if where else '1=1'

        order_map = {
            'created': 'done ASC, created_at DESC',
            'priority': 'done ASC, priority DESC, created_at DESC',
            'deadline': 'done ASC, CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline ASC, created_at DESC',
        }
        order = order_map.get(sort_by, order_map['created'])

        tasks = conn.execute(
            f'SELECT id, title, priority, deadline, note_id, done, created_at, updated_at '
            f'FROM tasks WHERE {where_clause} ORDER BY {order}',
            params
        ).fetchall()

        result = []
        for t in tasks:
            result.append({
                'id': t['id'],
                'title': t['title'],
                'priority': t['priority'],
                'deadline': t['deadline'],
                'note_id': t['note_id'],
                'done': bool(t['done']),
                'created_at': t['created_at'],
                'updated_at': t['updated_at'],
            })

        # 统计
        total = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        done_count = conn.execute('SELECT COUNT(*) FROM tasks WHERE done=1').fetchone()[0]
        high_priority = conn.execute('SELECT COUNT(*) FROM tasks WHERE priority=3 AND done=0').fetchone()[0]

        return jsonify({
            'success': True,
            'data': result,
            'stats': {'total': total, 'done': done_count, 'active': total - done_count, 'high_priority': high_priority},
        })
    finally:
        conn.close()


@bp.route('/tasks', methods=['POST'])
def create_task():
    """创建任务，可关联到某条笔记"""
    data = request.get_json(silent=True) or {}
    title = (data.get('title', '') or '').strip()
    if not title:
        return jsonify({'success': False, 'error': '标题不能为空'}), 400

    priority = int(data.get('priority', 1))
    if priority not in (1, 2, 3):
        priority = 1

    deadline = data.get('deadline') or None
    note_id = data.get('note_id')
    if note_id is not None:
        note_id = int(note_id)
    now = _now().strftime('%Y-%m-%d %H:%M:%S')

    conn = _get_db()
    try:
        cur = conn.execute(
            'INSERT INTO tasks (title, priority, deadline, note_id, created_at, updated_at) VALUES (?,?,?,?,?,?)',
            (title, priority, deadline, note_id, now, now)
        )
        conn.commit()
        tid = cur.lastrowid
        return jsonify({
            'success': True,
            'data': {
                'id': tid, 'title': title, 'priority': priority,
                'deadline': deadline, 'note_id': note_id, 'done': False,
                'created_at': now, 'updated_at': now,
            },
        })
    finally:
        conn.close()


@bp.route('/tasks/<int:tid>', methods=['PUT'])
def update_task(tid):
    """更新任务"""
    data = request.get_json(silent=True) or {}
    updates = []
    params = []

    for field in ['title', 'priority', 'deadline', 'note_id']:
        if field in data:
            val = data[field]
            if field == 'title' and not (val or '').strip():
                continue
            if field == 'priority' and int(val) not in (1, 2, 3):
                continue
            if field == 'title':
                val = val.strip()
            updates.append(f'{field} = ?')
            params.append(val)

    if updates:
        updates.append('updated_at = ?')
        params.append(_now().strftime('%Y-%m-%d %H:%M:%S'))
        params.append(tid)

        conn = _get_db()
        try:
            conn.execute(f'UPDATE tasks SET {", ".join(updates)} WHERE id = ?', params)
            conn.commit()
            row = conn.execute('SELECT * FROM tasks WHERE id = ?', (tid,)).fetchone()
            if row:
                return jsonify({
                    'success': True,
                    'data': {
                        'id': row['id'], 'title': row['title'], 'priority': row['priority'],
                        'deadline': row['deadline'], 'note_id': row['note_id'],
                        'done': bool(row['done']), 'created_at': row['created_at'], 'updated_at': row['updated_at'],
                    },
                })
        finally:
            conn.close()

    return jsonify({'success': False, 'error': '无更新内容'}), 400


@bp.route('/tasks/<int:tid>/toggle', methods=['POST'])
def toggle_task(tid):
    """切换任务完成状态"""
    conn = _get_db()
    try:
        row = conn.execute('SELECT done FROM tasks WHERE id = ?', (tid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '任务不存在'}), 404
        new_done = 0 if row['done'] else 1
        now = _now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('UPDATE tasks SET done = ?, updated_at = ? WHERE id = ?', (new_done, now, tid))
        conn.commit()
        return jsonify({'success': True, 'data': {'id': tid, 'done': bool(new_done)}})
    finally:
        conn.close()


@bp.route('/tasks/<int:tid>', methods=['DELETE'])
def delete_task(tid):
    """删除任务"""
    conn = _get_db()
    try:
        conn.execute('DELETE FROM tasks WHERE id = ?', (tid,))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


# ==================== 笔记 API ====================

@bp.route('/notes', methods=['GET'])
def list_notes():
    """获取笔记列表（不含正文，含预览）"""
    search = request.args.get('search', '').strip()
    conn = _get_db()
    try:
        if search:
            rows = conn.execute(
                'SELECT id, title, content, created_at, updated_at, pinned '
                'FROM notes WHERE title LIKE ? ORDER BY pinned DESC, updated_at DESC',
                (f'%{search}%',)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT id, title, content, created_at, updated_at, pinned '
                'FROM notes ORDER BY pinned DESC, updated_at DESC'
            ).fetchall()

        result = []
        for r in rows:
            content = r['content'] or ''
            preview = content[:80].replace('\n', ' ') + ('...' if len(content) > 80 else '')
            result.append({
                'id': r['id'],
                'title': r['title'] or '无标题',
                'preview': preview,
                'created_at': r['created_at'],
                'updated_at': r['updated_at'],
                'pinned': bool(r['pinned']),
            })

        return jsonify({'success': True, 'data': result})
    finally:
        conn.close()


@bp.route('/notes', methods=['POST'])
def create_note():
    """创建笔记"""
    data = request.get_json(silent=True) or {}
    title = (data.get('title', '') or '').strip() or '无标题'
    content = data.get('content', '') or ''
    now = _now().strftime('%Y-%m-%d %H:%M:%S')

    conn = _get_db()
    try:
        cur = conn.execute(
            'INSERT INTO notes (title, content, created_at, updated_at) VALUES (?,?,?,?)',
            (title, content, now, now)
        )
        conn.commit()
        nid = cur.lastrowid
        return jsonify({
            'success': True,
            'data': {
                'id': nid, 'title': title, 'content': content,
                'pinned': False, 'created_at': now, 'updated_at': now,
            },
        })
    finally:
        conn.close()


@bp.route('/notes/<int:nid>', methods=['GET'])
def get_note(nid):
    """获取单条笔记详情（含正文 + 关联任务）"""
    conn = _get_db()
    try:
        row = conn.execute('SELECT * FROM notes WHERE id = ?', (nid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '笔记不存在'}), 404

        tasks = conn.execute(
            'SELECT id, title, priority, deadline, done, created_at, updated_at '
            'FROM tasks WHERE note_id = ? ORDER BY done ASC, created_at DESC',
            (nid,)
        ).fetchall()

        task_list = []
        for t in tasks:
            task_list.append({
                'id': t['id'], 'title': t['title'], 'priority': t['priority'],
                'deadline': t['deadline'], 'done': bool(t['done']),
                'created_at': t['created_at'], 'updated_at': t['updated_at'],
            })

        return jsonify({
            'success': True,
            'data': {
                'id': row['id'],
                'title': row['title'],
                'content': row['content'],
                'pinned': bool(row['pinned']),
                'created_at': row['created_at'],
                'updated_at': row['updated_at'],
                'tasks': task_list,
            },
        })
    finally:
        conn.close()


@bp.route('/notes/<int:nid>', methods=['PUT'])
def update_note(nid):
    """更新笔记"""
    data = request.get_json(silent=True) or {}
    updates = []
    params = []

    for field in ['title', 'content']:
        if field in data:
            val = data[field]
            if field == 'title':
                val = (val or '').strip() or '无标题'
            updates.append(f'{field} = ?')
            params.append(val)

    if updates:
        updates.append('updated_at = ?')
        params.append(_now().strftime('%Y-%m-%d %H:%M:%S'))
        params.append(nid)

        conn = _get_db()
        try:
            conn.execute(f'UPDATE notes SET {", ".join(updates)} WHERE id = ?', params)
            conn.commit()
            row = conn.execute('SELECT * FROM notes WHERE id = ?', (nid,)).fetchone()
            if row:
                return jsonify({
                    'success': True,
                    'data': {
                        'id': row['id'], 'title': row['title'], 'content': row['content'],
                        'pinned': bool(row['pinned']), 'created_at': row['created_at'], 'updated_at': row['updated_at'],
                    },
                })
        finally:
            conn.close()

    return jsonify({'success': False, 'error': '无更新内容'}), 400


@bp.route('/notes/<int:nid>/toggle-pin', methods=['POST'])
def toggle_pin(nid):
    """切换笔记置顶"""
    conn = _get_db()
    try:
        row = conn.execute('SELECT pinned FROM notes WHERE id = ?', (nid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '笔记不存在'}), 404
        new_pin = 0 if row['pinned'] else 1
        conn.execute('UPDATE notes SET pinned = ? WHERE id = ?', (new_pin, nid))
        conn.commit()
        return jsonify({'success': True, 'data': {'id': nid, 'pinned': bool(new_pin)}})
    finally:
        conn.close()


@bp.route('/notes/<int:nid>', methods=['DELETE'])
def delete_note(nid):
    """删除笔记（同时解除关联任务的绑定）"""
    conn = _get_db()
    try:
        conn.execute('UPDATE tasks SET note_id = NULL WHERE note_id = ?', (nid,))
        conn.execute('DELETE FROM notes WHERE id = ?', (nid,))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


# ==================== 联动：笔记内容中提取任务 ====================

@bp.route('/notes/<int:nid>/extract-tasks', methods=['POST'])
def extract_tasks(nid):
    """从笔记内容中提取以 - [ ] 或 - [x] 开头的行作为任务"""
    conn = _get_db()
    try:
        row = conn.execute('SELECT content FROM notes WHERE id = ?', (nid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '笔记不存在'}), 404

        content = row['content'] or ''
        now = _now().strftime('%Y-%m-%d %H:%M:%S')
        created = []

        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('- [ ] '):
                title = line[6:].strip()
                if title:
                    cur = conn.execute(
                        'INSERT INTO tasks (title, priority, note_id, done, created_at, updated_at) VALUES (?,?,?,?,?,?)',
                        (title, 1, nid, 0, now, now)
                    )
                    created.append({'id': cur.lastrowid, 'title': title, 'done': False})
            elif line.startswith('- [x] '):
                title = line[7:].strip()
                if title:
                    cur = conn.execute(
                        'INSERT INTO tasks (title, priority, note_id, done, created_at, updated_at) VALUES (?,?,?,?,?,?)',
                        (title, 1, nid, 1, now, now)
                    )
                    created.append({'id': cur.lastrowid, 'title': title, 'done': True})

        conn.commit()
        return jsonify({'success': True, 'data': {'created': created, 'count': len(created)}})
    finally:
        conn.close()


# ==================== 微信分享 ====================

@bp.route('/share', methods=['GET'])
def share_content():
    """生成可分享的纯文本页面（微信友好）"""
    nid = request.args.get('note_id')
    if not nid:
        return '<html><body><p>无效的分享链接</p></body></html>', 404

    conn = _get_db()
    try:
        row = conn.execute('SELECT * FROM notes WHERE id = ?', (int(nid),)).fetchone()
        if not row:
            return '<html><body><p>内容不存在</p></body></html>', 404

        content = row['content'] or ''
        title = row['title'] or '无标题'

        tasks = conn.execute(
            'SELECT title, priority, deadline, done FROM tasks WHERE note_id = ? ORDER BY done ASC, priority DESC',
            (int(nid),)
        ).fetchall()

        priority_map = {1: '低', 2: '中', 3: '高'}

        tasks_html = ''
        if tasks:
            tasks_html = '<div style="margin-top:20px;border-top:1px solid #eee;padding-top:12px"><h3>关联任务</h3><ul>'
            for t in tasks:
                status = '[完成]' if t['done'] else '[待办]'
                tasks_html += f'<li>{status} {t["title"]} · {priority_map.get(t["priority"], "低")}优先级'
                if t['deadline']:
                    tasks_html += f' · 截止: {t["deadline"]}'
                tasks_html += '</li>'
            tasks_html += '</ul></div>'

        html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{content[:200]}">
<meta name="description" content="{content[:200]}">
<title>{title}</title>
<style>
  body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:640px;margin:0 auto;padding:24px 20px;color:#333;line-height:1.8}}
  h1{{font-size:22px;color:#1e293b;margin-bottom:8px}}
  .meta{{color:#94a3b8;font-size:12px;margin-bottom:20px}}
  .content{{white-space:pre-wrap;word-break:break-word;font-size:15px;background:#f8fafc;padding:16px;border-radius:8px;border:1px solid #e2e8f0}}
  ul{{padding-left:20px}}li{{margin:6px 0}}
  .footer{{text-align:center;color:#cbd5e1;font-size:11px;margin-top:32px;padding-top:16px;border-top:1px solid #f1f5f9}}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="meta">更新于 {row['updated_at']}</div>
  <div class="content">{content}</div>
  {tasks_html}
  <div class="footer">来自 个人工具箱 · 随身口袋</div>
</body>
</html>'''
        return html
    finally:
        conn.close()

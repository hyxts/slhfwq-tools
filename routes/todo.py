# -*- coding: utf-8 -*-
"""待办清单 Blueprint"""
import os
from flask import Blueprint, request, jsonify

from .utils import make_logger, make_db, TZ, _now

bp = Blueprint('todo', __name__, url_prefix='/api/todo')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(BASE_DIR, '待办')
DB_FILE = os.path.join(DB_DIR, 'todo.db')
_get_db = make_db(DB_FILE)

_log = make_logger(os.path.join(DB_DIR, 'todo.log'))

VALID_PRIORITIES = ('high', 'medium', 'low')
VALID_STATUSES = ('pending', 'done')


def init_db():
    """初始化待办数据库"""
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        conn = _get_db()
        conn.execute('''CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'medium' CHECK(priority IN ('high','medium','low')),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done')),
            due_date TEXT DEFAULT '',
            note TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            completed_at TEXT DEFAULT NULL
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_todos_priority ON todos(priority)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_todos_due ON todos(due_date)')
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback
        print(f'[init_todo_db ERROR] {e}\n{traceback.format_exc()}', flush=True)


# ==================== 待办 CRUD ====================

@bp.route('/list', methods=['GET'])
def list_todos():
    """获取待办列表，支持筛选和排序"""
    try:
        status = request.args.get('status', '')       # pending / done / 空=全部
        priority = request.args.get('priority', '')    # high / medium / low
        sort = request.args.get('sort', 'created')     # created / priority / due
        keyword = request.args.get('search', '').strip()

        where = ['1=1']
        params = []

        if status and status in VALID_STATUSES:
            where.append('status = ?')
            params.append(status)
        if priority and priority in VALID_PRIORITIES:
            where.append('priority = ?')
            params.append(priority)
        if keyword:
            where.append('title LIKE ?')
            params.append(f'%{keyword}%')

        where_clause = ' AND '.join(where)

        order_map = {
            'created': 'id DESC',
            'priority': "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 END, id DESC",
            'due': "CASE WHEN due_date='' THEN 1 ELSE 0 END, due_date ASC, id DESC",
        }
        order_clause = order_map.get(sort, 'id DESC')

        conn = _get_db()
        rows = conn.execute(
            f'SELECT * FROM todos WHERE {where_clause} ORDER BY {order_clause}',
            params
        ).fetchall()
        conn.close()

        return jsonify({'success': True, 'data': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/create', methods=['POST'])
def create_todo():
    """创建待办"""
    try:
        data = request.get_json(silent=True) or {}
        title = (data.get('title', '') or '').strip()
        if not title:
            return jsonify({'success': False, 'error': '请输入待办标题'}), 400

        priority = data.get('priority', 'medium')
        if priority not in VALID_PRIORITIES:
            priority = 'medium'

        due_date = (data.get('due_date', '') or '').strip()
        note = (data.get('note', '') or '').strip()

        conn = _get_db()
        cur = conn.execute(
            'INSERT INTO todos (title, priority, due_date, note) VALUES (?, ?, ?, ?)',
            (title, priority, due_date, note)
        )
        new_id = cur.lastrowid
        conn.commit()
        conn.close()

        row = None
        if new_id:
            conn2 = _get_db()
            row = conn2.execute('SELECT * FROM todos WHERE id = ?', (new_id,)).fetchone()
            conn2.close()

        _log(f'新建待办: {title} (ID:{new_id})')
        return jsonify({'success': True, 'data': dict(row) if row else None})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/<int:tid>', methods=['PUT'])
def update_todo(tid):
    """更新待办"""
    try:
        data = request.get_json(silent=True) or {}

        conn = _get_db()
        existing = conn.execute('SELECT id FROM todos WHERE id = ?', (tid,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({'success': False, 'error': '待办不存在'}), 404

        sets = []
        params = []

        if 'title' in data:
            title = (data.get('title', '') or '').strip()
            if not title:
                conn.close()
                return jsonify({'success': False, 'error': '标题不能为空'}), 400
            sets.append('title = ?')
            params.append(title)

        if 'priority' in data:
            p = data['priority']
            if p in VALID_PRIORITIES:
                sets.append('priority = ?')
                params.append(p)

        if 'due_date' in data:
            sets.append('due_date = ?')
            params.append((data['due_date'] or '').strip())

        if 'note' in data:
            sets.append('note = ?')
            params.append((data['note'] or '').strip())

        if not sets:
            conn.close()
            return jsonify({'success': False, 'error': '没有需要更新的字段'}), 400

        params.append(tid)
        conn.execute(f"UPDATE todos SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()

        row = conn.execute('SELECT * FROM todos WHERE id = ?', (tid,)).fetchone()
        conn.close()

        _log(f'更新待办: ID:{tid}')
        return jsonify({'success': True, 'data': dict(row)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/<int:tid>', methods=['DELETE'])
def delete_todo(tid):
    """删除待办"""
    try:
        conn = _get_db()
        existing = conn.execute('SELECT id, title FROM todos WHERE id = ?', (tid,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({'success': False, 'error': '待办不存在'}), 404

        conn.execute('DELETE FROM todos WHERE id = ?', (tid,))
        conn.commit()
        conn.close()

        _log(f'删除待办: {existing["title"]} (ID:{tid})')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/<int:tid>/toggle', methods=['PUT'])
def toggle_todo(tid):
    """切换待办完成状态"""
    try:
        conn = _get_db()
        existing = conn.execute('SELECT id, title, status FROM todos WHERE id = ?', (tid,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({'success': False, 'error': '待办不存在'}), 404

        if existing['status'] == 'pending':
            conn.execute(
                "UPDATE todos SET status = 'done', completed_at = datetime('now','localtime') WHERE id = ?",
                (tid,)
            )
        else:
            conn.execute(
                "UPDATE todos SET status = 'pending', completed_at = NULL WHERE id = ?",
                (tid,)
            )

        conn.commit()
        row = conn.execute('SELECT * FROM todos WHERE id = ?', (tid,)).fetchone()
        conn.close()

        new_status = '完成' if existing['status'] == 'pending' else '待办'
        _log(f'切换待办状态: {existing["title"]} -> {new_status}')
        return jsonify({'success': True, 'data': dict(row)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

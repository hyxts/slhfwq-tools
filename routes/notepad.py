# -*- coding: utf-8 -*-
"""记事本 Blueprint"""
import os
from flask import Blueprint, request, jsonify

from .utils import make_logger, make_db

bp = Blueprint('notepad', __name__, url_prefix='/api/notepad')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(BASE_DIR, '记事')
DB_FILE = os.path.join(DB_DIR, 'notes.db')
_get_db = make_db(DB_FILE)

_log = make_logger(os.path.join(DB_DIR, 'notes.log'))


def init_db():
    """初始化记事本数据库"""
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        conn = _get_db()
        conn.execute('''CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_notes_pinned ON notes(pinned)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at)')
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback
        print(f'[init_notepad_db ERROR] {e}\n{traceback.format_exc()}', flush=True)


# ==================== 笔记 CRUD ====================

@bp.route('/list', methods=['GET'])
def list_notes():
    """获取笔记列表（不含正文内容，节省带宽）"""
    try:
        keyword = (request.args.get('search', '') or '').strip()
        where = '1=1'
        params = []
        if keyword:
            where = 'title LIKE ?'
            params.append(f'%{keyword}%')

        conn = _get_db()
        rows = conn.execute(
            f'''SELECT id, title, pinned,
                CASE WHEN content != '' THEN substr(content, 1, 80) ELSE '' END AS preview,
                created_at, updated_at
                FROM notes WHERE {where}
                ORDER BY pinned DESC, updated_at DESC''',
            params
        ).fetchall()
        conn.close()

        return jsonify({'success': True, 'data': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/create', methods=['POST'])
def create_note():
    """创建笔记"""
    try:
        data = request.get_json(silent=True) or {}
        title = (data.get('title', '') or '').strip()
        content = (data.get('content', '') or '').strip()
        if not title and not content:
            return jsonify({'success': False, 'error': '请输入标题或内容'}), 400

        title = title or '未命名笔记'
        conn = _get_db()
        cur = conn.execute(
            'INSERT INTO notes (title, content) VALUES (?, ?)',
            (title, content)
        )
        new_id = cur.lastrowid
        conn.commit()
        row = conn.execute('SELECT * FROM notes WHERE id = ?', (new_id,)).fetchone()
        conn.close()

        _log(f'创建笔记: {title} (ID:{new_id})')
        return jsonify({'success': True, 'data': dict(row)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/<int:nid>', methods=['GET'])
def get_note(nid):
    """获取单篇笔记完整内容"""
    try:
        conn = _get_db()
        row = conn.execute('SELECT * FROM notes WHERE id = ?', (nid,)).fetchone()
        conn.close()
        if not row:
            return jsonify({'success': False, 'error': '笔记不存在'}), 404
        return jsonify({'success': True, 'data': dict(row)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/<int:nid>', methods=['PUT'])
def update_note(nid):
    """更新笔记"""
    try:
        data = request.get_json(silent=True) or {}

        conn = _get_db()
        existing = conn.execute('SELECT id FROM notes WHERE id = ?', (nid,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({'success': False, 'error': '笔记不存在'}), 404

        sets = []
        params = []

        if 'title' in data:
            title = (data.get('title', '') or '').strip() or '未命名笔记'
            sets.append('title = ?')
            params.append(title)

        if 'content' in data:
            content = (data.get('content', '') or '')
            sets.append('content = ?')
            params.append(content)

        if 'pinned' in data:
            sets.append('pinned = ?')
            params.append(int(data['pinned']))

        if sets:
            sets.append("updated_at = datetime('now','localtime')")
        else:
            conn.close()
            return jsonify({'success': False, 'error': '没有需要更新的字段'}), 400

        params.append(nid)
        conn.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
        row = conn.execute('SELECT * FROM notes WHERE id = ?', (nid,)).fetchone()
        conn.close()

        _log(f'更新笔记: ID:{nid}')
        return jsonify({'success': True, 'data': dict(row)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/<int:nid>', methods=['DELETE'])
def delete_note(nid):
    """删除笔记"""
    try:
        conn = _get_db()
        existing = conn.execute('SELECT id, title FROM notes WHERE id = ?', (nid,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({'success': False, 'error': '笔记不存在'}), 404

        conn.execute('DELETE FROM notes WHERE id = ?', (nid,))
        conn.commit()
        conn.close()

        _log(f'删除笔记: {existing["title"]} (ID:{nid})')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/<int:nid>/pin', methods=['PUT'])
def toggle_pin(nid):
    """切换置顶状态"""
    try:
        conn = _get_db()
        existing = conn.execute('SELECT id, pinned FROM notes WHERE id = ?', (nid,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({'success': False, 'error': '笔记不存在'}), 404

        new_pinned = 1 if existing['pinned'] == 0 else 0
        conn.execute(
            "UPDATE notes SET pinned = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (new_pinned, nid)
        )
        conn.commit()
        row = conn.execute('SELECT * FROM notes WHERE id = ?', (nid,)).fetchone()
        conn.close()

        _log(f'切换置顶: ID:{nid} -> pinned={new_pinned}')
        return jsonify({'success': True, 'data': dict(row)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# -*- coding: utf-8 -*-
"""GPA系统 Blueprint"""
import os, json
from flask import Blueprint, request, jsonify

from .utils import make_logger, make_db, safe_json_load

bp = Blueprint('gpa', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GPA_DB_PATH = os.path.join(BASE_DIR, '绩点', 'gpa.db')
LOG_FILE = os.path.join(BASE_DIR, '绩点', 'gpa.log')
_get_db = make_db(GPA_DB_PATH)

_log = make_logger(LOG_FILE)

DEFAULT_SEMESTERS = [
    {'id': 'sem-1', 'name': '大一上学期', 'startDate': '', 'endDate': '', 'note': ''},
    {'id': 'sem-2', 'name': '大一下学期', 'startDate': '', 'endDate': '', 'note': ''},
    {'id': 'sem-3', 'name': '大二上学期', 'startDate': '', 'endDate': '', 'note': ''},
    {'id': 'sem-4', 'name': '大二下学期', 'startDate': '', 'endDate': '', 'note': ''},
    {'id': 'sem-5', 'name': '大三上学期', 'startDate': '', 'endDate': '', 'note': ''},
    {'id': 'sem-6', 'name': '大三下学期', 'startDate': '', 'endDate': '', 'note': ''},
    {'id': 'sem-7', 'name': '大四上学期', 'startDate': '', 'endDate': '', 'note': ''},
    {'id': 'sem-8', 'name': '大四下学期', 'startDate': '', 'endDate': '', 'note': ''}
]

DEFAULT_COURSES_SEM1 = [
    {'id': 'c-1', 'semesterId': 'sem-1', 'name': '大学生心理健康', 'credit': 2, 'score': 91, 'category': 'public_required'},
    {'id': 'c-2', 'semesterId': 'sem-1', 'name': '党史', 'credit': 1, 'score': 89, 'category': 'public_required'},
    {'id': 'c-3', 'semesterId': 'sem-1', 'name': '高等数学C1', 'credit': 4, 'score': 85, 'category': 'major_required'},
    {'id': 'c-4', 'semesterId': 'sem-1', 'name': '高级交际英语1', 'credit': 3, 'score': 75, 'category': 'public_required'},
    {'id': 'c-5', 'semesterId': 'sem-1', 'name': '国家安全教育', 'credit': 1, 'score': 80, 'category': 'public_required'},
    {'id': 'c-6', 'semesterId': 'sem-1', 'name': '逻辑学导论', 'credit': 3, 'score': 60, 'category': 'major_required'},
    {'id': 'c-7', 'semesterId': 'sem-1', 'name': '思想道德与法治', 'credit': 2.5, 'score': 90, 'category': 'public_required'},
    {'id': 'c-8', 'semesterId': 'sem-1', 'name': '素质体育1', 'credit': 1, 'score': 90, 'category': 'public_required'},
    {'id': 'c-9', 'semesterId': 'sem-1', 'name': '微观经济学', 'credit': 3, 'score': 80, 'category': 'major_required'},
    {'id': 'c-10', 'semesterId': 'sem-1', 'name': '形势与政策1', 'credit': 0.5, 'score': 96, 'category': 'public_required'},
    {'id': 'c-11', 'semesterId': 'sem-1', 'name': '政治学原理', 'credit': 3, 'score': 84, 'category': 'major_required'}
]


def init_db():
    try:
        os.makedirs(os.path.dirname(GPA_DB_PATH), exist_ok=True)
        conn = _get_db()
        conn.execute('''CREATE TABLE IF NOT EXISTS gpa_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            semesters TEXT DEFAULT '[]',
            courses TEXT DEFAULT '[]',
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )''')
        existing = conn.execute('SELECT id, semesters, courses FROM gpa_data LIMIT 1').fetchone()
        if not existing:
            conn.execute('INSERT INTO gpa_data (semesters, courses) VALUES (?, ?)',
                         (json.dumps(DEFAULT_SEMESTERS, ensure_ascii=False),
                          json.dumps(DEFAULT_COURSES_SEM1, ensure_ascii=False)))
        else:
            changed = False
            cur_courses = safe_json_load(existing[2])
            has_sem1 = any(c.get('semesterId') == 'sem-1' for c in cur_courses if isinstance(c, dict))
            if not has_sem1:
                cur_courses.extend(DEFAULT_COURSES_SEM1)
                changed = True
            if changed:
                conn.execute('''UPDATE gpa_data SET
                    courses = ?,
                    updated_at = datetime('now','localtime')
                ''', (json.dumps(cur_courses, ensure_ascii=False),))
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback
        print(f'[init_gpa_db ERROR] {e}\n{traceback.format_exc()}', flush=True)


@bp.route('/api/gpa/data', methods=['GET'])
def get_data():
    conn = _get_db()
    try:
        row = conn.execute('SELECT id, semesters, courses, updated_at FROM gpa_data LIMIT 1').fetchone()
        if not row:
            return jsonify({'success': False, 'error': '无数据'}), 404
        return jsonify({'success': True, 'data': {
            'semesters': safe_json_load(row[1]),
            'courses': safe_json_load(row[2]),
            'updated_at': row[3]
        }})
    except Exception as e:
        import traceback
        print(f'[gpa_get_data ERROR] {e}\n{traceback.format_exc()}', flush=True)
        return jsonify({'success': False, 'error': '服务器错误'}), 500
    finally:
        conn.close()


@bp.route('/api/gpa/data', methods=['POST'])
def save_data():
    data = request.get_json(silent=True) or {}
    if 'semesters' not in data and 'courses' not in data:
        return jsonify({'success': False, 'error': '缺少数据'}), 400
    conn = _get_db()
    try:
        sets, vals = [], []
        for field in ('semesters', 'courses'):
            if field in data:
                sets.append(f'{field} = ?')
                vals.append(json.dumps(data.get(field, []), ensure_ascii=False))
        conn.execute(f"UPDATE gpa_data SET {', '.join(sets)}, updated_at = datetime('now','localtime')", vals)
        conn.commit()
        _log(f'保存GPA数据: {len(data.get("semesters",[]))}学期 {len(data.get("courses",[]))}课程')
        return jsonify({'success': True})
    finally:
        conn.close()


@bp.route('/api/gpa/manifest')
def pwa_manifest():
    return jsonify({
        'name': 'GPA',
        'short_name': 'GPA',
        'description': 'GPA成绩管理系统',
        'start_url': '/gpa',
        'display': 'standalone',
        'orientation': 'portrait',
        'background_color': '#f5f7fa',
        'theme_color': '#2f7d6b',
        'icons': [{
            'src': "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 192 192'><rect width='192' height='192' rx='40' fill='%23ef4444'/><text x='96' y='132' text-anchor='middle' font-size='68' fill='white' font-weight='bold' font-family='Arial,sans-serif'>GPA</text></svg>",
            'sizes': '192x192',
            'type': 'image/svg+xml'
        }, {
            'src': "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='80' fill='%23ef4444'/><text x='256' y='360' text-anchor='middle' font-size='180' fill='white' font-weight='bold' font-family='Arial,sans-serif'>GPA</text></svg>",
            'sizes': '512x512',
            'type': 'image/svg+xml'
        }]
    })

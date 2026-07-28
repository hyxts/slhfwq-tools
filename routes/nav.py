# -*- coding: utf-8 -*-
"""导航首页跨模块数据摘要 Blueprint"""
import os, sqlite3
from flask import Blueprint, jsonify

bp = Blueprint('nav', __name__, url_prefix='/api/nav')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _safe_query(db_path, sql, params=()):
    """安全查询 SQLite，出错返回 None"""
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        result = conn.execute(sql, params).fetchall()
        conn.close()
        return result
    except Exception:
        return None


@bp.route('/summary')
def nav_summary():
    """返回跨模块数据摘要：倒计时、记账债务、绩点、人情"""
    result = {
        'countdown': [],
        'accounting': {},
        'gpa': {},
        'renqing': {},
    }

    # ---- 倒计时：最近 3 个即将到来的事件 ----
    countdown_rows = _safe_query(
        os.path.join(BASE_DIR, '倒计时', 'countdown.db'),
        '''SELECT name, date_str, is_lunar, category
           FROM events
           WHERE date_str >= date('now')
           ORDER BY date_str ASC
           LIMIT 3'''
    )
    if countdown_rows:
        from datetime import date
        today = date.today()
        for row in countdown_rows:
            try:
                target_date = date.fromisoformat(row['date_str'])
                days = (target_date - today).days
            except Exception:
                days = None
            result['countdown'].append({
                'name': row['name'],
                'date': row['date_str'],
                'days': days,
                'category': row['category'] or '',
                'is_lunar': bool(row['is_lunar']) if 'is_lunar' in row.keys() else False,
            })

    # ---- 记账：本月收支净额 ----
    acct_rows = _safe_query(
        os.path.join(BASE_DIR, '记账', 'accounting.db'),
        '''SELECT
             SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END) as total_income,
             SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END) as total_expense,
             COUNT(*) as record_count
           FROM transactions
           WHERE date >= date('now', 'start of month')
             AND date <= date('now')'''
    )
    if acct_rows and acct_rows[0]:
        r = acct_rows[0]
        total_income = r['total_income'] or 0
        total_expense = r['total_expense'] or 0
        result['accounting'] = {
            'month_income': round(total_income, 2),
            'month_expense': round(total_expense, 2),
            'month_net': round(total_income - total_expense, 2),
            'month_records': r['record_count'] or 0,
        }

    # ---- 绩点：最新学期 GPA ----
    gpa_rows = _safe_query(
        os.path.join(BASE_DIR, '绩点', 'gpa.db'),
        '''SELECT name, gpa, total_credits
           FROM semesters
           ORDER BY id DESC
           LIMIT 1'''
    )
    if gpa_rows and gpa_rows[0]:
        r = gpa_rows[0]
        result['gpa'] = {
            'semester': r['name'],
            'gpa': round(r['gpa'], 2) if r['gpa'] else None,
            'credits': round(r['total_credits'], 1) if r['total_credits'] else None,
        }

    # ---- 人情：今年净额 ----
    from datetime import date
    current_year = str(date.today().year)
    renqing_rows = _safe_query(
        os.path.join(BASE_DIR, '人情', 'gifts.db'),
        '''SELECT
             SUM(CASE WHEN direction = '收' THEN amount ELSE 0 END) as total_rec,
             SUM(CASE WHEN direction = '送' THEN amount ELSE 0 END) as total_send,
             COUNT(*) as record_count,
             COUNT(DISTINCT name) as person_count
           FROM records
           WHERE date LIKE ? || '%' AND date IS NOT NULL AND date != '' ''',
        (current_year,)
    )
    if renqing_rows and renqing_rows[0]:
        r = renqing_rows[0]
        total_rec = r['total_rec'] or 0
        total_send = r['total_send'] or 0
        result['renqing'] = {
            'year': current_year,
            'total_rec': round(total_rec, 2),
            'total_send': round(total_send, 2),
            'net': round(total_rec - total_send, 2),
            'record_count': r['record_count'] or 0,
            'person_count': r['person_count'] or 0,
        }

    return jsonify({'success': True, 'data': result})

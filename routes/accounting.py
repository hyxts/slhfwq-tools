# -*- coding: utf-8 -*-
"""债务往来追踪 Blueprint — 三向模型：付给(我给Ta) / 收到(Ta给我) / 应收(Ta应还我)
    净额 = 付给 + 应收 - 收到  (>0 Ta欠我, <0 我欠Ta)"""

import os, json
from datetime import datetime
from flask import Blueprint, request, jsonify, send_from_directory

from .utils import make_logger, make_db, TZ, _now

bp = Blueprint('accounting', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(BASE_DIR, '记账')
DB_FILE = os.path.join(DB_DIR, 'accounting.db')
_get_db = make_db(DB_FILE)

_log = make_logger(os.path.join(DB_DIR, 'accounting.log'))

# ========== 三向分类预设 ==========
PAY_CATEGORIES = ['借出', '垫付', '还借款', '礼金', '其他']
RECV_CATEGORIES = ['借入', '分摊', '收还款', '礼金', '其他']
RECEIVABLE_CATEGORIES = ['欠款', '补偿', '其他']

CATEGORY_PRESETS = {
    '付给': PAY_CATEGORIES,
    '收到': RECV_CATEGORIES,
    '应收': RECEIVABLE_CATEGORIES,
}

# 旧→新类型映射
OLD_TO_NEW = {'收入': '收到', '支出': '付给', '他欠我': '付给', '我欠他': '收到'}

# 旧分类→新分类映射
CAT_MAP = {
    '借款': '借入',        # 收入-借款 → 收到-借入
    '借出': '借出',        # 支出-借出 → 付给-借出
    '借出款项': '借出',    # 支出-借出款项 → 付给-借出
    '还款收回': '收还款',  # 收入-还款收回 → 收到-收还款
    '还借款': '还借款',    # 支出-还借款 → 付给-还借款
    '还款支出': '还借款',  # 支出-还款支出 → 付给-还借款
    '还款': '还借款',      # 历史兼容
    '垫付': '垫付',        # 他欠我-垫付 → 付给-垫付
    '分摊': '分摊',        # 我欠他-分摊 → 收到-分摊
    '欠款': '欠款',        # 直接沿用
    '礼金': '礼金',        # 直接沿用
    '其他': '其他',        # 直接沿用
    '其他收入': '其他',
    '其他支出': '其他',
}


def _auto_settle_if_needed(conn, event_id):
    if not event_id:
        return
    ev = conn.execute('SELECT id, status FROM events WHERE id=?', (event_id,)).fetchone()
    if not ev or ev['status'] != '进行中':
        return
    stats = conn.execute('''
        SELECT
            COALESCE(SUM(CASE WHEN type='付给' THEN amount ELSE 0 END), 0) as paid,
            COALESCE(SUM(CASE WHEN type='收到' THEN amount ELSE 0 END), 0) as recv,
            COALESCE(SUM(CASE WHEN type='应收' THEN amount ELSE 0 END), 0) as receivable,
            COUNT(*) as cnt
        FROM records WHERE event_id=?
    ''', (event_id,)).fetchone()
    if stats['cnt'] > 0:
        net = stats['paid'] + stats['receivable'] - stats['recv']
        if abs(net) < 0.001:
            conn.execute("UPDATE events SET status='已结清' WHERE id=?", (event_id,))


def init_db():
    """初始化数据库 — 双向模型 (付给/收到)
    使用 PRAGMA user_version 跳过已完成的迁移检查，大幅减少启动开销"""
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        conn = _get_db()

        conn.execute('''CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )''')

        # 检查迁移版本，跳过已完成的迁移（优化启动速度）
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        MIGRATED = ver >= 2

        # --- records 表 — 含旧表迁移逻辑 ---
        need_migrate = False
        if not MIGRATED:
            col_def = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='records'"
            ).fetchone()
            if col_def:
                sql_text = col_def[0] or ''
                if "'收入'" in sql_text and "'付给'" not in sql_text:
                    need_migrate = True

        if need_migrate:
            # 迁移: 重建表为新二类型
            conn.execute("BEGIN")
            existing_cols = [r[1] for r in conn.execute('PRAGMA table_info(records)').fetchall()]
            if 'event_id' not in existing_cols:
                conn.execute('ALTER TABLE records ADD COLUMN event_id INTEGER DEFAULT NULL')
            conn.execute('''CREATE TABLE records_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('付给','收到','应收')),
                amount REAL NOT NULL,
                category TEXT DEFAULT '',
                date TEXT NOT NULL,
                note TEXT DEFAULT '',
                event_id INTEGER DEFAULT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            )''')
            # 迁移数据: 类型+分类一并转换
            old_recs = conn.execute('SELECT * FROM records').fetchall()
            for r in old_recs:
                old_type = r['type']
                new_type = OLD_TO_NEW.get(old_type, old_type)
                old_cat = r['category'] or ''
                new_cat = CAT_MAP.get(old_cat, old_cat)
                if old_cat and old_cat not in CAT_MAP:
                    new_cat = old_cat
                conn.execute(
                    '''INSERT INTO records_new
                       (id, account_id, type, amount, category, date, note, event_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (r['id'], r['account_id'], new_type, r['amount'],
                     new_cat, r['date'], r['note'], r['event_id'], r['created_at'])
                )
            conn.execute('DROP TABLE records')
            conn.execute('ALTER TABLE records_new RENAME TO records')
            conn.execute('COMMIT')
        else:
            conn.execute('''CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('付给','收到','应收')),
                amount REAL NOT NULL,
                category TEXT DEFAULT '',
                date TEXT NOT NULL,
                note TEXT DEFAULT '',
                event_id INTEGER DEFAULT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            )''')

        # 补 event_id 列
        existing_cols = [r[1] for r in conn.execute('PRAGMA table_info(records)').fetchall()]
        if 'event_id' not in existing_cols:
            conn.execute('ALTER TABLE records ADD COLUMN event_id INTEGER DEFAULT NULL')

        # 迁移: 检测 records 表 CHECK 约束是否需要更新（缺少 应收 或有旧类型残留）
        needs_records_mig = False
        if not MIGRATED:
            records_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='records'"
            ).fetchone()
            if records_sql and records_sql[0]:
                rs = records_sql[0] or ''
                if "'应收'" not in rs or any(t in rs for t in ("'收入'", "'支出'", "'他欠我'", "'我欠他'")):
                    needs_records_mig = True

        if needs_records_mig:
            conn.execute("BEGIN")
            # 无约束临时表，避免 CHECK 冲突死胡同
            conn.execute('''CREATE TABLE records_mig (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT DEFAULT '',
                date TEXT NOT NULL,
                note TEXT DEFAULT '',
                event_id INTEGER DEFAULT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            )''')
            conn.execute('INSERT INTO records_mig SELECT * FROM records')
            conn.execute('DROP TABLE records')
            conn.execute('''CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('付给','收到','应收')),
                amount REAL NOT NULL,
                category TEXT DEFAULT '',
                date TEXT NOT NULL,
                note TEXT DEFAULT '',
                event_id INTEGER DEFAULT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            )''')
            # 从临时表回迁数据并映射 type 值
            old_rows = conn.execute('SELECT * FROM records_mig').fetchall()
            for r in old_rows:
                new_type = OLD_TO_NEW.get(r['type'], r['type'])
                if new_type not in ('付给', '收到', '应收'):
                    new_type = '付给'
                conn.execute(
                    '''INSERT INTO records
                       (id, account_id, type, amount, category, date, note, event_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (r['id'], r['account_id'], new_type, r['amount'],
                     r['category'], r['date'], r['note'], r['event_id'], r['created_at'])
                )
            conn.execute('DROP TABLE records_mig')
            conn.execute('COMMIT')

        conn.execute('''CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            status TEXT DEFAULT '进行中' CHECK(status IN ('进行中','已结清')),
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        )''')

        conn.execute('CREATE INDEX IF NOT EXISTS idx_records_account ON records(account_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_records_date ON records(date)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_records_event ON records(event_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id)')

        # --- categories 表 — 带完整迁移逻辑 ---
        has_old_constraint = False
        if not MIGRATED:
            cats_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='categories'"
            ).fetchone()
            if cats_sql and cats_sql[0]:
                sql_text = cats_sql[0]
                if ("'收入'" in sql_text or "'支出'" in sql_text
                        or "'他欠我'" in sql_text or "'我欠他'" in sql_text):
                    has_old_constraint = True

        if has_old_constraint:
            # 完整迁移：需要绕过旧 CHECK 约束
            # 1. 把旧数据导出到无约束临时表
            conn.execute("BEGIN")
            conn.execute('''CREATE TABLE categories_mig (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0
            )''')
            conn.execute('INSERT INTO categories_mig (id, type, name, sort_order) '
                         'SELECT id, type, name, COALESCE(sort_order, 0) FROM categories')
            # 2. 删旧表，建新表
            conn.execute('DROP TABLE categories')
            conn.execute('''CREATE TABLE categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('付给','收到','应收')),
                name TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0
            )''')
            # 3. 迁移数据：类型+分类名一并映射
            old_rows = conn.execute('SELECT id, type, name, sort_order FROM categories_mig').fetchall()
            seen = set()
            for r in old_rows:
                new_type = OLD_TO_NEW.get(r['type'], r['type'])
                if new_type not in ('付给', '收到', '应收'):
                    new_type = '付给'  # 兜底
                new_name = CAT_MAP.get(r['name'], r['name'])
                key = (new_type, new_name)
                if key in seen:
                    continue
                seen.add(key)
                conn.execute(
                    'INSERT INTO categories (type, name, sort_order) VALUES (?, ?, ?)',
                    (new_type, new_name, r['sort_order'] or 0)
                )
            conn.execute('DROP TABLE categories_mig')
            conn.execute('COMMIT')
        else:
            # 表不存在或已是新格式，直接创建
            conn.execute('''CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('付给','收到','应收')),
                name TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0
            )''')

        # 种子数据
        existing_cats = conn.execute('SELECT COUNT(*) FROM categories').fetchone()[0]
        if existing_cats == 0:
            defaults = [
                ('付给', '借出', 1), ('付给', '垫付', 2), ('付给', '还借款', 3),
                ('付给', '礼金', 4), ('付给', '其他', 5),
                ('收到', '借入', 1), ('收到', '分摊', 2), ('收到', '收还款', 3),
                ('收到', '礼金', 4), ('收到', '其他', 5),
                ('应收', '欠款', 1), ('应收', '补偿', 2), ('应收', '其他', 3),
            ]
            conn.executemany('INSERT INTO categories (type, name, sort_order) VALUES (?, ?, ?)', defaults)

        # 补充缺失分类
        missing = [
            ('付给', '垫付', 2), ('付给', '还借款', 3), ('付给', '其他', 5),
            ('收到', '分摊', 2), ('收到', '收还款', 3),
            ('应收', '欠款', 1), ('应收', '补偿', 2), ('应收', '其他', 3),
        ]
        for t, n, s in missing:
            if not conn.execute("SELECT 1 FROM categories WHERE type=? AND name=?", (t, n)).fetchone():
                conn.execute("INSERT INTO categories (type, name, sort_order) VALUES (?, ?, ?)", (t, n, s))

        # 标记迁移完成，后续启动跳过 sqlite_master 解析
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback
        print(f'[init_accounting_db ERROR] {e}\n{traceback.format_exc()}', flush=True)


# ==================== 往来账户 API ====================

def _account_balance(conn, aid=None):
    """计算账户净额: 付给 + 应收 - 收到"""
    where = 'WHERE r.account_id=?' if aid else ''
    params = (aid,) if aid else ()
    rows = conn.execute(
        f'''SELECT a.id, a.name, a.note, a.created_at,
            COALESCE(SUM(CASE WHEN r.type='付给' THEN r.amount ELSE 0 END), 0) as paid,
            COALESCE(SUM(CASE WHEN r.type='收到' THEN r.amount ELSE 0 END), 0) as recv,
            COALESCE(SUM(CASE WHEN r.type='应收' THEN r.amount ELSE 0 END), 0) as receivable,
            COUNT(r.id) as record_count
        FROM accounts a LEFT JOIN records r ON a.id=r.account_id
        {where} GROUP BY a.id ORDER BY a.id DESC''',
        params
    ).fetchall()
    return [dict(r) for r in rows]


@bp.route('/api/accounting/accounts', methods=['GET'])
def list_accounts():
    try:
        conn = _get_db()
        rows = _account_balance(conn)
        conn.close()
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/accounting/accounts', methods=['POST'])
def create_account():
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'success': False, 'error': '请输入对象名称'}), 400
        note = data.get('note', '').strip()
        conn = _get_db()
        conn.execute('INSERT INTO accounts (name, note) VALUES (?, ?)', (name, note))
        conn.commit()
        conn.close()
        _log(f'新增对象: {name}')
        return jsonify({'success': True})
    except Exception as e:
        err = str(e)
        if 'UNIQUE' in err.upper():
            return jsonify({'success': False, 'error': f'对象"{name}"已存在'}), 400
        return jsonify({'success': False, 'error': err}), 500


@bp.route('/api/accounting/accounts/<int:aid>', methods=['PUT'])
def update_account(aid):
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'success': False, 'error': '请输入对象名称'}), 400
        note = data.get('note', '').strip()
        conn = _get_db()
        conn.execute('UPDATE accounts SET name=?, note=? WHERE id=?', (name, note, aid))
        conn.commit()
        conn.close()
        _log(f'更新对象: {name} (ID:{aid})')
        return jsonify({'success': True})
    except Exception as e:
        err = str(e)
        if 'UNIQUE' in err.upper():
            return jsonify({'success': False, 'error': f'对象"{data.get("name","")}"已存在'}), 400
        return jsonify({'success': False, 'error': err}), 500


@bp.route('/api/accounting/accounts/<int:aid>', methods=['DELETE'])
def delete_account(aid):
    try:
        conn = _get_db()
        acc = conn.execute('SELECT name FROM accounts WHERE id=?', (aid,)).fetchone()
        if not acc:
            conn.close()
            return jsonify({'success': False, 'error': '对象不存在'}), 404
        name = acc['name']
        conn.execute('DELETE FROM records WHERE account_id=?', (aid,))
        conn.execute('DELETE FROM events WHERE account_id=?', (aid,))
        conn.execute('DELETE FROM accounts WHERE id=?', (aid,))
        conn.commit()
        conn.close()
        _log(f'删除对象: {name} (ID:{aid})')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 事件 API ====================

@bp.route('/api/accounting/events', methods=['GET'])
def list_events():
    try:
        aid = request.args.get('account_id', '')
        conn = _get_db()
        where = '1=1'
        params = []
        if aid:
            where = 'e.account_id=?'
            params.append(int(aid))
        rows = conn.execute(
            f'''SELECT e.*, a.name as account_name,
                COALESCE(SUM(CASE WHEN r.type='付给' THEN r.amount ELSE 0 END), 0) as paid,
                COALESCE(SUM(CASE WHEN r.type='收到' THEN r.amount ELSE 0 END), 0) as recv,
                COALESCE(SUM(CASE WHEN r.type='应收' THEN r.amount ELSE 0 END), 0) as receivable,
                COUNT(r.id) as record_count
            FROM events e
            JOIN accounts a ON e.account_id=a.id
            LEFT JOIN records r ON e.id=r.event_id
            WHERE {where}
            GROUP BY e.id ORDER BY e.status ASC, e.id DESC''',
            params
        ).fetchall()
        conn.close()
        return jsonify({'success': True, 'data': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/accounting/events', methods=['POST'])
def create_event():
    try:
        data = request.get_json(silent=True) or {}
        account_id = data.get('account_id')
        name = data.get('name', '').strip()
        if not account_id:
            return jsonify({'success': False, 'error': '请选择对象'}), 400
        if not name:
            return jsonify({'success': False, 'error': '请输入事项名称'}), 400
        note = data.get('note', '').strip()
        conn = _get_db()
        acc = conn.execute('SELECT id FROM accounts WHERE id=?', (account_id,)).fetchone()
        if not acc:
            conn.close()
            return jsonify({'success': False, 'error': '对象不存在'}), 404
        conn.execute('INSERT INTO events (account_id, name, note) VALUES (?, ?, ?)',
                     (account_id, name, note))
        conn.commit()
        conn.close()
        _log(f'新增事项: {name} (对象ID:{account_id})')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/accounting/events/<int:eid>', methods=['PUT'])
def update_event(eid):
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'success': False, 'error': '请输入事项名称'}), 400
        note = data.get('note', '').strip()
        conn = _get_db()
        conn.execute('UPDATE events SET name=?, note=? WHERE id=?', (name, note, eid))
        conn.commit()
        conn.close()
        _log(f'更新事项: {name} (ID:{eid})')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/accounting/events/<int:eid>', methods=['DELETE'])
def delete_event(eid):
    try:
        conn = _get_db()
        ev = conn.execute('SELECT name FROM events WHERE id=?', (eid,)).fetchone()
        if not ev:
            conn.close()
            return jsonify({'success': False, 'error': '事项不存在'}), 404
        conn.execute('UPDATE records SET event_id=NULL WHERE event_id=?', (eid,))
        conn.execute('DELETE FROM events WHERE id=?', (eid,))
        conn.commit()
        conn.close()
        _log(f'删除事项: {ev["name"]} (ID:{eid})')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/accounting/events/<int:eid>/settle', methods=['PUT'])
def toggle_event_settle(eid):
    try:
        conn = _get_db()
        ev = conn.execute('SELECT id, name, status FROM events WHERE id=?', (eid,)).fetchone()
        if not ev:
            conn.close()
            return jsonify({'success': False, 'error': '事项不存在'}), 404
        new_status = '已结清' if ev['status'] == '进行中' else '进行中'
        conn.execute('UPDATE events SET status=? WHERE id=?', (new_status, eid))
        conn.commit()
        conn.close()
        _log(f'事项状态变更: {ev["name"]} -> {new_status}')
        return jsonify({'success': True, 'data': {'status': new_status}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 记录 API ====================

VALID_TYPES = ('付给', '收到', '应收')


def _validate_record_data(data):
    """校验记账流水数据，返回 (error_msg_or_None, clean_data_dict)"""
    account_id = data.get('account_id')
    event_id = data.get('event_id') or None
    rtype = data.get('type', '')
    amount = data.get('amount', 0)
    category = data.get('category', '')
    date_str = data.get('date', '')
    note = (data.get('note', '') or '').strip()

    if not account_id:
        return '请选择对象', None
    if rtype not in VALID_TYPES:
        return f'类型不合法：{rtype}', None
    try:
        amount = float(amount)
        if amount <= 0:
            return '金额必须大于0', None
    except (ValueError, TypeError):
        return '金额格式不正确', None
    if not date_str:
        date_str = _now().strftime('%Y-%m-%d')

    if event_id:
        try:
            event_id = int(event_id)
        except (ValueError, TypeError):
            event_id = None

    clean = {
        'account_id': account_id,
        'event_id': event_id,
        'type': rtype,
        'amount': amount,
        'category': category,
        'date': date_str,
        'note': note,
    }
    return None, clean


@bp.route('/api/accounting/records', methods=['GET'])
def list_records():
    try:
        aid = request.args.get('account_id', '')
        eid = request.args.get('event_id', '')
        rtype = request.args.get('type', '')
        start = request.args.get('start', '')
        end = request.args.get('end', '')
        search = request.args.get('search', '')
        limit = int(request.args.get('limit', 100))

        where = ['1=1']
        params = []
        if aid:
            where.append('r.account_id=?')
            params.append(int(aid))
        if eid:
            where.append('r.event_id=?')
            params.append(int(eid))
        if rtype in VALID_TYPES:
            where.append('r.type=?')
            params.append(rtype)
        if start:
            where.append('r.date >= ?')
            params.append(start)
        if end:
            where.append('r.date <= ?')
            params.append(end)
        if search:
            where.append('(r.note LIKE ? OR r.category LIKE ? OR a.name LIKE ? OR ev.name LIKE ?)')
            kw = f'%{search}%'
            params.extend([kw, kw, kw, kw])

        where_clause = ' AND '.join(where)
        conn = _get_db()
        rows = conn.execute(
            f'''SELECT r.*, a.name as account_name,
                COALESCE(ev.name, '') as event_name, COALESCE(ev.status, '') as event_status
            FROM records r
            JOIN accounts a ON r.account_id=a.id
            LEFT JOIN events ev ON r.event_id=ev.id
            WHERE {where_clause}
            ORDER BY r.date DESC, r.id DESC
            LIMIT ?''',
            params + [limit]
        ).fetchall()
        conn.close()
        return jsonify({'success': True, 'data': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/accounting/records', methods=['POST'])
def create_record():
    try:
        data = request.get_json(silent=True) or {}
        err, clean = _validate_record_data(data)
        if err:
            return jsonify({'success': False, 'error': err}), 400

        conn = _get_db()
        acc = conn.execute('SELECT id, name FROM accounts WHERE id=?', (clean['account_id'],)).fetchone()
        if not acc:
            conn.close()
            return jsonify({'success': False, 'error': '对象不存在'}), 404

        if clean['event_id']:
            ev = conn.execute('SELECT id FROM events WHERE id=? AND account_id=?',
                            (clean['event_id'], clean['account_id'])).fetchone()
            if not ev:
                conn.close()
                return jsonify({'success': False, 'error': '所选事项不存在或不属于该对象'}), 400

        conn.execute(
            'INSERT INTO records (account_id, event_id, type, amount, category, date, note) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (clean['account_id'], clean['event_id'], clean['type'], clean['amount'],
             clean['category'], clean['date'], clean['note'])
        )
        _auto_settle_if_needed(conn, clean['event_id'])
        conn.commit()
        conn.close()
        _log(f'新增记录: {acc["name"]} {clean["type"]} {clean["amount"]}元 ({clean["category"]})')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/accounting/records/<int:rid>', methods=['PUT'])
def update_record(rid):
    try:
        data = request.get_json(silent=True) or {}
        err, clean = _validate_record_data(data)
        if err:
            return jsonify({'success': False, 'error': err}), 400

        conn = _get_db()
        conn.execute(
            'UPDATE records SET account_id=?, event_id=?, type=?, amount=?, category=?, date=?, note=? WHERE id=?',
            (clean['account_id'], clean['event_id'], clean['type'], clean['amount'],
             clean['category'], clean['date'], clean['note'], rid)
        )
        _auto_settle_if_needed(conn, clean['event_id'])
        conn.commit()
        conn.close()
        _log(f'更新记录: ID:{rid}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/accounting/records/<int:rid>', methods=['DELETE'])
def delete_record(rid):
    try:
        conn = _get_db()
        rec = conn.execute('SELECT event_id FROM records WHERE id=?', (rid,)).fetchone()
        event_id = rec['event_id'] if rec else None
        conn.execute('DELETE FROM records WHERE id=?', (rid,))
        _auto_settle_if_needed(conn, event_id)
        conn.commit()
        conn.close()
        _log(f'删除记录: ID:{rid}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 统计 API ====================

@bp.route('/api/accounting/stats', methods=['GET'])
def get_stats():
    try:
        aid = request.args.get('account_id', '')
        start = request.args.get('start', '')
        end = request.args.get('end', '')

        where = ['1=1']
        params = []
        if aid:
            where.append('r.account_id=?')
            params.append(int(aid))
        if start:
            where.append('r.date >= ?')
            params.append(start)
        if end:
            where.append('r.date <= ?')
            params.append(end)
        where_clause = ' AND '.join(where)

        conn = _get_db()

        # 总览: 三向汇总
        overview = dict(conn.execute(
            f'''SELECT
                COALESCE(SUM(CASE WHEN r.type='付给' THEN r.amount ELSE 0 END), 0) as total_pay,
                COALESCE(SUM(CASE WHEN r.type='收到' THEN r.amount ELSE 0 END), 0) as total_recv,
                COALESCE(SUM(CASE WHEN r.type='应收' THEN r.amount ELSE 0 END), 0) as total_receivable,
                COALESCE(SUM(CASE WHEN r.type='付给' AND r.category='借出' THEN r.amount ELSE 0 END), 0) as lent,
                COALESCE(SUM(CASE WHEN r.type='收到' AND r.category='借入' THEN r.amount ELSE 0 END), 0) as borrowed,
                COALESCE(SUM(CASE WHEN r.type='付给' AND r.category='垫付' THEN r.amount ELSE 0 END), 0) as reimbursed,
                COALESCE(SUM(CASE WHEN r.type='收到' AND r.category='分摊' THEN r.amount ELSE 0 END), 0) as shared,
                COUNT(*) as total_count,
                COUNT(DISTINCT r.account_id) as account_count
            FROM records r WHERE {where_clause}''',
            params
        ).fetchone())

        # 按对象统计
        by_account = [dict(r) for r in conn.execute(
            f'''SELECT a.id, a.name,
                COALESCE(SUM(CASE WHEN r.type='付给' THEN r.amount ELSE 0 END), 0) as pay,
                COALESCE(SUM(CASE WHEN r.type='收到' THEN r.amount ELSE 0 END), 0) as recv,
                COALESCE(SUM(CASE WHEN r.type='应收' THEN r.amount ELSE 0 END), 0) as receivable,
                COUNT(r.id) as count
            FROM accounts a LEFT JOIN records r ON a.id=r.account_id AND {where_clause}
            GROUP BY a.id ORDER BY (pay + receivable - recv) DESC''',
            params
        ).fetchall()]

        # 按事项统计
        ev_where_parts = ['1=1']
        ev_params_list = []
        if aid:
            ev_where_parts.append('e.account_id=?')
            ev_params_list.append(int(aid))
        if start:
            ev_where_parts.append('(r.date >= ? OR r.id IS NULL)')
            ev_params_list.append(start)
        if end:
            ev_where_parts.append('(r.date <= ? OR r.id IS NULL)')
            ev_params_list.append(end)
        by_event = [dict(r) for r in conn.execute(
            f'''SELECT e.id, e.name, e.status, a.name as account_name,
                COALESCE(SUM(CASE WHEN r.type='付给' THEN r.amount ELSE 0 END), 0) as pay,
                COALESCE(SUM(CASE WHEN r.type='收到' THEN r.amount ELSE 0 END), 0) as recv,
                COALESCE(SUM(CASE WHEN r.type='应收' THEN r.amount ELSE 0 END), 0) as receivable,
                COUNT(r.id) as count
            FROM events e
            JOIN accounts a ON e.account_id=a.id
            LEFT JOIN records r ON e.id=r.event_id
            WHERE {' AND '.join(ev_where_parts)}
            GROUP BY e.id
            ORDER BY e.status ASC, e.id DESC''',
            ev_params_list
        ).fetchall()]

        # 按分类统计
        by_category = [dict(r) for r in conn.execute(
            f'''SELECT r.category, r.type,
                COALESCE(SUM(r.amount), 0) as total, COUNT(*) as count
            FROM records r WHERE {where_clause} AND r.category != ''
            GROUP BY r.category, r.type ORDER BY total DESC''',
            params
        ).fetchall()]

        # 月度趋势（按方向拆分）
        by_month = [dict(r) for r in conn.execute(
            f'''SELECT substr(r.date,1,7) as month,
                COALESCE(SUM(CASE WHEN r.type='付给' THEN r.amount ELSE 0 END), 0) as pay,
                COALESCE(SUM(CASE WHEN r.type='收到' THEN r.amount ELSE 0 END), 0) as recv,
                COALESCE(SUM(CASE WHEN r.type='应收' THEN r.amount ELSE 0 END), 0) as receivable,
                COUNT(*) as count
            FROM records r WHERE {where_clause}
            GROUP BY month ORDER BY month ASC''',
            params
        ).fetchall()]

        # 事项结清率
        settlement_stats = dict(conn.execute(
            '''SELECT
                COUNT(*) as total_events,
                SUM(CASE WHEN status='已结清' THEN 1 ELSE 0 END) as settled_events
            FROM events'''
        ).fetchone())

        conn.close()
        return jsonify({
            'success': True,
            'data': {
                'overview': overview,
                'by_account': by_account,
                'by_event': by_event,
                'by_category': by_category,
                'by_month': by_month,
                'settlement': settlement_stats
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 分类管理 API ====================

@bp.route('/api/accounting/categories', methods=['GET'])
def get_categories():
    rtype = request.args.get('type', '')
    conn = _get_db()
    try:
        if rtype and rtype in ('付给', '收到', '应收'):
            rows = conn.execute(
                'SELECT name FROM categories WHERE type=? ORDER BY sort_order, id', (rtype,)
            ).fetchall()
            names = [r['name'] for r in rows]
            if not names:
                names = CATEGORY_PRESETS.get(rtype, [])
            return jsonify({'success': True, 'data': names})
        rows = conn.execute(
            'SELECT id, type, name, sort_order FROM categories ORDER BY type, sort_order, id'
        ).fetchall()
        result = {'付给': [], '收到': [], '应收': []}
        for r in rows:
            t = r['type']
            if t not in result:
                result[t] = []  # 防御：未知类型也收纳
            result[t].append({'id': r['id'], 'name': r['name']})
        for t in result:
            if not result[t]:
                result[t] = [{'id': 0, 'name': n} for n in CATEGORY_PRESETS.get(t, [])]
        return jsonify({'success': True, 'data': result})
    finally:
        conn.close()


@bp.route('/api/accounting/categories', methods=['POST'])
def add_category():
    try:
        data = request.get_json(silent=True) or {}
        ctype = data.get('type', '')
        name = data.get('name', '').strip()
        if not ctype or ctype not in ('付给', '收到', '应收'):
            return jsonify({'success': False, 'error': '类型不合法'}), 400
        if not name:
            return jsonify({'success': False, 'error': '请输入分类名称'}), 400
        conn = _get_db()
        existing = conn.execute(
            'SELECT id FROM categories WHERE type=? AND name=?', (ctype, name)
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({'success': False, 'error': f'分类"{name}"已存在'}), 400
        conn.execute('INSERT INTO categories (type, name) VALUES (?, ?)', (ctype, name))
        conn.commit()
        conn.close()
        _log(f'新增分类: {ctype} - {name}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/accounting/categories/<int:cid>', methods=['DELETE'])
def delete_category(cid):
    try:
        conn = _get_db()
        conn.execute('DELETE FROM categories WHERE id=?', (cid,))
        conn.commit()
        conn.close()
        _log(f'删除分类: ID:{cid}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 转账 API ====================

@bp.route('/api/accounting/transfer', methods=['POST'])
def transfer():
    """两个对象间的债务转移"""
    try:
        data = request.get_json(silent=True) or {}
        from_id = int(data.get('from_account_id', 0))
        to_id = int(data.get('to_account_id', 0))
        try:
            amount = float(data.get('amount', 0))
            if amount <= 0:
                return jsonify({'success': False, 'error': '金额必须大于0'}), 400
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': '金额格式不正确'}), 400

        if from_id == to_id:
            return jsonify({'success': False, 'error': '不能转给自己'}), 400

        note = data.get('note', '').strip()
        date_str = data.get('date', '') or _now().strftime('%Y-%m-%d')

        conn = _get_db()
        fa = conn.execute('SELECT name FROM accounts WHERE id=?', (from_id,)).fetchone()
        ta = conn.execute('SELECT name FROM accounts WHERE id=?', (to_id,)).fetchone()
        if not fa or not ta:
            conn.close()
            return jsonify({'success': False, 'error': '对象不存在'}), 404

        conn.execute(
            'INSERT INTO records (account_id, type, amount, category, date, note) VALUES (?, ?, ?, ?, ?, ?)',
            (from_id, '收到', amount, '转账', date_str, note or f'债务转至"{ta["name"]}"')
        )
        conn.execute(
            'INSERT INTO records (account_id, type, amount, category, date, note) VALUES (?, ?, ?, ?, ?, ?)',
            (to_id, '付给', amount, '转账', date_str, note or f'债务来自"{fa["name"]}"')
        )
        conn.commit()
        conn.close()
        _log(f'转账: {fa["name"]} -> {ta["name"]} {amount}元')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== PWA 静态文件 ====================

@bp.route('/accounting/manifest.json')
def accounting_manifest():
    return send_from_directory(DB_DIR, 'manifest.json')


@bp.route('/accounting/sw.js')
def accounting_sw():
    return send_from_directory(DB_DIR, 'sw.js')

# -*- coding: utf-8 -*-
"""专业记账会计平台 Blueprint（routes/ledger.py）

设计：标准复式记账，服务个体经营/小微企业。
- 科目五大类：资产(asset)/负债(liability)/权益(equity)/收入(income)/费用(expense)
- 余额方向：资产、费用为借方余额；负债、权益、收入为贷方余额
- 凭证：有借必有贷、借贷必相等；按月份自动编号 记-YYYYMM-XXXX
- 账簿：科目余额表/试算平衡表/明细账/总分类账（含自动期初、滚动余额）
- 报表：资产负债表（含未结转损益自动列示）、利润表（月/区间）
- 往来：应收账款/应付账款下级科目自动识别为往来对象，提供余额与账龄分析

与个人「记账」模块（routes/accounting.py）相互独立、互不影响。
"""
import os
from datetime import datetime, date, timedelta

from flask import Blueprint, jsonify, request, send_from_directory

from .utils import TZ, make_logger, make_db

bp = Blueprint('ledger', __name__)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ========== 静态目录 ==========
AC_DIR = os.path.join(BASE_DIR, '会计')
DB_FILE = os.path.join(AC_DIR, 'ledger.db')
_get_db = make_db(DB_FILE)

_log = make_logger(os.path.join(AC_DIR, 'ledger.log'))

# ========== 常量 ==========
CATEGORY_LABELS = {'asset': '资产', 'liability': '负债', 'equity': '权益',
                   'income': '收入', 'expense': '费用'}
DIRECTION_LABELS = {'debit': '借', 'credit': '贷'}
AR_CODE = '1122'   # 应收账款（客户往来）
AP_CODE = '2202'   # 应付账款（供应商往来）

# 预设科目表（仅当科目为空时导入）
PRESET_ACCOUNTS = [
    ('1001', '库存现金', 'asset', ''),
    ('1002', '银行存款', 'asset', ''),
    ('1012', '其他货币资金', 'asset', ''),
    ('1122', '应收账款', 'asset', ''),
    ('1123', '预付账款', 'asset', ''),
    ('1221', '其他应收款', 'asset', ''),
    ('1405', '库存商品', 'asset', ''),
    ('1601', '固定资产', 'asset', ''),
    ('1602', '累计折旧', 'asset', ''),
    ('2001', '短期借款', 'liability', ''),
    ('2202', '应付账款', 'liability', ''),
    ('2203', '预收账款', 'liability', ''),
    ('2211', '应付职工薪酬', 'liability', ''),
    ('2221', '应交税费', 'liability', ''),
    ('2241', '其他应付款', 'liability', ''),
    ('3001', '实收资本', 'equity', ''),
    ('3103', '本年利润', 'equity', ''),
    ('3104', '利润分配', 'equity', ''),
    ('5001', '主营业务收入', 'income', ''),
    ('5051', '其他业务收入', 'income', ''),
    ('6111', '投资收益', 'income', ''),
    ('6301', '营业外收入', 'income', ''),
    ('6401', '主营业务成本', 'expense', ''),
    ('6601', '销售费用', 'expense', ''),
    ('6602', '管理费用', 'expense', ''),
    ('6603', '财务费用', 'expense', ''),
    ('6711', '营业外支出', 'expense', ''),
]

_EPS = 0.009  # 平衡判断容差（分以下舍入误差）


# ========== 工具函数 ==========
def _round(v):
    try:
        return round(float(v), 2)
    except Exception:
        return 0.0


def _category_of(acct):
    return acct.get('category') if isinstance(acct, dict) else (acct[3] if acct else '')


def _is_debit_normal(category):
    return category in ('asset', 'expense')


def _load_accounts(conn, only_active=True):
    sql = 'SELECT * FROM accounts'
    if only_active:
        sql += ' WHERE active=1'
    rows = conn.execute(sql + ' ORDER BY code').fetchall()
    return [dict(r) for r in rows]


def _acct_map(conn):
    return {r['code']: r for r in _load_accounts(conn)}


def _validate_date(dstr, what='日期'):
    try:
        return datetime.strptime(dstr, '%Y-%m-%d').date()
    except Exception:
        raise ValueError(f'{what}格式应为 YYYY-MM-DD')


def _month_bounds(month):
    """校验 YYYY-MM，返回 (本月首日, 本月末日) date 对象"""
    try:
        y, m = month.split('-')
        y, m = int(y), int(m)
        if not (1 <= m <= 12):
            raise ValueError()
        start = date(y, m, 1)
        if m == 12:
            end = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(y, m + 1, 1) - timedelta(days=1)
        return start, end
    except Exception:
        raise ValueError('月份格式应为 YYYY-MM')


def _now():
    return datetime.now(TZ).date()


def _entry_sums(conn, code=None, before=None, start=None, end=None):
    """统计科目发生额合计：返回 (debit_total, credit_total)
    before/start/end 为 date 或 None，基于凭证日期 vdate"""
    sql = ('SELECT SUM(e.debit), SUM(e.credit) FROM voucher_entries e '
           'JOIN vouchers v ON v.id = e.voucher_id WHERE 1=1')
    params = []
    if code is not None:
        sql += ' AND e.account_code=?'
        params.append(code)
    if before is not None:
        sql += ' AND v.vdate<?'
        params.append(before.isoformat())
    if start is not None:
        sql += ' AND v.vdate>=?'
        params.append(start.isoformat())
    if end is not None:
        sql += ' AND v.vdate<=?'
        params.append(end.isoformat())
    row = conn.execute(sql, params).fetchone()
    return (_round(row[0] or 0), _round(row[1] or 0))


def _period_account_sums(conn, start, end):
    """期内按科目汇总发生额，返回 {code: [dr, cr]}"""
    rows = conn.execute(
        'SELECT e.account_code, SUM(e.debit), SUM(e.credit) '
        'FROM voucher_entries e JOIN vouchers v ON v.id=e.voucher_id '
        'WHERE v.vdate>=? AND v.vdate<=? GROUP BY e.account_code',
        (start.isoformat(), end.isoformat())).fetchall()
    result = {}
    for r in rows:
        result[r[0]] = [_round(r[1] or 0), _round(r[2] or 0)]
    return result


def _opening_of(account):
    """将科目的期初余额转为 (期初借方, 期初贷方)"""
    op = _round(account.get('opening') or 0)
    if op == 0:
        return 0.0, 0.0
    if _is_debit_normal(account['category']):
        return (op if op > 0 else 0.0), (abs(op) if op < 0 else 0.0)
    return (abs(op) if op < 0 else 0.0), (op if op > 0 else 0.0)


def _balance_side(dr, cr):
    """按净额返回 (方向, 金额)"""
    net = _round(dr - cr)
    if abs(net) <= _EPS:
        return '平', 0.0
    return ('借', abs(net)) if net > 0 else ('贷', abs(net))


def _ends_between(conn, account, start, end):
    """科目截至 end 的净余额（start 可 None = 期初起）。
    返回 (方向label, 金额)：方向 '借'/'贷'/'平'，金额恒为正"""
    op_dr, op_cr = _opening_of(account)
    pre_dr, pre_cr = (0.0, 0.0)
    if start is not None:
        pre_dr, pre_cr = _entry_sums(conn, code=account['code'], before=start)
    cur_dr, cur_cr = (0.0, 0.0)
    if end is not None:
        cur_dr, cur_cr = _entry_sums(conn, code=account['code'], start=start, end=end)
    dr = _round(op_dr + pre_dr + cur_dr)
    cr = _round(op_cr + pre_cr + cur_cr)
    side, amount = _balance_side(dr, cr)
    return side, amount


def _net_of(account, dr_total, cr_total):
    """科目(含期初)净余额，按正常方向取正。返回 (方向label, 数值)"""
    op_dr, op_cr = _opening_of(account)
    dr = _round(op_dr + dr_total)
    cr = _round(op_cr + cr_total)
    side, amount = _balance_side(dr, cr)
    return side, amount


def _contact_family(category):
    """往来根科目（按 code 前缀识别客户/供应商）"""
    if category == 'asset':
        return AR_CODE
    return AP_CODE


# ========== 页面路由 ==========
@bp.route('/ledger')
@bp.route('/ledger/')
def ledger_index():
    return send_from_directory(AC_DIR, 'index.html')


@bp.route('/ledger/manifest.json')
def ledger_manifest():
    return send_from_directory(AC_DIR, 'manifest.json')


@bp.route('/ledger/icon-192.svg')
def ledger_icon_192():
    return send_from_directory(AC_DIR, 'icon-192.svg')


@bp.route('/ledger/icon-512.svg')
def ledger_icon_512():
    return send_from_directory(AC_DIR, 'icon-512.svg')


# ========== 科目 API ==========
@bp.route('/api/ledger/accounts', methods=['GET'])
def list_accounts():
    try:
        conn = _get_db()
        rows = _load_accounts(conn)
        conn.close()
        # 附挂子科目数量与是否被凭证引用
        refs = {}
        cur = _get_db()
        for r in cur.execute('SELECT DISTINCT account_code FROM voucher_entries').fetchall():
            refs[r[0]] = True
        cur.close()
        data = []
        for r in rows:
            kids = [a for a in rows if a['parent_code'] == r['code']]
            r['has_children'] = bool(kids)
            r['in_use'] = r['code'] in refs
            r['category_label'] = CATEGORY_LABELS.get(r['category'], r['category'])
            data.append(r)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _validate_account(data, conn, exclude_id=None):
    code = str(data.get('code') or '').strip()
    name = str(data.get('name') or '').strip()
    category = str(data.get('category') or '')
    if not code or not name:
        raise ValueError('科目编码与名称不能为空')
    if not code.isdigit() or not (4 <= len(code) <= 8):
        raise ValueError('科目编码须为 4-8 位数字')
    if category not in CATEGORY_LABELS:
        raise ValueError('科目类别无效')
    parent_code = str(data.get('parent_code') or '').strip()
    opening = _round(data.get('opening') or 0)
    if parent_code:
        pr = conn.execute('SELECT * FROM accounts WHERE code=?',
                          (parent_code,)).fetchone()
        if not pr:
            raise ValueError('上级科目不存在')
        if dict(pr)['category'] != category:
            raise ValueError('下级科目类别必须与上级一致')
        if parent_code == code:
            raise ValueError('上级科目不能是自身')
    # 编码唯一
    dup = conn.execute('SELECT id FROM accounts WHERE code=? AND (id!=? OR ? IS NULL)',
                       (code, exclude_id or 0, exclude_id)).fetchone()
    if dup:
        raise ValueError(f'科目编码 {code} 已存在')
    return {'code': code, 'name': name, 'category': category,
            'parent_code': parent_code, 'opening': opening,
            'remark': str(data.get('remark') or '').strip()}


@bp.route('/api/ledger/accounts', methods=['POST'])
def create_account():
    try:
        data = request.get_json(silent=True) or {}
        conn = _get_db()
        a = _validate_account(data, conn)
        conn.execute(
            'INSERT INTO accounts (code, name, category, parent_code, opening, remark) '
            'VALUES (?,?,?,?,?,?)',
            (a['code'], a['name'], a['category'], a['parent_code'],
             a['opening'], a['remark']))
        conn.commit()
        conn.close()
        _log(f'新增科目 {a["code"]} {a["name"]}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/accounts/<acid>', methods=['PUT'])
def update_account(acid):
    try:
        data = request.get_json(silent=True) or {}
        conn = _get_db()
        old = conn.execute('SELECT * FROM accounts WHERE id=?', (acid,)).fetchone()
        if not old:
            raise ValueError('科目不存在')
        old = dict(old)
        a = _validate_account(data, conn, exclude_id=acid)
        # 防止改 code 破坏已有引用
        if a['code'] != old['code']:
            used = conn.execute(
                'SELECT 1 FROM voucher_entries WHERE account_code=?',
                (old['code'],)).fetchone()
            kids = conn.execute('SELECT 1 FROM accounts WHERE parent_code=?',
                                (old['code'],)).fetchone()
            if used or kids:
                raise ValueError('已有业务引用或下级科目，编码不可修改')
        # 若把上级科目挂到其它科目前检查子科目不会循环
        conn.execute(
            'UPDATE accounts SET code=?, name=?, category=?, parent_code=?, '
            'opening=?, remark=?, updated_at=datetime(\'now\',\'localtime\') WHERE id=?',
            (a['code'], a['name'], a['category'], a['parent_code'],
             a['opening'], a['remark'], acid))
        conn.commit()
        conn.close()
        _log(f'更新科目 {acid}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/accounts/<acid>', methods=['DELETE'])
def delete_account(acid):
    try:
        conn = _get_db()
        row = conn.execute('SELECT * FROM accounts WHERE id=?', (acid,)).fetchone()
        if not row:
            raise ValueError('科目不存在')
        row = dict(row)
        if conn.execute('SELECT 1 FROM accounts WHERE parent_code=?',
                        (row['code'],)).fetchone():
            raise ValueError('请先删除其下级科目')
        if conn.execute('SELECT 1 FROM voucher_entries WHERE account_code=?',
                        (row['code'],)).fetchone():
            raise ValueError('该科目已被凭证使用，不可删除（可将使用改到其它科目后重试）')
        if row['code'] in (AR_CODE, AP_CODE):
            raise ValueError('往来根科目为系统预设，不可删除')
        conn.execute('DELETE FROM accounts WHERE id=?', (acid,))
        conn.commit()
        conn.close()
        _log(f'删除科目 {row["code"]} {row["name"]}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ========== 凭证 API ==========
def _load_vouchers_list(conn, month):
    start, end = _month_bounds(month)
    rows = conn.execute(
        'SELECT v.id, v.voucher_no, v.vdate, v.summary, '
        '(SELECT COALESCE(SUM(e.debit),0) FROM voucher_entries e WHERE e.voucher_id=v.id) AS total '
        'FROM vouchers v WHERE v.vdate>=? AND v.vdate<=? '
        'ORDER BY v.vdate, v.id DESC',
        (start.isoformat(), end.isoformat())).fetchall()
    out = []
    for r in rows:
        out.append({'id': r['id'], 'voucher_no': r['voucher_no'], 'vdate': r['vdate'],
                    'summary': r['summary'], 'total': _round(r['total'])})
    return out


def _validate_entries(conn, raw_entries):
    """校验并清洗分录列表，返回 [{account_code,summary,debit,credit},...]"""
    accts = _acct_map(conn)
    if not isinstance(raw_entries, list) or len(raw_entries) < 2:
        raise ValueError('凭证至少需要 2 条分录（一借一贷）')
    cleaned = []
    total_dr = 0.0
    total_cr = 0.0
    for idx, e in enumerate(raw_entries):
        code = str(e.get('account_code') or '').strip()
        if code not in accts:
            raise ValueError(f'第{idx + 1}条分录科目不存在: {code or "空"}')
        dr = _round(e.get('debit') or 0)
        cr = _round(e.get('credit') or 0)
        if dr < 0 or cr < 0:
            raise ValueError('金额不能为负数')
        if dr > 0 and cr > 0:
            raise ValueError(f'第{idx + 1}条分录借贷不能同时填写')
        if dr == 0 and cr == 0:
            raise ValueError(f'第{idx + 1}条分录金额不能为 0')
        cleaned.append({'account_code': code,
                        'summary': str(e.get('summary') or '').strip(),
                        'debit': dr, 'credit': cr})
        total_dr += dr
        total_cr += cr
    total_dr, total_cr = _round(total_dr), _round(total_cr)
    if abs(total_dr - total_cr) > _EPS:
        raise ValueError(f'借贷不平衡：借方 {total_dr:.2f} ≠ 贷方 {total_cr:.2f}')
    return cleaned, total_dr


def _next_voucher_no(conn, ym):
    """生成凭证号 记-YYYYMM-XXXX"""
    base = f'记-{ym}-'
    rows = conn.execute('SELECT voucher_no FROM vouchers WHERE voucher_no LIKE ?',
                        (base + '%',)).fetchall()
    used = {r[0] for r in rows}
    seq = 1
    while f'{base}{seq:04d}' in used:
        seq += 1
    return f'{base}{seq:04d}'


@bp.route('/api/ledger/vouchers', methods=['GET'])
def list_vouchers():
    try:
        month = request.args.get('month', '')
        conn = _get_db()
        data = _load_vouchers_list(conn, month)
        conn.close()
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/vouchers/<vid>', methods=['GET'])
def get_voucher(vid):
    try:
        conn = _get_db()
        row = conn.execute('SELECT * FROM vouchers WHERE id=?', (vid,)).fetchone()
        if not row:
            raise ValueError('凭证不存在')
        v = dict(row)
        accts = _acct_map(conn)
        entries = []
        for e in conn.execute(
                'SELECT * FROM voucher_entries WHERE voucher_id=? ORDER BY id',
                (vid,)).fetchall():
            e = dict(e)
            acct = accts.get(e['account_code'], {})
            e['account_name'] = acct.get('name', e['account_code'])
            e['category'] = acct.get('category', '')
            e['category_label'] = CATEGORY_LABELS.get(acct.get('category', ''), '')
            e['debit'] = _round(e['debit'])
            e['credit'] = _round(e['credit'])
            entries.append(e)
        conn.close()
        return jsonify({'success': True, 'data': v, 'entries': entries})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/vouchers', methods=['POST'])
def create_voucher():
    try:
        data = request.get_json(silent=True) or {}
        vdate = _validate_date(str(data.get('vdate') or ''), '凭证日期')
        summary = str(data.get('summary') or '').strip()
        entries, total = _validate_entries(_get_db(), data.get('entries'))
        conn = _get_db()
        ym = vdate.strftime('%Y%m')
        no = _next_voucher_no(conn, ym)
        cur = conn.execute(
            'INSERT INTO vouchers (voucher_no, vdate, summary) VALUES (?,?,?)',
            (no, vdate.isoformat(), summary))
        vid = cur.lastrowid
        for e in entries:
            conn.execute(
                'INSERT INTO voucher_entries (voucher_id, account_code, summary, debit, credit) '
                'VALUES (?,?,?,?,?)',
                (vid, e['account_code'], e['summary'], e['debit'], e['credit']))
        conn.commit()
        conn.close()
        _log(f'新增凭证 {no} 合计 {total:.2f}')
        return jsonify({'success': True, 'id': vid, 'voucher_no': no})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/vouchers/<vid>', methods=['PUT'])
def update_voucher(vid):
    try:
        data = request.get_json(silent=True) or {}
        vdate = _validate_date(str(data.get('vdate') or ''), '凭证日期')
        summary = str(data.get('summary') or '').strip()
        conn = _get_db()
        old = conn.execute('SELECT * FROM vouchers WHERE id=?', (vid,)).fetchone()
        if not old:
            raise ValueError('凭证不存在')
        entries, total = _validate_entries(conn, data.get('entries'))
        ym = vdate.strftime('%Y%m')
        no = _next_voucher_no(conn, ym)
        conn.execute(
            'UPDATE vouchers SET voucher_no=?, vdate=?, summary=?, '
            'updated_at=datetime(\'now\',\'localtime\') WHERE id=?',
            (no, vdate.isoformat(), summary, vid))
        conn.execute('DELETE FROM voucher_entries WHERE voucher_id=?', (vid,))
        for e in entries:
            conn.execute(
                'INSERT INTO voucher_entries (voucher_id, account_code, summary, debit, credit) '
                'VALUES (?,?,?,?,?)',
                (vid, e['account_code'], e['summary'], e['debit'], e['credit']))
        conn.commit()
        conn.close()
        _log(f'更新凭证 {vid} 合计 {total:.2f}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/vouchers/<vid>', methods=['DELETE'])
def delete_voucher(vid):
    try:
        conn = _get_db()
        row = conn.execute('SELECT voucher_no FROM vouchers WHERE id=?', (vid,)).fetchone()
        if not row:
            raise ValueError('凭证不存在')
        conn.execute('DELETE FROM voucher_entries WHERE voucher_id=?', (vid,))
        conn.execute('DELETE FROM vouchers WHERE id=?', (vid,))
        conn.commit()
        conn.close()
        _log(f'删除凭证 {row["voucher_no"]}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ========== 账簿 API ==========
@bp.route('/api/ledger/ledger', methods=['GET'])
def account_ledger():
    """明细账/总账：某科目在区间内的逐笔记录 + 滚动余额"""
    try:
        code = str(request.args.get('account_code') or '').strip()
        from_d = request.args.get('from', '')
        to_d = request.args.get('to', '')
        conn = _get_db()
        accts = _acct_map(conn)
        if code not in accts:
            raise ValueError('科目不存在')
        acct = accts[code]
        from_date = _validate_date(from_d, '开始日期') if from_d else date(1900, 1, 1)
        to_date = _validate_date(to_d, '结束日期') if to_d else _now()
        if from_date > to_date:
            raise ValueError('开始日期不能晚于结束日期')
        # 期初：科目期初 + 区间前发生额（按净额给出 借/贷）
        op_dr, op_cr = _opening_of(acct)
        pre_dr, pre_cr = _entry_sums(conn, code=code, before=from_date)
        beg_dr, beg_cr = _round(op_dr + pre_dr), _round(op_cr + pre_cr)
        side, amount = _balance_side(beg_dr, beg_cr)
        opening = {'side': side, 'amount': amount}
        rows = conn.execute(
            'SELECT v.vdate, v.voucher_no, v.summary AS vsum, '
            'e.id AS eid, e.summary AS esum, e.debit, e.credit '
            'FROM voucher_entries e JOIN vouchers v ON v.id=e.voucher_id '
            'WHERE e.account_code=? AND v.vdate>=? AND v.vdate<=? '
            'ORDER BY v.vdate, v.id, e.id',
            (code, from_date.isoformat(), to_date.isoformat())).fetchall()
        items = []
        dr_run, cr_run = beg_dr, beg_cr
        for r in rows:
            dr = _round(r['debit'])
            cr = _round(r['credit'])
            dr_run, cr_run = _round(dr_run + dr), _round(cr_run + cr)
            bal_side, bal_amt = _balance_side(dr_run, cr_run)
            items.append({
                'date': r['vdate'], 'voucher_no': r['voucher_no'],
                'summary': r['esum'] or r['vsum'],
                'debit': dr, 'credit': cr,
                'bal_side': bal_side, 'balance': bal_amt,
            })
        conn.close()
        return jsonify({
            'success': True,
            'account': {'code': acct['code'], 'name': acct['name'],
                        'category': acct['category'],
                        'category_label': CATEGORY_LABELS.get(acct['category'], ''),
                        'direction': DIRECTION_LABELS['debit' if _is_debit_normal(acct['category']) else 'credit']},
            'from': from_date.isoformat(), 'to': to_date.isoformat(),
            'opening': opening,
            'items': items,
            'totals': {'debit': _round(sum(i['debit'] for i in items)),
                       'credit': _round(sum(i['credit'] for i in items))},
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/trial', methods=['GET'])
def trial_balance():
    """试算平衡表（某月）"""
    try:
        month = request.args.get('month', '')
        start, end = _month_bounds(month)
        conn = _get_db()
        accounts = _load_accounts(conn, only_active=False)
        in_period = _period_account_sums(conn, start, end)
        rows = []
        t_op_dr = t_op_cr = t_cur_dr = t_cur_cr = t_end_dr = t_end_cr = 0.0
        for a in accounts:
            op_dr, op_cr = _opening_of(a)
            pre_dr, pre_cr = _entry_sums(conn, code=a['code'], before=start)
            op_dr, op_cr = _round(op_dr + pre_dr), _round(op_cr + pre_cr)
            s = in_period.get(a['code'], [0, 0])
            cur_dr, cur_cr = s[0], s[1]
            op_side, op_amt = _balance_side(op_dr, op_cr)
            cur_side, cur_amt = _balance_side(cur_dr, cur_cr)
            end_dr, end_cr = _round(op_dr + cur_dr), _round(op_cr + cur_cr)
            end_side, end_amt = _balance_side(end_dr, end_cr)
            op_dr_disp = op_amt if op_side == '借' else 0.0
            op_cr_disp = op_amt if op_side == '贷' else 0.0
            cur_dr_disp = cur_amt if cur_side == '借' else 0.0
            cur_cr_disp = cur_amt if cur_side == '贷' else 0.0
            end_dr_disp = end_amt if end_side == '借' else 0.0
            end_cr_disp = end_amt if end_side == '贷' else 0.0
            t_op_dr += op_dr_disp
            t_op_cr += op_cr_disp
            t_cur_dr += cur_dr_disp
            t_cur_cr += cur_cr_disp
            t_end_dr += end_dr_disp
            t_end_cr += end_cr_disp
            if abs(op_amt) > _EPS or abs(cur_amt) > _EPS or abs(end_amt) > _EPS:
                rows.append({
                    'code': a['code'], 'name': a['name'],
                    'category': a['category'],
                    'category_label': CATEGORY_LABELS.get(a['category'], ''),
                    'op_dr': _round(op_dr_disp), 'op_cr': _round(op_cr_disp),
                    'cur_dr': _round(cur_dr_disp), 'cur_cr': _round(cur_cr_disp),
                    'end_dr': _round(end_dr_disp), 'end_cr': _round(end_cr_disp),
                })
        conn.close()
        balanced = (abs(t_op_dr - t_op_cr) <= _EPS and
                    abs(t_cur_dr - t_cur_cr) <= _EPS and
                    abs(t_end_dr - t_end_cr) <= _EPS)
        return jsonify({
            'success': True, 'month': month,
            'rows': rows,
            'totals': {'op_dr': _round(t_op_dr), 'op_cr': _round(t_op_cr),
                       'cur_dr': _round(t_cur_dr), 'cur_cr': _round(t_cur_cr),
                       'end_dr': _round(t_end_dr), 'end_cr': _round(t_end_cr)},
            'balanced': balanced,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


def _ends_by_category(conn, start, end):
    """报表底账：月末(截至 end) 各科目净额。返回 accounts + 汇总
    start 用于过滤区间发生（报表按月度累计口径不直接用），保留未来扩展"""
    accounts = _load_accounts(conn, only_active=False)
    out = []
    for a in accounts:
        dr_total, cr_total = 0.0, 0.0
        # 所有时点截至 end 的发生额
        pre_dr, pre_cr = _entry_sums(conn, code=a['code'], before=None,
                                     start=date(1900, 1, 1), end=end)
        dr_total, cr_total = _round(pre_dr), _round(pre_cr)
        op_dr, op_cr = _opening_of(a)
        dr_total = _round(op_dr + dr_total)
        cr_total = _round(op_cr + cr_total)
        side, amount = _balance_side(dr_total, cr_total)
        out.append({'account': a, 'side': side, 'amount': amount,
                    'dr_total': dr_total, 'cr_total': cr_total})
    return out


@bp.route('/api/ledger/balance', methods=['GET'])
def balance_sheet():
    """资产负债表（截至某月末）"""
    try:
        month = request.args.get('month', '')
        start, end = _month_bounds(month)
        conn = _get_db()
        entries = _ends_by_category(conn, start, end)
        asset_rows, liab_rows, eq_rows = [], [], []
        asset_total = liab_total = eq_total = 0.0
        # 收入/费用未结转损益（截至月末，利润为正记贷）
        profit_ytd = 0.0
        for it in entries:
            a, side, amount = it['account'], it['side'], it['amount']
            cat = a['category']
            if abs(amount) <= _EPS:
                continue
            if cat == 'asset':
                # 借方正常科目为正；备抵（贷方）以负数列示
                net = amount if side == '借' else -amount
                asset_total = _round(asset_total + net)
                asset_rows.append({'code': a['code'], 'name': a['name'], 'amount': net})
            elif cat == 'liability':
                net = amount if side == '贷' else -amount
                liab_total = _round(liab_total + net)
                liab_rows.append({'code': a['code'], 'name': a['name'], 'amount': net})
            elif cat == 'equity':
                net = amount if side == '贷' else -amount
                eq_total = _round(eq_total + net)
                eq_rows.append({'code': a['code'], 'name': a['name'], 'amount': net})
            elif cat in ('income', 'expense'):
                net = amount if side == '贷' else -amount
                profit_ytd = _round(profit_ytd + net)
        if abs(profit_ytd) > _EPS:
            eq_rows.append({'code': '(未结转)', 'name': '本年利润(未结转损益)',
                            'amount': profit_ytd})
            eq_total = _round(eq_total + profit_ytd)
        conn.close()
        eq_and_liab = _round(liab_total + eq_total)
        diff = _round(asset_total - eq_and_liab)
        return jsonify({
            'success': True, 'month': month,
            'as_of': end.isoformat(),
            'asset': {'rows': asset_rows, 'total': asset_total},
            'liability': {'rows': liab_rows, 'total': liab_total},
            'equity': {'rows': eq_rows, 'total': eq_total},
            'balanced': abs(diff) <= _EPS,
            'diff': diff,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/income', methods=['GET'])
def income_statement():
    """利润表：按月度或自定义区间"""
    try:
        month = request.args.get('month', '')
        frm = request.args.get('from', '')
        to = request.args.get('to', '')
        if month:
            start, end = _month_bounds(month)
        else:
            start = _validate_date(frm, '开始日期') if frm else date(1900, 1, 1)
            end = _validate_date(to, '结束日期') if to else _now()
        if start > end:
            raise ValueError('开始日期不能晚于结束日期')
        conn = _get_db()
        accts = _acct_map(conn)
        in_period = _period_account_sums(conn, start, end)
        income_rows, expense_rows = [], []
        income_total = expense_total = 0.0
        for code, s in sorted(in_period.items()):
            a = accts.get(code)
            if not a or a['category'] not in ('income', 'expense'):
                continue
            dr, cr = s[0], s[1]
            net = _round(cr - dr)
            if abs(net) <= _EPS:
                continue
            if a['category'] == 'income':
                income_total = _round(income_total + net)
                income_rows.append({'code': code, 'name': a['name'], 'amount': net})
            else:
                expense_total = _round(expense_total - net)  # net 负数
                expense_rows.append({'code': code, 'name': a['name'],
                                     'amount': _round(-net)})
        conn.close()
        profit = _round(income_total - expense_total)
        return jsonify({
            'success': True, 'month': month or 'custom',
            'from': start.isoformat(), 'to': end.isoformat(),
            'income': {'rows': income_rows, 'total': income_total},
            'expense': {'rows': expense_rows, 'total': expense_total},
            'profit': profit,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ========== 往来 API ==========
@bp.route('/api/ledger/contacts', methods=['GET'])
def contacts():
    """应收/应付往来：科目余额 + 简化账龄"""
    try:
        conn = _get_db()
        accts = _acct_map(conn)
        today = _now()
        sides = []
        for cat, root_code, label in (('asset', AR_CODE, '应收'),
                                      ('liability', AP_CODE, '应付')):
            kids = [a for a in accts.values()
                    if a['parent_code'] == root_code and a['category'] == cat]
            detail = []
            for k in sorted(kids, key=lambda x: x['code']):
                end_side, end_amt = _ends_between(conn, k, None, today)
                # 应收回款中可能有贷差（红字/多收）→ 归到反方向另示
                items = []
                run = 0.0
                for r in conn.execute(
                        'SELECT v.vdate, e.debit, e.credit FROM voucher_entries e '
                        'JOIN vouchers v ON v.id=e.voucher_id '
                        'WHERE e.account_code=? ORDER BY v.vdate, v.id',
                        (k['code'],)).fetchall():
                    run = _round(run + r['debit'] - r['credit'])
                    items.append({'date': r['vdate'], 'run': run})
                # 简化账龄：把期末余额按“最近的发生顺序”分摊到时间桶
                buckets = {'0-30': 0.0, '31-60': 0.0, '61-90': 0.0, '90+': 0.0}
                remain = end_amt
                rev_entries = []
                for r in conn.execute(
                        'SELECT v.vdate, e.debit FROM voucher_entries e '
                        'JOIN vouchers v ON v.id=e.voucher_id '
                        'WHERE e.account_code=? AND e.debit>0 ORDER BY v.vdate DESC, v.id DESC',
                        (k['code'],)).fetchall():
                    rev_entries.append((r['vdate'], r['debit']))
                for vd, amt in rev_entries:
                    if remain <= _EPS:
                        break
                    take = min(remain, amt)
                    days = (today - datetime.strptime(vd, '%Y-%m-%d').date()).days
                    key = '90+' if days > 90 else ('61-90' if days > 60 else
                                                   ('31-60' if days > 30 else '0-30'))
                    buckets[key] = _round(buckets[key] + take)
                    remain = _round(remain - take)
                last_date = items[-1]['date'] if items else ''
                detail.append({
                    'code': k['code'], 'name': k['name'],
                    'balance': end_amt,
                    'side': '借方余额' if end_side == '借' else ('贷方余额' if end_side == '贷' else '平'),
                    'last_date': last_date,
                    'buckets': buckets,
                })
            total = _round(sum(d['balance'] for d in detail))
            sides.append({'label': label, 'code': root_code,
                          'accounts': detail, 'total': total})
        conn.close()
        return jsonify({'success': True, 'sides': sides, 'today': today.isoformat()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ========== 概览/仪表盘 ==========
def _month_income_expense(conn, month):
    """单月收入/费用发生净额（按类别合计）"""
    start, end = _month_bounds(month)
    accts = _acct_map(conn)
    in_period = _period_account_sums(conn, start, end)
    income = 0.0
    expense = 0.0
    income_rows = []
    expense_rows = []
    for code, s in sorted(in_period.items()):
        a = accts.get(code)
        if not a:
            continue
        net = _round(s[1] - s[0])
        if abs(net) <= _EPS:
            continue
        if a['category'] == 'income':
            income = _round(income + net)
            income_rows.append({'code': code, 'name': a['name'], 'amount': net})
        elif a['category'] == 'expense':
            expense = _round(expense - net)
            expense_rows.append({'code': code, 'name': a['name'], 'amount': _round(-net)})
    expense_rows.sort(key=lambda x: x['amount'], reverse=True)
    return {'income': income, 'expense': expense,
            'net': _round(income - expense),
            'income_rows': income_rows[:5], 'expense_rows': expense_rows[:5]}


@bp.route('/api/ledger/dashboard', methods=['GET'])
def dashboard():
    try:
        month = request.args.get('month', '')
        if not month:
            month = _now().strftime('%Y-%m')
        m = _month_income_expense(_get_db(), month)
        conn = _get_db()
        accts = _acct_map(conn)
        # 资金类科目（现金/银行/其他货币资金及下级）
        money_codes = [a['code'] for a in accts.values()
                       if a['code'].startswith(('1001', '1002', '1012'))]
        cash_rows = []
        cash_total = 0.0
        today = _now()
        for code in sorted(money_codes):
            a = accts[code]
            side, amt = _ends_between(conn, a, None, today)
            if amt > _EPS:
                cash_total = _round(cash_total + amt)
                cash_rows.append({'code': code, 'name': a['name'],
                                  'amount': amt if side == '借' else -amt})
        # 往来净额（应收正=别人欠我，应付正=我欠别人）
        ar_net = ap_net = 0.0
        for a in accts.values():
            if a['parent_code'] != AR_CODE:
                continue
            side, amt = _ends_between(conn, a, None, today)
            net = amt if side == '借' else (-amt if side == '贷' else 0.0)
            ar_net = _round(ar_net + net)
        for a in accts.values():
            if a['parent_code'] != AP_CODE:
                continue
            side, amt = _ends_between(conn, a, None, today)
            net = amt if side == '贷' else (-amt if side == '借' else 0.0)
            ap_net = _round(ap_net + net)
        # 近 6 个月趋势
        trend = []
        cur = date(int(month[:4]), int(month[5:7]), 1)
        for _i in range(6):
            y, mo = cur.year, cur.month
            key = f'{y:04d}-{mo:02d}'
            mm = _month_income_expense(conn, key)
            trend.append({'month': key, 'income': mm['income'],
                          'expense': mm['expense'], 'net': mm['net']})
            if mo == 1:
                cur = date(y - 1, 12, 1)
            else:
                cur = date(y, mo - 1, 1)
        trend.reverse()
        # 最近凭证
        recent = conn.execute(
            'SELECT id, voucher_no, vdate, summary FROM vouchers '
            'ORDER BY vdate DESC, id DESC LIMIT 8').fetchall()
        recent = [dict(r) for r in recent]
        # 科目统计
        total_accts = len(accts)
        used_accts = len({r['account_code'] for r in conn.execute(
            'SELECT DISTINCT account_code FROM voucher_entries').fetchall()})
        v_count = conn.execute('SELECT COUNT(*) FROM vouchers').fetchone()[0]
        conn.close()
        return jsonify({
            'success': True, 'month': month,
            'monthly': m, 'cash': {'rows': cash_rows, 'total': cash_total},
            'ar_net': ar_net, 'ap_net': ap_net,
            'trend': trend, 'recent': recent,
            'stats': {'accounts': total_accts, 'used_accounts': used_accts,
                      'vouchers': v_count},
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ========== 初始化 ==========
def _init_db_impl():
    os.makedirs(AC_DIR, exist_ok=True)
    conn = _get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        parent_code TEXT DEFAULT '',
        opening REAL DEFAULT 0,
        remark TEXT DEFAULT '',
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS vouchers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voucher_no TEXT,
        vdate TEXT NOT NULL,
        summary TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS voucher_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voucher_id INTEGER NOT NULL,
        account_code TEXT NOT NULL,
        summary TEXT DEFAULT '',
        debit REAL DEFAULT 0,
        credit REAL DEFAULT 0
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_entries_vid ON voucher_entries(voucher_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_entries_code ON voucher_entries(account_code)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_vouchers_date ON vouchers(vdate)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_vouchers_no ON vouchers(voucher_no)')
    # 空库时导入预设科目表
    cnt = conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
    if cnt == 0:
        for code, name, category, parent in PRESET_ACCOUNTS:
            conn.execute(
                'INSERT INTO accounts (code, name, category, parent_code) '
                'VALUES (?,?,?,?)', (code, name, category, parent))
        _log('导入预设会计科目表')
    conn.execute('PRAGMA user_version = 1')
    conn.commit()
    conn.close()


def init_db():
    """初始化数据库（app.py 启动时调用，须有异常兜底）"""
    try:
        _init_db_impl()
        _log('数据库就绪')
    except Exception as e:
        print(f'[ledger init_db ERROR] {e}')

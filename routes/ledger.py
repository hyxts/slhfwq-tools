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
import os, json
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
    """统计科目发生额合计（不含作废凭证）：返回 (debit_total, credit_total)
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
    sql += " AND v.status<>'voided'"
    row = conn.execute(sql, params).fetchone()
    return (_round(row[0] or 0), _round(row[1] or 0))


def _period_account_sums(conn, start, end):
    """期内按科目汇总发生额（不含作废凭证），返回 {code: [dr, cr]}"""
    rows = conn.execute(
        'SELECT e.account_code, SUM(e.debit), SUM(e.credit) '
        'FROM voucher_entries e JOIN vouchers v ON v.id=e.voucher_id '
        'WHERE v.vdate>=? AND v.vdate<=? AND v.status<>\'voided\' GROUP BY e.account_code',
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


# ========== 期间 / 审核 / 辅助核算 工具 ==========
def _now_str():
    return datetime.now(TZ).strftime('%Y-%m-%d')


def _period_closed(conn, ym):
    """ym 形如 YYYY-MM，期间是否已结账"""
    return bool(conn.execute('SELECT 1 FROM closings WHERE month=?', (ym,)).fetchone())


def _vdate_ym(vdate):
    """date -> 'YYYY-MM'"""
    return vdate.strftime('%Y-%m')


def _active_sql():
    """排除作废凭证的 JOIN 片段"""
    return ' AND v.status<>\'voided\''


def _validate_aux_map(conn, raw_aux, accts=None):
    """校验分录行辅助核算。raw_aux: [{dim_id,item_id}] 或 {dim_id_str: item_id}，
    返回 {dim_id: aux_item_id}。dim 与 item 均须存在且 item 属于该 dim。"""
    out = {}
    if not raw_aux:
        return out
    if isinstance(raw_aux, dict):
        pairs = []
        for k, v in raw_aux.items():
            if k in ('', 'undefined', 'null'):
                continue
            pairs.append((k, v))
    elif isinstance(raw_aux, list):
        pairs = [(str(x.get('dim_id') or ''), x.get('item_id')) for x in raw_aux if isinstance(x, dict)]
    else:
        return out
    for dim_id, item_id in pairs:
        if not dim_id or not item_id:
            continue
        dim_id, item_id = str(dim_id), str(item_id)
        row = conn.execute('SELECT 1 FROM aux_dims WHERE id=?', (dim_id,)).fetchone()
        if not row:
            raise ValueError(f'辅助核算维度不存在: {dim_id}')
        it = conn.execute('SELECT 1 FROM aux_items WHERE id=? AND dim_id=? AND active=1',
                          (item_id, dim_id)).fetchone()
        if not it:
            raise ValueError(f'辅助项不存在或已停用: {item_id}')
        out[int(dim_id)] = int(item_id)
    return out


def _load_aux_map(conn):
    """全部辅助核算维度+启用项：{dim_id: {'name':..,'items':{item_id:name}}}"""
    dims = conn.execute('SELECT id, name FROM aux_dims ORDER BY id').fetchall()
    out = {}
    for d in dims:
        items = conn.execute(
            'SELECT id, name FROM aux_items WHERE dim_id=? AND active=1 ORDER BY name',
            (d['id'],)).fetchall()
        out[d['id']] = {'name': d['name'],
                        'items': {i['id']: i['name'] for i in items}}
    return out


def _entry_aux_list(conn, entry_id):
    """某分录行挂载的辅助项：[{dim_id,dim_name,item_id,item_name}]"""
    rows = conn.execute(
        'SELECT ea.aux_item_id, ai.dim_id, ai.name AS iname, ad.name AS dname '
        'FROM voucher_entry_aux ea JOIN aux_items ai ON ai.id=ea.aux_item_id '
        'JOIN aux_dims ad ON ad.id=ai.dim_id WHERE ea.entry_id=?',
        (entry_id,)).fetchall()
    return [{'dim_id': r['dim_id'], 'dim_name': r['dname'],
             'item_id': r['aux_item_id'], 'item_name': r['iname']} for r in rows]


def _replace_entry_aux(conn, entry_id, aux_map):
    """覆盖写入某分录行辅助项"""
    conn.execute('DELETE FROM voucher_entry_aux WHERE entry_id=?', (entry_id,))
    for _d, item_id in aux_map.items():
        conn.execute('INSERT OR IGNORE INTO voucher_entry_aux (entry_id, aux_item_id) '
                     'VALUES (?,?)', (entry_id, item_id))


def _save_entries(conn, vid, cleaned):
    """写入凭证分录（含辅助核算），返回 id 列表"""
    ids = []
    for e in cleaned:
        cur = conn.execute(
            'INSERT INTO voucher_entries (voucher_id, account_code, summary, debit, credit) '
            'VALUES (?,?,?,?,?)',
            (vid, e['account_code'], e['summary'], e['debit'], e['credit']))
        ids.append(cur.lastrowid)
        _replace_entry_aux(conn, cur.lastrowid, e.get('aux') or {})
    return ids


def _template_of(conn, content):
    """校验模板 content JSON，返回行列表 {summary,account_code,debit,credit}"""
    if isinstance(content, str):
        content = json.loads(content)
    accts = _acct_map(conn)
    lines = []
    for i, x in enumerate(content or []):
        code = str(x.get('account_code') or '').strip()
        if code not in accts:
            raise ValueError(f'模板第{i + 1}行科目不存在: {code}')
        dr = _round(x.get('debit') or 0)
        cr = _round(x.get('credit') or 0)
        if dr < 0 or cr < 0:
            raise ValueError('模板金额不能为负')
        lines.append({'summary': str(x.get('summary') or '').strip(),
                      'account_code': code, 'debit': dr, 'credit': cr})
    if not lines:
        raise ValueError('模板分录为空')
    return lines


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
    # 辅助核算维度：须存在
    aux_dims = data.get('aux_dims') or []
    if isinstance(aux_dims, str):
        aux_dims = [x for x in aux_dims.replace('，', ',').split(',') if x]
    dim_ids = []
    for did in aux_dims:
        did = str(did).strip()
        if not did:
            continue
        if not conn.execute('SELECT 1 FROM aux_dims WHERE id=?', (did,)).fetchone():
            raise ValueError(f'辅助核算维度不存在: {did}')
        if did not in dim_ids:
            dim_ids.append(did)
    return {'code': code, 'name': name, 'category': category,
            'parent_code': parent_code, 'opening': opening,
            'remark': str(data.get('remark') or '').strip(),
            'aux_dims': ','.join(dim_ids)}


@bp.route('/api/ledger/accounts', methods=['POST'])
def create_account():
    try:
        data = request.get_json(silent=True) or {}
        conn = _get_db()
        a = _validate_account(data, conn)
        conn.execute(
            'INSERT INTO accounts (code, name, category, parent_code, opening, remark, aux_dims) '
            'VALUES (?,?,?,?,?,?,?)',
            (a['code'], a['name'], a['category'], a['parent_code'],
             a['opening'], a['remark'], a['aux_dims']))
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
            'opening=?, remark=?, aux_dims=?, updated_at=datetime(\'now\',\'localtime\') WHERE id=?',
            (a['code'], a['name'], a['category'], a['parent_code'],
             a['opening'], a['remark'], a['aux_dims'], acid))
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
        if conn.execute('SELECT 1 FROM aux_openings WHERE account_code=?',
                        (row['code'],)).fetchone():
            raise ValueError('该科目存在辅助期初余额，不可删除（请先清空辅助期初）')
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
        'SELECT v.id, v.voucher_no, v.vdate, v.summary, v.status, v.red, v.auto, '
        'v.source_vid, v.attachments, '
        '(SELECT COALESCE(SUM(e.debit),0) FROM voucher_entries e WHERE e.voucher_id=v.id) AS total '
        'FROM vouchers v WHERE v.vdate>=? AND v.vdate<=? '
        'ORDER BY v.vdate, v.id',
        (start.isoformat(), end.isoformat())).fetchall()
    out = []
    for r in rows:
        out.append({'id': r['id'], 'voucher_no': r['voucher_no'], 'vdate': r['vdate'],
                    'summary': r['summary'], 'status': r['status'],
                    'red': bool(r['red']), 'auto': bool(r['auto']),
                    'source_vid': r['source_vid'],
                    'attachments': r['attachments'] or 0,
                    'total': _round(r['total'])})
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
        aux = _validate_aux_map(conn, e.get('aux') or {})
        if aux and accts[code].get('aux_dims'):
            bound = {str(x) for x in str(accts[code]['aux_dims'] or '').split(',') if x}
            for dim_id in aux:
                if str(dim_id) not in bound:
                    raise ValueError(f'科目 {accts[code]["name"]} 未启用该辅助核算维度')
        cleaned.append({'account_code': code,
                        'summary': str(e.get('summary') or '').strip(),
                        'debit': dr, 'credit': cr, 'aux': aux})
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
            e['aux'] = _entry_aux_list(conn, e['id'])
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
        conn = _get_db()
        ym = _vdate_ym(vdate)
        if _period_closed(conn, ym):
            raise ValueError(f'{ym} 期间已结账，不能新增凭证')
        entries, total = _validate_entries(conn, data.get('entries'))
        no = _next_voucher_no(conn, vdate.strftime('%Y%m'))
        attachments = int(data.get('attachments') or 0)
        cur = conn.execute(
            'INSERT INTO vouchers (voucher_no, vdate, summary, attachments) VALUES (?,?,?,?)',
            (no, vdate.isoformat(), summary, attachments))
        vid = cur.lastrowid
        _save_entries(conn, vid, entries)
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
        old = dict(old)
        ym_new = _vdate_ym(vdate)
        ym_old = _vdate_ym(_validate_date(old['vdate'], '凭证日期'))
        if _period_closed(conn, ym_new) or _period_closed(conn, ym_old):
            raise ValueError('凭证所在期间已结账，不能修改（可反结账后重试）')
        if old['status'] == 'voided':
            raise ValueError('作废凭证不可修改')
        entries, total = _validate_entries(conn, data.get('entries'))
        no = _next_voucher_no(conn, vdate.strftime('%Y%m'))
        attachments = int(data.get('attachments') or old.get('attachments') or 0)
        conn.execute(
            'UPDATE vouchers SET voucher_no=?, vdate=?, summary=?, attachments=?, '
            'updated_at=datetime(\'now\',\'localtime\') WHERE id=?',
            (no, vdate.isoformat(), summary, attachments, vid))
        # 删除旧分录（连带清理辅助关联）
        conn.execute('DELETE FROM voucher_entry_aux WHERE entry_id IN '
                     '(SELECT id FROM voucher_entries WHERE voucher_id=?)', (vid,))
        conn.execute('DELETE FROM voucher_entries WHERE voucher_id=?', (vid,))
        _save_entries(conn, vid, entries)
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
        row = conn.execute('SELECT * FROM vouchers WHERE id=?', (vid,)).fetchone()
        if not row:
            raise ValueError('凭证不存在')
        row = dict(row)
        if _period_closed(conn, _vdate_ym(_validate_date(row['vdate'], '凭证日期'))):
            raise ValueError('凭证所在期间已结账，不能删除（可反结账后重试）')
        if row['auto'] and row['source_vid']:
            # 删除红字/结转凭证不影响其它逻辑；若为自动结转将记录其关联
            pass
        conn.execute('DELETE FROM voucher_entry_aux WHERE entry_id IN '
                     '(SELECT id FROM voucher_entries WHERE voucher_id=?)', (vid,))
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
            'AND v.status<>\'voided\' '
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
    """科目余额表/试算平衡：month=YYYY-MM 或 from/to=YYYY-MM-DD"""
    try:
        month = request.args.get('month', '')
        frm = request.args.get('from', '')
        to = request.args.get('to', '')
        if frm or to:
            start = _validate_date(frm, '开始日期') if frm else date(1900, 1, 1)
            end = _validate_date(to, '结束日期') if to else _now()
            if start > end:
                raise ValueError('开始日期不能晚于结束日期')
            month = f'{start.strftime("%Y-%m")}~{end.strftime("%Y-%m-%d")}'
        else:
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
                        'WHERE e.account_code=? AND v.status<>\'voided\' ORDER BY v.vdate, v.id',
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
                        'WHERE e.account_code=? AND e.debit>0 AND v.status<>\'voided\' '
                        'ORDER BY v.vdate DESC, v.id DESC',
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


# ========== PC 桌面版页面 ==========
@bp.route('/ledger/pc')
def ledger_pc():
    return send_from_directory(AC_DIR, 'pc.html')


# ========== 辅助核算 API ==========
def _load_dims(conn):
    """维度 + 启用项，返回 [{id,name,items:[{id,name,active}]}]"""
    dims = conn.execute('SELECT * FROM aux_dims ORDER BY id').fetchall()
    out = []
    for d in dims:
        items = conn.execute(
            'SELECT id, name, active FROM aux_items WHERE dim_id=? ORDER BY id',
            (d['id'],)).fetchall()
        out.append({'id': d['id'], 'name': d['name'],
                    'items': [dict(i) for i in items]})
    return out


def _dim_in_use(conn, dim_id):
    """维度是否被科目绑定 / 有项被凭证或期初引用"""
    binds = conn.execute('SELECT COUNT(*) AS c FROM accounts WHERE aux_dims LIKE ?',
                         (f'%{dim_id}%',)).fetchone()['c']
    refs = conn.execute(
        'SELECT COUNT(*) AS c FROM voucher_entry_aux ea '
        'JOIN aux_items ai ON ai.id=ea.aux_item_id WHERE ai.dim_id=?',
        (dim_id,)).fetchone()['c']
    ops = conn.execute(
        'SELECT COUNT(*) AS c FROM aux_openings ao JOIN aux_items ai ON ai.id=ao.aux_item_id '
        'WHERE ai.dim_id=?', (dim_id,)).fetchone()['c']
    return binds > 0 or refs > 0 or ops > 0


@bp.route('/api/ledger/aux/dims', methods=['GET'])
def aux_list_dims():
    try:
        conn = _get_db()
        data = _load_dims(conn)
        conn.close()
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/aux/dims', methods=['POST'])
def aux_create_dim():
    try:
        name = str((request.get_json(silent=True) or {}).get('name') or '').strip()
        if not name:
            raise ValueError('维度名称不能为空')
        conn = _get_db()
        try:
            conn.execute('INSERT INTO aux_dims (name) VALUES (?)', (name,))
        except Exception:
            raise ValueError('同名辅助核算维度已存在')
        conn.commit()
        conn.close()
        _log(f'新增辅助核算维度: {name}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/aux/dims/<did>', methods=['PUT'])
def aux_update_dim(did):
    try:
        name = str((request.get_json(silent=True) or {}).get('name') or '').strip()
        if not name:
            raise ValueError('维度名称不能为空')
        conn = _get_db()
        if not conn.execute('SELECT 1 FROM aux_dims WHERE id=?', (did,)).fetchone():
            raise ValueError('维度不存在')
        try:
            conn.execute('UPDATE aux_dims SET name=?, '
                         'updated_at=datetime(\'now\',\'localtime\') WHERE id=?', (name, did))
        except Exception:
            raise ValueError('同名辅助核算维度已存在')
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/aux/dims/<did>', methods=['DELETE'])
def aux_delete_dim(did):
    try:
        conn = _get_db()
        if not conn.execute('SELECT 1 FROM aux_dims WHERE id=?', (did,)).fetchone():
            raise ValueError('维度不存在')
        if conn.execute('SELECT 1 FROM aux_items WHERE dim_id=?', (did,)).fetchone():
            raise ValueError('请先删除该维度下的所有辅助项')
        if _dim_in_use(conn, did):
            raise ValueError('该维度已被科目或凭证引用，不可删除')
        conn.execute('DELETE FROM aux_dims WHERE id=?', (did,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/aux/items', methods=['POST'])
def aux_create_item():
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        dim_id = str(data.get('dim_id') or '').strip()
        if not name:
            raise ValueError('辅助项名称不能为空')
        conn = _get_db()
        if not conn.execute('SELECT 1 FROM aux_dims WHERE id=?', (dim_id,)).fetchone():
            raise ValueError('维度不存在')
        conn.execute('INSERT INTO aux_items (dim_id, name) VALUES (?,?)', (dim_id, name))
        conn.commit()
        conn.close()
        _log(f'新增辅助项 {name}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/aux/items/<iid>', methods=['PUT'])
def aux_update_item(iid):
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        active = 1 if data.get('active', True) in (True, 1, '1') else 0
        if not name:
            raise ValueError('辅助项名称不能为空')
        conn = _get_db()
        if not conn.execute('SELECT 1 FROM aux_items WHERE id=?', (iid,)).fetchone():
            raise ValueError('辅助项不存在')
        conn.execute('UPDATE aux_items SET name=?, active=?, '
                     'updated_at=datetime(\'now\',\'localtime\') WHERE id=?', (name, active, iid))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/aux/items/<iid>', methods=['DELETE'])
def aux_delete_item(iid):
    try:
        conn = _get_db()
        if not conn.execute('SELECT 1 FROM aux_items WHERE id=?', (iid,)).fetchone():
            raise ValueError('辅助项不存在')
        ref = conn.execute('SELECT 1 FROM voucher_entry_aux WHERE aux_item_id=?', (iid,)).fetchone()
        op = conn.execute('SELECT 1 FROM aux_openings WHERE aux_item_id=?', (iid,)).fetchone()
        if ref or op:
            raise ValueError('该辅助项已被凭证或期初引用，只能停用不可删除')
        conn.execute('DELETE FROM aux_items WHERE id=?', (iid,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/aux/openings', methods=['GET'])
def aux_get_openings():
    """某科目（可选维度）的辅助期初。amount 带符号：正=借方、负=贷方"""
    try:
        code = str(request.args.get('account_code') or '').strip()
        dim = request.args.get('dim_id', '')
        conn = _get_db()
        sql = ('SELECT ao.aux_item_id, ai.name AS item_name, ai.dim_id, '
               'SUM(ao.amount) AS amt FROM aux_openings ao '
               'JOIN aux_items ai ON ai.id=ao.aux_item_id WHERE ao.account_code=?')
        params = [code]
        if dim:
            sql += ' AND ai.dim_id=?'
            params.append(dim)
        rows = conn.execute(sql + ' GROUP BY ao.aux_item_id', params).fetchall()
        conn.close()
        return jsonify({'success': True, 'data': [{
            'item_id': r['aux_item_id'], 'item_name': r['item_name'],
            'dim_id': r['dim_id'], 'amount': _round(r['amt'])} for r in rows]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/aux/openings', methods=['PUT'])
def aux_set_openings():
    """覆盖写某科目辅助期初。rows: [{item_id, amount}] amount 带符号 借正贷负"""
    try:
        data = request.get_json(silent=True) or {}
        code = str(data.get('account_code') or '').strip()
        rows = data.get('rows') or []
        if not code:
            raise ValueError('缺少科目')
        conn = _get_db()
        accts = _acct_map(conn)
        if code not in accts:
            raise ValueError('科目不存在')
        cleaned = {}
        for r in rows:
            item_id = str(r.get('item_id') or '').strip()
            amt = _round(r.get('amount') or 0)
            if not item_id or abs(amt) <= _EPS:
                continue
            if not conn.execute('SELECT 1 FROM aux_items WHERE id=? AND active=1',
                                (item_id,)).fetchone():
                raise ValueError(f'辅助项不存在或已停用: {item_id}')
            cleaned[item_id] = amt
        conn.execute('DELETE FROM aux_openings WHERE account_code=?', (code,))
        for item_id, amt in cleaned.items():
            conn.execute('INSERT INTO aux_openings (account_code, aux_item_id, amount) '
                         'VALUES (?,?,?)', (code, item_id, amt))
        conn.commit()
        conn.close()
        _log(f'更新辅助期初: {code}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/aux/balance', methods=['GET'])
def aux_balance():
    """辅助余额表：按维度（可限科目）统计 期初/本期/期末。
    month=YYYY-MM 表示本期为该月，期初为月初累计。"""
    try:
        dim = str(request.args.get('dim_id') or '').strip()
        code = str(request.args.get('account_code') or '').strip()
        month = request.args.get('month', '')
        conn = _get_db()
        if not dim or not conn.execute('SELECT 1 FROM aux_dims WHERE id=?', (dim,)).fetchone():
            raise ValueError('请指定有效的辅助核算维度')
        if month:
            start, end = _month_bounds(month)
        else:
            start, end = date(1900, 1, 1), _now()
        accts = _acct_map(conn)
        # 期初 = 辅助期初 + 期前发生额（均按借正贷负口径）
        pre = {}
        cur = {}
        if month:
            op_sql = ('SELECT ao.aux_item_id, ao.account_code, SUM(ao.amount) AS amt '
                      'FROM aux_openings ao JOIN aux_items ai ON ai.id=ao.aux_item_id '
                      'WHERE ai.dim_id=?')
            op_params = [dim]
            if code:
                op_sql += ' AND ao.account_code=?'
                op_params.append(code)
            for r in conn.execute(op_sql + ' GROUP BY ao.account_code, ao.aux_item_id', op_params):
                key = (r['account_code'], r['aux_item_id'])
                pre.setdefault(key, 0.0)
                pre[key] = _round(pre[key] + r['amt'])
            base = ('FROM voucher_entry_aux ea JOIN voucher_entries e ON e.id=ea.entry_id '
                    'JOIN vouchers v ON v.id=e.voucher_id JOIN aux_items ai ON ai.id=ea.aux_item_id '
                    'WHERE ai.dim_id=? AND v.status<>\'voided\'')
            if code:
                base += ' AND e.account_code=?'
            rows = conn.execute(
                'SELECT e.account_code, ea.aux_item_id, SUM(e.debit) AS dr, SUM(e.credit) AS cr '
                + base + ' AND v.vdate<? GROUP BY e.account_code, ea.aux_item_id',
                tuple([dim] + ([code] if code else []) + [start.isoformat()])).fetchall()
            for r in rows:
                key = (r['account_code'], r['aux_item_id'])
                pre.setdefault(key, 0.0)
                pre[key] = _round(pre[key] + _round(r['dr'] or 0) - _round(r['cr'] or 0))
            rows = conn.execute(
                'SELECT e.account_code, ea.aux_item_id, SUM(e.debit) AS dr, SUM(e.credit) AS cr '
                + base + ' AND v.vdate>=? AND v.vdate<=? GROUP BY e.account_code, ea.aux_item_id',
                tuple([dim] + ([code] if code else []) + [start.isoformat(), end.isoformat()])).fetchall()
            for r in rows:
                key = (r['account_code'], r['aux_item_id'])
                d = _round(r['dr'] or 0)
                c = _round(r['cr'] or 0)
                cur.setdefault(key, [0.0, 0.0])
                cur[key][0] = _round(cur[key][0] + d)
                cur[key][1] = _round(cur[key][1] + c)
        # 汇总展示
        pre = dict(pre)  # 完整期初累计（便于仅看发生期前时不再重复加 pre 期初）
        rows_out = []
        keys = set(list(pre.keys()) + list(cur.keys()))
        items_name = {}
        for r in conn.execute(
                'SELECT id, name FROM aux_items WHERE dim_id=? AND active=1', (dim,)).fetchall():
            items_name[r['id']] = r['name']
        t_op_dr = t_op_cr = t_cur_dr = t_cur_cr = 0.0
        for key in sorted(keys, key=lambda k: (items_name.get(k[1], ''), k[0])):
            code_k, item_k = key
            op_dr, op_cr = 0.0, 0.0
            # pre 已含发生净额累计；拆分为 期初(期前累计) 显示
            op = pre.get(key, 0.0)
            op_dr = op if op > 0 else 0.0
            op_cr = abs(op) if op < 0 else 0.0
            c = cur.get(key, [0.0, 0.0])
            bal = _round(pre.get(key, 0.0) + c[0] - c[1])
            end_dr = bal if bal > 0 else 0.0
            end_cr = abs(bal) if bal < 0 else 0.0
            t_op_dr += op_dr
            t_op_cr += op_cr
            t_cur_dr += c[0]
            t_cur_cr += c[1]
            if abs(op_dr) + abs(op_cr) + c[0] + c[1] + end_dr + end_cr <= _EPS:
                continue
            a = accts.get(code_k, {})
            rows_out.append({
                'account_code': code_k,
                'account_name': a.get('name', code_k),
                'item_id': item_k, 'item_name': items_name.get(item_k, '?'),
                'op_dr': _round(op_dr), 'op_cr': _round(op_cr),
                'cur_dr': _round(c[0]), 'cur_cr': _round(c[1]),
                'end_dr': _round(end_dr), 'end_cr': _round(end_cr),
            })
        conn.close()
        return jsonify({
            'success': True, 'dim_id': int(dim), 'month': month or 'all',
            'from': start.isoformat(), 'to': end.isoformat(),
            'rows': rows_out,
            'totals': {'op_dr': _round(t_op_dr), 'op_cr': _round(t_op_cr),
                       'cur_dr': _round(t_cur_dr), 'cur_cr': _round(t_cur_cr),
                       'end_dr': _round(sum(r['end_dr'] for r in rows_out)),
                       'end_cr': _round(sum(r['end_cr'] for r in rows_out))},
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ========== 凭证审核 / 作废 / 红冲 API ==========
@bp.route('/api/ledger/vouchers/<vid>/audit', methods=['POST'])
def voucher_audit(vid):
    """action: audit(审核) / unaudit(反审核) / void(作废) / unvoid(恢复)"""
    try:
        action = str((request.get_json(silent=True) or {}).get('action') or '').strip()
        allowed = {'audit': 'posted', 'unaudit': 'open', 'void': 'voided', 'unvoid': 'open'}
        if action not in allowed:
            raise ValueError('无效操作')
        conn = _get_db()
        row = conn.execute('SELECT * FROM vouchers WHERE id=?', (vid,)).fetchone()
        if not row:
            raise ValueError('凭证不存在')
        row = dict(row)
        if _period_closed(conn, _vdate_ym(_validate_date(row['vdate'], '凭证日期'))):
            raise ValueError('凭证所在期间已结账，不能执行该操作')
        target = allowed[action]
        if row['status'] == 'voided' and action in ('audit', 'unaudit'):
            raise ValueError('请先恢复作废凭证')
        if row['status'] == 'posted' and action == 'audit':
            raise ValueError('凭证已是审核状态')
        if row['status'] == 'open' and action == 'unaudit':
            raise ValueError('凭证已是未审核状态')
        conn.execute('UPDATE vouchers SET status=?, '
                     'updated_at=datetime(\'now\',\'localtime\') WHERE id=?', (target, vid))
        conn.commit()
        conn.close()
        _log(f'凭证{vid} 操作 {action} -> {target}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/vouchers/<vid>/reverse', methods=['POST'])
def voucher_reverse(vid):
    """红字冲销：生成一张借贷互换的红字凭证（自动审核）"""
    try:
        conn = _get_db()
        src = conn.execute('SELECT * FROM vouchers WHERE id=?', (vid,)).fetchone()
        if not src:
            raise ValueError('凭证不存在')
        src = dict(src)
        if src['status'] == 'voided':
            raise ValueError('作废凭证不能冲销')
        vdate = _validate_date(src['vdate'], '凭证日期')
        ym = _vdate_ym(vdate)
        if _period_closed(conn, ym):
            raise ValueError(f'{ym} 期间已结账，不能红冲')
        no = _next_voucher_no(conn, vdate.strftime('%Y%m'))
        cur = conn.execute(
            'INSERT INTO vouchers (voucher_no, vdate, summary, status, red, auto, source_vid) '
            'VALUES (?,?,?,?,?,?,?)',
            (no, vdate.isoformat(), f'冲销 {src["voucher_no"]}',
             'posted', 1, 1, vid))
        nid = cur.lastrowid
        for e in conn.execute(
                'SELECT * FROM voucher_entries WHERE voucher_id=? ORDER BY id', (vid,)).fetchall():
            e = dict(e)
            nsum = e['summary'] or src['summary']
            new_cur = conn.execute(
                'INSERT INTO voucher_entries (voucher_id, account_code, summary, debit, credit) '
                'VALUES (?,?,?,?,?)',
                (nid, e['account_code'], nsum, e['credit'], e['debit']))
            n_eid = new_cur.lastrowid
            for ea in conn.execute('SELECT aux_item_id FROM voucher_entry_aux WHERE entry_id=?',
                                   (e['id'],)).fetchall():
                conn.execute('INSERT OR IGNORE INTO voucher_entry_aux (entry_id, aux_item_id) '
                             'VALUES (?,?)', (n_eid, ea['aux_item_id']))
        conn.commit()
        conn.close()
        _log(f'红冲凭证 {src["voucher_no"]} -> {no}')
        return jsonify({'success': True, 'id': nid, 'voucher_no': no})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ========== 凭证模板 API ==========
@bp.route('/api/ledger/templates', methods=['GET'])
def list_templates():
    try:
        conn = _get_db()
        rows = conn.execute('SELECT id, name, content, created_at, updated_at '
                            'FROM voucher_templates ORDER BY id DESC').fetchall()
        conn.close()
        out = []
        for r in rows:
            try:
                content = json.loads(r['content']) if r['content'] else []
            except Exception:
                content = []
            out.append({'id': r['id'], 'name': r['name'], 'content': content,
                        'updated_at': r['updated_at']})
        return jsonify({'success': True, 'data': out})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/templates', methods=['POST'])
def create_template():
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        conn = _get_db()
        if not name:
            raise ValueError('模板名称不能为空')
        lines = _template_of(conn, data.get('content'))
        conn.execute('INSERT INTO voucher_templates (name, content) VALUES (?,?)',
                     (name, json.dumps(lines, ensure_ascii=False)))
        conn.commit()
        conn.close()
        _log(f'新增凭证模板: {name}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/templates/<tid>', methods=['PUT'])
def update_template(tid):
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        conn = _get_db()
        if not conn.execute('SELECT 1 FROM voucher_templates WHERE id=?', (tid,)).fetchone():
            raise ValueError('模板不存在')
        if not name:
            raise ValueError('模板名称不能为空')
        lines = _template_of(conn, data.get('content'))
        conn.execute('UPDATE voucher_templates SET name=?, content=?, '
                     'updated_at=datetime(\'now\',\'localtime\') WHERE id=?',
                     (name, json.dumps(lines, ensure_ascii=False), tid))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/templates/<tid>', methods=['DELETE'])
def delete_template(tid):
    try:
        conn = _get_db()
        if not conn.execute('SELECT 1 FROM voucher_templates WHERE id=?', (tid,)).fetchone():
            raise ValueError('模板不存在')
        conn.execute('DELETE FROM voucher_templates WHERE id=?', (tid,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ========== 期间结账 / 结转损益 API ==========
def _month_vouchers_stats(conn, month):
    """单月凭证统计：总数/已审核/作废/自动结转凭证id"""
    start, end = _month_bounds(month)
    rows = conn.execute(
        'SELECT id, status, auto, source_vid FROM vouchers WHERE vdate>=? AND vdate<=?',
        (start.isoformat(), end.isoformat())).fetchall()
    total = posted = open_ = voided = 0
    carry_vid = None
    for r in rows:
        total += 1
        s = r['status']
        if s == 'posted':
            posted += 1
        elif s == 'voided':
            voided += 1
        else:
            open_ += 1
        if r['auto'] and not r['source_vid'] and s != 'voided':
            carry_vid = r['id']
    return {'total': total, 'posted': posted, 'open': open_,
            'voided': voided, 'carry_vid': carry_vid}


@bp.route('/api/ledger/periods', methods=['GET'])
def list_periods():
    """期间状态总览：year 或最近 13 个月；monthly 收入费用 + 结转/结账状态"""
    try:
        year = request.args.get('year', '')
        conn = _get_db()
        now = _now()
        months = []
        if year:
            try:
                year = int(year)
            except Exception:
                raise ValueError('年份无效')
            for m in range(1, 13):
                key = f'{year:04d}-{m:02d}'
                if date(year, m, 1) > now:
                    break
                months.append(key)
        else:
            for i in range(12):
                d = date(now.year, now.month, 1)
                if i > 0:
                    d = date(d.year, d.month - i, 1) if d.month > i else \
                        date(d.year - 1, 12 - (i - d.month), 1)
                months.append(f'{d.year:04d}-{d.month:02d}')
            months.reverse()
        closed = {r['month'] for r in conn.execute('SELECT month FROM closings').fetchall()}
        out = []
        for m_key in months:
            st = _month_vouchers_stats(conn, m_key)
            mm = _month_income_expense(conn, m_key)
            out.append({
                'month': m_key,
                'closed': m_key in closed,
                'closed_at': '',
                **st,
                'income': mm['income'], 'expense': mm['expense'], 'net': mm['net'],
            })
        if year:
            closed_rows = conn.execute('SELECT month, closed_at FROM closings ORDER BY month').fetchall()
        else:
            closed_rows = conn.execute(
                'SELECT month, closed_at FROM closings ORDER BY month DESC LIMIT 12').fetchall()
        cmap = {r['month']: r['closed_at'] for r in closed_rows}
        for it in out:
            if it['month'] in cmap:
                it['closed_at'] = cmap[it['month']]
        conn.close()
        return jsonify({'success': True, 'data': out,
                        'now': now.strftime('%Y-%m')})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


def _ensure_profit_account(conn):
    """确保存在 本年利润 一级科目（code 3103），返回 code"""
    for a in conn.execute("SELECT code FROM accounts WHERE code LIKE '3103%' "
                          "AND parent_code='' AND category='equity'").fetchall():
        return a['code']
    conn.execute("INSERT INTO accounts (code, name, category) VALUES ('3103','本年利润','equity')")
    _log('自动创建科目 3103 本年利润')
    return '3103'


def _carry_rows(conn, month):
    """结转损益数据：返回 (income_rows, expense_rows)。
    income_rows：应结转收入科目（借收入科目/贷本年利润）金额 >0；
    expense_rows：应结转费用科目（借本年利润/贷费用科目）金额 >0。"""
    start, end = _month_bounds(month)
    sums = _period_account_sums(conn, start, end)
    accts = _acct_map(conn)
    income_rows, expense_rows = [], []
    for code, s in sorted(sums.items()):
        a = accts.get(code)
        if not a:
            continue
        if a['category'] == 'income':
            net = _round(s[1] - s[0])   # 收入贷方净额
        elif a['category'] == 'expense':
            net = _round(s[0] - s[1])   # 费用借方净额
        else:
            continue
        if abs(net) <= _EPS:
            continue
        row = {'account_code': code, 'amount': net}
        if a['category'] == 'income':
            income_rows.append(row)
        else:
            expense_rows.append(row)
    return income_rows, expense_rows


@bp.route('/api/ledger/carry-profit', methods=['POST'])
def carry_profit():
    """按 month 生成结转损益凭证（自动审核）。若已存在返回 existing。"""
    try:
        month = str((request.get_json(silent=True) or {}).get('month') or '').strip()
        start, end = _month_bounds(month)
        conn = _get_db()
        if _period_closed(conn, month):
            raise ValueError(f'{month} 已结账，请先反结账')
        old_stats = _month_vouchers_stats(conn, month)
        if old_stats['carry_vid']:
            conn.close()
            return jsonify({'success': True, 'existing': True, 'id': old_stats['carry_vid']})
        income_rows, expense_rows = _carry_rows(conn, month)
        if not income_rows and not expense_rows:
            raise ValueError('本期无收入或费用发生，无需结转')
        profit_code = _ensure_profit_account(conn)
        lines = []
        inc_total = 0.0
        exp_total = 0.0
        # 收入结转：借 收入科目 / 贷 本年利润
        for x in income_rows:
            lines.append({'summary': '结转收入', 'account_code': x['account_code'],
                          'debit': x['amount'], 'credit': 0.0})
            inc_total = _round(inc_total + x['amount'])
        # 费用结转：借 本年利润 / 贷 费用科目
        for x in expense_rows:
            lines.append({'summary': '结转费用', 'account_code': x['account_code'],
                          'debit': 0.0, 'credit': x['amount']})
            exp_total = _round(exp_total + x['amount'])
        net = _round(inc_total - exp_total)
        if net > 0:
            lines.append({'summary': '结转本年利润', 'account_code': profit_code,
                          'debit': 0.0, 'credit': net})
        elif net < 0:
            lines.append({'summary': '结转本年利润', 'account_code': profit_code,
                          'debit': abs(net), 'credit': 0.0})
        total_dr = _round(sum(x['debit'] for x in lines))
        total_cr = _round(sum(x['credit'] for x in lines))
        if abs(total_dr - total_cr) > _EPS:
            raise ValueError('结转计算借贷不平衡，请检查科目')
        no = _next_voucher_no(conn, end.strftime('%Y%m'))
        cur = conn.execute(
            'INSERT INTO vouchers (voucher_no, vdate, summary, status, auto) '
            'VALUES (?,?,?,?,?)',
            (no, end.isoformat(), '结转本期损益', 'posted', 1))
        vid = cur.lastrowid
        for ln in lines:
            conn.execute(
                'INSERT INTO voucher_entries (voucher_id, account_code, summary, debit, credit) '
                'VALUES (?,?,?,?,?)',
                (vid, ln['account_code'], ln['summary'], ln['debit'], ln['credit']))
        conn.commit()
        conn.close()
        _log(f'结转损益 {month} -> {no} 合计 {total_dr:.2f}')
        return jsonify({'success': True, 'id': vid, 'voucher_no': no,
                        'total': total_dr})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/carry-profit', methods=['DELETE'])
def delete_carry_profit():
    """删除某月自动结转损益凭证（仅未结账月）"""
    try:
        month = str((request.args.get('month') or '')).strip()
        _month_bounds(month)
        conn = _get_db()
        if _period_closed(conn, month):
            raise ValueError(f'{month} 已结账，请先反结账')
        st = _month_vouchers_stats(conn, month)
        if not st['carry_vid']:
            raise ValueError('本期尚未生成结转损益凭证')
        vid = st['carry_vid']
        conn.execute('DELETE FROM voucher_entry_aux WHERE entry_id IN '
                     '(SELECT id FROM voucher_entries WHERE voucher_id=?)', (vid,))
        conn.execute('DELETE FROM voucher_entries WHERE voucher_id=?', (vid,))
        conn.execute('DELETE FROM vouchers WHERE id=?', (vid,))
        conn.commit()
        conn.close()
        _log(f'删除结转凭证 {month} {vid}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/closing/<month>', methods=['POST'])
def close_period(month):
    """结账：校验借贷平衡 → 必须已结转损益 → 锁定期间"""
    try:
        _month_bounds(month)
        conn = _get_db()
        if _period_closed(conn, month):
            raise ValueError(f'{month} 已结账')
        # 试算平衡校验
        start, end = _month_bounds(month)
        sums = _period_account_sums(conn, start, end)
        if abs(sum(v[0] for v in sums.values()) -
               sum(v[1] for v in sums.values())) > _EPS:
            raise ValueError('本期借贷不平衡，请先修正凭证')
        # 校验损益已结转：存在收入/费用发生但未结转时提示
        st = _month_vouchers_stats(conn, month)
        pending = _carry_rows(conn, month)
        if pending[0] or pending[1]:
            if st['carry_vid']:
                pass  # 已生成结转凭证则视同处理完毕
            else:
                raise ValueError('本期损益尚未结转，请先“结转损益”再结账')
        conn.execute('INSERT INTO closings (month) VALUES (?)', (month,))
        conn.commit()
        conn.close()
        _log(f'结账 {month}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/ledger/closing/<month>', methods=['DELETE'])
def unclose_period(month):
    """反结账：仅允许最近一个已结月份"""
    try:
        _month_bounds(month)
        conn = _get_db()
        if not _period_closed(conn, month):
            raise ValueError(f'{month} 未结账')
        last = conn.execute('SELECT MAX(month) AS m FROM closings').fetchone()['m']
        if last != month:
            raise ValueError(f'请先反结账较新的期间 {last}，再处理 {month}')
        conn.execute('DELETE FROM closings WHERE month=?', (month,))
        conn.commit()
        conn.close()
        _log(f'反结账 {month}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


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
            'WHERE status<>\'voided\' ORDER BY vdate DESC, id DESC LIMIT 8').fetchall()
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
    # ---- 增量列（老库升级，重复执行自动跳过） ----
    _ALTERS = [
        "ALTER TABLE accounts ADD COLUMN aux_dims TEXT DEFAULT ''",
        "ALTER TABLE vouchers ADD COLUMN status TEXT DEFAULT 'open'",
        "ALTER TABLE vouchers ADD COLUMN red INTEGER DEFAULT 0",
        "ALTER TABLE vouchers ADD COLUMN auto INTEGER DEFAULT 0",
        "ALTER TABLE vouchers ADD COLUMN source_vid INTEGER DEFAULT 0",
        "ALTER TABLE vouchers ADD COLUMN attachments INTEGER DEFAULT 0",
    ]
    for _sql in _ALTERS:
        try:
            conn.execute(_sql)
        except Exception:
            pass

    # ---- 扩展业务表：辅助核算 / 期间结账 / 凭证模板 ----
    conn.execute('''CREATE TABLE IF NOT EXISTS aux_dims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS aux_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dim_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS voucher_entry_aux (
        entry_id INTEGER NOT NULL,
        aux_item_id INTEGER NOT NULL,
        PRIMARY KEY (entry_id, aux_item_id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS aux_openings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_code TEXT NOT NULL,
        aux_item_id INTEGER NOT NULL,
        amount REAL DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS closings (
        month TEXT PRIMARY KEY,
        closed_at TEXT DEFAULT (datetime('now','localtime'))
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS voucher_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        content TEXT DEFAULT '[]',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_entryaux_item ON voucher_entry_aux(aux_item_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_auxitems_dim ON aux_items(dim_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_auxopen_acct ON aux_openings(account_code)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_vouchers_status ON vouchers(status)')
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

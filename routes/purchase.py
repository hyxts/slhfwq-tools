# -*- coding: utf-8 -*-
"""采购台账 Blueprint

按 Excel 采购表建模：日期 / 单据号 / 供应商名称 / 品类 / 采购内容摘要 /
发票类型 / 采购总金额(含税/买价) / 进项税额 / 已开金额 / 付款账户 / 已付金额。
派生字段（不落库，查询时计算）：
- 不含税成本 = 采购总金额 - 进项税额
- 未付余额   = 采购总金额 - 已付金额
- 付款状态   = 已付清 / 部分付款 / 未付款
"""
from __future__ import annotations

import csv
import io
import os
import re
from flask import Blueprint, Response, jsonify, request, send_from_directory

from .utils import make_db, make_logger

bp = Blueprint('purchase', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(BASE_DIR, '采购')
DB_FILE = os.path.join(DB_DIR, 'purchase.db')
_get_db = make_db(DB_FILE)

_log = make_logger(os.path.join(DB_DIR, 'purchase.log'))

INVOICE_TYPES = ('无票(不抵)', '专票', '普票')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_EPS = 0.005          # 金额比较容差（分为单位以下忽略）


def _num(val, default: float = 0.0) -> float:
    """宽松解析数字：容忍千分位逗号与空串"""
    if val is None:
        return default
    s = str(val).strip().replace(',', '').replace('，', '')
    if not s:
        return default
    try:
        v = float(s)
    except ValueError:
        raise ValueError('金额格式不正确')
    return v


def _round2(v: float) -> float:
    return round(v + 0.0, 2)


def _validate(data: dict) -> dict:
    """校验并规整一条采购记录，失败抛 ValueError（API 层转 400）"""
    rec = {
        'date': str(data.get('date') or '').strip(),
        'bill_no': str(data.get('bill_no') or '').strip()[:50],
        'supplier': str(data.get('supplier') or '').strip()[:100],
        'category': str(data.get('category') or '').strip()[:50],
        'summary': str(data.get('summary') or '').strip()[:200],
        'invoice_type': str(data.get('invoice_type') or '无票(不抵)').strip(),
        'pay_account': str(data.get('pay_account') or '').strip()[:50],
    }
    if not DATE_RE.match(rec['date']):
        raise ValueError('日期格式应为 YYYY-MM-DD')
    if not rec['supplier']:
        raise ValueError('供应商名称不能为空')

    total = _round2(_num(data.get('total_amount')))
    tax = _round2(_num(data.get('input_tax')))
    invoiced = _round2(_num(data.get('invoiced_amount')))
    paid = _round2(_num(data.get('paid_amount')))
    if total <= 0:
        raise ValueError('采购总金额必须大于 0')
    if tax < 0 or invoiced < 0 or paid < 0:
        raise ValueError('金额不能为负数')
    if tax > total + _EPS:
        raise ValueError('进项税额不能大于采购总金额')
    if invoiced > total + _EPS:
        raise ValueError('已开金额不能大于采购总金额')
    if paid > total + _EPS:
        raise ValueError('已付金额不能大于采购总金额')

    rec['total_amount'] = total
    rec['input_tax'] = tax
    rec['invoiced_amount'] = invoiced
    rec['paid_amount'] = paid
    if rec['invoice_type'] not in INVOICE_TYPES:
        rec['invoice_type'] = '无票(不抵)'
    return rec


def _with_derived(row: dict) -> dict:
    """补上派生字段：不含税成本 / 未付余额 / 付款状态"""
    total = row.get('total_amount') or 0.0
    row['net_cost'] = _round2(total - (row.get('input_tax') or 0.0))
    row['unpaid_balance'] = _round2(total - (row.get('paid_amount') or 0.0))
    unpaid = row['unpaid_balance']
    if unpaid <= _EPS:
        row['pay_status'] = '已付清'
    elif unpaid >= total - _EPS:
        row['pay_status'] = '未付款'
    else:
        row['pay_status'] = '部分付款'
    return row


def init_db():
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        conn = _get_db()
        conn.execute('''CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            bill_no TEXT DEFAULT '',
            supplier TEXT NOT NULL,
            category TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            invoice_type TEXT NOT NULL DEFAULT '无票(不抵)',
            total_amount REAL NOT NULL,
            input_tax REAL DEFAULT 0,
            invoiced_amount REAL DEFAULT 0,
            paid_amount REAL DEFAULT 0,
            pay_account TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(date)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_purchases_supplier ON purchases(supplier)')
        conn.commit()
        conn.close()
        _log('purchase_db 初始化完成')
    except Exception as e:
        _log(f'purchase_db 初始化失败: {e}')
        raise


def _filters(args) -> tuple[list[str], list]:
    where: list[str] = ['1=1']
    params: list = []
    start = str(args.get('start') or '').strip()
    end = str(args.get('end') or '').strip()
    supplier = str(args.get('supplier') or '').strip()
    category = str(args.get('category') or '').strip()
    invoice = str(args.get('invoice') or '').strip()
    status = str(args.get('status') or '').strip()
    search = str(args.get('search') or '').strip()
    if start and DATE_RE.match(start):
        where.append('date >= ?')
        params.append(start)
    if end and DATE_RE.match(end):
        where.append('date <= ?')
        params.append(end)
    if supplier:
        where.append('supplier = ?')
        params.append(supplier)
    if category:
        where.append('category = ?')
        params.append(category)
    if invoice:
        where.append('invoice_type = ?')
        params.append(invoice)
    if search:
        where.append('(supplier LIKE ? OR category LIKE ? OR summary LIKE ? OR bill_no LIKE ? OR pay_account LIKE ?)')
        like = f'%{search}%'
        params.extend([like] * 5)
    # 付款状态是派生字段，用 SQL 表达式过滤
    if status in ('未付款', '部分付款', '已付清'):
        unpaid_expr = 'total_amount - paid_amount'
        if status == '未付款':
            where.append(f'{unpaid_expr} >= total_amount - 0.005')
        elif status == '已付清':
            where.append(f'{unpaid_expr} <= 0.005')
        else:
            where.append(f'{unpaid_expr} > 0.005 AND {unpaid_expr} < total_amount - 0.005')
    return where, params


@bp.route('/api/purchase/records', methods=['GET'])
def list_records():
    try:
        limit = min(int(request.args.get('limit', 500)), 5000)
        where, params = _filters(request.args)
        conn = _get_db()
        rows = conn.execute(
            f"SELECT * FROM purchases WHERE {' AND '.join(where)} "
            f"ORDER BY date DESC, id DESC LIMIT ?", (*params, limit)).fetchall()
        conn.close()
        return jsonify({'success': True,
                        'data': [_with_derived(dict(r)) for r in rows]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/records', methods=['POST'])
def create_record():
    try:
        rec = _validate(request.get_json(silent=True) or {})
        conn = _get_db()
        cur = conn.execute(
            '''INSERT INTO purchases (date, bill_no, supplier, category, summary,
               invoice_type, total_amount, input_tax, invoiced_amount,
               paid_amount, pay_account)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (rec['date'], rec['bill_no'], rec['supplier'], rec['category'],
             rec['summary'], rec['invoice_type'], rec['total_amount'],
             rec['input_tax'], rec['invoiced_amount'], rec['paid_amount'],
             rec['pay_account']))
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute('SELECT * FROM purchases WHERE id=?', (rid,)).fetchone()
        conn.close()
        _log(f'新增采购记录 id={rid} supplier={rec["supplier"]} total={rec["total_amount"]}')
        return jsonify({'success': True, 'data': _with_derived(dict(row))})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/purchase/records/<int:rid>', methods=['POST'])
def update_record(rid: int):
    try:
        rec = _validate(request.get_json(silent=True) or {})
        conn = _get_db()
        if not conn.execute('SELECT id FROM purchases WHERE id=?', (rid,)).fetchone():
            conn.close()
            return jsonify({'success': False, 'error': '记录不存在'}), 404
        conn.execute(
            '''UPDATE purchases SET date=?, bill_no=?, supplier=?, category=?,
               summary=?, invoice_type=?, total_amount=?, input_tax=?,
               invoiced_amount=?, paid_amount=?, pay_account=? WHERE id=?''',
            (rec['date'], rec['bill_no'], rec['supplier'], rec['category'],
             rec['summary'], rec['invoice_type'], rec['total_amount'],
             rec['input_tax'], rec['invoiced_amount'], rec['paid_amount'],
             rec['pay_account'], rid))
        conn.commit()
        row = conn.execute('SELECT * FROM purchases WHERE id=?', (rid,)).fetchone()
        conn.close()
        return jsonify({'success': True, 'data': _with_derived(dict(row))})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/purchase/records/<int:rid>', methods=['DELETE'])
def delete_record(rid: int):
    try:
        conn = _get_db()
        cur = conn.execute('DELETE FROM purchases WHERE id=?', (rid,))
        conn.commit()
        conn.close()
        if cur.rowcount == 0:
            return jsonify({'success': False, 'error': '记录不存在'}), 404
        _log(f'删除采购记录 id={rid}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/purchase/stats', methods=['GET'])
def stats():
    """合计 + 按供应商 / 品类 / 发票类型汇总（与列表同一套筛选条件）"""
    try:
        where, params = _filters(request.args)
        w = ' AND '.join(where)
        conn = _get_db()
        total = conn.execute(
            f'''SELECT COUNT(*) cnt, COALESCE(SUM(total_amount),0) total,
                COALESCE(SUM(input_tax),0) tax, COALESCE(SUM(invoiced_amount),0) invoiced,
                COALESCE(SUM(paid_amount),0) paid, COALESCE(SUM(total_amount-paid_amount),0) unpaid
                FROM purchases WHERE {w}''', params).fetchone()
        by_supplier = conn.execute(
            f'''SELECT supplier, COUNT(*) cnt, COALESCE(SUM(total_amount),0) total,
                COALESCE(SUM(total_amount-paid_amount),0) unpaid
                FROM purchases WHERE {w} GROUP BY supplier
                ORDER BY total DESC LIMIT 20''', params).fetchall()
        by_category = conn.execute(
            f'''SELECT category, COUNT(*) cnt, COALESCE(SUM(total_amount),0) total
                FROM purchases WHERE {w} GROUP BY category
                ORDER BY total DESC LIMIT 20''', params).fetchall()
        by_invoice = conn.execute(
            f'''SELECT invoice_type, COUNT(*) cnt, COALESCE(SUM(total_amount),0) total
                FROM purchases WHERE {w} GROUP BY invoice_type
                ORDER BY total DESC''', params).fetchall()
        conn.close()
        return jsonify({'success': True, 'data': {
            'count': total['cnt'],
            'total': _round2(total['total']),
            'tax': _round2(total['tax']),
            'net_cost': _round2(total['total'] - total['tax']),
            'invoiced': _round2(total['invoiced']),
            'paid': _round2(total['paid']),
            'unpaid': _round2(total['unpaid']),
            'by_supplier': [dict(r) for r in by_supplier],
            'by_category': [dict(r) for r in by_category],
            'by_invoice': [dict(r) for r in by_invoice],
        }})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/options', methods=['GET'])
def options():
    """供应商 / 品类 / 付款账户的已有取值（表单下拉建议用）"""
    try:
        conn = _get_db()
        suppliers = [r[0] for r in conn.execute(
            'SELECT DISTINCT supplier FROM purchases ORDER BY supplier')]
        categories = [r[0] for r in conn.execute(
            "SELECT DISTINCT category FROM purchases WHERE category<>'' ORDER BY category")]
        accounts = [r[0] for r in conn.execute(
            "SELECT DISTINCT pay_account FROM purchases WHERE pay_account<>'' ORDER BY pay_account")]
        conn.close()
        return jsonify({'success': True, 'data': {
            'suppliers': suppliers, 'categories': categories,
            'accounts': accounts, 'invoice_types': list(INVOICE_TYPES)}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/export', methods=['GET'])
def export_csv():
    """导出 CSV（UTF-8 BOM，Excel 直接打开不乱码），列与 Excel 台账一致"""
    try:
        where, params = _filters(request.args)
        conn = _get_db()
        rows = conn.execute(
            f"SELECT * FROM purchases WHERE {' AND '.join(where)} "
            f"ORDER BY date, id", params).fetchall()
        conn.close()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(['日期', '单据号', '供应商名称', '品类', '采购内容摘要', '发票类型',
                         '采购总金额(含税/买价)', '进项税额', '不含税成本',
                         '已开金额', '已付金额', '未付余额', '付款账户', '付款状态'])
        for r in rows:
            d = _with_derived(dict(r))
            writer.writerow([d['date'], d['bill_no'], d['supplier'], d['category'],
                             d['summary'], d['invoice_type'], f"{d['total_amount']:.2f}",
                             f"{d['input_tax']:.2f}", f"{d['net_cost']:.2f}",
                             f"{d['invoiced_amount']:.2f}", f"{d['paid_amount']:.2f}",
                             f"{d['unpaid_balance']:.2f}", d['pay_account'], d['pay_status']])
        out = io.BytesIO()
        out.write(b'\xef\xbb\xbf')
        out.write(buf.getvalue().encode('utf-8'))
        return Response(
            out.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename="purchase.csv"'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/purchase')
@bp.route('/purchase/')
def purchase_index():
    resp = send_from_directory(DB_DIR, 'index.html')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

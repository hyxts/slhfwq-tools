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
import datetime
import io
import os
import re
import sqlite3
import urllib.parse

# Excel 读写（openpyxl 已在 requirements.txt）。服务器万一没装，只在用到
# Excel 的接口报错，不让整个采购模块挂掉。
try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.utils.exceptions import InvalidFileException
    EXCEL_OK = True
    EXCEL_ERR = ''
except Exception as _e:                      # pragma: no cover - 依赖缺失时
    EXCEL_OK = False
    EXCEL_ERR = str(_e)
    Workbook = load_workbook = None          # type: ignore[assignment]

    class InvalidFileException(Exception):   # type: ignore[no-redef]
        """openpyxl 缺失时的占位，仅为让后续 import 判断能跑通"""
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


def now_str() -> str:
    """对账时间等展示用的本地时间串"""
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ---------------- Excel 读写（导出 / 模板 / 导入） ----------------

XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
_HEAD_FILL = '1E3A8A'

# 导出与导入共用的列名（顺序即台账顺序，导入时按名字认列，顺序无所谓）
EXPORT_HEADERS = ['日期', '单据号', '供应商名称', '品类', '采购内容摘要', '发票类型',
                  '采购总金额(含税/买价)', '进项税额', '不含税成本', '已开金额',
                  '已付金额', '未付余额', '付款账户', '付款状态']
_MONEY_IDX = (7, 8, 9, 10, 11, 12)          # 1-based：不含税成本是派生列，导入时忽略

# 导入时的列名别名（小写比较，中英文都认）
_COL_ALIASES: dict[str, tuple[str, ...]] = {
    'date': ('日期', '采购日期', 'date'),
    'bill_no': ('单据号', '单号', '票号', 'bill_no', 'billno'),
    'supplier': ('供应商名称', '供应商', '往来单位', 'supplier'),
    'category': ('品类', '类别', 'category'),
    'summary': ('采购内容摘要', '摘要', '内容', 'summary'),
    'invoice_type': ('发票类型', '票种', 'invoice_type'),
    'total_amount': ('采购总金额(含税/买价)', '采购总金额', '总金额', '金额',
                     'total_amount', 'total'),
    'input_tax': ('进项税额', '进项税', '税额', 'input_tax'),
    'invoiced_amount': ('已开金额', '已开票金额', '开票金额', 'invoiced_amount'),
    'paid_amount': ('已付金额', '已付', '付款金额', 'paid_amount'),
    'pay_account': ('付款账户', '账户', '付款方式', 'pay_account'),
}


_OLE2_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'    # 老版 .xls/.et/.doc 的二进制头


def _read_uploaded_workbook(f):
    """读上传的 Excel：先看**文件内容**，不靠后缀（返回 (workbook, error)）

    用户常遇到「明明是 xlsx 却被拒」——多数是这几类：文件名末尾有空格、
    大写 .XLSX、启用宏的 .xlsm、或老 .xls 被直接改了后缀。所以只要内容是
    标准 xlsx（zip 头 PK）就收，后缀只用来在失败时给更有针对性的提示。
    """
    raw = f.read() or b''
    name = (f.filename or '').strip().lower()
    if not raw:
        return None, '文件内容是空的，换个文件试试'
    if raw[:2] == b'PK':                                  # xlsx/xlsm 本质是 zip
        try:
            return load_workbook(io.BytesIO(raw), data_only=True, read_only=True), ''
        except InvalidFileException:
            return None, (f'「{name}」后缀是 Excel，但里面不是 Excel 工作簿'
                          '（常见于是 .xls 直接把后缀改成了 .xlsx）。'
                          '请用 Excel/WPS 打开后另存为「Excel 工作簿(*.xlsx)」再导入')
        except Exception as e:
            return None, f'文件解析失败：{e}'
    if raw[:8] == _OLE2_MAGIC or name.endswith(('.xls', '.xlt', '.et')):
        return None, ('这是老版 Excel 格式（.xls / .et，二进制），本系统只认 .xlsx。'
                      '请用 Excel 或 WPS 打开它，再「文件 → 另存为 → '
                      '保存类型：Excel 工作簿(*.xlsx)」，然后导入新存的文件')
    if name.endswith('.xlsb'):
        return None, '这是二进制工作簿 .xlsb，请另存为 .xlsx 再导入'
    if name.endswith('.csv'):
        return None, '这是 CSV，请先用 Excel 打开并另存为 .xlsx 再导入'
    return None, (f'「{name}」读不出 Excel 内容：多半是文件被改过后缀、已损坏或加了密码。'
                  '请用 Excel/WPS 打开后另存为「Excel 工作簿(*.xlsx)」再导入')


def _xlsx_response(wb, filename: str):
    out = io.BytesIO()
    wb.save(out)
    quoted = urllib.parse.quote(filename)
    return Response(out.getvalue(), mimetype=XLSX_MIME,
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quoted}"})


def _style_header(ws, ncols: int, row: int = 1) -> None:
    """表头：深底白字加粗 + 冻结该行"""
    fill = PatternFill('solid', fgColor=_HEAD_FILL)
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = Font(bold=True, color='FFFFFF', size=11)
        cell.fill = fill
        cell.alignment = Alignment(horizontal='center', vertical='center')
    ws.freeze_panes = f'A{row + 1}'


def _style_money(ws, first_row: int, last_row: int, cols: tuple[int, ...]) -> None:
    for r in range(first_row, last_row + 1):
        for c in cols:
            ws.cell(row=r, column=c).number_format = '#,##0.00'


def _set_widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _cell_text(v) -> str:
    """单元格 → 文本（Excel 里的日期会被 openpyxl 读成 datetime）"""
    if v is None:
        return ''
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime('%Y-%m-%d')
    return str(v).strip()


def _cell_date(v) -> str:
    """单元格 → YYYY-MM-DD；认 datetime、2026-10-01、2026/10/1、20261001"""
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime('%Y-%m-%d')
    s = str(v or '').strip()
    if not s:
        return ''
    s = s.split(' ')[0].replace('/', '-').replace('.', '-') \
         .replace('年', '-').replace('月', '-').replace('日', '')
    if re.match(r'^\d{8}$', s):
        s = f'{s[:4]}-{s[4:6]}-{s[6:]}'
    return s


def _cell_money(v) -> float:
    """单元格 → 数字；认 1234.5、¥1,234.50、'1 234'（非法抛 ValueError）"""
    if v is None or v == '':
        return 0.0
    if isinstance(v, bool):
        raise ValueError('金额不是数字')
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace('¥', '').replace('￥', '').replace(',', '') \
        .replace('，', '').replace(' ', '').replace('元', '')
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        raise ValueError(f'「{v}」不是有效金额')


def _map_header(cells, aliases: dict[str, tuple[str, ...]] | None = None) -> dict[str, int]:
    """表头行 → {字段: 列下标}（aliases 省略时用采购记录的列名表）"""
    aliases = aliases or _COL_ALIASES
    found: dict[str, int] = {}
    for idx, val in enumerate(cells):
        key = _cell_text(val).lower()
        if not key:
            continue
        for field, names in aliases.items():
            if field in found:
                continue
            if key in names or key in [n.lower() for n in names]:
                found[field] = idx
                break
    return found


def _find_header(rows: list[tuple], aliases: dict[str, tuple[str, ...]] | None = None,
                 required: tuple[str, ...] | None = None) -> tuple[int, dict[str, int]]:
    """在前 10 行里找表头：required 指定的字段必须都能认出来

    采购记录默认要求「日期 + 供应商 + 采购总金额」，供应商档案只要求「供应商名称」。
    """
    aliases = aliases or _COL_ALIASES
    required = required or ('date', 'supplier', 'total_amount')
    for i, row in enumerate(rows[:10]):
        m = _map_header(row, aliases)
        if all(k in m for k in required):
            return i, m
    return -1, {}


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
    row['reconciled'] = bool(row.get('recon_at'))
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
        # 对账标记（老库补列）：recon_at 为空=未对账，否则是对账时间
        cols = [r[1] for r in conn.execute('PRAGMA table_info(purchases)').fetchall()]
        if 'recon_at' not in cols:
            conn.execute("ALTER TABLE purchases ADD COLUMN recon_at TEXT DEFAULT ''")
        # 基础配置：供应商档案
        conn.execute('''CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            contact TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            category TEXT DEFAULT '',
            default_invoice TEXT DEFAULT '',
            note TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )''')
        # 基础配置：品类 / 付款账户等通用字典（kind 区分）
        conn.execute('''CREATE TABLE IF NOT EXISTS purchase_dicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            sort INTEGER NOT NULL DEFAULT 0,
            UNIQUE(kind, name)
        )''')
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
                COALESCE(SUM(input_tax),0) tax, COALESCE(SUM(invoiced_amount),0) invoiced,
                COALESCE(SUM(paid_amount),0) paid,
                COALESCE(SUM(total_amount-paid_amount),0) unpaid
                FROM purchases WHERE {w} GROUP BY supplier
                ORDER BY total DESC LIMIT 50''', params).fetchall()
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
            'by_supplier': [_round_summary(dict(r)) for r in by_supplier],
            'by_category': [_round_summary(dict(r)) for r in by_category],
            'by_invoice': [_round_summary(dict(r)) for r in by_invoice],
        }})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ==================== 趋势统计 / 对账报表 ====================

_GROUP_EXPR = {
    'year': "substr(date, 1, 4)",     # 2026
    'month': "substr(date, 1, 7)",    # 2026-10
    'day': "date",                    # 2026-10-18
}


def _period_where(args, extra_supplier: bool = True) -> tuple[list[str], list]:
    """对账专用筛选：日期区间 + 供应商（不走 _filters 的品类/状态等条件）"""
    where: list[str] = ['1=1']
    params: list = []
    start = str(args.get('start') or '').strip()
    end = str(args.get('end') or '').strip()
    if start and DATE_RE.match(start):
        where.append('date >= ?')
        params.append(start)
    if end and DATE_RE.match(end):
        where.append('date <= ?')
        params.append(end)
    if extra_supplier:
        supplier = str(args.get('supplier') or '').strip()
        if supplier:
            where.append('supplier = ?')
            params.append(supplier)
    return where, params


@bp.route('/api/purchase/trends', methods=['GET'])
def trends():
    """按年 / 月 / 日分组的趋势统计（与列表共用筛选条件）"""
    try:
        group = str(request.args.get('group') or 'month').strip()
        expr = _GROUP_EXPR.get(group)
        if not expr:
            return jsonify({'success': False, 'error': 'group 只能是 year / month / day'}), 400
        where, params = _filters(request.args)
        w = ' AND '.join(where)
        conn = _get_db()
        rows = conn.execute(
            f'''SELECT {expr} AS period, COUNT(*) cnt,
                COALESCE(SUM(total_amount),0) total, COALESCE(SUM(input_tax),0) tax,
                COALESCE(SUM(invoiced_amount),0) invoiced, COALESCE(SUM(paid_amount),0) paid,
                COALESCE(SUM(total_amount-paid_amount),0) unpaid
                FROM purchases WHERE {w} GROUP BY period ORDER BY period DESC''',
            params).fetchall()
        conn.close()
        data = [_round_summary(dict(r)) for r in rows]
        return jsonify({'success': True, 'data': {
            'group': group,
            'rows': data,
            'count': sum(r['cnt'] for r in data),
            'total': _round2(sum(r['total'] for r in data)),
            'unpaid': _round2(sum(r['unpaid'] for r in data)),
        }})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/reconcile', methods=['GET'])
def reconcile():
    """对账报表：某供应商（或全部）在某段时间内的逐笔明细 + 汇总

    - summary：本期间合计（采购额 / 进项税 / 不含税成本 / 已开票 / 待开票 /
      已付 / 未付）与对账进度（已核对笔数）
    - outstanding：该供应商**截至现在**的全部未付余额（不只本期间），对账用
    - by_invoice / by_month：本期间按发票类型、按月份的小计
    """
    try:
        supplier = str(request.args.get('supplier') or '').strip()
        where, params = _period_where(request.args)
        w = ' AND '.join(where)
        conn = _get_db()
        rows = conn.execute(
            f'SELECT * FROM purchases WHERE {w} ORDER BY date, id', params).fetchall()
        items = [_with_derived(dict(r)) for r in rows]

        agg = conn.execute(
            f'''SELECT COUNT(*) cnt, COALESCE(SUM(total_amount),0) total,
                COALESCE(SUM(input_tax),0) tax, COALESCE(SUM(invoiced_amount),0) invoiced,
                COALESCE(SUM(paid_amount),0) paid,
                COALESCE(SUM(total_amount-paid_amount),0) unpaid,
                COALESCE(SUM(CASE WHEN recon_at<>"" THEN 1 ELSE 0 END),0) recon_cnt
                FROM purchases WHERE {w}''', params).fetchone()
        by_invoice = conn.execute(
            f'''SELECT invoice_type, COUNT(*) cnt, COALESCE(SUM(total_amount),0) total,
                COALESCE(SUM(total_amount-paid_amount),0) unpaid
                FROM purchases WHERE {w} GROUP BY invoice_type ORDER BY total DESC''',
            params).fetchall()
        by_month = conn.execute(
            f'''SELECT substr(date,1,7) AS period, COUNT(*) cnt,
                COALESCE(SUM(total_amount),0) total, COALESCE(SUM(paid_amount),0) paid,
                COALESCE(SUM(total_amount-paid_amount),0) unpaid
                FROM purchases WHERE {w} GROUP BY period ORDER BY period''',
            params).fetchall()

        # 该供应商全部历史的未付余额（对账看的是「还欠多少」，不只看本期）
        out_where, out_params = ['1=1'], []
        if supplier:
            out_where.append('supplier = ?')
            out_params.append(supplier)
        outstanding = conn.execute(
            f'''SELECT COALESCE(SUM(total_amount),0) total, COALESCE(SUM(paid_amount),0) paid,
                COALESCE(SUM(total_amount-paid_amount),0) unpaid, COUNT(*) cnt
                FROM purchases WHERE {' AND '.join(out_where)}''', out_params).fetchone()

        profile = None
        if supplier:
            p = conn.execute('SELECT * FROM suppliers WHERE name=?', (supplier,)).fetchone()
            profile = dict(p) if p else None
        conn.close()

        total = _round2(agg['total'])
        invoiced = _round2(agg['invoiced'])
        summary = {
            'count': agg['cnt'],
            'total': total,
            'tax': _round2(agg['tax']),
            'net_cost': _round2(total - agg['tax']),
            'invoiced': invoiced,
            'uninvoiced': _round2(total - invoiced),     # 待开票
            'paid': _round2(agg['paid']),
            'unpaid': _round2(agg['unpaid']),
            'recon_cnt': agg['recon_cnt'],
            'unrecon_cnt': agg['cnt'] - agg['recon_cnt'],
        }
        return jsonify({'success': True, 'data': {
            'supplier': supplier,
            'profile': profile,
            'start': str(request.args.get('start') or '').strip(),
            'end': str(request.args.get('end') or '').strip(),
            'generated_at': now_str(),
            'items': items,
            'summary': summary,
            'by_invoice': [_round_summary(dict(r)) for r in by_invoice],
            'by_month': [_round_summary(dict(r)) for r in by_month],
            'outstanding': {
                'count': outstanding['cnt'],
                'total': _round2(outstanding['total']),
                'paid': _round2(outstanding['paid']),
                'unpaid': _round2(outstanding['unpaid']),
            },
        }})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/reconcile/export', methods=['GET'])
def reconcile_export():
    """导出对账单 CSV：抬头（供应商/期间/生成时间）+ 逐笔明细 + 合计行"""
    try:
        supplier = str(request.args.get('supplier') or '').strip()
        where, params = _period_where(request.args)
        w = ' AND '.join(where)
        conn = _get_db()
        rows = conn.execute(
            f'SELECT * FROM purchases WHERE {w} ORDER BY date, id', params).fetchall()
        conn.close()
        items = [_with_derived(dict(r)) for r in rows]

        if not EXCEL_OK:
            return jsonify({'success': False,
                            'error': f'服务器缺少 openpyxl，无法导出 Excel：{EXCEL_ERR}'}), 500
        wb = Workbook()
        ws = wb.active
        ws.title = '对账单'
        ws.append(['采购对账单'])
        ws['A1'].font = Font(bold=True, size=14)
        ws.append(['供应商', supplier or '（全部供应商）', '生成时间', now_str()])
        ws.append(['对账期间', f"{request.args.get('start') or '最早'} 至 "
                               f"{request.args.get('end') or '最新'}"])
        ws.append([])
        ws.append(['日期', '单据号', '品类', '采购内容摘要', '发票类型', '采购总金额',
                   '进项税额', '不含税成本', '已开金额', '已付金额', '未付余额',
                   '付款账户', '付款状态', '已对账'])
        head = ws.max_row
        _style_header(ws, 14, head)
        for d in items:
            ws.append([d['date'], d['bill_no'], d['category'], d['summary'],
                       d['invoice_type'], d['total_amount'], d['input_tax'],
                       d['net_cost'], d['invoiced_amount'], d['paid_amount'],
                       d['unpaid_balance'], d['pay_account'], d['pay_status'],
                       '是' if d['reconciled'] else '否'])
        _style_money(ws, head + 1, ws.max_row, (6, 7, 8, 9, 10, 11))
        ws.append([])
        ws.append(['合计', '', '', f'{len(items)} 笔', '',
                   sum(d['total_amount'] for d in items),
                   sum(d['input_tax'] for d in items),
                   sum(d['net_cost'] for d in items),
                   sum(d['invoiced_amount'] for d in items),
                   sum(d['paid_amount'] for d in items),
                   sum(d['unpaid_balance'] for d in items)])
        last = ws.max_row
        _style_money(ws, last, last, (6, 7, 8, 9, 10, 11))
        for c in range(1, 15):
            ws.cell(row=last, column=c).font = Font(bold=True)
        _set_widths(ws, [12, 14, 10, 26, 12, 13, 12, 13, 12, 12, 12, 12, 10, 8])
        name = (f"对账单_{supplier or '全部'}_{request.args.get('start') or ''}_"
                f"{request.args.get('end') or ''}.xlsx")
        return _xlsx_response(wb, name)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/records/<int:rid>/reconcile', methods=['POST'])
def mark_reconciled(rid: int):
    """对账标记：reconciled=1 记下对账时间，=0 取消"""
    try:
        data = request.get_json(silent=True) or {}
        flag = str(data.get('reconciled', 1)) in ('1', 'true', 'True')
        conn = _get_db()
        if not conn.execute('SELECT id FROM purchases WHERE id=?', (rid,)).fetchone():
            conn.close()
            return jsonify({'success': False, 'error': '记录不存在'}), 404
        conn.execute('UPDATE purchases SET recon_at=? WHERE id=?',
                     (now_str() if flag else '', rid))
        conn.commit()
        row = conn.execute('SELECT * FROM purchases WHERE id=?', (rid,)).fetchone()
        conn.close()
        return jsonify({'success': True, 'data': _with_derived(dict(row))})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/purchase/options', methods=['GET'])
def options():
    """供应商 / 品类 / 付款账户的已有取值（表单下拉建议用）

    供应商：配置里「启用」的 + 历史采购记录里出现过但没建档的（兼容老数据），
    后者标在 extra_suppliers 里，前端一起给建议。
    """
    try:
        conn = _get_db()
        configured = [r[0] for r in conn.execute(
            'SELECT name FROM suppliers WHERE active=1 ORDER BY name')]
        historic = [r[0] for r in conn.execute(
            'SELECT DISTINCT supplier FROM purchases ORDER BY supplier')]
        extra = [n for n in historic if n not in configured]
        suppliers = configured + extra
        categories = [r[0] for r in conn.execute(
            "SELECT name FROM purchase_dicts WHERE kind='category' ORDER BY sort, name")]
        accounts = [r[0] for r in conn.execute(
            "SELECT name FROM purchase_dicts WHERE kind='account' ORDER BY sort, name")]
        historic_categories = [r[0] for r in conn.execute(
            "SELECT DISTINCT category FROM purchases WHERE category<>''")]
        historic_accounts = [r[0] for r in conn.execute(
            "SELECT DISTINCT pay_account FROM purchases WHERE pay_account<>''")]
        categories += [n for n in historic_categories if n not in categories]
        accounts += [n for n in historic_accounts if n not in accounts]
        conn.close()
        return jsonify({'success': True, 'data': {
            'suppliers': suppliers, 'extra_suppliers': extra,
            'categories': categories, 'accounts': accounts,
            'invoice_types': list(INVOICE_TYPES)}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


# ==================== 基础配置 ====================

DICT_KINDS = ('category', 'account')


def _supplier_rows(conn, keyword: str = '', active_only: bool = False) -> list[dict]:
    """供应商档案 + 每家的采购统计（笔数 / 总额 / 未付）"""
    where: list[str] = ['1=1']
    params: list = []
    if active_only:
        where.append('active=1')
    keyword = keyword.strip()
    if keyword:
        where.append('(s.name LIKE ? OR s.contact LIKE ? OR s.phone LIKE ?)')
        like = f'%{keyword}%'
        params.extend([like] * 3)
    sql = (
        'SELECT s.*, '
        '(SELECT COUNT(*) FROM purchases p WHERE p.supplier = s.name) used_cnt, '
        'COALESCE((SELECT SUM(p.total_amount) FROM purchases p WHERE p.supplier = s.name), 0) used_total, '
        'COALESCE((SELECT SUM(p.total_amount - p.paid_amount) FROM purchases p '
        'WHERE p.supplier = s.name), 0) used_unpaid '
        f"FROM suppliers s WHERE {' AND '.join(where)} "
        'ORDER BY s.active DESC, s.name')
    return [_round_supplier(dict(r)) for r in conn.execute(sql, params).fetchall()]


def _round_summary(row: dict) -> dict:
    """把分组汇总里的金额统一保留两位，并补出不含税成本"""
    for k in ('total', 'tax', 'invoiced', 'paid', 'unpaid'):
        if k in row:
            row[k] = _round2(row[k] or 0.0)
    if 'total' in row and 'tax' in row:
        row['net_cost'] = _round2((row['total'] or 0.0) - (row['tax'] or 0.0))
    return row


def _round_supplier(row: dict) -> dict:
    row['used_total'] = _round2(row.get('used_total') or 0.0)
    row['used_unpaid'] = _round2(row.get('used_unpaid') or 0.0)
    return row


def _supplier_payload(data: dict) -> dict:
    """校验供应商字段，失败抛 ValueError"""
    rec = {
        'name': str(data.get('name') or '').strip()[:100],
        'contact': str(data.get('contact') or '').strip()[:50],
        'phone': str(data.get('phone') or '').strip()[:30],
        'category': str(data.get('category') or '').strip()[:50],
        'default_invoice': str(data.get('default_invoice') or '').strip(),
        'note': str(data.get('note') or '').strip()[:200],
        'active': 1 if str(data.get('active', 1)) not in ('0', 'false', 'False', '') else 0,
    }
    if not rec['name']:
        raise ValueError('供应商名称不能为空')
    if rec['default_invoice'] and rec['default_invoice'] not in INVOICE_TYPES:
        raise ValueError('默认发票类型不合法')
    return rec


# 供应商档案导入用的列名（与采购记录的表分开）
_SUPPLIER_HEADERS = ['供应商名称', '联系人', '电话', '主营品类', '默认发票类型', '备注']
_SUPPLIER_ALIASES: dict[str, tuple[str, ...]] = {
    'name': ('供应商名称', '供应商', '单位名称', '名称', 'name', 'supplier'),
    'contact': ('联系人', 'contact'),
    'phone': ('电话', '手机', '联系电话', 'phone', 'tel'),
    'category': ('主营品类', '品类', '类别', 'category'),
    'default_invoice': ('默认发票类型', '发票类型', '票种', 'default_invoice'),
    'note': ('备注', '说明', 'note'),
}


@bp.route('/api/purchase/suppliers', methods=['GET'])
def list_suppliers():
    try:
        conn = _get_db()
        rows = _supplier_rows(conn, request.args.get('q') or '',
                              str(request.args.get('active_only') or '') == '1')
        conn.close()
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/suppliers', methods=['POST'])
def create_supplier():
    try:
        rec = _supplier_payload(request.get_json(silent=True) or {})
        conn = _get_db()
        try:
            cur = conn.execute(
                '''INSERT INTO suppliers (name, contact, phone, category,
                   default_invoice, note, active) VALUES (?,?,?,?,?,?,?)''',
                (rec['name'], rec['contact'], rec['phone'], rec['category'],
                 rec['default_invoice'], rec['note'], rec['active']))
            conn.commit()
            sid = cur.lastrowid
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'success': False, 'error': '该供应商名称已存在'}), 400
        row = _supplier_rows(conn, rec['name'])[0]
        conn.close()
        _log(f'新增供应商 id={sid} name={rec["name"]}')
        return jsonify({'success': True, 'data': row})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/purchase/suppliers/<int:sid>', methods=['POST'])
def update_supplier(sid: int):
    """编辑供应商；改名时可选同步历史采购记录（sync_history=1）"""
    try:
        data = request.get_json(silent=True) or {}
        rec = _supplier_payload(data)
        sync = str(data.get('sync_history', '')) in ('1', 'true', 'True')
        conn = _get_db()
        old = conn.execute('SELECT * FROM suppliers WHERE id=?', (sid,)).fetchone()
        if not old:
            conn.close()
            return jsonify({'success': False, 'error': '供应商不存在'}), 404
        try:
            conn.execute(
                '''UPDATE suppliers SET name=?, contact=?, phone=?, category=?,
                   default_invoice=?, note=?, active=? WHERE id=?''',
                (rec['name'], rec['contact'], rec['phone'], rec['category'],
                 rec['default_invoice'], rec['note'], rec['active'], sid))
            renamed = 0
            if sync and old['name'] != rec['name']:
                renamed = conn.execute(
                    'UPDATE purchases SET supplier=? WHERE supplier=?',
                    (rec['name'], old['name'])).rowcount
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'success': False, 'error': '该供应商名称已存在'}), 400
        row = _supplier_rows(conn, rec['name'])[0]
        conn.close()
        _log(f'编辑供应商 id={sid} name={rec["name"]} 同步历史={renamed}')
        return jsonify({'success': True, 'data': row, 'renamed': renamed})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/purchase/suppliers/<int:sid>', methods=['DELETE'])
def delete_supplier(sid: int):
    """已有采购记录的供应商不能删（只能停用），避免历史台账失去归属"""
    try:
        conn = _get_db()
        row = conn.execute('SELECT * FROM suppliers WHERE id=?', (sid,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': '供应商不存在'}), 404
        used = conn.execute('SELECT COUNT(*) c FROM purchases WHERE supplier=?',
                            (row['name'],)).fetchone()['c']
        if used:
            conn.close()
            return jsonify({'success': False,
                            'error': f'该供应商已有 {used} 笔采购记录，不能删除；请改为「停用」'}), 400
        conn.execute('DELETE FROM suppliers WHERE id=?', (sid,))
        conn.commit()
        conn.close()
        _log(f'删除供应商 id={sid} name={row["name"]}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/purchase/suppliers/template', methods=['GET'])
def supplier_template():
    """供应商导入模板：表头 + 一行示例（导入时自动跳过示例行）"""
    try:
        if not EXCEL_OK:
            return jsonify({'success': False,
                            'error': f'服务器缺少 openpyxl：{EXCEL_ERR}'}), 500
        wb = Workbook()
        ws = wb.active
        ws.title = '供应商'
        ws.append(_SUPPLIER_HEADERS)
        _style_header(ws, len(_SUPPLIER_HEADERS))
        ws.append(['示例供应商', '张老板', '13800000000', '生鲜', '专票', '示例行，导入前请删除'])
        ws.cell(row=2, column=1).font = Font(color='9CA3AF', italic=True)
        ws.append(['填写说明：只有「供应商名称」必填；默认发票类型只能填 无票(不抵)/专票/普票，'
                   '填错或不填会自动留空；重名的默认跳过，要覆盖已存在的请在导入时选「更新」。'])
        ws.cell(row=3, column=1).font = Font(color='6B7280', size=10)
        _set_widths(ws, [22, 12, 16, 14, 16, 30])
        return _xlsx_response(wb, '供应商导入模板.xlsx')
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/suppliers/import', methods=['POST'])
def import_suppliers():
    """导入供应商 Excel（.xlsx）

    - 按列名认列（顺序随意），至少要有「供应商名称」
    - mode=skip（默认）：重名跳过，不动已有档案；mode=update：重名则更新联系人/
      电话/主营品类/默认发票类型/备注（启用状态不改动）
    - 不会删除已有供应商，也不会动采购记录
    """
    try:
        if not EXCEL_OK:
            return jsonify({'success': False,
                            'error': f'服务器缺少 openpyxl，无法导入 Excel：{EXCEL_ERR}'}), 500
        f = request.files.get('file')
        if f is None or not f.filename:
            return jsonify({'success': False, 'error': '请选择要导入的 Excel 文件（.xlsx）'}), 400
        wb, err = _read_uploaded_workbook(f)
        if wb is None:
            return jsonify({'success': False, 'error': err}), 400
        rows = [tuple(r) for r in wb.active.iter_rows(values_only=True)][:5001]
        head_idx, colmap = _find_header(rows, _SUPPLIER_ALIASES, ('name',))
        if head_idx < 0:
            return jsonify({'success': False,
                            'error': '没认出表头（至少要有一列「供应商名称」），'
                                     '请先下载导入模板'}), 400
        mode = 'update' if str(request.form.get('mode', 'skip')) == 'update' else 'skip'
        conn = _get_db()
        added: list[int] = []
        updated: list[int] = []
        skipped: list[dict] = []
        errors: list[dict] = []

        for i, row in enumerate(rows[head_idx + 1:], start=head_idx + 2):
            def cell(field: str):
                idx = colmap.get(field)
                return row[idx] if idx is not None and idx < len(row) else None
            if not any(c is not None and str(c).strip() for c in row):
                continue
            name = _cell_text(cell('name'))[:100]
            if not name or name == '示例供应商':
                continue
            invoice = _cell_text(cell('default_invoice'))
            if invoice and invoice not in INVOICE_TYPES:
                invoice = ''                       # 票种写错就留空，不让整行失败
            rec = {
                'name': name,
                'contact': _cell_text(cell('contact'))[:50],
                'phone': _cell_text(cell('phone'))[:30],
                'category': _cell_text(cell('category'))[:50],
                'default_invoice': invoice,
                'note': _cell_text(cell('note'))[:200],
            }
            old = conn.execute('SELECT id FROM suppliers WHERE name=?', (name,)).fetchone()
            if old:
                if mode == 'skip':
                    skipped.append({'row': i, 'name': name, 'reason': '已存在，未改动'})
                    continue
                conn.execute(
                    '''UPDATE suppliers SET contact=?, phone=?, category=?,
                       default_invoice=?, note=? WHERE id=?''',
                    (rec['contact'], rec['phone'], rec['category'],
                     rec['default_invoice'], rec['note'], old['id']))
                updated.append(old['id'])
                continue
            cur = conn.execute(
                '''INSERT INTO suppliers (name, contact, phone, category,
                   default_invoice, note, active) VALUES (?,?,?,?,?,?,1)''',
                (rec['name'], rec['contact'], rec['phone'], rec['category'],
                 rec['default_invoice'], rec['note']))
            added.append(cur.lastrowid)
        conn.commit()
        conn.close()
        _log(f'导入供应商：新增 {len(added)} / 更新 {len(updated)} / '
             f'跳过 {len(skipped)} / 错误 {len(errors)}')
        return jsonify({'success': True, 'data': {
            'added': len(added), 'added_ids': added,
            'updated': len(updated), 'updated_ids': updated,
            'skipped': len(skipped), 'skipped_detail': skipped[:50],
            'errors': errors[:50], 'mode': mode,
        }})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/dicts', methods=['GET'])
def list_dicts():
    """品类 / 付款账户字典；kind=category|account，省略则一起返回"""
    try:
        kind = str(request.args.get('kind') or '').strip()
        conn = _get_db()
        if kind:
            if kind not in DICT_KINDS:
                conn.close()
                return jsonify({'success': False, 'error': '字典类型不合法'}), 400
            rows = [dict(r) for r in conn.execute(
                'SELECT * FROM purchase_dicts WHERE kind=? ORDER BY sort, name', (kind,))]
            conn.close()
            return jsonify({'success': True, 'data': rows})
        out = {k: [dict(r) for r in conn.execute(
            'SELECT * FROM purchase_dicts WHERE kind=? ORDER BY sort, name', (k,))]
            for k in DICT_KINDS}
        conn.close()
        return jsonify({'success': True, 'data': out})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/dicts', methods=['POST'])
def create_dict():
    try:
        data = request.get_json(silent=True) or {}
        kind = str(data.get('kind') or '').strip()
        name = str(data.get('name') or '').strip()[:50]
        if kind not in DICT_KINDS:
            raise ValueError('字典类型不合法')
        if not name:
            raise ValueError('名称不能为空')
        conn = _get_db()
        try:
            cur = conn.execute(
                'INSERT INTO purchase_dicts (kind, name, sort) VALUES (?,?,?)',
                (kind, name, int(data.get('sort') or 0)))
            conn.commit()
            did = cur.lastrowid
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'success': False, 'error': '该名称已存在'}), 400
        row = conn.execute('SELECT * FROM purchase_dicts WHERE id=?', (did,)).fetchone()
        conn.close()
        return jsonify({'success': True, 'data': dict(row)})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/purchase/dicts/<int:did>', methods=['POST'])
def update_dict(did: int):
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()[:50]
        if not name:
            raise ValueError('名称不能为空')
        conn = _get_db()
        if not conn.execute('SELECT id FROM purchase_dicts WHERE id=?', (did,)).fetchone():
            conn.close()
            return jsonify({'success': False, 'error': '条目不存在'}), 404
        try:
            conn.execute('UPDATE purchase_dicts SET name=?, sort=? WHERE id=?',
                         (name, int(data.get('sort') or 0), did))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'success': False, 'error': '该名称已存在'}), 400
        row = conn.execute('SELECT * FROM purchase_dicts WHERE id=?', (did,)).fetchone()
        conn.close()
        return jsonify({'success': True, 'data': dict(row)})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/purchase/dicts/<int:did>', methods=['DELETE'])
def delete_dict(did: int):
    try:
        conn = _get_db()
        cur = conn.execute('DELETE FROM purchase_dicts WHERE id=?', (did,))
        conn.commit()
        conn.close()
        if cur.rowcount == 0:
            return jsonify({'success': False, 'error': '条目不存在'}), 404
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/purchase/export', methods=['GET'])
def export_excel():
    """导出 Excel（.xlsx）：跟随当前筛选，列与原台账一致，带合计行"""
    try:
        if not EXCEL_OK:
            return jsonify({'success': False,
                            'error': f'服务器缺少 openpyxl，无法导出 Excel：{EXCEL_ERR}'}), 500
        where, params = _filters(request.args)
        conn = _get_db()
        rows = conn.execute(
            f"SELECT * FROM purchases WHERE {' AND '.join(where)} "
            f"ORDER BY date, id", params).fetchall()
        conn.close()
        items = [_with_derived(dict(r)) for r in rows]

        wb = Workbook()
        ws = wb.active
        ws.title = '采购台账'
        ws.append(EXPORT_HEADERS)
        _style_header(ws, len(EXPORT_HEADERS))
        for d in items:
            ws.append([d['date'], d['bill_no'], d['supplier'], d['category'],
                       d['summary'], d['invoice_type'], d['total_amount'],
                       d['input_tax'], d['net_cost'], d['invoiced_amount'],
                       d['paid_amount'], d['unpaid_balance'], d['pay_account'],
                       d['pay_status']])
        if items:
            _style_money(ws, 2, ws.max_row, _MONEY_IDX)
            ws.append(['合计', '', f'{len(items)} 笔', '', '', '',
                       sum(d['total_amount'] for d in items),
                       sum(d['input_tax'] for d in items),
                       sum(d['net_cost'] for d in items),
                       sum(d['invoiced_amount'] for d in items),
                       sum(d['paid_amount'] for d in items),
                       sum(d['unpaid_balance'] for d in items)])
            last = ws.max_row
            _style_money(ws, last, last, _MONEY_IDX)
            for c in range(1, len(EXPORT_HEADERS) + 1):
                ws.cell(row=last, column=c).font = Font(bold=True)
        _set_widths(ws, [12, 14, 20, 10, 28, 12, 18, 12, 13, 12, 12, 12, 12, 10])
        return _xlsx_response(wb, f'采购台账_{now_str()[:10]}.xlsx')
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/template', methods=['GET'])
def download_template():
    """导入模板：只有表头 + 一行示例（导入时会自动跳过示例行）"""
    try:
        if not EXCEL_OK:
            return jsonify({'success': False,
                            'error': f'服务器缺少 openpyxl：{EXCEL_ERR}'}), 500
        wb = Workbook()
        ws = wb.active
        ws.title = '采购台账'
        ws.append(EXPORT_HEADERS)
        _style_header(ws, len(EXPORT_HEADERS))
        ws.append(['2026-01-01', 'CG-0001', '示例供应商', '生鲜', '示例行，导入前请删除本行',
                   '专票', 1130.00, 130.00, 1000.00, 0, 1130.00, 0, '微信', '已付清'])
        ws.cell(row=2, column=3).font = Font(color='9CA3AF', italic=True)
        _style_money(ws, 2, 2, _MONEY_IDX)
        ws.append(['填写说明：日期写 2026-01-01 这种格式；金额只填数字，不要带 ¥ 和千分位；'
                   '不含税成本与未付余额是自动算的，填了也会被忽略。'])
        ws.cell(row=3, column=1).font = Font(color='6B7280', size=10)
        _set_widths(ws, [12, 14, 20, 10, 28, 12, 18, 12, 13, 12, 12, 12, 12, 10])
        return _xlsx_response(wb, '采购台账导入模板.xlsx')
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/api/purchase/import', methods=['POST'])
def import_excel():
    """导入 Excel（.xlsx）

    - 前 10 行里找表头（须能认出「日期 / 供应商 / 采购总金额」，列名支持常见别名）
    - 逐行校验，一行出错只跳过该行，不影响其余行
    - 重复判定：有单据号按单据号，没有就按 日期+供应商+金额 组合
    - auto_supplier=1（默认）时，新供应商自动建档到配置里
    """
    try:
        if not EXCEL_OK:
            return jsonify({'success': False,
                            'error': f'服务器缺少 openpyxl，无法导入 Excel：{EXCEL_ERR}'}), 500
        f = request.files.get('file')
        if f is None or not f.filename:
            return jsonify({'success': False, 'error': '请选择要导入的 Excel 文件（.xlsx）'}), 400
        wb, err = _read_uploaded_workbook(f)
        if wb is None:
            return jsonify({'success': False, 'error': err}), 400
        ws = wb.active
        rows = [tuple(r) for r in ws.iter_rows(values_only=True)][:5001]
        head_idx, colmap = _find_header(rows)
        if head_idx < 0:
            return jsonify({'success': False,
                            'error': '没认出表头（至少要能找到「日期」「供应商名称」「采购总金额」'
                                     '三列），请先下载导入模板'}), 400
        auto_supplier = str(request.form.get('auto_supplier', '1')) != '0'
        conn = _get_db()
        imported: list[int] = []
        skipped: list[dict] = []
        errors: list[dict] = []

        for i, row in enumerate(rows[head_idx + 1:], start=head_idx + 2):
            def cell(field: str):
                idx = colmap.get(field)
                return row[idx] if idx is not None and idx < len(row) else None
            if not any(c is not None and str(c).strip() for c in row):
                continue                                   # 空行
            data = {
                'date': _cell_date(cell('date')),
                'bill_no': _cell_text(cell('bill_no')),
                'supplier': _cell_text(cell('supplier')),
                'category': _cell_text(cell('category')),
                'summary': _cell_text(cell('summary')),
                'invoice_type': _cell_text(cell('invoice_type')),
                'pay_account': _cell_text(cell('pay_account')),
            }
            if data['supplier'] == '示例供应商':               # 模板自带的示例行
                continue
            try:
                data['total_amount'] = _cell_money(cell('total_amount'))
                data['input_tax'] = _cell_money(cell('input_tax'))
                data['invoiced_amount'] = _cell_money(cell('invoiced_amount'))
                data['paid_amount'] = _cell_money(cell('paid_amount'))
                rec = _validate(data)
            except ValueError as e:
                errors.append({'row': i, 'reason': str(e),
                               'supplier': data['supplier'] or '（空）'})
                continue

            # 重复判定
            if rec['bill_no']:
                dup = conn.execute(
                    'SELECT id FROM purchases WHERE bill_no=?', (rec['bill_no'],)).fetchone()
            else:
                dup = conn.execute(
                    '''SELECT id FROM purchases WHERE date=? AND supplier=?
                       AND total_amount=? AND paid_amount=?''',
                    (rec['date'], rec['supplier'], rec['total_amount'],
                     rec['paid_amount'])).fetchone()
            if dup:
                skipped.append({'row': i, 'id': dup['id'],
                                'supplier': rec['supplier'],
                                'reason': f"单据号 {rec['bill_no'] or '（无）'} 已存在"})
                continue

            cur = conn.execute(
                '''INSERT INTO purchases (date, bill_no, supplier, category, summary,
                   invoice_type, total_amount, input_tax, invoiced_amount,
                   paid_amount, pay_account) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (rec['date'], rec['bill_no'], rec['supplier'], rec['category'],
                 rec['summary'], rec['invoice_type'], rec['total_amount'],
                 rec['input_tax'], rec['invoiced_amount'], rec['paid_amount'],
                 rec['pay_account']))
            imported.append(cur.lastrowid)
            if auto_supplier and rec['supplier']:
                conn.execute(
                    'INSERT OR IGNORE INTO suppliers (name, active) VALUES (?, 1)',
                    (rec['supplier'],))
        conn.commit()
        conn.close()
        _log(f'导入 Excel：成功 {len(imported)} 行 / 跳过 {len(skipped)} 行 / '
             f'错误 {len(errors)} 行')
        return jsonify({'success': True, 'data': {
            'imported': len(imported), 'imported_ids': imported,
            'skipped': len(skipped), 'errors': errors[:50],
            'skipped_detail': skipped[:50],
        }})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/purchase')
@bp.route('/purchase/')
def purchase_index():
    resp = send_from_directory(DB_DIR, 'index.html')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

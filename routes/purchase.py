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
import math
import os
import re
import sqlite3
import sys
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

DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_EPS = 0.005          # 金额比较容差（分为单位以下忽略）


def _server_error(e: Exception) -> tuple:
    """统一错误出口

    ValueError 是用户输入问题 → 400 + 原样提示；其余算内部错误 → 记日志（带上
    出错函数名，线上才有得查）+ 500，且不把 sqlite 报错、路径之类的内部细节抛给用户。
    """
    if isinstance(e, ValueError):
        return jsonify({'success': False, 'error': str(e)}), 400
    try:
        fn = sys._getframe(1).f_code.co_name        # noqa: SLF001  取调用者函数名
    except Exception:
        fn = '?'
    _log(f'[{fn}] 服务器内部错误: {e!r}')
    return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500

# 发票类型（生鲜行业实际会碰到的票种）
#   rate = 默认税率/扣除率(%)，mode = 进项税怎么算：
#     'deduct'  价税分离（票面税额）：进项税 = 总额 ÷(1+率) × 率
#     'compute' 计算抵扣（农产品收购发票）：进项税 = 买价 × 扣除率
#     None      不自动算，税额手工填（老数据里的「专票」「普票」归到这里）
_INVOICE_DEFS: tuple[tuple[str, float, str | None], ...] = (
    ('无票(不抵)', 0.0, None),
    ('免税普票', 0.0, None),
    ('农产品收购发票', 9.0, 'compute'),
    ('普票1%', 1.0, 'deduct'),
    ('普票3%', 3.0, 'deduct'),
    ('专票9%', 9.0, 'deduct'),
    ('专票13%', 13.0, 'deduct'),
    ('专票(税额手填)', 0.0, None),
    ('普票(税额手填)', 0.0, None),
)
INVOICE_TYPES = tuple(n for n, _, _ in _INVOICE_DEFS)
_INVOICE_DEF: dict[str, tuple[float, str | None]] = {n: (r, m) for n, r, m in _INVOICE_DEFS}
# 老票种 → 新票种（init_db 里一次性迁移，税额不动）
_INVOICE_MIGRATE = {'专票': '专票(税额手填)', '普票': '普票(税额手填)'}

# 供应商结算方式（生鲜多为月结）
SETTLE_TYPES = ('现结', '周结', '月结', '压批结')

# 对账单抬头设置（我方公司信息 + 月结付款日 + 单据条款），存 purchase_settings
SETTING_KEYS = ('company_name', 'company_tax_no', 'company_address',
                'company_phone', 'settle_day', 'recon_note')
DEFAULT_SETTINGS = {
    'company_name': '',
    'company_tax_no': '',
    'company_address': '',
    'company_phone': '',
    'settle_day': '10',          # 月结：次月几号前付款
    'recon_note': '请核对无误后签字盖章回传，逾期 3 日未回复视为确认无误。',
}


def _due_date(end: str, settle: str, settle_day: str) -> str:
    """按结算方式算最迟付款日

    现结=期末当天 / 周结=期末+7天 / 月结=次月 settle_day 日 / 压批结=约定不清，返回空
    """
    if not end or not DATE_RE.match(end):
        return ''
    try:
        d = datetime.date.fromisoformat(end)
    except ValueError:
        return ''
    if settle == '现结':
        return d.isoformat()
    if settle == '周结':
        return (d + datetime.timedelta(days=7)).isoformat()
    if settle == '月结':
        try:
            day = max(1, min(28, int(str(settle_day or '10').strip() or 10)))
        except ValueError:
            day = 10
        y, m = d.year, d.month + 1
        if m > 12:
            y, m = y + 1, 1
        return datetime.date(y, m, day).isoformat()
    return ''


def _load_settings(conn=None) -> dict:
    """读取抬头设置：没有的键用默认值补齐"""
    own = conn is None
    conn = conn or _get_db()
    try:
        rows = conn.execute('SELECT key, value FROM purchase_settings').fetchall()
    except Exception:
        rows = []
    if own:
        conn.close()
    out = dict(DEFAULT_SETTINGS)
    out.update({r['key']: r['value'] for r in rows})
    return out


AMOUNT_MAX = 1e12          # 金额上限：挡住手滑多敲几个 0 和恶意超大值


def _num(val, default: float = 0.0) -> float:
    """宽松解析数字：容忍千分位逗号与空串

    float() 会接受 nan / inf / 1e400，这些值写进 SQLite 会变 NULL 或让 SUM()
    变成 Infinity（jsonify 直接报序列化错误），所以这里必须显式挡掉。
    """
    if val is None:
        return default
    s = str(val).strip().replace(',', '').replace('，', '')
    if not s:
        return default
    try:
        v = float(s)
    except ValueError:
        raise ValueError('金额格式不正确')
    if not math.isfinite(v):
        raise ValueError('金额不是有效数字')
    if abs(v) > AMOUNT_MAX:
        raise ValueError(f'金额超出上限（{AMOUNT_MAX:,.0f}）')
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
EXPORT_HEADERS = ['日期', '单据号', '供应商名称', '客户/门店', '品类', '采购内容摘要',
                  '发票类型', '采购总金额(含税/买价)', '进项税额', '不含税成本',
                  '退货金额', '扣款金额', '应付净额', '已开金额', '已付金额',
                  '未付余额', '付款账户', '付款状态']
# 1-based：金额列（不含税成本 / 应付净额 / 未付余额是派生列，导入时忽略）
_MONEY_IDX = (8, 9, 10, 11, 12, 13, 14, 15, 16)

# 导入时的列名别名（小写比较，中英文都认）
_COL_ALIASES: dict[str, tuple[str, ...]] = {
    'date': ('日期', '采购日期', 'date'),
    'bill_no': ('单据号', '单号', '票号', 'bill_no', 'billno'),
    'supplier': ('供应商名称', '供应商', '往来单位', 'supplier'),
    'customer': ('客户/门店', '客户', '门店', '送货客户', 'customer', 'client'),
    'category': ('品类', '类别', 'category'),
    'summary': ('采购内容摘要', '摘要', '内容', 'summary'),
    'invoice_type': ('发票类型', '票种', 'invoice_type'),
    'return_amount': ('退货金额', '退货', '退货款', '退金额', 'return_amount'),
    'deduct_amount': ('扣款金额', '扣款', '扣重', '扣水', '扣款/扣重', 'deduct_amount'),
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
    # 老浏览器不认 RFC5987 的 filename*，所以同时给一个纯英文兜底名
    ascii_name = re.sub(r'[^A-Za-z0-9._-]', '_', filename)
    if not re.search(r'[A-Za-z0-9]', filename.rsplit('.', 1)[0]):  # 纯中文名 → 通用兜底名
        ascii_name = 'export.xlsx'
    return Response(out.getvalue(), mimetype=XLSX_MIME,
                    headers={'Content-Disposition':
                             f"attachment; filename=\"{ascii_name}\"; "
                             f"filename*=UTF-8''{quoted}"})


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
    # 月/日补零：2026/10/1、2026年10月1日 归一化后是 2026-10-1，不补零过不了校验
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', s)
    if m:
        s = f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
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


def _calc_input_tax(total: float, invoice_type: str) -> float | None:
    """按票种自动算进项税；票种不支持自动算（无票 / 税额手填）时返回 None

    两种算法不一样，别混：
    - 专票 / 普票是价税合计，要价税分离：  税 = 总额 ÷(1+率) × 率
    - 农产品收购发票是计算抵扣：          税 = 买价 × 扣除率
    """
    d = _INVOICE_DEF.get(str(invoice_type or '').strip())
    if not d:
        return None
    rate, mode = d
    if rate <= 0 or not mode:
        return None
    if mode == 'deduct':
        return _round2(total / (1 + rate / 100) * (rate / 100))
    return _round2(total * rate / 100)          # compute：买价 × 扣除率


def _validate(data: dict) -> dict:
    """校验并规整一条采购记录，失败抛 ValueError（API 层转 400）

    税额规则：请求里没给 input_tax（空 / None）就按票种自动算；给了就以手填
    为准（票面税额和系统算的有几分差异时以票面为准）。
    """
    inv = str(data.get('invoice_type') or '无票(不抵)').strip()
    rec = {
        'date': str(data.get('date') or '').strip(),
        'bill_no': str(data.get('bill_no') or '').strip()[:50],
        'supplier': str(data.get('supplier') or '').strip()[:100],
        'category': str(data.get('category') or '').strip()[:50],
        'summary': str(data.get('summary') or '').strip()[:200],
        'invoice_type': _INVOICE_MIGRATE.get(inv, inv),   # 老票种 → 税额手填
        'pay_account': str(data.get('pay_account') or '').strip()[:50],
        'customer': str(data.get('customer') or '').strip()[:50],
    }
    if not DATE_RE.match(rec['date']):
        raise ValueError('日期格式应为 YYYY-MM-DD')
    if not rec['supplier']:
        raise ValueError('供应商名称不能为空')

    total = _round2(_num(data.get('total_amount')))
    raw_tax = data.get('input_tax')
    auto_tax = _calc_input_tax(total, rec['invoice_type'])
    if raw_tax is None or str(raw_tax).strip() == '':
        tax = auto_tax if auto_tax is not None else 0.0
    else:
        tax = _round2(_num(raw_tax))
    ret = _round2(_num(data.get('return_amount')))
    deduct = _round2(_num(data.get('deduct_amount')))
    invoiced = _round2(_num(data.get('invoiced_amount')))
    paid = _round2(_num(data.get('paid_amount')))
    if total <= 0:
        raise ValueError('采购总金额必须大于 0')
    if min(tax, ret, deduct, invoiced, paid) < 0:
        raise ValueError('金额不能为负数')
    if ret + deduct > total + _EPS:
        raise ValueError('退货金额 + 扣款金额不能大于采购总金额')
    if tax > total + _EPS:
        raise ValueError('进项税额不能大于采购总金额')
    if invoiced > total + _EPS:
        raise ValueError('已开金额不能大于采购总金额')
    payable = _round2(total - ret - deduct)
    if paid > payable + _EPS:
        raise ValueError('已付金额不能大于应付净额（采购额-退货-扣款）')

    rec['total_amount'] = total
    rec['input_tax'] = tax
    rec['return_amount'] = ret
    rec['deduct_amount'] = deduct
    rec['invoiced_amount'] = invoiced
    rec['paid_amount'] = paid
    if rec['invoice_type'] not in INVOICE_TYPES:
        rec['invoice_type'] = '无票(不抵)'
    return rec


def _with_derived(row: dict) -> dict:
    """补上派生字段：不含税成本 / 应付净额 / 未付余额 / 付款状态

    生鲜经常有不新鲜退货、扣水扣冰扣款，所以：
        应付净额 = 采购额 − 退货 − 扣款
        未付余额 = 应付净额 − 已付
    """
    total = row.get('total_amount') or 0.0
    ret = row.get('return_amount') or 0.0
    deduct = row.get('deduct_amount') or 0.0
    row['net_cost'] = _round2(total - (row.get('input_tax') or 0.0))
    row['payable'] = _round2(total - ret - deduct)
    row['unpaid_balance'] = _round2(row['payable'] - (row.get('paid_amount') or 0.0))
    unpaid = row['unpaid_balance']
    if unpaid <= _EPS:
        row['pay_status'] = '已付清'
    elif unpaid >= row['payable'] - _EPS:
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
            return_amount REAL DEFAULT 0,     -- 退货金额（不新鲜退回等）
            deduct_amount REAL DEFAULT 0,     -- 扣款金额（扣重 / 扣水 / 缺斤少两）
            customer TEXT DEFAULT '',         -- 客户 / 门店（按客户统计用）
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(date)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_purchases_supplier ON purchases(supplier)')
        # 筛选与分组（stats / 对账 / 分析）都会用到这几列
        for _col in ('category', 'customer', 'invoice_type', 'pay_account'):
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS idx_purchases_{_col} ON purchases({_col})')
        # 对账标记（老库补列）：recon_at 为空=未对账，否则是对账时间
        cols = [r[1] for r in conn.execute('PRAGMA table_info(purchases)').fetchall()]
        if 'recon_at' not in cols:
            conn.execute("ALTER TABLE purchases ADD COLUMN recon_at TEXT DEFAULT ''")
        # 退货 / 扣款 / 客户（老库补列）
        for col, ddl in (('return_amount', 'REAL DEFAULT 0'),
                         ('deduct_amount', 'REAL DEFAULT 0'),
                         ('customer', "TEXT DEFAULT ''")):
            if col not in cols:
                conn.execute(f'ALTER TABLE purchases ADD COLUMN {col} {ddl}')
        # 票种改造：老的「专票」「普票」没有税率信息，迁到「税额手填」，税额不动
        for old, new in _INVOICE_MIGRATE.items():
            conn.execute('UPDATE purchases SET invoice_type=? WHERE invoice_type=?', (new, old))
        # 基础配置：供应商档案
        conn.execute('''CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            code TEXT DEFAULT '',
            s_type TEXT DEFAULT '',
            market TEXT DEFAULT '',
            address TEXT DEFAULT '',
            contact TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            category TEXT DEFAULT '',
            default_invoice TEXT DEFAULT '',
            settle_type TEXT DEFAULT '',      -- 结算方式：现结 / 周结 / 月结 / 压批结
            note TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )''')
        # 供应商编码唯一（老库补列后建索引；空编码不参与唯一约束）
        scols = [r[1] for r in conn.execute('PRAGMA table_info(suppliers)').fetchall()]
        for col, ddl in (('code', "TEXT DEFAULT ''"), ('s_type', "TEXT DEFAULT ''"),
                         ('market', "TEXT DEFAULT ''"), ('address', "TEXT DEFAULT ''"),
                         ('settle_type', "TEXT DEFAULT ''")):
            if col not in scols:
                conn.execute(f'ALTER TABLE suppliers ADD COLUMN {col} {ddl}')
        conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_suppliers_code
                        ON suppliers(code) WHERE code <> '' ''')
        # 基础配置：品类 / 付款账户等通用字典（kind 区分）
        conn.execute('''CREATE TABLE IF NOT EXISTS purchase_dicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            sort INTEGER NOT NULL DEFAULT 0,
            UNIQUE(kind, name)
        )''')
        # 对账单抬头设置：我方公司信息 / 月结付款日 / 单据条款（key-value）
        conn.execute('''CREATE TABLE IF NOT EXISTS purchase_settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )''')
        conn.commit()
        conn.close()
        _log('purchase_db 初始化完成')
    except Exception as e:
        _log(f'purchase_db 初始化失败: {e}')
        raise


def _multi_values(raw, limit: int = 60) -> list[str]:
    """'A,B,C' → ['A','B','C']（去空去重、限个数），用于多选筛选"""
    out: list[str] = []
    seen: set[str] = set()
    for v in str(raw or '').split(','):
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
        if len(out) >= limit:
            break
    return out


def _filters(args) -> tuple[list[str], list]:
    where: list[str] = ['1=1']
    params: list = []
    start = str(args.get('start') or '').strip()
    end = str(args.get('end') or '').strip()
    status = str(args.get('status') or '').strip()
    search = str(args.get('search') or '').strip()
    # 日期填错要报错，不能静默忽略——否则条件整个不生效，返回的其实是「全量数据」，
    # 用户拿到一份口径完全不对的报表还以为是准的
    for _label, _v in (('开始日期', start), ('结束日期', end)):
        if _v and not DATE_RE.match(_v):
            raise ValueError(f'{_label}格式应为 YYYY-MM-DD')
    # 多选具体日期（dates=2026-10-06,2026-10-08）：挑几天对比时用，
    # 有值就忽略起止区间，免得区间把多选的日期又并进来
    dates = [d for d in _multi_values(args.get('dates'), 366) if DATE_RE.match(d)]
    if dates:
        where.append(f'date IN ({",".join("?" * len(dates))})')
        params.extend(dates)
    else:
        if start and DATE_RE.match(start):
            where.append('date >= ?')
            params.append(start)
        if end and DATE_RE.match(end):
            where.append('date <= ?')
            params.append(end)
    # 供应商 / 品类 / 票种 / 客户都支持多选（逗号分隔），单个值仍走等值
    for arg_key, col in (('supplier', 'supplier'), ('category', 'category'),
                         ('invoice', 'invoice_type'), ('customer', 'customer')):
        vals = _multi_values(args.get(arg_key))
        if len(vals) == 1:
            where.append(f'{col} = ?')
            params.append(vals[0])
        elif vals:
            where.append(f'{col} IN ({",".join("?" * len(vals))})')
            params.extend(vals)
    if search:
        where.append('(supplier LIKE ? OR category LIKE ? OR summary LIKE ? OR bill_no LIKE ? '
                     'OR pay_account LIKE ? OR customer LIKE ?)')
        like = f'%{search}%'
        params.extend([like] * 6)
    # 付款状态是派生字段，用 SQL 表达式过滤（应付净额 = 总额 - 退货 - 扣款）
    if status in ('未付款', '部分付款', '已付清'):
        payable = 'total_amount - return_amount - deduct_amount'
        unpaid_expr = f'({payable}) - paid_amount'
        if status == '未付款':
            where.append(f'{unpaid_expr} >= {payable} - 0.005')
        elif status == '已付清':
            where.append(f'{unpaid_expr} <= 0.005')
        else:
            where.append(f'{unpaid_expr} > 0.005 AND {unpaid_expr} < {payable} - 0.005')
    return where, params


@bp.route('/api/purchase/records', methods=['GET'])
def list_records():
    """列表；带 page/page_size 时分页，否则按 limit 返回（兼容旧调用）"""
    try:
        where, params = _filters(request.args)
        conn = _get_db()
        total = conn.execute(
            f"SELECT COUNT(*) c FROM purchases WHERE {' AND '.join(where)}",
            params).fetchone()['c']
        order = ' ORDER BY date DESC, id DESC'
        try:
            page = max(1, int(request.args.get('page', 1)))
            page_size = int(request.args.get('page_size', 0))
            # 负的 limit 在 SQLite 里等于「不限量」，会把整表灌进内存，必须钳下界
            limit = max(1, min(int(request.args.get('limit', 500)), 5000))
        except ValueError:
            return jsonify({'success': False, 'error': 'page/page_size/limit 必须是数字'}), 400
        if page_size > 0:                       # 分页模式
            page_size = min(page_size, 500)
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)             # 翻过头了就停在最后一页
            rows = conn.execute(
                f"SELECT * FROM purchases WHERE {' AND '.join(where)}{order} "
                'LIMIT ? OFFSET ?',
                (*params, page_size, (page - 1) * page_size)).fetchall()
        else:                                   # 兼容：一次性拉取
            pages = 1
            rows = conn.execute(
                f"SELECT * FROM purchases WHERE {' AND '.join(where)}{order} "
                'LIMIT ?', (*params, limit)).fetchall()
        conn.close()
        return jsonify({'success': True,
                        'data': [_with_derived(dict(r)) for r in rows],
                        'total': total, 'page': page, 'pages': pages,
                        'page_size': page_size or None})
    except Exception as e:
        return _server_error(e)


@bp.route('/api/purchase/records', methods=['POST'])
def create_record():
    try:
        rec = _validate(request.get_json(silent=True) or {})
        conn = _get_db()
        cur = conn.execute(
            '''INSERT INTO purchases (date, bill_no, supplier, category, summary,
               invoice_type, total_amount, input_tax, invoiced_amount,
               paid_amount, pay_account, return_amount, deduct_amount, customer)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (rec['date'], rec['bill_no'], rec['supplier'], rec['category'],
             rec['summary'], rec['invoice_type'], rec['total_amount'],
             rec['input_tax'], rec['invoiced_amount'], rec['paid_amount'],
             rec['pay_account'], rec['return_amount'], rec['deduct_amount'],
             rec['customer']))
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute('SELECT * FROM purchases WHERE id=?', (rid,)).fetchone()
        conn.close()
        _log(f'新增采购记录 id={rid} supplier={rec["supplier"]} total={rec["total_amount"]}')
        return jsonify({'success': True, 'data': _with_derived(dict(row))})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return _server_error(e)


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
               invoiced_amount=?, paid_amount=?, pay_account=?,
               return_amount=?, deduct_amount=?, customer=? WHERE id=?''',
            (rec['date'], rec['bill_no'], rec['supplier'], rec['category'],
             rec['summary'], rec['invoice_type'], rec['total_amount'],
             rec['input_tax'], rec['invoiced_amount'], rec['paid_amount'],
             rec['pay_account'], rec['return_amount'], rec['deduct_amount'],
             rec['customer'], rid))
        conn.commit()
        row = conn.execute('SELECT * FROM purchases WHERE id=?', (rid,)).fetchone()
        conn.close()
        return jsonify({'success': True, 'data': _with_derived(dict(row))})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return _server_error(e)


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
        return _server_error(e)


@bp.route('/api/purchase/stats', methods=['GET'])
def stats():
    """合计 + 按供应商 / 品类 / 发票类型汇总（与列表同一套筛选条件）"""
    try:
        where, params = _filters(request.args)
        w = ' AND '.join(where)
        conn = _get_db()
        agg = conn.execute(
            f'''SELECT COUNT(*) cnt, COALESCE(SUM(total_amount),0) total,
                COALESCE(SUM(input_tax),0) tax, COALESCE(SUM(invoiced_amount),0) invoiced,
                COALESCE(SUM(return_amount),0) ret, COALESCE(SUM(deduct_amount),0) deduct,
                COALESCE(SUM(total_amount-return_amount-deduct_amount),0) payable,
                COALESCE(SUM(paid_amount),0) paid,
                COALESCE(SUM(total_amount-return_amount-deduct_amount-paid_amount),0) unpaid
                FROM purchases WHERE {w}''', params).fetchone()
        by_supplier = conn.execute(
            f'''SELECT supplier, COUNT(*) cnt, COALESCE(SUM(total_amount),0) total,
                COALESCE(SUM(input_tax),0) tax, COALESCE(SUM(invoiced_amount),0) invoiced,
                COALESCE(SUM(return_amount),0) ret, COALESCE(SUM(deduct_amount),0) deduct,
                COALESCE(SUM(total_amount-return_amount-deduct_amount),0) payable,
                COALESCE(SUM(paid_amount),0) paid,
                COALESCE(SUM(total_amount-return_amount-deduct_amount-paid_amount),0) unpaid
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
        by_customer = conn.execute(
            f'''SELECT customer, COUNT(*) cnt, COALESCE(SUM(total_amount),0) total,
                COALESCE(SUM(input_tax),0) tax,
                COALESCE(SUM(total_amount-return_amount-deduct_amount),0) payable,
                COALESCE(SUM(paid_amount),0) paid,
                COALESCE(SUM(total_amount-return_amount-deduct_amount-paid_amount),0) unpaid
                FROM purchases WHERE {w} GROUP BY customer
                ORDER BY total DESC LIMIT 50''', params).fetchall()
        conn.close()
        return jsonify({'success': True, 'data': {
            'count': agg['cnt'],
            'total': _round2(agg['total']),
            'tax': _round2(agg['tax']),
            'net_cost': _round2(agg['total'] - agg['tax']),
            'returned': _round2(agg['ret']),
            'deducted': _round2(agg['deduct']),
            'payable': _round2(agg['payable']),
            'invoiced': _round2(agg['invoiced']),
            'paid': _round2(agg['paid']),
            'unpaid': _round2(agg['unpaid']),
            'by_supplier': [_round_summary(dict(r)) for r in by_supplier],
            'by_category': [_round_summary(dict(r)) for r in by_category],
            'by_invoice': [_round_summary(dict(r)) for r in by_invoice],
            'by_customer': [_round_summary(dict(r)) for r in by_customer],
        }})
    except Exception as e:
        return _server_error(e)


# ==================== 趋势统计 / 对账报表 ====================

_GROUP_EXPR = {
    'year': "substr(date, 1, 4)",     # 2026
    'month': "substr(date, 1, 7)",    # 2026-10
    'day': "date",                    # 2026-10-18
}


# 对账单的两种「多维度」：
#   1) 筛选维度：供应商 / 品类 / 客户 / 票种 / 付款账户 / 付款状态 / 关键词（和台账口径一致）
#   2) 汇总维度：对账单里按哪个维度出分组小计
RECON_DIMS = {
    'day': ('按日期', 'date'),
    'supplier': ('按供应商', 'supplier'),
    'category': ('按品类', 'category'),
    'customer': ('按客户/门店', 'customer'),
    'invoice': ('按发票类型', 'invoice_type'),
    'month': ('按月份', "substr(date,1,7)"),
}
# 日期类维度按时间正序（对账单要顺着日期看），其余按金额从大到小
RECON_DIM_ORDER = {'day': 'name ASC', 'month': 'name ASC'}
RECON_FILTER_KEYS = ('supplier', 'category', 'customer', 'invoice',
                     'pay_account', 'status', 'search')


def _recon_filters(args, before: bool = False) -> tuple[list[str], list]:
    """对账筛选：日期 + 供应商 / 品类 / 客户 / 票种 / 账户 / 状态 / 关键词

    before=True 时日期条件换成 `date < start`（算「期初未付」用），其余维度照旧，
    保证期初与本期口径一致；start 为空时不该调用（会查到全部记录，与本期重复）。
    """
    where: list[str] = ['1=1']
    params: list = []
    start = str(args.get('start') or '').strip()
    end = str(args.get('end') or '').strip()
    for _label, _v in (('开始日期', start), ('结束日期', end)):
        if _v and not DATE_RE.match(_v):
            raise ValueError(f'{_label}格式应为 YYYY-MM-DD')
    if start and DATE_RE.match(start):
        where.append('date < ?' if before else 'date >= ?')
        params.append(start)
    if end and DATE_RE.match(end) and not before:
        where.append('date <= ?')
        params.append(end)
    for arg_key, col in (('supplier', 'supplier'), ('category', 'category'),
                         ('customer', 'customer'), ('invoice', 'invoice_type'),
                         ('pay_account', 'pay_account')):
        v = str(args.get(arg_key) or '').strip()
        if v:
            where.append(f'{col} = ?')
            params.append(v)
    search = str(args.get('search') or '').strip()
    if search:
        where.append('(supplier LIKE ? OR category LIKE ? OR summary LIKE ? OR bill_no LIKE ? '
                     'OR pay_account LIKE ? OR customer LIKE ?)')
        params.extend([f'%{search}%'] * 6)
    # 付款状态是派生字段，用 SQL 表达式过滤（应付净额 = 总额 - 退货 - 扣款）
    status = str(args.get('status') or '').strip()
    if status in ('未付款', '部分付款', '已付清'):
        payable = 'total_amount - return_amount - deduct_amount'
        unpaid_expr = f'({payable}) - paid_amount'
        if status == '未付款':
            where.append(f'{unpaid_expr} >= {payable} - 0.005')
        elif status == '已付清':
            where.append(f'{unpaid_expr} <= 0.005')
        else:
            where.append(f'{unpaid_expr} > 0.005 AND {unpaid_expr} < {payable} - 0.005')
    return where, params


def _recon_active_filters(args) -> dict:
    """当前生效的筛选维度（打印到对账单上，说明这单是按什么口径出的）"""
    return {k: str(args.get(k) or '').strip() for k in RECON_FILTER_KEYS}


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
                COALESCE(SUM(total_amount-return_amount-deduct_amount),0) payable,
                COALESCE(SUM(total_amount-return_amount-deduct_amount-paid_amount),0) unpaid
                FROM purchases WHERE {w} GROUP BY period ORDER BY period DESC''',
            params).fetchall()
        conn.close()
        data = [_round_summary(dict(r)) for r in rows]
        return jsonify({'success': True, 'data': {
            'group': group,
            'rows': data,
            'count': sum(r['cnt'] for r in data),
            'total': _round2(sum(r['total'] for r in data)),
            'payable': _round2(sum(r['payable'] for r in data)),
            'unpaid': _round2(sum(r['unpaid'] for r in data)),
        }})
    except Exception as e:
        return _server_error(e)


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
        where, params = _recon_filters(request.args)
        w = ' AND '.join(where)
        conn = _get_db()
        rows = conn.execute(
            f'SELECT * FROM purchases WHERE {w} ORDER BY date, id', params).fetchall()
        items = [_with_derived(dict(r)) for r in rows]

        agg = conn.execute(
            f'''SELECT COUNT(*) cnt, COALESCE(SUM(total_amount),0) total,
                COALESCE(SUM(input_tax),0) tax, COALESCE(SUM(invoiced_amount),0) invoiced,
                COALESCE(SUM(return_amount),0) ret, COALESCE(SUM(deduct_amount),0) deduct,
                COALESCE(SUM(total_amount-return_amount-deduct_amount),0) payable,
                COALESCE(SUM(paid_amount),0) paid,
                COALESCE(SUM(total_amount-return_amount-deduct_amount-paid_amount),0) unpaid,
                COALESCE(SUM(CASE WHEN recon_at<>"" THEN 1 ELSE 0 END),0) recon_cnt
                FROM purchases WHERE {w}''', params).fetchone()
        by_invoice = conn.execute(
            f'''SELECT invoice_type, COUNT(*) cnt, COALESCE(SUM(total_amount),0) total
                FROM purchases WHERE {w} GROUP BY invoice_type ORDER BY total DESC''',
            params).fetchall()
        by_month = conn.execute(
            f'''SELECT substr(date,1,7) AS period, COUNT(*) cnt,
                COALESCE(SUM(total_amount),0) total,
                COALESCE(SUM(total_amount-return_amount-deduct_amount),0) payable,
                COALESCE(SUM(paid_amount),0) paid,
                COALESCE(SUM(total_amount-return_amount-deduct_amount-paid_amount),0) unpaid
                FROM purchases WHERE {w} GROUP BY period ORDER BY period''',
            params).fetchall()

        # 期初未付：本期开始日之前的历史欠款，往来对账要从上期结转开始。
        # 用的是同一套筛选维度（供应商/品类/客户…），只有日期条件不同。
        opening_unpaid = 0.0
        opening_cnt = 0
        start_arg = str(request.args.get('start') or '').strip()
        if start_arg and DATE_RE.match(start_arg):
            ow, op = _recon_filters(request.args, before=True)
            op_row = conn.execute(
                f'''SELECT COUNT(*) cnt,
                    COALESCE(SUM(total_amount-return_amount-deduct_amount-paid_amount),0) unpaid
                    FROM purchases WHERE {' AND '.join(ow)}''', op).fetchone()
            opening_unpaid = op_row['unpaid']
            opening_cnt = op_row['cnt']

        # 汇总维度：对账单里按哪个维度出分组小计（group_by=none 表示不分组）
        # 明细粒度 detail=day（按日汇总出单）时强制按日期分组
        group_key = str(request.args.get('group_by') or 'supplier').strip()
        if str(request.args.get('detail') or '').strip() == 'day':
            group_key = 'day'
        by_group: list[dict] = []
        if group_key in RECON_DIMS:
            expr = RECON_DIMS[group_key][1]
            order = RECON_DIM_ORDER.get(group_key, 'total DESC')
            by_group = [dict(r) for r in conn.execute(
                f'''SELECT {expr} AS name, COUNT(*) cnt,
                    COALESCE(SUM(total_amount),0) total,
                    COALESCE(SUM(input_tax),0) tax,
                    COALESCE(SUM(return_amount),0) ret,
                    COALESCE(SUM(deduct_amount),0) deduct,
                    COALESCE(SUM(total_amount-return_amount-deduct_amount),0) payable,
                    COALESCE(SUM(paid_amount),0) paid,
                    COALESCE(SUM(total_amount-return_amount-deduct_amount-paid_amount),0) unpaid
                    FROM purchases WHERE {w} GROUP BY name ORDER BY {order}''',
                params).fetchall()]

        # 该供应商全部历史的未付余额（对账看的是「还欠多少」，不只看本期）
        out_where, out_params = ['1=1'], []
        if supplier:
            out_where.append('supplier = ?')
            out_params.append(supplier)
        outstanding = conn.execute(
            f'''SELECT COALESCE(SUM(total_amount),0) total, COALESCE(SUM(paid_amount),0) paid,
                COALESCE(SUM(total_amount-return_amount-deduct_amount),0) payable,
                COALESCE(SUM(total_amount-return_amount-deduct_amount-paid_amount),0) unpaid,
                COUNT(*) cnt
                FROM purchases WHERE {' AND '.join(out_where)}''', out_params).fetchone()

        profile = None
        if supplier:
            p = conn.execute('SELECT * FROM suppliers WHERE name=?', (supplier,)).fetchone()
            profile = dict(p) if p else None
        company = _load_settings(conn)
        conn.close()

        total = _round2(agg['total'])
        invoiced = _round2(agg['invoiced'])
        payable = _round2(agg['payable'])
        summary = {
            'count': agg['cnt'],
            'total': total,
            'tax': _round2(agg['tax']),
            'net_cost': _round2(total - agg['tax']),
            'returned': _round2(agg['ret']),             # 退货
            'deducted': _round2(agg['deduct']),          # 扣款（扣重/扣水等）
            'payable': payable,                          # 应付净额 = 采购额-退货-扣款
            'invoiced': invoiced,
            'uninvoiced': _round2(total - invoiced),     # 待开票
            'paid': _round2(agg['paid']),
            'unpaid': _round2(payable - agg['paid']),
            'recon_cnt': agg['recon_cnt'],
            'unrecon_cnt': agg['cnt'] - agg['recon_cnt'],
        }
        end_arg = str(request.args.get('end') or '').strip()
        unpaid_now = _round2(payable - agg['paid'])
        settle = (profile or {}).get('settle_type') or ''
        return jsonify({'success': True, 'data': {
            'supplier': supplier,
            'profile': profile,
            'company': company,
            'start': start_arg,
            'end': end_arg,
            'settle_type': settle,
            'due_date': _due_date(end_arg, settle, company.get('settle_day', '10')),
            'generated_at': now_str(),
            'items': items,
            'summary': summary,
            'opening': {'count': opening_cnt, 'unpaid': _round2(opening_unpaid)},
            'closing': _round2(opening_unpaid + unpaid_now),
            'group_by': group_key,
            'by_group': [_round_summary(r) for r in by_group],
            'filters': _recon_active_filters(request.args),
            'by_invoice': [_round_summary(dict(r)) for r in by_invoice],
            'by_month': [_round_summary(dict(r)) for r in by_month],
            'outstanding': {
                'count': outstanding['cnt'],
                'total': _round2(outstanding['total']),
                'payable': _round2(outstanding['payable']),
                'paid': _round2(outstanding['paid']),
                'unpaid': _round2(outstanding['unpaid']),
            },
        }})
    except Exception as e:
        return _server_error(e)


@bp.route('/api/purchase/reconcile/export', methods=['GET'])
def reconcile_export():
    """导出对账单 xlsx：抬头 + 分组小计 + 逐笔明细 + 往来三段 + 签章行"""
    try:
        supplier = str(request.args.get('supplier') or '').strip()
        where, params = _recon_filters(request.args)
        w = ' AND '.join(where)
        conn = _get_db()
        rows = conn.execute(
            f'SELECT * FROM purchases WHERE {w} ORDER BY date, id', params).fetchall()
        # 汇总维度小计（与页面上的 group_by 一致）
        # 明细粒度：full=逐笔 / day=按日汇总 / none=只出汇总
        detail = str(request.args.get('detail') or 'full').strip()
        group_key = str(request.args.get('group_by') or 'supplier').strip()
        if detail == 'day':
            group_key = 'day'
        by_group: list[dict] = []
        if group_key in RECON_DIMS:
            expr = RECON_DIMS[group_key][1]
            order = RECON_DIM_ORDER.get(group_key, 'total DESC')
            by_group = [dict(r) for r in conn.execute(
                f'''SELECT {expr} AS name, COUNT(*) cnt,
                    COALESCE(SUM(total_amount),0) total,
                    COALESCE(SUM(return_amount),0) ret,
                    COALESCE(SUM(deduct_amount),0) deduct,
                    COALESCE(SUM(total_amount-return_amount-deduct_amount),0) payable,
                    COALESCE(SUM(paid_amount),0) paid,
                    COALESCE(SUM(total_amount-return_amount-deduct_amount-paid_amount),0) unpaid
                    FROM purchases WHERE {w} GROUP BY name ORDER BY {order}''',
                params).fetchall()]
        settle = ''
        if supplier:
            p = conn.execute('SELECT settle_type FROM suppliers WHERE name=?',
                             (supplier,)).fetchone()
            settle = (p['settle_type'] if p else '') or ''
        conn.close()
        items = [_with_derived(dict(r)) for r in rows]

        if not EXCEL_OK:
            return jsonify({'success': False,
                            'error': f'服务器缺少 openpyxl，无法导出 Excel：{EXCEL_ERR}'}), 500
        wb = Workbook()
        ws = wb.active
        ws.title = '对账单'
        company = _load_settings()
        end_arg = str(request.args.get('end') or '').strip()
        start_arg = str(request.args.get('start') or '').strip()
        due = _due_date(end_arg, settle, company.get('settle_day', '10'))
        ws.append([company.get('company_name') or '采购对账单', '', '', '', '', '采购对账单'])
        ws['A1'].font = Font(bold=True, size=14)
        ws['F1'].font = Font(bold=True, size=14)
        if company.get('company_tax_no'):
            ws.append(['税号', company['company_tax_no'], '', '地址',
                       company.get('company_address', ''), '电话', company.get('company_phone', '')])
        ws.append(['致（供应商）', supplier or '（全部供应商）', '', '生成时间', now_str()])
        ws.append(['对账期间', f"{start_arg or '最早'} 至 {end_arg or '最新'}",
                   '', '结算方式', settle or '未设置', '最迟付款日', due or '按约定'])
        # 多维度筛选：把生效的口径写进单据，避免双方对不上数
        active = {k: v for k, v in _recon_active_filters(request.args).items() if v}
        if active:
            ws.append(['筛选口径', ' / '.join(f'{k}={v}' for k, v in active.items())])
        ws.append([])
        if detail == 'day':                     # 按日汇总出单：一行一天
            ws.append(['日期', '笔数', '采购总金额', '退货金额', '扣款金额',
                       '应付净额', '已付金额', '未付余额'])
            head = ws.max_row
            _style_header(ws, 8, head)
            for g in by_group:
                ws.append([g['name'] or '（未填）', g['cnt'], g['total'], g['ret'],
                           g['deduct'], g['payable'], g['paid'], g['unpaid']])
            _style_money(ws, head + 1, ws.max_row, (3, 4, 5, 6, 7, 8))
        elif detail == 'none':                  # 只给汇总，不列明细
            ws.append(['（本单按汇总出单，未附明细）'])
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
        else:                                   # 逐笔明细
            ws.append(['日期', '单据号', '品类', '客户/门店', '采购内容摘要', '发票类型',
                       '采购总金额', '进项税额', '不含税成本', '退货金额', '扣款金额',
                       '应付净额', '已开金额', '已付金额', '未付余额', '付款账户',
                       '付款状态', '已对账'])
            head = ws.max_row
            _style_header(ws, 18, head)
            for d in items:
                ws.append([d['date'], d['bill_no'], d['category'], d['customer'],
                           d['summary'], d['invoice_type'], d['total_amount'],
                           d['input_tax'], d['net_cost'], d['return_amount'],
                           d['deduct_amount'], d['payable'], d['invoiced_amount'],
                           d['paid_amount'], d['unpaid_balance'], d['pay_account'],
                           d['pay_status'], '是' if d['reconciled'] else '否'])
            _style_money(ws, head + 1, ws.max_row, (7, 8, 9, 10, 11, 12, 13, 14, 15))
        ws.append([])
        ws.append(['合计', '', '', f'{len(items)} 笔', '', '',
                   sum(d['total_amount'] for d in items),
                   sum(d['input_tax'] for d in items),
                   sum(d['net_cost'] for d in items),
                   sum(d['return_amount'] for d in items),
                   sum(d['deduct_amount'] for d in items),
                   sum(d['payable'] for d in items),
                   sum(d['invoiced_amount'] for d in items),
                   sum(d['paid_amount'] for d in items),
                   sum(d['unpaid_balance'] for d in items)])
        last = ws.max_row
        _style_money(ws, last, last, (7, 8, 9, 10, 11, 12, 13, 14, 15))
        # 汇总维度小计（多维度对账时按品类/客户等分组看）；按日出单时明细本身就是小计，不重复
        dim_label = RECON_DIMS.get(group_key, ('', ''))[0]
        if by_group and detail != 'day':
            ws.append([f'{dim_label}小计', '笔数', '采购额', '退货', '扣款', '应付净额', '已付', '未付'])
            gh = ws.max_row
            for g in by_group:
                ws.append([g['name'] or '（未填）', g['cnt'], g['total'], g['ret'],
                           g['deduct'], g['payable'], g['paid'], g['unpaid']])
                _style_money(ws, ws.max_row, ws.max_row, (3, 4, 5, 6, 7, 8))
            ws.cell(row=gh, column=1).font = Font(bold=True)
            ws.append([])

        # 往来三段：期初未付 + 本期应付 - 本期已付 = 期末未付
        opening_unpaid = 0.0
        if start_arg and DATE_RE.match(start_arg):
            ow, op = _recon_filters(request.args, before=True)
            conn = _get_db()
            opening_unpaid = conn.execute(
                f'''SELECT COALESCE(SUM(total_amount-return_amount-deduct_amount-paid_amount),0) u
                    FROM purchases WHERE {' AND '.join(ow)}''', op).fetchone()['u']
            conn.close()
        period_unpaid = sum(d['unpaid_balance'] for d in items)
        ws.append(['期初未付（上期结转）', _round2(opening_unpaid)])
        ws.append(['本期未付', _round2(period_unpaid)])
        ws.append(['期末未付（应付合计）', _round2(opening_unpaid + period_unpaid)])
        for r in range(ws.max_row - 2, ws.max_row + 1):
            ws.cell(row=r, column=1).font = Font(bold=True)
        ws.append([company.get('recon_note', '')])
        ws.append(['供应商签章：', '', '', '', '财务审核：', '', '', '制单：'])
        for c in range(1, 19):
            ws.cell(row=last, column=c).font = Font(bold=True)
        _set_widths(ws, [12, 14, 10, 12, 26, 14, 13, 12, 13, 11, 11, 13, 12, 12, 12, 12, 10, 8])
        name = (f"对账单_{supplier or '全部'}_{request.args.get('start') or ''}_"
                f"{request.args.get('end') or ''}.xlsx")
        return _xlsx_response(wb, name)
    except Exception as e:
        return _server_error(e)


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
        return _server_error(e)


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
        customers = [r[0] for r in conn.execute(
            "SELECT name FROM purchase_dicts WHERE kind='customer' ORDER BY sort, name")]
        historic_customers = [r[0] for r in conn.execute(
            "SELECT DISTINCT customer FROM purchases WHERE customer<>''")]
        customers += [n for n in historic_customers if n not in customers]
        conn.close()
        return jsonify({'success': True, 'data': {
            'suppliers': suppliers, 'extra_suppliers': extra,
            'categories': categories, 'accounts': accounts,
            'customers': customers,
            'invoice_types': list(INVOICE_TYPES),
            'invoice_defs': [{'name': n, 'rate': r, 'mode': m or ''}
                             for n, r, m in _INVOICE_DEFS],
            'settle_types': list(SETTLE_TYPES)}})
    except Exception as e:
        return _server_error(e)


@bp.route('/api/purchase/settings', methods=['GET'])
def get_settings():
    """对账单抬头设置：我方公司信息 / 月结付款日 / 单据条款"""
    try:
        return jsonify({'success': True, 'data': _load_settings()})
    except Exception as e:
        return _server_error(e)


@bp.route('/api/purchase/settings', methods=['POST'])
def save_settings():
    """保存抬头设置：只认 SETTING_KEYS 里的键，其余忽略（不报 400）"""
    try:
        data = request.get_json(silent=True) or {}
        conn = _get_db()
        for k in SETTING_KEYS:
            if k not in data:
                continue
            v = str(data.get(k) or '').strip()[:200]
            conn.execute('''INSERT INTO purchase_settings(key, value) VALUES(?,?)
                            ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (k, v))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'data': _load_settings()})
    except Exception as e:
        return _server_error(e)


# ==================== 基础配置 ====================

DICT_KINDS = ('category', 'account', 'customer')


def _supplier_rows(conn, keyword: str = '', active_only: bool = False,
                   sid: int | None = None) -> list[dict]:
    """供应商档案 + 每家的采购统计（笔数 / 总额 / 未付）

    sid 用于精确取某一行（新增/编辑后回查）；keyword 是 LIKE 模糊匹配，
    同名前缀的供应商可能被一起匹配到，不能靠它取唯一结果。
    """
    where: list[str] = ['1=1']
    params: list = []
    if sid:
        where.append('s.id = ?')
        params.append(sid)
    if active_only:
        where.append('active=1')
    keyword = keyword.strip()
    if keyword:
        where.append('(s.name LIKE ? OR s.code LIKE ? OR s.s_type LIKE ? OR s.market LIKE ? '
                     'OR s.address LIKE ? OR s.contact LIKE ? OR s.phone LIKE ? '
                     'OR s.category LIKE ?)')
        like = f'%{keyword}%'
        params.extend([like] * 8)
    sql = (
        'SELECT s.*, '
        '(SELECT COUNT(*) FROM purchases p WHERE p.supplier = s.name) used_cnt, '
        'COALESCE((SELECT SUM(p.total_amount) FROM purchases p WHERE p.supplier = s.name), 0) used_total, '
        # 未付口径必须和台账/对账单一致：总额 − 退货 − 扣款 − 已付
        'COALESCE((SELECT SUM(p.total_amount - p.return_amount - p.deduct_amount '
        '- p.paid_amount) FROM purchases p WHERE p.supplier = s.name), 0) used_unpaid '
        f"FROM suppliers s WHERE {' AND '.join(where)} "
        'ORDER BY s.active DESC, s.name')
    return [_round_supplier(dict(r)) for r in conn.execute(sql, params).fetchall()]


def _round_summary(row: dict) -> dict:
    """把分组汇总里的金额统一保留两位，并补出不含税成本"""
    for k in ('total', 'tax', 'invoiced', 'paid', 'unpaid', 'ret', 'deduct', 'payable'):
        if k in row:
            row[k] = _round2(row[k] or 0.0)
    if 'total' in row and 'tax' in row:
        row['net_cost'] = _round2((row['total'] or 0.0) - (row['tax'] or 0.0))
    return row


def _round_supplier(row: dict) -> dict:
    row['used_total'] = _round2(row.get('used_total') or 0.0)
    row['used_unpaid'] = _round2(row.get('used_unpaid') or 0.0)
    return row


def _next_supplier_code(conn) -> str:
    """自动生成供应商编码：GYS001 起，接现有最大号 +1（编码留空时用）"""
    row = conn.execute(
        "SELECT MAX(CAST(substr(code,4) AS INTEGER)) m FROM suppliers "
        "WHERE code GLOB 'GYS[0-9]*'").fetchone()
    return f'GYS{(row["m"] or 0) + 1:03d}'


def _supplier_payload(data: dict) -> dict:
    """校验供应商字段，失败抛 ValueError"""
    rec = {
        'name': str(data.get('name') or '').strip()[:100],
        'code': str(data.get('code') or '').strip()[:30],
        's_type': str(data.get('s_type') or '').strip()[:20],
        'market': str(data.get('market') or '').strip()[:50],
        'address': str(data.get('address') or '').strip()[:100],
        'contact': str(data.get('contact') or '').strip()[:50],
        'phone': str(data.get('phone') or '').strip()[:30],
        'category': str(data.get('category') or '').strip()[:50],
        'default_invoice': str(data.get('default_invoice') or '').strip(),
        'settle_type': str(data.get('settle_type') or '').strip()[:10],
        'note': str(data.get('note') or '').strip()[:200],
        'active': 1 if str(data.get('active', 1)) not in ('0', 'false', 'False', '') else 0,
    }
    if not rec['name']:
        raise ValueError('供应商名称不能为空')
    rec['default_invoice'] = _INVOICE_MIGRATE.get(rec['default_invoice'],
                                                  rec['default_invoice'])
    if rec['default_invoice'] and rec['default_invoice'] not in INVOICE_TYPES:
        raise ValueError('默认发票类型不合法')
    if rec['settle_type'] and rec['settle_type'] not in SETTLE_TYPES:
        raise ValueError('结算方式只能是：' + ' / '.join(SETTLE_TYPES))
    return rec


# 供应商档案导入用的列名（与采购记录的表分开）
_SUPPLIER_HEADERS = ['供应商编码', '供应商名称', '供应商类型', '批发市场名称', '经营地址',
                     '法人/负责人', '联系方式', '主营品类', '默认发票类型', '结算方式', '备注']
_SUPPLIER_ALIASES: dict[str, tuple[str, ...]] = {
    'code': ('供应商编码', '编码', '编号', 'code'),
    'name': ('供应商名称', '供应商', '单位名称', '名称', 'name', 'supplier'),
    's_type': ('供应商类型', '类型', 's_type'),
    'market': ('批发市场名称', '批发市场', '市场', 'market'),
    'address': ('经营地址', '地址', 'address'),
    'contact': ('法人/负责人', '法人', '负责人', '联系人', 'contact'),
    'phone': ('联系方式', '电话', '手机', '联系电话', 'phone', 'tel'),
    'category': ('主营品类', '品类', '类别', 'category'),
    'default_invoice': ('默认发票类型', '发票类型', '票种', 'default_invoice'),
    'settle_type': ('结算方式', '结账方式', '账期', '结算', 'settle_type'),
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
        return _server_error(e)


@bp.route('/api/purchase/suppliers', methods=['POST'])
def create_supplier():
    try:
        rec = _supplier_payload(request.get_json(silent=True) or {})
        conn = _get_db()
        if not rec['code']:
            rec['code'] = _next_supplier_code(conn)      # 编码留空自动生成
        elif conn.execute('SELECT id FROM suppliers WHERE code=?',
                          (rec['code'],)).fetchone():
            conn.close()
            return jsonify({'success': False,
                            'error': f'供应商编码 {rec["code"]} 已被其它供应商使用'}), 400
        try:
            cur = conn.execute(
                '''INSERT INTO suppliers (name, code, s_type, market, address,
                   contact, phone, category, default_invoice, settle_type, note, active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (rec['name'], rec['code'], rec['s_type'], rec['market'],
                 rec['address'], rec['contact'], rec['phone'], rec['category'],
                 rec['default_invoice'], rec['settle_type'], rec['note'],
                 rec['active']))
            conn.commit()
            sid = cur.lastrowid
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'success': False, 'error': '该供应商名称已存在'}), 400
        row = _supplier_rows(conn, rec['name'], sid=sid)[0]   # 按 id 精确回查，别用名称模糊匹配
        conn.close()
        _log(f'新增供应商 id={sid} name={rec["name"]}')
        return jsonify({'success': True, 'data': row})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return _server_error(e)


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
        if rec['code'] and rec['code'] != old['code'] and conn.execute(
                'SELECT id FROM suppliers WHERE code=?', (rec['code'],)).fetchone():
            conn.close()
            return jsonify({'success': False,
                            'error': f'供应商编码 {rec["code"]} 已被其它供应商使用'}), 400
        try:
            conn.execute(
                '''UPDATE suppliers SET name=?, code=?, s_type=?, market=?, address=?,
                   contact=?, phone=?, category=?, default_invoice=?, settle_type=?,
                   note=?, active=? WHERE id=?''',
                (rec['name'], rec['code'], rec['s_type'], rec['market'],
                 rec['address'], rec['contact'], rec['phone'], rec['category'],
                 rec['default_invoice'], rec['settle_type'], rec['note'],
                 rec['active'], sid))
            renamed = 0
            if sync and old['name'] != rec['name']:
                renamed = conn.execute(
                    'UPDATE purchases SET supplier=? WHERE supplier=?',
                    (rec['name'], old['name'])).rowcount
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'success': False, 'error': '该供应商名称已存在'}), 400
        row = _supplier_rows(conn, rec['name'], sid=sid)[0]   # 按 id 精确回查，别用名称模糊匹配
        conn.close()
        _log(f'编辑供应商 id={sid} name={rec["name"]} 同步历史={renamed}')
        return jsonify({'success': True, 'data': row, 'renamed': renamed})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return _server_error(e)


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
        return _server_error(e)


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
        ws.append(['GYS001', '示例供应商', '个体', '恩施华硒', '恩施华硒农产品市场3号',
                   '张老板', '13800000000', '生鲜', '农产品收购发票', '月结',
                   '示例行，导入前请删除'])
        ws.cell(row=2, column=2).font = Font(color='9CA3AF', italic=True)
        ws.append(['填写说明：只有「供应商名称」必填，其余选填；「供应商编码」留空会自动按'
                   ' GYS001 顺序生成；「供应商类型」建议填 个体/批发市场/公司/其他；'
                   '「结算方式」填 现结/周结/月结/压批结；默认发票类型填错或不填会自动留空；'
                   '重名的默认跳过，要覆盖已存在的请在导入时选「更新」。'])
        ws.cell(row=3, column=1).font = Font(color='6B7280', size=10)
        _set_widths(ws, [12, 20, 12, 16, 26, 12, 14, 12, 16, 10, 22])
        return _xlsx_response(wb, '供应商导入模板.xlsx')
    except Exception as e:
        return _server_error(e)


@bp.route('/api/purchase/suppliers/import', methods=['POST'])
def import_suppliers():
    """导入供应商 Excel（.xlsx）

    - 按列名认列（顺序随意），至少要有「供应商名称」
    - mode=skip（默认）：重名跳过，不动已有档案；mode=update：重名则更新全部
      档案字段（启用状态不改动）；编码留空自动按 GYS001 顺序生成；编码与其它
      供应商重复则该行报错
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
            invoice = _INVOICE_MIGRATE.get(invoice, invoice)
            if invoice and invoice not in INVOICE_TYPES:
                invoice = ''                       # 票种写错就留空，不让整行失败
            settle = _cell_text(cell('settle_type'))
            if settle and settle not in SETTLE_TYPES:
                settle = ''                        # 结算方式同理，写错留空
            rec = {
                'name': name,
                'code': _cell_text(cell('code'))[:30],
                's_type': _cell_text(cell('s_type'))[:20],
                'market': _cell_text(cell('market'))[:50],
                'address': _cell_text(cell('address'))[:100],
                'contact': _cell_text(cell('contact'))[:50],
                'phone': _cell_text(cell('phone'))[:30],
                'category': _cell_text(cell('category'))[:50],
                'default_invoice': invoice,
                'settle_type': settle,
                'note': _cell_text(cell('note'))[:200],
            }
            old = conn.execute('SELECT id FROM suppliers WHERE name=?', (name,)).fetchone()
            if old:
                if mode == 'skip':
                    skipped.append({'row': i, 'name': name, 'reason': '已存在，未改动'})
                    continue
                conn.execute(
                    '''UPDATE suppliers SET code=?, s_type=?, market=?, address=?,
                       contact=?, phone=?, category=?, default_invoice=?, settle_type=?,
                       note=? WHERE id=?''',
                    (rec['code'], rec['s_type'], rec['market'], rec['address'],
                     rec['contact'], rec['phone'], rec['category'],
                     rec['default_invoice'], rec['settle_type'], rec['note'],
                     old['id']))
                updated.append(old['id'])
                continue
            if not rec['code']:
                rec['code'] = _next_supplier_code(conn)
            elif conn.execute('SELECT id FROM suppliers WHERE code=?',
                              (rec['code'],)).fetchone():
                errors.append({'row': i, 'name': name,
                               'reason': f'编码 {rec["code"]} 已被其它供应商使用'})
                continue
            cur = conn.execute(
                '''INSERT INTO suppliers (name, code, s_type, market, address,
                   contact, phone, category, default_invoice, settle_type, note, active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,1)''',
                (rec['name'], rec['code'], rec['s_type'], rec['market'],
                 rec['address'], rec['contact'], rec['phone'], rec['category'],
                 rec['default_invoice'], rec['settle_type'], rec['note']))
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
        return _server_error(e)


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
        return _server_error(e)


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
        return _server_error(e)


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
        return _server_error(e)


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
        return _server_error(e)


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
            ws.append([d['date'], d['bill_no'], d['supplier'], d['customer'],
                       d['category'], d['summary'], d['invoice_type'],
                       d['total_amount'], d['input_tax'], d['net_cost'],
                       d['return_amount'], d['deduct_amount'], d['payable'],
                       d['invoiced_amount'], d['paid_amount'], d['unpaid_balance'],
                       d['pay_account'], d['pay_status']])
        if items:
            _style_money(ws, 2, ws.max_row, _MONEY_IDX)
            ws.append(['合计', '', f'{len(items)} 笔', '', '', '', '',
                       sum(d['total_amount'] for d in items),
                       sum(d['input_tax'] for d in items),
                       sum(d['net_cost'] for d in items),
                       sum(d['return_amount'] for d in items),
                       sum(d['deduct_amount'] for d in items),
                       sum(d['payable'] for d in items),
                       sum(d['invoiced_amount'] for d in items),
                       sum(d['paid_amount'] for d in items),
                       sum(d['unpaid_balance'] for d in items)])
            last = ws.max_row
            _style_money(ws, last, last, _MONEY_IDX)
            for c in range(1, len(EXPORT_HEADERS) + 1):
                ws.cell(row=last, column=c).font = Font(bold=True)
        _set_widths(ws, [12, 14, 20, 12, 10, 28, 14, 18, 12, 13, 11, 11, 13, 12, 12, 12, 12, 10])
        return _xlsx_response(wb, f'采购台账_{now_str()[:10]}.xlsx')
    except Exception as e:
        return _server_error(e)


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
        ws.append(['2026-01-01', 'CG-0001', '示例供应商', '一中食堂', '生鲜',
                   '示例行，导入前请删除本行', '专票9%', 1130.00, '', '',
                   0, 30.00, '', 0, 1100.00, '', '微信', '部分付款'])
        ws.cell(row=2, column=3).font = Font(color='9CA3AF', italic=True)
        _style_money(ws, 2, 2, _MONEY_IDX)
        ws.append(['填写说明：日期写 2026-01-01 这种格式；金额只填数字，不要带 ¥ 和千分位；'
                   '进项税额留空会按票种自动算（农产品收购发票按买价×9%计算抵扣，'
                   '专票/普票价税分离）；不含税成本、应付净额、未付余额是自动算的，填了也会忽略。'])
        ws.cell(row=3, column=1).font = Font(color='6B7280', size=10)
        _set_widths(ws, [12, 14, 20, 12, 10, 28, 14, 18, 12, 13, 11, 11, 13, 12, 12, 12, 12, 10])
        return _xlsx_response(wb, '采购台账导入模板.xlsx')
    except Exception as e:
        return _server_error(e)


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
                'customer': _cell_text(cell('customer')),
            }
            if data['supplier'] == '示例供应商':               # 模板自带的示例行
                continue
            try:
                data['total_amount'] = _cell_money(cell('total_amount'))
                # 税额列留空 → 交给 _validate 按票种自动算
                tax_cell = cell('input_tax')
                if tax_cell is not None and str(tax_cell).strip() != '':
                    data['input_tax'] = _cell_money(tax_cell)
                data['return_amount'] = _cell_money(cell('return_amount'))
                data['deduct_amount'] = _cell_money(cell('deduct_amount'))
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
                   paid_amount, pay_account, return_amount, deduct_amount, customer)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (rec['date'], rec['bill_no'], rec['supplier'], rec['category'],
                 rec['summary'], rec['invoice_type'], rec['total_amount'],
                 rec['input_tax'], rec['invoiced_amount'], rec['paid_amount'],
                 rec['pay_account'], rec['return_amount'], rec['deduct_amount'],
                 rec['customer']))
            imported.append(cur.lastrowid)
            if auto_supplier and rec['supplier']:
                conn.execute(
                    "INSERT OR IGNORE INTO suppliers (name, code, active) "
                    "VALUES (?, ?, 1)", (rec['supplier'], _next_supplier_code(conn)))
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
        return _server_error(e)


@bp.route('/purchase')
@bp.route('/purchase/')
def purchase_index():
    resp = send_from_directory(DB_DIR, 'index.html')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

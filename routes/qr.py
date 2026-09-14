# -*- coding: utf-8 -*-
"""二维码生成模块

纯标准库实现 QR 编码（字节模式，版本 1~10，纠错 L/M/Q/H），输出 SVG / PNG / 矩阵。

设计说明：
- 不依赖任何第三方库：PythonAnywhere 免费版无法保证 pip 安装成功，
  因此 QR 编码、Reed-Solomon 纠错、PNG(zlib/struct) 全部自行实现。
- 内容统一按 UTF-8 字节模式编码，中文可直接生成。
- 无数据库：本模块是纯转换工具，不落库、不上传用户输入内容。
"""
import os
import struct
import zlib
from typing import Any

from flask import Blueprint, Response, jsonify, request

from .utils import make_logger

bp = Blueprint('qr', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QR_DIR = os.path.join(BASE_DIR, '二维码')
_log = make_logger(os.path.join(QR_DIR, 'app.log'))

MAX_TEXT_CHARS = 400      # 输入字符上限（防超长占用 CPU）
MAX_SCALE = 40            # 每个模块的像素上限
MAX_BORDER = 10           # 静区（模块数）上限
MAX_PIXELS = 1600         # 输出图片边长上限，超出自动缩小 scale
LEVELS = ('L', 'M', 'Q', 'H')
MIN_VERSION, MAX_VERSION = 1, 10


class QRError(ValueError):
    """二维码生成的业务错误（参数非法或内容超出容量）"""


# ==================== GF(256) 与 Reed-Solomon ====================

_EXP = [0] * 512
_LOG = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D          # 本原多项式 x^8+x^4+x^3+x^2+1
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _poly_mul(p: list[int], q: list[int]) -> list[int]:
    res = [0] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        if a == 0:
            continue
        for j, b in enumerate(q):
            res[i + j] ^= _gf_mul(a, b)
    return res


def _rs_gen_poly(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        poly = _poly_mul(poly, [1, _EXP[i]])
    return poly


def _rs_encode(data: list[int], ec_count: int) -> list[int]:
    """计算 RS 纠错码字"""
    gen = _rs_gen_poly(ec_count)
    buf = list(data) + [0] * ec_count
    for i in range(len(data)):
        coef = buf[i]
        if coef != 0:
            for j in range(1, len(gen)):
                buf[i + j] ^= _gf_mul(gen[j], coef)
    return buf[len(data):]


# ==================== 规格表 ====================

# 各版本总码字数
_TOTAL_CODEWORDS: dict[int, int] = {
    1: 26, 2: 44, 3: 70, 4: 100, 5: 134,
    6: 172, 7: 196, 8: 242, 9: 292, 10: 346,
}

# (版本, 纠错等级) -> (每块纠错码字数, ((块数, 每块数据码字数), ...))
_ECC_TABLE: dict[tuple[int, str], tuple[int, tuple[tuple[int, int], ...]]] = {
    (1, 'L'): (7, ((1, 19),)),
    (1, 'M'): (10, ((1, 16),)),
    (1, 'Q'): (13, ((1, 13),)),
    (1, 'H'): (17, ((1, 9),)),
    (2, 'L'): (10, ((1, 34),)),
    (2, 'M'): (16, ((1, 28),)),
    (2, 'Q'): (22, ((1, 22),)),
    (2, 'H'): (28, ((1, 16),)),
    (3, 'L'): (15, ((1, 55),)),
    (3, 'M'): (26, ((1, 44),)),
    (3, 'Q'): (18, ((2, 17),)),
    (3, 'H'): (22, ((2, 13),)),
    (4, 'L'): (20, ((1, 80),)),
    (4, 'M'): (18, ((2, 32),)),
    (4, 'Q'): (26, ((2, 24),)),
    (4, 'H'): (16, ((4, 9),)),
    (5, 'L'): (26, ((1, 108),)),
    (5, 'M'): (24, ((2, 43),)),
    (5, 'Q'): (18, ((2, 15), (2, 16))),
    (5, 'H'): (22, ((2, 11), (2, 12))),
    (6, 'L'): (18, ((2, 68),)),
    (6, 'M'): (16, ((4, 27),)),
    (6, 'Q'): (24, ((4, 19),)),
    (6, 'H'): (28, ((4, 15),)),
    (7, 'L'): (20, ((2, 78),)),
    (7, 'M'): (18, ((4, 31),)),
    (7, 'Q'): (18, ((2, 14), (4, 15))),
    (7, 'H'): (26, ((4, 13), (1, 14))),
    (8, 'L'): (24, ((2, 97),)),
    (8, 'M'): (22, ((2, 38), (2, 39))),
    (8, 'Q'): (22, ((4, 18), (2, 19))),
    (8, 'H'): (26, ((4, 14), (2, 15))),
    (9, 'L'): (30, ((2, 116),)),
    (9, 'M'): (22, ((3, 36), (2, 37))),
    (9, 'Q'): (20, ((4, 16), (4, 17))),
    (9, 'H'): (24, ((4, 12), (4, 13))),
    (10, 'L'): (18, ((2, 68), (2, 69))),
    (10, 'M'): (26, ((4, 43), (1, 44))),
    (10, 'Q'): (24, ((6, 19), (2, 20))),
    (10, 'H'): (28, ((6, 15), (2, 16))),
}

# 各版本对齐图案中心坐标
_ALIGN_POS: dict[int, list[int]] = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

# 纠错等级在格式信息中的位
_LEVEL_BITS: dict[str, int] = {'L': 0b01, 'M': 0b00, 'Q': 0b11, 'H': 0b10}


def version_capacity(version: int, level: str) -> int:
    """返回指定版本/纠错等级下可容纳的数据码字数"""
    _ec_per_block, groups = _ECC_TABLE[(version, level)]
    return sum(count * data_cw for count, data_cw in groups)


# ==================== 位缓冲 ====================

class _BitBuffer:
    def __init__(self) -> None:
        self.bits: list[int] = []

    def put(self, value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def __len__(self) -> int:
        return len(self.bits)

    def to_bytes(self) -> list[int]:
        out = []
        for i in range(0, len(self.bits), 8):
            byte = 0
            for b in self.bits[i:i + 8]:
                byte = (byte << 1) | b
            out.append(byte)
        return out


# ==================== 数据编码 ====================

def _pick_version(data: bytes, level: str) -> int:
    """按内容长度选择最小可用版本"""
    for version in range(MIN_VERSION, MAX_VERSION + 1):
        count_bits = 8 if version < 10 else 16
        need = 4 + count_bits + len(data) * 8
        if need <= version_capacity(version, level) * 8:
            return version
    raise QRError('内容过长，请缩短内容或降低纠错等级')


def _data_codewords(data: bytes, version: int, level: str) -> list[int]:
    """字节模式编码 + 分块 RS + 交织，返回最终码字序列"""
    ec_per_block, groups = _ECC_TABLE[(version, level)]
    capacity_bits = version_capacity(version, level) * 8
    buf = _BitBuffer()
    buf.put(0b0100, 4)                                   # 字节模式
    buf.put(len(data), 8 if version < 10 else 16)        # 字符计数
    for byte in data:
        buf.put(byte, 8)
    # 终止符 + 补齐到字节边界
    buf.put(0, min(4, capacity_bits - len(buf)))
    while len(buf) % 8 != 0:
        buf.bits.append(0)
    # 填充字节 0xEC / 0x11 交替
    idx = 0
    while len(buf) < capacity_bits:
        buf.put(0xEC if idx % 2 == 0 else 0x11, 8)
        idx += 1

    words = buf.to_bytes()
    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    pos = 0
    for count, data_cw in groups:
        for _ in range(count):
            block = words[pos:pos + data_cw]
            pos += data_cw
            data_blocks.append(block)
            ec_blocks.append(_rs_encode(block, ec_per_block))

    result: list[int] = []
    max_len = max(len(b) for b in data_blocks)
    for i in range(max_len):                             # 数据码字交织
        for block in data_blocks:
            if i < len(block):
                result.append(block[i])
    for i in range(ec_per_block):                        # 纠错码字交织
        for block in ec_blocks:
            result.append(block[i])
    return result


# ==================== 矩阵构造 ====================

def _mask_bit(mask: int, i: int, j: int) -> bool:
    """i=行, j=列"""
    if mask == 0:
        return (i + j) % 2 == 0
    if mask == 1:
        return i % 2 == 0
    if mask == 2:
        return j % 3 == 0
    if mask == 3:
        return (i + j) % 3 == 0
    if mask == 4:
        return (i // 2 + j // 3) % 2 == 0
    if mask == 5:
        return (i * j) % 2 + (i * j) % 3 == 0
    if mask == 6:
        return ((i * j) % 2 + (i * j) % 3) % 2 == 0
    return ((i + j) % 2 + (i * j) % 3) % 2 == 0


def _place_patterns(m: list[list[int]], reserved: list[list[bool]], version: int) -> None:
    """布置定位/分隔/时序/对齐图案，并标记功能区"""
    size = len(m)

    def mark(r: int, c: int, dark: int) -> None:
        m[r][c] = dark
        reserved[r][c] = True

    # 定位图案（含分隔符）
    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    edge = dr in (0, 6) or dc in (0, 6)
                    center = 2 <= dr <= 4 and 2 <= dc <= 4
                    mark(r, c, 1 if (edge or center) else 0)
                else:
                    mark(r, c, 0)

    # 时序图案
    for i in range(8, size - 8):
        dark = 0 if i % 2 else 1
        m[6][i] = dark
        reserved[6][i] = True
        m[i][6] = dark
        reserved[i][6] = True

    # 对齐图案
    coords = _ALIGN_POS[version]
    for r in coords:
        for c in coords:
            if (r, c) in ((6, 6), (6, size - 7), (size - 7, 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    mark(r + dr, c + dc, 0 if max(abs(dr), abs(dc)) == 1 else 1)

    # 格式信息区域预留
    for i in range(9):
        if not reserved[8][i]:
            reserved[8][i] = True
        if not reserved[i][8]:
            reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True
    # 固定黑模块
    m[size - 8][8] = 1
    reserved[size - 8][8] = True

    # 版本信息区域预留（版本 7 及以上）
    if version >= 7:
        for i in range(18):
            col = size - 11 + i % 3
            row = i // 3
            reserved[row][col] = True
            reserved[col][row] = True


def _place_data(m: list[list[int]], reserved: list[list[bool]], codewords: list[int]) -> None:
    size = len(m)
    total_bits = len(codewords) * 8
    bit_idx = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:                      # 跳过竖直时序列
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if reserved[row][c]:
                    continue
                bit = 0
                if bit_idx < total_bits:
                    bit = (codewords[bit_idx >> 3] >> (7 - (bit_idx & 7))) & 1
                m[row][c] = bit
                bit_idx += 1
        upward = not upward
        col -= 2


def _write_format(m: list[list[int]], level: str, mask: int) -> None:
    size = len(m)
    data = (_LEVEL_BITS[level] << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((data << 10) | rem) ^ 0x5412

    def put(col: int, row: int, index: int) -> None:
        m[row][col] = (bits >> index) & 1

    for i in range(6):
        put(8, i, i)
    put(8, 7, 6)
    put(8, 8, 7)
    put(7, 8, 8)
    for i in range(9, 15):
        put(14 - i, 8, i)
    for i in range(8):
        put(size - 1 - i, 8, i)
    for i in range(8, 15):
        put(8, size - 15 + i, i)
    m[size - 8][8] = 1


def _write_version(m: list[list[int]], version: int) -> None:
    size = len(m)
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    bits = (version << 12) | rem
    for i in range(18):
        bit = (bits >> i) & 1
        col = size - 11 + i % 3
        row = i // 3
        m[row][col] = bit
        m[col][row] = bit


def _penalty(m: list[list[int]]) -> int:
    size = len(m)
    score = 0

    def line_score(line: list[int]) -> int:
        s = 0
        run, prev = 1, line[0]
        for x in range(1, size):
            if line[x] == prev:
                run += 1
            else:
                if run >= 5:
                    s += 3 + (run - 5)
                run, prev = 1, line[x]
        if run >= 5:
            s += 3 + (run - 5)
        return s

    # N1：连续同色
    for row in m:
        score += line_score(row)
    for c in range(size):
        score += line_score([m[r][c] for r in range(size)])

    # N2：2x2 同色块
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3

    # N3：类似定位图案的 1:1:3:1:1
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for r in range(size):
        row = m[r]
        for i in range(size - 10):
            seg = row[i:i + 11]
            if seg == pat1 or seg == pat2:
                score += 40
    for c in range(size):
        col = [m[r][c] for r in range(size)]
        for i in range(size - 10):
            seg = col[i:i + 11]
            if seg == pat1 or seg == pat2:
                score += 40

    # N4：黑白比例偏离 50%
    dark = sum(sum(row) for row in m)
    percent = dark * 100.0 / (size * size)
    score += int(abs(percent - 50) / 5) * 10
    return score


def encode(text: str, level: str = 'M', mask: int | None = None) -> tuple[list[list[int]], int, int]:
    """生成二维码矩阵

    返回 (矩阵, 版本, 实际使用的掩码)。mask 为 None 时按惩罚分自动选择最优掩码。
    """
    data = text.encode('utf-8')
    version = _pick_version(data, level)
    codewords = _data_codewords(data, version, level)

    size = version * 4 + 17
    base: list[list[int]] = [[0] * size for _ in range(size)]
    reserved: list[list[bool]] = [[False] * size for _ in range(size)]
    _place_patterns(base, reserved, version)
    _place_data(base, reserved, codewords)

    best: tuple[list[list[int]], int, int] | None = None
    candidates = range(8) if mask is None else (mask,)
    for m_idx in candidates:
        cand = [row[:] for row in base]
        for r in range(size):
            for c in range(size):
                if not reserved[r][c] and _mask_bit(m_idx, r, c):
                    cand[r][c] ^= 1
        _write_format(cand, level, m_idx)
        if version >= 7:
            _write_version(cand, version)
        score = _penalty(cand)
        if best is None or score < best[2]:
            best = (cand, m_idx, score)
    assert best is not None
    return best[0], version, best[1]


# ==================== 渲染 ====================

def render_svg(matrix: list[list[int]], scale: int = 8, border: int = 4) -> str:
    n = len(matrix) + border * 2
    path: list[str] = []
    for y, row in enumerate(matrix):
        x = 0
        while x < len(row):
            if row[x]:
                x2 = x
                while x2 < len(row) and row[x2]:
                    x2 += 1
                path.append(f'M{x + border} {y + border}h{x2 - x}v1h-{x2 - x}z')
                x = x2
            else:
                x += 1
    size_px = n * scale
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size_px}" height="{size_px}" '
        f'viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
        f'<rect width="{n}" height="{n}" fill="#ffffff"/>'
        f'<path d="{"".join(path)}" fill="#000000"/></svg>'
    )


def render_png(matrix: list[list[int]], scale: int = 8, border: int = 4) -> bytes:
    n = len(matrix) + border * 2
    raw = bytearray()
    for y in range(n):
        my = y - border
        source = matrix[my] if 0 <= my < len(matrix) else None
        line = bytearray(b'\x00')          # filter type 0
        for x in range(n):
            mx = x - border
            dark = bool(source and 0 <= mx < len(source) and source[mx])
            line += (b'\x00\x00\x00' if dark else b'\xff\xff\xff') * scale
        for _ in range(scale):             # 每个模块纵向放大
            raw += line

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack('>IIBBBBB', n * scale, n * scale, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(bytes(raw), 9))
            + chunk(b'IEND', b''))


# ==================== 参数解析 ====================

def _read_params() -> dict[str, Any]:
    src: Any = request.get_json(silent=True) if request.method == 'POST' else None
    src = src if isinstance(src, dict) else request.args
    return {
        'text': (str(src.get('text') or '')).strip(),
        'level': (str(src.get('level') or 'M')).strip().upper(),
        'fmt': (str(src.get('fmt') or 'svg')).strip().lower(),
        'scale': src.get('scale'),
        'border': src.get('border'),
    }


def _int_param(value: Any, default: int, low: int, high: int, name: str) -> int:
    if value is None or value == '':
        return default
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise QRError(f'{name} 必须是整数')
    if not low <= num <= high:
        raise QRError(f'{name} 需在 {low}~{high} 之间')
    return num


def _build_result() -> tuple[list[list[int]], int, int, int, str]:
    """解析校验参数并生成矩阵，返回 (矩阵, scale, border, 版本, fmt)"""
    p = _read_params()
    if not p['text']:
        raise QRError('请输入要生成二维码的内容')
    if len(p['text']) > MAX_TEXT_CHARS:
        raise QRError(f'内容过长，最多 {MAX_TEXT_CHARS} 个字符')
    if p['level'] not in LEVELS:
        raise QRError('纠错等级只能是 L / M / Q / H')
    if p['fmt'] not in ('svg', 'png', 'json'):
        raise QRError('输出格式只能是 svg / png / json')

    scale = _int_param(p['scale'], 8, 1, MAX_SCALE, '缩放')
    border = _int_param(p['border'], 4, 0, MAX_BORDER, '静区')

    matrix, version, _mask = encode(p['text'], p['level'])
    n = len(matrix) + border * 2
    if n * scale > MAX_PIXELS:                 # 限制输出尺寸，避免超大图片
        scale = max(1, MAX_PIXELS // n)
    return matrix, scale, border, version, str(p['fmt'])


# ==================== API ====================

@bp.route('/api/qrcode', methods=['GET', 'POST'])
def make_qrcode():
    """生成二维码

    GET  参数：text / level / scale / border / fmt
    POST JSON：同上
    fmt=svg 返回 SVG 图片；fmt=png 返回 PNG 图片；fmt=json 返回矩阵数据
    """
    try:
        matrix, scale, border, version, fmt = _build_result()
        if fmt == 'png':
            data = render_png(matrix, scale, border)
            return Response(data, mimetype='image/png',
                            headers={'Cache-Control': 'no-store'})
        if fmt == 'json':
            return jsonify({'success': True, 'data': {
                'matrix': matrix,
                'size': len(matrix),
                'version': version,
                'level': _read_params()['level'],
            }})
        svg = render_svg(matrix, scale, border)
        return Response(svg, mimetype='image/svg+xml',
                        headers={'Cache-Control': 'no-store'})
    except QRError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:                     # 兜底，避免 500 暴露内部信息
        _log(f'生成失败: {e!r}')
        return jsonify({'success': False, 'error': '生成失败，请稍后重试'}), 500


@bp.route('/api/qrcode/capacity')
def capacity():
    """返回各纠错等级在当前实现下可容纳的最大字节数（供前端提示）"""
    return jsonify({'success': True, 'data': {
        lv: version_capacity(MAX_VERSION, lv) for lv in LEVELS
    }, 'max_chars': MAX_TEXT_CHARS})


@bp.route('/api/backup/qrdiag')
def qrdiag():                              # 临时诊断：确认静态目录在服务器上的落盘情况
    import subprocess as _sp
    info: dict[str, Any] = {'base': BASE_DIR}
    info['exists_qr'] = os.path.isdir(QR_DIR)
    try:
        info['entries'] = sorted(os.listdir(BASE_DIR))[:60]
    except Exception as e:
        info['entries'] = 'ERR %r' % e
    if os.path.isdir(QR_DIR):
        try:
            info['qr_files'] = sorted(os.listdir(QR_DIR))
        except Exception as e:
            info['qr_files'] = 'ERR %r' % e
    for cmd, key in ((['git', 'ls-files'], 'git_ls'),
                     (['git', 'status', '--porcelain'], 'git_status'),
                     (['git', 'config', '--get', 'core.sparseCheckout'], 'sparse'),
                     (['git', 'rev-parse', 'HEAD'], 'head')):
        try:
            r = _sp.run(cmd, cwd=BASE_DIR, capture_output=True, text=True, timeout=15)
            lines = r.stdout.splitlines()
            info[key] = (lines[:15] if key != 'head' else r.stdout.strip())
            info[key + '_rc'] = r.returncode
        except Exception as e:
            info[key] = 'ERR %r' % e
    return jsonify({'success': True, 'diag': info})

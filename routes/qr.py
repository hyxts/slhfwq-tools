# -*- coding: utf-8 -*-
"""电话二维码（挪车码）模块

纯标准库实现 QR 编码（字节模式，版本 1~10，纠错 L/M/Q/H），输出 SVG / PNG / 矩阵。

设计说明：
- 不依赖任何第三方库：PythonAnywhere 免费版无法保证 pip 安装成功，
  因此 QR 编码、Reed-Solomon 纠错、PNG(zlib/struct) 全部自行实现。
- 只生成电话二维码（挪车码）。**扫码内容固定为本站中转页** `/qrcode/call/<号码>`：
  微信 / 相机 / 浏览器都能打开，页面立即唤起拨号（也有大按钮兜底）。
  曾尝试过 `tel:`（微信里只当文本）与 vCard 名片（要再点一次「呼叫」），都不稳，已移除。
- 号码不做国际区号处理：用户输入几位就用几位（6~15 位数字），不再自动补 +86。
- SVG 支持标题（如「扫描挪车」）与底部提示行，并做圆角码点、圆角定位点与渐变
  标题条美化；**二维码下方不再印号码**（扫码即可拨号，印出来反而泄露隐私）。
- **二维码本身是彩色的**：码点与定位点取主题深色对角渐变（6 套配色：绿 / 蓝 /
  紫 / 橙 / 墨黑 / 流光），卡片底为同色系浅渐变，定位点白环与静区保持纯白。渐变
  上每个色停都必须是深色（亮度 ≤ 120），否则彩色码扫不出来——见 THEMES 注释与测试断言。
- **码点样式可选**（参数 style）：solid 纯色 / grad 同色渐变 / multi 多色渐变（默认）。
  「多色」= 四个**都是深色但色相不同**的色停（如深蓝 → 深紫 → 深玫红 → 深青），
  不是把亮色堆上去——亮色当码点会直接扫不出来。
- PNG 同样彩色（逐模块按对角位置取渐变色，与 SVG 一致），仍为纯码无文字。
- 无数据库：本模块是纯转换工具，不落库、不上传用户输入内容。
"""
import os
import re
import struct
import uuid
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
MAX_TITLE_CHARS = 12      # 标题字数上限
MAX_NOTE_CHARS = 24       # 提示行字数上限
LEVELS = ('L', 'M', 'Q', 'H')
MIN_VERSION, MAX_VERSION = 1, 10

# 主题：彩色渐变配色。from/accent/to = 标题条渐变（accent 是中间色，让标题条也是多色），
# dot1 → m1 → m2 → dot2 = 码点渐变（四个色停，m1/m2 是跨色相的深色，构成真正的多色渐变），
# bg1/bg2 = 背景渐变（必须接近纯白），ink = 提示文字色。
# ⚠ 新增/改配色必须满足：四个码点色停亮度都要 ≤ 120、背景两端亮度 ≥ 235
#   （tests 里 test_theme_contrast 会断言，否则彩色码会扫不出来）
# ⚠ 「多色」不是把亮色堆上去：黄/青/浅粉这类高亮度色当码点会直接扫不出来，
#   多色是「同是深色、但色相不同」——如深蓝 → 深紫 → 深玫红 → 深青。
THEMES: dict[str, dict[str, str]] = {
    'green': {'from': '#34d399', 'accent': '#22d3ee', 'to': '#059669',
              'dot1': '#065f46', 'm1': '#0e7490', 'm2': '#15803d', 'dot2': '#0f766e',
              'bg1': '#f0fdf4', 'bg2': '#ffffff', 'ink': '#5f8d80'},
    'blue': {'from': '#60a5fa', 'accent': '#818cf8', 'to': '#2563eb',
             'dot1': '#1e3a8a', 'm1': '#6d28d9', 'm2': '#0369a1', 'dot2': '#1d4ed8',
             'bg1': '#eff6ff', 'bg2': '#ffffff', 'ink': '#6b86c4'},
    'purple': {'from': '#a78bfa', 'accent': '#f472b6', 'to': '#7c3aed',
               'dot1': '#4c1d95', 'm1': '#9d174d', 'm2': '#6d28d9', 'dot2': '#7e22ce',
               'bg1': '#faf5ff', 'bg2': '#ffffff', 'ink': '#8a72b8'},
    'orange': {'from': '#fb923c', 'accent': '#f59e0b', 'to': '#db2777',
               'dot1': '#7c2d12', 'm1': '#b91c1c', 'm2': '#9a3412', 'dot2': '#c2410c',
               'bg1': '#fff7ed', 'bg2': '#ffffff', 'ink': '#c07a4e'},
    'dark': {'from': '#475569', 'accent': '#64748b', 'to': '#0f172a',
             'dot1': '#0f172a', 'm1': '#312e81', 'm2': '#1e293b', 'dot2': '#334155',
             'bg1': '#f8fafc', 'bg2': '#ffffff', 'ink': '#94a3b8'},
    'rainbow': {'from': '#38bdf8', 'accent': '#a855f7', 'to': '#f43f5e',
                'dot1': '#1d4ed8', 'm1': '#7e22ce', 'm2': '#be123c', 'dot2': '#0f766e',
                'bg1': '#f5f3ff', 'bg2': '#ffffff', 'ink': '#7c6bb0'},
}
THEME_ORDER = ('green', 'blue', 'purple', 'orange', 'dark', 'rainbow')   # 前端按钮顺序

# 码点样式：solid = 纯色（识别最稳）；grad = 同色系两色渐变；multi = 多色相渐变（默认）
STYLES = ('solid', 'grad', 'multi')
DEFAULT_STYLE = 'multi'
_DOT_KEYS = ('dot1', 'm1', 'm2', 'dot2')       # multi 样式的四个色停（首尾复用 dot1/dot2）
_FONT = "'PingFang SC','Microsoft YaHei','Helvetica Neue',Arial,sans-serif"

# 美化参数（改动后必须重新跑解码验证：码点之间一旦留缝，识别率会明显下降，
# 实测 inset=0 时圆角半径 0.25~0.4、定位点外框 ≤1.6 均可稳定解码）
_DOT_INSET = 0.0        # 码点四周留缝（模块单位，必须保持 0，保证码点相连）
_DOT_RX = 0.32          # 码点圆角半径
_FINDER_RX = (1.0, 0.6, 0.4)   # 定位点：外框 / 白环 / 圆心的圆角半径（外框 >1.2 会掉识别率）


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

def _esc(text: str) -> str:
    """XML 文本转义（标题/号码由用户输入，必须转义后再写入 SVG）"""
    return (text.replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;'))


def _finder_boxes(size: int) -> list[tuple[int, int]]:
    """三个定位图案的左上角坐标（7x7 区域）"""
    return [(0, 0), (0, size - 7), (size - 7, 0)]


def _in_finder(r: int, c: int, size: int) -> bool:
    for fr, fc in _finder_boxes(size):
        if fr <= r < fr + 7 and fc <= c < fc + 7:
            return True
    return False


def _rgb(color: str) -> tuple[int, int, int]:
    """#rrggbb → (r, g, b)"""
    h = (color or '#000000').lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def luminance(color: str) -> int:
    """感知亮度 0~255：配色自检用（码点必须足够暗、背景必须足够亮）"""
    r, g, b = _rgb(color)
    return round(0.299 * r + 0.587 * g + 0.114 * b)


def mix_color(c1: str, c2: str, t: float) -> tuple[int, int, int]:
    """在 c1 → c2 的渐变上取色（PNG 逐模块上色用，t=0 取 c1）"""
    r1, g1, b1 = _rgb(c1)
    r2, g2, b2 = _rgb(c2)
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return (round(r1 + (r2 - r1) * t), round(g1 + (g2 - g1) * t), round(b1 + (b2 - b1) * t))


def _dot_stops(t: dict[str, str], style: str) -> list[tuple[float, str]]:
    """码点渐变的色停（offset 0~1）

    solid：单色，整块码一个颜色，识别最稳；
    grad ：同色系 dot1 → dot2；
    multi：dot1 → m1 → m2 → dot2，四个色停跨色相，是真正的多色渐变。
    """
    if style == 'solid':
        return [(0.0, t['dot1'])]
    if style == 'grad':
        return [(0.0, t['dot1']), (1.0, t['dot2'])]
    keys = _DOT_KEYS
    return [(i / (len(keys) - 1), t[k]) for i, k in enumerate(keys)]


def dot_color(t: dict[str, str], style: str, k: float) -> tuple[int, int, int]:
    """取对角位置 k(0~1) 处的码点颜色（PNG 逐模块上色用，与 SVG 渐变一致）"""
    stops = _dot_stops(t, style)
    if len(stops) == 1:
        return _rgb(stops[0][1])
    for i in range(len(stops) - 1):
        o1, c1 = stops[i]
        o2, c2 = stops[i + 1]
        if o1 <= k <= o2:
            span = o2 - o1
            return mix_color(c1, c2, 0.0 if span <= 0 else (k - o1) / span)
    return _rgb(stops[-1][1])


def _defs(gid: str, t: dict[str, str], x1: float, y1: float, x2: float, y2: float,
          style: str = DEFAULT_STYLE) -> str:
    """码点 / 背景 / 标题条三套渐变

    码点与背景用 userSpaceOnUse + 码区坐标：整块码呈一个连续的对角渐变，
    而不是每个小方块各自渐变（后者会让相邻码点明暗不一，降低识别稳定性）。
    """
    dot_stops = ''.join(f'<stop offset="{o * 100:.1f}%" stop-color="{c}"/>'
                        for o, c in _dot_stops(t, style))
    return (
        f'<defs>'
        f'<linearGradient id="{gid}d" gradientUnits="userSpaceOnUse" '
        f'x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}">{dot_stops}</linearGradient>'
        f'<linearGradient id="{gid}b" gradientUnits="userSpaceOnUse" '
        f'x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}">'
        f'<stop offset="0%" stop-color="{t["bg1"]}"/>'
        f'<stop offset="100%" stop-color="{t["bg2"]}"/></linearGradient>'
        f'<linearGradient id="{gid}t" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0%" stop-color="{t["from"]}"/>'
        f'<stop offset="50%" stop-color="{t["accent"]}"/>'
        f'<stop offset="100%" stop-color="{t["to"]}"/></linearGradient>'
        f'</defs>'
    )


def render_svg(matrix: list[list[int]], scale: int = 8, border: int = 4,
               title: str = '', hint: str = '', theme: str = 'green',
               style: str = DEFAULT_STYLE) -> str:
    """渲染彩色 SVG

    码点与定位点用主题深色渐变（userSpaceOnUse 对角渐变），定位点白环与静区保持
    纯白以保证识别；title / hint 均为空时输出「纯码」方形 SVG（只有码，无文字）。
    style：solid 纯色 / grad 同色渐变 / multi 多色渐变。
    """
    size = len(matrix)
    n = size + border * 2
    title = (title or '').strip()
    hint = (hint or '').strip()
    t = THEMES.get(theme, THEMES['green'])

    # ---- 纯码：沿用整行合并的 path，元素最少 ----
    if not title and not hint:
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
        gid = f'qrg{uuid.uuid4().hex[:8]}'
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{size_px}" height="{size_px}" '
            f'viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            + _defs(gid, t, border, border, border + size, border + size, style)
            + f'<rect width="{n}" height="{n}" fill="url(#{gid}b)"/>'
            f'<path d="{"".join(path)}" fill="url(#{gid}d)"/></svg>'
        )

    # ---- 精美卡片 ----
    pad = 1.8                                   # 卡片内边距（模块单位）
    top_h = 5.4 if title else 0.0               # 渐变标题条高度
    hint_h = 3.0 if hint else 0.0               # 提示行高度
    W = n + pad * 2
    H = pad + top_h + n + hint_h + pad
    ox, oy = pad + border, pad + top_h + border  # 二维码左上角（模块坐标，含静区）

    gid = f'qrg{uuid.uuid4().hex[:8]}'          # 避免同页多个 SVG 的渐变 id 冲突
    dot = f'url(#{gid}d)'                       # 码点 / 定位点：主题深色渐变
    body: list[str] = []
    # 定位图案：外框 → 白环 → 圆心，三层圆角矩形
    fr_outer, fr_ring, fr_core = _FINDER_RX
    for fr, fc in _finder_boxes(size):
        x, y = ox + fc, oy + fr
        body.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="7" height="7" rx="{fr_outer}" fill="{dot}"/>')
        body.append(f'<rect x="{x + 1:.2f}" y="{y + 1:.2f}" width="5" height="5" rx="{fr_ring}" fill="#ffffff"/>')
        body.append(f'<rect x="{x + 2:.2f}" y="{y + 2:.2f}" width="3" height="3" rx="{fr_core}" fill="{dot}"/>')
    # 数据码点：圆角小方块（略留缝隙 + 小圆角，形成精致的点阵质感）
    side = 1 - _DOT_INSET * 2
    for r in range(size):
        row = matrix[r]
        for c in range(size):
            if row[c] and not _in_finder(r, c, size):
                body.append(f'<rect x="{ox + c + _DOT_INSET:.2f}" y="{oy + r + _DOT_INSET:.2f}" '
                            f'width="{side:.2f}" height="{side:.2f}" rx="{_DOT_RX}" fill="{dot}"/>')

    texts: list[str] = []
    if title:
        # 顶部渐变圆角条（只圆上方两角）
        rr = 3.0
        texts.append(f'<path d="M0 {rr} a{rr} {rr} 0 0 1 {rr} -{rr} h{W - 2 * rr:.2f} '
                     f'a{rr} {rr} 0 0 1 {rr} {rr} v{top_h - rr:.2f} h-{W:.2f} z" fill="url(#{gid}t)"/>')
        fs = 3.0
        texts.append(f'<text x="{W / 2:.2f}" y="{top_h * 0.68:.2f}" text-anchor="middle" '
                     f'font-family="{_FONT}" font-size="{fs}" font-weight="700" '
                     f'letter-spacing="0.4" fill="#ffffff">{_esc(title)}</text>')
    base = pad + top_h + n                      # 二维码底边
    if hint:
        fs = 1.7
        top = base + 0.6
        texts.append(f'<text x="{W / 2:.2f}" y="{top + fs * 0.9:.2f}" text-anchor="middle" '
                     f'font-family="{_FONT}" font-size="{fs}" fill="{t["ink"]}">{_esc(hint)}</text>')

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W * scale:.0f}" height="{H * scale:.0f}" '
        f'viewBox="0 0 {W:.2f} {H:.2f}">'
        + _defs(gid, t, ox, oy, ox + size, oy + size, style)
        # 卡片底：主题浅色渐变；码区另铺一层纯白，静区保持最大对比
        + f'<rect x="0" y="0" width="{W:.2f}" height="{H:.2f}" rx="3.2" fill="url(#{gid}b)"/>'
        f'<rect x="{ox - border:.2f}" y="{oy - border:.2f}" width="{n}" height="{n}" '
        f'rx="1.2" fill="#ffffff"/>'
        + ''.join(body)
        + ''.join(texts)
        + '</svg>'
    )
    return svg


def render_png(matrix: list[list[int]], scale: int = 8, border: int = 4,
               theme: str = 'green', style: str = DEFAULT_STYLE) -> bytes:
    """输出彩色 PNG：按模块位置在主题的深色渐变上取色（与 SVG 的对角渐变一致）"""
    t = THEMES.get(theme, THEMES['green'])
    size = len(matrix)
    n = size + border * 2
    span = max(1, 2 * (size - 1))
    raw = bytearray()
    for y in range(n):
        my = min(max(y - border, 0), size - 1)
        source = matrix[y - border] if 0 <= y - border < size else None
        line = bytearray(b'\x00')          # filter type 0
        for x in range(n):
            mx = min(max(x - border, 0), size - 1)
            dark = bool(source and 0 <= x - border < size and source[x - border])
            k = (mx + my) / span           # 对角渐变位置
            r, g, b = (dot_color(t, style, k) if dark
                       else mix_color(t['bg1'], t['bg2'], k))
            line += bytes((r, g, b)) * scale
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

def normalize_phone(raw: str) -> str:
    """把用户输入的号码规范为纯数字（不加国际区号）

    挪车码只在国内用，加 +86 反而让号码变长、码点变密，统一按用户输入的数字处理。
    """
    digits = re.sub(r'\D', '', raw or '')
    if not digits:
        raise QRError('请输入手机号码')
    if not 6 <= len(digits) <= 15:
        raise QRError('请输入正确的手机号码')
    return digits


def _text_param(src: Any, key: str, limit: int) -> str:
    val = str(src.get(key) or '').strip()
    if len(val) > limit:
        raise QRError(f'{key} 最多 {limit} 个字符')
    return val


def mask_phone(phone: str) -> str:
    """号码掩码显示：138 **** 8000（中转页与前端提示用）"""
    d = re.sub(r'\D', '', phone or '')
    if len(d) > 11:                            # 带区号的号码只按后 11 位掩码
        d = d[-11:]
    if len(d) >= 8:
        return d[:3] + ' **** ' + d[-4:]
    return d or phone


def build_content(phone: str) -> str:
    """二维码内容：本站中转页 URL（微信 / 相机 / 浏览器都能打开，页面再触发拨号）"""
    return f'{request.host_url}qrcode/call/{phone}'


def _read_params() -> dict[str, Any]:
    src: Any = request.get_json(silent=True) if request.method == 'POST' else None
    src = src if isinstance(src, dict) else request.args
    # phone 为主参数；text 兼容旧调用（tel: 开头会去掉协议头后按号码处理）
    phone = str(src.get('phone') or '').strip()
    if not phone:
        raw_text = str(src.get('text') or '').strip()
        phone = re.sub(r'^tel:', '', raw_text, flags=re.I)
    return {
        'phone': phone,
        'title': _text_param(src, 'title', MAX_TITLE_CHARS),
        'hint': _text_param(src, 'hint', MAX_NOTE_CHARS),
        'theme': (str(src.get('theme') or 'green')).strip().lower(),
        'style': (str(src.get('style') or DEFAULT_STYLE)).strip().lower(),
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


def _build_result() -> dict[str, Any]:
    """解析校验参数并生成矩阵

    返回 dict：matrix / scale / border / version / fmt / phone / tel /
    content / mode / title / hint / theme / level
    """
    p = _read_params()
    phone = normalize_phone(p['phone'])
    if p['level'] not in LEVELS:
        raise QRError('纠错等级只能是 L / M / Q / H')
    if p['fmt'] not in ('svg', 'png', 'json'):
        raise QRError('输出格式只能是 svg / png / json')
    if p['style'] not in STYLES:
        raise QRError('码点样式只能是 solid / grad / multi')

    scale = _int_param(p['scale'], 8, 1, MAX_SCALE, '缩放')
    border = _int_param(p['border'], 4, 0, MAX_BORDER, '静区')

    content = build_content(phone)
    if len(content) > MAX_TEXT_CHARS:
        raise QRError('号码过长')
    matrix, version, _mask = encode(content, p['level'])
    n = len(matrix) + border * 2
    if n * scale > MAX_PIXELS:                 # 限制输出尺寸，避免超大图片
        scale = max(1, MAX_PIXELS // n)
    return {
        'matrix': matrix, 'scale': scale, 'border': border, 'version': version,
        'fmt': str(p['fmt']), 'phone': phone, 'tel': 'tel:' + phone,
        'content': content, 'mode': 'page',      # 固定网页中转，保留字段兼容旧调用
        'title': p['title'], 'hint': p['hint'],
        'theme': p['theme'], 'style': p['style'], 'level': p['level'],
    }


# ==================== API ====================

@bp.route('/api/qrcode', methods=['GET', 'POST'])
def make_qrcode():
    """生成电话二维码（挪车码）

    GET  参数：phone / title / hint / theme / style / level / scale / border / fmt
    POST JSON：同上
    号码只取 6~15 位数字（不加区号），内容固定是本码中转页 URL
    style=solid 纯色 / grad 同色渐变 / multi 多色渐变（默认）
    fmt=svg 返回 SVG（带标题与提示的精美卡片）；fmt=png 返回纯码 PNG；fmt=json 返回矩阵
    """
    try:
        r = _build_result()
        if r['fmt'] == 'png':
            data = render_png(r['matrix'], r['scale'], r['border'], r['theme'], r['style'])
            return Response(data, mimetype='image/png',
                            headers={'Cache-Control': 'no-store'})
        if r['fmt'] == 'json':
            return jsonify({'success': True, 'data': {
                'matrix': r['matrix'],
                'size': len(r['matrix']),
                'version': r['version'],
                'level': r['level'],
                'phone': r['phone'],
                'tel': r['tel'],
                'content': r['content'],
                'mode': r['mode'],
            }})
        svg = render_svg(r['matrix'], r['scale'], r['border'],
                         r['title'], r['hint'], r['theme'], r['style'])
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


# ==================== 网页中转拨号页（免登录，扫码后打开） ====================

# 用占位符替换而非 f-string，避免 CSS 花括号转义麻烦
_CALL_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>呼叫车主</title>
<meta http-equiv="refresh" content="0; url={TEL}">
<meta name="robots" content="noindex">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;
     background:#f0f2f5;color:#1e293b;padding:24px;
     font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.box{width:100%;max-width:360px;background:#fff;border-radius:16px;padding:28px 20px;text-align:center;
     box-shadow:0 2px 12px rgba(0,0,0,.08)}
.ico{width:64px;height:64px;margin:0 auto 14px;border-radius:50%;background:#ecfdf5;color:#059669;
     font-size:30px;line-height:64px}
.t{font-size:19px;font-weight:600;margin-bottom:6px}
.n{font-size:22px;font-weight:700;letter-spacing:1px;color:#059669;margin:14px 0 4px}
.s{font-size:13px;color:#64748b;line-height:1.7}
.btn{display:block;margin-top:20px;padding:14px;border-radius:12px;background:#059669;color:#fff;
     font-size:17px;font-weight:600;text-decoration:none}
.btn:active{opacity:.85}
.alt{display:block;margin-top:12px;font-size:14px;color:#059669;text-decoration:none}
</style>
</head>
<body>
<div class="box">
  <div class="ico">&#9990;</div>
  <div class="t">车主临时停车</div>
  <div class="n">{MASK}</div>
  <div class="s">若没有自动弹出拨号，请点击下面的按钮</div>
  <a class="btn" href="{TEL}">呼叫 {MASK}</a>
  <a class="alt" href="{TEL}" id="again">再次拨号</a>
</div>
<script>
var TEL = '{TEL}';
window.addEventListener('load', function(){
  setTimeout(function(){ window.location.href = TEL; }, 400);
});
document.getElementById('again').addEventListener('click', function(){
  window.location.href = TEL;
});
</script>
</body>
</html>
"""


@bp.route('/qrcode/call/<path:digits>')
def call_page(digits: str):
    """扫码中转页：打开后自动触发拨号（兼容只把 tel: 当文本显示的扫码器）"""
    d = re.sub(r'\D', '', digits or '')
    if not 6 <= len(d) <= 15:
        return Response('号码无效', status=400, mimetype='text/plain; charset=utf-8')
    tel = 'tel:' + d                           # 不加区号：号码就是用户填的那串数字
    html = (_CALL_PAGE.replace('{TEL}', tel).replace('{MASK}', _esc(mask_phone(d))))
    return Response(html, mimetype='text/html; charset=utf-8',
                    headers={'Cache-Control': 'no-store'})

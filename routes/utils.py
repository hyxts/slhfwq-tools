# -*- coding: utf-8 -*-
"""共享工具函数：时区、时间、日志、数据库连接、密码编码、CSRF提取等"""
import os, re, sqlite3, base64, hashlib, hmac, json, threading, queue
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))  # 北京时间 UTC+8


def safe_json_load(val, default=None):
    """安全解析 JSON，失败时返回默认值"""
    if not val: return default or []
    try: return json.loads(val)
    except (json.JSONDecodeError, TypeError): return default or []

# 中文/英文目录映射
FOLDER_MAP = {
    'countdown': '倒计时', 'gpa': '绩点', 'hsgrades': '成绩',
    'pa': '服务器', 'renqing': '人情', 'deploy': '部署',
    'accounting': '记账', 'todo': '待办',
}


def _now():
    return datetime.now(TZ)


def _size_str(b):
    """字节数转可读字符串"""
    if b < 1024:
        return f'{b}B'
    if b < 1048576:
        return f'{b/1024:.1f}KB'
    return f'{b/1048576:.1f}MB'


def make_logger(log_file):
    """返回一个绑定到指定日志文件的日志函数（异步写入，不阻塞请求响应）"""
    _log_queue = queue.Queue()
    _dir_ensured = [False]

    def _writer():
        while True:
            msg = _log_queue.get()
            if msg is None:
                break
            try:
                if not _dir_ensured[0]:
                    os.makedirs(os.path.dirname(log_file), exist_ok=True)
                    _dir_ensured[0] = True
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(msg)
            except Exception:
                pass

    t = threading.Thread(target=_writer, daemon=True)
    t.start()

    def _log(msg):
        ts = _now().strftime('%Y-%m-%d %H:%M:%S')
        _log_queue.put(f'[{ts}] {msg}\n')

    return _log


def make_db(db_file):
    """返回一个绑定到指定数据库文件的连接工厂"""
    def _get_db():
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    return _get_db


def db_has_data(db_path):
    """检查 SQLite 数据库是否有实际数据"""
    try:
        conn = sqlite3.connect(db_path)
        tables = [t[0] for t in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        for t in tables:
            if conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0] > 0:
                conn.close()
                return True
        conn.close()
        return False
    except Exception:
        return False


def extract_csrf(html_text):
    """从PA页面提取CSRF token: 优先 hidden input，其次 script 变量"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, 'html.parser')
        inp = soup.find('input', {'name': 'csrfmiddlewaretoken'})
        if inp:
            return inp.get('value', '')
        for script in soup.find_all('script'):
            if script.string:
                m = re.search(r'Anywhere\.csrfToken\s*=\s*"([^"]+)"', script.string)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


# === 密码编码（PBKDF2 + XOR + HMAC 完整性校验，兼容旧格式） ===

_PBKDF2_ITERATIONS = 200000
_SALT_SIZE = 16
_HMAC_SIZE = 32
_NEW_FORMAT_PREFIX = 'v2:'  # 新格式前缀，与旧 ENC: 前缀区分


def _xor_crypt(data, key):
    """XOR 对称加密/解密"""
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _derive_key(salt_path, salt, dklen, iterations=_PBKDF2_ITERATIONS):
    """PBKDF2 派生密钥"""
    return hashlib.pbkdf2_hmac('sha256', salt_path.encode(), salt, iterations, dklen=dklen)


def encode_pw(plain, salt_path):
    """PBKDF2 派生密钥 + XOR 加密 + HMAC 完整性校验（返回 v2:base64 格式）"""
    if not plain:
        return ''
    salt = os.urandom(_SALT_SIZE)
    enc_key = _derive_key(salt_path, salt, 32)
    mac_key = _derive_key(salt_path, salt, 32, iterations=_PBKDF2_ITERATIONS + 1)
    encrypted = _xor_crypt(plain.encode('utf-8'), enc_key)
    mac = hmac.new(mac_key, encrypted, hashlib.sha256).digest()
    result = salt + mac + encrypted
    return _NEW_FORMAT_PREFIX + base64.b64encode(result).decode('ascii')


def decode_pw(encoded, salt_path):
    """解码密码（自动识别 v2/PBKDF2 格式和旧版 XOR 格式）"""
    if not encoded:
        return ''
    try:
        # 新格式：v2:base64(salt+mac+encrypted)
        if encoded.startswith(_NEW_FORMAT_PREFIX):
            data = base64.b64decode(encoded[3:])
            salt = data[:_SALT_SIZE]
            mac = data[_SALT_SIZE:_SALT_SIZE + _HMAC_SIZE]
            encrypted = data[_SALT_SIZE + _HMAC_SIZE:]
            enc_key = _derive_key(salt_path, salt, 32)
            mac_key = _derive_key(salt_path, salt, 32, iterations=_PBKDF2_ITERATIONS + 1)
            expected_mac = hmac.new(mac_key, encrypted, hashlib.sha256).digest()
            if not hmac.compare_digest(mac, expected_mac):
                return ''  # HMAC 校验失败，数据可能被篡改
            return _xor_crypt(encrypted, enc_key).decode('utf-8')
        # 旧格式兼容：base64(XOR数据) | ENC:base64(XOR数据)
        if encoded.startswith('ENC:'):
            encoded = encoded[4:]
        data = base64.b64decode(encoded)
        key = hashlib.sha256(salt_path.encode()).digest()[:16]
        return _xor_crypt(data, key).decode('utf-8')
    except Exception:
        return ''


# === JsonStore：简单 JSON 数据存取（替代 gpa/hsgrades 重复代码） ===

class JsonStore:
    """通用 JSON 键值存储，基于 SQLite 单表单行"""
    def __init__(self, get_db, table_name='data_store'):
        self._get_db = get_db
        self._table = table_name

    def _ensure_table(self, conn):
        conn.execute(f'CREATE TABLE IF NOT EXISTS {self._table} (id INTEGER PRIMARY KEY, data TEXT)')

    def get(self):
        conn = self._get_db()
        try:
            self._ensure_table(conn)
            row = conn.execute(f'SELECT data FROM {self._table} ORDER BY id LIMIT 1').fetchone()
            return json.loads(row['data']) if row else {}
        finally:
            conn.close()

    def save(self, data):
        conn = self._get_db()
        try:
            self._ensure_table(conn)
            payload = json.dumps(data, ensure_ascii=False)
            conn.execute(f'DELETE FROM {self._table}')
            conn.execute(f'INSERT INTO {self._table} (data) VALUES (?)', (payload,))
            conn.commit()
        finally:
            conn.close()

# ==================== 统一模块目录常量 ====================
# 所有已知模块的中文目录名（用于存储分布、遗留目录检测等）
KNOWN_MODULE_DIRS = {'人情', '绩点', '成绩', '倒计时', '服务器', '记账', '部署', '导航', '待办'}
# 项目内部目录（不对外展示为模块，但也不能被清理）
INTERNAL_DIRS = {'routes', 'backups'}
# 系统保留目录（扫描时跳过）
SYSTEM_RESERVED_DIRS = {'.git', '.codebuddy', '__pycache__', '.venv', 'venv', 'env', '.env'}
# 遗留目录清理时跳过的所有目录
ALL_SAFE_DIRS = KNOWN_MODULE_DIRS | INTERNAL_DIRS | SYSTEM_RESERVED_DIRS

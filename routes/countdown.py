# -*- coding: utf-8 -*-
"""倒计时蓝图 - 支持农历/公历互转、生日/高考等倒计时、岁数显示 v3"""
import os, json, sqlite3, threading
from datetime import date, timedelta, timezone, datetime

from .utils import _now, make_logger, make_db
from flask import Blueprint, request, jsonify, send_from_directory

bp = Blueprint('countdown', __name__)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CD_DIR = os.path.join(BASE_DIR, '倒计时')
DB_FILE = os.path.join(CD_DIR, 'countdown.db')
LOG_FILE = os.path.join(CD_DIR, 'countdown.log')

TZ = timezone(timedelta(hours=8))  # 北京时间 UTC+8

_log = make_logger(LOG_FILE)

# 尝试导入农历库
try:
    from zhdate import ZhDate
    HAS_LUNAR = True
except ImportError:
    HAS_LUNAR = False

_HAS_LUNAR_LOCK = threading.Lock()

_get_db = make_db(DB_FILE)


def _today():
    """获取北京时间今天日期"""
    # 优先使用 _now()（datetime.now(TZ)），如果不可靠则用 UTC 换算
    try:
        d = _now().date()
        # 安全校验：如果日期与预期差距过大（如 server 时钟错误），用 UTC 修正
        utc_now = datetime.now(timezone.utc)
        expected = (utc_now + timedelta(hours=8)).date()
        if abs((d - expected).days) > 1:
            return expected
        return d
    except Exception:
        utc_now = datetime.now(timezone.utc)
        return (utc_now + timedelta(hours=8)).date()


# ==================== 数据库 ====================

def init_db():
    os.makedirs(CD_DIR, exist_ok=True)
    conn = _get_db()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            cal_type TEXT NOT NULL DEFAULT 'solar',
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            day INTEGER NOT NULL,
            is_leap INTEGER DEFAULT 0,
            note TEXT DEFAULT '',
            display_mode TEXT DEFAULT 'full',
            category TEXT DEFAULT 'custom',
            birth_year INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )''')
        existing_cols = set(r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall())
        for col, defval in [('display_mode', "'full'"), ('category', "'custom'"), ('birth_year', '0')]:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT DEFAULT {defval}")
        conn.commit()
    except Exception as e:
        print(f'[countdown] 数据库初始化失败: {e}')
        raise
    finally:
        conn.close()


# ==================== 农历公历转换 ====================

def lunar_to_solar(year, month, day, is_leap=False):
    """农历转公历，返回 date 对象"""
    if not HAS_LUNAR:
        return None
    try:
        lunar = ZhDate(year, month, day, is_leap)
        return lunar.to_datetime().date()
    except Exception:
        return None


def solar_to_lunar(d):
    """公历转农历，返回 (year, month, day, is_leap, 中文描述)"""
    if not HAS_LUNAR:
        return None
    try:
        lunar = ZhDate.from_datetime(d)
        return (lunar.lunar_year, lunar.lunar_month, lunar.lunar_day, lunar.is_leap, str(lunar))
    except Exception:
        return None


def get_event_solar_date(event):
    """获取事件的公历日期，农历事件自动转换"""
    cal_type = event['cal_type']
    year = event['year']
    month = event['month']
    day = event['day']
    is_leap = event.get('is_leap', 0)

    if cal_type == 'lunar':
        if not HAS_LUNAR:
            return None
        return lunar_to_solar(year, month, day, bool(is_leap))
    else:
        try:
            return date(year, month, day)
        except Exception:
            return None


# ==================== 岁数计算 ====================

def calc_age(birth_date, cal_type='solar'):
    """计算岁数，返回精确岁数（浮点）和整数岁"""
    today = _today()
    if not birth_date:
        return None, None

    # 计算精确岁数
    delta = today - birth_date
    exact_age = delta.days / 365.2425

    # 计算整数岁（按生日是否已过）
    try:
        this_birthday = date(today.year, birth_date.month, birth_date.day)
    except ValueError:
        # 2月29日的情况
        this_birthday = date(today.year, 2, 28)

    int_age = today.year - birth_date.year
    if today < this_birthday:
        int_age -= 1

    return round(exact_age, 2), int_age


# ==================== API ====================

def _validate_event_dates(data):
    """校验事件日期字段，返回 (error_msg_or_None, clean_data_dict)"""
    cal_type = data.get('cal_type', 'solar')
    repeat_annual = data.get('repeat_annual', False)
    year = data.get('year', 0)
    month = data.get('month', 0)
    day = data.get('day', 0)
    is_leap = data.get('is_leap', 0)

    try:
        month = int(month)
        day = int(day)
        year = int(year)
    except (ValueError, TypeError):
        return '请填写有效数字日期', None

    if not month or not day:
        return '请填写完整的日期', None

    if repeat_annual:
        year = 0
        test_year = 2000  # 闰年，可验证2月29日
        if cal_type == 'lunar':
            if not HAS_LUNAR:
                return '服务器未安装农历库，无法使用农历事件', None
            try:
                ZhDate(test_year, month, day, bool(is_leap))
            except Exception as ee:
                return f'农历日期不合法: {str(ee)}', None
        else:
            try:
                date(test_year, month, day)
            except Exception:
                return '公历日期不合法', None
    else:
        if not year:
            return '请选择年份', None
        if cal_type == 'lunar':
            if not HAS_LUNAR:
                return '服务器未安装农历库，无法使用农历事件', None
            try:
                ZhDate(year, month, day, bool(is_leap))
            except Exception as ee:
                return f'农历日期不合法: {str(ee)}', None
        else:
            try:
                date(year, month, day)
            except Exception:
                return '公历日期不合法', None

    clean = {
        'name': (data.get('name', '') or '').strip(),
        'cal_type': cal_type,
        'year': year,
        'month': month,
        'day': day,
        'is_leap': int(is_leap),
        'note': (data.get('note', '') or '').strip(),
        'display_mode': data.get('display_mode', 'full'),
        'category': data.get('category', 'custom'),
        'birth_year': int(data.get('birth_year', 0) or 0),
    }
    return None, clean

# 倒计时页面
@bp.route('/countdown')
@bp.route('/countdown/')
def countdown_page():
    return send_from_directory(CD_DIR, 'index.html')

# PWA Manifest
@bp.route('/countdown/manifest.json')
def pwa_manifest():
    return send_from_directory(CD_DIR, 'manifest.json')

@bp.route('/countdown/icon-192.svg')
def cd_icon_192():
    return send_from_directory(CD_DIR, 'icon-192.svg')

@bp.route('/countdown/icon-512.svg')
def cd_icon_512():
    return send_from_directory(CD_DIR, 'icon-512.svg')

@bp.route('/api/countdown/events', methods=['GET'])
def list_events():
    """获取所有倒计时事件列表"""
    try:
        conn = _get_db()
        rows = conn.execute("SELECT * FROM events ORDER BY month, day").fetchall()
        conn.close()

        today = _today()
        events = []
        for r in rows:
            e = dict(r)
            # SQLite 返回的可能为字符串，统一转为整数确保类型安全
            cal_type = e['cal_type']
            e['year'] = int(e['year'] or 0)
            e['month'] = int(e['month'] or 0)
            e['day'] = int(e['day'] or 0)
            is_leap = int(e.get('is_leap', 0) or 0)
            repeat_annual = e['year'] == 0

            # 获取原始日期（每年重复事件无特定年份）
            solar_date = None if repeat_annual else get_event_solar_date(e)

            # 计算倒计时天数（周期性循环：今年过了自动找明年，最多往后找5年）
            days_left = None
            status = 'past'
            target_date = None
            this_year_date = None

            # 尝试今年开始，往后找最近的未来日期
            for offset in range(5):  # 最多找5年（今年到后四年），覆盖待产生日等场景
                try_year = today.year + offset
                if cal_type == 'lunar' and HAS_LUNAR:
                    candidate = lunar_to_solar(try_year, e['month'], e['day'], bool(is_leap))
                else:
                    try:
                        candidate = date(try_year, e['month'], e['day'])
                    except ValueError:
                        # 2月29日在非闰年
                        try:
                            candidate = date(try_year, 2, 28)
                        except ValueError:
                            candidate = None
                if candidate is not None:
                    diff = (candidate - today).days
                    if diff >= 0:
                        days_left = diff
                        target_date = candidate
                        break

            if target_date:
                this_year_date = target_date
                if days_left == 0:
                    status = 'today'
                else:
                    status = 'upcoming'

            # 计算岁数
            # 对于每年重复事件：如果有 birth_year，基于 birth_year 计算当前年龄
            # 对于非重复事件：基于原始日期计算经过的岁数
            exact_age, int_age, xu_sui = None, None, None
            birth_year = int(e.get('birth_year', 0) or 0)
            if repeat_annual and birth_year > 0:
                # 生日类事件：周岁 = 今年 - 出生年（如果今年生日未过则减1）
                int_age = today.year - birth_year
                # 虚岁 = 今年 - 出生年 + 1（中国传统计岁，出生即算1岁）
                xu_sui = today.year - birth_year + 1
                # 独立计算今年生日日期（不能用target_date，因为它是下一个未来日期，可能是明年的）
                this_year_birthday = None
                if cal_type == 'lunar' and HAS_LUNAR:
                    this_year_birthday = lunar_to_solar(today.year, e['month'], e['day'], bool(is_leap))
                else:
                    try:
                        this_year_birthday = date(today.year, e['month'], e['day'])
                    except ValueError:
                        pass  # 如2月29日非闰年，忽略
                if this_year_birthday and this_year_birthday > today:
                    int_age -= 1  # 今年生日还没过，周岁不减
                exact_age = float(int_age)
            elif not repeat_annual and solar_date and solar_date <= today:
                exact_age, int_age = calc_age(solar_date, cal_type)

            # 日期描述
            if repeat_annual:
                leap_prefix = '闰' if (cal_type == 'lunar' and is_leap) else ''
                if cal_type == 'lunar':
                    date_desc = f'每年农历{leap_prefix}{e["month"]}月{e["day"]}日'
                else:
                    date_desc = f'每年{e["month"]}月{e["day"]}日'
            elif cal_type == 'lunar':
                leap_prefix = '闰' if is_leap else ''
                date_desc = f'农历{leap_prefix}{e["month"]}月{e["day"]}日'
                if HAS_LUNAR and this_year_date:
                    date_desc += f'（{this_year_date.strftime("%Y-%m-%d")}）'
            else:
                date_desc = f'{e["year"]}-{e["month"]:02d}-{e["day"]:02d}'

            # 今年日期
            this_year_str = ''
            if this_year_date:
                this_year_str = this_year_date.strftime('%Y-%m-%d')

            # 计算已过天数
            days_since = None
            if status == 'past':
                if repeat_annual:
                    last_date = None
                    for offset in [0, -1]:
                        try_year = today.year + offset
                        if cal_type == 'lunar' and HAS_LUNAR:
                            last_date = lunar_to_solar(try_year, e['month'], e['day'], bool(is_leap))
                        else:
                            try:
                                last_date = date(try_year, e['month'], e['day'])
                            except ValueError:
                                pass
                        if last_date and last_date <= today:
                            break
                    if last_date:
                        days_since = (today - last_date).days
                elif solar_date:
                    days_since = (today - solar_date).days

            events.append({
                'id': e['id'],
                'name': e['name'],
                'cal_type': cal_type,
                'year': e['year'],
                'month': e['month'],
                'day': e['day'],
                'is_leap': is_leap,
                'note': e.get('note', ''),
                'display_mode': e.get('display_mode', 'full'),
                'category': e.get('category', 'custom'),
                'birth_year': birth_year,
                'repeat_annual': repeat_annual,
                'date_desc': date_desc,
                'this_year_date': this_year_str,
                'days_left': days_left,
                'days_since': days_since,
                'status': status,
                'exact_age': exact_age,
                'int_age': int_age,
                'xu_sui': xu_sui,
            })

        # 按今年日期排序（已过的排后面）
        def sort_key(x):
            if x['days_left'] is None:
                return (2, 0)
            if x['status'] == 'today':
                return (0, 0)
            if x['status'] == 'upcoming':
                return (0, x['days_left'] or 0)
            return (1, abs(x['days_left'] or 0))

        events.sort(key=sort_key)
        return jsonify({'success': True, 'events': events, 'has_lunar': HAS_LUNAR})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/countdown/events', methods=['POST'])
def create_event():
    """创建倒计时事件"""
    try:
        data = request.get_json(silent=True) or {}
        err, clean = _validate_event_dates(data)
        if err:
            return jsonify({'success': False, 'error': err}), 400
        if not clean['name']:
            return jsonify({'success': False, 'error': '请输入事件名称'}), 400

        conn = _get_db()
        conn.execute(
            "INSERT INTO events (name, cal_type, year, month, day, is_leap, note, display_mode, category, birth_year) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (clean['name'], clean['cal_type'], clean['year'], clean['month'], clean['day'],
             clean['is_leap'], clean['note'], clean['display_mode'], clean['category'], clean.get('birth_year', 0)))
        conn.commit()
        conn.close()
        _log(f'新增倒计时: {clean["name"]} ({clean["cal_type"]})')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/countdown/events/<int:eid>', methods=['PUT'])
def update_event(eid):
    """更新倒计时事件"""
    try:
        data = request.get_json(silent=True) or {}
        err, clean = _validate_event_dates(data)
        if err:
            return jsonify({'success': False, 'error': err}), 400
        if not clean['name']:
            return jsonify({'success': False, 'error': '请输入事件名称'}), 400

        conn = _get_db()
        conn.execute(
            "UPDATE events SET name=?, cal_type=?, year=?, month=?, day=?, is_leap=?, note=?, display_mode=?, category=?, birth_year=?, updated_at=datetime('now','localtime') WHERE id=?",
            (clean['name'], clean['cal_type'], clean['year'], clean['month'], clean['day'],
             clean['is_leap'], clean['note'], clean['display_mode'], clean['category'], clean.get('birth_year', 0), eid))
        conn.commit()
        conn.close()
        _log(f'更新倒计时: {clean["name"]} (ID:{eid})')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/countdown/events/<int:eid>', methods=['DELETE'])
def delete_event(eid):
    """删除倒计时事件"""
    try:
        conn = _get_db()
        conn.execute("DELETE FROM events WHERE id = ?", (eid,))
        conn.commit()
        conn.close()
        _log(f'删除倒计时: ID:{eid}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/countdown/lunar-check', methods=['POST'])
def lunar_check():
    """验证农历日期合法性并返回对应公历日期"""
    if not HAS_LUNAR:
        return jsonify({'success': False, 'error': '未安装农历库'}), 400

    data = request.get_json(silent=True) or {}
    year = data.get('year', 0)
    month = data.get('month', 0)
    day = data.get('day', 0)
    is_leap = data.get('is_leap', 0)

    try:
        lunar = ZhDate(int(year), int(month), int(day), bool(is_leap))
        solar = lunar.to_datetime().date()
        return jsonify({
            'success': True,
            'solar_date': solar.strftime('%Y-%m-%d'),
            'lunar_str': str(lunar),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': f'农历日期不合法: {str(e)}'}), 400


@bp.route('/api/countdown/status', methods=['GET'])
def status_check():
    """检查农历库安装状态及其他信息"""
    try:
        import importlib
        zhdate_version = None
        if HAS_LUNAR:
            try:
                m = importlib.import_module('zhdate')
                zhdate_version = getattr(m, '__version__', 'installed')
            except Exception:
                pass
        return jsonify({
            'success': True,
            'has_lunar': HAS_LUNAR,
            'zhdate_version': zhdate_version,
            'python_version': __import__('sys').version.split()[0],
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/countdown/install-lunar', methods=['POST'])
def install_lunar():
    """尝试安装农历库"""
    try:
        import subprocess, sys
        r = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', 'zhdate'],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            # 尝试重新导入（线程安全）
            global HAS_LUNAR
            with _HAS_LUNAR_LOCK:
                try:
                    from zhdate import ZhDate
                    HAS_LUNAR = True
                except ImportError:
                    pass
            return jsonify({
                'success': True,
                'has_lunar': HAS_LUNAR,
                'stdout': r.stdout.strip(),
            })
        return jsonify({
            'success': False,
            'has_lunar': HAS_LUNAR,
            'stderr': r.stderr.strip(),
        }), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/countdown/solar-to-lunar', methods=['POST'])
def solar_to_lunar_api():
    """公历转农历"""
    if not HAS_LUNAR:
        return jsonify({'success': False, 'error': '未安装农历库'}), 400

    data = request.get_json(silent=True) or {}
    year = data.get('year', 0)
    month = data.get('month', 0)
    day = data.get('day', 0)

    try:
        d = date(int(year), int(month), int(day))
        result = solar_to_lunar(d)
        if result:
            return jsonify({
                'success': True,
                'lunar_year': result[0],
                'lunar_month': result[1],
                'lunar_day': result[2],
                'is_leap': result[3],
                'lunar_str': result[4],
            })
        return jsonify({'success': False, 'error': '转换失败'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

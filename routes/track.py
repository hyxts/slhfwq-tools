# -*- coding: utf-8 -*-
"""开车轨迹与里程记录 Blueprint

手机 GPS 实时采集 -> 分段保存轨迹 -> 里程分段累计与总计
"""
import os, math
from datetime import datetime

from flask import Blueprint, jsonify, request, send_from_directory, make_response

from .utils import now_ts, make_logger, make_db

bp = Blueprint('track', __name__)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACK_DIR = os.path.join(BASE_DIR, '轨迹')
DB_FILE = os.path.join(TRACK_DIR, 'track.db')
LOG_FILE = os.path.join(TRACK_DIR, 'track.log')

_get_db = make_db(DB_FILE)
_log = make_logger(LOG_FILE)

# ========== 采集过滤阈值（前端使用同一套规则，保证两端结果一致） ==========
MAX_ACCURACY_M = 100.0    # 精度（米）差于此值的点直接丢弃
MIN_STEP_M = 5.0          # 相邻点最小位移，低于此值视为 GPS 静止抖动
MAX_SPEED_KMH = 300.0     # 相邻点推算速度超过此值视为异常跳点
NAME_MAX = 20             # 分段名称最大字数


def init_db():
    """初始化数据库表"""
    try:
        os.makedirs(TRACK_DIR, exist_ok=True)
        conn = _get_db()
        try:
            conn.execute('''CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT DEFAULT '',
                note TEXT DEFAULT '',
                started_at TEXT DEFAULT '',
                ended_at TEXT DEFAULT '',
                distance_m REAL DEFAULT 0,
                duration_sec INTEGER DEFAULT 0,
                point_count INTEGER DEFAULT 0,
                max_speed_kmh REAL DEFAULT 0,
                avg_speed_kmh REAL DEFAULT 0,
                status TEXT DEFAULT 'recording',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seg_id INTEGER NOT NULL,
                seq INTEGER DEFAULT 0,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                accuracy REAL DEFAULT 0,
                speed_kmh REAL DEFAULT 0,
                ts TEXT DEFAULT ''
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_points_seg ON points(seg_id, seq)')
            conn.commit()
        finally:
            conn.close()
        _log('数据库初始化完成')
    except Exception as e:
        _log(f'数据库初始化失败: {e}')


# ==================== 工具函数 ====================

def _haversine(lat1, lng1, lat2, lng2):
    """两点球面距离（米）"""
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def _parse_ts(val):
    """解析 ISO 时间字符串，失败返回 None"""
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def _to_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _sanitize_points(raw):
    """过滤非法点与低质量点，返回规范化的点列表"""
    out = []
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        lat = _to_float(p.get('lat'), None)
        lng = _to_float(p.get('lng'), None)
        if lat is None or lng is None:
            continue
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            continue
        acc = _to_float(p.get('accuracy'))
        if acc > MAX_ACCURACY_M:
            continue
        spd = _to_float(p.get('speed_kmh'))
        if spd < 0 or spd > MAX_SPEED_KMH:
            spd = 0.0
        out.append({'lat': round(lat, 7), 'lng': round(lng, 7),
                    'accuracy': round(acc, 1), 'speed_kmh': round(spd, 1),
                    'ts': str(p.get('ts') or '')[:32]})
    return out


def _summarize(pts):
    """基于全部有效点结算里程、时长与速度"""
    empty = {'distance_m': 0.0, 'duration_sec': 0, 'point_count': 0,
             'max_speed_kmh': 0.0, 'avg_speed_kmh': 0.0}
    if not pts:
        return empty
    dist = 0.0
    max_spd = 0.0
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        d = _haversine(a['lat'], a['lng'], b['lat'], b['lng'])
        if d < MIN_STEP_M:
            continue
        t1, t2 = _parse_ts(a['ts']), _parse_ts(b['ts'])
        dt = (t2 - t1).total_seconds() if (t1 and t2) else 0
        spd = 0.0
        if dt > 0:
            spd = d / dt * 3.6
            if spd > MAX_SPEED_KMH:
                continue  # 异常跳点不计入里程
        spd = max(spd, b['speed_kmh'] if b['speed_kmh'] < MAX_SPEED_KMH else 0.0)
        if spd > max_spd:
            max_spd = spd
        dist += d
    t_first, t_last = _parse_ts(pts[0]['ts']), _parse_ts(pts[-1]['ts'])
    duration = int((t_last - t_first).total_seconds()) if (t_first and t_last) else 0
    if duration < 0:
        duration = 0
    avg = (dist / duration * 3.6) if duration > 0 else 0.0
    return {'distance_m': round(dist, 1), 'duration_sec': duration,
            'point_count': len(pts), 'max_speed_kmh': round(max_spd, 1),
            'avg_speed_kmh': round(avg, 1)}


def _load_points(conn, seg_id):
    """读取某分段的全部点"""
    rows = conn.execute(
        'SELECT lat, lng, accuracy, speed_kmh, ts FROM points WHERE seg_id=? ORDER BY seq, id',
        (seg_id,)).fetchall()
    return [dict(r) for r in rows]


def _refresh_segment(conn, seg_id):
    """重算并写回分段统计（服务端为权威数据源）"""
    stats = _summarize(_load_points(conn, seg_id))
    conn.execute('''UPDATE segments SET distance_m=?, duration_sec=?, point_count=?,
                    max_speed_kmh=?, avg_speed_kmh=? WHERE id=?''',
                 (stats['distance_m'], stats['duration_sec'], stats['point_count'],
                  stats['max_speed_kmh'], stats['avg_speed_kmh'], seg_id))
    conn.commit()
    return stats


# ==================== API ====================

@bp.route('/api/track/start', methods=['POST'])
def track_start():
    """开始一段新记录（若已有未结束的段则复用，避免产生幽灵分段）"""
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()[:NAME_MAX]
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT id FROM segments WHERE status='recording' ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                seg_id = row['id']
                if name:
                    conn.execute('UPDATE segments SET name=? WHERE id=?', (name, seg_id))
                    conn.commit()
            else:
                if not name:
                    name = now_ts().strftime('%m-%d %H:%M 行程')
                cur = conn.execute(
                    "INSERT INTO segments (name, started_at, status) VALUES (?, ?, 'recording')",
                    (name, now_ts().strftime('%Y-%m-%d %H:%M:%S')))
                seg_id = cur.lastrowid
                conn.commit()
        finally:
            conn.close()
        _log(f'开始记录 seg={seg_id}')
        return jsonify({'success': True, 'seg_id': seg_id, 'name': name})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/track/push', methods=['POST'])
def track_push():
    """增量上传轨迹点，返回该段最新统计"""
    try:
        data = request.get_json(silent=True) or {}
        seg_id = data.get('seg_id')
        if not seg_id:
            return jsonify({'success': False, 'error': '缺少 seg_id'}), 400
        pts = _sanitize_points(data.get('points'))
        if not pts:
            return jsonify({'success': True, 'saved': 0, 'stats': None})
        conn = _get_db()
        try:
            row = conn.execute("SELECT id, status FROM segments WHERE id=?", (seg_id,)).fetchone()
            if not row:
                return jsonify({'success': False, 'error': '分段不存在'}), 404
            if row['status'] != 'recording':
                return jsonify({'success': False, 'error': '该分段已结束'}), 409
            seq_row = conn.execute(
                'SELECT COALESCE(MAX(seq), -1) AS m FROM points WHERE seg_id=?', (seg_id,)).fetchone()
            seq = int(seq_row['m']) + 1
            conn.executemany(
                '''INSERT INTO points (seg_id, seq, lat, lng, accuracy, speed_kmh, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                [(seg_id, seq + i, p['lat'], p['lng'], p['accuracy'], p['speed_kmh'], p['ts'])
                 for i, p in enumerate(pts)])
            conn.commit()
            stats = _refresh_segment(conn, seg_id)
        finally:
            conn.close()
        return jsonify({'success': True, 'saved': len(pts), 'stats': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/track/finish', methods=['POST'])
def track_finish():
    """结束当前分段并保存名称/备注"""
    try:
        data = request.get_json(silent=True) or {}
        seg_id = data.get('seg_id')
        if not seg_id:
            return jsonify({'success': False, 'error': '缺少 seg_id'}), 400
        name = str(data.get('name') or '').strip()[:NAME_MAX]
        note = str(data.get('note') or '').strip()[:200]
        conn = _get_db()
        try:
            row = conn.execute('SELECT id FROM segments WHERE id=?', (seg_id,)).fetchone()
            if not row:
                return jsonify({'success': False, 'error': '分段不存在'}), 404
            stats = _refresh_segment(conn, seg_id)
            conn.execute(
                '''UPDATE segments SET status='finished', ended_at=?,
                          name=COALESCE(NULLIF(?, ''), name), note=? WHERE id=?''',
                (now_ts().strftime('%Y-%m-%d %H:%M:%S'), name, note, seg_id))
            conn.commit()
        finally:
            conn.close()
        _log(f'结束记录 seg={seg_id} 里程={stats["distance_m"]}m')
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/track/active', methods=['GET'])
def track_active():
    """获取未结束的分段（用于页面刷新后继续记录）"""
    try:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT * FROM segments WHERE status='recording' ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return jsonify({'success': True, 'data': None})
            seg = dict(row)
            seg['points'] = _load_points(conn, seg['id'])
        finally:
            conn.close()
        return jsonify({'success': True, 'data': seg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/track/segments', methods=['GET'])
def track_segments():
    """分段列表 + 今日统计 + 全部总计"""
    try:
        try:
            limit = int(request.args.get('limit', 50))
        except (TypeError, ValueError):
            limit = 50
        limit = min(max(limit, 1), 200)
        conn = _get_db()
        try:
            rows = conn.execute(
                '''SELECT id, name, note, started_at, ended_at, distance_m, duration_sec,
                          point_count, max_speed_kmh, avg_speed_kmh, status
                   FROM segments ORDER BY id DESC LIMIT ?''', (limit,)).fetchall()
            data = [dict(r) for r in rows]
            agg = conn.execute(
                '''SELECT COUNT(*) AS segs, COALESCE(SUM(distance_m),0) AS dist,
                          COALESCE(SUM(duration_sec),0) AS dur,
                          COALESCE(MAX(max_speed_kmh),0) AS mx
                   FROM segments WHERE status='finished' ''').fetchone()
            today = now_ts().strftime('%Y-%m-%d')
            tdy = conn.execute(
                '''SELECT COUNT(*) AS segs, COALESCE(SUM(distance_m),0) AS dist,
                          COALESCE(SUM(duration_sec),0) AS dur
                   FROM segments WHERE status='finished' AND substr(started_at,1,10)=?''',
                (today,)).fetchone()
        finally:
            conn.close()
        return jsonify({
            'success': True,
            'data': data,
            'total': {'segments': agg['segs'], 'distance_m': round(agg['dist'] or 0, 1),
                      'duration_sec': agg['dur'] or 0, 'max_speed_kmh': round(agg['mx'] or 0, 1)},
            'today': {'segments': tdy['segs'], 'distance_m': round(tdy['dist'] or 0, 1),
                      'duration_sec': tdy['dur'] or 0},
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/track/segment/<int:sid>', methods=['GET'])
def track_segment_detail(sid):
    """分段详情（含全部轨迹点）"""
    try:
        conn = _get_db()
        try:
            row = conn.execute('SELECT * FROM segments WHERE id=?', (sid,)).fetchone()
            if not row:
                return jsonify({'success': False, 'error': '分段不存在'}), 404
            seg = dict(row)
            seg['points'] = _load_points(conn, sid)
        finally:
            conn.close()
        return jsonify({'success': True, 'data': seg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/track/segment/<int:sid>', methods=['POST'])
def track_segment_update(sid):
    """修改分段名称与备注"""
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()[:NAME_MAX]
        note = str(data.get('note') or '').strip()[:200]
        if not name:
            return jsonify({'success': False, 'error': '名称不能为空'}), 400
        conn = _get_db()
        try:
            row = conn.execute('SELECT id FROM segments WHERE id=?', (sid,)).fetchone()
            if not row:
                return jsonify({'success': False, 'error': '分段不存在'}), 404
            conn.execute('UPDATE segments SET name=?, note=? WHERE id=?', (name, note, sid))
            conn.commit()
        finally:
            conn.close()
        _log(f'更新分段 seg={sid} 名称={name}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/track/segment/<int:sid>', methods=['DELETE'])
def track_segment_delete(sid):
    """删除分段及其轨迹点"""
    try:
        conn = _get_db()
        try:
            row = conn.execute('SELECT id FROM segments WHERE id=?', (sid,)).fetchone()
            if not row:
                return jsonify({'success': False, 'error': '分段不存在'}), 404
            conn.execute('DELETE FROM points WHERE seg_id=?', (sid,))
            conn.execute('DELETE FROM segments WHERE id=?', (sid,))
            conn.commit()
        finally:
            conn.close()
        _log(f'删除分段 seg={sid}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== 前端页面与 PWA 资源 ====================

@bp.route('/track')
@bp.route('/track/')
def track_index():
    """记录页面：禁用缓存，保证手机端拿到的始终是最新版本"""
    resp = make_response(send_from_directory(TRACK_DIR, 'index.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@bp.route('/track/manifest.json')
def track_manifest():
    return send_from_directory(TRACK_DIR, 'manifest.json')


@bp.route('/track/icon-192.svg')
def track_icon_192():
    return send_from_directory(TRACK_DIR, 'icon-192.svg')


@bp.route('/track/icon-512.svg')
def track_icon_512():
    return send_from_directory(TRACK_DIR, 'icon-512.svg')

# -*- coding: utf-8 -*-
# pyright: reportUninitializedInstanceVariable=false, reportAssignmentType=false
# pyright: reportMissingTypeArgument=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportAny=false, reportExplicitAny=false
"""
单元测试 - 个人工具箱
运行方式: python -m pytest tests/ -v
或: python tests/test_app.py
"""
import sys
import os
import unittest
import tempfile
import sqlite3
from typing import override

# 将项目根目录加入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
from flask.testing import FlaskClient


class TestAppBasics(unittest.TestCase):
    """应用基础测试"""

    client: FlaskClient = None

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_index_200(self) -> None:
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)

    def test_api_version(self) -> None:
        r = self.client.get('/api/version')
        self.assertEqual(r.status_code, 200)
        data: dict = r.get_json()
        self.assertIn('version', data)
        self.assertIn('build', data)

    def test_api_health(self) -> None:
        r = self.client.get('/api/ping')
        self.assertEqual(r.status_code, 200)
        data: dict = r.get_json()
        self.assertEqual(data.get('status'), 'ok')

    def test_nav_page(self) -> None:
        r = self.client.get('/nav')
        self.assertEqual(r.status_code, 200)

    def test_static_icon(self) -> None:
        r = self.client.get('/nav/icon-192.svg')
        self.assertIn(r.status_code, [200, 404])

    def test_404_unknown(self) -> None:
        r = self.client.get('/nonexistent-path-xyz')
        self.assertIn(r.status_code, [404, 302])


class TestRenqingAPI(unittest.TestCase):
    """人情礼金 API 测试"""

    client: FlaskClient = None

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_list_events(self) -> None:
        r = self.client.get('/api/renqing/events')
        self.assertEqual(r.status_code, 200)
        data: dict = r.get_json()
        self.assertIn('success', data)

    def test_list_events_with_params(self) -> None:
        r = self.client.get('/api/renqing/events?page=1&page_size=5')
        self.assertEqual(r.status_code, 200)

    def test_stats(self) -> None:
        r = self.client.get('/api/renqing/stats')
        self.assertEqual(r.status_code, 200)

    def test_list_names(self) -> None:
        r = self.client.get('/api/renqing/names')
        self.assertEqual(r.status_code, 200)


class TestGPAAPI(unittest.TestCase):
    """绩点 API 测试"""

    client: FlaskClient = None

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_page_accessible(self) -> None:
        r = self.client.get('/gpa')
        self.assertEqual(r.status_code, 200)

    def test_list_courses(self) -> None:
        r = self.client.get('/api/gpa/courses')
        self.assertEqual(r.status_code, 200)
        data: dict = r.get_json()
        self.assertIn('success', data)

    def test_get_stats(self) -> None:
        r = self.client.get('/api/gpa/stats')
        self.assertEqual(r.status_code, 200)


class TestHSGradesAPI(unittest.TestCase):
    """成绩 API 测试"""

    client: FlaskClient = None

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_page_accessible(self) -> None:
        r = self.client.get('/hsgrades')
        self.assertEqual(r.status_code, 200)

    def test_list_exams(self) -> None:
        r = self.client.get('/api/hsgrades/exams')
        self.assertEqual(r.status_code, 200)
        data: dict = r.get_json()
        self.assertIn('success', data)


class TestAccountingAPI(unittest.TestCase):
    """记账 API 测试"""

    client: FlaskClient = None

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_page_accessible(self) -> None:
        r = self.client.get('/accounting')
        self.assertEqual(r.status_code, 200)

    def test_list_records(self) -> None:
        r = self.client.get('/api/accounting/records')
        self.assertEqual(r.status_code, 200)
        data: dict = r.get_json()
        self.assertIn('success', data)

    def test_get_stats(self) -> None:
        r = self.client.get('/api/accounting/stats')
        self.assertEqual(r.status_code, 200)

    def test_list_categories(self) -> None:
        r = self.client.get('/api/accounting/categories')
        self.assertEqual(r.status_code, 200)


class TestCountdownAPI(unittest.TestCase):
    """倒计时 API 测试"""

    client: FlaskClient = None

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_page_accessible(self) -> None:
        r = self.client.get('/countdown')
        self.assertEqual(r.status_code, 200)

    def test_list_events(self) -> None:
        r = self.client.get('/api/countdown/events')
        self.assertEqual(r.status_code, 200)
        data: dict = r.get_json()
        self.assertIn('success', data)


class TestLedgerAPI(unittest.TestCase):
    """专业记账会计 API 测试（需要登录会话）"""

    client: FlaskClient = None

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True
        with cls.client.session_transaction() as sess:  # type: ignore[attr-defined]
            sess['auth'] = True

    def test_page_accessible(self) -> None:
        r = self.client.get('/ledger')
        self.assertEqual(r.status_code, 200)
        self.assertIn('记账会计', r.get_data(as_text=True))

    def test_manifest(self) -> None:
        r = self.client.get('/ledger/manifest.json')
        self.assertEqual(r.status_code, 200)

    def test_list_accounts(self) -> None:
        r = self.client.get('/api/ledger/accounts')
        self.assertEqual(r.status_code, 200)
        data: dict = r.get_json()
        self.assertIn('success', data)
        # 空库/已种子库都应返回数据数组
        self.assertIsInstance(data.get('data'), list)

    def test_dashboard(self) -> None:
        r = self.client.get('/api/ledger/dashboard')
        self.assertEqual(r.status_code, 200)
        data: dict = r.get_json()
        self.assertEqual(data.get('success'), True)
        self.assertIn('monthly', data)

    def test_trial(self) -> None:
        r = self.client.get('/api/ledger/trial?month=2026-08')
        self.assertEqual(r.status_code, 200)
        data: dict = r.get_json()
        self.assertIn('rows', data)

    def test_contacts(self) -> None:
        r = self.client.get('/api/ledger/contacts')
        self.assertEqual(r.status_code, 200)
        data: dict = r.get_json()
        self.assertEqual(data.get('success'), True)


class TestRateLimiting(unittest.TestCase):
    """速率限制测试"""

    client: FlaskClient = None

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_normal_request_passes(self) -> None:
        """正常请求不被限制"""
        r = self.client.get('/api/version')
        self.assertEqual(r.status_code, 200)

    def test_html_page_not_limited_easily(self) -> None:
        """HTML 页面有较高限制"""
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)


class TestDeployAPI(unittest.TestCase):
    """部署 API 测试"""

    client: FlaskClient = None

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_ping_no_token(self) -> None:
        """没有 token 也能 ping"""
        r = self.client.get('/api/ping')
        self.assertEqual(r.status_code, 200)

    def test_pull_without_token(self) -> None:
        """没有 token 应该被拒绝"""
        r = self.client.post('/api/git-pull')
        self.assertEqual(r.status_code, 403)

    def test_reload_without_token(self) -> None:
        """没有 token 应该被拒绝"""
        r = self.client.post('/api/reload-webapp')
        self.assertEqual(r.status_code, 403)


class TestDBConnection(unittest.TestCase):
    """数据库连接测试"""

    def test_sqlite_create_and_write(self) -> None:
        """测试 SQLite WAL 模式连接"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path: str = f.name
        try:
            conn = sqlite3.connect(db_path)
            _ = conn.execute('PRAGMA journal_mode=WAL')
            _ = conn.execute('CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)')
            _ = conn.execute('INSERT INTO test (name) VALUES (?)', ('hello',))
            conn.commit()
            row = conn.execute('SELECT name FROM test WHERE id=1').fetchone()
            self.assertEqual(row[0], 'hello')
            conn.close()
        finally:
            os.unlink(db_path)


if __name__ == '__main__':
    _ = unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
单元测试 - 个人工具箱
运行方式: python -m pytest tests/ -v
或: python tests/test_app.py
"""
import sys
import os
import json
import unittest
import tempfile
import sqlite3
import threading
import time

# 将项目根目录加入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


class TestAppBasics(unittest.TestCase):
    """应用基础测试"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_index_200(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)

    def test_api_version(self):
        r = self.client.get('/api/version')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('version', data)
        self.assertIn('build', data)

    def test_api_health(self):
        r = self.client.get('/api/ping')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data.get('status'), 'ok')

    def test_nav_page(self):
        r = self.client.get('/nav')
        self.assertEqual(r.status_code, 200)

    def test_static_icon(self):
        r = self.client.get('/nav/icon-192.svg')
        self.assertIn(r.status_code, [200, 404])

    def test_404_unknown(self):
        r = self.client.get('/nonexistent-path-xyz')
        self.assertIn(r.status_code, [404, 302])


class TestRenqingAPI(unittest.TestCase):
    """人情礼金 API 测试"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_list_events(self):
        r = self.client.get('/api/renqing/events')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('success', data)

    def test_list_events_with_params(self):
        r = self.client.get('/api/renqing/events?page=1&page_size=5')
        self.assertEqual(r.status_code, 200)

    def test_stats(self):
        r = self.client.get('/api/renqing/stats')
        self.assertEqual(r.status_code, 200)

    def test_list_names(self):
        r = self.client.get('/api/renqing/names')
        self.assertEqual(r.status_code, 200)


class TestGPAAPI(unittest.TestCase):
    """绩点 API 测试"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_page_accessible(self):
        r = self.client.get('/gpa')
        self.assertEqual(r.status_code, 200)

    def test_list_courses(self):
        r = self.client.get('/api/gpa/courses')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('success', data)

    def test_get_stats(self):
        r = self.client.get('/api/gpa/stats')
        self.assertEqual(r.status_code, 200)


class TestHSGradesAPI(unittest.TestCase):
    """成绩 API 测试"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_page_accessible(self):
        r = self.client.get('/hsgrades')
        self.assertEqual(r.status_code, 200)

    def test_list_exams(self):
        r = self.client.get('/api/hsgrades/exams')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('success', data)


class TestAccountingAPI(unittest.TestCase):
    """记账 API 测试"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_page_accessible(self):
        r = self.client.get('/accounting')
        self.assertEqual(r.status_code, 200)

    def test_list_records(self):
        r = self.client.get('/api/accounting/records')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('success', data)

    def test_get_stats(self):
        r = self.client.get('/api/accounting/stats')
        self.assertEqual(r.status_code, 200)

    def test_list_categories(self):
        r = self.client.get('/api/accounting/categories')
        self.assertEqual(r.status_code, 200)


class TestCountdownAPI(unittest.TestCase):
    """倒计时 API 测试"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_page_accessible(self):
        r = self.client.get('/countdown')
        self.assertEqual(r.status_code, 200)

    def test_list_events(self):
        r = self.client.get('/api/countdown/events')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('success', data)


class TestRateLimiting(unittest.TestCase):
    """速率限制测试"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_normal_request_passes(self):
        """正常请求不被限制"""
        r = self.client.get('/api/version')
        self.assertEqual(r.status_code, 200)

    def test_html_page_not_limited_easily(self):
        """HTML 页面有较高限制"""
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)


class TestDeployAPI(unittest.TestCase):
    """部署 API 测试"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()
        app.config['TESTING'] = True

    def test_ping_no_token(self):
        """没有 token 也能 ping"""
        r = self.client.get('/api/ping')
        self.assertEqual(r.status_code, 200)

    def test_pull_without_token(self):
        """没有 token 应该被拒绝"""
        r = self.client.post('/api/git-pull')
        self.assertEqual(r.status_code, 403)

    def test_reload_without_token(self):
        """没有 token 应该被拒绝"""
        r = self.client.post('/api/reload-webapp')
        self.assertEqual(r.status_code, 403)


class TestDBConnection(unittest.TestCase):
    """数据库连接测试"""

    def test_sqlite_create_and_write(self):
        """测试 SQLite WAL 模式连接"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)')
            conn.execute('INSERT INTO test (name) VALUES (?)', ('hello',))
            conn.commit()
            row = conn.execute('SELECT name FROM test WHERE id=1').fetchone()
            self.assertEqual(row[0], 'hello')
            conn.close()
        finally:
            os.unlink(db_path)


if __name__ == '__main__':
    unittest.main(verbosity=2)

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

import app as app_mod
import routes.ledger as ledger_mod
from routes.utils import make_db

# ledger 模块真实的 DB 连接工厂（扩展测试替换后需要恢复）
_REAL_LEDGER_GET_DB = ledger_mod._get_db


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


class TestLedgerExtAPI(unittest.TestCase):
    """专业记账扩展 API 测试（辅助核算/凭证审核/红冲/模板/结转/结账）

    使用独立临时数据库，每个测试重建干净库，不触碰真实数据。
    """

    client: FlaskClient = None
    _tmp_dir: str = ''
    _names: set[str] = set()
    _seq: int = 0

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True
        with cls.client.session_transaction() as sess:  # type: ignore[attr-defined]
            sess['auth'] = True
        cls._tmp_dir = tempfile.mkdtemp(prefix='ledger_ext_')
        cls._names = {'测试客户', '测试客户2', '测试客户3', '测试应收-甲', '测试模板'}
        cls._seq = 0

    @classmethod
    @override
    def tearDownClass(cls) -> None:
        import gc
        import shutil
        gc.collect()
        shutil.rmtree(cls._tmp_dir, ignore_errors=True)
        ledger_mod._get_db = _REAL_LEDGER_GET_DB  # 恢复真实库连接，避免污染其它测试

    def _new_db(self) -> str:
        """为当前测试创建唯一临时库文件，替换连接工厂并初始化表结构"""
        import uuid
        self._seq += 1
        name = f'ledger_ext_{self._seq}_{uuid.uuid4().hex[:8]}.db'
        path = os.path.join(self._tmp_dir, name)
        ledger_mod._get_db = make_db(path)  # 替换连接工厂，隔离真实库
        ledger_mod._init_db_impl()
        return path

    def setUp(self) -> None:
        self._new_db()
        app_mod._rate_limits.clear()  # 避免速率限制干扰测试

    def _call(self, method: str, url: str, **kw):
        """清限流后调用 API"""
        app_mod._rate_limits.clear()
        r = getattr(self.client, method)(url, **kw)
        try:
            return r.status_code, r.get_json()
        except Exception:
            return r.status_code, None

    def _acct(self, code: str) -> dict:
        _, data = self._call('get', '/api/ledger/accounts')
        accts: list = data['data'] or []
        for a in accts:
            if a['code'] == code:
                return a
        raise AssertionError(f'科目 {code} 不存在')

    def _mk_dim(self, name: str) -> int:
        """创建辅助维度并回查 id（创建接口不返回 id）"""
        sc, d = self._call('post', '/api/ledger/aux/dims', json={'name': name})
        assert sc == 200 and d.get('success'), (name, sc, d)
        _, dims = self._call('get', '/api/ledger/aux/dims')
        dim = [x for x in dims['data'] if x['name'] == name][0]
        return dim['id']

    def _mk_item(self, dim_id: int, name: str) -> int:
        """创建辅助项并回查 id"""
        sc, d = self._call('post', '/api/ledger/aux/items',
                           json={'dim_id': dim_id, 'name': name})
        assert sc == 200 and d.get('success'), (name, sc, d)
        _, dims = self._call('get', '/api/ledger/aux/dims')
        dim = [x for x in dims['data'] if x['id'] == dim_id][0]
        item = [i for i in dim['items'] if i['name'] == name][0]
        return item['id']

    def _del_dim(self, dim_id: int) -> None:
        """删除维度（先删项）"""
        _, dims = self._call('get', '/api/ledger/aux/dims')
        dim = [x for x in dims['data'] if x['id'] == dim_id]
        if not dim:
            return
        for it in dim[0].get('items') or []:
            self._call('delete', f"/api/ledger/aux/items/{it['id']}")
        self._call('delete', f'/api/ledger/aux/dims/{dim_id}')

    def _acct_id(self, code: str) -> int:
        return self._acct(code)['id']

    def test_pc_pages(self) -> None:
        """桌面版页面与移动页面均可访问"""
        r = self.client.get('/ledger/pc')
        self.assertEqual(r.status_code, 200)
        self.assertIn('记账会计 · 财务桌面', r.get_data(as_text=True))
        r2 = self.client.get('/ledger')
        self.assertEqual(r2.status_code, 200)

    def test_aux_dims_and_items_crud(self) -> None:
        """辅助核算维度/项 创建、查询、删除"""
        sc, d = self._call('get', '/api/ledger/aux/dims')
        self.assertEqual(sc, 200)
        self.assertEqual(d['success'], True)
        # 创建维度并回查
        dim_id = self._mk_dim('测试客户')
        # 建两项
        item_a = self._mk_item(dim_id, '公司A')
        self._mk_item(dim_id, '公司B')
        # 查询含子项
        _, dims = self._call('get', '/api/ledger/aux/dims')
        dim = [x for x in dims['data'] if x['name'] == '测试客户'][0]
        self.assertEqual(len(dim['items']), 2)
        # 删除项
        sc, _ = self._call('delete', f'/api/ledger/aux/items/{item_a}')
        self.assertEqual(sc, 200)
        _, dims = self._call('get', '/api/ledger/aux/dims')
        dim = [x for x in dims['data'] if x['name'] == '测试客户'][0]
        self.assertEqual(len(dim['items']), 1)
        # 删除维度（先删剩余辅助项）
        self._del_dim(dim['id'])
        _, dims = self._call('get', '/api/ledger/aux/dims')
        self.assertNotIn('测试客户', [x['name'] for x in dims['data']])

    def test_account_bind_and_aux_opening(self) -> None:
        """科目绑定维度 + 下级科目 + 辅助期初读写"""
        # 准备维度与科目
        dim_id = self._mk_dim('测试客户2')
        item_a = self._mk_item(dim_id, '公司A')
        item_b = self._mk_item(dim_id, '公司B')
        # 绑定维度到银行存款 1002
        cash = self._acct('1002')
        sc, d = self._call('put', f"/api/ledger/accounts/{cash['id']}", json={
            'code': '1002', 'name': '银行存款', 'category': 'asset', 'parent_code': '',
            'aux_dims': [str(dim_id)], 'opening': 0, 'remark': ''})
        self.assertEqual(sc, 200)
        # 新建下级科目 112299（父 1122），绑维度
        sc, d = self._call('post', '/api/ledger/accounts', json={
            'code': '112299', 'name': '测试应收-甲', 'category': 'asset',
            'parent_code': '1122', 'aux_dims': [str(dim_id)], 'opening': 0, 'remark': ''})
        self.assertEqual(sc, 200)
        # 写入辅助期初并回读
        sc, d = self._call('put', '/api/ledger/aux/openings', json={
            'account_code': '112299',
            'rows': [{'item_id': item_a, 'amount': 1000.0}, {'item_id': item_b, 'amount': 500.0}]})
        self.assertEqual(sc, 200)
        sc, d = self._call('get', '/api/ledger/aux/openings?account_code=112299')
        self.assertEqual(d['success'], True)
        self.assertEqual(len(d['data']), 2)
        # 清理：解除绑定、删除科目与维度
        sub = self._acct('112299')
        self._call('put', '/api/ledger/aux/openings', json={'account_code': '112299', 'rows': []})
        self._call('delete', f"/api/ledger/accounts/{sub['id']}")
        cash = self._acct('1002')
        self._call('put', f"/api/ledger/accounts/{cash['id']}", json={
            'code': '1002', 'name': '银行存款', 'category': 'asset', 'parent_code': '',
            'aux_dims': [], 'opening': 0, 'remark': ''})
        self._del_dim(dim_id)

    def test_voucher_aux_audit_void_reverse(self) -> None:
        """凭证带辅助核算 + 审核/作废/恢复 + 红冲"""
        dim_id = self._mk_dim('测试客户3')
        item_a = self._mk_item(dim_id, '公司A')
        # 绑定维度到 1122 应收账款
        ar = self._acct('1122')
        sc, _ = self._call('put', f"/api/ledger/accounts/{ar['id']}", json={
            'code': '1122', 'name': ar['name'], 'category': ar['category'],
            'parent_code': ar['parent_code'], 'aux_dims': [str(dim_id)],
            'opening': ar.get('opening') or 0, 'remark': ar.get('remark') or ''})
        self.assertEqual(sc, 200)
        # 正确维度凭证可过
        sc, d = self._call('post', '/api/ledger/vouchers', json={
            'vdate': '2099-01-05', 'summary': '带辅助凭证', 'attachments': 1,
            'entries': [
                {'summary': 'A公司挂账', 'account_code': '1122', 'debit': 1000, 'credit': 0,
                 'aux': [{'dim_id': dim_id, 'item_id': item_a}]},
                {'summary': '收现金', 'account_code': '1001', 'debit': 0, 'credit': 1000},
            ]})
        self.assertEqual(sc, 200)
        self.assertEqual(d['success'], True)
        vid: int = d['id']
        # 维度不存在 → 拒绝
        sc, bad = self._call('post', '/api/ledger/vouchers', json={
            'vdate': '2099-01-06', 'summary': '坏维度',
            'entries': [
                {'summary': '错维度', 'account_code': '1122', 'debit': 10, 'credit': 0,
                 'aux': [{'dim_id': 99999, 'item_id': item_a}]},
                {'summary': '收现金', 'account_code': '1001', 'debit': 0, 'credit': 10},
            ]})
        self.assertEqual(sc, 400)
        # 回读携带 aux 名称
        _, gv = self._call('get', f'/api/ledger/vouchers/{vid}')
        self.assertEqual(gv['success'], True)
        self.assertEqual(gv['entries'][0]['aux'][0]['item_name'], '公司A')
        # 审核
        sc, d = self._call('post', f'/api/ledger/vouchers/{vid}/audit', json={'action': 'audit'})
        self.assertEqual(sc, 200)
        self.assertEqual(d['success'], True)
        # 新建一张作废
        sc, v2 = self._call('post', '/api/ledger/vouchers', json={
            'vdate': '2099-01-06', 'summary': '待作废',
            'entries': [{'summary': 'a', 'account_code': '1001', 'debit': 20, 'credit': 0},
                        {'summary': 'b', 'account_code': '1001', 'debit': 0, 'credit': 20}]})
        self.assertEqual(sc, 200)
        v2id: int = v2['id']
        sc, _ = self._call('post', f'/api/ledger/vouchers/{v2id}/audit', json={'action': 'void'})
        self.assertEqual(sc, 200)
        _, gv2 = self._call('get', f'/api/ledger/vouchers/{v2id}')
        self.assertEqual(gv2['data']['status'], 'voided')
        # 红冲审核过的凭证
        sc, rev = self._call('post', f'/api/ledger/vouchers/{vid}/reverse')
        self.assertEqual(sc, 200)
        self.assertEqual(rev['success'], True)
        rev_id: int = rev['id']
        # 清理
        for xid in (vid, v2id, rev_id):
            self._call('delete', f'/api/ledger/vouchers/{xid}')
        ar = self._acct('1122')
        self._call('put', f"/api/ledger/accounts/{ar['id']}", json={
            'code': '1122', 'name': ar['name'], 'category': ar['category'],
            'parent_code': ar['parent_code'], 'aux_dims': [],
            'opening': ar.get('opening') or 0, 'remark': ar.get('remark') or ''})
        self._del_dim(dim_id)

    def test_template_crud(self) -> None:
        """凭证模板创建/查询/删除"""
        sc, d = self._call('post', '/api/ledger/templates', json={
            'name': '测试模板',
            'content': [{'summary': '借', 'account_code': '1002', 'debit': 0, 'credit': 0},
                        {'summary': '贷', 'account_code': '5001', 'debit': 0, 'credit': 0}]})
        self.assertEqual(sc, 200)
        self.assertEqual(d['success'], True)
        _, ts = self._call('get', '/api/ledger/templates')
        tmpl = [t for t in ts['data'] if t['name'] == '测试模板'][0]
        self.assertEqual(len(tmpl['content']), 2)
        sc, _ = self._call('delete', f"/api/ledger/templates/{tmpl['id']}")
        self.assertEqual(sc, 200)
        _, ts = self._call('get', '/api/ledger/templates')
        self.assertNotIn('测试模板', [t['name'] for t in ts['data']])

    def test_carry_profit_and_close(self) -> None:
        """结转损益 + 结账锁定 + 反结账"""
        # 动态取收入/费用科目
        _, accts = self._call('get', '/api/ledger/accounts')
        inc_code = [a['code'] for a in accts['data'] if a['category'] == 'income'][0]
        exp_code = [a['code'] for a in accts['data'] if a['category'] == 'expense'][0]
        # 造收入与费用凭证（含借银行存款平衡）
        sc, d = self._call('post', '/api/ledger/vouchers', json={
            'vdate': '2099-01-10', 'summary': '损益业务',
            'entries': [
                {'summary': '费用', 'account_code': exp_code, 'debit': 400, 'credit': 0},
                {'summary': '银行存款', 'account_code': '1002', 'debit': 200, 'credit': 0},
                {'summary': '收入', 'account_code': inc_code, 'debit': 0, 'credit': 600},
            ]})
        self.assertEqual(sc, 200)
        vid: int = d['id']
        # 结转损益
        sc, cp = self._call('post', '/api/ledger/carry-profit', json={'month': '2099-01'})
        self.assertEqual(sc, 200)
        self.assertEqual(cp['success'], True)
        # 结账
        sc, d = self._call('post', '/api/ledger/closing/2099-01')
        self.assertEqual(sc, 200)
        self.assertEqual(d['success'], True)
        # 已结账不允许删除凭证
        sc, d = self._call('delete', f'/api/ledger/vouchers/{vid}')
        self.assertEqual(sc, 400)
        # 重复结账被拒
        sc, d = self._call('post', '/api/ledger/closing/2099-01')
        self.assertEqual(sc, 400)
        self.assertIn('已结账', d.get('error') or '')
        # 反结账
        sc, d = self._call('delete', '/api/ledger/closing/2099-01')
        self.assertEqual(sc, 200)
        self.assertEqual(d['success'], True)
        # 清理
        sc, d = self._call('delete', f'/api/ledger/carry-profit?month=2099-01')
        self.assertEqual(sc, 200)
        self._call('delete', f'/api/ledger/vouchers/{vid}')

    def test_periods_listing(self) -> None:
        """期间列表接口可用"""
        sc, d = self._call('get', '/api/ledger/periods?year=2099')
        self.assertEqual(sc, 200)
        self.assertEqual(d['success'], True)
        self.assertIsInstance(d.get('data'), list)


class TestPurchase(unittest.TestCase):
    """采购台账模块测试（需要登录会话）"""

    client: FlaskClient = None
    _created_ids: list[int] = []

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True
        with cls.client.session_transaction() as sess:  # type: ignore[attr-defined]
            sess['auth'] = True
        cls._rate_json: int = app_mod.RATE_LIMIT_MAX_JSON
        cls._rate_html: int = app_mod.RATE_LIMIT_MAX_HTML
        app_mod.RATE_LIMIT_MAX_JSON = 100000
        app_mod.RATE_LIMIT_MAX_HTML = 100000

    @classmethod
    @override
    def tearDownClass(cls) -> None:
        # 注意顺序：先清数据，再恢复限流。反过来的话清理请求会被限流拦掉（429），
        # 测试数据就残留在真实库里，下一轮跑测试会撞「名称已存在」。
        for rid in cls._created_ids:
            cls.client.delete(f'/api/purchase/records/{rid}')
        app_mod.RATE_LIMIT_MAX_JSON = cls._rate_json
        app_mod.RATE_LIMIT_MAX_HTML = cls._rate_html

    def _create(self, payload: dict) -> dict:
        r = self.client.post('/api/purchase/records', json=payload)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()['data']
        type(self)._created_ids.append(data['id'])
        return data

    def test_page_accessible(self) -> None:
        r = self.client.get('/purchase')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('采购台账', html)
        self.assertIn('基础配置', html)          # 配置面板入口随页面一起下发

    def test_manifest(self) -> None:
        r = self.client.get('/purchase/manifest.json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json().get('short_name'), '采购台账')

    def test_crud_and_derived(self) -> None:
        """新增 → 派生字段（不含税成本/未付余额/付款状态）→ 编辑 → 删除"""
        rec = self._create({'date': '2026-10-10', 'bill_no': 'T-001',
                            'supplier': '测试供应商甲', 'category': '测试品类',
                            'summary': '测试摘要', 'invoice_type': '专票',
                            'total_amount': 1000, 'input_tax': 90,
                            'invoiced_amount': 500, 'paid_amount': 0,
                            'pay_account': '测试账户'})
        self.assertEqual(rec['net_cost'], 910.0)
        self.assertEqual(rec['unpaid_balance'], 1000.0)
        self.assertEqual(rec['pay_status'], '未付款')

        rid = rec['id']
        r = self.client.post(f'/api/purchase/records/{rid}',
                             json={'date': '2026-10-10', 'supplier': '测试供应商甲',
                                   'invoice_type': '专票', 'total_amount': 1000,
                                   'input_tax': 90, 'paid_amount': 400})
        self.assertEqual(r.status_code, 200)
        upd = r.get_json()['data']
        self.assertEqual(upd['unpaid_balance'], 600.0)
        self.assertEqual(upd['pay_status'], '部分付款')

        r = self.client.post(f'/api/purchase/records/{rid}',
                             json={'date': '2026-10-10', 'supplier': '测试供应商甲',
                                   'total_amount': 1000, 'paid_amount': 1000})
        self.assertEqual(r.get_json()['data']['pay_status'], '已付清')

        r = self.client.delete(f'/api/purchase/records/{rid}')
        self.assertEqual(r.status_code, 200)
        type(self)._created_ids.remove(rid)

    def test_invalid_params(self) -> None:
        cases: list[dict] = [
            {'date': '2026-10-10'},                                   # 缺供应商
            {'date': '2026-10-10', 'supplier': '某', 'total_amount': 0},   # 金额必须 > 0
            {'date': '2026/10/10', 'supplier': '某', 'total_amount': 10},  # 日期格式
            {'date': '2026-10-10', 'supplier': '某', 'total_amount': 10,
             'input_tax': 99},                                        # 进项税 > 总额
            {'date': '2026-10-10', 'supplier': '某', 'total_amount': 10,
             'paid_amount': -1},                                      # 负数
            {'supplier': '某', 'total_amount': 10},                   # 缺日期
        ]
        for payload in cases:
            r = self.client.post('/api/purchase/records', json=payload)
            self.assertEqual(r.status_code, 400, str(payload))
            self.assertFalse(r.get_json()['success'])

    def test_stats_and_filters(self) -> None:
        a = self._create({'date': '2026-10-11', 'supplier': '测试供应商乙',
                          'category': '测试品类', 'invoice_type': '无票(不抵)',
                          'total_amount': 200.5, 'input_tax': 0, 'paid_amount': 200.5})
        b = self._create({'date': '2026-10-12', 'supplier': '测试供应商乙',
                          'category': '测试品类2', 'invoice_type': '专票',
                          'total_amount': 99.5, 'paid_amount': 0})
        r = self.client.get('/api/purchase/stats?supplier=测试供应商乙')
        self.assertEqual(r.status_code, 200)
        s = r.get_json()['data']
        self.assertEqual(s['count'], 2)
        self.assertEqual(s['total'], 300.0)
        self.assertEqual(s['unpaid'], 99.5)
        self.assertEqual(s['net_cost'], 300.0)
        self.assertEqual(len(s['by_supplier']), 1)

        r = self.client.get('/api/purchase/records?supplier=测试供应商乙&status=已付清')
        ids = [x['id'] for x in r.get_json()['data']]
        self.assertIn(a['id'], ids)
        self.assertNotIn(b['id'], ids)

        r = self.client.get('/api/purchase/export?supplier=测试供应商乙')
        self.assertEqual(r.status_code, 200)
        self.assertIn('测试供应商乙'.encode('utf-8'), r.get_data())

    def test_options(self) -> None:
        r = self.client.get('/api/purchase/options')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()['data']
        self.assertIn('无票(不抵)', d['invoice_types'])

    def test_anonymous_blocked(self) -> None:
        anon = app.test_client()
        self.assertNotEqual(
            anon.post('/api/purchase/records', json={'date': '2026-10-10',
                                                     'supplier': 'x',
                                                     'total_amount': 1}).status_code, 200)


class TestPurchaseConfig(unittest.TestCase):
    """采购模块的基础配置：供应商 / 品类 / 付款账户（需要登录会话）"""

    client: FlaskClient = None
    _supplier_ids: list[int] = []
    _dict_ids: list[int] = []
    _record_ids: list[int] = []

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.client = app.test_client()
        app.config['TESTING'] = True
        with cls.client.session_transaction() as sess:  # type: ignore[attr-defined]
            sess['auth'] = True
        cls._rate_json: int = app_mod.RATE_LIMIT_MAX_JSON
        cls._rate_html: int = app_mod.RATE_LIMIT_MAX_HTML
        app_mod.RATE_LIMIT_MAX_JSON = 100000
        app_mod.RATE_LIMIT_MAX_HTML = 100000
        cls._clean_leftovers()

    @classmethod
    def _clean_leftovers(cls) -> None:
        """清掉上一轮可能没删干净的本用例数据（否则撞 UNIQUE 名称，用例直接失败）"""
        from urllib.parse import quote
        for s in cls.client.get('/api/purchase/suppliers').get_json()['data']:
            if not s['name'].startswith('配置测试供应商'):
                continue
            for r in cls.client.get(
                    '/api/purchase/records?search=' + quote(s['name'])).get_json()['data']:
                if r['supplier'] == s['name']:
                    cls.client.delete(f'/api/purchase/records/{r["id"]}')
            cls.client.delete(f'/api/purchase/suppliers/{s["id"]}')
        for kind, items in cls.client.get('/api/purchase/dicts').get_json()['data'].items():
            for d in items:
                if d['name'].startswith('配置测试'):
                    cls.client.delete(f'/api/purchase/dicts/{d["id"]}')

    @classmethod
    @override
    def tearDownClass(cls) -> None:
        # 同上：先清数据，再恢复限流，否则清理请求会被限流拦掉留下脏数据
        for rid in cls._record_ids:
            cls.client.delete(f'/api/purchase/records/{rid}')
        for did in cls._dict_ids:
            cls.client.delete(f'/api/purchase/dicts/{did}')
        for sid in cls._supplier_ids:
            cls.client.delete(f'/api/purchase/suppliers/{sid}')
        app_mod.RATE_LIMIT_MAX_JSON = cls._rate_json
        app_mod.RATE_LIMIT_MAX_HTML = cls._rate_html

    def _supplier(self, payload: dict) -> dict:
        r = self.client.post('/api/purchase/suppliers', json=payload)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()['data']
        type(self)._supplier_ids.append(data['id'])
        return data

    def test_supplier_crud(self) -> None:
        """新增 → 重名拒绝 → 编辑 → 停用 → 无记录可删"""
        a = self._supplier({'name': '配置测试供应商A', 'contact': '张老板',
                            'phone': '13000000000', 'category': '配置测试品类',
                            'default_invoice': '专票', 'note': '测试备注'})
        self.assertEqual(a['category'], '配置测试品类')
        self.assertEqual(a['used_cnt'], 0)

        r = self.client.post('/api/purchase/suppliers', json={'name': '配置测试供应商A'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('已存在', r.get_json()['error'])

        r = self.client.post('/api/purchase/suppliers', json={'name': ''})
        self.assertEqual(r.status_code, 400)

        r = self.client.post(f'/api/purchase/suppliers/{a["id"]}',
                             json={'name': '配置测试供应商A', 'contact': '李老板',
                                   'default_invoice': '无票(不抵)'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['data']['contact'], '李老板')

        # 停用后 active=0，且仍查得到
        r = self.client.post(f'/api/purchase/suppliers/{a["id"]}',
                             json={'name': '配置测试供应商A', 'active': 0})
        self.assertEqual(r.get_json()['data']['active'], 0)
        rows = self.client.get('/api/purchase/suppliers').get_json()['data']
        a_row = next(x for x in rows if x['id'] == a['id'])
        self.assertEqual(a_row['active'], 0)
        self.assertEqual(len(self.client.get(
            '/api/purchase/suppliers?active_only=1').get_json()['data']),
            len(rows) - 1)

        # 没有采购记录 → 可以删除
        r = self.client.delete(f'/api/purchase/suppliers/{a["id"]}')
        self.assertEqual(r.status_code, 200)
        type(self)._supplier_ids.remove(a['id'])

    def test_rename_syncs_history(self) -> None:
        """供应商改名并同步时，历史采购记录跟着改；有记录的供应商不允许删除"""
        s = self._supplier({'name': '配置测试供应商B'})
        rec = self.client.post('/api/purchase/records', json={
            'date': '2026-10-15', 'supplier': '配置测试供应商B',
            'total_amount': 100, 'paid_amount': 0})
        rid = rec.get_json()['data']['id']
        type(self)._record_ids.append(rid)

        r = self.client.post(f'/api/purchase/suppliers/{s["id"]}',
                             json={'name': '配置测试供应商B改名', 'sync_history': 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['renamed'], 1)
        names = [x['supplier'] for x in
                 self.client.get('/api/purchase/records?search=配置测试供应商').get_json()['data']]
        self.assertIn('配置测试供应商B改名', names)
        self.assertNotIn('配置测试供应商B', names)

        # 有历史记录 → 禁止删除
        r = self.client.delete(f'/api/purchase/suppliers/{s["id"]}')
        self.assertEqual(r.status_code, 400)
        self.assertIn('停用', r.get_json()['error'])

    def test_dicts(self) -> None:
        """品类 / 付款账户字典：新增、重名拒绝、列表、删除"""
        r = self.client.post('/api/purchase/dicts', json={'kind': 'category',
                                                          'name': '配置测试品类X'})
        self.assertEqual(r.status_code, 200)
        did = r.get_json()['data']['id']
        type(self)._dict_ids.append(did)

        r = self.client.post('/api/purchase/dicts', json={'kind': 'category',
                                                          'name': '配置测试品类X'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('已存在', r.get_json()['error'])

        r = self.client.post('/api/purchase/dicts', json={'kind': '不存在的类型',
                                                          'name': 'x'})
        self.assertEqual(r.status_code, 400)

        names = [x['name'] for x in self.client.get(
            '/api/purchase/dicts?kind=category').get_json()['data']]
        self.assertIn('配置测试品类X', names)

        r = self.client.post(f'/api/purchase/dicts/{did}', json={'name': '配置测试品类Y'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['data']['name'], '配置测试品类Y')

        # 配置的品类要出现在表单建议里
        opts = self.client.get('/api/purchase/options').get_json()['data']
        self.assertIn('配置测试品类Y', opts['categories'])

        r = self.client.delete(f'/api/purchase/dicts/{did}')
        self.assertEqual(r.status_code, 200)
        type(self)._dict_ids.remove(did)

    def test_config_anonymous_blocked(self) -> None:
        anon = app.test_client()
        self.assertNotEqual(
            anon.post('/api/purchase/suppliers', json={'name': '匿名供应商'}).status_code, 200)
        self.assertNotEqual(
            anon.get('/api/purchase/suppliers').status_code, 200)


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

# -*- coding: utf-8 -*-
"""临时：在维护记录表末尾追加采购配置功能的记录"""
import io

PATH = '开发规范.md'
ROW = ('| 2026-10-18 | 采购模块：新增基础配置（供应商/品类/付款账户） | '
       '用户要求「设计一个配置功能，如可以编辑添加供应商」。配置数据落在同一个 '
       '`采购/purchase.db`（不新增模块目录，备份/部署清单无需改动）。'
       '1) **两张表**：`suppliers`（name UNIQUE、联系人、电话、主营品类、默认发票类型、'
       '备注、active 启用/停用）+ `purchase_dicts`（kind 区分 category/account 的通用字典，'
       'UNIQUE(kind,name)）；老库 `CREATE TABLE IF NOT EXISTS` 自动补表，无需迁移脚本；'
       '2) **API**：`/api/purchase/suppliers` 列表（带每家累计笔数/总额/未付，支持 q 搜索与 '
       'active_only=1）/ 新增 / 编辑 / 删除；`/api/purchase/dicts[?kind=]` 与 `/<id>` 增删改；'
       '重名走 UNIQUE 约束 → 捕获 sqlite3.IntegrityError 返回 400（不先查后插，并发不撞名）；'
       '3) **两条保护规则**：已有采购记录的供应商禁止删除（400 提示改用「停用」），'
       '避免历史台账失去归属；改名时可选同步历史（sync_history=1 → '
       'UPDATE purchases SET supplier=? WHERE supplier=?，返回 renamed 条数）；'
       '4) **前端**：页头「⚙ 配置」打开面板，三 tab（供应商/品类/付款账户），'
       '供应商行显示联系人电话与累计统计，支持编辑、停用/启用、删除；'
       '采购表单选到已建档供应商自动带出主营品类与默认发票类型；'
       '5) **options 取值改为**：配置里启用的 + 历史记录里出现过但没建档的名字'
       '（extra_suppliers）合并，老数据照样能选；品类/账户同理。'
       '**踩坑**：teardown 先恢复限流导致清理请求被 429 拦掉、脏数据留在真实库'
       '（下一轮撞「名称已存在」）→ 改为先清数据再恢复限流，并加 setUpClass 自清理，'
       '已记入规范 7.43。'
       '**验证**：py_compile 通过；新增 TestPurchaseConfig 4 条（供应商 CRUD 与重名/停用/'
       '删除保护、改名同步历史、字典 CRUD 与非法 kind、未登录拦截）全绿，连跑两轮稳定；'
       'test_page_accessible 加断言页面含「基础配置」；全量 51 条，16 个失败与基线一致。|\n')

with io.open(PATH, encoding='utf-8') as f:
    lines = f.readlines()
last = max(i for i, s in enumerate(lines) if s.startswith('| 2026-'))
lines.insert(last + 1, ROW)
with io.open(PATH, 'w', encoding='utf-8') as f:
    f.writelines(lines)
print('inserted after line', last + 1)

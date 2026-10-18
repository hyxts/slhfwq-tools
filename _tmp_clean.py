# -*- coding: utf-8 -*-
"""临时：清掉测试残留的采购/供应商数据"""
import sqlite3

conn = sqlite3.connect('采购/purchase.db')
for pat in ('配置测试%', '测试供应商%'):
    conn.execute('DELETE FROM purchases WHERE supplier LIKE ?', (pat,))
    conn.execute('DELETE FROM suppliers WHERE name LIKE ?', (pat,))
conn.commit()
print('suppliers:', conn.execute('SELECT id, name FROM suppliers').fetchall())
print('dicts:', conn.execute('SELECT id, kind, name FROM purchase_dicts').fetchall())
print('purchases:', conn.execute('SELECT id, supplier FROM purchases').fetchall())
conn.close()

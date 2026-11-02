"""验证 #163 合同管理：表+CRUD + 同步有效期到关联账号。临时库副本，不影响真实库。"""
import os, sys, tempfile, shutil
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "data", "guibeicai.db")
tmp = tempfile.mkdtemp(prefix="contract_")
TMP_DB = os.path.join(tmp, "t.db")
shutil.copyfile(SRC, TMP_DB)
os.environ["SQLITE_PATH"] = TMP_DB

from guibeicai.web import _db

_db.init_db()

# 1) 表存在
c = _db.get_conn().cursor()
tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
assert "contracts" in tabs, f"contracts 表缺失: {tabs}"
print("[OK] contracts 表已建")

# 2) 建一个关联用户（给一个初始有效期，便于观察同步覆盖）
_db.add_user("contract_link_user", "secret123", "merchant", [1], valid_from="2020-01-01", valid_until="2021-01-01")
linked = _db.authenticate_user("contract_link_user", "secret123")
assert linked, "关联用户认证失败"
linked_id = linked["id"]

# 3) 新增合同并同步有效期
eff = (date.today() - timedelta(days=1)).isoformat()
exp = (date.today() + timedelta(days=30)).isoformat()
cid = _db.add_contract(
    contract_no="HT-2026-001", name="测试合同", amount=12345.67, payment_method="月结",
    party_a="平台方", party_b="客户A", sign_date="2026-09-01",
    effective_date=eff, expiry_date=exp, linked_user_id=linked_id, remark="自动化测试",
)
assert cid > 0
print("[OK] add_contract 成功")

res = _db.sync_contract_to_user(cid)
assert res["ok"], res
u = _db.authenticate_user("contract_link_user", "secret123")
assert u["valid_from"] == eff and u["valid_until"] == exp, f"同步未生效: {u}"
print("[OK] 同步合同有效期 -> 关联账号 valid_from/valid_until 已更新")

# 4) get_all_contracts / get_contract
allc = _db.get_all_contracts()
assert any(x["id"] == cid for x in allc), "列表不含新合同"
ct = _db.get_contract(cid)
assert ct["contract_no"] == "HT-2026-001" and float(ct["amount"]) == 12345.67
print("[OK] get_all_contracts / get_contract 正常")

# 5) update_contract + 附件 + 再次同步
_db.set_contract_attachment(cid, "data/contracts/x.pdf", "x.pdf")
_db.update_contract(cid, name="测试合同(改)", expiry_date=(date.today() + timedelta(days=60)).isoformat())
ct2 = _db.get_contract(cid)
assert ct2["name"] == "测试合同(改)"
assert ct2["attachment_name"] == "x.pdf"
res2 = _db.sync_contract_to_user(cid)
assert res2["ok"]
u2 = _db.authenticate_user("contract_link_user", "secret123")
assert u2["valid_until"] == (date.today() + timedelta(days=60)).isoformat(), f"二次同步失败: {u2}"
print("[OK] update_contract + 附件 + 二次同步生效")

# 6) 未关联账号的同步应失败
cid2 = _db.add_contract(contract_no="HT-2", name="无关联", linked_user_id=None)
r3 = _db.sync_contract_to_user(cid2)
assert not r3["ok"] and "未关联" in r3["msg"], r3
print("[OK] 未关联账号同步被正确拒绝")

# 7) delete_contract
_db.delete_contract(cid)
assert _db.get_contract(cid) is None
print("[OK] delete_contract 正常")

print("\nALL_CONTRACT_OK")
shutil.rmtree(tmp, ignore_errors=True)

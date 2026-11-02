"""#39 系统性集成测试：全链路 + 多角色。

全链路（模拟真实业务闭环）：
  ① 渠道商录入门店 → ② 配置供货范围 → ③ 门店上传菜品/销量
  → ④ 执行预测 → ⑤ 生成采购单 → ⑥ 渠道工作台覆盖/缺口拆分
  → ⑦ POS 推送自动对接（自动建档 + 对账回灌）→ ⑧ 智能预警生成与推送去重

多角色（权限边界 + 租户隔离）：
  admin / channel / merchant 三类账号的页面访问权、门店可见范围校验。

隔离：SQLITE_PATH 指向临时库；外部依赖（LLM / 天气 / 地理编码 / 通知）全部打桩，
保证离线可重复运行。绝不触碰真实库。
"""
import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

TMP = os.path.join(tempfile.gettempdir(), "integration_fullchain.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["SQLITE_PATH"] = TMP
os.environ["GUOBEICAI_DISABLE_SEED"] = "1"

PASS = 0
FAIL = 0
STEPS = []


def check(step, name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")
    return cond


def section(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def raw(sql, params=()):
    con = sqlite3.connect(TMP)
    try:
        cur = con.execute(sql, params)
        rows = cur.fetchall()
        con.commit()
        return rows
    finally:
        con.close()


# ==================== 依赖导入 + 外部打桩 ====================
import guibeicai.web._db as db  # noqa: E402
from guibeicai.web import _predict_core as pc  # noqa: E402
from guibeicai.web import _ingredient_forecast as ingfc  # noqa: E402
from guibeicai.web import _alerts as alerts_mod  # noqa: E402
from guibeicai.web import _scheduler as sched  # noqa: E402
from guibeicai.data_import import pos_client  # noqa: E402

db.init_db()

# 打桩：LLM 菜品分析（返回空即用默认值）、天气文本、地理编码（避免真实网络）
pc.analyze_by_dishes = lambda *a, **k: {}
pc.get_future_factors = lambda *a, **k: "晴 26℃"
pc.city_to_coords = lambda *a, **k: (22.82, 108.37)


class _FakeNotifier:
    """记录推送调用，避免真实发邮件/企微。"""
    calls = []

    def __init__(self, *a, **k):
        pass

    def send_store_alert(self, store_name, alerts, targets):
        _FakeNotifier.calls.append({"store": store_name, "n_alerts": len(alerts), "targets": targets})
        return True


sched.StoreAlertNotifier = _FakeNotifier

# ==================== ① 多角色账号与门店 ====================
section("① 角色/账号准备（channel 录门店，merchant 绑定门店）")

# 渠道商先录入门店（模拟 21/23 页动作）
sid_a = db.upsert_store("集成测试店A", city="南宁市", province="广西壮族自治区",
                        owner_email="a@example.com")
sid_b = db.upsert_store("集成测试店B", city="桂林市", province="广西壮族自治区",
                        owner_email="b@example.com")
check("①", "渠道商录入门店A/B", sid_a and sid_b and sid_a != sid_b, f"{sid_a},{sid_b}")

# 再建账号（add_user 返回 bool；id 需回查）
ok_admin = db.add_user("it_admin", "Admin@123", role="admin")
ok_chan = db.add_user("it_channel", "Chan@123", role="channel")
ok_mer = db.add_user("it_merchant", "Mer@123", role="merchant", store_ids=[sid_a])
check("①", "创建 admin/channel/merchant 账号", ok_admin and ok_chan and ok_mer)

uid_admin = raw("SELECT id FROM users WHERE username='it_admin'")[0][0]
uid_chan = raw("SELECT id FROM users WHERE username='it_channel'")[0][0]
uid_mer = raw("SELECT id FROM users WHERE username='it_merchant'")[0][0]

# 渠道商绑定这两家门店（merchant 已在建号时绑 A）
db.set_user_store_ids(uid_chan, [sid_a, sid_b])

chan_user = db.get_user_by_id(uid_chan)
mer_user = db.get_user_by_id(uid_mer)
admin_user = db.get_user_by_id(uid_admin)

acc_chan = {s["id"] for s in db.get_accessible_stores(chan_user)}
check("①", "渠道商可见 A/B 两家门店", acc_chan == {sid_a, sid_b}, acc_chan)
acc_mer = {s["id"] for s in db.get_accessible_stores(mer_user)}
check("①", "门店账号仅可见自家 A（租户/门店隔离）", acc_mer == {sid_a}, acc_mer)

# ==================== ② 供货范围配置 ====================
section("② 渠道商为门店配置食材供货范围（子集）")
db.set_store_supply_scope(sid_a, [("白菜", "白菜"), ("土豆", "土豆")])
norms_a = db.get_store_supply_scope_norms(sid_a)
check("②", "门店A供货范围 = {白菜,土豆}", norms_a == {"白菜", "土豆"}, norms_a)

# ==================== ③ 门店上传菜品/配方/销量 ====================
section("③ 门店上传菜品配方 + 历史销量")
did_cabbage, is_new = db.upsert_dish_ex(sid_a, "醋溜白菜")
check("③", "新菜自动建档（bom_status=pending）", is_new and
      db.get_dish_by_id(did_cabbage)["name"] == "醋溜白菜")
db.replace_recipe_for_dish(did_cabbage, [
    {"ingredient_name": "白菜", "quantity": 0.2, "unit": "kg", "unit_price": 4.0, "category": "蔬菜"},
    {"ingredient_name": "干辣椒", "quantity": 0.01, "unit": "kg", "unit_price": 30.0, "category": "调料"},
])
# 为方便预测，把 BOM 标记为 ready
db.mark_bom_status(did_cabbage, "ready")
recs = db.get_recipes_by_dish(did_cabbage)
check("③", "配方写入 2 条食材", len(recs) == 2, len(recs))

# 历史销量（<14 天 → 走均值路径，避免 Prophet/天气网络依赖）
base = date.today() - timedelta(days=12)
for i in range(10):
    db.upsert_sales_history(sid_a, did_cabbage, (base + timedelta(days=i)).isoformat(), 10 + i, 18.0)
cnt = raw("SELECT COUNT(*) FROM sales_history WHERE store_id=?", (sid_a,))[0][0]
check("③", "历史销量入库 10 条", cnt == 10, cnt)

# ==================== ④ 执行预测 ====================
section("④ 执行预测（LLM/天气已打桩，离线可跑）")
pd1 = (date.today() + timedelta(days=1)).isoformat()
pd2 = (date.today() + timedelta(days=2)).isoformat()
try:
    res = pc.run_prediction(sid_a, [pd1, pd2], store_name="集成测试店A",
                            store_city="南宁市", progress_cb=None, log_action=None,
                            user=admin_user)
    ok_pred = isinstance(res, dict) and res.get("predictions")
except Exception as e:
    ok_pred = False
    res = str(e)
check("④", "run_prediction 正常返回两条预测", bool(ok_pred), res if not ok_pred else "")
if ok_pred:
    p1 = db.get_prediction_by_store_date(sid_a, pd1)
    check("④", "预测结果已落库（可按日期取回）", bool(p1))
    check("④", "落库含 dish_predictions", bool((p1 or {}).get("result", {}).get("dish_predictions")))

# ==================== ⑤ 采购单 ====================
section("⑤ 生成采购单（预测 → 食材维度）")
po = ingfc.build_purchase_order(sid_a, predict_date=pd1)
items = po.get("items") or []
names = {i["ingredient_name"] for i in items}
check("⑤", "采购单含食材明细", len(items) >= 1, items)
check("⑤", "采购单含白菜需求", "白菜" in names, names)
check("⑤", "采购单金额 > 0", float(po.get("total_cost") or 0) > 0, po.get("total_cost"))

# ==================== ⑥ 渠道覆盖/缺口拆分 ====================
section("⑥ 渠道工作台：供货范围内『已覆盖』vs『未覆盖缺口』")
covered, uncovered = {}, {}
for sid in (sid_a, sid_b):
    snorms = db.get_store_supply_scope_norms(sid)
    _po = ingfc.build_purchase_order(sid, predict_date=pd1)
    for it in _po.get("items") or []:
        nn = db.normalize_name(it.get("ingredient_name") or "")
        tgt = covered if nn in snorms else uncovered
        tgt[nn] = tgt.get(nn, 0.0) + float(it.get("quantity") or 0)
check("⑥", "白菜落入『已覆盖』", "白菜" in covered, covered)
check("⑥", "干辣椒落入『未覆盖缺口』", "干辣椒" in uncovered, uncovered)

# ==================== ⑦ POS 自动对接 + 对账回灌 ====================
section("⑦ POS 推送自动对接（自动建档 + reconciled 回灌预测历史）")
sale_d = (date.today() - timedelta(days=1)).isoformat()
mapping = {"auto_create_dish": True, "auto_regen_bom": False}  # 关 BOM 生成以免触发 LLM
stats = pos_client.normalize_and_upsert(
    sid_a,
    [{"dish_name": "醋溜白菜", "qty": 15, "sale_date": sale_d, "amount": 270},
     {"dish_name": "POS新菜-麻婆豆腐", "qty": 8, "sale_date": sale_d, "amount": 96}],
    sale_date_default=sale_d, mapping=mapping,
)
check("⑦", "POS 写入 2 条实际销量", stats.get("upserted") == 2, stats)
check("⑦", "POS 新菜自动建档 1 道", stats.get("auto_created") == 1, stats)
row = raw("SELECT reconciled FROM actual_sales WHERE store_id=? AND sale_date=? LIMIT 1", (sid_a, sale_d))
check("⑦", "POS 写入标记 reconciled=1", row and row[0][0] == 1, row)

before = raw("SELECT COUNT(*) FROM sales_history WHERE store_id=? AND sale_date=?", (sid_a, sale_d))[0][0]
db.backfill_history_from_actual(sid_a, sale_d, only_reconciled=True)
after = raw("SELECT COUNT(*) FROM sales_history WHERE store_id=? AND sale_date=?", (sid_a, sale_d))[0][0]
check("⑦", "对账数据回灌预测历史", after > before, f"{before}->{after}")
db.backfill_history_from_actual(sid_a, sale_d, only_reconciled=True)  # 幂等
after2 = raw("SELECT COUNT(*) FROM sales_history WHERE store_id=? AND sale_date=?", (sid_a, sale_d))[0][0]
check("⑦", "回灌幂等（重复回灌不新增）", after2 == after, f"{after}->{after2}")

# ==================== ⑧ 智能预警生成与推送去重 ====================
section("⑧ 智能预警：生成 → 推送 → 同日去重")
# 造一条"预测远高于历史均值"的最新预测（日期取最靠后，确保被 get_latest_prediction 选中）
today = date.today().isoformat()
import datetime as _dt  # noqa: E402
db.save_prediction({
    "store_id": sid_a, "predict_date": (date.today() + timedelta(days=30)).isoformat(),
    "run_time": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "revenue": 3600, "cost": 100, "gross_margin": 97,
    "result": {"dish_predictions": {"醋溜白菜": 200}},
})
al = alerts_mod.generate_alerts(sid_a)
check("⑧", "预警生成（预测远超历史均值 → 触发加备）", isinstance(al, list) and len(al) >= 1, al)

sched.push_store_alerts_for_stores([sid_a])
check("⑧", "推送已下发到桩通知器", len(_FakeNotifier.calls) >= 1, _FakeNotifier.calls)
pushed1 = db.alert_pushed_today(sid_a, today)
check("⑧", "推送后记录已写入去重表", pushed1)
n_calls = len(_FakeNotifier.calls)
sched.push_store_alerts_for_stores([sid_a])  # 同日第二推
check("⑧", "同日重复推送被去重（不再调用通知器）", len(_FakeNotifier.calls) == n_calls,
      _FakeNotifier.calls)

# ==================== 多角色权限边界 ====================
section("多角色：页面访问权限边界")
checks = [
    (("admin", "user_manage"), True, "管理员可访问用户管理"),
    (("admin", "contract_manage"), True, "管理员可访问合同管理"),
    (("admin", "role_manage"), True, "管理员可访问角色管理"),
    (("channel", "channel_store"), True, "渠道可访问渠道门店与供货"),
    (("channel", "channel_store_account"), True, "渠道可访问渠道门店账号"),
    (("channel", "user_manage"), False, "渠道不可访问用户管理"),
    (("channel", "contract_manage"), False, "渠道不可访问合同管理"),
    (("merchant", "predict_config"), True, "门店可访问执行预测"),
    (("merchant", "dish_manage"), True, "门店可访问菜品管理"),
    (("merchant", "user_manage"), False, "门店不可访问用户管理"),
    (("merchant", "contract_manage"), False, "门店不可访问合同管理"),
]
for (role, key), expect, desc in checks:
    got = db.can_access_page(role, key)
    check("权限", desc, got == expect, f"期望{expect} 实际{got}")

section("多角色：登录与租户隔离")
auth_admin = db.authenticate_user("it_admin", "Admin@123")
auth_chan = db.authenticate_user("it_channel", "Chan@123")
auth_mer = db.authenticate_user("it_merchant", "Mer@123")
check("登录", "admin/channel/merchant 均可登录",
      bool(auth_admin) and bool(auth_chan) and bool(auth_mer))
check("登录", "错误密码被拒绝", db.authenticate_user("it_admin", "wrong") is None)

# ==================== 汇总 ====================
section(f"集成测试结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)

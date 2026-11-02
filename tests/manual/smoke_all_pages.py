"""全页面冒烟测试（隔离库 + 有数据 + 覆盖全部页面）。

目的：用桩 streamlit 顺序执行每个页面的顶层代码，捕获结构型崩溃
（列数越界 / 未定义变量 / 函数签名不匹配 / 新增组件调用错误），
并验证新增代码（危险操作二次确认等）在渲染阶段不会崩溃。

隔离：SQLITE_PATH 指向临时库（含种子数据），绝不污染真实库。
判定：执行到底 = PASS(full)；被 st.stop/rerun/switch_page 截断 = PASS(...)，
      并在下方标注截断所在的【页面行号】，便于确认是否为预期的数据守卫。

桩定义见 tests/manual/_stub_streamlit.py（与 verify_danger_confirm 共用）。
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ---------- 1. 环境隔离（必须在 import 任何东西之前） ----------
TMP = os.path.join(tempfile.gettempdir(), "smoke_all_pages.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["SQLITE_PATH"] = TMP
os.environ["GUOBEICAI_DISABLE_SEED"] = "1"

# ---------- 2. 注入桩 streamlit（必须在 import 页面/组件之前） ----------
from tests.manual._stub_streamlit import (  # noqa: E402
    install, RerunScript, StopScript, SwitchPage,
)

STATE = install(user={"id": 1, "username": "admin", "role": "admin"})

# ---------- 3. 隔离库 + 种子数据 ----------
import guibeicai.web._db as _db  # noqa: E402

_db.init_db()


def _seed():
    con = sqlite3.connect(TMP)
    con.execute("PRAGMA foreign_keys=ON")
    c = con.cursor()
    for nm in ("冒烟A店", "冒烟B店"):
        c.execute("INSERT INTO stores (name, province, city, status, auto_predict, "
                  "auto_predict_target_offset, alert_push_enabled) VALUES (?,?,?,?,?,?,?)",
                  (nm, "广西壮族自治区", "南宁市", "active", 1, 0, 1))
    c.execute("INSERT OR IGNORE INTO roles (name, code, description, permissions, pages) "
              "VALUES ('系统管理员','admin','全部权限','{\"all\": true}','[\"*\"]')")
    c.execute("INSERT OR IGNORE INTO roles (name, code, description, permissions, pages) "
              "VALUES ('商家用户','merchant','门店用户','{}','[]')")
    c.execute("INSERT INTO users (id, username, password_hash, role, store_id) VALUES (1,'admin','x','admin',0)")
    c.execute("INSERT INTO users (id, username, password_hash, role, store_id) VALUES (2,'merchantA','x','merchant',1)")
    c.execute("INSERT INTO user_stores (user_id, store_id) VALUES (2,1)")
    c.execute("INSERT INTO ingredients (store_id, name, status) VALUES (1,'白菜','active')")
    c.execute("INSERT INTO dishes (store_id, name, status, bom_status) VALUES (1,'醋溜白菜','active','ready')")
    did = c.lastrowid
    c.execute("INSERT INTO recipes (dish_id, ingredient_name, quantity, unit) VALUES (?,?,?,?)",
              (did, "白菜", 0.2, "kg"))
    # 历史销量（让「预测配置」等页不停在“暂无数据”守卫）
    for d in range(1, 11):
        c.execute("INSERT INTO sales_history (store_id, dish_id, sale_date, qty) VALUES (1,?,?,?)",
                  (did, f"2026-08-{d:02d}", 10 + d))
    # 实际销量 / 损耗（让「损耗分析」出数）
    c.execute("INSERT INTO actual_sales (store_id, dish_id, sale_date, actual_qty, actual_waste, reconciled) "
              "VALUES (1,?,?,?,?,1)", (did, "2026-09-01", 12, 1))
    # 一次预测结果（让「预测总览/食材采购/渠道工作台」出数）
    c.execute("INSERT INTO predictions (store_id, predict_date, run_time, special_event, revenue, cost, "
              "gross_margin, result_json) VALUES (1,'2026-09-02','2026-09-01 06:00:00','',1200,600,600,?)",
              ('{"dish_predictions": {"醋溜白菜": 50}}',))
    c.execute("INSERT INTO contracts (contract_no, name, amount, party_a, party_b, sign_date, "
              "effective_date, expiry_date) VALUES ('HT-2026-001','冒烟合同',1000,'甲方','乙方','2026-01-01','2026-01-01','2027-01-01')")
    c.execute("INSERT INTO pos_sources (store_id, name, source_type, base_url, auth_type, enabled) "
              "VALUES (1,'冒烟POS','http_json','http://x','none',1)")
    con.commit()
    con.close()


_seed()

# ---------- 4. 遍历所有页面 ----------
_WEB = _PROJECT_ROOT / "guibeicai" / "web"
PAGES = [
    "app.py",
    "pages/login.py",
    "pages/1_数据上传.py",
    "pages/2_预测配置.py",
    "pages/3_预测总览.py",
    "pages/4_门店管理.py",
    "pages/5_菜品管理.py",
    "pages/7_用户管理.py",
    "pages/8_角色管理.py",
    "pages/9_预测对比.py",
    "pages/10_回测.py",
    "pages/11_操作审计.py",
    "pages/12_食材采购.py",
    "pages/14_POS对接.py",
    "pages/16_损耗分析.py",
    "pages/15_智能预警.py",
    "pages/17_食材管理.py",
    "pages/18_渠道工作台.py",
    "pages/20_数据初始化.py",
    "pages/21_渠道门店与供货.py",
    "pages/22_合同管理.py",
    "pages/23_渠道门店账号.py",
]

results = []
for rel in PAGES:
    path = _WEB / rel
    if not path.exists():
        results.append((rel, "SKIP", "文件不存在"))
        continue
    # 每页重置登录态（保留登录），避免页面间 session 相互污染
    STATE.reset({"user": {"id": 1, "username": "admin", "role": "admin"},
                 "login_time": time.time()})
    mod_name = "smokeall_" + rel.replace("\\", "_").replace("/", "_").replace(".", "_")
    try:
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        results.append((rel, "PASS(full)", ""))
    except StopScript:
        results.append((rel, "PASS(stop)", traceback.format_exc()))
    except RerunScript:
        results.append((rel, "PASS(rerun)", traceback.format_exc()))
    except SwitchPage:
        results.append((rel, "PASS(switch)", traceback.format_exc()))
    except Exception:
        results.append((rel, "FAIL", traceback.format_exc()))

print("=" * 78)
fails = []
for rel, status, tb in results:
    mark = "OK  " if status.startswith("PASS") else ("SKIP" if status == "SKIP" else "FAIL")
    print(f"[{mark}] {rel:28} {status}")
    if status not in ("PASS(full)", "SKIP") and tb:
        lines = [ln.strip() for ln in tb.strip().splitlines()
                 if ln.strip().startswith("File ") and ("web\\pages\\" in ln or "web/pages/" in ln)]
        if lines:
            print(f"         ↳ 截断于 {lines[-1]}")
    if status == "FAIL":
        fails.append(rel)
        print(tb)
print("=" * 78)
print(f"TOTAL={len(results)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)

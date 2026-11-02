# -*- coding: utf-8 -*-
"""Phase 3 回测体系自测（离线模拟数据；临时库隔离真实数据）。"""
import sys, os, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import py_compile, glob
passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"[PASS] {name}")
    else:
        failed += 1; print(f"[FAIL] {name}  {detail}")

# ============ 0. 全量语法编译 ============
py_files = glob.glob(os.path.join(ROOT, "guibeicai", "**", "*.py"), recursive=True)
errs = []
for f in py_files:
    try:
        py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as e:
        errs.append((f, str(e)[:160]))
check("全量语法编译通过(含新 10_回测.py / _backtest.py)", len(errs) == 0, f"errs={errs[:3]}")

# ============ 准备临时库 ============
import pandas as pd
from pathlib import Path
tmpdir = tempfile.mkdtemp(prefix="phase3_")
db_path = Path(tmpdir) / "test.db"
from guibeicai.web import _db
_db.DB_PATH = db_path
_db.init_db()

# 建门店 + 菜品 + 45天销量
conn = _db.get_conn(); c = conn.cursor()
c.execute("INSERT INTO stores (name, province, city, status) VALUES ('测试店','广西','南宁市','active')")
sid = c.lastrowid
for name in ["鱼香肉丝", "宫保鸡丁"]:
    c.execute("INSERT INTO dishes (store_id, name, status) VALUES (?, ?, 'active')", (sid, name))
conn.commit(); conn.close()

dishes = {d["name"]: d["id"] for d in _db.get_dishes_by_store(sid)}
import datetime as _dt
for name, base in [("鱼香肉丝", 20.0), ("宫保鸡丁", 12.0)]:
    did = dishes[name]
    for i in range(45):
        d = (_dt.date(2025, 1, 1) + _dt.timedelta(days=i))
        wk = d.weekday()
        qty = base + (6.0 if wk >= 4 else 0.0) + (i % 3)  # 周末抬升 + 轻微波动
        _db.upsert_sales_history(sid, did, d.strftime("%Y-%m-%d"), round(qty, 1), 30.0)

# ============ monkeypatch Prophet 为可控 FakeModel（避免真实 Stan，且模拟系统性高估 1.3x） ============
from guibeicai.web import _forecast
class FakeModel:
    __version__ = "fake-1.0"
    PRED_SCALE = 1.3
    def __init__(self, *a, **k): self._hist = None
    def add_country_holidays(self, *a, **k): pass
    def add_regressor(self, *a, **k): pass
    def fit(self, hist):
        self._hist = hist.reset_index(drop=True)
        self._train_mean = float(hist["y"].mean())
    def make_future_dataframe(self, periods):
        last = self._hist["ds"].max()
        extra = pd.date_range(last + pd.Timedelta(days=1), periods=periods, freq="D")
        return pd.concat([self._hist[["ds"]].copy(), pd.DataFrame({"ds": extra})], ignore_index=True)
    def predict(self, future):
        f = future.copy()
        f["yhat"] = self._train_mean * FakeModel.PRED_SCALE
        return f
_orig = _forecast.Prophet
_forecast.Prophet = FakeModel

# ============ #17 回测准确度评估 ============
from guibeicai.web import _backtest
res = _backtest.run_backtest(store_id=sid, holdout_days=7, min_history_days=30, use_weather=False)
ov = res["overall"]
check("#17 回测返回整体指标", ov.get("mae") is not None and ov.get("mape") is not None, f"ov={ov}")
check("#17 覆盖两个达标菜品", ov.get("n_dishes") == 2, f"n_dishes={ov.get('n_dishes')}")
check("#17 系统性高估被捕获(bias≈0.3)", abs((ov.get("bias") or 0) - 0.3) < 0.1, f"bias={ov.get('bias')}")
check("#17 逐菜品指标完整", len(res["per_dish"]) == 2 and all("mape" in d for d in res["per_dish"]))

# _metrics 直接计算正确性
m = _backtest._metrics([(10, 10), (20, 10)])
check("#17 _metrics MAE 正确", abs(m["mae"] - 5.0) < 1e-6, f"mae={m['mae']}")
check("#17 _metrics MAPE 正确", abs(m["mape"] - 50.0) < 1e-6, f"mape={m['mape']}")
check("#17 _metrics bias 正确", abs(m["bias"] - 0.5) < 1e-6, f"bias={m['bias']}")

# ============ #14 误差反馈闭环 ============
corr = _backtest.apply_and_save_bias_corrections(sid, res)
check("#14 偏差校正因子已生成", len(corr) == 2, f"corr={corr}")
# 因子应≈1/1.3≈0.769（限幅内）
ok_factor = all(0.6 < v < 0.9 for v in corr.values())
check("#14 校正因子≈1/1.3(纠偏高估)", ok_factor, f"corr={corr}")

# 持久化验证
persisted = _db.get_bias_corrections(sid)
check("#14 校正因子已持久化到 DB", set(persisted.keys()) == set(dishes.keys()), f"persisted={persisted}")

# 闭环效果：应用校正后误差应低于未校正
# 取首个菜品：未校正 pred = train_mean*1.3；校正后 = pred * factor ≈ train_mean
d0 = res["per_dish"][0]
raw_pred_mean = d0["pred_mean"]
corrected_pred_mean = raw_pred_mean * corr[d0["dish_name"]]
check("#14 闭环降低预测偏差(校正后≈真值)", abs(corrected_pred_mean - d0["actual_mean"]) < abs(raw_pred_mean - d0["actual_mean"]),
      f"raw_err={abs(raw_pred_mean-d0['actual_mean']):.2f} corr_err={abs(corrected_pred_mean-d0['actual_mean']):.2f}")

# 回测记录已写入
runs = _db.load_backtest_runs(limit=5)
check("#14 回测运行记录已写入 DB", len(runs) >= 1, f"runs={len(runs)}")

# ============ 新增表存在性 ============
conn = _db.get_conn(); c = conn.cursor()
tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
conn.close()
check("#14 新表 bias_corrections/backtest_runs 已建", {"bias_corrections", "backtest_runs"}.issubset(tabs), f"tables={tabs}")

# 还原 Prophet
_forecast.Prophet = _orig

print(f"\n==== Phase 3 自测结果: 通过 {passed} / 失败 {failed} ====")
sys.exit(1 if failed else 0)

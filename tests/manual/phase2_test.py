# -*- coding: utf-8 -*-
"""Phase 2 性能优化自测（全离线，模拟数据；临时库隔离真实数据）。"""
import sys, os, time, types, threading, importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = 0
failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")

# ============ 0. 全量语法编译 ============
import py_compile, glob
py_files = glob.glob(os.path.join(ROOT, "guibeicai", "**", "*.py"), recursive=True)
compile_err = []
for f in py_files:
    try:
        py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as e:
        compile_err.append((f, str(e)[:160]))
check("全量语法编译通过", len(compile_err) == 0, f"错误文件数={len(compile_err)} {compile_err[:3]}")

# ============ #12 LLM 并发批处理 + 缓存修复 ============
from guibeicai.web import _llm

# --- 并发：monkeypatch 单菜请求为快函数，记录线程，验证并行 ---
_calls = []
_threads = set()
def fake_call(dish, weather_text, holiday_dates, manual_event, city):
    _calls.append(dish)
    _threads.add(threading.current_thread().ident)
    time.sleep(0.05)
    return {"multiplier": 1.0, "dish": dish}
_orig_call = _llm._call_llm_for_dish
_llm._call_llm_for_dish = fake_call
t0 = time.time()
res = _llm.analyze_by_dishes(["鱼香肉丝", "宫保鸡丁", "水煮鱼", "麻婆豆腐"],
                             weather_text="晴", holiday_dates=[], manual_event="", city="南宁市")
elapsed = time.time() - t0
_llm._call_llm_for_dish = _orig_call
check("#12 并发返回全部菜品", set(res["by_dish"].keys()) == {"鱼香肉丝", "宫保鸡丁", "水煮鱼", "麻婆豆腐"})
check("#12 并发真正并行(>1线程)", len(_threads) > 1, f"distinct_threads={len(_threads)}")
check("#12 并发节省时间(并行<串行)", elapsed < 0.15, f"elapsed={elapsed:.3f}s (串行应~0.2s)")

# --- 缓存：相同输入二次请求命中缓存，跳过 API ---
_llm.API_KEY = "test-key-123"
key = _llm._llm_cache_key("宫保鸡丁", "晴", [], "", "南宁市", _llm.MODEL)
_llm._llm_cache_put(key, '{"multiplier":1.3,"risk_level":"高","conclusion":"c","purchase_advice":"p","risk_factors":["高温"],"note":"缓存命中测试"}')

class BoomClient:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, *a, **k): raise RuntimeError("不应在缓存命中时调用 API")
_orig_client = _llm.httpx.Client
_llm.httpx.Client = BoomClient
try:
    out = _llm._call_llm_for_dish("宫保鸡丁", "晴", [], "", "南宁市")
    check("#12 缓存命中跳过 API(取数正确)", out.get("multiplier") == 1.3 and out.get("risk_level") == "高",
          f"out={out}")
finally:
    _llm.httpx.Client = _orig_client

# --- 缓存写+读 round-trip + 过期 ---
k2 = _llm._llm_cache_key("麻婆豆腐", "雨", [], "", "南宁市", _llm.MODEL)
_llm._llm_cache_put(k2, '{"multiplier":0.9}')
check("#12 缓存读取 round-trip", _llm._llm_cache_get(k2) == '{"multiplier":0.9}')
_old_ttl = _llm.LLM_CACHE_TTL_SECONDS
_llm.LLM_CACHE_TTL_SECONDS = 0
check("#12 缓存过期返回 None", _llm._llm_cache_get(k2) is None)
_llm.LLM_CACHE_TTL_SECONDS = _old_ttl

# ============ #16 Prophet 模型缓存（避免同数据重复 Stan 训练） ============
from guibeicai.web import _forecast
import pandas as pd

fits = {"n": 0}
class FakeModel:
    __version__ = "fake-1.0"
    def __init__(self, *a, **k): self._hist = None
    def add_country_holidays(self, *a, **k): pass
    def add_regressor(self, *a, **k): pass
    def fit(self, hist):
        fits["n"] += 1
        self._hist = hist.reset_index(drop=True)
    def make_future_dataframe(self, periods):
        last = self._hist["ds"].max()
        extra = pd.date_range(last + pd.Timedelta(days=1), periods=periods, freq="D")
        return pd.concat([self._hist[["ds"]].copy(), pd.DataFrame({"ds": extra})], ignore_index=True)
    def predict(self, future):
        f = future.copy()
        f["yhat"] = 1.0
        return f

_orig_prophet = _forecast.Prophet
_forecast.Prophet = FakeModel
try:
    dish_a = pd.DataFrame({"sale_date": pd.date_range("2025-01-01", periods=20), "qty": list(range(20))})
    w = pd.DataFrame({"ds": pd.date_range("2025-01-01", periods=20), "temp_mean": 20.0, "rain_sum": 0.0})
    o1 = _forecast.prophet_predict_for_dish(dish_a, w, 5)
    o2 = _forecast.prophet_predict_for_dish(dish_a, w, 5)
    check("#16 相同数据仅训练一次(fit=1)", fits["n"] == 1, f"fits={fits['n']}")
    check("#16 缓存命中仍返回预测", len(o2) == 5, f"len={len(o2)}")
    # 不同数据 → 新训练
    dish_b = pd.DataFrame({"sale_date": pd.date_range("2025-02-01", periods=20), "qty": list(range(20))})
    _forecast.prophet_predict_for_dish(dish_b, w, 5)
    check("#16 不同数据触发新训练(fit=2)", fits["n"] == 2, f"fits={fits['n']}")
finally:
    _forecast.Prophet = _orig_prophet
    _forecast.prophet_model_cache_clear()

# ============ #13 历史懒加载 + 按菜品分组索引 ============
from guibeicai.web import _cache
import streamlit as st
_cache.st = types.SimpleNamespace(session_state={})  # 模拟 Streamlit session

_sales_calls = {"n": 0}
def fake_sales(store_id):
    _sales_calls["n"] += 1
    return pd.DataFrame({
        "dish_name": ["鱼", "鱼", "鸡", "鸡"],
        "sale_date": pd.date_range("2025-01-01", periods=4),
        "qty": [10, 12, 5, 7],
        "unit_price": [30, 30, 20, 20],
    })
_cache.get_sales_history_df = fake_sales
_cache.get_all_stores = lambda active_only=False: [{"id": 1, "name": "s1", "status": "active"}]
_cache.get_dishes_by_store = lambda sid, active_only=True: []
_cache.get_active_recipes_by_store = lambda sid: {}
_cache.get_date_factors_for_store = lambda sid: {}

_cache.init_cache()
check("#13 启动不全量扫描销量(懒加载)", _sales_calls["n"] == 0, f"启动期DB调用={_sales_calls['n']}")

grp = _cache.get_cached_sales_by_dish(1)
check("#13 分组索引首次触发一次加载", _sales_calls["n"] == 1, f"DB调用={_sales_calls['n']}")
check("#13 分组键正确", set(grp.keys()) == {"鱼", "鸡"}, f"keys={set(grp.keys())}")
check("#13 分组计数正确", len(grp["鱼"]) == 2 and len(grp["鸡"]) == 2)

_cache.get_cached_sales_by_dish(1)
check("#13 二次读取走缓存(无新DB调用)", _sales_calls["n"] == 1, f"DB调用={_sales_calls['n']}")

# 模拟预测循环取数（O(1)）
fish = grp.get("鱼", pd.DataFrame())
check("#13 分组取数可用", len(fish) == 2 and float(fish["qty"].iloc[-1]) == 12.0)

_cache.refresh_sales_cache(1)
_cache.get_cached_sales_by_dish(1)
check("#13 刷新后失效并重新加载", _sales_calls["n"] == 2, f"DB调用={_sales_calls['n']}")

# ============ 汇总 ============
print(f"\n==== Phase 2 自测结果: 通过 {passed} / 失败 {failed} ====")
sys.exit(1 if failed else 0)

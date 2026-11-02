# -*- coding: utf-8 -*-
"""
Phase #18 自测：未来特殊事件建模进 Prophet
- _expand_event_dates：单日/多日/跨年区间展开
- build_special_event_series：城市匹配 + 手动事件，返回 0/1 序列
- prophet_predict_for_dish(..., special_event_df)：真实 Prophet 训练不崩、无 NaN、
  未来事件日预测高于同星期非事件日（回归量生效），且向后兼容（无 special_event_df 仍可用）
全离线、临时库隔离。
"""
import sys, os, io
import pandas as pd
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- 本机 Windows 中文 locale（GBK）下 cmdstanpy 的 subprocess.Popen(universal_newlines=True)
#      会把 where.exe 的 GBK 输出按 UTF-8 解码而崩；给 Popen 注入 errors='replace' 使其可本地训练 ----
import subprocess as _sp
_orig_Popen = _sp.Popen
class _PatchedPopen(_orig_Popen):
    def __init__(self, *a, **k):
        if k.get("universal_newlines") or k.get("text"):
            k.setdefault("errors", "replace")
        super().__init__(*a, **k)
_sp.Popen = _PatchedPopen
# -----------------------------------------------------------------------------------

from guibeicai.web import _special_events, _forecast
from guibeicai.web._special_events import build_special_event_series, _expand_event_dates

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail}")

# ===== 1) _expand_event_dates =====
ev_single = {"date": "09-04", "label": "店庆", "city": "all"}
ev_list   = {"dates": ["10-01", "10-02", "10-03"], "label": "国庆活动", "city": "南宁"}
ev_range  = {"start": "12-20", "end": "01-10", "label": "跨年庙会", "city": "all"}
s1 = _expand_event_dates(ev_single)
check("_expand 单日", (9, 4) in s1 and len(s1) == 1, f"set={s1}")
s2 = _expand_event_dates(ev_list)
check("_expand 多日", {(10, 1), (10, 2), (10, 3)}.issubset(s2), f"count={len(s2)}")
s3 = _expand_event_dates(ev_range)
check("_expand 跨年区间", (12, 20) in s3 and (12, 25) in s3 and (1, 1) in s3 and (1, 10) in s3, f"跨年命中")

# ===== 2) build_special_event_series（注入假配置，隔离真实 json）=====
fake_events = [ev_single, ev_list, ev_range]
_orig_load = _special_events._load_config
_special_events._load_config = lambda: fake_events
try:
    idx = pd.date_range("2026-09-01", periods=45, freq="D")  # 09-01..10-15
    se = build_special_event_series(city="南宁", ds_index=idx, manual_event_dates=[pd.Timestamp("2026-09-15")])
    check("序列列名正确", list(se.columns) == ["ds", "special_event"], str(list(se.columns)))
    check("配置事件命中 09-04", float(se.loc[se["ds"] == pd.Timestamp("2026-09-04"), "special_event"].iloc[0]) == 1.0)
    check("配置事件命中 10-02", float(se.loc[se["ds"] == pd.Timestamp("2026-10-02"), "special_event"].iloc[0]) == 1.0)
    check("非事件日 09-05 = 0", float(se.loc[se["ds"] == pd.Timestamp("2026-09-05"), "special_event"].iloc[0]) == 0.0)
    check("手动事件 09-15 = 1", float(se.loc[se["ds"] == pd.Timestamp("2026-09-15"), "special_event"].iloc[0]) == 1.0)
    # all 城市匹配：跨年事件 12-25 若在索引内应命中（本索引不含，跳过）
finally:
    _special_events._load_config = _orig_load

# ===== 3) prophet_predict_for_dish + 特殊事件（真实 Prophet）=====
def make_hist(n=40, event_days=("09-04",), base=20.0, bump=25.0):
    dates = pd.date_range("2026-08-01", periods=n, freq="D")
    y = np.full(n, base, dtype=float)
    for i, d in enumerate(dates):
        if f"{d.month:02d}-{d.day:02d}" in event_days:
            y[i] += bump
    return pd.DataFrame({"sale_date": dates, "qty": y})

# 历史里 09-04/05/06 为事件日（强信号 +25），让模型学到 special_event 正向影响
hist = make_hist(n=40, event_days=("09-04", "09-05", "09-06"), base=20.0, bump=25.0)
# 未来窗口 09-10..09-23（periods=14）；09-14(周一) 为事件日，09-21(周一) 为非事件日，同星期隔离
future_dates = pd.date_range("2026-09-10", periods=14, freq="D")

# 特殊事件配置：过去教模型 + 未来 09-14 触发
fake_events2 = [
    {"date": "09-04", "label": "x", "city": "all"},
    {"date": "09-05", "label": "x", "city": "all"},
    {"date": "09-06", "label": "x", "city": "all"},
    {"date": "09-14", "label": "x", "city": "all"},
]
_special_events._load_config = lambda: fake_events2
try:
    ds_index = list(hist["sale_date"]) + list(future_dates)
    se_df = build_special_event_series(city="all", ds_index=ds_index, manual_event_dates=None)
    # 天气全量覆盖历史+未来，避免 NaN
    weather_df = pd.DataFrame({
        "ds": pd.date_range("2026-08-01", periods=len(hist) + 14, freq="D"),
        "temp_mean": 25.0, "rain_sum": 0.0,
    })
    periods = 14
    fc = _forecast.prophet_predict_for_dish(hist, weather_df, periods, special_event_df=se_df).set_index("ds")
    check("Prophet+事件 返回14行", len(fc) == 14, f"rows={len(fc)}")
    check("yhat 无 NaN", not fc["yhat"].isna().any())
    d_event = pd.Timestamp("2026-09-14")
    d_none = pd.Timestamp("2026-09-21")
    check("事件日/非事件日都在窗口", d_event in fc.index and d_none in fc.index)
    diff = fc.loc[d_event, "yhat"] - fc.loc[d_none, "yhat"]
    check("事件日预测高于同星期非事件日(回归量生效)", diff > 5, f"Δ={diff:.2f}")
finally:
    _special_events._load_config = _orig_load

# ===== 4) 回归量确实接通：有事件 vs 全零事件，事件日预测应不同 =====
_special_events._load_config = lambda: fake_events2
try:
    ds_index = list(hist["sale_date"]) + list(future_dates)
    se_with = build_special_event_series(city="all", ds_index=ds_index, manual_event_dates=None)
    se_zero = se_with.copy(); se_zero["special_event"] = 0.0
    weather_df = pd.DataFrame({
        "ds": pd.date_range("2026-08-01", periods=len(hist) + 14, freq="D"),
        "temp_mean": 25.0, "rain_sum": 0.0,
    })
    fc_with = _forecast.prophet_predict_for_dish(hist, weather_df, 14, special_event_df=se_with).set_index("ds")
    fc_zero = _forecast.prophet_predict_for_dish(hist, weather_df, 14, special_event_df=se_zero).set_index("ds")
    v_with = fc_with.loc[d_event, "yhat"]; v_zero = fc_zero.loc[d_event, "yhat"]
    check("事件回归量改变预测(有vs无)", abs(v_with - v_zero) > 1e-6, f"with={v_with:.2f} zero={v_zero:.2f}")
finally:
    _special_events._load_config = _orig_load

# ===== 5) 向后兼容：不传 special_event_df 仍可用 =====
weather_df = pd.DataFrame({
    "ds": pd.date_range("2026-08-01", periods=len(hist) + 14, freq="D"),
    "temp_mean": 25.0, "rain_sum": 0.0,
})
fc_legacy = _forecast.prophet_predict_for_dish(hist, weather_df, 14).set_index("ds")
check("向后兼容(无special_event_df)返回14行", len(fc_legacy) == 14, f"rows={len(fc_legacy)}")

# ===== 汇总 =====
fails = [n for n, ok, _ in results if not ok]
print("\n==== 汇总 ====")
print(f"总计 {len(results)} 项，通过 {len(results)-len(fails)} 项，失败 {len(fails)} 项")
if fails:
    print("失败项:", fails)
    sys.exit(1)
print("ALL PASS")

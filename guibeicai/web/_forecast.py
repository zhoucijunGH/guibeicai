"""Prophet 预测引擎 + 自动天气/节假日获取"""
import hashlib
import logging
import os
import pickle
import pandas as pd
from datetime import timedelta
from prophet import Prophet
import requests
from guibeicai.web._geo import city_to_coords

logger = logging.getLogger(__name__)

# 修复 cmdstanpy 在 Windows + Python 3.14(UTF-8 模式) 下调用 where.exe 读取 GBK 输出崩溃：
# where.exe 输出为系统编码(GBK)，utf-8 模式下 subprocess 按 utf-8 解码抛 UnicodeDecodeError，
# 被 cmdstanpy 吞掉导致 stan_backend 为 None、Prophet 不可用。此处为文本模式 Popen 注入 errors='replace'。
import subprocess as _sp

class _SafePopen(_sp.Popen):
    def __init__(self, *args, **kwargs):
        if kwargs.get("text") or kwargs.get("universal_newlines"):
            kwargs.setdefault("errors", "replace")
        super().__init__(*args, **kwargs)

_sp.Popen = _SafePopen

# 兼容 prophet 版本号位置差异（1.4+ 放在模块级 prophet.__version__，而非 Prophet.__version__）
try:
    from prophet import __version__ as _PROPHET_VERSION
except Exception:
    try:
        import importlib.metadata as _md
        _PROPHET_VERSION = _md.version("prophet")
    except Exception:
        _PROPHET_VERSION = "unknown"


# ==================== 模型缓存（避免同数据重复 Stan 训练） ====================
# 缓存键由"训练数据全量 + 回归量集合 + Prophet 版本"决定，数据变化时签名自动失效 → 重新训练。
_PROPHET_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_prophet_cache")
os.makedirs(_PROPHET_CACHE_DIR, exist_ok=True)
_PROPHET_MODEL_CACHE = {}  # 进程内 LRU 兜底


def _prophet_sig(hist: pd.DataFrame, w, se) -> str:
    frames = [hist[["ds", "y", "temp_mean", "rain_sum", "special_event"]].astype(str)]
    if w is not None and len(w):
        frames.append(w[["ds", "temp_mean", "rain_sum"]].astype(str).add_prefix("w_"))
    if se is not None and len(se):
        frames.append(se[["ds", "special_event"]].astype(str).add_prefix("se_"))
    blob = "\n###\n".join(f.to_csv(index=False) for f in frames) + f"\nVER:{_PROPHET_VERSION}"
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:16]


def _prophet_load(sig: str):
    if sig in _PROPHET_MODEL_CACHE:
        return _PROPHET_MODEL_CACHE[sig]
    fp = os.path.join(_PROPHET_CACHE_DIR, f"model_{sig}.pkl")
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        logger.warning("[Prophet] 模型反序列化失败，将重训: %s", e)
        return None


def _prophet_save(sig: str, model):
    _PROPHET_MODEL_CACHE[sig] = model
    try:
        fp = os.path.join(_PROPHET_CACHE_DIR, f"model_{sig}.pkl")
        with open(fp, "wb") as f:
            pickle.dump(model, f)
    except Exception as e:
        logger.warning("[Prophet] 模型持久化失败（仅影响跨进程复用）: %s", e)


def prophet_model_cache_clear():
    """清空进程内与磁盘模型缓存（数据大批量更新后可手动调用）。"""
    _PROPHET_MODEL_CACHE.clear()
    try:
        for fn in os.listdir(_PROPHET_CACHE_DIR):
            if fn.endswith(".pkl"):
                os.remove(os.path.join(_PROPHET_CACHE_DIR, fn))
    except Exception:
        pass


def _fetch_archive(lat, lon, start, end):
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "timezone": "Asia/Shanghai",
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
    }
    r = requests.get(url, params=params, timeout=15)
    j = r.json()
    daily = j.get("daily", {})
    return pd.DataFrame({
        "ds": pd.to_datetime(daily["time"]),
        "temp_mean": (pd.Series(daily["temperature_2m_max"]) + pd.Series(daily["temperature_2m_min"])) / 2,
        "rain_sum": pd.Series(daily["precipitation_sum"]).fillna(0),
    })


def _fetch_forecast(lat, lon, start, end):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "timezone": "Asia/Shanghai",
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
    }
    r = requests.get(url, params=params, timeout=15)
    j = r.json()
    daily = j.get("daily", {})
    return pd.DataFrame({
        "ds": pd.to_datetime(daily["time"]),
        "temp_mean": (pd.Series(daily["temperature_2m_max"]) + pd.Series(daily["temperature_2m_min"])) / 2,
        "rain_sum": pd.Series(daily["precipitation_sum"]).fillna(0),
    })


def fetch_weather(lat, lon, start, end):
    today = pd.Timestamp.today().normalize()
    parts = []
    if start < today:
        hist_end = min(end, today - timedelta(days=1))
        parts.append(_fetch_archive(lat, lon, start, hist_end))
    if end >= today:
        fc_start = max(start, today)
        parts.append(_fetch_forecast(lat, lon, fc_start, end))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["ds", "temp_mean", "rain_sum"])


def get_forecast_context(lat, lon, predict_dates: list) -> str:
    if not predict_dates:
        return "无天气数据"
    start = min(pd.Timestamp(d) for d in predict_dates)
    end = max(pd.Timestamp(d) for d in predict_dates)
    df = fetch_weather(lat, lon, start, end)
    df = df[df["ds"].isin([pd.Timestamp(d) for d in predict_dates])]
    if len(df) == 0:
        return "无天气数据"
    lines = []
    for _, row in df.iterrows():
        lines.append(f"{row['ds'].strftime('%m-%d')} 均温{row['temp_mean']:.1f}℃ 降水{row['rain_sum']:.0f}mm")
    return "；".join(lines)


def _normalize_dish_columns(dish_data: pd.DataFrame) -> pd.DataFrame:
    """将菜品历史数据归一化为 Prophet 需要的 ds/y 列，兼容多种列名（sale_date/qty 或 日期/销量）。"""
    df = dish_data.copy()
    col_map = {}
    for cand in ("sale_date", "日期", "date", "ds"):
        if cand in df.columns:
            col_map[cand] = "ds"
            break
    for cand in ("qty", "销量", "y", "quantity"):
        if cand in df.columns:
            col_map[cand] = "y"
            break
    if len(col_map) < 2:
        raise KeyError("菜品历史数据缺少日期/销量列，无法预测（需要 sale_date/qty 或 日期/销量）")
    df = df.rename(columns=col_map)
    df = df[["ds", "y"]].copy()
    df["ds"] = pd.to_datetime(df["ds"], errors="coerce")
    df["y"] = pd.to_numeric(df["y"], errors="coerce")
    df = df.dropna(subset=["ds", "y"])
    return df


def prophet_predict_for_dish(dish_data: pd.DataFrame, weather_df: pd.DataFrame, periods: int, special_event_df: pd.DataFrame = None):
    hist = _normalize_dish_columns(dish_data)
    if len(hist) < 2:
        # 历史数据过少，无法训练 Prophet，返回空结果由调用方走均值兜底
        return pd.DataFrame(columns=["ds", "yhat"])

    # ---- 天气回归量 ----
    w = None
    if weather_df is not None and len(weather_df) > 0:
        w = weather_df[["ds", "temp_mean", "rain_sum"]].copy()
        w["ds"] = pd.to_datetime(w["ds"], errors="coerce")
        hist = hist.merge(w, on="ds", how="left")
    else:
        hist["temp_mean"] = 0.0
        hist["rain_sum"] = 0.0

    # ---- 特殊事件外生变量（寒暑假/民俗/店铺活动 + 手动事件）----
    se = None
    if special_event_df is not None and len(special_event_df) > 0:
        se = special_event_df[["ds", "special_event"]].copy()
        se["ds"] = pd.to_datetime(se["ds"], errors="coerce")
        hist = hist.merge(se, on="ds", how="left")
    else:
        hist["special_event"] = 0.0

    # 缺失兜底：避免 Prophet 外生变量出现 NaN 导致训练失败
    tmean = hist["temp_mean"]
    hist["temp_mean"] = tmean.interpolate().fillna(tmean.mean() if tmean.notna().any() else 0.0)
    hist["rain_sum"] = hist["rain_sum"].fillna(0.0)
    hist["special_event"] = hist["special_event"].fillna(0.0)

    # ---- 模型缓存：相同训练数据直接复用已拟合模型，跳过昂贵的 Stan 训练 ----
    sig = _prophet_sig(hist, w, se)
    model = _prophet_load(sig)
    if model is None:
        # 历史不足 730 天时关闭 yearly seasonality，避免 Prophet 警告且模型更稳定
        days = (hist["ds"].max() - hist["ds"].min()).days + 1
        use_yearly = days >= 730
        if not use_yearly:
            logger.info("[Prophet] 历史数据 %d 天 < 730 天，关闭 yearly_seasonality", days)
        model = Prophet(
            weekly_seasonality=True,
            yearly_seasonality=use_yearly,
            daily_seasonality=False,
            holidays_prior_scale=10,
        )
        model.add_country_holidays(country_name="CN")
        model.add_regressor("temp_mean", mode="additive")
        model.add_regressor("rain_sum", mode="additive")
        model.add_regressor("special_event", mode="additive")
        model.fit(hist)
        _prophet_save(sig, model)
    else:
        logger.info("[Prophet] 命中模型缓存 sig=%s，跳过重新训练", sig)

    future = model.make_future_dataframe(periods=periods)
    future["ds"] = pd.to_datetime(future["ds"], errors="coerce")
    if w is not None:
        future = future.merge(w, on="ds", how="left")
    else:
        future["temp_mean"] = 0.0
        future["rain_sum"] = 0.0
    if se is not None:
        future = future.merge(se, on="ds", how="left")
    else:
        future["special_event"] = 0.0
    ftmean = future["temp_mean"]
    future["temp_mean"] = ftmean.interpolate().fillna(ftmean.mean() if ftmean.notna().any() else 0.0)
    future["rain_sum"] = future["rain_sum"].fillna(0.0)
    future["special_event"] = future["special_event"].fillna(0.0)

    fc = model.predict(future)
    future_only = fc[fc["ds"] > hist["ds"].max()][["ds", "yhat"]].copy()
    return future_only
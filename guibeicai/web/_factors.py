"""桂备菜 - 影响因素查询（天气/节假日/特殊事件），带缓存去重"""

import streamlit as st
import pandas as pd
from datetime import date, datetime, timedelta
from chinese_calendar import is_holiday

from guibeicai.web._cache import get_cached_factors, upsert_cached_factor
from guibeicai.web._db import upsert_date_factor
from guibeicai.web._forecast import fetch_weather, get_forecast_context, city_to_coords


def ensure_history_factors(store_id: int, store_city: str, dates: list):
    """
    确保历史日期的影响因素已查询并缓存。
    只查没有的日期，已有则跳过。
    """
    cached = get_cached_factors(store_id)
    lat, lon = city_to_coords(store_city)

    for d in dates:
        d_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        if d_str in cached and cached[d_str].get("weather_text"):
            continue  # 已有，跳过

        # 查询天气
        try:
            weather_df = fetch_weather(lat, lon, pd.Timestamp(d_str), pd.Timestamp(d_str))
            if len(weather_df) > 0:
                w_row = weather_df.iloc[0]
                # fetch_weather 返回 temp_mean/rain_sum（历史归档接口），
                # 字段名与旧代码 weather/temp 不一致，这里对齐，避免天气文本恒空、温度恒 0
                temp_mean = w_row.get("temp_mean", w_row.get("temperature", 0)) or 0
                rain_sum = w_row.get("rain_sum", w_row.get("precipitation", 0)) or 0
                weather_text = f"均温{temp_mean:.1f}℃ 降水{rain_sum:.0f}mm"
                temperature = float(temp_mean)
            else:
                weather_text = ""
                temperature = 0
        except Exception:
            weather_text = ""
            temperature = 0

        # 节假日
        try:
            hol = 1 if is_holiday(d) else 0
        except Exception:
            hol = 0

        factor_dict = {
            "weather_text": weather_text,
            "temperature": temperature,
            "is_holiday": hol,
            "special_event": "",
        }

        # 写入 DB + 缓存
        upsert_date_factor(store_id, d_str, weather_text, temperature, hol, "")
        upsert_cached_factor(store_id, d_str, factor_dict)


def get_future_factors(store_id: int, store_city: str, dates: list) -> str:
    """
    获取未来日期的影响因素文本（实时查询，写入缓存）。
    返回 weather_text 供 LLM 使用。
    """
    lat, lon = city_to_coords(store_city)
    weather_text = get_forecast_context(lat, lon, dates)

    for d in dates:
        d_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        try:
            hol = 1 if is_holiday(d) else 0
        except Exception:
            hol = 0

        factor_dict = {
            "weather_text": weather_text,
            "temperature": 0,
            "is_holiday": hol,
            "special_event": "",
        }

        upsert_date_factor(store_id, d_str, weather_text, 0, hol, "")
        upsert_cached_factor(store_id, d_str, factor_dict)

    return weather_text
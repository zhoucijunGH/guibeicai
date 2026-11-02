"""桂备菜 - 全局缓存管理"""

import streamlit as st
import pandas as pd

from guibeicai.web._db import (
    get_all_stores, get_dishes_by_store, get_active_recipes_by_store,
    get_sales_history_df, get_date_factors_for_store
)


def init_cache():
    """程序启动时调用，加载所有基础数据到缓存"""
    if "_cache_initialized" in st.session_state:
        return

    # 门店缓存
    stores = get_all_stores(active_only=False)
    st.session_state["_cache_stores"] = stores

    # 门店名→ID映射
    st.session_state["_cache_store_name_to_id"] = {s["name"]: s["id"] for s in stores}

    # 菜品缓存 {store_id: [dishes]}
    dishes_cache = {}
    for s in stores:
        dishes_cache[s["id"]] = get_dishes_by_store(s["id"], active_only=False)
    st.session_state["_cache_dishes"] = dishes_cache

    # 配方缓存 {store_id: {dish_name: [ingredients]}}
    recipes_cache = {}
    for s in stores:
        if s["status"] == "active":
            recipes_cache[s["id"]] = get_active_recipes_by_store(s["id"])
    st.session_state["_cache_recipes"] = recipes_cache

    # 销量/影响因素改按需按店懒加载（避免启动时全量扫描所有门店历史），见下方访问器
    st.session_state.setdefault("_cache_sales", {})
    st.session_state.setdefault("_cache_factors", {})
    st.session_state.setdefault("_cache_sales_by_dish", {})

    st.session_state["_cache_initialized"] = True


def get_cached_sales(store_id: int) -> pd.DataFrame:
    """获取缓存中的销量数据（按需按店加载并缓存，避免启动全量扫描）。"""
    init_cache()
    cache = st.session_state.setdefault("_cache_sales", {})
    if store_id not in cache:
        df = get_sales_history_df(store_id)
        cache[store_id] = df
        st.session_state.setdefault("_cache_sales_by_dish", {}).pop(store_id, None)
    return cache.get(store_id, pd.DataFrame())


def get_cached_factors(store_id: int) -> dict:
    """获取缓存中的影响因素（按需按店加载并缓存）。"""
    init_cache()
    cache = st.session_state.setdefault("_cache_factors", {})
    if store_id not in cache or cache.get(store_id) is None:
        from guibeicai.web._db import get_date_factors_for_store
        cache[store_id] = get_date_factors_for_store(store_id)
    return cache.get(store_id, {})


def refresh_sales_cache(store_id: int):
    """上传新数据后刷新销量缓存"""
    df = get_sales_history_df(store_id)
    if "_cache_sales" not in st.session_state:
        st.session_state["_cache_sales"] = {}
    if len(df) > 0:
        st.session_state["_cache_sales"][store_id] = df
    else:
        st.session_state["_cache_sales"].pop(store_id, None)
    # 失效按菜品分组索引，迫使下次重建
    st.session_state.setdefault("_cache_sales_by_dish", {}).pop(store_id, None)


def refresh_factors_cache(store_id: int):
    """刷新影响因素缓存"""
    from guibeicai.web._db import get_date_factors_for_store
    if "_cache_factors" not in st.session_state:
        st.session_state["_cache_factors"] = {}
    st.session_state["_cache_factors"][store_id] = get_date_factors_for_store(store_id)


def get_cached_sales_by_dish(store_id: int) -> dict:
    """按菜品分组的历史销量索引（构建一次，预测循环 O(1) 取数，消除重复全表扫描）。"""
    init_cache()
    by_dish_cache = st.session_state.setdefault("_cache_sales_by_dish", {})
    if store_id not in by_dish_cache or by_dish_cache.get(store_id) is None:
        sales = get_cached_sales(store_id)
        if len(sales) > 0:
            grouped = {d: g.reset_index(drop=True) for d, g in sales.groupby("dish_name")}
        else:
            grouped = {}
        by_dish_cache[store_id] = grouped
    return by_dish_cache.get(store_id, {})


def upsert_cached_factor(store_id: int, factor_date: str, factor_dict: dict):
    """写入单条影响因素到缓存"""
    init_cache()
    if "_cache_factors" not in st.session_state:
        st.session_state["_cache_factors"] = {}
    if store_id not in st.session_state["_cache_factors"]:
        st.session_state["_cache_factors"][store_id] = {}
    st.session_state["_cache_factors"][store_id][factor_date] = factor_dict
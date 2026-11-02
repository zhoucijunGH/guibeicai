"""数据初始化：新客户 / 门店快速录入（管理员 + 渠道商均可访问，key=data_init）

提供 CSV 批量导入入口，覆盖上线时手工逐条录入的缺口：
门店、菜品配方(自动建食材)。

- 管理员（运营方）：导入门店不绑定特定账号，属全租户公共门店；
- 渠道商：导入门店自动绑定到本渠道账号，仅在本渠道权限范围内可见与管理
  （配合「渠道门店账号」页做门店账号分发）。

生产割接如需清空全部业务数据，直接删除数据库文件后重新 init 即可，不在页面提供该危险操作。
"""

import streamlit as st
import pandas as pd
from io import BytesIO

from guibeicai.web._nav import render_nav
from guibeicai.web._db import init_db, get_tenant
from guibeicai.web._bulk_import import (
    import_stores_csv, import_recipes_csv,
    template_stores, template_recipes,
)
from guibeicai.web._audit import log_action, ACTION_DATA_INIT
from guibeicai.web._auth import require_auth, require_page_access, is_admin
from guibeicai.web.components import page_header, section, banner, empty_state

init_db()
user = require_auth()
require_page_access("data_init")

st.set_page_config(page_title="数据初始化", page_icon="📥", layout="wide")
render_nav()
page_header("数据初始化", "新客户 / 门店快速录入：CSV 批量导入门店、菜品配方", icon="📥")

# 渠道商导入门店时自动绑定到本账号；管理员（运营方）导入不绑定特定账号
bind_user_id = None if is_admin(user) else user["id"]


def _download_template(name: str, df: pd.DataFrame):
    buf = BytesIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    st.download_button(f"📥 下载{name}模板", buf.getvalue(),
                       file_name=f"桂备菜_{name}模板.csv", mime="text/csv",
                       use_container_width=True, key=f"tpl_{name}")


def _show_result(res: dict, label: str):
    if res.get("errors"):
        st.error(f"❌ {label}：{len(res['errors'])} 处错误")
        for e in res["errors"][:20]:
            st.write(f"  - {e}")
    created = res.get("created", 0)
    updated = res.get("updated", 0)
    skipped = res.get("skipped", 0)
    st.success(f"✅ {label}完成：新增 {created}，更新/写入 {updated}，跳过 {skipped}")


def _import_block(title: str, key: str, template_fn, import_fn, cols: str, order: str, fn_kwargs: dict = None):
    section(title, icon="📥")
    st.caption(cols)
    st.caption(order)
    c1, c2 = st.columns([3, 1])
    with c1:
        up = st.file_uploader(f"上传{title}CSV", type=["csv"], key=f"up_{key}")
    with c2:
        _download_template(title, template_fn())

    if up is not None:
        if st.button(f"📤 导入{title}", type="primary", key=f"run_{key}", use_container_width=True):
            try:
                df = pd.read_csv(up, dtype=str, keep_default_na=False)
                res = import_fn(df, **(fn_kwargs or {}))
                _show_result(res, title)
                log_action(ACTION_DATA_INIT, target_type="bulk", target_id=key,
                           detail={"rows": int(len(df)), "created": res.get("created", 0),
                                   "updated": res.get("updated", 0), "skipped": res.get("skipped", 0),
                                   "errors": len(res.get("errors", []))}, user=user)
                st.rerun()
            except Exception as e:
                st.error(f"❌ 导入失败：{e}")


# ==================== 各实体导入 ====================
_import_block(
    "门店", "stores", template_stores, import_stores_csv,
    cols="列：门店、省份、城市(必填)、老板邮箱、企微Webhook、自动预测(是/否)、预测时间(HH:MM)、预测目标日(今天/明天/后天)",
    order="顺序：最先导入。门店是菜品/销量/食材的外键。" + ("渠道商导入的门店将自动绑定到本渠道账号。" if bind_user_id else ""),
    fn_kwargs={"bind_user_id": bind_user_id} if bind_user_id else None,
)

_import_block(
    "菜品配方(BOM)", "recipes", template_recipes, import_recipes_csv,
    cols="列：门店、菜品、菜品单价(可选)、食材、品类、每份用量、单位、食材单价(可选)",
    order="顺序：门店之后。同一(门店,菜品)的多行会整体替换该菜配方；菜品/食材不存在则自动创建，无需大模型。",
)

st.caption("💡 提示：门店 / 菜品配方 BOM 批量导入均**幂等**（重复导入同名记录走更新，不重复建）。"
           "生产割接如需清空全部业务数据，直接删除数据库文件后重新初始化即可。")

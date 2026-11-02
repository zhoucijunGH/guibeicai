"""真实 Streamlit 运行时页面测试（AppTest）。

与 smoke_all_pages 的区别：AppTest 在**真实的 Streamlit 运行时**里执行页面脚本，
能捕获 mock 桩看不出来的问题（真实 st.* 调用签名、session_state 语义、组件参数等）。

数据：把真实演示库复制一份到临时路径（SQLITE_PATH 指向副本），
既能用真实体量数据渲染，又完全不触碰 data/guibeicai.db。

用法：
    python tests/manual/apptest_pages.py            # 全部页面
    python tests/manual/apptest_pages.py 4 5 17     # 只测文件名含这些编号的页面
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

REAL_DB = _PROJECT_ROOT / "data" / "guibeicai.db"
TMP_DB = os.path.join(tempfile.gettempdir(), "apptest_copy.db")
if REAL_DB.exists():
    shutil.copy2(REAL_DB, TMP_DB)
os.environ["SQLITE_PATH"] = TMP_DB           # 必须在 import streamlit/_db 之前
os.environ["GUOBEICAI_DISABLE_SEED"] = "1"

from streamlit.testing.v1 import AppTest  # noqa: E402
import streamlit as _st  # noqa: E402

# AppTest 的已知限制：在“单页隔离”上下文里 st.page_link 读不到多页注册表，
# 会抛 KeyError: 'url_pathname'。真实运行无此问题（应用可正常启动、HTTP 200）。
# 这里把 page_link/switch_page 置为 no-op，便于用真实运行时校验页面其余逻辑。
_st.page_link = lambda *a, **k: None
_st.switch_page = lambda *a, **k: None

_WEB = _PROJECT_ROOT / "guibeicai" / "web"

PAGES = [
    "app.py",
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
    "pages/15_智能预警.py",
    "pages/16_损耗分析.py",
    "pages/17_食材管理.py",
    "pages/18_渠道工作台.py",
    "pages/20_数据初始化.py",
    "pages/21_渠道门店与供货.py",
    "pages/22_合同管理.py",
    "pages/23_渠道门店账号.py",
]

filters = sys.argv[1:]
if filters:
    PAGES = [p for p in PAGES if any(f in p for f in filters)]

results = []
for rel in PAGES:
    path = _WEB / rel
    if not path.exists():
        results.append((rel, "SKIP", ""))
        continue
    t0 = time.time()
    try:
        at = AppTest.from_file(str(path), default_timeout=90)
        at.session_state["user"] = {"id": 1, "username": "admin", "role": "admin"}
        at.session_state["login_time"] = time.time()
        at.run()
        exc = list(at.exception)
        if exc:
            msg = "; ".join(str(e.value) for e in exc)
            results.append((rel, "FAIL", msg))
        else:
            results.append((rel, "PASS", f"{time.time() - t0:.1f}s"))
    except Exception as e:
        results.append((rel, "FAIL", f"{type(e).__name__}: {e}"))

print("=" * 78)
fails = 0
for rel, status, info in results:
    mark = {"PASS": "OK  ", "SKIP": "SKIP", "FAIL": "FAIL"}[status]
    print(f"[{mark}] {rel:28} {status:10} {info}")
    if status == "FAIL":
        fails += 1
print("=" * 78)
print(f"TOTAL={len(results)} FAIL={fails}")
sys.exit(1 if fails else 0)

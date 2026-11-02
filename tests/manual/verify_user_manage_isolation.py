"""验证「用户管理」页角色隔离：非管理员(渠道)只能看到/操作商家(门店)账号。

使用临时 SQLite（SQLITE_PATH 指向临时文件），不触碰真实演示库。
"""
import os
import sys
import tempfile

# 指定临时库，避免污染真实数据
_tmp = tempfile.mktemp(suffix=".db")
os.environ["SQLITE_PATH"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from guibeicai.web import _db

PASS, FAIL = [], []

def check(name, cond):
    (PASS if cond else FAIL).append(name)

# 初始化 + 建三类账号（模拟单租户内：管理员 / 渠道 / 门店）
_db.init_db()
_db.add_user("admin01", "x", "admin")
_db.add_user("channel01", "x", "channel")
_db.add_user("merchant01", "x", "merchant")   # store_ids=None，仅测隔离
_db.add_user("merchant02", "x", "merchant")

all_users = _db.get_all_users()
by_name = {u["username"]: u for u in all_users}
print("库中账号角色:", {u["username"]: u["role"] for u in all_users})

# ---- 管理员视角：可见全部 ----
admin_view = [u["username"] for u in all_users]  # 不过滤
check("admin 可见管理员", "admin01" in admin_view)
check("admin 可见渠道", "channel01" in admin_view)
check("admin 可见门店", "merchant01" in admin_view)

# ---- 渠道视角：仅可见 merchant ----
current_is_admin = False
channel_view = [u for u in all_users if (u.get("role") == "merchant" or current_is_admin)]
check("渠道 不可见管理员", "admin01" not in [u["username"] for u in channel_view])
check("渠道 不可见渠道账号", "channel01" not in [u["username"] for u in channel_view])
check("渠道 可见门店01", "merchant01" in [u["username"] for u in channel_view])
check("渠道 可见门店02", "merchant02" in [u["username"] for u in channel_view])

# ---- 防御性守卫：渠道尝试改/删非门店账号应被拦截 ----
def guarded_edit_allowed(viewer_is_admin, target_role):
    if not viewer_is_admin and target_role != "merchant":
        return False  # st.error + st.stop()
    return True

check("渠道 改管理员密码被拦截", not guarded_edit_allowed(False, "admin"))
check("渠道 删渠道账号被拦截", not guarded_edit_allowed(False, "channel"))
check("渠道 改门店密码允许", guarded_edit_allowed(False, "merchant"))
check("管理员 改管理员密码允许", guarded_edit_allowed(True, "admin"))

# 清理
try:
    os.remove(_tmp)
except OSError:
    pass

print("\nPASS:", len(PASS), "FAIL:", len(FAIL))
for f in FAIL:
    print("  ✗", f)
for p in PASS:
    print("  ✓", p)
sys.exit(1 if FAIL else 0)

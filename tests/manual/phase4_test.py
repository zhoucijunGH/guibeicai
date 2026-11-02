"""#15 自测脚本（Phase 4：操作审计 + 密钥托管）

使用临时 SQLite 库（不污染真实数据），验证：
1. init_db 能创建 audit_log 表（迁移幂等）
2. log_action 写入 + load_audit_log 倒序读取
3. 按动作 / 用户名 / 日期区间筛选
4. 审计写入失败不抛异常（空 detail / 特殊字符）
5. settings.validate_secrets() / mask_secret() 行为正确
"""

import os
import sys
import tempfile
import traceback

# 必须在导入 guibeicai 模块之前设置临时库路径（_db 在 import 时即解析 DB_PATH）
_TMP = tempfile.mkdtemp(prefix="guibeicai_phase4_")
os.environ["SQLITE_PATH"] = os.path.join(_TMP, "test_audit.db")

# 让 guibeicai 包可导入（从项目根运行）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {extra}")


print("=== #15 自测开始（临时库: %s）===" % os.environ["SQLITE_PATH"])

# 1) 建表迁移
from guibeicai.web import _db
_db.init_db()
conn = _db.get_conn()
cols = [r[1] for r in conn.execute("PRAGMA table_info(audit_log)").fetchall()]
conn.close()
check("audit_log 表已创建", "id" in cols and "action" in cols and "detail_json" in cols, str(cols))

# 2) 写入 + 倒序读取
from guibeicai.web import _audit
_audit.log_action(_audit.ACTION_LOGIN, target_type="user", target_id=1, detail={"username": "admin"}, user={"id": 1, "username": "admin", "role": "admin"})
_audit.log_action(_audit.ACTION_PREDICT_MANUAL, target_type="store", target_id=3, detail={"store_name": "总店", "revenue": 1234.5}, user={"id": 2, "username": "merchant", "role": "merchant"})
_audit.log_action(_audit.ACTION_LOGIN_FAILED, target_type="user", target_id="hacker", detail={"username": "hacker"}, result="fail")

rows = _audit.load_audit_log(limit=50)
check("写入3条并全部读出", len(rows) == 3, f"got {len(rows)}")
check("默认倒序（最新在前）", rows[0]["action"] == _audit.ACTION_LOGIN_FAILED, rows[0]["action"])

# 3) 动作筛选
login_rows = _audit.load_audit_log(action=_audit.ACTION_LOGIN)
check("按动作筛选 login", len(login_rows) == 1 and login_rows[0]["action"] == _audit.ACTION_LOGIN, str(len(login_rows)))

# 4) 用户名模糊筛选
admin_rows = _audit.load_audit_log(username="admin")
check("按用户名筛选 admin", len(admin_rows) == 1, str(len(admin_rows)))

# 5) 日期区间筛选（用 ts 字符串比较）
all_rows = _audit.load_audit_log(date_from="2000-01-01 00:00:00", date_to="2999-12-31 23:59:59")
check("日期区间筛选全量", len(all_rows) == 3, str(len(all_rows)))

# 6) 审计写入健壮性：detail 为 None / 含特殊字符不抛异常
try:
    _audit.log_action("weird", detail=None, user=None)
    _audit.log_action("weird2", detail={"x": "引号'\"中文"}, user={"id": 0, "username": "系统(定时任务)", "role": "system"})
    check("异常 detail/无用户 不抛异常", True)
except Exception as e:
    check("异常 detail/无用户 不抛异常", False, str(e))

# 7) 失败结果被记录
fail_rows = _audit.load_audit_log(action=_audit.ACTION_LOGIN_FAILED)
check("失败结果写入 result=fail", fail_rows and fail_rows[0]["result"] == "fail", str(fail_rows))

# 8) settings 密钥校验 / 脱敏
from guibeicai.config.settings import settings
# 行为校验（与环境无关）：全部配置则缺失为空；清掉某个则被标记
os.environ["SILICONFLOW_API_KEY"] = "sk-test-dummy"
os.environ["WEATHER_API_KEY"] = "wx-test-dummy"
os.environ["EMAIL_PASSWORD"] = "pw-test-dummy"
missing_all = settings.validate_secrets()
check("validate_secrets 返回列表", isinstance(missing_all, list))
check("全部配置时缺失列表为空", missing_all == [], str(missing_all))

os.environ.pop("WEATHER_API_KEY", None)
missing_partial = settings.validate_secrets()
check("清掉某密钥后被标记为缺失", "WEATHER_API_KEY" in missing_partial, str(missing_partial))
# 还原（避免误影响后续，若原环境有则恢复无副作用）
os.environ["WEATHER_API_KEY"] = "wx-test-dummy"

masked = settings.mask_secret("sk-abcdefghijklmnop", keep=4)
check("mask_secret 脱敏正确", masked.startswith("sk-a") and masked.endswith("mnop") and "*" in masked, masked)
check("mask_secret 空值安全", settings.mask_secret("") == "")
check("mask_secret 短值不越界", settings.mask_secret("ab", keep=4) == "**")

# 9) 迁移幂等：再次 init_db 不报错且表仍在
try:
    _db.init_db()
    conn = _db.get_conn()
    cols2 = [r[1] for r in conn.execute("PRAGMA table_info(audit_log)").fetchall()]
    conn.close()
    check("二次 init_db 幂等（表仍在）", "action" in cols2)
except Exception as e:
    check("二次 init_db 幂等（表仍在）", False, str(e))

print("=== #15 自测结束：PASS=%d / FAIL=%d ===" % (passed, failed))
sys.exit(1 if failed else 0)

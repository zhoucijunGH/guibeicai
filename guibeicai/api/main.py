r"""桂备菜 · 小程序端后端：轻量只读 + 少量写 API（FastAPI）。

设计原则：
- 复用 WEB 端 guibeicai.web._db 的查询函数与同一份 SQLite（data/guibeicai.db），不重复业务逻辑。
- 鉴权复用 WEB 端账户体系：POST /api/login 校验 users 表（authenticate_user），
  颁发 HMAC 签名令牌；其余接口经 Authorization: Bearer <token> 鉴权，
  并按用户绑定的门店（get_user_store_ids）做数据隔离，管理员可访问全部门店。
- 仅两个写操作：POST /api/predict/run（触发预测，异步）、POST /api/actual-sales（录入实际销量）。
- 预测逻辑复用共享核心 _predict_core.run_prediction（与 WEB 端页面同一套，保证两端一致）。

本机运行：
    python -m venv .venv-api && .venv-api\Scripts\activate
    pip install fastapi uvicorn
    uvicorn guibeicai.api.main:app --host 0.0.0.0 --port 8000
小程序（H5 本机）访问：http://127.0.0.1:8000 或本机局域网 IP。
"""
import sys
import os
import json
import time
import logging
import threading
import datetime
import base64
import hashlib
import hmac
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from guibeicai.web import _db as db
from guibeicai.web import _ingredient_forecast as ingfc
from guibeicai.web import _predict_core
from guibeicai.data_import import pos_client

db.init_db()

# 统一日志（幂等；取代原先仅控制台的 basicConfig）
from guibeicai.web._logging_setup import init_logging
init_logging()

app = FastAPI(title="桂备菜小程序 API", version="0.2.0")

# [C6 安全加固] CORS 来源改为可配置：默认开发放开、生产收紧。
#   小程序（wx.request）不受 CORS 限制，收紧不影响主客户端；
#   若生产另有 H5 端，请用 GUOBEICAI_API_CORS_ORIGINS 显式列出来源（逗号分隔）。
_ENV = os.getenv("GUOBEICAI_ENV", os.getenv("ENV", "development")).strip().lower()
_IS_PROD = _ENV in ("production", "prod", "1")
_cors_env = os.getenv("GUOBEICAI_API_CORS_ORIGINS", "").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
elif _IS_PROD:
    _cors_origins = []  # 生产未配置：默认不允许跨域（小程序无需 CORS）
    logging.getLogger("guibeicai_api").warning(
        "[安全] 生产环境未配置 GUOBEICAI_API_CORS_ORIGINS，CORS 已收紧为不允许跨域。"
        "如需 H5 跨域访问，请显式配置允许来源。"
    )
else:
    _cors_origins = ["*"]  # 开发环境放开，便于本地调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger("guibeicai_api")


# ----------------------------------------------------------------------------
# 鉴权：复用 WEB 端账户体系（users 表 + user_stores 绑定 + 角色）
# ----------------------------------------------------------------------------
# [S2 安全加固] API 签名密钥：禁止硬编码兜底（旧值 dev-local-guibeicai-secret 可被用于伪造令牌）。
# - 配置了 GUOBEICAI_API_SECRET：使用固定强随机值（推荐，且与 WEB 端无耦合）。
# - 未配置且为生产环境（GUOBEICAI_ENV=production/prod/1）：直接拒绝启动。
# - 未配置且为开发环境：生成临时随机密钥（仅本进程有效，重启失效）并明确告警。
_API_SECRET_ENV = os.getenv("GUOBEICAI_API_SECRET", "").strip()
if _API_SECRET_ENV:
    API_SECRET = _API_SECRET_ENV
else:
    if _IS_PROD:  # 生产环境判定见上方 CORS 加固处（复用同一 _IS_PROD）
        raise RuntimeError(
            "安全策略拒绝启动：生产环境必须配置 GUOBEICAI_API_SECRET 环境变量"
            "（建议 32+ 字节强随机值，如 `python -c \"import secrets;print(secrets.token_hex(32))\"`）。"
            "未配置时存在令牌被伪造的风险。"
        )
    import secrets
    API_SECRET = secrets.token_hex(32)
    logger.warning(
        "安全告警：GUOBEICAI_API_SECRET 未配置，已生成临时随机密钥（仅本进程有效，重启后失效）。"
        "请勿在生产环境使用此模式；请通过环境变量配置固定强随机值。"
    )

_TOKEN_TTL = int(os.getenv("GUOBEICAI_TOKEN_TTL", str(7 * 24 * 3600)))


def _issue_token(user_id: int) -> str:
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + _TOKEN_TTL}).encode("utf-8")
    b64 = base64.urlsafe_b64encode(payload).decode("ascii")
    sig = hmac.new(API_SECRET.encode("utf-8"), b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _verify_token(token: str):
    try:
        b64, sig = token.split(".", 1)
        exp_sig = hmac.new(API_SECRET.encode("utf-8"), b64.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(exp_sig, sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(b64))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    """校验 Bearer 令牌，返回当前用户（含绑定门店）。401/403 由 FastAPI 自动转 HTTP 状态。"""
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Authorization 头")
    token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    payload = _verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="无效或过期的令牌")
    u = db.get_user_by_id(payload["uid"])
    if not u:
        raise HTTPException(status_code=401, detail="用户不存在")
    # 账号有效期校验（按合同期限管控）：过期或尚未生效的账号拒绝访问
    valid, valid_reason = db.user_validity_status(u)
    if not valid:
        raise HTTPException(status_code=403, detail=valid_reason)
    store_ids = db.get_user_store_ids(u["id"])
    return {
        "id": u["id"],
        "username": u["username"],
        "role": u["role"],
        "is_admin": db.is_admin_role(u["role"]),
        "store_ids": store_ids,
    }


def _assert_store(user: dict, store_id: int):
    """门店数据隔离：管理员可访问全部，普通用户仅限绑定门店。"""
    if user["is_admin"]:
        return
    if store_id not in user["store_ids"]:
        raise HTTPException(status_code=403, detail="无权访问该门店数据")


# 只读角色：页面层已用 roles.pages 屏蔽入口，API 层再兜底阻断写接口，
# 杜绝"无页面入口但仍能通过令牌写入"。
# 注：渠道商（channel）在 2026-09-09 重构后已升级为可写（录入门店 + 配置供货范围），
# 故不再列入只读集合；如需新增纯只读角色，在此登记即可生效。
READONLY_ROLES = set()


def _assert_writable(user: dict):
    """写操作权限校验：只读角色禁止调用任何会改变业务数据的接口。"""
    if (user.get("role") or "") in READONLY_ROLES:
        raise HTTPException(status_code=403, detail="当前角色为只读视角，无权执行该操作")


# ----------------------------------------------------------------------------
# 异步预测任务状态（底层调用共享预测核心 _predict_core.run_prediction，与 WEB 端一致）
# ----------------------------------------------------------------------------
_tasks: dict = {}
_tasks_lock = threading.Lock()

# [S6 安全加固] 预测并发限制：
# - 有界线程池，避免高频调用耗尽线程/CPU；
# - 单店串行：同一门店预测进行中拒绝新请求，保护 SQLite 写入并避免重复预测；
# - 全局并发上限 = 线程池大小，超出返回 429。
import concurrent.futures

_PREDICT_MAX_WORKERS = max(1, int(os.getenv("GUOBEICAI_PREDICT_MAX_WORKERS", "2")))
_predict_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_PREDICT_MAX_WORKERS, thread_name_prefix="guibeicai-predict"
)
_running_stores: set = set()
_running_stores_lock = threading.Lock()

# [S7 安全加固] 任务结果表内存治理：
# 任务结果原无限增长（且不跨 worker 共享）。此处为任务结果加 TTL 自动过期，
# 配合惰性清理 + 后台回收线程，避免长期运行内存泄漏。最终预测结果仍以
# predictions 表持久化为准（前端/调度以 DB 结果而非轮询状态）。
_TASK_TTL = max(60, int(os.getenv("GUOBEICAI_TASK_TTL", str(3600))))  # 秒
_task_times: dict = {}  # task_id -> 创建时间戳


def _evict_expired_tasks():
    """惰性清理：删除超过 TTL 的任务结果（释放内存）。"""
    now = time.time()
    with _tasks_lock:
        expired = [tid for tid in list(_tasks) if now - _task_times.get(tid, 0) > _TASK_TTL]
        for tid in expired:
            _tasks.pop(tid, None)
            _task_times.pop(tid, None)
    if expired:
        logger.info("已清理 %d 个过期预测任务结果（TTL=%ds）", len(expired), _TASK_TTL)


def _task_reaper():
    """后台回收线程：周期性清理过期任务结果。"""
    while True:
        time.sleep(max(60, _TASK_TTL // 2))
        try:
            _evict_expired_tasks()
        except Exception as e:  # 回收失败不应影响主流程
            logger.warning("任务回收异常: %s", e)


threading.Thread(target=_task_reaper, daemon=True, name="task-reaper").start()


def _do_predict(task_id: str, store_id: int, predict_date: str, special_event: str):
    try:
        with _tasks_lock:
            _tasks[task_id] = {"status": "running", "progress": "读取菜品与历史"}

        def _cb(done, total, msg):
            with _tasks_lock:
                _tasks[task_id] = {"status": "running", "progress": msg}

        res = _predict_core.run_prediction(
            store_id, [predict_date], manual_event=special_event or "",
            progress_cb=_cb,
        )
        with _tasks_lock:
            _tasks[task_id] = {
                "status": "done", "progress": "完成",
                "predict_date": predict_date,
                "total_revenue": res.get("total_revenue"),
                "gross_margin": res.get("gross_margin"),
                "overall_risk": res.get("overall_risk"),
            }
    except Exception as e:
        logger.exception("预测失败 store_id=%s date=%s: %s", store_id, predict_date, e)
        with _tasks_lock:
            _tasks[task_id] = {"status": "error", "progress": "", "error": str(e)}
    finally:
        # 预测结束（成功/失败）后释放该门店的并发占用
        with _running_stores_lock:
            _running_stores.discard(store_id)


# ----------------------------------------------------------------------------
# 端点
# ----------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "ts": datetime.datetime.now().isoformat()}


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def api_login(body: LoginIn):
    """登录：复用 WEB 端账户（users 表）。返回令牌与可访问门店列表。"""
    user = db.authenticate_user(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    store_ids = db.get_user_store_ids(user["id"])
    is_admin = db.is_admin_role(user["role"])
    if is_admin:
        stores = db.get_all_stores()
    else:
        stores = [s for s in db.get_all_stores() if s["id"] in store_ids] if store_ids else []
    return {
        "token": _issue_token(user["id"]),
        "user": {
            "id": user["id"], "username": user["username"], "role": user["role"],
            "is_admin": is_admin, "store_ids": store_ids,
            "need_change_pwd": bool(user.get("need_change_pwd", False)),
        },
        "stores": stores,
    }


# ============================================================================
# [OPENID] 微信 openID 登录 + 绑定（移动端推送身份识别）
# ----------------------------------------------------------------------------
# 启用条件：写入环境变量 GUOBEICAI_WX_APPID / GUOBEICAI_WX_SECRET。
# 未配置时 /api/wx/login 返回明确错误，其他接口不受影响（优雅降级）。
# openID 仅作「绑定后一键登录」便捷通道，不替代账号体系（门店隔离逻辑不变）。
# ============================================================================
GUOBEICAI_WX_APPID = os.getenv("GUOBEICAI_WX_APPID", "")
GUOBEICAI_WX_SECRET = os.getenv("GUOBEICAI_WX_SECRET", "")


@app.get("/api/wx/login")
def api_wx_login(code: str = Query(...)):
    """微信小程序登录：wx.login 拿 code → 微信 code2session → openid → 已绑定用户 → 发令牌。"""
    if not GUOBEICAI_WX_APPID or not GUOBEICAI_WX_SECRET:
        raise HTTPException(status_code=400, detail="微信登录未配置 AppID/AppSecret（请联系管理员）")
    import requests  # 延迟导入，避免无 requests 环境启动失败
    r = requests.get(
        "https://api.weixin.qq.com/sns/jscode2session",
        params={"appid": GUOBEICAI_WX_APPID, "secret": GUOBEICAI_WX_SECRET,
                "js_code": code, "grant_type": "authorization_code"},
        timeout=8,
    )
    data = r.json()
    if "openid" not in data:
        raise HTTPException(status_code=400, detail="微信登录失败: " + str(data))
    openid = data["openid"]
    u = db.get_user_by_openid(openid)
    if not u:
        # 未绑定的微信：返回 openid 让前端引导「先用账号登录再绑定」
        raise HTTPException(status_code=401, detail="该微信未绑定账户，请先用 WEB 账号登录后绑定")
    store_ids = db.get_user_store_ids(u["id"])
    is_admin = db.is_admin_role(u["role"])
    stores = db.get_all_stores() if is_admin else [s for s in db.get_all_stores() if s["id"] in store_ids]
    return {
        "token": _issue_token(u["id"]),
        "user": {"id": u["id"], "username": u["username"], "role": u["role"],
                 "is_admin": is_admin, "store_ids": store_ids,
                 "need_change_pwd": bool(u.get("need_change_pwd", False))},
        "stores": stores,
        "openid": openid,
    }


class ChangePasswordIn(BaseModel):
    new_password: str
    # 强制改密场景下用户不知道旧密码，故 old_password 可选；
    # 非强制场景下建议由前端先校验旧密码（此处仅做最小校验）。
    old_password: str = None


@app.post("/api/change-password")
def api_change_password(body: ChangePasswordIn, authorization: str = Header(None)):
    """修改密码（需鉴权）。用于首次登录强制改密或用户自助改密。"""
    user = get_current_user(authorization)
    if not body.new_password or len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度至少 6 位")
    db.update_user_password(user["id"], body.new_password, force_change_pwd=0)
    return {"ok": True, "msg": "密码已修改"}


class WxBindIn(BaseModel):
    openid: str = None
    code: str = None


@app.post("/api/wx/bind")
def api_wx_bind(body: WxBindIn, authorization: str = Header(None)):
    """把当前登录账户的 openid 绑定（用于移动端一键登录与推送）。

    支持两种入参：
    - openid: 前端已持有 openid（如 wx.login 后由后端回传）
    - code: 前端持有 wx.login() 的 code，后端 code2session 换取 openid 后绑定
    """
    user = get_current_user(authorization)
    openid = body.openid
    if not openid and body.code:
        if not GUOBEICAI_WX_APPID or not GUOBEICAI_WX_SECRET:
            raise HTTPException(status_code=400, detail="微信登录未配置 AppID/AppSecret")
        import requests
        r = requests.get(
            "https://api.weixin.qq.com/sns/jscode2session",
            params={"appid": GUOBEICAI_WX_APPID, "secret": GUOBEICAI_WX_SECRET,
                    "js_code": body.code, "grant_type": "authorization_code"},
            timeout=8,
        )
        data = r.json()
        if "openid" not in data:
            raise HTTPException(status_code=400, detail="微信登录失败: " + str(data))
        openid = data["openid"]
    if not openid:
        raise HTTPException(status_code=400, detail="openid 或 code 不能为空")
    # 若该 openid 已被别的账户占用，拒绝
    exist = db.get_user_by_openid(openid)
    if exist and exist["id"] != user["id"]:
        raise HTTPException(status_code=409, detail="该微信已绑定其他账户")
    ok = db.bind_openid(user["id"], openid)
    if not ok:
        raise HTTPException(status_code=500, detail="绑定失败")
    return {"ok": True, "openid": openid}


@app.post("/api/wx/unbind")
def api_wx_unbind(authorization: str = Header(None)):
    """解除当前账户的 openid 绑定。"""
    user = get_current_user(authorization)
    db.unbind_openid(user["id"])
    return {"ok": True}


def _wx_access_token():
    """获取小程序全局 access_token（用于手机号解密接口）。失败返回 None。"""
    import requests
    try:
        r = requests.get(
            "https://api.weixin.qq.com/cgi-bin/token",
            params={"grant_type": "client_credential",
                    "appid": GUOBEICAI_WX_APPID, "secret": GUOBEICAI_WX_SECRET},
            timeout=8,
        )
        return r.json().get("access_token")
    except Exception:
        return None


def _wx_phone_from_code(phone_code: str):
    """用 getPhoneNumber 组件返回的 code 换取真实手机号；失败返回 None。"""
    import requests
    tok = _wx_access_token()
    if not tok:
        return None
    try:
        r = requests.post(
            "https://api.weixin.qq.com/wxa/business/getuserphonenumber",
            params={"access_token": tok}, json={"code": phone_code}, timeout=8,
        )
        data = r.json()
        if data.get("errcode") == 0:
            return data.get("phone_info", {}).get("phoneNumber")
    except Exception:
        return None
    return None


class WxBindPhoneIn(BaseModel):
    code: str = None         # wx.login() 的 code（必需，用于换取本次微信 openid）
    phone_code: str = None   # getPhoneNumber 组件返回的 code（微信授权手机号，优先）
    phone: str = None        # 用户手动输入的手机号（无 phone_code 时使用）


@app.post("/api/wx/bind-phone")
def api_wx_bind_phone(body: WxBindPhoneIn):
    """按手机号绑定微信（小程序「手机号绑定」入口）。

    流程：code→openid（确定本次微信身份）→ 手机号（授权优先，手输兜底）
          → 按手机号匹配 WEB 账号 → 绑定 openid → 直接签发令牌。
    用户侧只认手机号（用户知道），openid 全程自动，无需任何人手输。
    """
    if not GUOBEICAI_WX_APPID or not GUOBEICAI_WX_SECRET:
        raise HTTPException(status_code=400, detail="微信登录未配置 AppID/AppSecret（请联系管理员）")
    if not body.code:
        raise HTTPException(status_code=400, detail="缺少 wx.login code")
    import requests
    r = requests.get(
        "https://api.weixin.qq.com/sns/jscode2session",
        params={"appid": GUOBEICAI_WX_APPID, "secret": GUOBEICAI_WX_SECRET,
                "js_code": body.code, "grant_type": "authorization_code"},
        timeout=8,
    )
    data = r.json()
    if "openid" not in data:
        raise HTTPException(status_code=400, detail="微信登录失败: " + str(data))
    openid = data["openid"]

    phone = None
    if body.phone_code:
        phone = _wx_phone_from_code(body.phone_code)
        if not phone:
            raise HTTPException(status_code=400, detail="微信手机号授权失败，请改用手动输入")
    if not phone:
        phone = (body.phone or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="请提供手机号或完成微信手机号授权")

    res = db.bind_openid_by_phone(phone, openid)
    if not res["ok"]:
        _msg = {
            "no_user": "该手机号未开通账号，请联系管理员",
            "openid_taken": "该微信已绑定其他账号",
            "inactive": "账号未生效或已过期",
            "bad_input": "手机号无效",
            "bind_failed": "绑定失败，请重试",
        }.get(res["reason"], "绑定失败")
        _code = 404 if res["reason"] == "no_user" else (409 if res["reason"] == "openid_taken" else 400)
        raise HTTPException(status_code=_code, detail=_msg)

    u = res["user"]
    store_ids = db.get_user_store_ids(u["id"])
    is_admin = db.is_admin_role(u["role"])
    stores = db.get_all_stores() if is_admin else [s for s in db.get_all_stores() if s["id"] in store_ids]
    return {
        "token": _issue_token(u["id"]),
        "user": {"id": u["id"], "username": u["username"], "role": u["role"],
                 "is_admin": is_admin, "store_ids": store_ids,
                 "need_change_pwd": bool(u.get("need_change_pwd", False))},
        "stores": stores,
        "openid": openid,
        "phone": phone,
    }



@app.get("/api/me")
def api_me(authorization: str = Header(None)):
    """返回当前登录用户信息（令牌自检/刷新用）。"""
    return get_current_user(authorization)


@app.get("/api/stores")
def api_stores(authorization: str = Header(None)):
    user = get_current_user(authorization)
    if user["is_admin"]:
        return db.get_all_stores()
    return [s for s in db.get_all_stores() if s["id"] in user["store_ids"]]


@app.get("/api/prediction-dates")
def api_prediction_dates(store_id: int = Query(...), authorization: str = Header(None)):
    user = get_current_user(authorization)
    _assert_store(user, store_id)
    return {"store_id": store_id, "dates": ingfc.get_prediction_dates_by_store(store_id)}


@app.get("/api/dishes")
def api_dishes_list(store_id: int = Query(...), authorization: str = Header(None)):
    """菜品菜单（id + name + category），供小程序录入实际销量时选菜/回填，与预测解耦。"""
    user = get_current_user(authorization)
    _assert_store(user, store_id)
    dishes = db.get_dishes_by_store(store_id, active_only=True)
    return {"store_id": store_id, "dishes": [{"id": d["id"], "name": d["name"], "category": d.get("category", "")} for d in dishes]}


@app.get("/api/prediction")
def api_prediction(store_id: int = Query(...), date: str = Query(...), authorization: str = Header(None)):
    user = get_current_user(authorization)
    _assert_store(user, store_id)
    rec = db.get_prediction_by_store_date(store_id, date)
    if not rec:
        raise HTTPException(status_code=404, detail="该日无预测结果")
    return rec


@app.get("/api/prediction/{date}/dishes")
def api_dishes(date: str, store_id: int = Query(...), authorization: str = Header(None)):
    """单菜品维度：当日每菜品预测销量 + 其食材采购需求（用量/单价/小计）。"""
    user = get_current_user(authorization)
    _assert_store(user, store_id)
    pred = db.get_prediction_by_store_date(store_id, date)
    if not pred:
        raise HTTPException(status_code=404, detail="该日无预测结果")
    dish_preds = (pred.get("result") or {}).get("dish_predictions", {})
    recipes = db.get_active_recipes_by_store(store_id)
    # 菜名 → dish_id 映射，便于小程序录入实际销量（upsert 按 dish_id）
    dish_rows = db.get_dishes_by_store(store_id, active_only=True)
    name2id = {d["name"]: d["id"] for d in dish_rows}
    out = []
    for name, qty in dish_preds.items():
        items = []
        for r in recipes.get(name, []):
            items.append({
                "ingredient": r.get("ingredient_name"),
                "quantity": r.get("quantity"),
                "unit": r.get("unit"),
                "unit_price": r.get("unit_price"),
                "amount": r.get("amount"),
                "category": r.get("category"),
            })
        out.append({"dish_id": name2id.get(name), "dish": name, "predict_qty": qty, "ingredients": items})
    return out


@app.get("/api/prediction/{date}/ingredients")
def api_ingredients(date: str, store_id: int = Query(...), authorization: str = Header(None)):
    """食材维度：当日所有菜品的食材采购量汇总 + 金额，按成本降序。"""
    user = get_current_user(authorization)
    _assert_store(user, store_id)
    pred = db.get_prediction_by_store_date(store_id, date)
    if not pred:
        raise HTTPException(status_code=404, detail="该日无预测结果")
    dish_preds = (pred.get("result") or {}).get("dish_predictions", {})
    agg = ingfc.aggregate_ingredients(store_id, dish_preds)
    total_cost = round(sum(it.get("cost", 0) for it in agg), 2)
    total_qty = round(sum(it.get("quantity", 0) for it in agg), 3)
    return {"predict_date": date, "store_id": store_id, "items": agg,
            "total_cost": total_cost, "total_qty": total_qty, "dish_count": len(dish_preds)}


@app.get("/api/compare")
def api_compare(store_id: int = Query(...), date: str = Query(...), authorization: str = Header(None)):
    """预测 vs 实际对比：按菜名 join predictions.dish_predictions 与 actual_sales。"""
    user = get_current_user(authorization)
    _assert_store(user, store_id)
    pred = db.get_prediction_by_store_date(store_id, date)
    if not pred:
        raise HTTPException(status_code=404, detail="该日无预测结果")
    dish_preds = (pred.get("result") or {}).get("dish_predictions", {})

    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT d.name, a.actual_qty, a.reconciled FROM actual_sales a "
        "JOIN dishes d ON a.dish_id=d.id "
        "WHERE a.store_id=? AND a.sale_date=?",
        (store_id, date),
    )
    actual = {}
    reconciled_map = {}
    for row in cur.fetchall():
        actual[row[0]] = float(row[1] or 0)
        reconciled_map[row[0]] = int(row[2] or 0)
    conn.close()

    rows = []
    for name, pred_qty in dish_preds.items():
        act = actual.get(name)
        if act is None:
            status = "no_actual"
            bias = None
            source = None
        else:
            bias = round((act - pred_qty) / pred_qty * 100, 1) if pred_qty else None
            status = "over" if (bias or 0) > 0 else ("under" if (bias or 0) < 0 else "match")
            source = "pos" if reconciled_map.get(name, 0) else "manual"
        rows.append({"dish": name, "predict": pred_qty, "actual": act, "bias_pct": bias,
                     "status": status, "reconciled": reconciled_map.get(name, 0), "source": source})
    return {"predict_date": date, "store_id": store_id, "rows": rows}


class ActualSalesItem(BaseModel):
    dish_id: int
    actual_qty: float
    actual_waste: float = 0
    amount: float = 0


class ActualSalesIn(BaseModel):
    store_id: int
    sale_date: str
    items: list[ActualSalesItem]


@app.post("/api/actual-sales")
def api_actual_sales(body: ActualSalesIn, authorization: str = Header(None)):
    """录入/更新某日实际销量（按 store+dish+date upsert）。

    POS 推送模式：写入即视为已对账(reconciled=1)，并回灌 sales_history 供预测使用。
    """
    user = get_current_user(authorization)
    _assert_writable(user)
    _assert_store(user, body.store_id)
    n = 0
    for it in body.items:
        db.upsert_actual_sales(body.store_id, it.dish_id, body.sale_date, it.actual_qty,
                               it.actual_waste, actual_amount=it.amount, reconciled=1)
        n += 1
    try:
        bf = db.backfill_history_from_actual(store_id=body.store_id, sale_date=body.sale_date, only_reconciled=True)
    except Exception as e:
        logger.warning("回灌 sales_history 失败 store=%s date=%s: %s", body.store_id, body.sale_date, e)
        bf = {"rows": 0, "upserted": 0}
    return {"ok": True, "count": n, "backfill": bf}


class PredictRunIn(BaseModel):
    store_id: int
    predict_date: str
    special_event: str = ""


@app.post("/api/predict/run")
def api_predict_run(body: PredictRunIn, authorization: str = Header(None)):
    """触发预测（异步，后台线程执行；用 /api/predict/status/{task_id} 轮询）。

    [S6] 并发限制：同一门店预测进行中直接拒绝（429）；全局并发达上限也拒绝。
    """
    user = get_current_user(authorization)
    _assert_writable(user)
    _assert_store(user, body.store_id)
    with _running_stores_lock:
        if body.store_id in _running_stores:
            raise HTTPException(status_code=429, detail="该门店正在预测中，请稍后再试")
        if len(_running_stores) >= _PREDICT_MAX_WORKERS:
            raise HTTPException(status_code=429, detail="预测任务过多，请稍后再试")
        _running_stores.add(body.store_id)
    task_id = f"p_{int(time.time()*1000)}"
    with _tasks_lock:
        _tasks[task_id] = {"status": "queued", "progress": "排队中"}
        _task_times[task_id] = time.time()
    _evict_expired_tasks()
    _predict_executor.submit(_do_predict, task_id, body.store_id, body.predict_date, body.special_event)
    return {"task_id": task_id, "status": "running"}


@app.get("/api/predict/status/{task_id}")
def api_predict_status(task_id: str):
    _evict_expired_tasks()
    with _tasks_lock:
        return _tasks.get(task_id, {"status": "unknown"})


# ============================================================================
# POS/收银通用数据源对接（#商业化：页面可配置、支持多种数据源）
# ============================================================================
class PosSourceIn(BaseModel):
    store_id: int
    name: str
    source_type: str = "http_json"
    base_url: str = ""
    auth_type: str = "none"
    auth_config: dict = {}
    field_mapping: dict = {}
    request_config: dict = {}
    schedule: str = ""
    enabled: bool = True
    id: Optional[int] = None  # 给定则更新


@app.get("/api/pos-sources")
def api_pos_sources(store_id: int = Query(None), authorization: str = Header(None)):
    user = get_current_user(authorization)
    if user["is_admin"] and not store_id:
        srcs = db.get_all_pos_sources()
    elif store_id:
        _assert_store(user, store_id)
        srcs = db.get_pos_sources_by_store(store_id)
    else:
        srcs = []
        for sid in user["store_ids"]:
            srcs.extend(db.get_pos_sources_by_store(sid))
    # [S3] 脱敏：不回显凭据明文
    return {"sources": [db.mask_pos_source(s) for s in srcs]}


@app.post("/api/pos-sources")
def api_pos_sources_upsert(body: PosSourceIn, authorization: str = Header(None)):
    user = get_current_user(authorization)
    _assert_writable(user)
    _assert_store(user, body.store_id)
    try:
        sid = db.upsert_pos_source(
            store_id=body.store_id, name=body.name, source_type=body.source_type,
            base_url=body.base_url, auth_type=body.auth_type, auth_config=body.auth_config,
            field_mapping=body.field_mapping, request_config=body.request_config,
            schedule=body.schedule,
            enabled=1 if body.enabled else 0, source_id=body.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "id": sid}


@app.delete("/api/pos-sources/{source_id}")
def api_pos_sources_delete(source_id: int, authorization: str = Header(None)):
    user = get_current_user(authorization)
    _assert_writable(user)
    src = db.get_pos_source(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="数据源不存在")
    _assert_store(user, src["store_id"])
    db.delete_pos_source(source_id)
    return {"ok": True}


@app.post("/api/pos-sources/test")
def api_pos_sources_test(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    _assert_writable(user)
    if body.get("store_id"):
        _assert_store(user, body["store_id"])
    return pos_client.test_connection(body)


@app.post("/api/pos-sources/{source_id}/pull")
def api_pos_sources_pull(source_id: int, body: dict = None, authorization: str = Header(None)):
    user = get_current_user(authorization)
    _assert_writable(user)
    src = db.get_pos_source(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="数据源不存在")
    _assert_store(user, src["store_id"])
    sale_date = (body or {}).get("sale_date") if body else None
    return pos_client.pull_source(source_id, sale_date)


# ==================== 采购单推送（移动端核心） ====================

class PurchasePushIn(BaseModel):
    store_id: int
    date: Optional[str] = None
    wx_template_data: Optional[dict] = None
    wx_page: str = "pages/ingredients/ingredients"


@app.post("/api/push/purchase-order")
def api_push_purchase_order(body: PurchasePushIn, authorization: str = Header(None)):
    """生成采购单并推送到邮件 / 企业微信 / 微信订阅消息。

    推送目标来自门店配置：owner_email（邮件）、wecom_webhook（企微）、
    以及绑定到该门店且已绑 openid 的用户（微信订阅消息）。
    未配置/未绑定的渠道自动跳过（优雅降级）。
    """
    user = get_current_user(authorization)
    _assert_writable(user)
    _assert_store(user, body.store_id)
    from guibeicai.web._ingredient_forecast import build_purchase_order
    from guibeicai.scheduler.notifiers.purchase_notifier import PurchaseNotifier

    po = build_purchase_order(body.store_id, predict_date=body.date)
    if not po.get("items"):
        raise HTTPException(status_code=400, detail="该门店/日期暂无采购单可推送")

    targets = db.get_store_push_targets(body.store_id)
    store = db.get_store_by_id(body.store_id) or {}
    from guibeicai.config.settings import Settings
    email_cfg = dict(Settings().email_config)
    notifier = PurchaseNotifier(config={
        "email": email_cfg,
        "wx_subscribe": {},
    })
    results = notifier.send_purchase_order(
        order=po,
        store_name=store.get("name", ""),
        date=po.get("predict_date") or body.date or "",
        emails=targets["emails"],
        openids=targets["openids"],
        wecom_webhook=targets["wecom_webhook"],
        wx_template_data=body.wx_template_data,
        wx_page=body.wx_page,
    )
    return {
        "ok": True,
        "store": store.get("name", ""),
        "date": po.get("predict_date") or body.date,
        "total_cost": po.get("total_cost"),
        "channels": results,
        "pushed_openids": len(targets["openids"]),
        "pushed_emails": len(targets["emails"]),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

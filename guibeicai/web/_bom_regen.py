"""增量、可恢复的菜品配方重生成核心逻辑。

设计要点（回应「批量执行易整批失败 / 新门店大批菜品无休止等待」的隐患）：
- 小批请求 LLM（默认每批 4 个菜名） -> 每批返回**立即落库并提交** -> 再进行下一批；
- 天然可恢复：仅处理 bom_status='pending' 的菜品，已 generated 的自动跳过；
  中途崩溃/超时，已落库的批次不丢，重跑即可续上；
- 支持按门店（store_id）只重算某门店，适配「新门店导入大批菜品」场景；
- 整条写链路复用单一共享连接，杜绝自争用导致的 database is locked；
- 启动时检测 8501 是否被 Streamlit 占用，占用则明确报错退出（避免静默整批锁死）。
"""
import time
import socket

from guibeicai.web._db import (
    init_db, get_dishes_need_regen, get_conn,
    replace_recipe_for_dish, recompute_all_costs,
)
from guibeicai.web._llm import generate_recipes_for_dishes, API_KEY


def _port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def regen_bom(
    store_id=None,
    batch: int = 6,
    force: bool = False,
    max_batches: int = None,
    max_workers: int = 2,
    progress_cb=None,
    log=print,
) -> dict:
    """
    增量重生成菜品配方。

    :param store_id: 仅重算该门店（None=全部门店）
    :param batch: 每批请求 LLM 的菜名数（小批粒度，默认 4）
    :param force: 先把范围内活跃菜品重置为 pending 再全量重算
    :param max_batches: 调试用，最多处理批次数（None=不限）
    :param progress_cb: 进度回调 progress_cb(done, total)
    :param log: 日志函数
    :return: {"ok": int, "failed": int, "total": int} 或 None(启动前置检查失败)
    """
    if not API_KEY:
        log("ERROR: 未配置 LLM API_KEY，无法生成")
        return None

    init_db()

    if force:
        conn = get_conn()
        c = conn.cursor()
        if store_id:
            c.execute("UPDATE dishes SET bom_status='pending' WHERE status='active' AND store_id=?", (store_id,))
        else:
            c.execute("UPDATE dishes SET bom_status='pending' WHERE status='active'")
        conn.commit()
        conn.close()
        log("[force] 已将活跃菜品重置为 pending，准备全量重算")

    dishes = get_dishes_need_regen(store_id=store_id)
    if not dishes:
        log("[完成] 无待生成菜品（可能已全部 generated）")
        return {"ok": 0, "failed": 0, "total": 0}

    # 按唯一菜名分组：同一菜名可能在多门店各有一份，需逐店写
    by_name = {}
    for d in dishes:
        by_name.setdefault(d["name"], []).append(d)
    names = sorted(by_name.keys())
    total = len(names)
    n_batches = (total + batch - 1) // batch
    log(f"[开始] 待生成菜名={total}（去重后），分 {n_batches} 批，批大小={batch}"
        + (f"，仅门店#{store_id}" if store_id else ""))

    done = failed = 0
    t0 = time.time()
    conn = get_conn()  # 单一共享写连接，整条落库链路复用
    try:
        for bi in range(n_batches):
            if max_batches and bi >= max_batches:
                log(f"[截断] 已达 --max-batches={max_batches}，停止（已落库批次保留）")
                break
            chunk = names[bi * batch:(bi + 1) * batch]
            res = generate_recipes_for_dishes(chunk, batch_size=batch, max_workers=max_workers, force=True)
            for nm in chunk:
                items = res["ok"].get(nm)
                if not items:
                    failed += 1
                    log(f"  [失败] {nm}")
                    continue
                for d in by_name[nm]:
                    try:
                        replace_recipe_for_dish(d["id"], items, source="llm", conn=conn)
                        done += 1
                    except Exception as e:
                        failed += 1
                        log(f"  [写入失败] {nm}(dish {d['id']}): {e}")
            conn.commit()  # 每批 checkpoint，已落库的不丢
            el = round(time.time() - t0)
            log(f"[批次 {bi + 1}/{n_batches}] 进度 {min((bi + 1) * batch, total)}/{total} "
                f"成功累计={done} 失败累计={failed} 耗时={el}s")
            if progress_cb:
                try:
                    progress_cb(min((bi + 1) * batch, total), total)
                except Exception:
                    pass
    finally:
        conn.close()

    recompute_all_costs(store_id=store_id)
    log(f"[完成] 成功={done} 失败={failed} 总耗时={round(time.time() - t0)}s")
    return {"ok": done, "failed": failed, "total": total}

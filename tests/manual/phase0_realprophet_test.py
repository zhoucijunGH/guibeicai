"""Phase 0 #1 真实 Prophet 训练验证（cmdstan 已安装）。

本地 Windows 中文 locale 下，cmdstanpy 1.3.0 调用 where.exe tbb.dll 时按 UTF-8 解码
GBK 输出会 UnicodeDecodeError。此处仅在【测试脚手架内】monkeypatch do_command 加
errors='replace' 容错，用于本地验证 #1 代码正确性。生产（Linux）无 where.exe，不受影响。
"""
import io
import os
import sys
import subprocess

# ---------- 打补丁：修复 cmdstanpy 在 GBK locale 下的解码崩溃 ----------
import cmdstanpy.utils.command as _cmdcmd
from cmdstanpy.utils.filesystem import pushd as _pushd
from cmdstanpy.utils.logging import get_logger as _get_logger


def _patched_do_command(cmd, cwd=None, *, fd_out=sys.stdout, pbar=None):
    _get_logger().debug('cmd: %s\ncwd: %s', ' '.join(cmd), cwd)
    try:
        with _pushd(cwd if cwd is not None else '.'):
            proc = subprocess.Popen(
                cmd, bufsize=1, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=os.environ, encoding='utf-8', errors='replace',  # FIX
            )
            while proc.poll() is None:
                if proc.stdout is not None:
                    line = proc.stdout.readline()
                    if fd_out is not None:
                        fd_out.write(line)
                    if pbar is not None:
                        pbar(line.strip())
            stdout, _ = proc.communicate()
            if stdout and len(stdout) > 0:
                if fd_out is not None:
                    fd_out.write(stdout)
                if pbar is not None:
                    pbar(stdout.strip())
            if proc.returncode != 0:
                serror = ''
                try:
                    serror = os.strerror(proc.returncode)
                except (ArithmeticError, ValueError):
                    pass
                raise RuntimeError("Command {}\n\texited with code '{}' {}".format(cmd, proc.returncode, serror))
    except OSError as e:
        raise RuntimeError('Command: {}\nfailed with error {}\n'.format(cmd, str(e))) from e


_cmdcmd.do_command = _patched_do_command
import cmdstanpy.model as _cmdm
_cmdm.do_command = _patched_do_command
# --------------------------------------------------------------------------

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
from guibeicai.web import _forecast

RNG = np.random.default_rng(42)
PASS, FAIL = 0, 0

def build_hist(cols):
    n = 60
    dates = pd.date_range("2025-06-01", periods=n, freq="D")
    base = 20 + 5 * np.sin(np.arange(n) / 7.0 * 2 * np.pi)
    y = base + RNG.normal(0, 1.5, n)
    if cols == "new":
        return pd.DataFrame({"sale_date": dates, "qty": y, "unit_price": 12.0})
    return pd.DataFrame({"日期": dates, "销量": y})

def build_weather(hist_dates, periods):
    all_dates = list(hist_dates) + list(pd.date_range(hist_dates[-1] + pd.Timedelta(days=1), periods=periods, freq="D"))
    n = len(all_dates)
    return pd.DataFrame({"ds": all_dates, "temp_mean": 20 + RNG.normal(0, 3, n), "rain_sum": np.abs(RNG.normal(0, 2, n))})

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"[PASS] {name} {extra}")
    else:
        FAIL += 1; print(f"[FAIL] {name} {extra}")

print("=== 真实 Prophet 训练（#1 验证，cmdstan 已就绪）===")
_periods = 7

histA = build_hist("new")
wA = build_weather(histA["sale_date"].tolist(), _periods)
try:
    fcA = _forecast.prophet_predict_for_dish(histA, wA, _periods)
    check("A-1 无异常返回", True)
    check("A-2 返回行数==periods", len(fcA) == _periods, f"({len(fcA)} vs {_periods})")
    check("A-3 yhat 无 NaN", not fcA["yhat"].isna().any())
    check("A-5 预测值合理(>0)", (fcA["yhat"] > 0).all(), f"min={fcA['yhat'].min():.2f}")
    print(f"       A 未来 7 日预测: {[round(v,1) for v in fcA['yhat'].tolist()]}")
except Exception as e:
    check("A-1 无异常返回", False, f"-> {type(e).__name__}: {str(e)[:200]}")

histB = build_hist("old")
wB = build_weather(histB["日期"].tolist(), _periods)
try:
    fcB = _forecast.prophet_predict_for_dish(histB, wB, _periods)
    check("B-1 旧列名归一化无异常", True)
    check("B-2 返回行数==periods", len(fcB) == _periods, f"({len(fcB)} vs {_periods})")
    check("B-3 yhat 无 NaN", not fcB["yhat"].isna().any())
    print(f"       B 未来 7 日预测: {[round(v,1) for v in fcB['yhat'].tolist()]}")
except Exception as e:
    check("B-1 旧列名归一化无异常", False, f"-> {type(e).__name__}: {str(e)[:200]}")

histC = build_hist("new")
try:
    fcC = _forecast.prophet_predict_for_dish(histC, pd.DataFrame(columns=["ds","temp_mean","rain_sum"]), _periods)
    check("C-1 天气缺失兜底不崩", True)
    check("C-2 兜底返回 periods 行", len(fcC) == _periods, f"({len(fcC)})")
    check("C-3 兜底 yhat 无 NaN", not fcC["yhat"].isna().any())
except Exception as e:
    check("C-1 天气缺失兜底不崩", False, f"-> {type(e).__name__}: {str(e)[:200]}")

tiny = pd.DataFrame({"sale_date": [pd.Timestamp("2025-06-01")], "qty": [10.0]})
fcD = _forecast.prophet_predict_for_dish(tiny, None, _periods)
check("D-1 历史过少返回空 DF", len(fcD) == 0, f"(len={len(fcD)})")

print(f"\n=== 结果: PASS={PASS} FAIL={FAIL} ===")
sys.exit(0 if FAIL == 0 else 1)

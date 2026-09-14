"""时间序列重建。

把 Sentinel-2 不规则、带云洞的观测序列，变成可用于物候提取和建模的规则曲线。

三步：
  1. to_regular_grid  不规则观测 -> 固定步长网格（缺口只做有限插值，不无限外推）
  2. smooth_savgol    Savitzky-Golay 多项式平滑（保形、不引入相移）
  3. fit_double_logistic  可选的双逻辑斯蒂拟合（参数化，便于求导定位物候期）
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit
from scipy.signal import savgol_filter


def to_regular_grid(
    df: pd.DataFrame,
    value_col: str = "NDVI",
    date_col: str = "date",
    step_days: int = 5,
    max_gap_days: int = 30,
) -> pd.DataFrame:
    """把不规则观测插值到固定步长网格。

    超过 max_gap_days 的空洞保持 NaN——那段时间真的没有有效观测，
    用长期均值硬填会凭空造出物候信号，宁缺毋滥。
    """
    s = df[[date_col, value_col]].copy()
    s[date_col] = pd.to_datetime(s[date_col])
    s[value_col] = pd.to_numeric(s[value_col], errors="coerce")
    s = s.dropna(subset=[value_col])
    if s.empty:
        return pd.DataFrame(columns=[date_col, value_col, "observed"])

    s = s.groupby(date_col, as_index=False)[value_col].mean().sort_values(date_col)

    grid = pd.date_range(s[date_col].min(), s[date_col].max(), freq=str(step_days) + "D")
    out = pd.DataFrame({date_col: grid}).merge(s, on=date_col, how="left")
    out["observed"] = out[value_col].notna()

    limit = max(1, int(round(max_gap_days / step_days)))
    out[value_col] = out[value_col].interpolate(
        method="linear", limit=limit, limit_area="inside"
    )
    return out.reset_index(drop=True)


def smooth_savgol(values, window: int = 7, polyorder: int = 2) -> np.ndarray:
    """对可能含 NaN 的序列做 S-G 平滑。

    NaN 处先线性内插以便卷积，平滑后把原本缺失的位置还原成 NaN，
    避免把插值出来的值当成观测。
    """
    v = pd.Series(np.asarray(values, dtype=float))
    if v.empty:
        return v.to_numpy()
    nan_mask = v.isna()
    if nan_mask.all():
        return v.to_numpy()

    filled = v.interpolate(limit_direction="both")
    n = len(filled)

    w = int(window)
    if w % 2 == 0:
        w += 1
    if w > n:
        w = n if n % 2 == 1 else n - 1
    if w < 3:
        return v.to_numpy()

    p = int(polyorder)
    if p >= w:
        p = w - 1
    if p < 1:
        return v.to_numpy()

    smoothed = savgol_filter(filled.to_numpy(), window_length=w, polyorder=p, mode="interp")
    out = pd.Series(smoothed, index=v.index)
    out[nan_mask] = np.nan
    return out.to_numpy()


def smooth_dataframe(
    df: pd.DataFrame, value_col: str = "NDVI", window: int = 7, polyorder: int = 2
) -> pd.DataFrame:
    out = df.copy()
    out[value_col + "_smooth"] = smooth_savgol(out[value_col].to_numpy(), window, polyorder)
    return out


def _double_logistic(t, mn, mx, m_s, s, m_a, a):
    """Fisher / TIMESAT 常用的双逻辑斯蒂生长曲线。

    前一项描述返青（上升），后一项描述衰老（下降）。
    """
    return mn + (mx - mn) * (
        1.0 / (1.0 + np.exp(-m_s * (t - s))) - 1.0 / (1.0 + np.exp(-m_a * (t - a)))
    )


@dataclass
class DoubleLogisticFit:
    """双逻辑斯蒂拟合结果，t 以「距 t0 的天数」计。"""

    t0: pd.Timestamp
    params: np.ndarray
    success: bool
    rmse: float

    def predict_days(self, t_days) -> np.ndarray:
        return _double_logistic(np.asarray(t_days, dtype=float), *self.params)

    def predict(self, dates) -> np.ndarray:
        d = pd.to_datetime(pd.Series(dates))
        t = (d - self.t0).dt.total_seconds().to_numpy() / 86400.0
        return self.predict_days(t)

    def curve(self, step_days: int = 1) -> pd.DataFrame:
        t = np.arange(0.0, float(self.params[4]) + 30.0, float(step_days))
        return pd.DataFrame({
            "date": self.t0 + pd.to_timedelta(t, unit="D"),
            "fitted": self.predict_days(t),
        })


def fit_double_logistic(
    dates, values, maxfev: int = 20000
) -> DoubleLogisticFit | None:
    """拟合双逻辑斯蒂曲线。观测太少或拟合不收敛时返回 None。"""
    d = pd.to_datetime(pd.Series(list(dates))).reset_index(drop=True)
    v = pd.Series(np.asarray(values, dtype=float)).reset_index(drop=True)
    ok = v.notna() & d.notna()
    d, v = d[ok].reset_index(drop=True), v[ok].reset_index(drop=True)

    if len(v) < 10 or v.max() - v.min() < 1e-3:
        return None

    t0 = d.iloc[0]
    t = (d - t0).dt.total_seconds().to_numpy() / 86400.0
    y = v.to_numpy()

    vmin, vmax = float(np.min(y)), float(np.max(y))
    i_max = int(np.argmax(y))
    # 用 25% / 75% 分位点给个像样的初值，否则 curve_fit 很容易跑飞
    s0 = float(t[i_max] * 0.4) if i_max > 0 else float(t[-1] * 0.3)
    a0 = float(t[i_max] + (t[-1] - t[i_max]) * 0.6)

    p0 = [vmin, vmax, 0.15, max(s0, 1.0), 0.15, max(a0, s0 + 1.0)]
    bounds = (
        [vmin - 0.2, vmax - 0.2, 0.01, t[0], 0.01, t[0]],
        [vmin + 0.2, vmax + 0.3, 1.00, t[-1], 1.00, t[-1] + 60.0],
    )

    try:
        params, _ = curve_fit(
            _double_logistic, t, y, p0=p0, bounds=bounds, maxfev=maxfev
        )
    except (RuntimeError, ValueError):
        return None

    resid = y - _double_logistic(t, *params)
    return DoubleLogisticFit(
        t0=t0, params=params, success=True, rmse=float(np.sqrt(np.nanmean(resid ** 2)))
    )

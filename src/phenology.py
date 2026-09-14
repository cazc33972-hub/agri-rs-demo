"""物候参数提取：TIMESAT 式动态阈值法。

为什么用动态阈值而不是固定阈值（比如 NDVI > 0.5）：
不同地块的土壤背景、作物种类、冠层结构不同，NDVI 的绝对水平差异很大，
固定阈值会把贫瘠地块整年判成"未返青"。动态阈值以地块自身的
NDVI_min / NDVI_max 为基准，跨地块可比。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:                      # numpy >= 2.0
    from numpy import trapezoid as _trapz
except ImportError:       # numpy 1.x
    from numpy import trapz as _trapz

PHENO_KEYS = ["SOS", "POS", "EOS", "LOS", "NDVI_min", "NDVI_max",
              "amplitude", "NDVI_integral", "is_crop"]


def _crossing_date(dates, values, level, rising: bool):
    """线性插值求曲线与 level 的交点日期，比取最近观测精度高一个量级。"""
    v = np.asarray(values, dtype=float)
    for i in range(1, len(v)):
        a, b = v[i - 1], v[i]
        if np.isnan(a) or np.isnan(b):
            continue
        crossed = (a < level <= b) if rising else (a >= level > b)
        if crossed and b != a:
            frac = (level - a) / (b - a)
            d0 = pd.Timestamp(dates[i - 1])
            d1 = pd.Timestamp(dates[i])
            return d0 + (d1 - d0) * float(frac)
    return pd.NaT


def extract_phenology(
    dates,
    values,
    step_days: int = 5,
    sos_ratio: float = 0.2,
    eos_ratio: float = 0.2,
    min_amplitude: float = 0.15,
) -> dict:
    """从一个生长季的 NDVI 曲线提取物候参数。

    返回字段：
      SOS 返青期, POS 峰值期, EOS 成熟/衰老期, LOS 生长季长度(天),
      NDVI_min/max, amplitude 振幅, NDVI_integral 生长季累积(NDVI·天),
      is_crop 是否判定为有作物的像元。
    """
    empty = {k: np.nan for k in PHENO_KEYS}
    empty["is_crop"] = False

    d = pd.to_datetime(pd.Series(list(dates))).reset_index(drop=True)
    v = pd.Series(np.asarray(values, dtype=float)).reset_index(drop=True)
    ok = v.notna() & d.notna()
    d, v = d[ok].reset_index(drop=True), v[ok].reset_index(drop=True)

    if len(v) < 8:
        return empty

    vmin = float(v.min())
    vmax = float(v.max())
    amp = vmax - vmin
    if amp < min_amplitude:
        # 振幅太小：裸地、水体、常绿林地，或整年被云污染
        empty["NDVI_min"], empty["NDVI_max"], empty["amplitude"] = vmin, vmax, amp
        return empty

    pos_idx = int(np.argmax(v.to_numpy()))
    pos_date = pd.Timestamp(d.iloc[pos_idx])

    thr_sos = vmin + sos_ratio * amp
    thr_eos = vmin + eos_ratio * amp

    sos = pd.NaT
    if pos_idx >= 1:
        sos = _crossing_date(d.iloc[: pos_idx + 1], v.iloc[: pos_idx + 1].to_numpy(),
                             thr_sos, rising=True)

    eos = pd.NaT
    if pos_idx < len(v) - 1:
        # 从峰值往后找首次跌回阈值的位置（单季曲线的下降沿是单调的）
        tail_d = d.iloc[pos_idx:].reset_index(drop=True)
        tail_v = v.iloc[pos_idx:].to_numpy()
        eos = _crossing_date(tail_d, tail_v, thr_eos, rising=False)

    los = np.nan
    if pd.notna(sos) and pd.notna(eos):
        # 用真实天数差而不是 .days（后者会截断掉插值得到的小数天）
        los = float((pd.Timestamp(eos) - pd.Timestamp(sos)).total_seconds() / 86400.0)

    integral = np.nan
    if pd.notna(sos) and pd.notna(eos):
        mask = (d >= pd.Timestamp(sos)) & (d <= pd.Timestamp(eos))
        vals = v[mask]
        if len(vals) >= 2:
            # 梯形积分，单位 NDVI·天，近似生长季累积光合能力
            integral = float(_trapz(vals.to_numpy() - vmin,
                                      dx=float(step_days)))

    return {
        "SOS": sos,
        "POS": pos_date,
        "EOS": eos,
        "LOS": los,
        "NDVI_min": vmin,
        "NDVI_max": vmax,
        "amplitude": amp,
        "NDVI_integral": integral,
        "is_crop": True,
    }


def doy_relative(ts, year: int) -> float:
    """相对「生长年 1 月 1 日」的天数，1 月 1 日 = 1。

    冬作物的物候期可能落在上一年的秋冬，直接取 dayofyear 会让
    10 月(=300) 和 3 月(=60) 在数值上差 240 天，跨年比较完全错乱。
    用相对天数就自然得到 -92 这样的负值，排序和建图都正确。
    """
    if ts is None or pd.isna(ts):
        return float("nan")
    origin = pd.Timestamp(year=int(year), month=1, day=1)
    return float((pd.Timestamp(ts) - origin).days + 1)


def sos_doy(pheno: dict, year: int) -> float:
    """SOS 的相对天数，便于跨年比较和建图。"""
    return doy_relative(pheno.get("SOS"), year)

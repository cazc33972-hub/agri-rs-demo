"""灌溉决策：FAO-56 双作物系数法 + 根区土壤水分平衡。

完整链路：
    ET0（纯气象，跟作物无关）
      x  Kc（作物系数，由 NDVI 遥感反演）
      =  ETc（作物实际需水）
    逐日根区水分平衡 -> 根区亏缺量 Dr
    Dr 触及 RAW（管理允许亏缺）-> 出灌溉处方："哪天灌、灌多少 mm"

这一步是整个 demo 的落脚点：前面所有遥感处理，最后都要变成一句
农民能执行的话，否则就只是"好看的热力图"。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Cfg


def kc_from_ndvi(ndvi, a: float = 1.457, b: float = -0.1725,
                 kc_min: float = 0.15, kc_max: float = 1.20):
    """由 NDVI 反演作物系数 Kc。

    Kc = a * NDVI + b 是 Er-Raki 等在半干旱区建立的经验关系。
    注意：a、b 强烈依赖作物类型、气候区和土壤背景，生产使用前
    必须用本地实测（蒸渗仪 / 涡度相关 / 水量平衡试验）重新标定。
    demo 里先用文献值，报告里要写明这一条假设。
    """
    ndvi = np.asarray(ndvi, dtype=float)
    return np.clip(a * ndvi + b, kc_min, kc_max)


def taw_mm(field_capacity: float, wilting_point: float, root_zone_mm: float) -> float:
    """总可用水量 TAW（mm）。

    TAW = 1000 * (theta_fc - theta_wp) * Zr，其中 Zr 单位为 m。
    把 Zr 换算成 mm 后，1000 与 /1000 相消，直接就是 (fc - wp) * Zr_mm。
    """
    return max(0.0, float(field_capacity - wilting_point) * float(root_zone_mm))


def kc_from_stage(dates, sos, pos, eos,
                  kc_ini: float = 0.30, kc_mid: float = 1.15, kc_end: float = 0.35):
    """FAO-56 分段线性 Kc 曲线，作为 NDVI 反演不可用时的兜底方案。

    四个阶段：初期(ini) -> 快速发育 -> 中期(mid) -> 后期(end)。
    pos 同时作为中期起点和终点（中期的典型时长按 pos~eos 的一半近似）。
    """
    d = pd.to_datetime(pd.Series(list(dates)))
    sos = pd.Timestamp(sos) if sos is not None and pd.notna(sos) else None
    pos = pd.Timestamp(pos) if pos is not None and pd.notna(pos) else None
    eos = pd.Timestamp(eos) if eos is not None and pd.notna(eos) else None
    if sos is None or pos is None or eos is None or not (sos < pos < eos):
        return np.full(len(d), kc_mid)

    mid_end = pos + (eos - pos) * 0.4
    x = np.array([sos, pos, mid_end, eos], dtype="datetime64[ns]").astype(float)
    y = np.array([kc_ini, kc_mid, kc_mid, kc_end], dtype=float)
    xp = d.to_numpy().astype("datetime64[ns]").astype(float)
    return np.interp(xp, x, y)


def build_daily_driver(ts: pd.DataFrame, weather: pd.DataFrame, cfg: Cfg,
                       plot_id: str | None = None) -> pd.DataFrame:
    """把「地块 NDVI 序列」和「日气象」对齐成逐日驱动表。

    做法：NDVI 观测太稀疏（5 天以上才一景），先按天线性内插成日 NDVI，
    再由它逐日算 Kc，最后乘 ET0 得到 ETc。非生长季用 kc_min 兜底。
    """
    sub = ts if plot_id is None else ts[ts["plot_id"] == plot_id]
    sub = sub.dropna(subset=["NDVI"])[["date", "NDVI"]].sort_values("date")
    w = weather.sort_values("date").copy()
    if "et0" not in w.columns:
        raise ValueError(
            "气象表里没有 et0 列。请先用 src.et0.load_weather_csv() 或 "
            "src.et0.et0_from_frame() 把原始气象算成 ET0。"
        )
    if sub.empty:
        w["ndvi"] = np.nan
    else:
        s = sub.set_index("date")["NDVI"]
        s = s[~s.index.duplicated(keep="last")]
        # 只在内插范围内取值，范围外用 NaN，再由 kc_min 兜底
        w["ndvi"] = s.reindex(pd.DatetimeIndex(w["date"])).interpolate(
            method="time", limit_area="inside"
        ).to_numpy()

    # 记录哪些日子真的落在生长季内（NDVI 有内插值）。
    # 生长季之外 Kc 落到下限，但那时地里没有作物，不该触发灌溉决策。
    w["in_season"] = w["ndvi"].notna()

    kc = kc_from_ndvi(
        w["ndvi"].fillna(0.0).to_numpy(),
        a=float(cfg.get("irrigation.kc_ndvi_a", 1.457)),
        b=float(cfg.get("irrigation.kc_ndvi_b", -0.1725)),
        kc_min=float(cfg.get("irrigation.kc_min", 0.15)),
        kc_max=float(cfg.get("irrigation.kc_max", 1.20)),
    )
    # NDVI 缺失的日期（非生长季 / 长期云污染）直接用最低 Kc，不硬造作物
    kc[w["ndvi"].isna().to_numpy()] = float(cfg.get("irrigation.kc_min", 0.15))
    w["kc"] = kc
    w["etc"] = w["kc"] * w["et0"]
    if "rain" not in w.columns:
        w["rain"] = 0.0
    return w


def simulate_water_balance(daily: pd.DataFrame, cfg: Cfg,
                           dr0: float = 0.0) -> pd.DataFrame:
    """逐日根区水分平衡（FAO-56 式 85）。

        Dr,i = Dr,i-1 - (P - RO) - I - CR + ETc,i + DPi

    demo 里简化 RO（地表径流）= CR（毛管上升）= DP（深层渗漏）= 0，
    并把 Dr 截断在 [0, TAW] 内，等价于"灌多了就渗漏掉、干不到凋萎点以下"。

    灌溉触发规则：当天开始时的亏缺 Dr >= RAW 就补到田间持水量（Dr 归零）。
    这对应"早上看一眼土壤干不干，干了就灌"，比事后补算更贴近实际决策。

    另外只在 in_season（生长季内）才允许触发灌溉：
    越冬期裸地即使亏缺超标也不该灌水，否则处方里会冒出一堆冬天的灌溉事件。
    """
    fc = float(cfg.get("irrigation.field_capacity", 0.30))
    wp = float(cfg.get("irrigation.wilting_point", 0.12))
    zr = float(cfg.get("irrigation.root_zone_depth_mm", 600))
    mad = float(cfg.get("irrigation.mad", 0.50))
    eff = float(cfg.get("irrigation.irrigation_efficiency", 0.85))

    taw = taw_mm(fc, wp, zr)
    raw = mad * taw

    d = daily.sort_values("date").reset_index(drop=True)
    dr = float(dr0)
    drs, nets, gross, stressed = [], [], [], []

    has_season = "in_season" in d.columns

    for i in range(len(d)):
        etc = float(d.at[i, "etc"])
        rain = float(d.at[i, "rain"]) if pd.notna(d.at[i, "rain"]) else 0.0
        in_season = bool(d.at[i, "in_season"]) if has_season else True

        dr = dr - rain                      # 降雨补充根区
        net = 0.0
        if dr >= raw and in_season:         # 触及管理允许亏缺 -> 补到田间持水量
            net = dr
            dr -= net
        dr = dr + etc                       # 蒸散消耗
        over = max(0.0, dr - taw)           # 超出 TAW 的部分即深层渗漏
        dr = float(np.clip(dr, 0.0, taw))

        drs.append(dr)
        nets.append(net)
        gross.append(net / eff if net > 0 else 0.0)   # 毛灌溉量 = 净量 / 利用系数
        stressed.append(bool(dr >= raw))

    out = d.copy()
    out["dr_mm"] = drs
    out["net_irrigation_mm"] = nets
    out["irrigation_gross_mm"] = gross
    out["taw_mm"] = taw
    out["raw_mm"] = raw
    out["dp_mm"] = 0.0
    out["stressed"] = stressed
    out["dr_ratio"] = out["dr_mm"] / taw if taw > 0 else 0.0
    if has_season:
        out["in_season"] = d["in_season"].to_numpy()
    return out


def build_plan(balance: pd.DataFrame, min_depth_mm: float = 5.0) -> pd.DataFrame:
    """从水分平衡结果里抽出灌溉事件，形成可执行的处方式清单。"""
    if balance.empty:
        return pd.DataFrame(columns=["date", "net_irrigation_mm",
                                     "irrigation_gross_mm", "dr_before_mm"])
    rows = []
    prev_dr = 0.0
    for _, r in balance.iterrows():
        if r["net_irrigation_mm"] >= min_depth_mm:
            rows.append({
                "date": r["date"],
                "net_irrigation_mm": round(float(r["net_irrigation_mm"]), 1),
                "irrigation_gross_mm": round(float(r["irrigation_gross_mm"]), 1),
                "dr_before_mm": round(float(r["net_irrigation_mm"]), 1),
                "kc": round(float(r["kc"]), 3),
                "et0": round(float(r["et0"]), 2),
                "etc": round(float(r["etc"]), 2),
                "in_season": bool(r["in_season"]) if "in_season" in balance.columns else True,
            })
        prev_dr = float(r["dr_mm"])
    return pd.DataFrame(rows)


def forecast_prescription(balance: pd.DataFrame, cfg: Cfg,
                          rain_forecast: list[float] | None = None) -> dict:
    """基于当前亏缺量，给出"未来若干天内要不要灌、灌多少"的建议。

    预报期内 ETc 用最近 7 天的均值外推（demo 的简化做法；
    生产上应该接数值天气预报的 ET0 预报）。
    """
    if balance.empty:
        return {"needs_irrigation": False, "reason": "没有可用的水分平衡结果"}

    horizon = int(cfg.get("irrigation.forecast_days", 7))
    fc = float(cfg.get("irrigation.field_capacity", 0.30))
    wp = float(cfg.get("irrigation.wilting_point", 0.12))
    zr = float(cfg.get("irrigation.root_zone_depth_mm", 600))
    mad = float(cfg.get("irrigation.mad", 0.50))
    eff = float(cfg.get("irrigation.irrigation_efficiency", 0.85))

    taw = taw_mm(fc, wp, zr)
    raw = mad * taw

    recent = balance.tail(7)
    etc_fore = float(recent["etc"].mean()) if len(recent) else 3.0
    rains = list(rain_forecast) if rain_forecast else [0.0] * horizon

    # 循环里 k 就是"距今第几天"，k=0 是今天：
    # 每次迭代先看当天早晨的亏缺，再扣掉当天的耗水。
    dr = float(balance["dr_mm"].iloc[-1])
    for k in range(horizon):
        if dr >= raw:
            return {
                "needs_irrigation": True,
                "days_until": int(k),
                "net_irrigation_mm": round(dr, 1),
                "gross_irrigation_mm": round(dr / eff, 1),
                "current_dr_mm": round(float(balance["dr_mm"].iloc[-1]), 1),
                "taw_mm": round(taw, 1),
                "raw_mm": round(raw, 1),
                "etc_forecast_mm_per_day": round(etc_fore, 2),
                "reason": ("根区亏缺已达到管理允许亏缺，建议立即灌溉"
                           if k == 0 else
                           "根区亏缺将在 " + str(k) + " 天后触及管理允许亏缺"),
            }
        dr = dr - (rains[k] if k < len(rains) else 0.0) + etc_fore

    # 预报窗口内不触发：剩余可用天数要加上已经推演掉的 horizon 天
    days_left = horizon + (raw - dr) / etc_fore if etc_fore > 0 else np.inf
    return {
        "needs_irrigation": False,
        "days_until": None,
        "net_irrigation_mm": 0.0,
        "gross_irrigation_mm": 0.0,
        "current_dr_mm": round(float(balance["dr_mm"].iloc[-1]), 1),
        "taw_mm": round(taw, 1),
        "raw_mm": round(raw, 1),
        "etc_forecast_mm_per_day": round(etc_fore, 2),
        "days_of_water_left": None if not np.isfinite(days_left) else round(float(days_left), 1),
        "reason": "未来 " + str(horizon) + " 天内不需要灌溉",
    }

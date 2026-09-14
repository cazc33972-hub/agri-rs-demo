"""离线自检：验证不依赖 scipy / sklearn 的模块是否符合物理与数值预期。

用法（项目根目录下）:
    python tools/selfcheck.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.et0 import et0_daily, et0_from_frame
from src.io_utils import load_timeseries
from src.irrigation import (build_daily_driver, build_plan, forecast_prescription,
                            kc_from_ndvi, simulate_water_balance, taw_mm)
from src.phenology import doy_relative, extract_phenology

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append(bool(cond))
    suffix = "" if extra == "" else "   " + str(extra)
    print(("PASS  " if cond else "FAIL  ") + name + suffix)


def main() -> int:
    cfg = load_config("config/config.yaml")

    # ---------- 1) 时序读取 ----------
    ts = load_timeseries("data/raw/plot_timeseries.csv")
    check("时序载入", len(ts) > 500, ts.shape)
    check("地块数为 12", ts["plot_id"].nunique() == 12, ts["plot_id"].nunique())
    check("NDVI 落在 [-1,1]", bool(ts["NDVI"].between(-1, 1).all()))
    check("时间已解析为 datetime", str(ts["date"].dtype).startswith("datetime64"))

    # ---------- 2) 物候提取 ----------
    one = ts[(ts["plot_id"] == "P001") & (ts["date"].dt.year == 2023)]
    ph = extract_phenology(one["date"], one["NDVI"], step_days=5)
    check("SOS 非空", pd.notna(ph["SOS"]), ph["SOS"])
    check("EOS 非空", pd.notna(ph["EOS"]), ph["EOS"])
    check("SOS < POS < EOS",
          pd.Timestamp(ph["SOS"]) < pd.Timestamp(ph["POS"]) < pd.Timestamp(ph["EOS"]))
    check("LOS 落在 40~150 天", 40 <= (ph["LOS"] or 0) <= 150, ph["LOS"])
    check("生长季积分 > 0", (ph["NDVI_integral"] or -1) > 0, round(ph["NDVI_integral"], 1))
    check("判定为作物", ph["is_crop"] is True)
    check("doy_relative 跨年可为负",
          doy_relative(pd.Timestamp("2021-10-01"), 2022) < 0,
          doy_relative(pd.Timestamp("2021-10-01"), 2022))

    flat = extract_phenology(one["date"], np.full(len(one), 0.3), step_days=5)
    check("平坦序列判为非作物", flat["is_crop"] is False)

    # ---------- 3) ET0 ----------
    wx = pd.read_csv("data/raw/weather_synthetic.csv", parse_dates=["date"],
                     encoding="utf-8-sig")
    et0 = et0_from_frame(wx, lat=36.3015, elev_m=50)
    wx["et0"] = et0          # 后面 build_daily_driver 需要
    check("ET0 无 NaN", bool(et0.notna().all()))
    check("ET0 年均落在 2~5 mm/d", 2.0 <= float(et0.mean()) <= 5.0,
          round(float(et0.mean()), 2))
    summer = float(et0[wx["date"].dt.month == 6].mean())
    winter = float(et0[wx["date"].dt.month == 1].mean())
    check("夏季 ET0 > 冬季 ET0", summer > winter,
          "夏 " + str(round(summer, 2)) + " / 冬 " + str(round(winter, 2)))
    check("ET0 全部非负", bool((et0 >= 0).all()))

    # FAO-56 手册量级的参照算例：Tmax=25 Tmin=15 RH=60 u2=2 Rs=20 海拔100m
    ref = float(et0_daily(25, 15, 60, 2.0, 20.0, 30.0, 100))
    check("FAO-56 参照算例落在 3.5~4.5 mm/d", 3.5 <= ref <= 4.5, round(ref, 3))

    # ---------- 4) Kc 与 TAW ----------
    # 1.457 * 0.8 - 0.1725 = 0.9931
    check("Kc(NDVI=0.8) 与经验公式一致",
          abs(float(kc_from_ndvi(0.8)) - 0.9931) < 0.01, round(float(kc_from_ndvi(0.8)), 4))
    check("Kc 单调递增", float(kc_from_ndvi(0.7)) < float(kc_from_ndvi(0.8)))
    check("Kc(NDVI=0.1) 不低于下限",
          float(kc_from_ndvi(0.1)) >= 0.15, round(float(kc_from_ndvi(0.1)), 3))
    check("TAW = (0.30-0.12) x 600mm = 108mm",
          abs(taw_mm(0.30, 0.12, 600) - 108.0) < 1e-9, taw_mm(0.30, 0.12, 600))

    # ---------- 5) 水分平衡与灌溉处方 ----------
    try:
        build_daily_driver(ts, wx.drop(columns=["et0"]), cfg, plot_id="P001")
        check("缺 et0 列时报错", False)
    except ValueError:
        check("缺 et0 列时抛 ValueError", True)

    driver = build_daily_driver(ts, wx, cfg, plot_id="P001")
    check("驱动表行数 = 气象天数", len(driver) == len(wx), len(driver))
    check("ETc = Kc x ET0", bool(np.allclose(driver["etc"], driver["kc"] * driver["et0"])))
    check("ETc 全部非负", bool((driver["etc"] >= 0).all()))

    bal = simulate_water_balance(driver, cfg)
    taw0 = float(bal["taw_mm"].iloc[0])
    check("Dr 始终落在 [0, TAW]",
          bool(bal["dr_mm"].between(0.0, taw0 + 1e-6).all()),
          str(round(float(bal["dr_mm"].min()), 2)) + " ~ "
          + str(round(float(bal["dr_mm"].max()), 2)))
    check("RAW = MAD x TAW", abs(float(bal["raw_mm"].iloc[0]) - 0.5 * taw0) < 1e-6)
    check("灌溉量非负", bool((bal["net_irrigation_mm"] >= 0).all()))

    plan = build_plan(bal)
    print("      灌溉事件 " + str(len(plan)) + " 次, 累计净灌溉量 "
          + str(round(float(plan["net_irrigation_mm"].sum()), 1)) + " mm")
    check("驱动表带 in_season 标记", "in_season" in driver.columns)
    if not plan.empty and "in_season" in plan.columns:
        check("灌溉事件全部落在生长季内", bool(plan["in_season"].all()),
              str(int(plan["in_season"].sum())) + "/" + str(len(plan)) + " 条在生长季)")
    check("非生长季不产生灌溉",
          bool((bal.loc[~bal["in_season"], "net_irrigation_mm"] == 0).all()))

    fc = forecast_prescription(bal, cfg)
    check("处方含 needs_irrigation 字段", "needs_irrigation" in fc)
    print("      处方: " + str(fc.get("reason")) + " | 建议毛灌溉量 "
          + str(fc.get("gross_irrigation_mm")) + " mm")

    # 极端干旱（ET0 三倍 + 零降雨）必须触发灌溉
    dry = driver.copy()
    dry["rain"] = 0.0
    dry["et0"] = dry["et0"] * 3.0
    dry["etc"] = dry["kc"] * dry["et0"]
    bal_dry = simulate_water_balance(dry, cfg)
    n_irr = int((bal_dry["net_irrigation_mm"] > 0).sum())
    check("极端干旱下产生灌溉事件", n_irr > 0, str(n_irr) + " 次")

    # ---------- 6) 处方逻辑的确定性验证 ----------
    # 手工构造：TAW=108 RAW=54，今日亏缺 30mm，日耗水 4mm
    # 今天 30, 明天 34, ... 6 天后 54，正好触及 RAW
    def fake_balance(dr, etc, n=7):
        return pd.DataFrame({
            "date": pd.date_range("2024-05-01", periods=n, freq="D"),
            "etc": [etc] * n,
            "dr_mm": [dr] * n,
        })

    f1 = forecast_prescription(fake_balance(30.0, 4.0), cfg)
    check("处方：亏缺 30mm / 耗水 4mm 时应第 6 天灌溉",
          f1["needs_irrigation"] and f1["days_until"] == 6,
          "days_until=" + str(f1.get("days_until")))
    check("处方：净灌溉量 = 触及 RAW 时的亏缺 54mm",
          abs(f1["net_irrigation_mm"] - 54.0) < 1e-6, f1["net_irrigation_mm"])
    check("处方：毛灌溉量 = 净量 / 利用系数",
          abs(f1["gross_irrigation_mm"] - 54.0 / 0.85) < 0.06, f1["gross_irrigation_mm"])

    f2 = forecast_prescription(fake_balance(50.0, 4.0), cfg)
    check("处方：亏缺 50mm 时应第 1 天灌溉",
          f2["needs_irrigation"] and f2["days_until"] == 1,
          "days_until=" + str(f2.get("days_until")))

    f3 = forecast_prescription(fake_balance(5.0, 1.0), cfg)
    check("处方：水分充足时不要求灌溉", f3["needs_irrigation"] is False,
          "剩余可用天数=" + str(f3.get("days_of_water_left")))

    # 有降雨预报时应推迟灌溉
    f4 = forecast_prescription(fake_balance(30.0, 4.0), cfg,
                               rain_forecast=[10.0] * 7)
    check("处方：考虑降雨预报后推迟或取消灌溉",
          (not f4["needs_irrigation"]) or f4["days_until"] > 6,
          "days_until=" + str(f4.get("days_until")))

    n_fail = RESULTS.count(False)
    print("")
    print("共 " + str(len(RESULTS)) + " 项, 通过 "
          + str(len(RESULTS) - n_fail) + ", 失败 " + str(n_fail))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())

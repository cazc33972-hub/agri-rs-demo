"""FAO-56 Penman-Monteith 参考作物蒸散量 ET0（日尺度）。

这是灌溉决策的物理基础：ET0 只跟气象有关（与作物无关），
再乘作物系数 Kc 才得到作物需水 ETc。公式见 FAO-56 灌溉与排水手册第 6 章。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

GSC = 0.0820                 # 太阳常数 MJ m-2 min-1
SIGMA = 4.903e-9             # 斯特藩-玻尔兹曼常数 MJ K-4 m-2 day-1
POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"


def svp(t_c):
    """饱和水汽压 es(T)，kPa（FAO-56 式 11）。"""
    return 0.6108 * np.exp(17.27 * t_c / (t_c + 237.3))


def slope_svp(t_c):
    """饱和水汽压曲线斜率 delta，kPa/°C（FAO-56 式 13）。"""
    return 4098.0 * svp(t_c) / np.power(t_c + 237.3, 2)


def atm_pressure(elev_m: float):
    """大气压 P，kPa（FAO-56 式 7）。"""
    return 101.3 * np.power((293.0 - 0.0065 * elev_m) / 293.0, 5.26)


def psy_const(p_kpa):
    """湿度计常数 gamma，kPa/°C（FAO-56 式 8）。"""
    return 0.000665 * p_kpa


def ea_from_rh(tmax, tmin, rh_mean):
    """由平均相对湿度估算实际水汽压 ea，kPa（FAO-56 式 17/19）。"""
    es = (svp(tmax) + svp(tmin)) / 2.0
    return np.clip(es * np.asarray(rh_mean) / 100.0, 0.0, None)


def ra_extraterrestrial(lat_deg, doy):
    """天顶辐射 Ra，MJ m-2 day-1（FAO-56 式 21）。"""
    phi = np.radians(lat_deg)
    doy = np.asarray(doy, dtype=float)
    dr = 1.0 + 0.033 * np.cos(2.0 * np.pi * doy / 365.0)
    dec = 0.409 * np.sin(2.0 * np.pi * doy / 365.0 - 1.39)
    ws = np.arccos(np.clip(-np.tan(phi) * np.tan(dec), -1.0, 1.0))
    return (24.0 * 60.0 / np.pi) * GSC * dr * (
        ws * np.sin(phi) * np.sin(dec) + np.cos(phi) * np.cos(dec) * np.sin(ws)
    )


def net_radiation(rs, tmax, tmin, ea, ra, albedo=0.23):
    """净辐射 Rn，MJ m-2 day-1（FAO-56 式 38-40）。"""
    rns = (1.0 - albedo) * np.asarray(rs, dtype=float)
    tmax_k = np.asarray(tmax, dtype=float) + 273.16
    tmin_k = np.asarray(tmin, dtype=float) + 273.16
    rnl = (SIGMA * (np.power(tmax_k, 4) + np.power(tmin_k, 4)) / 2.0
           * (0.34 - 0.14 * np.sqrt(np.clip(ea, 0.0, None)))
           * (1.35 * np.clip(np.asarray(rs, dtype=float) / np.maximum(ra, 1e-6), 0, None) - 0.35))
    return rns - rnl


def et0_daily(tmax, tmin, rh_mean, u2, rs, ra, elev_m, g=0.0):
    """FAO-56 式 6。日尺度下土壤热通量 G 近似为 0。返回 mm/day。"""
    tmax = np.asarray(tmax, dtype=float)
    tmin = np.asarray(tmin, dtype=float)
    u2 = np.asarray(u2, dtype=float)
    rs = np.asarray(rs, dtype=float)

    t_mean = (tmax + tmin) / 2.0
    p = atm_pressure(elev_m)
    gamma = psy_const(p)
    delta = slope_svp(t_mean)
    es = (svp(tmax) + svp(tmin)) / 2.0
    ea = ea_from_rh(tmax, tmin, rh_mean)
    rn = net_radiation(rs, tmax, tmin, ea, ra)

    num = (0.408 * delta * (rn - g)
           + gamma * (900.0 / (t_mean + 273.0)) * u2 * (es - ea))
    den = delta + gamma * (1.0 + 0.34 * u2)
    et0 = num / den
    return np.clip(et0, 0.0, None)     # 负值在日尺度上没有物理意义


def et0_from_frame(df: pd.DataFrame, lat: float, elev_m: float) -> pd.Series:
    """DataFrame 版本。需要列: date, tmax, tmin, rh, u2, rs。"""
    d = pd.to_datetime(df["date"])
    ra = ra_extraterrestrial(lat, d.dt.dayofyear.to_numpy())
    vals = et0_daily(df["tmax"], df["tmin"], df["rh"], df["u2"],
                     df["rs"], ra, elev_m)
    return pd.Series(vals, index=df.index, name="et0")


def load_weather_csv(path: str | Path, lat: float, elev_m: float) -> pd.DataFrame:
    """读本地气象 CSV。

    需要列: date, tmax, tmin, rh, u2, rs，可选 rain。
    若文件里已有 et0 列就直接用，否则按 FAO-56 现算。
    """
    df = pd.read_csv(path, parse_dates=["date"], encoding="utf-8-sig")
    need = {"date", "tmax", "tmin", "rh", "u2", "rs"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError("气象 CSV 缺少列: " + ", ".join(sorted(missing)))
    if "rain" not in df.columns:
        df["rain"] = 0.0
    if "et0" not in df.columns:
        df["et0"] = et0_from_frame(df, lat, elev_m)
    return df


def fetch_nasa_power(
    lat: float,
    lon: float,
    start: str,
    end: str,
    cache_path: str | Path | None = None,
) -> pd.DataFrame:
    """从 NASA POWER 拉日气象数据（免费、无需 key），返回含 et0 的 DataFrame。

    需要的参数:
      T2M_MAX/T2M_MIN 气温极值(°C)
      RH2M            2m 相对湿度(%)
      WS2M            2m 风速(m/s) —— 已经是 2m，不需要再乘 0.748
      ALLSKY_SFC_SW_DWN 全天空短波辐射(MJ/m2/day)，直接就是 FAO-56 的 Rs
      PRECTOTCORR     校正后日降水(mm/day)，用于土壤水分平衡
    """
    cache = Path(cache_path) if cache_path else None
    if cache and cache.exists():
        return pd.read_csv(cache, parse_dates=["date"], encoding="utf-8-sig")

    import requests

    params = {
        "parameters": "T2M_MAX,T2M_MIN,RH2M,WS2M,ALLSKY_SFC_SW_DWN,PRECTOTCORR",
        "community": "AG",
        "longitude": lon,
        "latitude": lat,
        "start": start.replace("-", ""),
        "end": end.replace("-", ""),
        "format": "JSON",
    }
    resp = requests.get(POWER_URL, params=params, timeout=120)
    resp.raise_for_status()
    payload = resp.json()

    raw = payload["properties"]["parameter"]
    dates = sorted(raw["T2M_MAX"].keys())
    fill = -999.0

    df = pd.DataFrame({
        "date": pd.to_datetime(dates),
        "tmax": [raw["T2M_MAX"][d] for d in dates],
        "tmin": [raw["T2M_MIN"][d] for d in dates],
        "rh": [raw["RH2M"][d] for d in dates],
        "u2": [raw["WS2M"][d] for d in dates],
        "rs": [raw["ALLSKY_SFC_SW_DWN"][d] for d in dates],
        "rain": [raw["PRECTOTCORR"][d] for d in dates],
    })
    for c in ["tmax", "tmin", "rh", "u2", "rs", "rain"]:
        df.loc[df[c] <= fill, c] = np.nan
    df = df.dropna().reset_index(drop=True)
    df["et0"] = et0_from_frame(df, lat, float(payload["geometry"]["coordinates"][2]))

    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache, index=False, encoding="utf-8-sig")
    return df

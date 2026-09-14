"""建模：随机森林 + 分组交叉验证 + 精度评价。

为什么必须用分组交叉验证（这是整个 demo 里最容易被面试官追问的点）：
同一地块相邻年份的物候特征高度自相关，同一年的不同地块又共享气象条件。
如果用随机 KFold，训练集和验证集里会同时出现"几乎一样的样本"，R² 能虚高 0.1~0.3。
两种正确的切法：
  GroupKFold(groups=year) —— 检验模型能否外推到没见过的年份（更严格，推荐）
  GroupKFold(groups=plot) —— 检验模型能否外推到没见过的地块
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.metrics import (
    accuracy_score,
    cohen_kappa_score,
    f1_score,
    mean_absolute_error,
    mean_squared_error,
    r2_score,
)
from sklearn.model_selection import GroupKFold

from .config import Cfg
from .features import feature_columns

# 这些列不参与建模（标识 + 中间量）
NON_FEATURE = {"plot_id", "year", "n_obs", "PHENO_is_crop", "FIT_rmse"}


def make_model(cfg: Cfg, task: str):
    params = {
        "n_estimators": int(cfg.get("model.rf.n_estimators", 500)),
        "min_samples_leaf": int(cfg.get("model.rf.min_samples_leaf", 2)),
        "random_state": 42,
        "n_jobs": -1,
    }
    max_depth = cfg.get("model.rf.max_depth", None)
    if max_depth:
        params["max_depth"] = int(max_depth)
    if task == "classification":
        return RandomForestClassifier(**params)
    return RandomForestRegressor(**params)


def compute_metrics(y_true, y_pred, task: str, y_mean: float | None = None) -> dict:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    if task == "classification":
        return {
            "n": int(len(y_true)),
            "accuracy": float(accuracy_score(y_true, y_pred)),
            "kappa": float(cohen_kappa_score(y_true, y_pred)),
            "macro_f1": float(f1_score(y_true, y_pred, average="macro")),
        }
    rmse = float(np.sqrt(mean_squared_error(y_true, y_pred)))
    out = {
        "n": int(len(y_true)),
        "R2": float(r2_score(y_true, y_pred)),
        "RMSE": rmse,
        "MAE": float(mean_absolute_error(y_true, y_pred)),
    }
    if y_mean:
        # 相对 RMSE 跨数据集可比，比裸 RMSE 更能说明问题
        out["nRMSE_pct"] = float(rmse / y_mean * 100.0)
    return out


def train_and_evaluate(
    features: pd.DataFrame,
    ground_truth: pd.DataFrame,
    cfg: Cfg,
) -> dict:
    """合并实测值 -> 分组 CV -> 留出年测试 -> 全量重训。

    返回 dict，含 model / metrics_cv / metrics_holdout / importance /
    predictions / feature_names / skipped。
    """
    task = str(cfg.get("model.task", "regression"))
    target = str(cfg.require("model.target"))
    group_col = str(cfg.get("model.cv_group", "year"))
    test_years = [int(y) for y in (cfg.get("model.test_years", []) or [])]

    if target not in ground_truth.columns:
        raise ValueError(
            "实测数据里没有目标列 " + target + "，现有列: " + ", ".join(ground_truth.columns)
        )

    gt = ground_truth[["plot_id", "year", target]].copy()
    gt["year"] = gt["year"].astype(int)

    data = features.merge(gt, on=["plot_id", "year"], how="inner")
    data = data.dropna(subset=[target]).reset_index(drop=True)

    dropped = []
    if len(data) < 20:
        return {"skipped": "可用样本仅 " + str(len(data)) + " 条（<20），不足以训练模型",
                "metrics_cv": {}, "metrics_holdout": {}, "predictions": data}

    feats = [c for c in feature_columns(data) if c not in NON_FEATURE and c != target]
    if not feats:
        return {"skipped": "没有可用的数值特征列", "metrics_cv": {},
                "metrics_holdout": {}, "predictions": data}

    # 特征里 NaN 用中位数填（RF 本身能处理，但 NaN 会影响 importance 的可解释性）
    X = data[feats].copy()
    X = X.fillna(X.median(numeric_only=True))
    y = data[target].to_numpy()
    groups = data[group_col].to_numpy()

    n_groups = len(np.unique(groups))
    is_reg = task != "classification"
    y_mean = float(np.mean(y)) if is_reg else None

    result: dict = {
        "skipped": None,
        "task": task,
        "target": target,
        "n_samples": int(len(data)),
        "n_features": len(feats),
        "feature_names": feats,
        "group_col": group_col,
        "n_groups": int(n_groups),
    }

    # ---------- 1) 分组交叉验证 ----------
    if n_groups >= 3:
        cv = GroupKFold(n_splits=min(5, n_groups))
        oof = np.full(len(y), np.nan)
        for tr, va in cv.split(X, y, groups):
            m = make_model(cfg, task)
            m.fit(X.iloc[tr], y[tr])
            oof[va] = m.predict(X.iloc[va])
        ok = ~np.isnan(oof)
        result["metrics_cv"] = compute_metrics(y[ok], oof[ok], task, y_mean)
        cv_pred = oof
    else:
        result["metrics_cv"] = {}
        result["cv_warning"] = "分组数少于 3，跳过交叉验证"
        cv_pred = np.full(len(y), np.nan)

    # ---------- 2) 留出年份（真正的时间外推测试）----------
    if test_years:
        te = data[group_col].isin(test_years).to_numpy()
        tr = ~te
        if te.sum() >= 5 and tr.sum() >= 20:
            m = make_model(cfg, task)
            m.fit(X[tr], y[tr])
            pred_te = m.predict(X[te])
            result["metrics_holdout"] = compute_metrics(y[te], pred_te, task, y_mean)
            result["holdout_years"] = test_years
            result["holdout_detail"] = pd.DataFrame({
                "plot_id": data.loc[te, "plot_id"].to_numpy(),
                "year": data.loc[te, "year"].to_numpy(),
                "observed": y[te],
                "predicted": pred_te,
            })
        else:
            result["holdout_warning"] = "留出年份样本不足，跳过"
    else:
        result["metrics_holdout"] = {}

    # ---------- 3) 全量重训，用于出图和上线 ----------
    final = make_model(cfg, task)
    final.fit(X, y)
    result["model"] = final

    imp = pd.DataFrame({
        "feature": feats,
        "importance": final.feature_importances_,
    }).sort_values("importance", ascending=False).reset_index(drop=True)
    result["importance"] = imp

    preds = data[["plot_id", "year", target]].copy()
    preds["predicted"] = final.predict(X)
    preds["cv_predicted"] = cv_pred
    result["predictions"] = preds
    return result

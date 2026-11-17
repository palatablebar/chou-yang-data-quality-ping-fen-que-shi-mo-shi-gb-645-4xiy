"""变化裁决：把总分变化精确拆成「字段质量效应」与「样本覆盖效应」。

七次同引擎重评（全部 0.01 量化）：
- S0 旧批整批、S1 新批整批（与运行记录逐位相同）
- C0/C1 两批共有工单、R0 仅旧批（移除）、A1 仅新批（新增）
- X0 反事实批：新批字段质量的共有工单 + 旧批移除工单（行数与 S0 相同）

扣分空间的精确恒等分解（望远镜相消，残差恒 0.00，不引入二次舍入）：
    字段质量效应 Q = P_X0 − P_S0  （覆盖与旧批完全相同，仅共有工单质量变了）
    样本覆盖效应 C = P_S1 − P_X0  （共有质量固定在新批，仅把 R0 换成 A1）
    ΔP = P_S1 − P_S0 = Q + C
分数空间取相反数：总分变化 ΔS = −Q − C。
只看共有工单会漏掉新增高缺失工单，A1 的缺失率单独列示并打风险旗标。
朴素做法（共有扣分差 + 新增/移除扣分差）不闭合，因为各视图比率分母不同、
封顶非线性，本模块用反事实批而不是相减两个子集分。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from ... import config
from ..m2_profile import column_stats
from . import matching, subset

ZERO = Decimal("0.00")
QUANT = config.SCORE_QUANT
# 「新增高缺失工单」风险：新增桶缺失率高于共有桶 2 个百分点即提示
HIGH_MISSING_GAP = Decimal("0.0200")

# 裁决码：以 0.01（一个计分刻度）为持平阈值
VERDICTS = {
    "V_QUALITY_DRIVEN": "字段质量主导",
    "V_COVERAGE_DRIVEN": "样本覆盖主导",
    "V_BOTH_RISE": "双重恶化",
    "V_STABLE": "基本持平",
    "V_NO_OVERLAP": "无共有工单，无法裁决字段质量",
}


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _rate(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0000"
    return str((Decimal(numerator) / Decimal(denominator)).quantize(
        config.RATE_QUANT, rounding=ROUND_HALF_UP))


def _view(name: str, label: str, evaluated: dict, rows: int) -> dict:
    scored = evaluated["score"]
    counts = evaluated["counts"]
    return {
        "key": name,
        "label": label,
        "rows": rows,
        "score": str(scored["score"]),
        "penalty_sum": str(scored["penalty_sum"]),
        "cells_total": counts["cells_total"],
        "cells_missing": counts["cells_missing"],
        "format_bad": counts["format_bad"],
        "rows_dup": counts["rows_dup"],
        "outlier_cells": counts["outlier_cells"],
        "tested_cells": counts["tested_cells"],
        "missing_rate": _rate(counts["cells_missing"], counts["cells_total"]),
        "dims": {item["key"]: {
            "key": item["key"], "label": item["label"],
            "rate": item["rate"], "penalty": item["penalty"],
            "numerator": item["numerator"], "denominator": item["denominator"],
            "capped": item["capped"],
        } for item in scored["dims"]},
    }


def _synthetic_profile(columns: list[dict], rows: list[dict]) -> dict:
    """把来自两批的画像行拼成反事实批，再按同一套列统计重算。"""
    per_column = []
    for column in columns:
        contract = config.COLUMN_MAP[column["name"]]
        cells = [row["cells"][column["name"]] for row in rows]
        per_column.append(column_stats.describe(contract, cells))
    missing = sum(item["missing"] for item in per_column)
    bad = sum(item["format_bad"] for item in per_column)
    total = len(rows) * len(columns)
    return {
        "columns": columns,
        "rows": rows,
        "profile": per_column,
        "counts": {
            "rows": len(rows),
            "cells_total": total,
            "cells_missing": missing,
            "cells_value": total - missing,
            "cells_ok": total - missing - bad,
            "cells_format_bad": bad,
            "columns": len(columns),
            "empty_columns": [item["name"] for item in per_column
                              if item["status"] == "empty_column"],
        },
    }


def _dim_rows(left_view: dict | None, right_view: dict | None) -> list[dict]:
    """逐维扣分差（right − left）；无共有工单时该效应不存在。"""
    rows = []
    for weight in config.WEIGHTS:
        key = weight["key"]
        if left_view is None or right_view is None:
            rows.append({"key": key, "label": weight["label"],
                         "left_penalty": None, "right_penalty": None,
                         "effect": None})
            continue
        left = _dec(left_view["dims"][key]["penalty"])
        right = _dec(right_view["dims"][key]["penalty"])
        rows.append({"key": key, "label": weight["label"],
                     "left_penalty": str(left), "right_penalty": str(right),
                     "effect": str((right - left).quantize(QUANT))})
    return rows


def build(base_profile: dict, target_profile: dict) -> dict:
    parts = matching.partition(base_profile, target_profile)
    no_overlap = not parts["common_keys"]

    full_base = subset.evaluate(base_profile, list(range(len(base_profile["rows"]))))
    full_target = subset.evaluate(target_profile,
                                  list(range(len(target_profile["rows"]))))
    only_removed = subset.evaluate(base_profile, parts["base_only"])
    only_added = subset.evaluate(target_profile, parts["target_only"])
    if no_overlap:
        common_base = common_target = counterfactual = None
    else:
        common_base = subset.evaluate(base_profile, parts["base_common"])
        common_target = subset.evaluate(target_profile, parts["target_common"])
        cf_rows = ([target_profile["rows"][i] for i in parts["target_common"]]
                   + [base_profile["rows"][i] for i in parts["base_only"]])
        cf_profile = _synthetic_profile(base_profile["columns"], cf_rows)
        counterfactual = subset.evaluate(cf_profile, list(range(len(cf_rows))))

    views = {
        "base_full": _view("base_full", "旧批整批", full_base,
                           len(base_profile["rows"])),
        "target_full": _view("target_full", "新批整批", full_target,
                             len(target_profile["rows"])),
        "base_common": None if no_overlap else _view(
            "base_common", "共有工单·旧批", common_base, len(parts["base_common"])),
        "target_common": None if no_overlap else _view(
            "target_common", "共有工单·新批", common_target,
            len(parts["target_common"])),
        "base_only": _view("base_only", "仅旧批（移除）", only_removed,
                           len(parts["base_only"])),
        "target_only": _view("target_only", "仅新批（新增）", only_added,
                             len(parts["target_only"])),
        "counterfactual": None if no_overlap else _view(
            "counterfactual", "反事实批（新质量共有＋移除）",
            counterfactual, len(parts["target_common"]) + len(parts["base_only"])),
    }

    score_delta = (_dec(views["target_full"]["score"])
                   - _dec(views["base_full"]["score"])).quantize(QUANT)

    if no_overlap:
        quality_effect = coverage_effect = None
        quality_score_effect = coverage_score_effect = None
        quality_dims = coverage_dims = []
        closure_residual = None
        verdict_code = "V_NO_OVERLAP"
    else:
        # 扣分空间望远镜分解：Q = P_X0 − P_S0，C = P_S1 − P_X0
        quality_effect = (_dec(views["counterfactual"]["penalty_sum"])
                          - _dec(views["base_full"]["penalty_sum"])).quantize(QUANT)
        coverage_effect = (_dec(views["target_full"]["penalty_sum"])
                           - _dec(views["counterfactual"]["penalty_sum"])).quantize(QUANT)
        penalty_delta = (_dec(views["target_full"]["penalty_sum"])
                         - _dec(views["base_full"]["penalty_sum"])).quantize(QUANT)
        closure_residual = (
            penalty_delta - quality_effect - coverage_effect).quantize(QUANT)
        quality_score_effect = (-quality_effect).quantize(QUANT)
        coverage_score_effect = (-coverage_effect).quantize(QUANT)
        quality_dims = _dim_rows(views["base_full"], views["counterfactual"])
        coverage_dims = _dim_rows(views["counterfactual"], views["target_full"])
        verdict_code = _verdict(quality_effect, coverage_effect)

    added_risk = _added_risk(views, no_overlap)
    return {
        "views": views,
        "overlap": {
            "common_tickets": len(parts["common_keys"]),
            "base_common_rows": len(parts["base_common"]),
            "target_common_rows": len(parts["target_common"]),
            "base_only_rows": len(parts["base_only"]),
            "target_only_rows": len(parts["target_only"]),
            "base_unkeyed_rows": len(parts["base_unkeyed"]),
            "target_unkeyed_rows": len(parts["target_unkeyed"]),
            "base_accepted_rows": len(base_profile["rows"]),
            "target_accepted_rows": len(target_profile["rows"]),
        },
        "effects": {
            "score_delta": str(score_delta),
            "quality_effect": None if quality_effect is None else str(quality_effect),
            "coverage_effect": None if coverage_effect is None else str(coverage_effect),
            "quality_score_effect": (None if quality_score_effect is None
                                     else str(quality_score_effect)),
            "coverage_score_effect": (None if coverage_score_effect is None
                                      else str(coverage_score_effect)),
            "closure_residual": None if closure_residual is None else str(closure_residual),
            "verdict_code": verdict_code,
            "verdict_label": VERDICTS[verdict_code],
        },
        "quality_dims": quality_dims,
        "coverage_dims": coverage_dims,
        "added_risk": added_risk,
        "no_overlap": no_overlap,
        "formula": ("Δ扣分 = 字段质量效应(反事实批−旧批) + 样本覆盖效应"
                    "(新批−反事实批)，望远镜相消残差恒 0.00；总分变化取相反数"),
    }


def _verdict(quality_effect: Decimal, coverage_effect: Decimal) -> str:
    """正扣分效应 = 拉低总分；超过一个计分刻度(0.01)才算恶化，否则记持平。"""
    flat = QUANT
    quality_worse = quality_effect > flat
    coverage_worse = coverage_effect > flat
    if quality_worse and coverage_worse:
        return "V_BOTH_RISE"
    if quality_worse:
        return "V_QUALITY_DRIVEN"
    if coverage_worse:
        return "V_COVERAGE_DRIVEN"
    if abs(quality_effect) <= flat and abs(coverage_effect) <= flat:
        return "V_STABLE"
    # 一侧在改善、另一侧在持平区间内：按绝对量较大者命名主因
    return ("V_QUALITY_DRIVEN" if abs(quality_effect) >= abs(coverage_effect)
            else "V_COVERAGE_DRIVEN")


def _added_risk(views: dict, no_overlap: bool) -> dict:
    added = views["target_only"]
    added_rate = _dec(added["missing_rate"]) if added["rows"] else ZERO
    common_rate = (ZERO if no_overlap
                   else _dec(views["target_common"]["missing_rate"]))
    gap = (added_rate - common_rate).quantize(config.RATE_QUANT)
    flagged = added["rows"] > 0 and gap >= HIGH_MISSING_GAP
    return {
        "flagged": flagged,
        "added_rows": added["rows"],
        "added_missing_rate": str(added_rate),
        "common_missing_rate": None if no_overlap else str(common_rate),
        "gap": None if no_overlap else str(gap),
        "threshold": str(HIGH_MISSING_GAP),
        "code": "K_ADDED_HIGH_MISSING" if flagged else "",
    }

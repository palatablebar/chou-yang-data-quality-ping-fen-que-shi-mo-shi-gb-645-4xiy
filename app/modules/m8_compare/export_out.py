"""M8 导出：对比结论 CSV 与快照 JSON，全部读同一条对比快照，与页面同口径。"""
from __future__ import annotations

import csv
import io
import json

BOM = "﻿"

VIEW_ORDER = (
    ("base_full", "旧批整批"),
    ("counterfactual", "反事实批（新质量共有＋移除）"),
    ("target_full", "新批整批"),
    ("base_common", "共有工单·旧批"),
    ("target_common", "共有工单·新批"),
    ("base_only", "仅旧批（移除）"),
    ("target_only", "仅新批（新增）"),
)


def _csv_text(header: list[str], rows: list[list]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if cell is None else str(cell) for cell in row])
    return BOM + buffer.getvalue()


def comparison_csv(payload: dict) -> str:
    comparison = payload["comparison"]
    effects = comparison["effects"]
    overlap = comparison["overlap"]
    rows: list[list] = [
        ["== 结论 ==", "", "", "", ""],
        ["对比编号", payload["comparison_id"], "", "", ""],
        ["旧批", f'{payload["base"]["name"]} v{payload["base"]["version"]}',
         payload["base"]["run_id"], payload["base"]["score"], ""],
        ["新批", f'{payload["target"]["name"]} v{payload["target"]["version"]}',
         payload["target"]["run_id"], payload["target"]["score"], ""],
        ["总分变化(新−旧)", effects["score_delta"], "分", "负值表示总分下降", ""],
        ["字段质量效应", effects["quality_effect"], "扣分",
         "共有工单扣分差，正值=字段质量恶化", ""],
        ["样本覆盖效应", effects["coverage_effect"], "扣分",
         "新增−移除工单扣分差，正值=换样本拖累", ""],
        ["分解残差", effects["closure_residual"], "分",
         "Δ扣分 − 两效应，恒为 0.00", ""],
        ["裁决", effects["verdict_code"], effects["verdict_label"], "", ""],
        ["== 配对 ==", "", "", "", ""],
        ["共有工单数", overlap["common_tickets"], "键", "", ""],
        ["共有行(旧/新)", overlap["base_common_rows"], overlap["target_common_rows"],
         "", ""],
        ["移除行/新增行", overlap["base_only_rows"], overlap["target_only_rows"],
         "", ""],
        ["无键行(旧/新)", overlap["base_unkeyed_rows"], overlap["target_unkeyed_rows"],
         "工单号缺失或格式不合格，不参与配对", ""],
        ["== 六组重评 ==", "", "", "", ""],
    ]
    for key, label in VIEW_ORDER:
        view = comparison["views"][key]
        if view is None:
            rows.append([label, "不适用", "无共有工单", "", ""])
            continue
        rows.append([label, view["rows"], view["score"], view["penalty_sum"],
                     view["missing_rate"]])
    rows.append(["== 字段质量效应·逐维扣分(反事实批−旧批) ==", "", "", "", ""])
    for item in comparison["quality_dims"]:
        rows.append([item["label"], item["left_penalty"], item["right_penalty"],
                     item["effect"], ""])
    rows.append(["== 样本覆盖效应·逐维扣分(新批−反事实批) ==", "", "", "", ""])
    for item in comparison["coverage_dims"]:
        rows.append([item["label"], item["left_penalty"], item["right_penalty"],
                     item["effect"], ""])
    risk = comparison["added_risk"]
    rows.append(["== 新增工单风险 ==", "", "", "", ""])
    rows.append(["新增高缺失旗标", "是" if risk["flagged"] else "否",
                 risk["code"], "缺失率差", risk["gap"]])
    rows.append(["新增桶缺失率/共有桶缺失率", risk["added_missing_rate"],
                 risk["common_missing_rate"], "阈值", risk["threshold"]])
    return _csv_text(["项目", "数值一", "数值二", "数值三", "数值四"], rows)


def comparison_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)

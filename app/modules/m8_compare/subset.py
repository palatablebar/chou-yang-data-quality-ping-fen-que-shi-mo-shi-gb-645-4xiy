"""子集重评：从一次运行对应的原始字节重建逐格三态，再取子集重走
画像 → 重复 → 离群 → 评分。

重建只使用该运行自己的输入摘要与数据集原文：重采信后的字节 sha256 必须与
运行记录一致，否则拒绝对比（E_DIGEST_MISMATCH），保证复核用的就是当时的数据。
整批作为子集代入时，得分必须与运行记录逐位相同（service 层会断言）。
"""
from __future__ import annotations

from ... import config, manifest
from ...errors import BadRequest
from ..m1_ingest import service as ingest_service
from ..m2_profile import column_stats, profile as profile_mod
from ..m4_duplicates import groups as duplicate_groups
from ..m5_outliers import flags as outlier_flags
from ..m6_score import score as score_mod


def rebuild_profile(settings, store, run: dict) -> dict:
    """从 run_id 对应的不可变原文重采信 + 画像。

    只使用该运行自己留存的字节：重采信后的 sha256 必须与运行记录一致，
    否则拒绝对比（E_REINGEST_MISMATCH），保证复核用的就是当时那一版数据，
    不受同名数据集后续换版影响。
    """
    artifact = store.get_run_artifact(run["run_id"])
    if artifact is None:
        raise BadRequest(
            f"运行 {run['run_id']} 的输入原文未留存，无法复核（历史数据请重跑一次评分）",
            code="E_ARTIFACT_GONE")
    actual_digest = manifest.digest_bytes(artifact["content"])
    if artifact["digest"] != run["digest"] or actual_digest != run["digest"]:
        raise BadRequest(
            f"运行 {run['run_id']} 留存原文摘要与记录不一致，拒绝基于错版数据对比",
            code="E_DIGEST_MISMATCH",
            detail={"recorded": run["digest"], "artifact": artifact["digest"],
                    "actual": actual_digest})
    ingest = ingest_service.ingest(
        settings, name=run["name"], data=artifact["content"],
        declared=artifact["declared_encoding"], store=store)
    if ingest["digest"] != run["digest"] or len(ingest["rows"]) != run["rows_accepted"]:
        raise BadRequest(f"运行 {run['run_id']} 重采信结果与记录不一致，拒绝对比",
                         code="E_REINGEST_MISMATCH")
    return profile_mod.build(ingest)


def build_profile(full_profile: dict, row_indexes: list[int]) -> dict:
    """按行下标取子集，重算列级统计；逐格三态沿用重建画像，不重新解析。"""
    all_rows = full_profile["rows"]
    rows = [all_rows[index] for index in row_indexes]
    columns = full_profile["columns"]
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


def evaluate(full_profile: dict, row_indexes: list[int]) -> dict:
    """对子集跑 M4/M5/M6，返回计数与完整评分（含逐维扣分）。"""
    profile = build_profile(full_profile, row_indexes)
    duplicates = duplicate_groups.build(profile)
    outliers = outlier_flags.mark(profile)
    counts = {
        "cells_total": profile["counts"]["cells_total"],
        "cells_missing": profile["counts"]["cells_missing"],
        "format_bad": profile["counts"]["cells_format_bad"],
        "rows_accepted": len(profile["rows"]),
        "rows_dup": duplicates["duplicate_rows"],
        "outlier_cells": outliers["counts"]["outlier_cells"],
        "tested_cells": outliers["counts"]["tested_cells"],
    }
    scored = score_mod.evaluate(counts)
    return {"counts": counts, "score": scored}

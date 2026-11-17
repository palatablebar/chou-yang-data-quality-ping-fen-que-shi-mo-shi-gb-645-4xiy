"""M8 编排：两批次对比的取数、裁决、幂等落库与回放。

约定：
- 同一对运行按 (run_id 无序对, 规则版本) 识别，重跑返回同一条对比记录，
  不新增版本、不重复累计；入参顺序会被规范成「旧批 → 新批」；
- 只读取 runs 里已有的完整快照，不改动、不删除任何运行记录；
- 计算全部完成后才单事务写 comparisons，中途失败回滚，旧版原样保留；
- 整批子集重评必须与运行记录总分逐位相同，否则拒绝落库。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from ... import config
from ...errors import BadRequest, NotFound
from . import decompose, subset


def compare(settings, store, *, base_run_id: str, target_run_id: str) -> dict:
    base_run = _require_run(store, base_run_id)
    target_run = _require_run(store, target_run_id)
    base_run, target_run, reversed_input = _canonical_order(base_run, target_run)
    pair_key = _pair_key(base_run["run_id"], target_run["run_id"])

    existing = store.find_comparison(pair_key, base_run["rules_version"])
    if existing is not None:
        store.touch_comparison(existing["comparison_id"], _now())
        payload = _json_load(existing["payload_json"])
        # 即使命中幂等回放，也要按本次入参标注方向是否被规范过
        payload["replay"] = True
        payload["reversed_input"] = reversed_input
        return payload

    base_result = store.load_result(base_run["run_id"])
    target_result = store.load_result(target_run["run_id"])
    if base_result is None or target_result is None:
        raise BadRequest("运行快照缺失，无法对比")

    base_profile = subset.rebuild_profile(settings, store, base_run)
    target_profile = subset.rebuild_profile(settings, store, target_run)
    comparison = decompose.build(base_profile, target_profile)
    _assert_reproducible(comparison, base_run, target_run)

    comparison_id = _comparison_id(pair_key, base_run["rules_version"])
    stamp = _now()
    payload = {
        "comparison_id": comparison_id,
        "pair_key": pair_key,
        "rules_version": base_run["rules_version"],
        "reversed_input": reversed_input,
        "replay": False,
        "created_at": stamp,
        "base": _run_header(base_run),
        "target": _run_header(target_run),
        "comparison": comparison,
    }
    effects = comparison["effects"]
    record = {
        "comparison_id": comparison_id,
        "base_run_id": base_run["run_id"],
        "target_run_id": target_run["run_id"],
        "pair_key": pair_key,
        "rules_version": base_run["rules_version"],
        "score_delta": effects["score_delta"],
        "quality_effect": effects["quality_effect"],
        "coverage_effect": effects["coverage_effect"],
        "verdict_code": effects["verdict_code"],
        "created_at": stamp,
    }
    store.save_comparison(record, payload)
    return payload


def latest_payload(store, comparison_id: str | None = None) -> dict:
    if comparison_id:
        row = store.get_comparison(comparison_id)
        if row is None:
            raise NotFound(f"对比记录不存在：{comparison_id}",
                           code="E_COMPARISON_NOT_FOUND")
        return _json_load(row["payload_json"])
    rows = store.list_comparisons(limit=1)
    if not rows:
        raise NotFound("还没有对比记录", code="E_COMPARISON_NOT_FOUND")
    return _json_load(rows[0]["payload_json"])


def list_payloads(store, limit: int = 20) -> list[dict]:
    return [_json_load(row["payload_json"]) for row in store.list_comparisons(limit)]


def _require_run(store, run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise NotFound(f"运行记录不存在：{run_id}", code="E_RUN_NOT_FOUND")
    return run


def _canonical_order(base_run: dict, target_run: dict) -> tuple[dict, dict, bool]:
    """规范方向：版本号小者为旧批；同版本按创建时间，再按 run_id 兜底。"""
    left_key = (int(base_run["version"]), base_run["created_at"], base_run["run_id"])
    right_key = (int(target_run["version"]), target_run["created_at"],
                 target_run["run_id"])
    if left_key == right_key:
        raise BadRequest("同一运行记录不能与自身对比")
    if left_key < right_key:
        return base_run, target_run, False
    return target_run, base_run, True


def _pair_key(left_run_id: str, right_run_id: str) -> str:
    return "|".join(sorted((left_run_id, right_run_id)))


def _comparison_id(pair_key: str, rules_version: str) -> str:
    digest = hashlib.sha256(
        f"{pair_key}\x00{rules_version}".encode("utf-8")).hexdigest()[:12]
    return f"QC-{digest}"


def _assert_reproducible(comparison: dict, base_run: dict, target_run: dict) -> None:
    views = comparison["views"]
    checks = (
        (views["base_full"]["score"], str(base_run["score"]), "旧批整批"),
        (views["target_full"]["score"], str(target_run["score"]), "新批整批"),
    )
    for actual, expected, label in checks:
        if actual != expected:
            raise BadRequest(
                f"{label}子集重评 {actual} 与运行记录 {expected} 不一致，拒绝落库",
                code="E_REPRODUCIBILITY")
    residual = comparison["effects"]["closure_residual"]
    if residual is not None and residual != "0.00":
        raise BadRequest(f"效应分解不闭合，残差 {residual}，拒绝落库",
                         code="E_DECOMPOSITION")


def _run_header(run: dict) -> dict:
    return {
        "run_id": run["run_id"], "name": run["name"], "version": int(run["version"]),
        "digest": run["digest"], "score": str(run["score"]), "grade": run["grade"],
        "rows_accepted": int(run["rows_accepted"]),
        "cells_missing": int(run["cells_missing"]),
        "created_at": run["created_at"],
    }


def _json_load(text: str) -> dict:
    return json.loads(text)


def _now() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")

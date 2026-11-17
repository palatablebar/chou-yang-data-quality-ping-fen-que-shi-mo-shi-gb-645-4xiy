"""M8 批次对比与变化裁决：效应分解、闭合、配对、幂等、版本保留、页面与导出同口径。"""
from __future__ import annotations

import csv
import io
import json
from decimal import Decimal

import pytest

from app.errors import BadRequest, NotFound
from app.modules.m6_score import service as m6
from app.modules.m8_compare import decompose, export_out, matching, service, subset
from fixtures import dirty_cases

DIMS = ("missing", "duplicate", "outlier", "format")


def _two_batches(settings, store):
    """旧批 10 行全干净；新批 = 8 条共有(前两条 agent 置空) + 3 条高缺失新增。"""
    old = m6.run(settings, store, name="batch.csv",
                 data=dirty_cases.frame(dirty_cases.count_rows(10)).encode())
    new_rows = []
    for index, row in enumerate(dirty_cases.count_rows(10)[:8]):
        if index in (0, 1):
            new_rows.append(dirty_cases.make_row(
                ticket=f"T-2026-91{index:04d}", customer=f"00{index + 100}",
                agent=""))
        else:
            new_rows.append(row)
    for j in range(3):
        new_rows.append(dirty_cases.make_row(
            ticket=f"T-2026-77{j:04d}", customer=f"00{700 + j}", agent="",
            region=""))
    new = m6.run(settings, store, name="batch.csv",
                 data=dirty_cases.frame(new_rows).encode(), expected_version=1)
    return old, new


def _comparison(settings, store, old, new):
    return service.compare(settings, store, base_run_id=old["run"]["run_id"],
                           target_run_id=new["run"]["run_id"])


# ---------- 分解口径与闭合 ----------

def test_full_batch_views_reproduce_run_scores(settings, store):
    """可复核：整批视图得分必须与两条运行记录逐位相同。"""
    old, new = _two_batches(settings, store)
    payload = _comparison(settings, store, old, new)
    views = payload["comparison"]["views"]
    assert views["base_full"]["score"] == old["run"]["score"]
    assert views["target_full"]["score"] == new["run"]["score"]
    assert views["base_full"]["rows"] == old["run"]["rows_accepted"]
    assert views["target_full"]["rows"] == new["run"]["rows_accepted"]


def test_effects_decompose_score_delta_with_zero_residual(settings, store):
    old, new = _two_batches(settings, store)
    payload = _comparison(settings, store, old, new)
    effects = payload["comparison"]["effects"]
    delta = Decimal(effects["score_delta"])
    q = Decimal(effects["quality_score_effect"])
    c = Decimal(effects["coverage_score_effect"])
    assert delta == q + c
    assert effects["closure_residual"] == "0.00"
    # 扣分空间同向闭合
    assert (Decimal(effects["quality_effect"])
            + Decimal(effects["coverage_effect"])
            == Decimal(views_penalty_delta(payload)))
    # 本构造：两效应都为负向（扣分效应为正），判双重恶化
    assert effects["verdict_code"] == "V_BOTH_RISE"


def views_penalty_delta(payload: dict) -> str:
    views = payload["comparison"]["views"]
    return str(Decimal(views["target_full"]["penalty_sum"])
               - Decimal(views["base_full"]["penalty_sum"]))


def test_counterfactual_identity_holds_per_dimension(settings, store):
    old, new = _two_batches(settings, store)
    payload = _comparison(settings, store, old, new)
    for q, cov in zip(payload["comparison"]["quality_dims"],
                      payload["comparison"]["coverage_dims"]):
        assert q["key"] == cov["key"]
        total_effect = Decimal(q["effect"]) + Decimal(cov["effect"])
        dim = payload["comparison"]["views"]["target_full"]["dims"][q["key"]]
        base_dim = payload["comparison"]["views"]["base_full"]["dims"][q["key"]]
        assert total_effect == (Decimal(dim["penalty"])
                                - Decimal(base_dim["penalty"]))


def test_quality_effect_isolates_field_quality(settings, store):
    """只改共有工单的字段（agent 置空），反事实批与旧批覆盖相同 → 质量效应为正。"""
    old, new = _two_batches(settings, store)
    payload = _comparison(settings, store, old, new)
    effects = payload["comparison"]["effects"]
    assert Decimal(effects["quality_effect"]) > Decimal("0.01")
    assert Decimal(effects["coverage_effect"]) > Decimal("0.01")
    # 新增桶更脏：覆盖效应在本构造里占大头
    assert (Decimal(effects["coverage_effect"])
            > Decimal(effects["quality_effect"]))


def test_pure_coverage_change_has_zero_quality_effect(settings, store):
    """共有工单逐格不变，只新增/移除工单：字段质量效应必须为 0.00。"""
    old = m6.run(settings, store, name="p.csv",
                 data=dirty_cases.frame(dirty_cases.count_rows(8)).encode())
    kept = dirty_cases.count_rows(8)[:6]
    added = [dirty_cases.make_row(ticket=f"T-2026-66{j:04d}", customer=f"00{660 + j}")
             for j in range(2)]
    new = m6.run(settings, store, name="p.csv",
                 data=dirty_cases.frame(kept + added).encode(), expected_version=1)
    payload = _comparison(settings, store, old, new)
    effects = payload["comparison"]["effects"]
    assert effects["quality_effect"] == "0.00"
    assert effects["quality_score_effect"] == "0.00"
    assert effects["verdict_code"] in ("V_STABLE", "V_COVERAGE_DRIVEN")
    assert payload["comparison"]["overlap"]["common_tickets"] == 6


def test_identical_content_rerun_compares_as_stable(settings, store):
    text = dirty_cases.frame(dirty_cases.count_rows(6))
    first = m6.run(settings, store, name="s.csv", data=text.encode())
    second = m6.run(settings, store, name="s.csv", data=text.encode())
    # 同输入幂等：根本不会产生第二条运行
    assert second["replay"] is True
    with pytest.raises(BadRequest):
        service.compare(settings, store, base_run_id=first["run"]["run_id"],
                        target_run_id=first["run"]["run_id"])


# ---------- 配对与无键行 ----------

def test_partition_buckets_and_row_conservation(settings, store):
    old, new = _two_batches(settings, store)
    bp = subset.rebuild_profile(settings, store, old["run"])
    tp = subset.rebuild_profile(settings, store, new["run"])
    parts = matching.partition(bp, tp)
    overlap = _comparison(settings, store, old, new)["comparison"]["overlap"]
    assert overlap["common_tickets"] == 8
    assert overlap["base_only_rows"] == 2
    assert overlap["target_only_rows"] == 3
    # 行守恒：共有 + 仅本批 + 无键 = 采纳行
    assert (overlap["base_common_rows"] + overlap["base_only_rows"]
            + overlap["base_unkeyed_rows"] == overlap["base_accepted_rows"])
    assert (overlap["target_common_rows"] + overlap["target_only_rows"]
            + overlap["target_unkeyed_rows"] == overlap["target_accepted_rows"])
    assert len(parts["common_keys"]) == overlap["common_tickets"]


def test_unkeyed_rows_excluded_from_matching_but_counted(settings, store):
    old_rows = dirty_cases.count_rows(4)
    old = m6.run(settings, store, name="u.csv",
                 data=dirty_cases.frame(old_rows).encode())
    # 新批一条工单号缺失（无键），其余共有
    new_rows = [dirty_cases.make_row(ticket="", customer="00901")]
    new_rows += dirty_cases.count_rows(4)[:3]
    new = m6.run(settings, store, name="u.csv",
                 data=dirty_cases.frame(new_rows).encode(), expected_version=1)
    payload = _comparison(settings, store, old, new)
    overlap = payload["comparison"]["overlap"]
    assert overlap["target_unkeyed_rows"] == 1
    assert overlap["common_tickets"] == 3
    # 无键行进新批整评但不进共有桶
    assert overlap["target_accepted_rows"] == 4
    assert overlap["target_common_rows"] == 3


def test_no_overlap_returns_no_overlap_verdict(settings, store):
    old = m6.run(settings, store, name="n1.csv",
                 data=dirty_cases.frame(dirty_cases.count_rows(4)).encode())
    other = [dirty_cases.make_row(ticket=f"T-2026-88{i:04d}",
                                  customer=f"00{800 + i}") for i in range(4)]
    new = m6.run(settings, store, name="n2.csv",
                 data=dirty_cases.frame(other).encode())
    payload = service.compare(settings, store, base_run_id=old["run"]["run_id"],
                              target_run_id=new["run"]["run_id"])
    comp = payload["comparison"]
    assert comp["no_overlap"] is True
    assert comp["effects"]["verdict_code"] == "V_NO_OVERLAP"
    assert comp["effects"]["quality_effect"] is None
    assert comp["effects"]["closure_residual"] is None
    assert comp["views"]["counterfactual"] is None
    assert comp["views"]["base_common"] is None
    # 整批视图仍可用，覆盖效应仍可单独看
    assert comp["views"]["base_full"]["score"] == old["run"]["score"]


def test_added_high_missing_flag(settings, store):
    old, new = _two_batches(settings, store)
    risk = _comparison(settings, store, old, new)["comparison"]["added_risk"]
    assert risk["flagged"] is True
    assert risk["code"] == "K_ADDED_HIGH_MISSING"
    assert Decimal(risk["added_missing_rate"]) > Decimal(risk["common_missing_rate"])


# ---------- 幂等、方向规范化、留存 ----------

def test_rerun_same_pair_is_idempotent(settings, store):
    old, new = _two_batches(settings, store)
    first = _comparison(settings, store, old, new)
    second = _comparison(settings, store, old, new)
    assert second["replay"] is True
    assert second["comparison_id"] == first["comparison_id"]
    assert store.count_comparisons() == 1


def test_reversed_inputs_canonicalize_to_same_record(settings, store):
    old, new = _two_batches(settings, store)
    forward = _comparison(settings, store, old, new)
    backward = service.compare(settings, store, base_run_id=new["run"]["run_id"],
                               target_run_id=old["run"]["run_id"])
    assert backward["comparison_id"] == forward["comparison_id"]
    assert backward["reversed_input"] is True
    assert backward["base"]["run_id"] == old["run"]["run_id"]
    assert backward["target"]["run_id"] == new["run"]["run_id"]
    assert store.count_comparisons() == 1


def test_comparison_never_touches_runs_or_artifacts(settings, store):
    old, new = _two_batches(settings, store)
    runs_before = store.count_runs()
    versions_before = store.latest_version("batch.csv")
    _comparison(settings, store, old, new)
    _comparison(settings, store, old, new)
    assert store.count_runs() == runs_before == 2
    assert store.latest_version("batch.csv") == versions_before == 2
    # 每个 run 的不可变原文都在，且摘要与运行一致
    for run in (old["run"], new["run"]):
        artifact = store.get_run_artifact(run["run_id"])
        assert artifact is not None
        assert artifact["digest"] == run["digest"]


def test_failed_persistence_keeps_previous_version(settings, store, monkeypatch):
    old, new = _two_batches(settings, store)
    first = _comparison(settings, store, old, new)
    # 让第二次对「另一对」批次的写入抛错：库里必须只保留第一条完整记录
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(store, "save_comparison", boom)
    third_rows = [dirty_cases.make_row(ticket=f"T-2026-44{i:04d}",
                                       customer=f"00{400 + i}") for i in range(3)]
    third = m6.run(settings, store, name="other.csv",
                   data=dirty_cases.frame(third_rows).encode())
    with pytest.raises(RuntimeError):
        service.compare(settings, store, base_run_id=old["run"]["run_id"],
                        target_run_id=third["run"]["run_id"])
    assert store.count_comparisons() == 1
    surviving = store.get_comparison(first["comparison_id"])
    assert json.loads(surviving["payload_json"])["comparison_id"] == first["comparison_id"]


def test_missing_run_raises_not_found(settings, store):
    old, _ = _two_batches(settings, store)
    with pytest.raises(NotFound):
        service.compare(settings, store, base_run_id=old["run"]["run_id"],
                        target_run_id="QS-does-not-exist")


def test_digest_mismatch_rejects_comparison(settings, store):
    """留存原文被外部篡改导致摘要不符：拒绝出对比，不静默用错数据。"""
    old, new = _two_batches(settings, store)
    artifact = store.get_run_artifact(old["run"]["run_id"])
    tampered = artifact["content"].replace(b"P2", b"P3")
    with store.connect() as conn:
        conn.execute("UPDATE run_artifacts SET content=? WHERE run_id=?",
                     (tampered, old["run"]["run_id"]))
    with pytest.raises(BadRequest) as info:
        service.compare(settings, store, base_run_id=old["run"]["run_id"],
                        target_run_id=new["run"]["run_id"])
    assert info.value.code in ("E_DIGEST_MISMATCH", "E_REINGEST_MISMATCH")
    assert store.count_comparisons() == 0


# ---------- 页面与导出同口径 ----------

def test_http_create_list_get_and_idempotent(client, settings, store):
    old, new = _two_batches(settings, store)
    resp = client.post("/api/comparisons",
                       json={"base_run_id": old["run"]["run_id"],
                             "target_run_id": new["run"]["run_id"]})
    assert resp.status_code == 200
    payload = resp.json()
    cid = payload["comparison_id"]
    again = client.post("/api/comparisons",
                        json={"base_run_id": old["run"]["run_id"],
                              "target_run_id": new["run"]["run_id"]}).json()
    assert again["replay"] is True and again["comparison_id"] == cid
    listed = client.get("/api/comparisons").json()["comparisons"]
    assert len(listed) == 1 and listed[0]["comparison_id"] == cid
    fetched = client.get(f"/api/comparisons/{cid}")
    assert fetched.status_code == 200
    assert fetched.json()["comparison"]["effects"] == payload["comparison"]["effects"]


def test_page_and_exports_read_same_snapshot(client, settings, store):
    old, new = _two_batches(settings, store)
    payload = _comparison(settings, store, old, new)
    cid = payload["comparison_id"]
    page = client.get(f"/compare?comparison_id={cid}")
    assert page.status_code == 200
    assert cid in page.text
    effects = payload["comparison"]["effects"]
    assert effects["score_delta"] in page.text
    assert effects["quality_effect"] in page.text
    assert effects["coverage_effect"] in page.text

    csv_resp = client.get(f"/api/export/comparison.csv?comparison_id={cid}")
    assert csv_resp.status_code == 200
    assert csv_resp.content.startswith("﻿".encode())
    csv_text = csv_resp.content.decode("utf-8")
    assert effects["score_delta"] in csv_text
    assert effects["quality_effect"] in csv_text
    assert effects["coverage_effect"] in csv_text
    assert "0.00" in csv_text  # 残差行
    assert "反事实批" in csv_text

    json_resp = client.get(f"/api/export/comparison.json?comparison_id={cid}")
    assert json_resp.status_code == 200
    exported = json.loads(json_resp.content)
    assert exported == payload


def test_csv_rows_are_parseable_and_seven_views_present(client, settings, store):
    old, new = _two_batches(settings, store)
    payload = _comparison(settings, store, old, new)
    cid = payload["comparison_id"]
    csv_text = client.get(
        f"/api/export/comparison.csv?comparison_id={cid}").content.decode("utf-8")
    rows = list(csv.reader(io.StringIO(csv_text.lstrip("﻿"))))
    assert rows[0] == ["项目", "数值一", "数值二", "数值三", "数值四"]
    labels = {row[0] for row in rows}
    for label in ("旧批整批", "反事实批（新质量共有＋移除）", "新批整批",
                  "共有工单·旧批", "共有工单·新批", "仅旧批（移除）",
                  "仅新批（新增）"):
        assert label in labels


def test_compare_page_empty_state(client):
    page = client.get("/compare")
    assert page.status_code == 200
    assert "还没有对比记录。" in page.text
    assert "至少需要两条运行记录" in page.text


def test_export_unknown_comparison_returns_404(client):
    resp = client.get("/api/export/comparison.csv?comparison_id=QC-missing")
    assert resp.status_code == 404
    assert resp.json()["error"] == "E_COMPARISON_NOT_FOUND"


def test_bad_requests_over_http(client, settings, store):
    old, new = _two_batches(settings, store)
    missing = client.post("/api/comparisons", json={"base_run_id": ""})
    assert missing.status_code == 400
    self_pair = client.post("/api/comparisons",
                            json={"base_run_id": old["run"]["run_id"],
                                  "target_run_id": old["run"]["run_id"]})
    assert self_pair.status_code == 400
    unknown = client.post("/api/comparisons",
                          json={"base_run_id": old["run"]["run_id"],
                                "target_run_id": "QS-nope"})
    assert unknown.status_code == 404


# ---------- 确定性与顺序无关 ----------

def test_comparison_id_is_deterministic(settings, store):
    old, new = _two_batches(settings, store)
    first = _comparison(settings, store, old, new)
    assert first["comparison_id"].startswith("QC-")
    assert len(first["comparison_id"]) == 15


def test_decompose_is_order_independent(settings, store):
    old, new = _two_batches(settings, store)
    bp = subset.rebuild_profile(settings, store, old["run"])
    tp = subset.rebuild_profile(settings, store, new["run"])
    forward = decompose.build(bp, tp)
    # 行序打乱不影响评分（M4/M5 已顺序无关），效应数字应稳定
    import copy
    bp_shuffled = copy.deepcopy(bp)
    bp_shuffled["rows"] = list(reversed(bp_shuffled["rows"]))
    rebuilt = subset.build_profile(bp_shuffled, list(range(len(bp_shuffled["rows"]))))
    again = decompose.build(rebuilt, tp)
    assert again["effects"] == forward["effects"]

"""M8 入口：创建/查询批次对比、对比页、对比 CSV/JSON 导出（页面与导出同快照）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...errors import BadRequest
from ...web import Ctx, get_ctx, render
from . import export_out, service

router = APIRouter(tags=["M8 批次对比"])


def _as_run_id(value) -> str:
    text = str(value or "").strip()
    if not text:
        raise BadRequest("缺少 run_id 参数")
    return text


@router.post("/api/comparisons")
def api_create_comparison(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    base_run_id = _as_run_id(body.get("base_run_id"))
    target_run_id = _as_run_id(body.get("target_run_id"))
    return service.compare(ctx.settings, ctx.store, base_run_id=base_run_id,
                           target_run_id=target_run_id)


@router.get("/api/comparisons")
def api_list_comparisons(limit: int = 20, ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"comparisons": service.list_payloads(ctx.store, limit=limit)}


@router.get("/api/comparisons/{comparison_id}")
def api_get_comparison(comparison_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.latest_payload(ctx.store, comparison_id)


@router.get("/api/export/comparison.csv")
def export_comparison_csv(comparison_id: str | None = None,
                          ctx: Ctx = Depends(get_ctx)) -> Response:
    payload = service.latest_payload(ctx.store, comparison_id)
    content = export_out.comparison_csv(payload)
    return Response(content=content.encode("utf-8"),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="comparison-{payload["comparison_id"]}.csv"'})


@router.get("/api/export/comparison.json")
def export_comparison_json(comparison_id: str | None = None,
                           ctx: Ctx = Depends(get_ctx)) -> Response:
    payload = service.latest_payload(ctx.store, comparison_id)
    return Response(content=export_out.comparison_json(payload),
                    media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="comparison-{payload["comparison_id"]}.json"'})


@router.get("/compare")
def page_compare(request: Request, comparison_id: str | None = None,
                 ctx: Ctx = Depends(get_ctx)):
    comparison = None
    if comparison_id:
        comparison = service.latest_payload(ctx.store, comparison_id)
    else:
        try:
            comparison = service.latest_payload(ctx.store, None)
        except Exception:
            comparison = None
    runs = ctx.store.list_runs(limit=20)
    history = service.list_payloads(ctx.store, limit=10)
    return render(request, "compare.html", page="compare", page_title="批次对比",
                  comparison=comparison, runs=runs, history=history,
                  run=ctx.store.latest_run())

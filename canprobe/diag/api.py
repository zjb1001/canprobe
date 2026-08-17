"""通信诊断 API 路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from ..store import project
from .engine import run_diagnostics
from .export import to_markdown
from .model import DiagConfig

router = APIRouter(prefix="/api/diag", tags=["diagnostics"])


def _build_report() -> dict:
    p = project()
    if p.store is None:
        raise HTTPException(400, "尚未加载报文日志")
    cfg = DiagConfig.from_dbc(p.dbc)
    return run_diagnostics(p.store, p.dbc, cfg)


@router.get("/report")
def diag_report():
    """对当前已加载日志运行通信诊断，返回完整报告（JSON）。"""
    return _build_report()


@router.get("/export")
def diag_export(fmt: str = "json"):
    """导出诊断报告：fmt=json 或 fmt=md/markdown。"""
    report = _build_report()
    if fmt in ("md", "markdown"):
        return PlainTextResponse(to_markdown(report), media_type="text/markdown")
    return report


@router.get("/config")
def diag_config():
    """返回当前诊断配置（由 DBC 自动播种，可被覆盖）。"""
    p = project()
    return DiagConfig.from_dbc(p.dbc).to_dict()

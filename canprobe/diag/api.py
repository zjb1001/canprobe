"""通信诊断 API 路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from ..store import project
from .engine import run_diagnostics
from .export import message_to_markdown, to_markdown
from .message import diagnose_message, rank_messages, resolve_message
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


def _build_message_report(ident: str) -> dict:
    """把用户输入解析成报文并体检。解析不出来时 400 带原因（含候选）。"""
    p = project()
    if p.store is None:
        raise HTTPException(400, "尚未加载报文日志")
    hit = resolve_message(p.dbc, p.store, ident)
    if hit["id"] is None:
        raise HTTPException(400, hit["error"] or f"无法识别报文「{ident}」")
    cfg = DiagConfig.from_dbc(p.dbc)
    return diagnose_message(p.store, p.dbc, hit["id"], cfg,
                            query=ident, matched_by=hit["matched_by"])


@router.get("/message")
def diag_message(ident: str):
    """单条报文体检：ident 支持 0x270 / 624 / 270h / 报文名。"""
    return _build_message_report(ident)


@router.get("/messages")
def diag_messages(limit: int = 30):
    """帧级异常报文排行（不解码信号），用于「还不知道该查哪条报文」。"""
    p = project()
    if p.store is None:
        raise HTTPException(400, "尚未加载报文日志")
    return rank_messages(p.store, p.dbc, DiagConfig.from_dbc(p.dbc), limit=limit)


@router.get("/export")
def diag_export(fmt: str = "json", ident: str | None = None):
    """导出诊断报告：fmt=json 或 fmt=md/markdown；带 ident 时导出该报文的体检报告。"""
    if ident:
        report = _build_message_report(ident)
        if fmt in ("md", "markdown"):
            return PlainTextResponse(message_to_markdown(report), media_type="text/markdown")
        return report
    report = _build_report()
    if fmt in ("md", "markdown"):
        return PlainTextResponse(to_markdown(report), media_type="text/markdown")
    return report


@router.get("/config")
def diag_config():
    """返回当前诊断配置（由 DBC 自动播种，可被覆盖）。"""
    p = project()
    return DiagConfig.from_dbc(p.dbc).to_dict()

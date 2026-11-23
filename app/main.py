"""FastAPI 接口：转换 / 封存 / 补报更正 / 取数。

失败语义：无解（422）或写入失败（500）时不改动任何既有数据，
调用方仍可从现行版号取得原表。
"""

from __future__ import annotations

import os
from fractions import Fraction
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .alloc import SIM_DAYS, AllocationError, allocate
from .storage import DayRow, StorageError, Store

DB_PATH = os.environ.get("RAIN_DB_PATH", "/data/rain.db")

app = FastAPI(
    title="气象数仓·模拟降雨转公历日表服务",
    version=__version__,
    description=(
        "把单站每月 30 个模拟日雨量按日历重叠权重拆成公历日表，"
        "支持核准日照录、版本封存与补报更正。单位：十分之一毫米。\n\n"
        "日表保证：非负整数、整月总量与模拟值相同、"
        "月内任意连续日期累计量与取整前误差严格小于 1 个单位。"
    ),
)
store = Store(DB_PATH)


# ---------------- 请求/响应模型 ----------------
class ConvertRequest(BaseModel):
    year: int = Field(..., ge=1, le=9999, description="公历年")
    month: int = Field(..., ge=1, le=12, description="公历月")
    sim_rain: list[int] = Field(
        ..., min_length=SIM_DAYS, max_length=SIM_DAYS,
        description=f"恰好 {SIM_DAYS} 个模拟日雨量，非负整数",
    )
    approved: dict[int, int] = Field(
        default_factory=dict,
        description="已核准公历日雨量，键为日序号(1起)，值为非负整数",
    )

    @field_validator("sim_rain")
    @classmethod
    def _sim_non_negative(cls, v: list[int]) -> list[int]:
        if any(x < 0 for x in v):
            raise ValueError("模拟日雨量必须为非负整数")
        return v

    @field_validator("approved")
    @classmethod
    def _approved_non_negative(cls, v: dict[int, int]) -> dict[int, int]:
        if any(x < 0 for x in v.values()):
            raise ValueError("核准雨量必须为非负整数")
        return v


class RecomputeRequest(BaseModel):
    sim_rain: Optional[list[int]] = Field(
        None, min_length=SIM_DAYS, max_length=SIM_DAYS,
        description="可选：更正后的 30 个模拟日雨量；不传则沿用现行版",
    )
    approved: dict[int, int] = Field(
        default_factory=dict,
        description="本次补报/更正的核准日雨量（仅允许作用于未封存日）",
    )

    @field_validator("sim_rain")
    @classmethod
    def _sim_non_negative(cls, v):
        if v is not None and any(x < 0 for x in v):
            raise ValueError("模拟日雨量必须为非负整数")
        return v

    @field_validator("approved")
    @classmethod
    def _approved_non_negative(cls, v: dict[int, int]) -> dict[int, int]:
        if any(x < 0 for x in v.values()):
            raise ValueError("核准雨量必须为非负整数")
        return v


class SealRequest(BaseModel):
    through_day: int = Field(..., ge=1, description="封存从月初到该日（含）")


class DayOut(BaseModel):
    day: int
    amount: int
    source: str
    exact: str = Field(description="取整前精确份额（分数字符串，审计用）")


class TableOut(BaseModel):
    version: int
    year: int
    month: int
    calendar_days: int
    sim_total: int
    table_total: int
    sealed_through: int
    note: Optional[str]
    created_at: str
    days: list[DayOut]


def _rows_to_days(rows: list[DayRow]) -> list[DayOut]:
    return [
        DayOut(
            day=r.day, amount=r.amount, source=r.source,
            exact=str(Fraction(r.exact_num, r.exact_den)),
        )
        for r in rows
    ]


def _result_to_day_rows(result) -> list[DayRow]:
    return [
        DayRow(
            day=r.day, amount=r.amount, source=r.source,
            exact_num=r.exact.numerator, exact_den=r.exact.denominator,
        )
        for r in result.rows
    ]


def _build_table_out(version: int) -> TableOut:
    meta = store.get_version(version)
    if meta is None:
        raise HTTPException(500, f"版本 {version} 数据缺失")
    rows = store.list_days(version)
    return TableOut(
        version=meta.version, year=meta.year, month=meta.month,
        calendar_days=len(rows), sim_total=meta.sim_total,
        table_total=sum(r.amount for r in rows),
        sealed_through=store.sealed_through(version),
        note=meta.note, created_at=meta.created_at,
        days=_rows_to_days(rows),
    )


# ---------------- 端点 ----------------
@app.get("/", tags=["meta"])
def root() -> dict:
    cur = store.get_current()
    return {
        "service": "rain-calendar-service",
        "version": __version__,
        "current_version": None if cur is None else cur.version,
        "docs": "/docs",
    }


@app.get("/health", tags=["meta"])
def health() -> dict:
    cur = store.current_version_no()
    return {"status": "ok", "current_version": cur}


@app.post("/convert", tags=["write"], response_model=TableOut,
          status_code=201,
          summary="初始转换：模拟月 -> 公历日表，生成现行版本")
def convert(req: ConvertRequest) -> TableOut:
    cur = store.get_current()
    if cur is not None and (cur.year, cur.month) != (req.year, req.month):
        raise HTTPException(
            409,
            f"库中现行版本属于 {cur.year}-{cur.month:02d}，"
            f"不能直接改写为 {req.year}-{req.month:02d}；"
            "本服务按单站单月版本线运作，请使用新数据库或联系管理员",
        )
    try:
        result = allocate(req.year, req.month, req.sim_rain, req.approved)
        v = store.write_new_version(
            year=req.year, month=req.month,
            sim_rain=tuple(req.sim_rain), sim_total=result.sim_total,
            rows=_result_to_day_rows(result),
            note=f"初始转换，核准日 {sorted(req.approved)}",
        )
    except AllocationError as exc:
        raise HTTPException(422, f"无解：{exc}") from exc
    except StorageError as exc:
        raise HTTPException(500, f"写入失败，原表继续供应：{exc}") from exc
    return _build_table_out(v)


@app.post("/seal", tags=["write"],
          summary="封存现行版本从月初到指定日的结果")
def seal(req: SealRequest) -> dict:
    cur = store.get_current()
    if cur is None:
        raise HTTPException(409, "尚无现行版本，无法封存")
    rows = store.list_days(cur.version)
    if req.through_day > len(rows):
        raise HTTPException(
            422,
            f"封存日 {req.through_day} 超出该月日历天数 {len(rows)}",
        )
    try:
        info = store.seal_current(req.through_day)
    except StorageError as exc:
        raise HTTPException(409, str(exc)) from exc
    info["sealed_amount"] = sum(
        r.amount for r in rows if r.day <= req.through_day
    )
    info["days"] = [
        {"day": r.day, "amount": r.amount, "source": r.source}
        for r in rows if r.day <= req.through_day
    ]
    return info


@app.post("/recompute", tags=["write"], response_model=TableOut,
          status_code=201,
          summary="补报/更正：仅限未封存日，重算并保留封存值")
def recompute(req: RecomputeRequest) -> TableOut:
    cur = store.get_current()
    if cur is None:
        raise HTTPException(409, "尚无现行版本，请先调用 /convert")
    base_no = cur.version
    seal_through = store.sealed_through(base_no)
    base_rows = store.list_days(base_no)

    touched = sorted(req.approved)
    locked = [d for d in touched if d <= seal_through]
    if locked:
        raise HTTPException(
            422,
            f"日 {locked} 已封存（封存至第 {seal_through} 日），"
            "补报或更正仅限未封存日",
        )

    sim_rain = tuple(req.sim_rain) if req.sim_rain is not None else cur.sim_rain
    # 合并核准信息：基版全部核准日 + 本次补报/更正（未封存日允许覆盖）。
    approved = {r.day: r.amount for r in base_rows if r.source == "approved"}
    approved.update(req.approved)

    # 封存日以整数核准方式钉住，确保重算数值原样保留；来源标签稍后还原。
    base_source = {r.day: r.source for r in base_rows}
    base_amount = {r.day: r.amount for r in base_rows}
    for day, amount in base_amount.items():
        if day <= seal_through:
            approved[day] = amount

    try:
        result = allocate(cur.year, cur.month, sim_rain, approved)
        fixed_rows = []
        for r in result.rows:
            if r.day <= seal_through:
                if r.amount != base_amount[r.day]:
                    raise AllocationError(
                        f"重算改动了已封存日 {r.day} 的值，拒绝写入"
                    )
                # 封存只冻结数值，不改变其“核准/估算”的原始来源
                fixed_rows.append(r.__class__(
                    day=r.day, amount=r.amount,
                    source=base_source[r.day], exact=r.exact))
            else:
                fixed_rows.append(r)
        result = result.__class__(
            year=result.year, month=result.month,
            calendar_days=result.calendar_days, sim_total=result.sim_total,
            approved_total=result.approved_total, rows=tuple(fixed_rows))
        new_no = store.add_version_from(
            base_no, sim_rain=sim_rain, sim_total=result.sim_total,
            rows=_result_to_day_rows(result),
            note=f"补报/更正，本次变更日 {touched}，封存至第 {seal_through} 日",
            seal_through=seal_through,
        )
    except AllocationError as exc:
        raise HTTPException(422, f"无解：{exc}") from exc
    except StorageError as exc:
        raise HTTPException(500, f"写入失败，原表继续供应：{exc}") from exc
    return _build_table_out(new_no)


@app.get("/table", tags=["read"], response_model=TableOut,
         summary="取日表；不传 version 取现行表，区分核准与估算")
def get_table(version: Optional[int] = None,
              source: Optional[str] = None) -> TableOut:
    if source not in (None, "approved", "estimated"):
        raise HTTPException(422, "source 仅支持 approved / estimated")
    no = version if version is not None else store.current_version_no()
    if no is None:
        raise HTTPException(404, "尚无任何版本，请先调用 /convert")
    if store.get_version(no) is None:
        raise HTTPException(404, f"版本 {no} 不存在")
    out = _build_table_out(no)
    if source is not None:
        out.days = [d for d in out.days if d.source == source]
    return out


@app.get("/versions", tags=["read"], summary="列出版本线与封存情况")
def list_versions() -> dict:
    current = store.current_version_no()
    items = []
    for m in store.list_versions():
        items.append({
            "version": m.version, "year": m.year, "month": m.month,
            "sim_total": m.sim_total, "created_at": m.created_at,
            "note": m.note,
            "sealed_through": store.sealed_through(m.version),
            "is_current": m.version == current,
        })
    return {"current_version": current, "versions": items}


@app.get("/cumulative", tags=["read"],
         summary="查询现行（或指定）版本从月初到指定日的累计雨量")
def cumulative(through_day: int, version: Optional[int] = None) -> dict:
    no = version if version is not None else store.current_version_no()
    if no is None:
        raise HTTPException(404, "尚无任何版本，请先调用 /convert")
    meta = store.get_version(no)
    if meta is None:
        raise HTTPException(404, f"版本 {no} 不存在")
    rows = store.list_days(no)
    if not 1 <= through_day <= len(rows):
        raise HTTPException(422, f"through_day 须在 1..{len(rows)} 之间")
    part = [r for r in rows if r.day <= through_day]
    return {
        "version": no,
        "year": meta.year,
        "month": meta.month,
        "through_day": through_day,
        "cumulative_amount": sum(r.amount for r in part),
        "sealed_through": store.sealed_through(no),
        "is_sealed": through_day <= store.sealed_through(no),
        "breakdown": [
            {"day": r.day, "amount": r.amount, "source": r.source}
            for r in part
        ],
    }

"""API 数据模型。金额一律以 *字符串* 收发，杜绝 float 精度问题。"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DECIMAL_RE = re.compile(r"^-?\d+(\.\d+)?$|^-?\.\d+$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_iso_date(value: str, field: str = "日期") -> date:
    """严格解析 ISO 8601 **日期**（YYYY-MM-DD）。

    只接受日期形状，拒绝带时间的字符串（如 ``2026-09-23T00:00:00Z``）与
    不存在的日期（如 ``2026-02-30``）；失败抛 ``ValueError``（API 层映射为 422）。
    """

    text = value.strip()
    if not _ISO_DATE_RE.match(text):
        raise ValueError(f"{field} 必须是 ISO 日期（YYYY-MM-DD），收到 {value!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法日期（YYYY-MM-DD），收到 {value!r}") from exc


class LineIn(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    net: str = Field(description="不含税净额，币种最小单位的整数倍，如 '100.00'")
    tax_rate: str = Field(description="税率，0~1 的小数字符串，如 '0.13'")

    @field_validator("net", "tax_rate")
    @classmethod
    def _check_decimal(cls, v: str, info: Any) -> str:
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"{info.field_name} 必须是十进制数字字符串，收到 {v!r}")
        return v


class InvoiceCreate(BaseModel):
    currency: str = Field(description="发票币种 ISO 4217 代码，如 USD / CNY / JPY")
    lines: list[LineIn] = Field(min_length=1)
    target_currency: str | None = Field(
        default=None, description="可选：换算目标币种；提供时必须同时给 exchange_rate"
    )
    exchange_rate: str | None = Field(
        default=None, description="汇率，1 单位发票币种 = exchange_rate 单位目标币种"
    )
    due_date: str | None = Field(
        default=None, description="可选：到期日，ISO 8601 日期 YYYY-MM-DD（如 '2026-10-01'）"
    )

    @field_validator("currency", "target_currency")
    @classmethod
    def _upper_currency(cls, v: str | None) -> str | None:
        return v.upper().strip() if v is not None else None

    @field_validator("due_date")
    @classmethod
    def _check_due_date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return parse_iso_date(v, "due_date").isoformat()

    @field_validator("exchange_rate")
    @classmethod
    def _check_rate(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"exchange_rate 必须是十进制数字字符串，收到 {v!r}")
        return v


# ---- 出参（宽松模型，extra 允许，主要服务于 OpenAPI 文档）----

class AllocationOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    line_no: int
    delta_units: int
    remainder: str
    rank: int


class RoundingOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: str
    minor_unit: str
    residual_units: int
    residual_amount: str
    method: str
    allocations: list[AllocationOut]


class ConvertedLineOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    currency: str
    net: str
    tax: str
    gross: str


class LineOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    line_no: int
    description: str | None = None
    tax_rate: str
    currency: str
    net: str
    exact_tax: str
    tax: str
    gross: str
    converted: ConvertedLineOut | None = None


class TotalsOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    currency: str
    total_net: str
    total_tax: str
    total_gross: str


class FxOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    target_currency: str
    target_digits: int
    exchange_rate: str
    rate_direction: str
    total_net: str
    total_tax: str
    total_gross: str
    residual_units: int
    residual_unit: str
    allocations: list[AllocationOut]


class InvoiceResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    rule_id: str
    currency: str
    currency_digits: int
    lines: list[LineOut]
    totals: TotalsOut
    rounding: RoundingOut
    fx: FxOut | None = None
    due_date: str | None = None
    created_at: str
    recompute_matched: bool


class InvoiceSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    currency: str
    target_currency: str | None = None
    total_net: str
    total_tax: str
    total_gross: str
    line_count: int
    created_at: str


class PagedInvoices(BaseModel):
    items: list[InvoiceSummary]
    page: int
    page_size: int
    total: int
    pages: int


class Health(BaseModel):
    status: str
    database: str


# ---- 收款登记 ----

class PaymentCreate(BaseModel):
    amount: str = Field(
        description="收款金额，发票币种的十进制字符串，必须大于 0 且为币种最小单位的整数倍，如 '33.11'"
    )

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, v: str) -> str:
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"amount 必须是十进制数字字符串，收到 {v!r}")
        return v


class PaymentOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    invoice_id: int
    amount: str
    currency: str
    created_at: str


class PaymentResult(PaymentOut):
    """登记收款成功后的响应：收款记录 + 最新余额。"""

    paid_total: str
    outstanding: str


class PaymentAllocationCreate(BaseModel):
    """一笔来款在多张同币种待结发票间的分配请求。"""

    currency: str = Field(
        description="来款币种 ISO 4217 代码；只分配给同币种发票，不做跨币种换算，如 USD / JPY"
    )
    amount: str = Field(
        description="来款总金额，十进制字符串，必须大于 0 且为币种最小单位的整数倍，如 '100.00'"
    )
    invoice_ids: list[int] = Field(
        min_length=1,
        description="待结发票 ID 列表，不得重复；只处理列表内发票，"
        "实际按到期日升序（无到期日排最后）、同日按 ID 升序逐张抵扣",
    )

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, v: str) -> str:
        v = v.strip()
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"amount 必须是十进制数字字符串，收到 {v!r}")
        return v

    @field_validator("invoice_ids")
    @classmethod
    def _check_invoice_ids(cls, v: list[int]) -> list[int]:
        for invoice_id in v:
            if invoice_id <= 0:
                raise ValueError(f"invoice_ids 必须是正整数，收到 {invoice_id}")
        if len(v) != len(set(v)):
            raise ValueError(f"invoice_ids 不得包含重复的发票 ID，收到 {v}")
        return v


class PaymentAllocationItem(BaseModel):
    """单张发票在本次来款中的分配结果（按实际抵扣顺序排列）。"""

    model_config = ConfigDict(extra="allow")
    invoice_id: int
    due_date: str | None = None
    allocated: str
    outstanding: str
    payment_id: int | None = None


class PaymentAllocationResult(BaseModel):
    """来款分配成功后的响应：逐发票分配金额与剩余余额。"""

    model_config = ConfigDict(extra="allow")
    currency: str
    amount: str
    allocated_total: str
    created_at: str
    allocations: list[PaymentAllocationItem]


class PaymentOverview(BaseModel):
    """按发票查询的收款记录与未收余额。"""

    model_config = ConfigDict(extra="allow")
    invoice_id: int
    currency: str
    total_gross: str
    paid_total: str
    outstanding: str
    payment_count: int
    payments: list[PaymentOut]


# ---- 逾期查询 ----


class OverdueInvoice(BaseModel):
    """逾期发票条目：到期日早于 as_of 且仍有未收余额。"""

    model_config = ConfigDict(extra="allow")
    id: int
    currency: str
    due_date: str
    total_gross: str
    paid_total: str
    outstanding: str
    created_at: str


class PagedOverdueInvoices(BaseModel):
    items: list[OverdueInvoice]
    page: int
    page_size: int
    total: int
    pages: int
    as_of: str

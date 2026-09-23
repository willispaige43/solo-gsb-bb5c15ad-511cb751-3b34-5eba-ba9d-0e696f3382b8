"""SQLite 持久化层。

存储三张表：
* ``invoices``      —— 发票头（含原始请求 JSON，用于复算）与汇总金额；
* ``invoice_lines`` —— 逐行结果（税额残差分配后的最终值，含换算列）；
* ``payments``      —— 收款登记（每笔收款的金额、币种与登记时间）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    currency        TEXT NOT NULL,
    currency_digits INTEGER NOT NULL,
    target_currency TEXT,
    exchange_rate   TEXT,
    due_date        TEXT,
    total_net       TEXT NOT NULL,
    total_tax       TEXT NOT NULL,
    total_gross     TEXT NOT NULL,
    conv_total_net   TEXT,
    conv_total_tax   TEXT,
    conv_total_gross TEXT,
    residual_units  INTEGER NOT NULL,
    rule_id         TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    line_no      INTEGER NOT NULL,
    description  TEXT,
    tax_rate     TEXT NOT NULL,
    net          TEXT NOT NULL,
    exact_tax    TEXT NOT NULL,
    tax          TEXT NOT NULL,
    gross        TEXT NOT NULL,
    conv_net     TEXT,
    conv_tax     TEXT,
    conv_gross   TEXT,
    UNIQUE(invoice_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_invoices_created_at ON invoices(created_at);

-- 逾期查询只关心已设置到期日的发票
CREATE INDEX IF NOT EXISTS idx_invoices_due_date ON invoices(due_date);

CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    amount     TEXT NOT NULL,
    currency   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_invoice_id ON payments(invoice_id);
"""


class InvoiceNotFoundError(Exception):
    """收款登记/查询时发票不存在。"""

    def __init__(self, invoice_id: int) -> None:
        self.invoice_id = invoice_id
        super().__init__(f"发票 {invoice_id} 不存在")


def utc_now_iso() -> str:
    """UTC 时间，ISO 8601 带 Z 后缀，秒精度。"""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def db_path() -> str:
    return os.getenv("DB_PATH", "/data/invoices.db")


def connect(database: str | None = None) -> sqlite3.Connection:
    path = database or db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # check_same_thread=False：FastAPI 的依赖注入与端点可能运行在线程池的
    # 不同线程；连接始终是每请求一条、单请求内串行使用，跨线程创建/使用是安全的
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    # 兼容 v1.1 旧库：先为已存在的 invoices 表补上 due_date 列，
    # 再执行 CREATE ... IF NOT EXISTS（新库则无表可迁，直接由 SCHEMA 建出）
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='invoices'"
    ).fetchone()
    if table_exists is not None:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)").fetchall()}
        if "due_date" not in columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN due_date TEXT")
            conn.commit()
    conn.executescript(SCHEMA)
    conn.commit()


def save_invoice(conn: sqlite3.Connection, payload: dict[str, Any], result: dict[str, Any]) -> int:
    """在单个事务内写入发票头与行项目，返回发票 id。"""

    with conn:
        cur = conn.execute(
            """
            INSERT INTO invoices (
                currency, currency_digits, target_currency, exchange_rate,
                due_date,
                total_net, total_tax, total_gross,
                conv_total_net, conv_total_tax, conv_total_gross,
                residual_units, rule_id, request_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["currency"],
                result["currency_digits"],
                result.get("fx", {}).get("target_currency"),
                result.get("fx", {}).get("exchange_rate"),
                payload.get("due_date"),
                result["totals"]["total_net"],
                result["totals"]["total_tax"],
                result["totals"]["total_gross"],
                (result.get("fx") or {}).get("total_net"),
                (result.get("fx") or {}).get("total_tax"),
                (result.get("fx") or {}).get("total_gross"),
                result["rounding"]["residual_units"],
                result["rule_id"],
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                utc_now_iso(),
            ),
        )
        invoice_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO invoice_lines (
                invoice_id, line_no, description, tax_rate,
                net, exact_tax, tax, gross,
                conv_net, conv_tax, conv_gross
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    invoice_id,
                    line["line_no"],
                    line.get("description"),
                    line["tax_rate"],
                    line["net"],
                    line["exact_tax"],
                    line["tax"],
                    line["gross"],
                    (line.get("converted") or {}).get("net"),
                    (line.get("converted") or {}).get("tax"),
                    (line.get("converted") or {}).get("gross"),
                )
                for line in result["lines"]
            ],
        )
    return invoice_id


def get_invoice(conn: sqlite3.Connection, invoice_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()


def get_lines(conn: sqlite3.Connection, invoice_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no",
            (invoice_id,),
        ).fetchall()
    )


def list_invoices(
    conn: sqlite3.Connection, page: int, page_size: int
) -> tuple[list[sqlite3.Row], int]:
    total = int(conn.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"])
    offset = (page - 1) * page_size
    rows = list(
        conn.execute(
            "SELECT * FROM invoices ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
    )
    return rows, total


# ---- 逾期查询 ----


def list_overdue_invoices(
    conn: sqlite3.Connection, as_of: date, page: int, page_size: int
) -> tuple[list[dict[str, Any]], int]:
    """逾期发票分页查询。

    逾期口径：``due_date`` 非空且 **严格早于** ``as_of``，且按 Decimal 逐笔求和
    得到的未收余额（total_gross − Σ payments.amount）**大于 0**；缺少到期日、
    已收满（余额 0）的发票不纳入。结果按 id 倒序。

    未收余额不走 SQLite 浮点聚合：一次性取回候选发票的收款流水，在 Python 侧
    用 Decimal 逐发票（每张发票币种单一）求和。
    """

    as_of_text = as_of.isoformat()
    rows = list(
        conn.execute(
            """
            SELECT * FROM invoices
            WHERE due_date IS NOT NULL AND due_date < ?
            ORDER BY id DESC
            """,
            (as_of_text,),
        ).fetchall()
    )
    if not rows:
        return [], 0

    paid_rows = conn.execute(
        "SELECT invoice_id, amount FROM payments WHERE invoice_id IN (%s)"
        % ",".join(str(int(r["id"])) for r in rows)
    ).fetchall()
    paid_map: dict[int, Decimal] = {}
    for pr in paid_rows:
        paid_map[pr["invoice_id"]] = paid_map.get(
            pr["invoice_id"], Decimal("0")
        ) + Decimal(pr["amount"])

    overdue: list[dict[str, Any]] = []
    for r in rows:
        unit = _minor_unit(int(r["currency_digits"]))
        gross = Decimal(r["total_gross"])
        paid = paid_map.get(int(r["id"]), Decimal("0")).quantize(unit)
        outstanding = gross - paid
        if outstanding > 0:
            overdue.append(
                {
                    "id": int(r["id"]),
                    "currency": r["currency"],
                    "due_date": r["due_date"],
                    "total_gross": format(gross, "f"),
                    "paid_total": format(paid, "f"),
                    "outstanding": format(outstanding, "f"),
                    "created_at": r["created_at"],
                }
            )

    total = len(overdue)
    offset = (page - 1) * page_size
    return overdue[offset : offset + page_size], total


# ---- 收款登记 ----


def _minor_unit(digits: int) -> Decimal:
    """某币种的 1 个最小单位，例如 digits=2 -> Decimal('0.01')。"""

    return Decimal(1).scaleb(-digits)


def _paid_total(conn: sqlite3.Connection, invoice_id: int) -> Decimal:
    """已收金额合计。用 Decimal 逐笔求和，不走 SQLite 的浮点 SUM 聚合。"""

    rows = conn.execute(
        "SELECT amount FROM payments WHERE invoice_id = ?", (invoice_id,)
    ).fetchall()
    return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))


def register_payment(
    conn: sqlite3.Connection, invoice_id: int, amount: Decimal
) -> dict[str, Any]:
    """登记一笔收款：余额校验与写入在 **同一个事务** 中完成。

    使用 ``BEGIN IMMEDIATE`` 在事务开始即取得写锁，把并发的收款请求串行化，
    「读已收合计 -> 校验未收余额 -> 写入」之间不会被另一笔收款插队，避免超收。

    * 发票不存在 -> :class:`InvoiceNotFoundError`
    * 金额不是币种最小单位整数倍、或累计收款将超过含税总额 -> ``ValueError``
      （事务回滚，不留下任何记录；由 API 层映射为 422）
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        inv = conn.execute(
            "SELECT currency, currency_digits, total_gross FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if inv is None:
            raise InvoiceNotFoundError(invoice_id)

        digits = int(inv["currency_digits"])
        unit = _minor_unit(digits)
        if (amount / unit) != (amount / unit).to_integral_value():
            raise ValueError(
                f"收款金额 {amount} 不是 {inv['currency']} 最小单位 {unit} 的整数倍"
            )
        amount = amount.quantize(unit)

        gross = Decimal(inv["total_gross"])
        paid = _paid_total(conn, invoice_id)
        outstanding = gross - paid
        if amount > outstanding:
            raise ValueError(
                f"收款 {amount} 超出未收余额 {outstanding}"
                f"（含税总额 {gross}，已收 {paid}），未登记任何记录"
            )

        created_at = utc_now_iso()
        cur = conn.execute(
            """
            INSERT INTO payments (invoice_id, amount, currency, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (invoice_id, format(amount, "f"), inv["currency"], created_at),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    new_paid = paid + amount
    return {
        "id": int(cur.lastrowid),
        "invoice_id": invoice_id,
        "amount": format(amount, "f"),
        "currency": inv["currency"],
        "created_at": created_at,
        "paid_total": format(new_paid, "f"),
        "outstanding": format(gross - new_paid, "f"),
    }


# ---- 来款分配（一笔来款分配到多张待结发票）----


class CurrencyMismatchError(Exception):
    """来款分配时发票币种与来款币种不一致。"""

    def __init__(self, invoice_id: int, expected: str, actual: str) -> None:
        self.invoice_id = invoice_id
        super().__init__(
            f"发票 {invoice_id} 币种为 {actual}，与来款币种 {expected} 不符，"
            "本服务不做跨币种换算"
        )


def _invoices_for_allocation(
    conn: sqlite3.Connection, invoice_ids: list[int], currency: str
) -> list[sqlite3.Row]:
    """取齐待分配发票并按分配顺序排序：到期日升序（NULL 最后）、同日 id 升序。

    发票不存在抛 :class:`InvoiceNotFoundError`；币种与来款不一致抛
    :class:`CurrencyMismatchError`。
    """

    placeholders = ",".join("?" for _ in invoice_ids)
    rows = list(
        conn.execute(
            f"SELECT * FROM invoices WHERE id IN ({placeholders})",
            invoice_ids,
        ).fetchall()
    )
    found = {int(r["id"]) for r in rows}
    missing = next((i for i in invoice_ids if i not in found), None)
    if missing is not None:
        raise InvoiceNotFoundError(missing)

    for r in rows:
        if r["currency"] != currency:
            raise CurrencyMismatchError(int(r["id"]), currency, r["currency"])

    # ISO 日期字符串可直接按字典序比较；NULL 排在所有已设置到期日的发票之后
    rows.sort(
        key=lambda r: (1, "", 0)
        if r["due_date"] is None
        else (0, r["due_date"], int(r["id"]))
    )
    return rows


def allocate_incoming_payment(
    conn: sqlite3.Connection,
    currency: str,
    amount: Decimal,
    invoice_ids: list[int],
    digits: int,
) -> dict[str, Any]:
    """把一笔来款分配到多张待结发票。

    分配规则：按 ``due_date`` 升序（未设置到期日排最后）、同日按 id 升序逐张
    抵扣，每张最多抵扣其当前未收余额（``total_gross − Σ payments.amount``）。
    只处理 ``invoice_ids`` 中的发票，不跨币种换算。

    「读余额 -> 校验总未收 -> 逐张计算 -> 多笔流水写入」全部在同一个
    ``BEGIN IMMEDIATE`` 写事务内完成，与 :func:`register_payment` 共用同一把
    SQLite 写锁，并发收款/分配不会超收。任一校验失败整体回滚，不留下任何
    部分收款记录。

    * 发票不存在 -> :class:`InvoiceNotFoundError`
    * 发票币种与来款不一致 -> :class:`CurrencyMismatchError`
    * 总未收小于来款金额，或金额不是最小单位整数倍 -> ``ValueError``
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = _invoices_for_allocation(conn, invoice_ids, currency)

        unit = _minor_unit(digits)
        if amount <= 0:
            raise ValueError(f"来款金额必须大于 0，收到 {format(amount, 'f')}")
        if (amount / unit) != (amount / unit).to_integral_value():
            raise ValueError(
                f"来款金额 {format(amount, 'f')} 不是 {currency} 最小单位 "
                f"{format(unit, 'f')} 的整数倍"
            )
        amount = amount.quantize(unit)

        # 锁内重新读取每张发票的已收合计（_paid_total 不另开事务，参与本事务的
        # 一致性快照），得到各发票当前未收余额与可抵扣总额
        items: list[dict[str, Any]] = []
        total_outstanding = Decimal("0")
        for r in rows:
            inv_digits = int(r["currency_digits"])
            inv_unit = _minor_unit(inv_digits)
            gross = Decimal(r["total_gross"])
            paid = _paid_total(conn, int(r["id"])).quantize(inv_unit)
            outstanding = max(gross - paid, Decimal("0").quantize(inv_unit))
            total_outstanding += outstanding
            items.append(
                {
                    "id": int(r["id"]),
                    "due_date": r["due_date"],
                    "gross": gross,
                    "paid_before": paid,
                    "outstanding_before": outstanding,
                }
            )

        if amount > total_outstanding:
            raise ValueError(
                f"来款 {format(amount, 'f')} {currency} 超出待结发票总未收余额 "
                f"{format(total_outstanding, 'f')}，整笔拒绝，未登记任何记录"
            )

        # 逐张抵扣：排在前面的发票先吃满，直到来款分完
        remaining = amount
        created_at = utc_now_iso()
        for item in items:
            allocated = min(remaining, item["outstanding_before"])
            item["allocated"] = allocated
            remaining -= allocated
            if allocated > 0:
                cur = conn.execute(
                    """
                    INSERT INTO payments (invoice_id, amount, currency, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (item["id"], format(allocated, "f"), currency, created_at),
                )
                item["payment_id"] = int(cur.lastrowid)
            else:
                item["payment_id"] = None
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    allocations: list[dict[str, Any]] = []
    for item in items:
        new_paid = item["paid_before"] + item["allocated"]
        new_outstanding = item["gross"] - new_paid
        allocations.append(
            {
                "invoice_id": item["id"],
                "due_date": item["due_date"],
                "allocated": format(item["allocated"], "f"),
                "paid_total": format(new_paid, "f"),
                "outstanding": format(new_outstanding, "f"),
                "payment_id": item["payment_id"],
            }
        )

    return {
        "currency": currency,
        "amount": format(amount, "f"),
        "remaining": format(remaining, "f"),
        "allocations": allocations,
        "created_at": created_at,
    }


def payment_overview(conn: sqlite3.Connection, invoice: sqlite3.Row) -> dict[str, Any]:
    """某张发票的收款记录列表与未收余额（供 GET 使用）。"""

    rows = list(
        conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ? ORDER BY id",
            (invoice["id"],),
        ).fetchall()
    )
    unit = _minor_unit(int(invoice["currency_digits"]))
    paid = sum((Decimal(r["amount"]) for r in rows), Decimal("0")).quantize(unit)
    gross = Decimal(invoice["total_gross"])
    return {
        "invoice_id": invoice["id"],
        "currency": invoice["currency"],
        "total_gross": invoice["total_gross"],
        "paid_total": format(paid, "f"),
        "outstanding": format(gross - paid, "f"),
        "payment_count": len(rows),
        "payments": [
            {
                "id": r["id"],
                "invoice_id": r["invoice_id"],
                "amount": r["amount"],
                "currency": r["currency"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }

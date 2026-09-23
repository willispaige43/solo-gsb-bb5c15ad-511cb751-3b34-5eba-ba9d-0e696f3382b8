"""端到端测试：真实 uvicorn 起服务 + stdlib urllib 调用，覆盖
创建 / 查询 / 分页 / 健康检查 / 422 / 复算标志 / SQLite 持久化。

运行：先 pip install -r requirements.txt，再
    python -m unittest tests.test_api -v
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread(threading.Thread):
    def __init__(self, db_file: str, port: int) -> None:
        super().__init__(daemon=True)
        self.db_file = db_file
        self.port = port

    def run(self) -> None:
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=self.port,
            log_level="error",
        )


class ApiTests(unittest.TestCase):
    server: ServerThread
    tmpdir: tempfile.TemporaryDirectory
    base: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_file = str(Path(cls.tmpdir.name) / "test.db")
        os.environ["DB_PATH"] = db_file
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = ServerThread(db_file, port)
        cls.server.start()
        cls._wait_ready()

    @classmethod
    def _wait_ready(cls, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{cls.base}/health", timeout=1) as resp:
                    if resp.status == 200:
                        return
            except Exception as exc:  # 启动期间连接被拒是正常的
                last = exc
                time.sleep(0.2)
        raise RuntimeError(f"服务未在 {timeout}s 内就绪: {last}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmpdir.cleanup()
        os.environ.pop("DB_PATH", None)

    def _request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_01_health(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "database": "up"})

    def test_02_create_and_get(self) -> None:
        payload = {
            "currency": "USD",
            "lines": [
                {"description": "books", "net": "10.05", "tax_rate": "0.10"},
                {"description": "pens", "net": "20.05", "tax_rate": "0.10"},
            ],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertIn("id", body)
        self.assertEqual(body["totals"]["total_tax"], "3.01")
        self.assertEqual(body["rounding"]["residual_units"], -1)
        self.assertTrue(body["recompute_matched"])

        invoice_id = body["id"]
        status2, fetched = self._request("GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(status2, 200)
        self.assertEqual(fetched["totals"], body["totals"])
        self.assertTrue(fetched["recompute_matched"])
        self.assertEqual(len(fetched["lines"]), 2)

    def test_03_create_fx(self) -> None:
        payload = {
            "currency": "USD",
            "target_currency": "JPY",
            "exchange_rate": "150.25",
            "lines": [{"net": "1.00", "tax_rate": "0.10"}],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["fx"]["target_currency"], "JPY")
        # 150.25 -> 150 JPY；税 15.025 -> 15 JPY
        self.assertEqual(body["lines"][0]["converted"]["net"], "150")
        self.assertEqual(body["lines"][0]["converted"]["tax"], "15")
        # 原币保留
        self.assertEqual(body["lines"][0]["net"], "1.00")
        self.assertEqual(body["lines"][0]["tax"], "0.10")

    def test_04_validation_error(self) -> None:
        # 净额不是最小单位整数倍
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "1.005", "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", body)

        # 未知币种
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "ZZZ", "lines": [{"net": "1.00", "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)

        # pydantic 校验：金额必须是字符串
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": 1.0, "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)

    def test_05_not_found(self) -> None:
        status, body = self._request("GET", "/api/v1/invoices/999999")
        self.assertEqual(status, 404)

    def test_06_pagination_and_persistence(self) -> None:
        # 再建 3 张，配合前两张共 5 张
        for i in range(3):
            status, body = self._request(
                "POST",
                "/api/v1/invoices",
                {"currency": "CNY", "lines": [{"net": "100.00", "tax_rate": "0.13"}]},
            )
            self.assertEqual(status, 201)

        status, body = self._request("GET", "/api/v1/invoices?page=1&page_size=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["pages"], 3)
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["page_size"], 2)
        self.assertEqual(len(body["items"]), 2)
        # 按 id 倒序
        self.assertGreater(body["items"][0]["id"], body["items"][1]["id"])
        self.assertEqual(body["items"][0]["line_count"], 1)
        self.assertEqual(body["items"][0]["currency"], "CNY")

        status, body = self._request("GET", "/api/v1/invoices?page=3&page_size=2")
        self.assertEqual(len(body["items"]), 1)

        # 分页参数越界 -> 422
        status, _ = self._request("GET", "/api/v1/invoices?page=0")
        self.assertEqual(status, 422)

    def test_07_persisted_db_file(self) -> None:
        # SQLite 文件确实落盘
        self.assertTrue(os.path.exists(os.environ["DB_PATH"]))
        self.assertGreater(os.path.getsize(os.environ["DB_PATH"]), 0)

    def test_08_payments(self) -> None:
        # 建一张 USD 发票：total_gross = 33.11
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        self.assertEqual(inv["totals"]["total_gross"], "33.11")

        # 初始：无收款，未收余额 = 含税总额
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["total_gross"], "33.11")
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "33.11")
        self.assertEqual(ov["payment_count"], 0)
        self.assertEqual(ov["payments"], [])

        # 登记第一笔收款
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["invoice_id"], invoice_id)
        self.assertEqual(pay["amount"], "20.00")  # 按币种最小单位规范化
        self.assertEqual(pay["currency"], "USD")
        self.assertIn("created_at", pay)
        self.assertEqual(pay["paid_total"], "20.00")
        self.assertEqual(pay["outstanding"], "13.11")

        # 第二笔：恰好收满
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "13.11"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["paid_total"], "33.11")
        self.assertEqual(pay["outstanding"], "0.00")

        # 查询：两笔记录，按登记顺序返回，均带登记时间
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["payment_count"], 2)
        self.assertEqual([p["amount"] for p in ov["payments"]], ["20.00", "13.11"])
        for p in ov["payments"]:
            self.assertIn("created_at", p)
        self.assertEqual(ov["paid_total"], "33.11")
        self.assertEqual(ov["outstanding"], "0.00")

        # 已收满，再收 0.01 -> 422，且不留下记录
        status, body = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "0.01"}
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", body)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["payment_count"], 2)
        self.assertEqual(ov["paid_total"], "33.11")

    def test_09_payment_validation(self) -> None:
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/payments"

        # 超额（累计 > 含税总额 10.00）
        status, _ = self._request("POST", path, {"amount": "10.01"})
        self.assertEqual(status, 422)
        # 零 / 负数
        status, _ = self._request("POST", path, {"amount": "0"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": "-5.00"})
        self.assertEqual(status, 422)
        # 非十进制字符串 / 非字符串
        status, _ = self._request("POST", path, {"amount": "abc"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": 1.5})
        self.assertEqual(status, 422)
        # 小于币种最小单位
        status, _ = self._request("POST", path, {"amount": "0.001"})
        self.assertEqual(status, 422)

        # 以上全部失败，不得留下任何记录
        status, ov = self._request("GET", path)
        self.assertEqual(ov["payment_count"], 0)
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "10.00")

        # 不存在的发票：登记 404、查询 404
        status, _ = self._request(
            "POST", "/api/v1/invoices/999999/payments", {"amount": "1.00"}
        )
        self.assertEqual(status, 404)
        status, _ = self._request("GET", "/api/v1/invoices/999999/payments")
        self.assertEqual(status, 404)

    def test_10_concurrent_payments_no_overpay(self) -> None:
        # total_gross = 100.00，10 个并发请求各付 20.00：
        # 同事务校验+写入，恰好 5 笔成功、5 笔 422，最终不超收
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "100.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]

        codes: list[int] = []
        lock = threading.Lock()

        def pay() -> None:
            code, _ = self._request(
                "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
            )
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=pay) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(codes), [201] * 5 + [422] * 5)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["payment_count"], 5)
        self.assertEqual(ov["paid_total"], "100.00")
        self.assertEqual(ov["outstanding"], "0.00")

    def test_11_due_date_persisted(self) -> None:
        # 带 due_date 创建并持久化，GET 单张时原样返回，且复算仍然一致
        payload = {
            "currency": "USD",
            "due_date": "2026-10-01",
            "lines": [{"net": "10.00", "tax_rate": "0.10"}],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["due_date"], "2026-10-01")
        self.assertTrue(body["recompute_matched"])
        invoice_id = body["id"]

        status, fetched = self._request("GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["due_date"], "2026-10-01")
        self.assertTrue(fetched["recompute_matched"])

        # 不传 due_date：响应中为 null，不影响其他字段
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0.10"}]},
        )
        self.assertEqual(status, 201, body)
        self.assertIsNone(body["due_date"])

        # 非法日期一律 422：格式不符 / 不存在的日期 / 带时间成分
        for bad in ("2026-10", "2026-10-01T00:00:00Z", "2026/10/01",
                    "2026-13-01", "2026-02-30", "not-a-date"):
            status, resp = self._request(
                "POST",
                "/api/v1/invoices",
                {
                    "currency": "USD",
                    "due_date": bad,
                    "lines": [{"net": "10.00", "tax_rate": "0.10"}],
                },
            )
            self.assertEqual(status, 422, bad)
            self.assertIn("detail", resp)

    def test_12_overdue_query(self) -> None:
        # 构造发票集合，固定 as_of=2026-09-23
        def make(due_date, currency="USD", lines=None, pay=None):
            payload = {"currency": currency, "lines": lines or [{"net": "10.00", "tax_rate": "0"}]}
            if due_date is not None:
                payload["due_date"] = due_date
            status, inv = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, inv)
            if pay:
                status, pay_resp = self._request(
                    "POST", f"/api/v1/invoices/{inv['id']}/payments", {"amount": pay}
                )
                self.assertEqual(status, 201, pay_resp)
            return inv["id"]

        id_a = make("2026-09-22", lines=[
            {"net": "10.05", "tax_rate": "0.10"},
            {"net": "20.05", "tax_rate": "0.10"},
        ])  # gross 33.11，逾期未付
        make("2026-09-23")  # due_date == as_of，不逾期
        make("2026-09-10", pay="10.00")  # 已收满，排除
        id_d = make("2026-09-01", currency="JPY",
                    lines=[{"net": "1000", "tax_rate": "0.10"}])  # gross 1100，0 位小数
        id_e = make("2026-09-20",
                    lines=[{"net": "100.00", "tax_rate": "0"}],
                    pay="40.00")  # gross 100.00，部分收款
        make(None)  # 无到期日，排除
        make("2026-12-31")  # 未来到期，排除
        id_h = make("2000-01-01")  # 远古到期，缺省 as_of（UTC 今日）下也必逾期

        # 固定 as_of：共 4 张逾期（早前测试创建的发票均无 due_date，不计入）
        status, body = self._request(
            "GET", "/api/v1/invoices/overdue?as_of=2026-09-23&page=1&page_size=2"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["as_of"], "2026-09-23")
        self.assertEqual(body["total"], 4)
        self.assertEqual(body["pages"], 2)
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["page_size"], 2)
        # 按 id 倒序：H(最晚建) 在前，A 最后
        self.assertEqual([it["id"] for it in body["items"]], [id_h, id_e])

        status, page2 = self._request(
            "GET", "/api/v1/invoices/overdue?as_of=2026-09-23&page=2&page_size=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual([it["id"] for it in page2["items"]], [id_d, id_a])
        self.assertEqual(page2["total"], 4)

        by_id = {it["id"]: it for it in body["items"] + page2["items"]}

        # 部分收款：精确未收余额 = 100.00 - 40.00 = 60.00
        e = by_id[id_e]
        self.assertEqual(e["currency"], "USD")
        self.assertEqual(e["due_date"], "2026-09-20")
        self.assertEqual(e["total_gross"], "100.00")
        self.assertEqual(e["paid_total"], "40.00")
        self.assertEqual(e["outstanding"], "60.00")
        self.assertIn("created_at", e)

        # JPY：0 位小数，金额不带小数点
        d = by_id[id_d]
        self.assertEqual(d["currency"], "JPY")
        self.assertEqual(d["total_gross"], "1100")
        self.assertEqual(d["paid_total"], "0")
        self.assertEqual(d["outstanding"], "1100")

        # 未付 USD 发票
        a = by_id[id_a]
        self.assertEqual(a["total_gross"], "33.11")
        self.assertEqual(a["paid_total"], "0.00")
        self.assertEqual(a["outstanding"], "33.11")

        # 缺省 as_of：取 UTC 当前日期；远古到期的 H 必在结果中
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        status, body = self._request(
            "GET", "/api/v1/invoices/overdue?page_size=100"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["as_of"], today)
        self.assertIn(id_h, [it["id"] for it in body["items"]])
        # 回显的每个条目都必须满足：due_date < as_of、outstanding > 0
        from decimal import Decimal
        for it in body["items"]:
            self.assertLess(it["due_date"], today)
            self.assertGreater(Decimal(it["outstanding"]), 0)

        # 非法 as_of -> 422
        for bad in ("2026-09", "2026-09-23T00:00:00Z", "2026/09/23",
                    "2026-13-01", "not-a-date"):
            status, resp = self._request(
                "GET", f"/api/v1/invoices/overdue?as_of={bad}"
            )
            self.assertEqual(status, 422, bad)
            self.assertIn("detail", resp)

        # 分页参数越界 -> 422
        status, _ = self._request("GET", "/api/v1/invoices/overdue?page=0")
        self.assertEqual(status, 422)
        status, _ = self._request("GET", "/api/v1/invoices/overdue?page_size=101")
        self.assertEqual(status, 422)

    def test_13_incoming_payment_allocation(self) -> None:
        def make(due_date, currency="USD", net="10.00", rate="0"):
            payload = {"currency": currency, "lines": [{"net": net, "tax_rate": rate}]}
            if due_date is not None:
                payload["due_date"] = due_date
            status, inv = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, inv)
            return inv["id"]

        # 待结发票集合（USD）：到期日/含税总额各不相同
        id_a = make("2026-09-01", net="30.10", rate="0.10")   # gross 33.11
        id_b = make("2026-09-10", net="10.00")                # gross 10.00
        id_c = make("2026-09-10", net="20.00")                # gross 20.00 同日，id 更大
        id_d = make(None, net="10.00")                        # 无到期日，排最后
        id_jpy = make("2026-09-01", currency="JPY", net="1000")  # 币种不符用例

        # 50.00 来款：A 吃满 33.11，B 吃满 10.00，余 6.89 分给 C，D 为 0
        status, body = self._request(
            "POST",
            "/api/v1/payments/allocate",
            {"currency": "usd", "amount": "50.00",
             "invoice_ids": [id_d, id_c, id_b, id_a]},  # 输入乱序，不影响分配顺序
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["currency"], "USD")
        self.assertEqual(body["amount"], "50.00")
        self.assertEqual(body["remaining"], "0.00")
        self.assertIn("created_at", body)

        allocs = body["allocations"]
        # 到期日升序、同日 id 升序、NULL 最后
        self.assertEqual([a["invoice_id"] for a in allocs],
                         [id_a, id_b, id_c, id_d])
        self.assertEqual([a["allocated"] for a in allocs],
                         ["33.11", "10.00", "6.89", "0.00"])
        self.assertEqual([a["outstanding"] for a in allocs],
                         ["0.00", "0.00", "13.11", "10.00"])
        self.assertEqual([a["paid_total"] for a in allocs],
                         ["33.11", "10.00", "6.89", "0.00"])
        self.assertIsNone(allocs[3]["payment_id"])  # 未分到款不产生流水
        self.assertTrue(all(a["payment_id"] for a in allocs[:3]))
        # 同日两张按 id 升序
        self.assertLess(allocs[1]["invoice_id"], allocs[2]["invoice_id"])

        # 沿用 payments 流水：单张收款查询必须能看到分配结果
        status, ov = self._request("GET", f"/api/v1/invoices/{id_c}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["payment_count"], 1)
        self.assertEqual(ov["payments"][0]["amount"], "6.89")
        self.assertEqual(ov["payments"][0]["currency"], "USD")
        self.assertEqual(ov["paid_total"], "6.89")
        self.assertEqual(ov["outstanding"], "13.11")

        status, ov = self._request("GET", f"/api/v1/invoices/{id_d}/payments")
        self.assertEqual(ov["payment_count"], 0)

        # 逾期查询也能看到分配后的余额：C 已逾期但仍欠 13.11，A/B 已收满被排除
        status, overdue = self._request(
            "GET", "/api/v1/invoices/overdue?as_of=2026-09-23&page_size=100"
        )
        overdue_by_id = {it["id"]: it for it in overdue["items"]}
        self.assertNotIn(id_a, overdue_by_id)
        self.assertNotIn(id_b, overdue_by_id)
        self.assertEqual(overdue_by_id[id_c]["outstanding"], "13.11")
        self.assertNotIn(id_d, overdue_by_id)  # 无到期日不算逾期

    def test_14_allocation_validation_and_atomicity(self) -> None:
        def make(due_date=None, currency="USD", net="10.00"):
            payload = {"currency": currency, "lines": [{"net": net, "tax_rate": "0"}]}
            if due_date is not None:
                payload["due_date"] = due_date
            status, inv = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, inv)
            return inv["id"]

        id_a = make("2026-09-01")
        id_b = make("2026-09-02")
        id_jpy = make(currency="JPY", net="1000")

        def allocate(amount, ids, currency="USD"):
            return self._request(
                "POST", "/api/v1/payments/allocate",
                {"currency": currency, "amount": amount, "invoice_ids": ids},
            )

        def total_payments(ids):
            total = 0
            for i in ids:
                _, ov = self._request("GET", f"/api/v1/invoices/{i}/payments")
                total += ov["payment_count"]
            return total

        # 列表重复 -> 422
        status, body = allocate("10.00", [id_a, id_a])
        self.assertEqual(status, 422)
        self.assertIn("detail", body)

        # 空列表 -> 422
        status, _ = self._request(
            "POST", "/api/v1/payments/allocate",
            {"currency": "USD", "amount": "10.00", "invoice_ids": []},
        )
        self.assertEqual(status, 422)

        # 发票不存在 -> 404，且不写任何流水
        status, body = allocate("10.00", [id_a, 999999])
        self.assertEqual(status, 404)
        self.assertIn("detail", body)
        self.assertEqual(total_payments([id_a, id_b]), 0)

        # 币种不符 -> 422，整笔回滚（A 也不得被部分收款）
        status, body = allocate("10.00", [id_a, id_jpy])
        self.assertEqual(status, 422)
        self.assertIn("detail", body)
        self.assertEqual(total_payments([id_a, id_b, id_jpy]), 0)

        # 总未收不足（两张共 20.00，来款 20.01）-> 422，不得留下部分收款
        status, body = allocate("20.01", [id_a, id_b])
        self.assertEqual(status, 422)
        self.assertIn("detail", body)
        self.assertEqual(total_payments([id_a, id_b]), 0)

        # 金额非最小单位整数倍 / <=0 / 非字符串 -> 422
        for bad in ("0.001", "0", "-5.00"):
            status, _ = allocate(bad, [id_a])
            self.assertEqual(status, 422, bad)
        status, _ = self._request(
            "POST", "/api/v1/payments/allocate",
            {"currency": "USD", "amount": 5, "invoice_ids": [id_a]},
        )
        self.assertEqual(status, 422)

        # 未知币种 -> 422
        status, _ = allocate("10", [id_a], currency="ZZZ")
        self.assertEqual(status, 422)

        # 以上全部失败后，合法请求仍可正常分配
        status, body = allocate("20.00", [id_a, id_b])
        self.assertEqual(status, 201, body)
        self.assertEqual([a["allocated"] for a in body["allocations"]],
                         ["10.00", "10.00"])

        # JPY（0 位小数）正常分配，金额不带小数点
        status, body = allocate("1000", [id_jpy], currency="JPY")
        self.assertEqual(status, 201, body)
        self.assertEqual(body["allocations"][0]["allocated"], "1000")
        self.assertEqual(body["allocations"][0]["outstanding"], "0")

    def test_15_allocation_concurrent_with_single_payments(self) -> None:
        # gross 100.00 的发票：1 笔 100.00 分配 与 10 笔 20.00 单张收款并发，
        # 写事务串行化 -> 要么分配成功（10 笔单收全拒），要么 5 笔单收成功
        # （分配因总未收不足被拒），最终累计收款恒为 100.00，绝不超收。
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "100.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]

        results: list[tuple[str, int]] = []
        lock = threading.Lock()

        def allocate() -> None:
            code, _ = self._request(
                "POST", "/api/v1/payments/allocate",
                {"currency": "USD", "amount": "100.00",
                 "invoice_ids": [invoice_id]},
            )
            with lock:
                results.append(("alloc", code))

        def pay() -> None:
            code, _ = self._request(
                "POST", f"/api/v1/invoices/{invoice_id}/payments",
                {"amount": "20.00"},
            )
            with lock:
                results.append(("single", code))

        threads = [threading.Thread(target=allocate)]
        threads += [threading.Thread(target=pay) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        alloc_ok = any(kind == "alloc" and code == 201 for kind, code in results)
        single_ok = sum(1 for kind, code in results if kind == "single" and code == 201)
        if alloc_ok:
            self.assertEqual(single_ok, 0)
        else:
            self.assertEqual(single_ok, 5)

        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["paid_total"], "100.00")
        self.assertEqual(ov["outstanding"], "0.00")
        # 流水中所有金额之和（Decimal）等于 100.00
        from decimal import Decimal
        self.assertEqual(
            sum((Decimal(p["amount"]) for p in ov["payments"]), Decimal("0")),
            Decimal("100.00"),
        )


if __name__ == "__main__":
    unittest.main()

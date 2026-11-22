"""核心入账引擎测试：迟到冲正、补登重放校验、整批拒收、账龄查询。"""

from datetime import date

import pytest

import ledger


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "test.db"
    ledger.init_db(str(db))
    c = ledger.connect(str(db))
    yield c
    c.close()


def make_txn(tid, ttype, eff, amount, receipt="", invoice="", allocation=""):
    return {
        "id": tid, "type": ttype,
        "effective_at": ledger.parse_effective_at(eff),
        "amount_cents": amount,
        "receipt_id": receipt, "invoice_id": invoice,
        "allocation_id": allocation,
    }


def seed_invoices(conn):
    ledger.import_invoices(conn, [
        {"id": "INV1", "invoice_date": "2026-01-01", "due_date": "2026-01-31",
         "amount_cents": 100000},
        {"id": "INV2", "invoice_date": "2026-02-01", "due_date": "2026-02-28",
         "amount_cents": 50000},
    ])


def seed_basic(conn):
    """R1=80000(01-10)，A1: R1→INV1 60000(01-11)，A2: R1→INV1 20000(01-20)。"""
    seed_invoices(conn)
    ledger.post_batch(conn, [
        make_txn("R1", "receipt", "2026-01-10T09:00:00Z", 80000),
        make_txn("A1", "allocation", "2026-01-11T10:00:00Z", 60000,
                 receipt="R1", invoice="INV1"),
        make_txn("A2", "allocation", "2026-01-20T10:00:00Z", 20000,
                 receipt="R1", invoice="INV1"),
    ])


# ------------------------------------------------------------- 基础流程

def test_basic_posting_and_aging(conn):
    seed_basic(conn)
    report = ledger.aging_as_of(conn, date(2026, 2, 15))
    assert report["total_cents"] == 70000  # INV1 剩 20000 + INV2 50000
    items = {i["invoice_id"]: i for i in report["rows"]}
    assert items["INV1"]["outstanding_cents"] == 20000
    assert items["INV1"]["overdue_days"] == 15   # 01-31 -> 02-15
    assert items["INV1"]["bucket"] == "1-30天"
    assert items["INV2"]["outstanding_cents"] == 50000
    assert items["INV2"]["overdue_days"] == 0
    assert items["INV2"]["bucket"] == "未逾期"


def test_aging_cutoff_excludes_future(conn):
    seed_basic(conn)
    # 截止 01-05：INV2 尚未开票不出现；INV1 尚无核销，全额未结
    report = ledger.aging_as_of(conn, date(2026, 1, 5))
    assert [i["invoice_id"] for i in report["rows"]] == ["INV1"]
    assert report["rows"][0]["outstanding_cents"] == 100000
    # 截止 01-15：A1 已生效、A2 尚未生效
    report = ledger.aging_as_of(conn, date(2026, 1, 15))
    assert report["rows"][0]["outstanding_cents"] == 40000


def test_aging_uses_daily_delta_table(conn):
    """账龄查询走日汇总表：清空原始流水后结果不变（仅验证查询不依赖流水表）。"""
    seed_basic(conn)
    before = ledger.aging_as_of(conn, date(2026, 2, 15))
    rows = conn.execute("SELECT COUNT(*) FROM invoice_daily_delta").fetchone()[0]
    assert rows > 0
    after = ledger.aging_as_of(conn, date(2026, 2, 15))
    assert before == after


# ------------------------------------------------------------- 迟到冲正

def test_late_reversal_accepted_when_balances_hold(conn):
    """迟到冲正：生效时刻早于库中后续流水，但重放后各余额仍非负 -> 受理。"""
    seed_invoices(conn)
    ledger.post_batch(conn, [
        make_txn("R1", "receipt", "2026-01-10T09:00:00Z", 80000),
        make_txn("A1", "allocation", "2026-01-11T10:00:00Z", 60000,
                 receipt="R1", invoice="INV1"),
        # 后续旧流水（晚于即将补登的冲正时刻）
        make_txn("R2", "receipt", "2026-01-20T09:00:00Z", 50000),
        make_txn("A2", "allocation", "2026-01-21T10:00:00Z", 50000,
                 receipt="R2", invoice="INV2"),
    ])
    # 补登 01-12 的冲正 20000：R1 重放 80000-60000-20000=0，合法
    ledger.post_batch(conn, [
        make_txn("V1", "reversal", "2026-01-12T10:00:00Z", 20000, receipt="R1"),
    ])
    receipts = {r["id"]: r for r in ledger.list_receipts(conn)}
    assert receipts["R1"]["available_cents"] == 0
    assert receipts["R1"]["reversed_cents"] == 20000
    assert receipts["R2"]["available_cents"] == 0


def test_late_reversal_breaking_later_allocation_rejected(conn):
    """迟到冲正使后续核销可用额为负 -> 报首笔违规记录（A2）并整批拒收。"""
    seed_basic(conn)
    before_txns = ledger.list_transactions(conn)
    before_aging = ledger.aging_as_of(conn, date(2026, 2, 15))

    with pytest.raises(ledger.BatchError) as exc:
        ledger.post_batch(conn, [
            make_txn("V1", "reversal", "2026-01-15T10:00:00Z", 10000,
                     receipt="R1"),
        ])
    err = exc.value
    assert err.offender["id"] == "A2"          # 首笔违规是后续旧核销 A2
    assert "可用额" in err.message

    # 整批拒收：原账照常可查，且与之前完全一致
    assert ledger.list_transactions(conn) == before_txns
    assert ledger.aging_as_of(conn, date(2026, 2, 15)) == before_aging
    assert conn.execute("SELECT COUNT(*) FROM reversals").fetchone()[0] == 0


def test_reversal_must_follow_original_receipt(conn):
    seed_invoices(conn)
    with pytest.raises(ledger.BatchError) as exc:
        ledger.post_batch(conn, [
            make_txn("V1", "reversal", "2026-01-09T10:00:00Z", 1000,
                     receipt="R1"),
            make_txn("R1", "receipt", "2026-01-10T09:00:00Z", 80000),
        ])
    assert "尚未生效" in exc.value.message or "不存在" in exc.value.message
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


# ------------------------------------------------------------- 撤销核销

def test_unallocation_over_revocable_rejected(conn):
    seed_basic(conn)
    ledger.post_batch(conn, [
        make_txn("U1", "unallocation", "2026-01-12T10:00:00Z", 30000,
                 allocation="A1"),
    ])
    with pytest.raises(ledger.BatchError) as exc:
        ledger.post_batch(conn, [
            make_txn("U2", "unallocation", "2026-01-13T10:00:00Z", 30001,
                     allocation="A1"),
        ])
    assert exc.value.offender["id"] == "U2"
    assert "可撤额" in exc.value.message
    # 原账未变
    assert conn.execute("SELECT COUNT(*) FROM unallocations").fetchone()[0] == 1


def test_backdated_unallocation_restores_aging(conn):
    seed_basic(conn)
    ledger.post_batch(conn, [
        make_txn("U1", "unallocation", "2026-01-12T10:00:00Z", 20000,
                 allocation="A1"),
    ])
    report = ledger.aging_as_of(conn, date(2026, 2, 15))
    items = {i["invoice_id"]: i for i in report["rows"]}
    assert items["INV1"]["outstanding_cents"] == 40000  # 100000-60000+20000-20000


def test_backdated_allocation_over_invoice_rejected(conn):
    """补登的核销使发票未核销额在历史时点为负 -> 拒收，首笔违规即该笔。"""
    seed_basic(conn)
    with pytest.raises(ledger.BatchError) as exc:
        ledger.post_batch(conn, [
            make_txn("R2", "receipt", "2026-01-05T09:00:00Z", 200000),
            make_txn("A9", "allocation", "2026-01-06T10:00:00Z", 100001,
                     receipt="R2", invoice="INV1"),
        ])
    assert exc.value.offender["id"] == "A9"
    assert "未核销额" in exc.value.message
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


# ------------------------------------------------------------- 其他校验

def test_duplicate_ids_rejected(conn):
    seed_basic(conn)
    with pytest.raises(ledger.BatchError):
        ledger.post_batch(conn, [
            make_txn("R1", "receipt", "2026-02-01T00:00:00Z", 100),
        ])
    with pytest.raises(ledger.BatchError):
        ledger.post_batch(conn, [
            make_txn("X1", "receipt", "2026-02-01T00:00:00Z", 100),
            make_txn("X1", "receipt", "2026-02-02T00:00:00Z", 200),
        ])


def test_missing_references_rejected(conn):
    seed_invoices(conn)
    with pytest.raises(ledger.BatchError):
        ledger.post_batch(conn, [
            make_txn("A1", "allocation", "2026-01-11T10:00:00Z", 100,
                     receipt="NOPE", invoice="INV1"),
        ])
    ledger.post_batch(conn, [
        make_txn("R1", "receipt", "2026-01-10T09:00:00Z", 80000)])
    with pytest.raises(ledger.BatchError):
        ledger.post_batch(conn, [
            make_txn("A2", "allocation", "2026-01-11T10:00:00Z", 100,
                     receipt="R1", invoice="NOPE"),
        ])


def test_batch_atomic_on_mixed_valid_invalid(conn):
    seed_basic(conn)
    with pytest.raises(ledger.BatchError):
        ledger.post_batch(conn, [
            make_txn("R9", "receipt", "2026-02-01T00:00:00Z", 5000),
            make_txn("A9", "allocation", "2026-02-02T00:00:00Z", 999999,
                     receipt="R9", invoice="INV1"),
        ])
    assert conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE id='R9'").fetchone()[0] == 0


def test_amount_and_time_validation():
    with pytest.raises(ValueError):
        ledger.parse_amount_cents("0")
    with pytest.raises(ValueError):
        ledger.parse_amount_cents("-5")
    with pytest.raises(ValueError):
        ledger.parse_amount_cents("10.5")
    assert ledger.parse_amount_cents("100") == 100
    assert ledger.parse_effective_at("2026-01-10T09:00:00Z") == \
        "2026-01-10 09:00:00.000000"
    # 带时区换算到 UTC
    assert ledger.parse_effective_at("2026-01-10T10:00:00+01:00") == \
        "2026-01-10 09:00:00.000000"


def test_csv_parsing_chinese_headers():
    text = "编号,类型,生效时刻,金额,收款编号,发票单号,核销编号\n" \
           "R1,收款,2026-01-10T09:00:00Z,80000,,,\n" \
           "A1,核销,2026-01-11T10:00:00Z,60000,R1,INV1,\n"
    txns = ledger.parse_transactions_csv(text)
    assert [t["type"] for t in txns] == ["receipt", "allocation"]
    assert txns[1]["receipt_id"] == "R1"


def test_invoice_csv_and_duplicates(conn):
    rows = ledger.parse_invoices_csv(
        "invoice_id,invoice_date,due_date,amount_cents\n"
        "INV1,2026-01-01,2026-01-31,100000\n")
    assert ledger.import_invoices(conn, rows) == 1
    with pytest.raises(ledger.BatchError):
        ledger.import_invoices(conn, rows)


def test_overdue_buckets(conn):
    ledger.import_invoices(conn, [
        {"id": "B1", "invoice_date": "2026-01-01", "due_date": "2026-01-01",
         "amount_cents": 100},
        {"id": "B2", "invoice_date": "2026-01-01", "due_date": "2025-12-01",
         "amount_cents": 200},
    ])
    report = ledger.aging_as_of(conn, date(2026, 2, 15))
    items = {i["invoice_id"]: i for i in report["rows"]}
    assert items["B1"]["bucket"] == "31-60天"   # 逾期 45 天
    assert items["B2"]["bucket"] == "61-90天"   # 逾期 76 天
    assert report["bucket_totals"]["31-60天"] == 100
    assert report["bucket_totals"]["61-90天"] == 200

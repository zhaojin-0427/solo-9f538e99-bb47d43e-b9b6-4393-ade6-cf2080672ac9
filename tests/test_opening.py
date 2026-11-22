"""期初导入测试：三表 + 期初日一次性入账、整批拒收、开账后下限与撤销恢复。"""

from datetime import date

import pytest

import ledger


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "opening.db"
    ledger.init_db(str(db))
    c = ledger.connect(str(db))
    yield c
    c.close()


INV_CSV = (
    "invoice_id,invoice_date,due_date,amount_cents,outstanding_cents\n"
    "INV1,2026-01-01,2026-01-31,100000,40000\n"
    "INV2,2026-01-05,2026-02-28,50000,0\n"
)
RCP_CSV = (
    "receipt_id,amount_cents\n"
    "R1,60000\n"
    "R2,10000\n"
)
ALLOC_CSV = (
    "allocation_id,receipt_id,invoice_id,revocable_cents\n"
    "OA1,R1,INV1,10000\n"
)


def make_txn(tid, ttype, eff, amount, receipt="", invoice="", allocation=""):
    return {
        "id": tid, "type": ttype,
        "effective_at": ledger.parse_effective_at(eff),
        "amount_cents": amount,
        "receipt_id": receipt, "invoice_id": invoice,
        "allocation_id": allocation,
    }


def do_opening(conn, day="2026-02-01", inv=INV_CSV, rcp=RCP_CSV, alloc=ALLOC_CSV):
    parsed = ledger.parse_opening_csvs(inv, rcp, alloc)
    return ledger.import_opening(conn, day, parsed)


# ------------------------------------------------------------- 成功路径

def test_opening_imports_all_three_tables_and_date(conn):
    res = do_opening(conn)
    assert res == {"opening_date": "2026-02-01", "invoices": 2,
                   "receipts": 2, "allocations": 1}
    assert ledger.get_opening_date(conn) == "2026-02-01"
    # 期初流水统一记在期初日 00:00:00 UTC
    assert conn.execute("SELECT effective_at FROM receipts WHERE id='R1'"
                        ).fetchone()[0] == "2026-02-01 00:00:00.000000"

    receipts = {r["id"]: r for r in ledger.list_receipts(conn)}
    assert receipts["R1"]["available_cents"] == 60000
    assert receipts["R1"]["is_opening"] == 1
    assert receipts["R2"]["available_cents"] == 10000

    invoices = {i["id"]: i for i in ledger.list_invoices(conn)}
    assert invoices["INV1"]["outstanding_cents"] == 40000
    assert invoices["INV2"]["outstanding_cents"] == 0      # 期初未核销额 0

    # 流水页标注期初项
    txns = {t["id"]: t for t in ledger.list_transactions(conn)}
    assert txns["R1"]["is_opening"] == 1
    assert txns["OA1"]["is_opening"] == 1


def test_opening_ledger_is_closed_after_import(conn):
    do_opening(conn)
    with pytest.raises(ledger.BatchError) as exc:
        do_opening(conn)
    assert "空账" in exc.value.message


def test_opening_zero_outstanding_allowed(conn):
    """未核销额可为 0；可撤额按规范须为正。"""
    do_opening(conn)
    assert ledger.get_opening_date(conn) == "2026-02-01"
    invoices = {i["id"]: i for i in ledger.list_invoices(conn)}
    assert invoices["INV2"]["opening_outstanding_cents"] == 0


# ------------------------------------------------------------- 期初后可引用原单号

def test_post_opening_can_reference_opening_originals_from_day_zero(conn):
    do_opening(conn)
    # 期初日零点起即可引用原单号（核销 R2→INV1、冲正 R1、撤销 OA1）
    ledger.post_batch(conn, [
        make_txn("A1", "allocation", "2026-02-01T00:00:00Z", 1000,
                 receipt="R2", invoice="INV1"),
        make_txn("V1", "reversal", "2026-02-01T00:00:01Z", 1000, receipt="R1"),
        make_txn("U1", "unallocation", "2026-02-01T00:00:02Z", 1000,
                 allocation="OA1"),
    ])
    receipts = {r["id"]: r for r in ledger.list_receipts(conn)}
    assert receipts["R1"]["reversed_cents"] == 1000
    assert receipts["R2"]["available_cents"] == 9000


def test_unallocation_over_opening_revocable_rejected(conn):
    do_opening(conn)   # OA1 可撤额 10000
    with pytest.raises(ledger.BatchError) as exc:
        ledger.post_batch(conn, [
            make_txn("U1", "unallocation", "2026-02-02T10:00:00Z", 10001,
                     allocation="OA1"),
        ])
    assert "可撤额" in exc.value.message


# ------------------------------------------------------------- 校验 / 整批拒收

def test_invoice_date_after_opening_rejected(conn):
    inv = ("invoice_id,invoice_date,due_date,amount_cents,outstanding_cents\n"
           "INV1,2026-02-02,2026-03-01,100000,0\n")
    with pytest.raises(ledger.BatchError) as exc:
        do_opening(conn, inv=inv, rcp="receipt_id,amount_cents\n", alloc=ALLOC_CSV)
    assert "晚于期初日" in exc.value.message
    assert ledger.is_empty_ledger(conn)
    assert ledger.get_opening_date(conn) is None


def test_allocation_endpoints_must_be_in_batch(conn):
    alloc = ("allocation_id,receipt_id,invoice_id,revocable_cents\n"
             "OA1,R9,INV1,1000\n")
    with pytest.raises(ledger.BatchError) as exc:
        do_opening(conn, alloc=alloc)
    assert "R9" in exc.value.message
    assert ledger.is_empty_ledger(conn)

    alloc = ("allocation_id,receipt_id,invoice_id,revocable_cents\n"
             "OA1,R1,INV9,1000\n")
    with pytest.raises(ledger.BatchError) as exc:
        do_opening(conn, alloc=alloc)
    assert "INV9" in exc.value.message


def test_invoice_outstanding_plus_revocable_over_amount_rejected(conn):
    # INV1: 未核销 40000 + 可撤 70000 = 110000 > 100000
    rcp = "receipt_id,amount_cents\nR1,0\nR2,10000\n"
    alloc = ("allocation_id,receipt_id,invoice_id,revocable_cents\n"
             "OA1,R1,INV1,70000\n")
    with pytest.raises(ledger.BatchError) as exc:
        do_opening(conn, rcp=rcp, alloc=alloc)
    assert "超过原金额" in exc.value.message
    assert ledger.is_empty_ledger(conn)


def test_receipt_with_no_available_and_no_revocable_is_skipped(conn):
    # R2 可用 0 且没有可撤核销 -> 只带入有余额/可撤核销的收款，忽略 R2
    rcp = "receipt_id,amount_cents\nR1,60000\nR2,0\n"
    res = do_opening(conn, rcp=rcp)
    assert res["receipts"] == 1
    ids = [r["id"] for r in ledger.list_receipts(conn)]
    assert ids == ["R1"]


def test_invoice_id_may_share_name_with_txn_id(conn):
    """发票单号自成命名空间，可与收款/核销流水编号同名。"""
    inv = ("invoice_id,invoice_date,due_date,amount_cents,outstanding_cents\n"
           "SAME,2026-01-01,2026-01-31,1000,1000\n")
    rcp = "receipt_id,amount_cents\nSAME,500\n"
    res = do_opening(conn, inv=inv, rcp=rcp,
                     alloc="allocation_id,receipt_id,invoice_id,revocable_cents\n")
    assert res["invoices"] == 1 and res["receipts"] == 1


def test_opening_receipt_and_allocation_share_id_rejected(conn):
    alloc = ("allocation_id,receipt_id,invoice_id,revocable_cents\n"
             "R1,R2,INV1,1000\n")   # 核销编号与收款编号同名
    with pytest.raises(ledger.BatchError) as exc:
        do_opening(conn, alloc=alloc)
    assert "重复" in exc.value.message


def test_zero_available_receipt_kept_when_it_has_revocable_allocation(conn):
    # R2 可用 0，但有一笔可撤核销 -> 保留（只带入条件之二满足）
    rcp = "receipt_id,amount_cents\nR1,60000\nR2,0\n"
    alloc = ("allocation_id,receipt_id,invoice_id,revocable_cents\n"
             "OA1,R1,INV1,1000\nOA2,R2,INV1,1000\n")
    res = do_opening(conn, rcp=rcp, alloc=alloc)
    assert res["receipts"] == 2
    assert {r["id"] for r in ledger.list_receipts(conn)} == {"R1", "R2"}


def test_amount_rules_in_opening_parsing():
    # 原金额必须 > 0，未核销额可 0
    with pytest.raises(ledger.BatchError):
        ledger.parse_opening_invoices_csv(
            "invoice_id,invoice_date,due_date,amount_cents,outstanding_cents\n"
            "INV1,2026-01-01,2026-01-31,0,0\n")
    rows = ledger.parse_opening_invoices_csv(
        "单号,开票日,到期日,原金额,未核销额\n"
        "INV1,2026-01-01,2026-01-31,100000,0\n")
    assert rows[0]["opening_outstanding_cents"] == 0
    # 收款可用额可为 0
    assert ledger.parse_opening_receipts_csv(
        "编号,可用额\nR1,0\n")[0]["amount_cents"] == 0
    # 可撤额须为正
    assert ledger.parse_opening_allocations_csv(
        "编号,收款编号,发票单号,可撤额\nA1,R1,INV1,500\n")[0]["revocable_cents"] == 500
    with pytest.raises(ledger.BatchError):
        ledger.parse_opening_allocations_csv(
            "编号,收款编号,发票单号,可撤额\nA1,R1,INV1,0\n")


def test_opening_atomic_on_late_business_error(conn):
    # 期初日/结构都合法，但金额约束失败 -> 一行都不落库
    alloc = ("allocation_id,receipt_id,invoice_id,revocable_cents\n"
             "OA1,R1,INV1,70000\n")
    with pytest.raises(ledger.BatchError):
        do_opening(conn, alloc=alloc)
    for table in ("invoices", "receipts", "allocations",
                  "invoice_daily_delta", "ledger_meta"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


# ------------------------------------------------------------- 开账后下限

def test_transactions_before_opening_rejected(conn):
    do_opening(conn)
    with pytest.raises(ledger.BatchError) as exc:
        ledger.post_batch(conn, [
            make_txn("R9", "receipt", "2026-01-31T23:59:59Z", 1000),
        ])
    assert "早于" in exc.value.message and "期初日" in exc.value.message
    assert conn.execute("SELECT COUNT(*) FROM receipts WHERE id='R9'"
                        ).fetchone()[0] == 0


def test_aging_cutoff_before_opening_rejected(conn):
    do_opening(conn)
    with pytest.raises(ledger.BatchError) as exc:
        ledger.aging_as_of(conn, date(2026, 1, 31))
    assert "早于期初日" in exc.value.message
    # 期初日当天可查
    report = ledger.aging_as_of(conn, date(2026, 2, 1))
    assert report["cutoff"] == "2026-02-01"


def test_ledger_without_opening_has_no_lower_bound(conn):
    """普通上传建账（未做期初导入）：不受期初日下限约束。"""
    ledger.import_invoices(conn, [
        {"id": "INV1", "invoice_date": "2026-01-01", "due_date": "2026-01-31",
         "amount_cents": 1000}])
    assert ledger.get_opening_date(conn) is None
    ledger.post_batch(conn, [
        make_txn("R1", "receipt", "2026-01-02T10:00:00Z", 500)])
    assert ledger.aging_as_of(conn, date(2025, 12, 1))["rows"] == []


def test_old_schema_db_is_migrated(tmp_path):
    """老库（缺少期初列）在 init_db 时自动补列，老数据照常可读。"""
    import sqlite3
    db = str(tmp_path / "old.db")
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE invoices (id TEXT PRIMARY KEY, invoice_date TEXT NOT NULL,
        due_date TEXT NOT NULL, amount_cents INTEGER NOT NULL);
    CREATE TABLE receipts (id TEXT PRIMARY KEY, effective_at TEXT NOT NULL,
        amount_cents INTEGER NOT NULL);
    CREATE TABLE allocations (id TEXT PRIMARY KEY, effective_at TEXT NOT NULL,
        receipt_id TEXT NOT NULL, invoice_id TEXT NOT NULL,
        amount_cents INTEGER NOT NULL);
    CREATE TABLE unallocations (id TEXT PRIMARY KEY, effective_at TEXT NOT NULL,
        allocation_id TEXT NOT NULL, amount_cents INTEGER NOT NULL);
    CREATE TABLE reversals (id TEXT PRIMARY KEY, effective_at TEXT NOT NULL,
        receipt_id TEXT NOT NULL, amount_cents INTEGER NOT NULL);
    CREATE TABLE invoice_daily_delta (invoice_id TEXT NOT NULL, day TEXT NOT NULL,
        delta_cents INTEGER NOT NULL, PRIMARY KEY (invoice_id, day));
    INSERT INTO invoices VALUES ('OLD1','2026-01-01','2026-01-31',1000);
    """)
    c.commit()
    c.close()
    ledger.init_db(db)
    conn = ledger.connect(db)
    try:
        inv = ledger.list_invoices(conn)[0]
        assert inv["id"] == "OLD1" and inv["outstanding_cents"] == 1000
        assert inv["is_opening"] == 0
        assert ledger.get_opening_date(conn) is None
    finally:
        conn.close()


# ------------------------------------------------------------- 撤销结清发票的期初核销

def test_revoke_opening_allocation_restores_aging_and_receipt(conn):
    inv = ("invoice_id,invoice_date,due_date,amount_cents,outstanding_cents\n"
           "INV1,2026-01-01,2026-01-31,100000,0\n")
    # 该收款可用额已全部占用（0），只因此仍有一笔可撤核销而带入期初。
    rcp = "receipt_id,amount_cents\nR1,0\n"
    alloc = ("allocation_id,receipt_id,invoice_id,revocable_cents\n"
             "OA1,R1,INV1,100000\n")
    do_opening(conn, inv=inv, rcp=rcp, alloc=alloc)

    # 期初即结清：账龄无该发票
    report = ledger.aging_as_of(conn, date(2026, 2, 15))
    assert report["rows"] == []
    receipts = {r["id"]: r for r in ledger.list_receipts(conn)}
    assert receipts["R1"]["available_cents"] == 0

    # 撤销期初核销 40000
    ledger.post_batch(conn, [
        make_txn("U1", "unallocation", "2026-02-10T09:00:00Z", 40000,
                 allocation="OA1"),
    ])

    report = ledger.aging_as_of(conn, date(2026, 2, 15))
    items = {i["invoice_id"]: i for i in report["rows"]}
    assert items["INV1"]["outstanding_cents"] == 40000       # 恢复的余额
    receipts = {r["id"]: r for r in ledger.list_receipts(conn)}
    assert receipts["R1"]["available_cents"] == 40000        # 收款可用额恢复

    detail = {a["id"]: a for a in ledger.list_opening_allocations(conn)}
    assert detail["OA1"]["revocable_cents"] == 60000
    assert detail["OA1"]["unallocated_cents"] == 40000


@pytest.fixture()
def client(tmp_path):
    from app import create_app
    app = create_app(str(tmp_path / "opening_web.db"))
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _upload(client, url, text):
    import io
    data = {"file": (io.BytesIO(text.encode("utf-8")), "data.csv")}
    return client.post(url, data=data, content_type="multipart/form-data",
                       follow_redirects=True)


def test_full_opening_flow_via_web(client):
    import io

    def upload_opening(day, inv, rcp, alloc):
        data = {
            "opening_date": day,
            "invoices_file": (io.BytesIO(inv.encode()), "inv.csv"),
            "receipts_file": (io.BytesIO(rcp.encode()), "rcp.csv"),
            "allocations_file": (io.BytesIO(alloc.encode()), "al.csv"),
        }
        return client.post("/opening", data=data,
                           content_type="multipart/form-data",
                           follow_redirects=True)

    r = upload_opening("2026-02-01", INV_CSV, RCP_CSV,
                       "allocation_id,receipt_id,invoice_id,revocable_cents\n"
                       "OA1,R1,INV1,1000\nOA2,R2,INV1,1000\n")
    body = r.get_data(as_text=True)
    assert "期初导入成功" in body
    assert "2026-02-01" in body

    # 再导入被拒
    r = upload_opening("2026-02-01", INV_CSV, RCP_CSV,
                       "allocation_id,receipt_id,invoice_id,revocable_cents\n"
                       "OA1,R1,INV1,1000\n")
    assert "空账" in r.get_data(as_text=True)

    # 期初日之前流水被拒
    r = _upload(client, "/transactions/upload",
                "txn_id,txn_type,effective_at,amount_cents,receipt_id,invoice_id,allocation_id\n"
                "R9,receipt,2026-01-31T10:00:00Z,1000,,,\n")
    assert "早于" in r.get_data(as_text=True)

    # 页面上标注期初项
    assert "期初" in client.get("/transactions").get_data(as_text=True)
    assert "期初" in client.get("/receipts").get_data(as_text=True)
    assert client.get("/opening/allocations").status_code == 200

"""Web 层冒烟测试：上传、迟到冲正拒收提示、账龄页。"""

import io

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "web.db"))
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def upload(client, url, text):
    data = {"file": (io.BytesIO(text.encode("utf-8")), "data.csv")}
    return client.post(url, data=data,
                       content_type="multipart/form-data",
                       follow_redirects=True)


def test_full_flow_via_web(client):
    r = upload(client, "/invoices/upload",
               "invoice_id,invoice_date,due_date,amount_cents\n"
               "INV1,2026-01-01,2026-01-31,100000\n")
    assert r.status_code == 200
    assert "成功导入 1 张发票" in r.get_data(as_text=True)

    r = upload(client, "/transactions/upload",
               "txn_id,txn_type,effective_at,amount_cents,receipt_id,invoice_id,allocation_id\n"
               "R1,receipt,2026-01-10T09:00:00Z,80000,,\n"
               "A1,allocation,2026-01-11T10:00:00Z,60000,R1,INV1,\n")
    assert "成功入账 2 笔流水" in r.get_data(as_text=True)

    # 迟到冲正使后续无足够可用额 -> 页面提示整批拒收，原账可查
    r = upload(client, "/transactions/upload",
               "txn_id,txn_type,effective_at,amount_cents,receipt_id,invoice_id,allocation_id\n"
               "V1,reversal,2026-01-12T10:00:00Z,30000,R1,,\n")
    body = r.get_data(as_text=True)
    assert "整批拒收" in body

    r = client.get("/aging?cutoff=2026-02-15")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "INV1" in body and "400.00" in body  # 未核销余额 40000 分
    assert "15" in body                          # 逾期 15 天

    # 原账照常可查
    assert "R1" in client.get("/transactions").get_data(as_text=True)
    assert "INV1" in client.get("/invoices").get_data(as_text=True)
    assert "R1" in client.get("/receipts").get_data(as_text=True)


def test_pages_render_on_empty_db(client):
    for path in ("/", "/invoices", "/receipts", "/transactions", "/aging",
                 "/invoices/upload", "/transactions/upload"):
        assert client.get(path).status_code == 200

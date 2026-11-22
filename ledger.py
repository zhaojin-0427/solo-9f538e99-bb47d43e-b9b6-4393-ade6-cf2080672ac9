"""应收账龄核心账本引擎。

数据模型（SQLite）：
  invoices            发票主数据（CSV 上传）
  receipts            收款流水
  allocations         核销流水（关联收款 + 发票）
  unallocations       撤销核销流水（引用原核销）
  reversals           银行冲正流水（引用原收款）
  invoice_daily_delta 发票"已核销额"按 UTC 日的增量汇总表（派生数据）

设计要点：
  * 所有流水按 (UTC 生效时刻, 唯一编号) 的全序入账。
  * 补登（迟到流水）入账时，把它与库中旧流水合并后按全序重放，
    逐笔校验：发票未核销额、收款可用额、原核销可撤额都不得为负；
    有违规则报首笔违规记录并整批拒收（事务回滚，原账不受影响）。
  * 账龄查询只读 invoice_daily_delta 汇总表，不重读逐笔流水。
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from datetime import date, datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id           TEXT PRIMARY KEY,
    invoice_date TEXT NOT NULL,              -- YYYY-MM-DD
    due_date     TEXT NOT NULL,              -- YYYY-MM-DD
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS receipts (
    id           TEXT PRIMARY KEY,
    effective_at TEXT NOT NULL,              -- UTC, 'YYYY-MM-DD HH:MM:SS.ffffff'
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS allocations (
    id           TEXT PRIMARY KEY,
    effective_at TEXT NOT NULL,
    receipt_id   TEXT NOT NULL REFERENCES receipts(id),
    invoice_id   TEXT NOT NULL REFERENCES invoices(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS unallocations (
    id            TEXT PRIMARY KEY,
    effective_at  TEXT NOT NULL,
    allocation_id TEXT NOT NULL REFERENCES allocations(id),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS reversals (
    id           TEXT PRIMARY KEY,
    effective_at TEXT NOT NULL,
    receipt_id   TEXT NOT NULL REFERENCES receipts(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
);

-- 派生汇总：某发票在某 UTC 日"已核销额"的净增量（核销 +，撤销核销 -）。
-- 账龄查询按 day <= 截止日 求和即可，无需重读逐笔流水。
CREATE TABLE IF NOT EXISTS invoice_daily_delta (
    invoice_id  TEXT NOT NULL REFERENCES invoices(id),
    day         TEXT NOT NULL,               -- YYYY-MM-DD (UTC)
    delta_cents INTEGER NOT NULL,
    PRIMARY KEY (invoice_id, day)
);
"""

TXN_TABLES = ("receipts", "allocations", "unallocations", "reversals")

TYPE_ALIASES = {
    "receipt": "receipt", "收款": "receipt",
    "allocation": "allocation", "核销": "allocation",
    "unallocation": "unallocation", "撤销核销": "unallocation",
    "reversal": "reversal", "冲正": "reversal",
}

TYPE_LABELS = {
    "receipt": "收款",
    "allocation": "核销",
    "unallocation": "撤销核销",
    "reversal": "银行冲正",
}

INVOICE_HEADERS = {
    "invoice_id": "invoice_id", "单号": "invoice_id",
    "invoice_date": "invoice_date", "开票日": "invoice_date",
    "due_date": "due_date", "到期日": "due_date",
    "amount_cents": "amount_cents", "金额": "amount_cents",
}

TXN_HEADERS = {
    "txn_id": "txn_id", "编号": "txn_id",
    "txn_type": "txn_type", "类型": "txn_type",
    "effective_at": "effective_at", "生效时刻": "effective_at",
    "amount_cents": "amount_cents", "金额": "amount_cents",
    "receipt_id": "receipt_id", "收款编号": "receipt_id",
    "invoice_id": "invoice_id", "发票单号": "invoice_id",
    "allocation_id": "allocation_id", "核销编号": "allocation_id",
}


class BatchError(Exception):
    """整批拒收。offender 为首笔违规/出错记录（dict，可空）。"""

    def __init__(self, message: str, offender: dict | None = None):
        super().__init__(message)
        self.message = message
        self.offender = offender


# ---------------------------------------------------------------- 基础工具

def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def parse_amount_cents(raw: str) -> int:
    """金额一律为正整数分。"""
    s = (raw or "").strip()
    if not s or not s.isascii() or not s.isdigit():
        raise ValueError(f"金额必须为正整数分，得到：{raw!r}")
    value = int(s)
    if value <= 0:
        raise ValueError(f"金额必须为正整数分，得到：{raw!r}")
    return value


def parse_effective_at(raw: str) -> str:
    """解析 UTC 生效时刻，归一化为 'YYYY-MM-DD HH:MM:SS.ffffff'（UTC）。

    字符串字典序即时间序，便于排序与按日切片。无时区的输入按 UTC 处理。
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("生效时刻不能为空")
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"生效时刻格式非法：{raw!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def parse_day(raw: str, field: str) -> str:
    try:
        return date.fromisoformat((raw or "").strip()).isoformat()
    except ValueError:
        raise ValueError(f"{field} 格式非法（应为 YYYY-MM-DD）：{raw!r}")


def _normalize_headers(fieldnames, mapping, required):
    headers = {}
    for name in fieldnames or []:
        key = mapping.get((name or "").strip())
        if key:
            headers[key] = name
    missing = [k for k in required if k not in headers]
    if missing:
        raise BatchError(f"CSV 缺少必需列：{', '.join(missing)}")
    return headers


# ---------------------------------------------------------------- 发票导入

def parse_invoices_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    headers = _normalize_headers(
        reader.fieldnames, INVOICE_HEADERS,
        ("invoice_id", "invoice_date", "due_date", "amount_cents"))
    rows = []
    for lineno, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        try:
            rows.append({
                "id": (row[headers["invoice_id"]] or "").strip(),
                "invoice_date": parse_day(row[headers["invoice_date"]], "开票日"),
                "due_date": parse_day(row[headers["due_date"]], "到期日"),
                "amount_cents": parse_amount_cents(row[headers["amount_cents"]]),
            })
        except ValueError as exc:
            raise BatchError(f"发票 CSV 第 {lineno} 行：{exc}")
        if not rows[-1]["id"]:
            raise BatchError(f"发票 CSV 第 {lineno} 行：单号不能为空")
    return rows


def import_invoices(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """导入发票主数据；单号重复（批内或库中）则整批拒绝。"""
    if not rows:
        raise BatchError("发票 CSV 没有有效数据行")
    seen = set()
    for r in rows:
        if r["id"] in seen:
            raise BatchError(f"发票单号在文件内重复：{r['id']}")
        seen.add(r["id"])
    dup = [r["id"] for r in rows
           if conn.execute("SELECT 1 FROM invoices WHERE id=?", (r["id"],)).fetchone()]
    if dup:
        raise BatchError(f"发票单号已存在：{', '.join(dup)}")
    try:
        conn.executemany(
            "INSERT INTO invoices (id, invoice_date, due_date, amount_cents)"
            " VALUES (:id, :invoice_date, :due_date, :amount_cents)", rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


# ---------------------------------------------------------------- 流水解析

def parse_transactions_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    headers = _normalize_headers(
        reader.fieldnames, TXN_HEADERS,
        ("txn_id", "txn_type", "effective_at", "amount_cents"))

    def cell(row, key):
        name = headers.get(key)
        return (row.get(name) or "").strip() if name else ""

    txns = []
    for lineno, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        where = f"流水 CSV 第 {lineno} 行"
        ttype = TYPE_ALIASES.get(cell(row, "txn_type")) or \
            TYPE_ALIASES.get(cell(row, "txn_type").lower())
        if not ttype:
            raise BatchError(f"{where}：类型非法（应为 收款/核销/撤销核销/冲正 "
                             f"或 receipt/allocation/unallocation/reversal）："
                             f"{cell(row, 'txn_type')!r}")
        try:
            txn = {
                "id": cell(row, "txn_id"),
                "type": ttype,
                "effective_at": parse_effective_at(cell(row, "effective_at")),
                "amount_cents": parse_amount_cents(cell(row, "amount_cents")),
                "receipt_id": cell(row, "receipt_id"),
                "invoice_id": cell(row, "invoice_id"),
                "allocation_id": cell(row, "allocation_id"),
            }
        except ValueError as exc:
            raise BatchError(f"{where}：{exc}")
        if not txn["id"]:
            raise BatchError(f"{where}：编号不能为空")
        if ttype == "allocation" and (not txn["receipt_id"] or not txn["invoice_id"]):
            raise BatchError(f"{where}：核销必须填收款编号和发票单号")
        if ttype == "unallocation" and not txn["allocation_id"]:
            raise BatchError(f"{where}：撤销核销必须填核销编号")
        if ttype == "reversal" and not txn["receipt_id"]:
            raise BatchError(f"{where}：冲正必须填收款编号")
        txns.append(txn)
    return txns


# ---------------------------------------------------------------- 入账引擎

def post_batch(conn: sqlite3.Connection, txns: list[dict]) -> int:
    """整批入账。任一校验失败则回滚，原账不受影响。返回入账笔数。"""
    try:
        _post_batch_inner(conn, txns)
        conn.commit()
        return len(txns)
    except Exception:
        conn.rollback()
        raise


def _post_batch_inner(conn: sqlite3.Connection, txns: list[dict]) -> None:
    if not txns:
        raise BatchError("批次为空")

    # 1) 编号全局唯一（批内 + 库中四类流水）
    seen = set()
    for t in txns:
        if t["id"] in seen:
            raise BatchError(f"批次内编号重复：{t['id']}",
                             offender=_offender(t))
        seen.add(t["id"])
    for t in txns:
        for table in TXN_TABLES:
            if conn.execute(f"SELECT 1 FROM {table} WHERE id=?",
                            (t["id"],)).fetchone():
                raise BatchError(f"编号 {t['id']} 已入账过，不能重复",
                                 offender=_offender(t))

    # 2) 引用检查：原单须存在（库中或本批内）且已生效
    #    （按 (生效时刻, 编号) 序在原单之后）。先于落库执行，
    #    以免外键约束以 IntegrityError 形式报错。
    batch_by_id = {t["id"]: t for t in txns}
    for t in sorted(txns, key=lambda x: (x["effective_at"], x["id"])):
        err = _check_reference(conn, t, batch_by_id)
        if err:
            raise BatchError(err, offender=_offender(t))

    # 3) 落库（同一事务内，后面还要整体校验）
    for t in txns:
        if t["type"] == "receipt":
            conn.execute(
                "INSERT INTO receipts (id, effective_at, amount_cents)"
                " VALUES (?,?,?)",
                (t["id"], t["effective_at"], t["amount_cents"]))
        elif t["type"] == "allocation":
            conn.execute(
                "INSERT INTO allocations (id, effective_at, receipt_id,"
                " invoice_id, amount_cents) VALUES (?,?,?,?,?)",
                (t["id"], t["effective_at"], t["receipt_id"],
                 t["invoice_id"], t["amount_cents"]))
        elif t["type"] == "unallocation":
            conn.execute(
                "INSERT INTO unallocations (id, effective_at, allocation_id,"
                " amount_cents) VALUES (?,?,?,?)",
                (t["id"], t["effective_at"], t["allocation_id"],
                 t["amount_cents"]))
        elif t["type"] == "reversal":
            conn.execute(
                "INSERT INTO reversals (id, effective_at, receipt_id,"
                " amount_cents) VALUES (?,?,?,?)",
                (t["id"], t["effective_at"], t["receipt_id"],
                 t["amount_cents"]))

    # 4) 与库中旧流水合并，按 (生效时刻, 编号) 重放受影响实体，逐笔查非负
    affected_receipts, affected_allocations, affected_invoices = set(), set(), set()
    for t in txns:
        if t["type"] == "receipt":
            affected_receipts.add(t["id"])
        elif t["type"] == "allocation":
            affected_receipts.add(t["receipt_id"])
            affected_invoices.add(t["invoice_id"])
        elif t["type"] == "unallocation":
            affected_allocations.add(t["allocation_id"])
            ref = conn.execute(
                "SELECT receipt_id, invoice_id FROM allocations WHERE id=?",
                (t["allocation_id"],)).fetchone()
            affected_receipts.add(ref["receipt_id"])
            affected_invoices.add(ref["invoice_id"])
        elif t["type"] == "reversal":
            affected_receipts.add(t["receipt_id"])

    violations = []
    violations += _check_receipts(conn, affected_receipts)
    violations += _check_allocations(conn, affected_allocations)
    violations += _check_invoices(conn, affected_invoices)
    if violations:
        violations.sort(key=lambda v: (v["effective_at"], v["id"]))
        first = violations[0]
        raise BatchError(
            f"整批拒收：首笔违规记录 {first['id']}"
            f"（{TYPE_LABELS[first['type']]}，生效于 {first['effective_at']} UTC）"
            f"——{first['reason']}",
            offender=first)

    # 5) 全部通过：维护账龄用的日增量汇总
    _apply_daily_deltas(conn, txns)


def _offender(t: dict) -> dict:
    return {"id": t["id"], "type": t["type"],
            "effective_at": t["effective_at"], "reason": ""}


def _check_reference(conn, t: dict, batch_by_id: dict) -> str | None:
    key = (t["effective_at"], t["id"])
    label = TYPE_LABELS[t["type"]]

    def original_state(table, oid):
        if oid in batch_by_id:                      # 原单在本批内
            orig_eff = batch_by_id[oid]["effective_at"]
        else:
            row = conn.execute(f"SELECT effective_at FROM {table} WHERE id=?",
                               (oid,)).fetchone()
            if row is None:
                return f"{label} {t['id']} 引用的原单 {oid} 不存在"
            orig_eff = row["effective_at"]
        if not ((orig_eff, oid) < key):
            return (f"{label} {t['id']} 入账时原单 {oid} 尚未生效"
                    f"（原单生效于 {orig_eff} UTC）")
        return None

    if t["type"] == "allocation":
        err = original_state("receipts", t["receipt_id"])
        if err:
            return err
        if not conn.execute("SELECT 1 FROM invoices WHERE id=?",
                            (t["invoice_id"],)).fetchone():
            return f"核销 {t['id']} 引用的发票 {t['invoice_id']} 不存在"
    elif t["type"] == "unallocation":
        return original_state("allocations", t["allocation_id"])
    elif t["type"] == "reversal":
        return original_state("receipts", t["receipt_id"])
    return None


def _violation(tid, ttype, eff, reason):
    return {"id": tid, "type": ttype, "effective_at": eff, "reason": reason}


def _check_receipts(conn, receipt_ids) -> list[dict]:
    """收款可用额 = 收款额 - 有效核销 - 冲正，逐笔不得为负。"""
    out = []
    for rid in sorted(receipt_ids):
        r = conn.execute("SELECT amount_cents FROM receipts WHERE id=?",
                         (rid,)).fetchone()
        available = r["amount_cents"]
        events = []
        for a in conn.execute(
                "SELECT id, effective_at, amount_cents FROM allocations"
                " WHERE receipt_id=?", (rid,)):
            events.append((a["effective_at"], a["id"], "allocation",
                           -a["amount_cents"]))
        for u in conn.execute(
                "SELECT u.id, u.effective_at, u.amount_cents FROM unallocations u"
                " JOIN allocations a ON a.id = u.allocation_id"
                " WHERE a.receipt_id=?", (rid,)):
            events.append((u["effective_at"], u["id"], "unallocation",
                           u["amount_cents"]))
        for v in conn.execute(
                "SELECT id, effective_at, amount_cents FROM reversals"
                " WHERE receipt_id=?", (rid,)):
            events.append((v["effective_at"], v["id"], "reversal",
                           -v["amount_cents"]))
        events.sort(key=lambda e: (e[0], e[1]))
        for eff, tid, ttype, delta in events:
            available += delta
            if available < 0:
                out.append(_violation(
                    tid, ttype, eff,
                    f"入账后收款 {rid} 可用额为 {available} 分（为负）"))
                break
    return out


def _check_allocations(conn, allocation_ids) -> list[dict]:
    """原核销可撤额 = 核销额 - 已撤销额，逐笔不得为负。"""
    out = []
    for aid in sorted(allocation_ids):
        a = conn.execute("SELECT amount_cents FROM allocations WHERE id=?",
                         (aid,)).fetchone()
        revocable = a["amount_cents"]
        for u in conn.execute(
                "SELECT id, effective_at, amount_cents FROM unallocations"
                " WHERE allocation_id=? ORDER BY effective_at, id", (aid,)):
            revocable -= u["amount_cents"]
            if revocable < 0:
                out.append(_violation(
                    u["id"], "unallocation", u["effective_at"],
                    f"入账后核销 {aid} 可撤额为 {revocable} 分（为负）"))
                break
    return out


def _check_invoices(conn, invoice_ids) -> list[dict]:
    """发票未核销额 = 发票额 - 有效核销，逐笔不得为负。"""
    out = []
    for iid in sorted(invoice_ids):
        inv = conn.execute("SELECT amount_cents FROM invoices WHERE id=?",
                           (iid,)).fetchone()
        outstanding = inv["amount_cents"]
        events = []
        for a in conn.execute(
                "SELECT id, effective_at, amount_cents FROM allocations"
                " WHERE invoice_id=?", (iid,)):
            events.append((a["effective_at"], a["id"], "allocation",
                           -a["amount_cents"]))
        for u in conn.execute(
                "SELECT u.id, u.effective_at, u.amount_cents FROM unallocations u"
                " JOIN allocations a ON a.id = u.allocation_id"
                " WHERE a.invoice_id=?", (iid,)):
            events.append((u["effective_at"], u["id"], "unallocation",
                           u["amount_cents"]))
        events.sort(key=lambda e: (e[0], e[1]))
        for eff, tid, ttype, delta in events:
            outstanding += delta
            if outstanding < 0:
                out.append(_violation(
                    tid, ttype, eff,
                    f"入账后发票 {iid} 未核销额为 {outstanding} 分（为负）"))
                break
    return out


def _apply_daily_deltas(conn, txns) -> None:
    upsert = (
        "INSERT INTO invoice_daily_delta (invoice_id, day, delta_cents)"
        " VALUES (?,?,?)"
        " ON CONFLICT(invoice_id, day) DO UPDATE"
        " SET delta_cents = delta_cents + excluded.delta_cents")
    for t in txns:
        if t["type"] == "allocation":
            conn.execute(upsert, (t["invoice_id"], t["effective_at"][:10],
                                  t["amount_cents"]))
        elif t["type"] == "unallocation":
            inv = conn.execute(
                "SELECT invoice_id FROM allocations WHERE id=?",
                (t["allocation_id"],)).fetchone()
            conn.execute(upsert, (inv["invoice_id"], t["effective_at"][:10],
                                  -t["amount_cents"]))


# ---------------------------------------------------------------- 账龄查询

BUCKETS = ("未逾期", "1-30天", "31-60天", "61-90天", "90天以上")


def bucket_of(overdue_days: int) -> str:
    if overdue_days <= 0:
        return BUCKETS[0]
    if overdue_days <= 30:
        return BUCKETS[1]
    if overdue_days <= 60:
        return BUCKETS[2]
    if overdue_days <= 90:
        return BUCKETS[3]
    return BUCKETS[4]


def aging_as_of(conn: sqlite3.Connection, cutoff: date) -> dict:
    """截止日（含）当天结束时的未结发票账龄。

    只读 invoices 与 invoice_daily_delta 汇总表，不重读逐笔流水。
    """
    day = cutoff.isoformat()
    rows = conn.execute(
        "SELECT i.id, i.invoice_date, i.due_date, i.amount_cents,"
        " COALESCE((SELECT SUM(d.delta_cents) FROM invoice_daily_delta d"
        "           WHERE d.invoice_id = i.id AND d.day <= ?), 0) AS allocated"
        " FROM invoices i WHERE i.invoice_date <= ?"
        " ORDER BY i.due_date, i.id", (day, day)).fetchall()

    items = []
    bucket_totals = {b: 0 for b in BUCKETS}
    total = 0
    for r in rows:
        outstanding = r["amount_cents"] - r["allocated"]
        if outstanding <= 0:
            continue
        overdue = max(0, (cutoff - date.fromisoformat(r["due_date"])).days)
        bucket = bucket_of(overdue)
        items.append({
            "invoice_id": r["id"],
            "invoice_date": r["invoice_date"],
            "due_date": r["due_date"],
            "amount_cents": r["amount_cents"],
            "outstanding_cents": outstanding,
            "overdue_days": overdue,
            "bucket": bucket,
        })
        bucket_totals[bucket] += outstanding
        total += outstanding
    return {"cutoff": day, "rows": items,
            "bucket_totals": bucket_totals, "total_cents": total}


# ---------------------------------------------------------------- 查询展示

def list_invoices(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT i.id, i.invoice_date, i.due_date, i.amount_cents,"
        " COALESCE((SELECT SUM(d.delta_cents) FROM invoice_daily_delta d"
        "           WHERE d.invoice_id = i.id), 0) AS allocated"
        " FROM invoices i ORDER BY i.id").fetchall()
    return [dict(r, outstanding_cents=r["amount_cents"] - r["allocated"])
            for r in rows]


def list_receipts(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT id, effective_at, amount_cents FROM receipts"
            " ORDER BY effective_at, id"):
        allocated = conn.execute(
            "SELECT COALESCE(SUM(a.amount_cents),0) -"
            " COALESCE((SELECT SUM(u.amount_cents) FROM unallocations u"
            "           JOIN allocations a2 ON a2.id = u.allocation_id"
            "           WHERE a2.receipt_id = ?), 0)"
            " FROM allocations a WHERE a.receipt_id = ?",
            (r["id"], r["id"])).fetchone()[0]
        reversed_ = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM reversals"
            " WHERE receipt_id=?", (r["id"],)).fetchone()[0]
        out.append({
            "id": r["id"], "effective_at": r["effective_at"],
            "amount_cents": r["amount_cents"],
            "allocated_cents": allocated, "reversed_cents": reversed_,
            "available_cents": r["amount_cents"] - allocated - reversed_,
        })
    return out


def list_transactions(conn) -> list[dict]:
    txns = []
    for r in conn.execute("SELECT * FROM receipts"):
        txns.append({"id": r["id"], "type": "receipt",
                     "effective_at": r["effective_at"],
                     "amount_cents": r["amount_cents"], "detail": ""})
    for a in conn.execute("SELECT * FROM allocations"):
        txns.append({"id": a["id"], "type": "allocation",
                     "effective_at": a["effective_at"],
                     "amount_cents": a["amount_cents"],
                     "detail": f"收款 {a['receipt_id']} → 发票 {a['invoice_id']}"})
    for u in conn.execute("SELECT * FROM unallocations"):
        txns.append({"id": u["id"], "type": "unallocation",
                     "effective_at": u["effective_at"],
                     "amount_cents": u["amount_cents"],
                     "detail": f"撤销核销 {u['allocation_id']}"})
    for v in conn.execute("SELECT * FROM reversals"):
        txns.append({"id": v["id"], "type": "reversal",
                     "effective_at": v["effective_at"],
                     "amount_cents": v["amount_cents"],
                     "detail": f"冲正收款 {v['receipt_id']}"})
    txns.sort(key=lambda t: (t["effective_at"], t["id"]))
    for t in txns:
        t["type_label"] = TYPE_LABELS[t["type"]]
    return txns

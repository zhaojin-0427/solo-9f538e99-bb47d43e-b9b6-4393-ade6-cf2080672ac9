"""应收账龄核心账本引擎。

数据模型（SQLite）：
  invoices            发票主数据（CSV 上传）
  receipts            收款流水
  allocations         核销流水（关联收款 + 发票）
  unallocations       撤销核销流水（引用原核销）
  reversals           银行冲正流水（引用原收款）
  invoice_daily_delta 发票"已核销额"按 UTC 日的增量汇总表（派生数据）
  ledger_meta         账簿元数据（期初日 opening_date 等）

设计要点：
  * 所有流水按 (UTC 生效时刻, 唯一编号) 的全序入账。
  * 补登（迟到流水）入账时，把它与库中旧流水合并后按全序重放，
    逐笔校验：发票未核销额、收款可用额、原核销可撤额都不得为负；
    有违规则报首笔违规记录并整批拒收（事务回滚，原账不受影响）。
  * 账龄查询只读 invoice_daily_delta 汇总表，不重读逐笔流水。
  * 期初导入（仅限空账，一次性）：三份 CSV（发票 / 收款 / 仍可撤销的核销）
    与期初日同一事务落库；流水统一记在期初日 00:00:00 UTC，因此期初日零点
    起即可引用原单号办理核销、冲正或撤销。开账后流水生效日与账龄截止日
    均不得早于期初日。
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
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    -- 期初发票的未核销额（NULL 表示非期初发票）。期初时点已核销额
    -- （含仍可撤销部分）已从原金额中扣除，故该值即未核销余额。
    opening_outstanding_cents INTEGER,
    is_opening   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS receipts (
    id           TEXT PRIMARY KEY,
    effective_at TEXT NOT NULL,              -- UTC, 'YYYY-MM-DD HH:MM:SS.ffffff'
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),  -- 期初为可用额
    is_opening   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS allocations (
    id           TEXT PRIMARY KEY,
    effective_at TEXT NOT NULL,
    receipt_id   TEXT NOT NULL REFERENCES receipts(id),
    invoice_id   TEXT NOT NULL REFERENCES invoices(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    is_opening   INTEGER NOT NULL DEFAULT 0
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

-- 账簿元数据：期初日（一次性开账时写入，普通发票上传建账时取最早开票日）。
CREATE TABLE IF NOT EXISTS ledger_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    "原金额": "amount_cents",
}

OPENING_INVOICE_HEADERS = dict(
    INVOICE_HEADERS,
    **{"outstanding_cents": "outstanding_cents",
       "未核销额": "outstanding_cents"})

OPENING_RECEIPT_HEADERS = {
    "receipt_id": "receipt_id", "编号": "receipt_id",
    "amount_cents": "amount_cents", "可用额": "amount_cents", "金额": "amount_cents",
}

OPENING_ALLOCATION_HEADERS = {
    "allocation_id": "allocation_id", "编号": "allocation_id",
    "receipt_id": "receipt_id", "收款编号": "receipt_id",
    "invoice_id": "invoice_id", "发票单号": "invoice_id",
    "revocable_cents": "revocable_cents", "可撤额": "revocable_cents",
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
        _migrate(conn)


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """为既有数据库补齐期初相关列（老库无此列时 ALTER TABLE 添加）。"""
    adds = {
        "invoices": {
            "opening_outstanding_cents": "INTEGER",
            "is_opening": "INTEGER NOT NULL DEFAULT 0",
        },
        "receipts": {"is_opening": "INTEGER NOT NULL DEFAULT 0"},
        "allocations": {"is_opening": "INTEGER NOT NULL DEFAULT 0"},
    }
    for table, cols in adds.items():
        existing = _columns(conn, table)
        for name, decl in cols.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def get_opening_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM ledger_meta WHERE key='opening_date'").fetchone()
    return row["value"] if row else None


def _set_opening_date(conn: sqlite3.Connection, day: str) -> None:
    conn.execute(
        "INSERT INTO ledger_meta (key, value) VALUES ('opening_date', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (day,))


def is_empty_ledger(conn: sqlite3.Connection) -> bool:
    """空账：尚无任何发票或流水。期初导入仅允许在空账上做一次。"""
    for table in ("invoices",) + TXN_TABLES:
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            return False
    return True


def parse_amount_cents(raw: str, *, allow_zero: bool = False) -> int:
    """金额为整数分。默认必须为正；allow_zero=True 时允许 0。"""
    s = (raw or "").strip()
    if not s or not s.isascii() or not s.isdigit():
        raise ValueError(f"金额必须为整数分，得到：{raw!r}")
    value = int(s)
    if value < 0 or (value == 0 and not allow_zero):
        kind = "非负" if allow_zero else "正"
        raise ValueError(f"金额必须为{kind}整数分，得到：{raw!r}")
    return value


def _read_csv_text(data: bytes) -> str:
    """解码上传内容：优先 UTF-8（含 BOM），失败再按 GB18030。"""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError:
            raise ValueError("CSV 文件编码应为 UTF-8")
    return text.lstrip("﻿")


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


# ---------------------------------------------------------------- 期初导入

def parse_opening_invoices_csv(text: str) -> list[dict]:
    """期初发票：单号、开票日、到期日、原金额、未核销额。

    未核销额可为 0（<= 原金额）；开票日须早于（由调用方传入的）期初日。
    """
    reader = csv.DictReader(io.StringIO(text))
    headers = _normalize_headers(
        reader.fieldnames, OPENING_INVOICE_HEADERS,
        ("invoice_id", "invoice_date", "due_date", "amount_cents",
         "outstanding_cents"))
    rows = []
    for lineno, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        where = f"期初发票 CSV 第 {lineno} 行"
        try:
            invoice_id = (row[headers["invoice_id"]] or "").strip()
            amount = parse_amount_cents(row[headers["amount_cents"]])
            outstanding = parse_amount_cents(
                row[headers["outstanding_cents"]], allow_zero=True)
            inv = {
                "id": invoice_id,
                "invoice_date": parse_day(row[headers["invoice_date"]], "开票日"),
                "due_date": parse_day(row[headers["due_date"]], "到期日"),
                "amount_cents": amount,
                "opening_outstanding_cents": outstanding,
            }
        except ValueError as exc:
            raise BatchError(f"{where}：{exc}")
        if not invoice_id:
            raise BatchError(f"{where}：单号不能为空")
        if outstanding > amount:
            raise BatchError(
                f"{where}：未核销额 {outstanding} 分不得超过原金额 {amount} 分")
        rows.append(inv)
    return rows


def parse_opening_receipts_csv(text: str) -> list[dict]:
    """期初收款：编号、可用额（可为 0）。"""
    reader = csv.DictReader(io.StringIO(text))
    headers = _normalize_headers(
        reader.fieldnames, OPENING_RECEIPT_HEADERS,
        ("receipt_id", "amount_cents"))
    rows = []
    for lineno, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        where = f"期初收款 CSV 第 {lineno} 行"
        try:
            rid = (row[headers["receipt_id"]] or "").strip()
            amount = parse_amount_cents(row[headers["amount_cents"]],
                                        allow_zero=True)
        except ValueError as exc:
            raise BatchError(f"{where}：{exc}")
        if not rid:
            raise BatchError(f"{where}：编号不能为空")
        rows.append({"id": rid, "amount_cents": amount})
    return rows


def parse_opening_allocations_csv(text: str) -> list[dict]:
    """期初核销：编号、收款编号、发票单号、可撤额（须为正）。"""
    reader = csv.DictReader(io.StringIO(text))
    headers = _normalize_headers(
        reader.fieldnames, OPENING_ALLOCATION_HEADERS,
        ("allocation_id", "receipt_id", "invoice_id", "revocable_cents"))
    rows = []
    for lineno, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        where = f"期初核销 CSV 第 {lineno} 行"
        try:
            aid = (row[headers["allocation_id"]] or "").strip()
            rid = (row[headers["receipt_id"]] or "").strip()
            iid = (row[headers["invoice_id"]] or "").strip()
            revocable = parse_amount_cents(row[headers["revocable_cents"]])
        except ValueError as exc:
            raise BatchError(f"{where}：{exc}")
        missing = [label for label, val in
                   (("编号", aid), ("收款编号", rid), ("发票单号", iid)) if not val]
        if missing:
            raise BatchError(f"{where}：{'、'.join(missing)}不能为空")
        rows.append({"id": aid, "receipt_id": rid, "invoice_id": iid,
                     "revocable_cents": revocable})
    return rows


def parse_opening_csvs(invoices_text: str, receipts_text: str,
                       allocations_text: str) -> dict:
    """解析三份期初 CSV（发票 / 收款 / 核销）。"""
    return {
        "invoices": parse_opening_invoices_csv(invoices_text),
        "receipts": parse_opening_receipts_csv(receipts_text),
        "allocations": parse_opening_allocations_csv(allocations_text),
    }


def opening_at(day: str) -> str:
    """期初流水的统一生效时刻：期初日 00:00:00 UTC。

    期初日零点起，原单号即可被后续核销、冲正或撤销引用。
    """
    return parse_effective_at(f"{parse_day(day, '期初日')}T00:00:00Z")


def _require_opening_allowed(conn: sqlite3.Connection) -> None:
    if not is_empty_ledger(conn):
        raise BatchError("期初导入仅适用于空账；当前账中已有发票或流水，"
                         "不能再次导入期初")


def import_opening(conn: sqlite3.Connection, opening_day: str,
                   parsed: dict) -> dict:
    """期初一次性入账：三份表 + 期初日同一事务，有错整批拒收。

    校验要点：
      * 仅空账可导入；
      * 批内编号唯一，且发票 / 收款 / 核销编号互不冲突（也不与既有流水冲突）；
      * 开票日不得晚于期初日；
      * 核销两端（收款、发票）必须在本批中；
      * 每张发票：未核销额 + 关联核销可撤额之和 <= 原金额；
      * 只带入还有可用额（>0）或仍可撤销核销（可撤额 > 0）的收款，
        两者皆无的收款被忽略。
    解析层已拒绝语法错误，此处任一业务校验失败均回滚整批。
    """
    try:
        _require_opening_allowed(conn)
        day = parse_day(opening_day, "期初日")
        eff = opening_at(day)
        invoices = parsed["invoices"]
        receipts = parsed["receipts"]
        allocations = parsed["allocations"]

        # --- 批内唯一
        inv_by_id, rcp_by_id, alloc_by_id = {}, {}, {}
        for inv in invoices:
            if inv["id"] in inv_by_id:
                raise BatchError(f"期初发票单号在文件内重复：{inv['id']}")
            inv_by_id[inv["id"]] = inv
        for rcp in receipts:
            if rcp["id"] in rcp_by_id:
                raise BatchError(f"期初收款编号在文件内重复：{rcp['id']}")
            rcp_by_id[rcp["id"]] = rcp
        for alloc in allocations:
            if alloc["id"] in alloc_by_id:
                raise BatchError(f"期初核销编号在文件内重复：{alloc['id']}")
            alloc_by_id[alloc["id"]] = alloc

        # 编号唯一：发票单号自成命名空间；收款 / 核销编号属于流水命名空间，
        # 二者之间不得重名，也不得与既有流水重名（与 post_batch 的全局唯一
        # 语义一致；核销通过 invoice_id 列引用发票，故发票单号可与流水编号同名）。
        for oid in inv_by_id:
            if conn.execute("SELECT 1 FROM invoices WHERE id=?",
                            (oid,)).fetchone():
                raise BatchError(f"期初发票单号 {oid} 已存在")

        txn_ids = {}
        for kind, table, label in (
                ("receipt", rcp_by_id, "收款编号"),
                ("allocation", alloc_by_id, "核销编号")):
            for oid in table:
                if oid in txn_ids:
                    raise BatchError(
                        f"流水编号 {oid} 在期初数据中重复（{txn_ids[oid]} 与 {label}）")
                txn_ids[oid] = label
        for oid in txn_ids:
            for table in TXN_TABLES:
                if conn.execute(f"SELECT 1 FROM {table} WHERE id=?",
                                (oid,)).fetchone():
                    raise BatchError(f"编号 {oid} 已入账过，不能作为期初编号")

        # --- 开票日不得晚于期初日
        for inv in invoices:
            if inv["invoice_date"] > day:
                raise BatchError(
                    f"期初发票 {inv['id']} 的开票日 {inv['invoice_date']} "
                    f"晚于期初日 {day}")

        # --- 核销引用：两端须在本批
        for alloc in allocations:
            if alloc["receipt_id"] not in rcp_by_id:
                raise BatchError(
                    f"期初核销 {alloc['id']} 引用的收款 {alloc['receipt_id']} "
                    f"不在本次期初收款表中")
            if alloc["invoice_id"] not in inv_by_id:
                raise BatchError(
                    f"期初核销 {alloc['id']} 引用的发票 {alloc['invoice_id']} "
                    f"不在本次期初发票表中")

        # --- 金额约束：发票侧，未核销额 + 仍可撤销的核销额 <= 原金额
        #     （收款侧的可用额已是核销后的余额，可撤额只是其中可撤销的部分，
        #      两者不可相加比较；故仅校验发票侧。）
        inv_revocable, rcp_revocable = {}, {}
        for alloc in allocations:
            inv_revocable[alloc["invoice_id"]] = \
                inv_revocable.get(alloc["invoice_id"], 0) + alloc["revocable_cents"]
            rcp_revocable[alloc["receipt_id"]] = \
                rcp_revocable.get(alloc["receipt_id"], 0) + alloc["revocable_cents"]

        for inv in invoices:
            total = inv["opening_outstanding_cents"] + \
                inv_revocable.get(inv["id"], 0)
            if total > inv["amount_cents"]:
                raise BatchError(
                    f"期初发票 {inv['id']}：未核销额 "
                    f"{inv['opening_outstanding_cents']} 分 + 关联核销可撤额 "
                    f"{inv_revocable.get(inv['id'], 0)} 分 = {total} 分，"
                    f"超过原金额 {inv['amount_cents']} 分")

        # --- 只带入还有可用额（>0）或仍可撤销核销（可撤额 >0）的收款；
        #     两者皆无的收款不计入期初。其可撤额为 0，故不会成为任何核销的
        #     一端（核销两端须在本批），剔除安全。
        receipts = [rcp for rcp in receipts
                    if rcp["amount_cents"] > 0
                    or rcp_revocable.get(rcp["id"], 0) > 0]

        # --- 落库（同一事务）
        conn.executemany(
            "INSERT INTO invoices (id, invoice_date, due_date, amount_cents,"
            " opening_outstanding_cents, is_opening) VALUES (?,?,?,?,?,1)",
            [(inv["id"], inv["invoice_date"], inv["due_date"],
              inv["amount_cents"], inv["opening_outstanding_cents"])
             for inv in invoices])
        conn.executemany(
            "INSERT INTO receipts (id, effective_at, amount_cents, is_opening)"
            " VALUES (?,?,?,1)",
            [(rcp["id"], eff, rcp["amount_cents"]) for rcp in receipts])
        conn.executemany(
            "INSERT INTO allocations (id, effective_at, receipt_id, invoice_id,"
            " amount_cents, is_opening) VALUES (?,?,?,?,?,1)",
            [(alloc["id"], eff, alloc["receipt_id"], alloc["invoice_id"],
              alloc["revocable_cents"]) for alloc in allocations])

        # 期初余额（未核销额 / 可用额）已含期初核销的净影响，不再写入日汇总；
        # 之后撤销期初核销时，unallocation 会在其生效日写入正向增量。
        _set_opening_date(conn, day)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"opening_date": day, "invoices": len(invoices),
            "receipts": len(receipts), "allocations": len(allocations)}

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

    # 0) 开账后流水生效日不得早于期初日（仅约束新入账流水，期初流水除外）。
    opening = get_opening_date(conn)
    if opening:
        for t in sorted(txns, key=lambda x: (x["effective_at"], x["id"])):
            if t["effective_at"][:10] < opening:
                raise BatchError(
                    f"流水 {t['id']} 生效日 {t['effective_at'][:10]} 早于"
                    f"期初日 {opening}，不能入账",
                    offender=_offender(t))

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
        is_opening = False
        if oid in batch_by_id:                      # 原单在本批内
            orig_eff = batch_by_id[oid]["effective_at"]
        else:
            row = conn.execute(
                f"SELECT effective_at, is_opening FROM {table} WHERE id=?",
                (oid,)).fetchone()
            if row is None:
                return f"{label} {t['id']} 引用的原单 {oid} 不存在"
            orig_eff = row["effective_at"]
            is_opening = bool(row["is_opening"])
        # 期初原单统一生效于期初日 00:00:00 UTC；自期初日零点起即可引用，
        # 允许与期初原单同一时刻引用（不受编号字典序限制）。
        if is_opening and orig_eff == t["effective_at"]:
            return None
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
    """收款可用额 = 收款额 - 有效核销 - 冲正，逐笔不得为负。

    期初收款的可用额已是期初余额；期初核销（is_opening=1）已体现在该余额
    中，不作为重放扣减事件，只在其被撤销时以 unallocation 加回。
    """
    out = []
    for rid in sorted(receipt_ids):
        r = conn.execute("SELECT amount_cents FROM receipts WHERE id=?",
                         (rid,)).fetchone()
        available = r["amount_cents"]
        events = []
        for a in conn.execute(
                "SELECT id, effective_at, amount_cents FROM allocations"
                " WHERE receipt_id=? AND is_opening=0", (rid,)):
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
        inv = conn.execute(
            "SELECT amount_cents, opening_outstanding_cents, is_opening"
            " FROM invoices WHERE id=?", (iid,)).fetchone()
        # 期初发票的未核销额已是期初余额；期初核销已体现在其中，
        # 不作为重放扣减事件，只在其被撤销时以 unallocation 加回。
        outstanding = inv["opening_outstanding_cents"] if inv["is_opening"] \
            else inv["amount_cents"]
        events = []
        for a in conn.execute(
                "SELECT id, effective_at, amount_cents FROM allocations"
                " WHERE invoice_id=? AND is_opening=0", (iid,)):
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
    有期初日的账簿，截止日不得早于期初日。
    """
    opening = get_opening_date(conn)
    if opening and cutoff.isoformat() < opening:
        raise BatchError(
            f"账龄截止日 {cutoff.isoformat()} 早于期初日 {opening}，不能查询")
    day = cutoff.isoformat()
    rows = conn.execute(
        "SELECT i.id, i.invoice_date, i.due_date, i.amount_cents,"
        " i.opening_outstanding_cents, i.is_opening,"
        " COALESCE((SELECT SUM(d.delta_cents) FROM invoice_daily_delta d"
        "           WHERE d.invoice_id = i.id AND d.day <= ?), 0) AS allocated"
        " FROM invoices i WHERE i.invoice_date <= ?"
        " ORDER BY i.due_date, i.id", (day, day)).fetchall()

    items = []
    bucket_totals = {b: 0 for b in BUCKETS}
    total = 0
    for r in rows:
        base = r["opening_outstanding_cents"] if r["is_opening"] \
            else r["amount_cents"]
        outstanding = base - r["allocated"]
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
        " i.opening_outstanding_cents, i.is_opening,"
        " COALESCE((SELECT SUM(d.delta_cents) FROM invoice_daily_delta d"
        "           WHERE d.invoice_id = i.id), 0) AS allocated"
        " FROM invoices i ORDER BY i.id").fetchall()
    out = []
    for r in rows:
        base = r["opening_outstanding_cents"] if r["is_opening"] \
            else r["amount_cents"]
        outstanding = base - r["allocated"]
        # 期初发票：已核销额 = 原金额 - 未核销余额（含期初已核销的不可撤部分）。
        out.append(dict(r, outstanding_cents=outstanding,
                        allocated_cents=r["amount_cents"] - outstanding))
    return out


def list_receipts(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT id, effective_at, amount_cents, is_opening FROM receipts"
            " ORDER BY effective_at, id"):
        # 期初收款的 amount_cents 已是期初可用额，期初核销已含在其中：
        # 只把「期初后」（非期初）的核销计为扣减；任何撤销核销都加回。
        allocated = conn.execute(
            "SELECT COALESCE(SUM(a.amount_cents),0) FROM allocations a"
            " WHERE a.receipt_id = ? AND a.is_opening = 0",
            (r["id"],)).fetchone()[0]
        unallocated = conn.execute(
            "SELECT COALESCE(SUM(u.amount_cents),0) FROM unallocations u"
            " JOIN allocations a ON a.id = u.allocation_id"
            " WHERE a.receipt_id = ?", (r["id"],)).fetchone()[0]
        reversed_ = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM reversals"
            " WHERE receipt_id=?", (r["id"],)).fetchone()[0]
        out.append({
            "id": r["id"], "effective_at": r["effective_at"],
            "amount_cents": r["amount_cents"], "is_opening": r["is_opening"],
            "allocated_cents": allocated - unallocated,
            "reversed_cents": reversed_,
            "available_cents": r["amount_cents"] - allocated + unallocated
                               - reversed_,
        })
    return out


def list_opening_allocations(conn) -> list[dict]:
    """期初核销明细：含原核销额（可撤销）、已撤销额与是否仍可撤销。

    供「撤销已结清发票上的期初核销」操作选择目标，并展示可撤销性。
    """
    out = []
    for a in conn.execute(
            "SELECT id, effective_at, receipt_id, invoice_id, amount_cents"
            " FROM allocations WHERE is_opening=1 ORDER BY id"):
        unallocated = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM unallocations"
            " WHERE allocation_id=?", (a["id"],)).fetchone()[0]
        out.append({
            "id": a["id"], "effective_at": a["effective_at"],
            "receipt_id": a["receipt_id"], "invoice_id": a["invoice_id"],
            "amount_cents": a["amount_cents"],
            "unallocated_cents": unallocated,
            "revocable_cents": a["amount_cents"] - unallocated,
        })
    return out


def list_transactions(conn) -> list[dict]:
    txns = []
    for r in conn.execute("SELECT * FROM receipts"):
        txns.append({"id": r["id"], "type": "receipt",
                     "effective_at": r["effective_at"],
                     "amount_cents": r["amount_cents"], "detail": "",
                     "is_opening": r["is_opening"]})
    for a in conn.execute("SELECT * FROM allocations"):
        txns.append({"id": a["id"], "type": "allocation",
                     "effective_at": a["effective_at"],
                     "amount_cents": a["amount_cents"],
                     "detail": f"收款 {a['receipt_id']} → 发票 {a['invoice_id']}",
                     "is_opening": a["is_opening"]})
    for u in conn.execute("SELECT * FROM unallocations"):
        txns.append({"id": u["id"], "type": "unallocation",
                     "effective_at": u["effective_at"],
                     "amount_cents": u["amount_cents"],
                     "detail": f"撤销核销 {u['allocation_id']}",
                     "is_opening": 0})
    for v in conn.execute("SELECT * FROM reversals"):
        txns.append({"id": v["id"], "type": "reversal",
                     "effective_at": v["effective_at"],
                     "amount_cents": v["amount_cents"],
                     "detail": f"冲正收款 {v['receipt_id']}",
                     "is_opening": 0})
    txns.sort(key=lambda t: (t["effective_at"], t["id"]))
    for t in txns:
        t["type_label"] = TYPE_LABELS[t["type"]]
    return txns

"""Flask 应收账龄应用。"""

from __future__ import annotations

import os
from datetime import date

from flask import (Flask, flash, g, redirect, render_template, request,
                   url_for)

import ledger


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")
    app.config["DB_PATH"] = db_path or os.environ.get(
        "LEDGER_DB", os.path.join(os.path.dirname(__file__), "ledger.db"))
    ledger.init_db(app.config["DB_PATH"])

    def get_db():
        if "db" not in g:
            g.db = ledger.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def read_upload() -> str:
        f = request.files.get("file")
        if not f or not f.filename:
            raise ledger.BatchError("请选择要上传的 CSV 文件")
        return f.read().decode("utf-8-sig")

    @app.route("/")
    def index():
        conn = get_db()
        stats = {
            "invoices": conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
            "receipts": conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
            "allocations": conn.execute("SELECT COUNT(*) FROM allocations").fetchone()[0],
            "unallocations": conn.execute("SELECT COUNT(*) FROM unallocations").fetchone()[0],
            "reversals": conn.execute("SELECT COUNT(*) FROM reversals").fetchone()[0],
        }
        return render_template("index.html", stats=stats, today=date.today())

    @app.route("/invoices")
    def invoices():
        return render_template("invoices.html",
                               invoices=ledger.list_invoices(get_db()))

    @app.route("/invoices/upload", methods=["GET", "POST"])
    def upload_invoices():
        if request.method == "POST":
            try:
                rows = ledger.parse_invoices_csv(read_upload())
                n = ledger.import_invoices(get_db(), rows)
                flash(f"成功导入 {n} 张发票。", "ok")
                return redirect(url_for("invoices"))
            except ledger.BatchError as exc:
                get_db().rollback()
                flash(f"导入失败：{exc.message}", "error")
            except UnicodeDecodeError:
                flash("导入失败：文件编码应为 UTF-8。", "error")
        return render_template("upload_invoices.html")

    @app.route("/receipts")
    def receipts():
        return render_template("receipts.html",
                               receipts=ledger.list_receipts(get_db()))

    @app.route("/transactions")
    def transactions():
        return render_template("transactions.html",
                               txns=ledger.list_transactions(get_db()))

    @app.route("/transactions/upload", methods=["GET", "POST"])
    def upload_transactions():
        if request.method == "POST":
            try:
                txns = ledger.parse_transactions_csv(read_upload())
                n = ledger.post_batch(get_db(), txns)
                flash(f"成功入账 {n} 笔流水。", "ok")
                return redirect(url_for("transactions"))
            except ledger.BatchError as exc:
                flash(f"{exc.message}（原账未改动，可照常查询）", "error")
            except UnicodeDecodeError:
                flash("入账失败：文件编码应为 UTF-8。", "error")
        return render_template("upload_transactions.html")

    @app.route("/aging")
    def aging():
        raw = request.args.get("cutoff", "").strip()
        try:
            cutoff = date.fromisoformat(raw) if raw else date.today()
        except ValueError:
            flash("截止日格式应为 YYYY-MM-DD。", "error")
            cutoff = date.today()
        report = ledger.aging_as_of(get_db(), cutoff)
        return render_template("aging.html", report=report,
                               buckets=ledger.BUCKETS)

    return app

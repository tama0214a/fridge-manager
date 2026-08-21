"""研究室冷蔵庫管理 — メインアプリケーション。

共用PCのブラウザから使うローカルWebアプリ。起動方法は start.bat（Windows）。
データはすべて data/ フォルダ（SQLite + config.json）に保存される。
"""
from __future__ import annotations

import atexit
import csv
import io
import os
import re
import sqlite3
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta

from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, url_for)

from config import DEFAULTS, load_config, save_config
from db import BACKUP_DIR, DATA_DIR, DB_PATH, connect, init_db
from notifier import APP_NAME, run_notify_job, send_mail, start_notifier_thread

app = Flask(__name__)
app.secret_key = os.urandom(24)

STATUS_STORED = "保管中"
STATUS_OUT = "出庫済"
STATUS_DISCARDED = "破棄済"

# 冷蔵庫内の位置の選択肢（1〜3段 × 左右）
POSITIONS = [
    "1段目 左", "1段目 右",
    "2段目 左", "2段目 右",
    "3段目 左", "3段目 右",
]

ITEM_SELECT = """
SELECT i.*, r.name AS owner_name, f.name AS fridge_name, f.temp AS fridge_temp,
       ob.name AS out_by_name
FROM items i
JOIN researchers r ON r.id = i.owner_id
JOIN fridges f     ON f.id = i.fridge_id
LEFT JOIN researchers ob ON ob.id = i.out_by_id
"""


# ---------------------------------------------------------------- 共通ヘルパー

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_history(conn, action, item_id=None, item_name="", actor="", detail=""):
    conn.execute(
        "INSERT INTO history (ts, action, item_id, item_name, actor, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now_str(), action, item_id, item_name, actor, detail),
    )


def active_researchers(conn):
    return conn.execute(
        "SELECT * FROM researchers WHERE active = 1 ORDER BY name"
    ).fetchall()


def active_fridges(conn):
    return conn.execute(
        "SELECT * FROM fridges WHERE active = 1 ORDER BY name"
    ).fetchall()


def researcher_name(conn, rid) -> str:
    row = conn.execute("SELECT name FROM researchers WHERE id = ?", (rid,)).fetchone()
    return row["name"] if row else "不明"


def fridge_name(conn, fid) -> str:
    row = conn.execute("SELECT name FROM fridges WHERE id = ?", (fid,)).fetchone()
    return row["name"] if row else "不明"


def parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def expiry_state(row, warn_days: int):
    """期限の状態を (state, label) で返す。state: expired / warn / ok / none / closed"""
    if row["status"] != STATUS_STORED:
        return "closed", ""
    if not row["expiry_date"]:
        return "none", "期限なし"
    diff = (date.fromisoformat(row["expiry_date"]) - date.today()).days
    if diff < 0:
        return "expired", f"{-diff}日超過"
    if diff == 0:
        return "warn", "本日期限"
    if diff <= warn_days:
        return "warn", f"あと{diff}日"
    return "ok", f"あと{diff}日"


def decorate(rows, warn_days: int):
    out = []
    for row in rows:
        vm = dict(row)
        vm["exp_state"], vm["exp_label"] = expiry_state(row, warn_days)
        out.append(vm)
    return out


def decorate_one(row, warn_days: int):
    return decorate([row], warn_days)[0]


def _filtered_items(conn, args):
    """一覧・CSVで共用する検索。 (rows, status, fridge_id, owner_id, q) を返す。"""
    status = (args.get("status") or "").strip() or STATUS_STORED
    fridge_id = args.get("fridge_id", type=int)
    owner_id = args.get("owner_id", type=int)
    q = (args.get("q") or "").strip()

    where, params = [], []
    if status in (STATUS_STORED, STATUS_OUT, STATUS_DISCARDED):
        where.append("i.status = ?")
        params.append(status)
    else:
        status = "all"
    if fridge_id:
        where.append("i.fridge_id = ?")
        params.append(fridge_id)
    if owner_id:
        where.append("i.owner_id = ?")
        params.append(owner_id)
    if q:
        where.append("(i.name LIKE ? OR i.detail LIKE ? OR i.position LIKE ?)")
        params += [f"%{q}%"] * 3

    sql = ITEM_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    if status == STATUS_STORED:
        sql += " ORDER BY (i.expiry_date IS NULL), i.expiry_date, i.id"
    else:
        sql += " ORDER BY i.updated_at DESC, i.id DESC"
    rows = conn.execute(sql, params).fetchall()
    return rows, status, fridge_id, owner_id, q


# ---------------------------------------------------------------- ダッシュボード

@app.route("/")
def dashboard():
    conn = get_db()
    cfg = load_config()
    warn_days = int(cfg["warn_days"])
    today = date.today().isoformat()
    warn_until = (date.today() + timedelta(days=warn_days)).isoformat()

    stored_count = conn.execute(
        "SELECT COUNT(*) AS c FROM items WHERE status = ?", (STATUS_STORED,)
    ).fetchone()["c"]
    expired = conn.execute(
        ITEM_SELECT + " WHERE i.status = ? AND i.expiry_date IS NOT NULL "
        "AND i.expiry_date < ? ORDER BY i.expiry_date",
        (STATUS_STORED, today),
    ).fetchall()
    warning = conn.execute(
        ITEM_SELECT + " WHERE i.status = ? AND i.expiry_date IS NOT NULL "
        "AND i.expiry_date >= ? AND i.expiry_date <= ? ORDER BY i.expiry_date",
        (STATUS_STORED, today, warn_until),
    ).fetchall()
    per_fridge = conn.execute(
        """
        SELECT f.id, f.name, f.temp, f.location,
               SUM(CASE WHEN i.status = '保管中' THEN 1 ELSE 0 END) AS stored
        FROM fridges f
        LEFT JOIN items i ON i.fridge_id = f.id
        WHERE f.active = 1
        GROUP BY f.id
        ORDER BY f.name
        """
    ).fetchall()
    recent = conn.execute(
        "SELECT * FROM history ORDER BY id DESC LIMIT 8"
    ).fetchall()
    has_masters = bool(
        conn.execute("SELECT COUNT(*) AS c FROM fridges WHERE active = 1").fetchone()["c"]
    ) and bool(
        conn.execute("SELECT COUNT(*) AS c FROM researchers WHERE active = 1").fetchone()["c"]
    )
    return render_template(
        "index.html", active_page="dash", cfg=cfg,
        stored_count=stored_count,
        expired=decorate(expired, warn_days),
        warning=decorate(warning, warn_days),
        per_fridge=per_fridge, recent=recent, has_masters=has_masters,
    )


# ---------------------------------------------------------------- 入庫登録

@app.route("/items/new", methods=["GET", "POST"])
def item_new():
    conn = get_db()
    cfg = load_config()
    researchers = active_researchers(conn)
    fridges = active_fridges(conn)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        owner_id = request.form.get("owner_id", type=int)
        fridge_id = request.form.get("fridge_id", type=int)
        stored = parse_date(request.form.get("stored_date")) or date.today()
        no_expiry = request.form.get("no_expiry") == "1"
        expiry = None if no_expiry else parse_date(request.form.get("expiry_date"))
        if not no_expiry and expiry is None:
            expiry = stored + timedelta(days=int(cfg["default_storage_days"]))

        errors = []
        if not name:
            errors.append("品名を入力してください。")
        if not owner_id or not conn.execute(
            "SELECT 1 FROM researchers WHERE id = ? AND active = 1", (owner_id,)
        ).fetchone():
            errors.append("所有者（登録する人）を選択してください。")
        if not fridge_id or not conn.execute(
            "SELECT 1 FROM fridges WHERE id = ? AND active = 1", (fridge_id,)
        ).fetchone():
            errors.append("冷蔵庫を選択してください。")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "item_new.html", active_page="new", cfg=cfg,
                researchers=researchers, fridges=fridges, positions=POSITIONS,
                today=date.today().isoformat(),
                default_expiry=(date.today() + timedelta(days=int(cfg["default_storage_days"]))).isoformat(),
                form=request.form,
            )

        ts = now_str()
        cur = conn.execute(
            """
            INSERT INTO items (name, detail, quantity, position, owner_id, fridge_id,
                               stored_date, expiry_date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                (request.form.get("detail") or "").strip(),
                (request.form.get("quantity") or "").strip(),
                (request.form.get("position") or "").strip(),
                owner_id, fridge_id,
                stored.isoformat(),
                expiry.isoformat() if expiry else None,
                STATUS_STORED, ts, ts,
            ),
        )
        owner = researcher_name(conn, owner_id)
        log_history(
            conn, "入庫", cur.lastrowid, name, owner,
            f"{fridge_name(conn, fridge_id)} に入庫（期限: "
            f"{expiry.isoformat() if expiry else 'なし'}）",
        )
        conn.commit()
        flash(f"「{name}」を入庫登録しました。続けて登録できます。", "success")
        return redirect(url_for("item_new"))

    if not researchers or not fridges:
        flash("先にマスタ管理で「冷蔵庫」と「研究者」を登録してください。", "error")
        return redirect(url_for("masters"))
    return render_template(
        "item_new.html", active_page="new", cfg=cfg,
        researchers=researchers, fridges=fridges, positions=POSITIONS,
        today=date.today().isoformat(),
        default_expiry=(date.today() + timedelta(days=int(cfg["default_storage_days"]))).isoformat(),
        form=None,
    )


# ---------------------------------------------------------------- 一覧

@app.route("/items")
def items():
    conn = get_db()
    cfg = load_config()
    rows, status, fridge_id, owner_id, q = _filtered_items(conn, request.args)
    return render_template(
        "items.html", active_page="items", cfg=cfg,
        items=decorate(rows, int(cfg["warn_days"])), count=len(rows),
        researchers=active_researchers(conn), fridges=active_fridges(conn),
        status=status, sel_fridge=fridge_id, sel_owner=owner_id, q=q,
        st_stored=STATUS_STORED, st_out=STATUS_OUT, st_disc=STATUS_DISCARDED,
    )


# ---------------------------------------------------------------- 出庫・破棄

@app.route("/items/<int:item_id>/out", methods=["GET", "POST"])
def item_out(item_id):
    conn = get_db()
    cfg = load_config()
    row = conn.execute(ITEM_SELECT + " WHERE i.id = ?", (item_id,)).fetchone()
    if row is None:
        flash("アイテムが見つかりません。", "error")
        return redirect(url_for("items"))
    if row["status"] != STATUS_STORED:
        flash("このアイテムはすでに出庫・破棄済みです。", "error")
        return redirect(url_for("items"))

    researchers = active_researchers(conn)
    if request.method == "POST":
        out_type = request.form.get("out_type")
        by_id = request.form.get("out_by_id", type=int)
        out_date = parse_date(request.form.get("out_date")) or date.today()
        note = (request.form.get("out_note") or "").strip()

        errors = []
        if out_type not in ("破棄", "使用", "その他"):
            errors.append("種別を選択してください。")
        if not by_id or not conn.execute(
            "SELECT 1 FROM researchers WHERE id = ?", (by_id,)
        ).fetchone():
            errors.append("実施者を選択してください。")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            new_status = STATUS_DISCARDED if out_type == "破棄" else STATUS_OUT
            conn.execute(
                "UPDATE items SET status = ?, out_date = ?, out_by_id = ?, "
                "out_note = ?, updated_at = ? WHERE id = ?",
                (new_status, out_date.isoformat(), by_id, note, now_str(), item_id),
            )
            actor = researcher_name(conn, by_id)
            action = "破棄" if out_type == "破棄" else "出庫"
            detail = {"破棄": "破棄", "使用": "使用のため取り出し", "その他": "その他"}[out_type]
            if note:
                detail += f"（{note}）"
            log_history(conn, action, item_id, row["name"], actor, detail)
            conn.commit()
            flash(f"「{row['name']}」を{action}登録しました。", "success")
            return redirect(url_for("items"))

    return render_template(
        "item_out.html", active_page="items", cfg=cfg,
        item=decorate_one(row, int(cfg["warn_days"])),
        researchers=researchers, today=date.today().isoformat(),
    )


# ---------------------------------------------------------------- 編集・削除

EDIT_LABELS = {
    "name": "品名", "detail": "メモ", "quantity": "数量", "position": "位置",
    "owner_id": "所有者", "fridge_id": "冷蔵庫",
    "stored_date": "入庫日", "expiry_date": "保管期限",
}


@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
def item_edit(item_id):
    conn = get_db()
    cfg = load_config()
    row = conn.execute(ITEM_SELECT + " WHERE i.id = ?", (item_id,)).fetchone()
    if row is None:
        flash("アイテムが見つかりません。", "error")
        return redirect(url_for("items"))
    researchers = active_researchers(conn)
    fridges = active_fridges(conn)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        owner_id = request.form.get("owner_id", type=int)
        fridge_id = request.form.get("fridge_id", type=int)
        actor_id = request.form.get("actor_id", type=int)
        stored = parse_date(request.form.get("stored_date")) or parse_date(row["stored_date"])
        no_expiry = request.form.get("no_expiry") == "1"
        expiry = None if no_expiry else parse_date(request.form.get("expiry_date"))
        revert = request.form.get("revert") == "1" and row["status"] != STATUS_STORED

        errors = []
        if not name:
            errors.append("品名を入力してください。")
        if not owner_id or not conn.execute(
            "SELECT 1 FROM researchers WHERE id = ?", (owner_id,)
        ).fetchone():
            errors.append("所有者を選択してください。")
        if not fridge_id or not conn.execute(
            "SELECT 1 FROM fridges WHERE id = ?", (fridge_id,)
        ).fetchone():
            errors.append("冷蔵庫を選択してください。")
        if not actor_id or not conn.execute(
            "SELECT 1 FROM researchers WHERE id = ?", (actor_id,)
        ).fetchone():
            errors.append("操作者（あなた）を選択してください。")
        if not no_expiry and expiry is None:
            errors.append("保管期限を入力するか「期限なし」を選択してください。")

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            new_values = {
                "name": name,
                "detail": (request.form.get("detail") or "").strip(),
                "quantity": (request.form.get("quantity") or "").strip(),
                "position": (request.form.get("position") or "").strip(),
                "owner_id": owner_id,
                "fridge_id": fridge_id,
                "stored_date": stored.isoformat(),
                "expiry_date": expiry.isoformat() if expiry else None,
            }
            changes = []
            for key, new in new_values.items():
                old = row[key]
                if old != new:
                    if key == "owner_id":
                        changes.append(f"所有者: {researcher_name(conn, old)}→{researcher_name(conn, new)}")
                    elif key == "fridge_id":
                        changes.append(f"冷蔵庫: {fridge_name(conn, old)}→{fridge_name(conn, new)}")
                    elif key in ("stored_date", "expiry_date"):
                        changes.append(f"{EDIT_LABELS[key]}: {old or 'なし'}→{new or 'なし'}")
                    else:
                        changes.append(EDIT_LABELS[key])

            actor = researcher_name(conn, actor_id)
            if revert:
                conn.execute(
                    "UPDATE items SET status = ?, out_date = NULL, out_by_id = NULL, "
                    "out_note = '', updated_at = ? WHERE id = ?",
                    (STATUS_STORED, now_str(), item_id),
                )
                log_history(conn, "取消", item_id, name, actor, "出庫・破棄を取り消して保管中に戻した")
            conn.execute(
                "UPDATE items SET name = ?, detail = ?, quantity = ?, position = ?, "
                "owner_id = ?, fridge_id = ?, stored_date = ?, expiry_date = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    new_values["name"], new_values["detail"], new_values["quantity"],
                    new_values["position"], new_values["owner_id"], new_values["fridge_id"],
                    new_values["stored_date"], new_values["expiry_date"],
                    now_str(), item_id,
                ),
            )
            if changes:
                log_history(conn, "編集", item_id, name, actor, "変更: " + "、".join(changes))
            conn.commit()
            flash(f"「{name}」を更新しました。", "success")
            return redirect(url_for("items"))

    return render_template(
        "item_edit.html", active_page="items", cfg=cfg,
        item=decorate_one(row, int(cfg["warn_days"])),
        researchers=researchers, fridges=fridges, positions=POSITIONS,
        form=request.form if request.method == "POST" else None,
    )


@app.route("/items/<int:item_id>/delete", methods=["POST"])
def item_delete(item_id):
    conn = get_db()
    row = conn.execute(ITEM_SELECT + " WHERE i.id = ?", (item_id,)).fetchone()
    if row is None:
        flash("アイテムが見つかりません。", "error")
        return redirect(url_for("items"))
    actor_id = request.form.get("actor_id", type=int)
    if not actor_id or not conn.execute(
        "SELECT 1 FROM researchers WHERE id = ?", (actor_id,)
    ).fetchone():
        flash("削除するには操作者を選択してください。", "error")
        return redirect(url_for("item_edit", item_id=item_id))
    snapshot = (
        f"誤登録のため削除（{row['fridge_name']}、入庫 {row['stored_date']}、"
        f"所有者 {row['owner_name']}）"
    )
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    log_history(conn, "削除", item_id, row["name"], researcher_name(conn, actor_id), snapshot)
    conn.commit()
    flash(f"「{row['name']}」を削除しました（履歴には記録が残ります）。", "success")
    return redirect(url_for("items"))


# ---------------------------------------------------------------- 履歴

@app.route("/history")
def history():
    conn = get_db()
    q = (request.args.get("q") or "").strip()
    sel_action = (request.args.get("action") or "").strip()
    where, params = [], []
    if q:
        where.append("(item_name LIKE ? OR actor LIKE ? OR detail LIKE ?)")
        params += [f"%{q}%"] * 3
    if sel_action:
        where.append("action = ?")
        params.append(sel_action)
    sql = "SELECT * FROM history"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT 500"
    rows = conn.execute(sql, params).fetchall()
    actions = [
        r["action"]
        for r in conn.execute("SELECT DISTINCT action FROM history ORDER BY action")
    ]
    return render_template(
        "history.html", active_page="history",
        rows=rows, q=q, sel_action=sel_action, actions=actions,
    )


# ---------------------------------------------------------------- マスタ管理

@app.route("/masters")
def masters():
    conn = get_db()
    return render_template(
        "masters.html", active_page="masters",
        fridges=conn.execute("SELECT * FROM fridges ORDER BY active DESC, name").fetchall(),
        researchers=conn.execute("SELECT * FROM researchers ORDER BY active DESC, name").fetchall(),
    )


@app.route("/fridges/add", methods=["POST"])
def fridge_add():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("冷蔵庫の名前を入力してください。", "error")
    else:
        conn = get_db()
        conn.execute(
            "INSERT INTO fridges (name, location, temp) VALUES (?, ?, ?)",
            (name, (request.form.get("location") or "").strip(),
             (request.form.get("temp") or "").strip()),
        )
        conn.commit()
        flash(f"冷蔵庫「{name}」を追加しました。", "success")
    return redirect(url_for("masters"))


@app.route("/fridges/<int:fid>/update", methods=["POST"])
def fridge_update(fid):
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("冷蔵庫の名前は空にできません。", "error")
    else:
        conn = get_db()
        conn.execute(
            "UPDATE fridges SET name = ?, location = ?, temp = ? WHERE id = ?",
            (name, (request.form.get("location") or "").strip(),
             (request.form.get("temp") or "").strip(), fid),
        )
        conn.commit()
        flash(f"冷蔵庫「{name}」を更新しました。", "success")
    return redirect(url_for("masters"))


@app.route("/fridges/<int:fid>/toggle", methods=["POST"])
def fridge_toggle(fid):
    conn = get_db()
    row = conn.execute("SELECT * FROM fridges WHERE id = ?", (fid,)).fetchone()
    if row:
        conn.execute(
            "UPDATE fridges SET active = ? WHERE id = ?", (0 if row["active"] else 1, fid)
        )
        conn.commit()
        flash(
            f"冷蔵庫「{row['name']}」を{'無効化' if row['active'] else '有効化'}しました。",
            "success",
        )
    return redirect(url_for("masters"))


@app.route("/fridges/<int:fid>/delete", methods=["POST"])
def fridge_delete(fid):
    conn = get_db()
    row = conn.execute("SELECT * FROM fridges WHERE id = ?", (fid,)).fetchone()
    if row is None:
        flash("冷蔵庫が見つかりません。", "error")
        return redirect(url_for("masters"))
    used = conn.execute(
        "SELECT COUNT(*) AS c FROM items WHERE fridge_id = ?", (fid,)
    ).fetchone()["c"]
    if used:
        flash(
            f"冷蔵庫「{row['name']}」には入出庫記録が{used}件あるため削除できません。"
            "記録の追跡性を保つため、代わりに「無効化」を使ってください"
            "（新しい入庫先として選べなくなり、一覧の下にグレー表示されます）。",
            "error",
        )
    else:
        conn.execute("DELETE FROM fridges WHERE id = ?", (fid,))
        log_history(conn, "削除", None, "", "",
                    f"未使用の冷蔵庫「{row['name']}」をマスタから削除")
        conn.commit()
        flash(f"冷蔵庫「{row['name']}」を削除しました。", "success")
    return redirect(url_for("masters"))


@app.route("/researchers/add", methods=["POST"])
def researcher_add():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    if not name:
        flash("研究者の氏名を入力してください。", "error")
    elif email and "@" not in email:
        flash("メールアドレスの形式が正しくありません。", "error")
    else:
        conn = get_db()
        conn.execute(
            "INSERT INTO researchers (name, email) VALUES (?, ?)", (name, email)
        )
        conn.commit()
        flash(f"研究者「{name}」を追加しました。", "success")
    return redirect(url_for("masters"))


@app.route("/researchers/<int:rid>/update", methods=["POST"])
def researcher_update(rid):
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    if not name:
        flash("研究者の氏名は空にできません。", "error")
    elif email and "@" not in email:
        flash("メールアドレスの形式が正しくありません。", "error")
    else:
        conn = get_db()
        conn.execute(
            "UPDATE researchers SET name = ?, email = ? WHERE id = ?", (name, email, rid)
        )
        conn.commit()
        flash(f"研究者「{name}」を更新しました。", "success")
    return redirect(url_for("masters"))


@app.route("/researchers/<int:rid>/toggle", methods=["POST"])
def researcher_toggle(rid):
    conn = get_db()
    row = conn.execute("SELECT * FROM researchers WHERE id = ?", (rid,)).fetchone()
    if row:
        conn.execute(
            "UPDATE researchers SET active = ? WHERE id = ?",
            (0 if row["active"] else 1, rid),
        )
        conn.commit()
        flash(
            f"研究者「{row['name']}」を{'無効化' if row['active'] else '有効化'}しました。",
            "success",
        )
    return redirect(url_for("masters"))


@app.route("/researchers/<int:rid>/delete", methods=["POST"])
def researcher_delete(rid):
    conn = get_db()
    row = conn.execute("SELECT * FROM researchers WHERE id = ?", (rid,)).fetchone()
    if row is None:
        flash("研究者が見つかりません。", "error")
        return redirect(url_for("masters"))
    used = conn.execute(
        "SELECT COUNT(*) AS c FROM items WHERE owner_id = ? OR out_by_id = ?",
        (rid, rid),
    ).fetchone()["c"]
    if used:
        flash(
            f"研究者「{row['name']}」には入出庫記録が{used}件あるため削除できません。"
            "記録の追跡性を保つため、代わりに「無効化」を使ってください。",
            "error",
        )
    else:
        conn.execute("DELETE FROM researchers WHERE id = ?", (rid,))
        log_history(conn, "削除", None, "", "",
                    f"未使用の研究者「{row['name']}」をマスタから削除")
        conn.commit()
        flash(f"研究者「{row['name']}」を削除しました。", "success")
    return redirect(url_for("masters"))


# ---------------------------------------------------------------- 設定

@app.route("/settings", methods=["GET", "POST"])
def settings():
    cfg = load_config()
    if request.method == "POST":
        val = request.form.get("default_storage_days", type=int)
        if val is not None and val >= 1:
            cfg["default_storage_days"] = val
        val = request.form.get("warn_days", type=int)
        if val is not None and val >= 0:
            cfg["warn_days"] = val
        cfg["notify_enabled"] = request.form.get("notify_enabled") == "1"
        nt = (request.form.get("notify_time") or "").strip()
        if re.fullmatch(r"\d{2}:\d{2}", nt):
            cfg["notify_time"] = nt
        cfg["admin_email"] = (request.form.get("admin_email") or "").strip()
        cfg["smtp_host"] = (request.form.get("smtp_host") or "").strip()
        val = request.form.get("smtp_port", type=int)
        cfg["smtp_port"] = val if val else 587
        sec = request.form.get("smtp_security")
        if sec in ("starttls", "ssl", "none"):
            cfg["smtp_security"] = sec
        cfg["smtp_user"] = (request.form.get("smtp_user") or "").strip()
        cfg["smtp_from"] = (request.form.get("smtp_from") or "").strip()
        pw = request.form.get("smtp_password") or ""
        if request.form.get("clear_password") == "1":
            cfg["smtp_password"] = ""
        elif pw:
            cfg["smtp_password"] = pw
        save_config(cfg)
        flash("設定を保存しました。", "success")
        return redirect(url_for("settings"))

    conn = get_db()
    notify_rows = conn.execute(
        "SELECT * FROM notify_log ORDER BY id DESC LIMIT 20"
    ).fetchall()
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return render_template(
        "settings.html", active_page="settings", cfg=cfg,
        notify_rows=notify_rows, db_path=str(DB_PATH),
        db_size_kb=max(1, db_size // 1024),
        has_password=bool(cfg["smtp_password"]),
    )


@app.route("/settings/test-mail", methods=["POST"])
def settings_test_mail():
    cfg = load_config()
    to = (request.form.get("to") or "").strip() or cfg["admin_email"]
    if not to:
        flash("送信先メールアドレスを入力してください。", "error")
    else:
        try:
            send_mail(
                cfg, to, f"【{APP_NAME}】テストメール",
                "研究室冷蔵庫管理システムのテスト送信です。\n"
                "このメールが届いていれば、メール設定は正常です。",
            )
            flash(f"{to} にテストメールを送信しました。受信を確認してください。", "success")
        except Exception as exc:  # noqa: BLE001 - 設定ミスをそのまま画面に出す
            flash(f"送信に失敗しました: {type(exc).__name__}: {str(exc)[:300]}", "error")
    return redirect(url_for("settings"))


@app.route("/settings/run-notify", methods=["POST"])
def settings_run_notify():
    try:
        summary = run_notify_job(manual=True)
        flash("通知チェックを実行しました。" + summary, "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"通知チェックに失敗しました: {exc}", "error")
    return redirect(url_for("settings"))


@app.route("/backup", methods=["POST"])
def backup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"fridge_{datetime.now():%Y%m%d_%H%M%S}.db"
    src = connect()
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    flash(f"バックアップを作成しました: {dest}", "success")
    return redirect(url_for("settings"))


# ---------------------------------------------------------------- CSV出力

def _csv_response(rows_2d, filename: str) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows_2d:
        writer.writerow(row)
    return Response(
        "\ufeff" + buf.getvalue(),  # BOM付きUTF-8（Excelの文字化け対策）
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/export/items.csv")
def export_items():
    conn = get_db()
    rows, *_ = _filtered_items(conn, request.args)
    data = [[
        "ID", "品名", "メモ", "数量", "位置", "所有者", "冷蔵庫", "入庫日",
        "保管期限", "状態", "出庫日", "出庫実施者", "出庫メモ", "登録日時", "更新日時",
    ]]
    for r in rows:
        data.append([
            r["id"], r["name"], r["detail"], r["quantity"], r["position"],
            r["owner_name"], r["fridge_name"], r["stored_date"],
            r["expiry_date"] or "期限なし", r["status"],
            r["out_date"] or "", r["out_by_name"] or "", r["out_note"],
            r["created_at"], r["updated_at"],
        ])
    return _csv_response(data, "fridge_items.csv")


@app.route("/export/history.csv")
def export_history():
    conn = get_db()
    rows = conn.execute("SELECT * FROM history ORDER BY id").fetchall()
    data = [["ID", "日時", "操作", "アイテムID", "品名", "実施者", "詳細"]]
    for r in rows:
        data.append([
            r["id"], r["ts"], r["action"], r["item_id"] or "",
            r["item_name"], r["actor"], r["detail"],
        ])
    return _csv_response(data, "fridge_history.csv")


# ---------------------------------------------------------------- 起動

def _write_pid_file() -> None:
    pid_path = DATA_DIR / "server.pid"
    pid_path.write_text(str(os.getpid()), encoding="ascii")
    atexit.register(lambda: pid_path.unlink(missing_ok=True))


def main() -> None:
    init_db()
    _write_pid_file()
    cfg = load_config()
    host = str(cfg.get("host") or "0.0.0.0")
    port = int(cfg.get("port") or 8341)

    start_notifier_thread()

    if not os.environ.get("FRIDGE_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}/")).start()

    print(f" * {APP_NAME} を起動しました: http://localhost:{port}/")
    print("   （このウィンドウを閉じるか Ctrl+C で停止します）")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=6)
    except OSError as exc:
        print(f"[エラー] サーバーを起動できません: {exc}")
        print(f"ポート {port} が他のソフトに使われている場合は、"
              "data/config.json の \"port\" を別の番号に変更して再起動してください。")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""保管期限のチェックとメール通知。

毎分起床する常駐スレッドが、設定された通知時刻を過ぎた最初のタイミングで
1日1回だけ通知ジョブを実行する（PCが通知時刻に停止していた場合は起動後に送る）。
"""
from __future__ import annotations

import smtplib
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

from config import load_config
from db import connect, get_meta, set_meta

APP_NAME = "研究室冷蔵庫管理"


# ---------------------------------------------------------------- データ収集

def collect_alert_items(conn, warn_days: int):
    """保管中かつ期限切れ／期限間近のアイテムを (expired, warning) で返す。"""
    today = date.today().isoformat()
    warn_until = (date.today() + timedelta(days=warn_days)).isoformat()
    rows = conn.execute(
        """
        SELECT i.*, r.name AS owner_name, r.email AS owner_email,
               f.name AS fridge_name
        FROM items i
        JOIN researchers r ON r.id = i.owner_id
        JOIN fridges f     ON f.id = i.fridge_id
        WHERE i.status = '保管中'
          AND i.expiry_date IS NOT NULL
          AND i.expiry_date <= ?
        ORDER BY i.expiry_date
        """,
        (warn_until,),
    ).fetchall()
    expired = [r for r in rows if r["expiry_date"] < today]
    warning = [r for r in rows if r["expiry_date"] >= today]
    return expired, warning


def _item_line(row, with_owner: bool = False) -> str:
    diff = (date.today() - date.fromisoformat(row["expiry_date"])).days
    if diff > 0:
        state = f"{diff}日超過"
    elif diff == 0:
        state = "本日期限"
    else:
        state = f"あと{-diff}日"
    qty = f"（{row['quantity']}）" if row["quantity"] else ""
    owner = f"｜所有者: {row['owner_name']}" if with_owner else ""
    return (
        f"・{row['name']}{qty}｜{row['fridge_name']}｜入庫 {row['stored_date']}｜"
        f"期限 {row['expiry_date']}（{state}）{owner}"
    )


# ---------------------------------------------------------------- メール送信

def send_mail(cfg: dict, to_addr: str, subject: str, body: str) -> None:
    """SMTP設定に従って1通送信する。失敗時は例外を送出する。"""
    host = (cfg.get("smtp_host") or "").strip()
    if not host:
        raise RuntimeError("SMTPサーバーが未設定です（設定ページで入力してください）")
    port = int(cfg.get("smtp_port") or 587)
    user = (cfg.get("smtp_user") or "").strip()
    password = cfg.get("smtp_password") or ""
    from_addr = (cfg.get("smtp_from") or "").strip() or user or f"fridge@{host}"
    security = cfg.get("smtp_security") or "starttls"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(APP_NAME, "utf-8")), from_addr))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)

    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    try:
        server.ehlo()
        if security == "starttls":
            server.starttls()
            server.ehlo()
        if user and password:
            server.login(user, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _try_send(conn, cfg, to_addr, subject, body, ts) -> bool:
    try:
        send_mail(cfg, to_addr, subject, body)
        conn.execute(
            "INSERT INTO notify_log (ts, recipient, subject, ok, error) VALUES (?, ?, ?, 1, '')",
            (ts, to_addr, subject),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 送信失敗はログに残して継続
        conn.execute(
            "INSERT INTO notify_log (ts, recipient, subject, ok, error) VALUES (?, ?, ?, 0, ?)",
            (ts, to_addr, subject, f"{type(exc).__name__}: {exc}"[:500]),
        )
        return False


def _owner_body(cfg: dict, info: dict) -> str:
    lines = [
        f"{info['name']} 様",
        "",
        f"{APP_NAME}システムからの自動通知です。",
        "共用冷蔵庫に保管中の以下のアイテムが、保管期限切れまたは期限間近です。",
        "内容を確認のうえ、不要であれば廃棄し、システムで出庫・破棄の登録をお願いします。",
        "",
    ]
    if info["expired"]:
        lines.append("■ 期限切れ")
        lines += [_item_line(r) for r in info["expired"]]
        lines.append("")
    if info["warning"]:
        lines.append(f"■ 期限間近（{cfg['warn_days']}日以内）")
        lines += [_item_line(r) for r in info["warning"]]
        lines.append("")
    lines += ["--", f"このメールは {APP_NAME} から自動送信されています。"]
    return "\n".join(lines)


def _admin_body(cfg: dict, expired, warning, no_email: list[str]) -> str:
    lines = [
        "管理者向けまとめ通知です。",
        "",
    ]
    if expired:
        lines.append("■ 期限切れ")
        lines += [_item_line(r, with_owner=True) for r in expired]
        lines.append("")
    if warning:
        lines.append(f"■ 期限間近（{cfg['warn_days']}日以内）")
        lines += [_item_line(r, with_owner=True) for r in warning]
        lines.append("")
    if no_email:
        lines.append("■ メールアドレス未登録のため個別通知できなかった所有者")
        lines.append("　" + "、".join(no_email))
        lines.append("")
    lines += ["--", f"このメールは {APP_NAME} から自動送信されています。"]
    return "\n".join(lines)


# ---------------------------------------------------------------- 通知ジョブ

def run_notify_job(conn=None, manual: bool = False) -> str:
    """期限チェックを実行し、所有者ごと＋管理者へメールを送る。要約文字列を返す。"""
    cfg = load_config()
    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        suffix = "（手動実行）" if manual else ""
        expired, warning = collect_alert_items(conn, int(cfg["warn_days"]))

        if not expired and not warning:
            conn.execute(
                "INSERT INTO history (ts, action, item_id, item_name, actor, detail) "
                "VALUES (?, '通知', NULL, '', 'システム', ?)",
                (ts, "期限切れ・期限間近なし。メール送信なし" + suffix),
            )
            conn.commit()
            return "期限切れ・期限間近のアイテムはありません（メールは送信していません）。"

        by_owner: dict[int, dict] = {}
        for row in expired:
            by_owner.setdefault(
                row["owner_id"],
                {"name": row["owner_name"], "email": row["owner_email"],
                 "expired": [], "warning": []},
            )["expired"].append(row)
        for row in warning:
            by_owner.setdefault(
                row["owner_id"],
                {"name": row["owner_name"], "email": row["owner_email"],
                 "expired": [], "warning": []},
            )["warning"].append(row)

        sent = failed = 0
        no_email: list[str] = []
        for info in by_owner.values():
            addr = (info["email"] or "").strip()
            if not addr:
                no_email.append(info["name"])
                continue
            subject = (
                f"【{APP_NAME}】保管期限のお知らせ"
                f"（期限切れ{len(info['expired'])}件・間近{len(info['warning'])}件）"
            )
            if _try_send(conn, cfg, addr, subject, _owner_body(cfg, info), ts):
                sent += 1
            else:
                failed += 1

        admin = (cfg.get("admin_email") or "").strip()
        if admin:
            subject = (
                f"【{APP_NAME}】期限アラートまとめ"
                f"（期限切れ{len(expired)}件・間近{len(warning)}件）"
            )
            if _try_send(conn, cfg, admin, subject,
                         _admin_body(cfg, expired, warning, no_email), ts):
                sent += 1
            else:
                failed += 1

        summary = (
            f"期限切れ{len(expired)}件・期限間近{len(warning)}件。"
            f"メール送信 成功{sent}件・失敗{failed}件。"
        )
        if no_email:
            summary += f" アドレス未登録: {'、'.join(no_email)}"
        conn.execute(
            "INSERT INTO history (ts, action, item_id, item_name, actor, detail) "
            "VALUES (?, '通知', NULL, '', 'システム', ?)",
            (ts, summary + suffix),
        )
        conn.commit()
        return summary
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------- 常駐スレッド

def _scheduler_loop() -> None:
    while True:
        try:
            cfg = load_config()
            if cfg.get("notify_enabled"):
                now = datetime.now()
                target = str(cfg.get("notify_time") or "09:00")
                today = now.date().isoformat()
                conn = connect()
                try:
                    last = get_meta(conn, "last_notify_date", "")
                    if last != today and now.strftime("%H:%M") >= target:
                        # 失敗しても毎分再送し続けないよう、先に実行済みを記録する
                        set_meta(conn, "last_notify_date", today)
                        conn.commit()
                        run_notify_job(conn)
                finally:
                    conn.close()
        except Exception:  # noqa: BLE001 - スレッドを止めない
            traceback.print_exc()
        time.sleep(60)


def start_notifier_thread() -> None:
    thread = threading.Thread(
        target=_scheduler_loop, daemon=True, name="fridge-notifier"
    )
    thread.start()

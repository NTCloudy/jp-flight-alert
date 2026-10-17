"""Email delivery via SMTP (Gmail App Password by default); plain text + optional HTML."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def send_email(subject: str, text: str, html: str | None = None, dry_run: bool = False) -> bool:
    to_addr = os.environ.get("NOTIFY_EMAIL", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip()
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))

    if dry_run or not (to_addr and user and password):
        if not dry_run:
            print("[警告] 未設定 NOTIFY_EMAIL / SMTP_USER / SMTP_PASSWORD，改為只印出通知內容。")
        print(f"===== {subject} =====\n{text}\n")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"日本機票提醒 <{user}>"
    msg["To"] = to_addr
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"[已寄出] {subject} -> {to_addr}")
    return True

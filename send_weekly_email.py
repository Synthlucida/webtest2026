#!/usr/bin/env python3
"""
Pošle týdenní emailový souhrn kontroly webů (webzen2026/webtest2026).

Nečte historii celého týdne běh po běhu - posílá aktuální stav ke dni
odeslání (výsledek posledního proběhlého běhu kontroly), rozdělený na
OK / Pozor / Nefunguje, ať máš jednou týdně rychlý přehled do emailu i bez
otevírání reportu. Odesílá se přes Gmail (SMTP, App Password - viz README).
"""
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

ROOT = Path(__file__).parent
RESULTS_PATH = ROOT / "results" / "latest.json"
REPORT_URL = "https://webzen2026.github.io/webtest2026/"


def load_results():
    if not RESULTS_PATH.exists():
        raise SystemExit("results/latest.json neexistuje - kontrola webů ještě neproběhla ani jednou.")
    return json.loads(RESULTS_PATH.read_text(encoding="utf-8"))


def build_body(payload) -> str:
    results = payload["results"]
    summary = payload["summary"]
    run_at = payload["run_at"]

    ok = [r for r in results if r["overall"] == "ok"]
    warn = [r for r in results if r["overall"] == "warn"]
    fail = [r for r in results if r["overall"] == "fail"]

    lines = []
    lines.append("Týdenní souhrn kontroly webů")
    lines.append(f"(stav k poslednímu běhu: {run_at})")
    lines.append("")
    lines.append(f"OK: {summary['ok']}   Pozor: {summary['warn']}   Nefunguje: {summary['fail']}")
    lines.append("")

    if fail:
        lines.append("NEFUNGUJE:")
        for r in fail:
            reason = "; ".join(r.get("notes") or []) or "neznámý důvod"
            lines.append(f"  - {r['name']} ({r['url']})")
            lines.append(f"    {reason}")
        lines.append("")

    if warn:
        lines.append("POZOR:")
        for r in warn:
            reason = "; ".join(r.get("notes") or []) or "neznámý důvod"
            lines.append(f"  - {r['name']} ({r['url']})")
            lines.append(f"    {reason}")
        lines.append("")

    if not fail and not warn:
        lines.append("Nic k řešení - všechny weby jsou OK.")
        lines.append("")

    lines.append(f"OK ({len(ok)}):")
    lines.append("  " + (", ".join(r["name"] for r in ok) if ok else "-"))
    lines.append("")
    lines.append(f"Plný report s detaily: {REPORT_URL}")

    return "\n".join(lines)


def send_email(body: str) -> None:
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = "Týdenní souhrn kontroly webů"
    msg["From"] = user
    msg["To"] = user
    msg["Date"] = formatdate(localtime=True)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [user], msg.as_string())


def main():
    payload = load_results()
    body = build_body(payload)
    send_email(body)
    print("Týdenní email odeslán.")


if __name__ == "__main__":
    main()

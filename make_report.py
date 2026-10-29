#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_accounting_report.py (v4)
گزارش روزانه حسابداری از SQLite — با تفکیک چک‌های سررسید و تغییرات.

Usage:
    python daily_accounting_report.py --date 1405/08/07
    python daily_accounting_report.py --from 1405/08/01 --to 1405/08/07 --open
"""

import argparse
import os
import sqlite3
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, date
from html import escape

DEFAULT_DB = "local_db_1405.db"


# ==============================================================
#  Helpers
# ==============================================================
def esc(v):
    return "" if v is None else escape(str(v))


def fmt(n, signed=False):
    if n is None or n == "":
        return "-"
    try:
        v = float(n)
    except (TypeError, ValueError):
        return esc(n)
    s = f"{v:,.0f}"
    if signed:
        cls = "pos" if v >= 0 else "neg"
        return f'<span class="{cls}">{s}</span>'
    return s


def as_float(v):
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def rows_to_dicts(cursor):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def check_db(db_path):
    if not os.path.exists(db_path):
        print(f"✗ SQLite file not found: {db_path}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {r[0] for r in cur.fetchall()}
    conn.close()
    required = {"tbl_Factor", "tbl_Document", "tbl_Check"}
    missing = required - existing
    if missing:
        print(f"✗ Missing tables: {missing}", file=sys.stderr)
        sys.exit(2)
    print(f"✓ DB OK: {db_path}  ({len(existing)} tables)")


# ==============================================================
#  Jalali conversion (for check delay)
# ==============================================================
def _jalali_to_gregorian(jy, jm, jd):
    jy += 1595
    days = -355668 + (365 * jy) + ((jy // 33) * 8) + (((jy % 33) + 3) // 4) + jd
    if jm < 7:
        days += (jm - 1) * 31
    else:
        days += ((jm - 7) * 30) + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    sal_a = [0, 31, 29 if (gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0) else 28,
             31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 13 and gd > sal_a[gm]:
        gd -= sal_a[gm]
        gm += 1
    return (gy, gm, gd)


def jalali_diff_days(d1, d2):
    if not d1 or not d2:
        return None
    try:
        def to_date(s):
            parts = str(s).replace('-', '/').split('/')
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            gy, gm, gd = _jalali_to_gregorian(y, m, d)
            return date(gy, gm, gd)
        return (to_date(d2) - to_date(d1)).days
    except Exception:
        return None


# ==============================================================
#  SQL — shared document column list
# ==============================================================
DOC_COLUMNS = """
    d.Sanad_Num,
    d.SanadDate,
    d.KolCode, d.MoeenCode, d.TafsiliCode,
    COALESCE(d.Bed, 0) AS Bed,
    COALESCE(d.Bes, 0) AS Bes,
    d.Comment,
    d.Factor_Num,
    d.ChkNum,
    COALESCE(
        (SELECT MAX(CName) FROM tbl_Telephone
          WHERE KolCode=d.KolCode AND MoeenCode=d.MoeenCode
            AND TafsiliCode=d.TafsiliCode),
        (SELECT MAX(CName) FROM tbl_Tafsili
          WHERE KolCode=d.KolCode AND MoeenCode=d.MoeenCode
            AND Code=d.TafsiliCode),
        (SELECT MAX(CName) FROM tbl_Moeen
          WHERE KolCode=d.KolCode AND Code=d.MoeenCode),
        (SELECT MAX(CName) FROM tbl_Kol WHERE Code=d.KolCode),
        ''
    ) AS AccountName
"""


def fetch_documents(cur, d1, d2):
    sql = f"""
        SELECT {DOC_COLUMNS}
        FROM tbl_Document d
        WHERE d.SanadDate BETWEEN ? AND ?
        ORDER BY d.Sanad_Num, d.KolCode, d.MoeenCode
    """
    cur.execute(sql, (d1, d2))
    return rows_to_dicts(cur)


def fetch_sanads_by_nums(cur, nums):
    if not nums:
        return []
    placeholders = ",".join("?" * len(nums))
    sql = f"""
        SELECT {DOC_COLUMNS}
        FROM tbl_Document d
        WHERE d.Sanad_Num IN ({placeholders})
    """
    cur.execute(sql, tuple(nums))
    return rows_to_dicts(cur)


# ==============================================================
#  Factors
# ==============================================================
def fetch_factors_raw(cur, d1, d2):
    sql = """
        SELECT
            f.Factor_Date,
            f.Factor_Num,
            MAX(f.Sanad_Num) AS Sanad_Num,
            f.[I/O]          AS IO,
            f.Taraf_K, f.Taraf_M, f.Taraf_T,
            SUM(COALESCE(f.Price,0) * COALESCE(f.Tedad,0)
                - COALESCE(f.Discont,0)) AS Amount,
            MAX(f.FactorComment) AS Comment,
            COALESCE(
                (SELECT MAX(CName) FROM tbl_Telephone
                  WHERE KolCode=f.Taraf_K AND MoeenCode=f.Taraf_M
                    AND TafsiliCode=f.Taraf_T),
                (SELECT MAX(CName) FROM tbl_Tafsili
                  WHERE KolCode=f.Taraf_K AND MoeenCode=f.Taraf_M
                    AND Code=f.Taraf_T),
                ''
            ) AS PartyName
        FROM tbl_Factor f
        WHERE f.Factor_Date BETWEEN ? AND ?
        GROUP BY f.Factor_Date, f.Factor_Num, f.[I/O],
                 f.Taraf_K, f.Taraf_M, f.Taraf_T
        ORDER BY f.Factor_Date, f.Factor_Num
    """
    cur.execute(sql, (d1, d2))
    return rows_to_dicts(cur)


def enrich_factors(factors, sanad_rows_by_num):
    """IO semantics:
        1 = فروش (customer is debited)
        0 = خرید (supplier is credited)
    Derived from the linked sanad, not from tbl_Factor.[I/O].
    """
    for f in factors:
        sanad = f.get("Sanad_Num")
        rows = sanad_rows_by_num.get(sanad, [])
        if not rows:
            continue

        customer_row = None
        revenue_row = None
        inventory_row = None
        for r in rows:
            if r["KolCode"] == 12:
                customer_row = r
            elif r["KolCode"] == 60:
                revenue_row = r
            elif r["KolCode"] in (61, 62, 63):
                inventory_row = r

        if customer_row:
            if customer_row.get("AccountName"):
                f["PartyName"] = customer_row["AccountName"]
            f["Taraf_K"] = customer_row["KolCode"]
            f["Taraf_M"] = customer_row["MoeenCode"]
            f["Taraf_T"] = customer_row["TafsiliCode"]

        io = None
        if customer_row:
            if as_float(customer_row.get("Bed")) > 0:
                io = 1
            elif as_float(customer_row.get("Bes")) > 0:
                io = 0
        if io is None and revenue_row:
            if as_float(revenue_row.get("Bes")) > 0:
                io = 1
            elif as_float(revenue_row.get("Bed")) > 0:
                io = 0
        if io is None and inventory_row:
            if as_float(inventory_row.get("Bed")) > 0:
                io = 0
            elif as_float(inventory_row.get("Bes")) > 0:
                io = 1
        if io is not None:
            f["IO"] = io


# ==============================================================
#  Checks
# ==============================================================
def fetch_checks(cur, d1, d2):
    sql = """
        SELECT
            c.RecDate, c.Giv_Date, c.ExpDate,
            c.Chk_Num, c.Sanad_Num,
            c.Taraf_K, c.Taraf_M, c.Taraf_T,
            c.Bank_K, c.Bank_M, c.Bank_T,
            c.Bank, c.Money, c.Comment,
            c.Chk_Kind, c.Vos_Pas, c.IsActive,
            COALESCE(
                (SELECT MAX(CName) FROM tbl_Telephone
                  WHERE KolCode=c.Taraf_K AND MoeenCode=c.Taraf_M
                    AND TafsiliCode=c.Taraf_T),
                (SELECT MAX(CName) FROM tbl_Tafsili
                  WHERE KolCode=c.Taraf_K AND MoeenCode=c.Taraf_M
                    AND Code=c.Taraf_T),
                ''
            ) AS PartyName,
            COALESCE(
                (SELECT MAX(CName) FROM tbl_Telephone
                  WHERE KolCode=c.Bank_K AND MoeenCode=c.Bank_M
                    AND TafsiliCode=c.Bank_T),
                ''
            ) AS BankAccountName
        FROM tbl_Check c
        WHERE (c.RecDate BETWEEN ? AND ?
            OR c.Giv_Date BETWEEN ? AND ?
            OR c.ExpDate BETWEEN ? AND ?)
        ORDER BY c.Chk_Kind, c.ExpDate, c.Chk_Num
    """
    cur.execute(sql, (d1, d2, d1, d2, d1, d2))
    checks = rows_to_dicts(cur)

    # ── Fallback: pull party name from linked sanad ──
    missing = list({
        c["Sanad_Num"] for c in checks
        if not c["PartyName"] and c.get("Sanad_Num")
    })
    if missing:
        placeholders = ",".join("?" * len(missing))
        cur.execute(f"""
            SELECT d.Sanad_Num,
                   COALESCE(
                       (SELECT MAX(CName) FROM tbl_Telephone
                         WHERE KolCode=d.KolCode AND MoeenCode=d.MoeenCode
                           AND TafsiliCode=d.TafsiliCode),
                       (SELECT MAX(CName) FROM tbl_Tafsili
                         WHERE KolCode=d.KolCode AND MoeenCode=d.MoeenCode
                           AND Code=d.TafsiliCode),
                       ''
                   ) AS PartyName
            FROM tbl_Document d
            WHERE d.Sanad_Num IN ({placeholders})
              AND d.KolCode = 12
        """, tuple(missing))
        name_map = {r[0]: r[1] for r in cur.fetchall() if r[1]}
        for c in checks:
            if not c["PartyName"] and c["Sanad_Num"] in name_map:
                c["PartyName"] = name_map[c["Sanad_Num"]]

    return checks


_VOS_PAS_MAP = {
    0:  ("در انتظار",  "pending"),
    2:  ("وصول شد",    "settled"),
    5:  ("برگشت خورد", "bounced"),
    7:  ("پاس شد",     "paid"),
    25: ("عودت شد",    "recycled"),
    28: ("عودت شد",    "recycled"),
}


def classify_check_full(c):
    try:
        vp = int(c.get("Vos_Pas")) if c.get("Vos_Pas") is not None else 0
    except (TypeError, ValueError):
        vp = 0
    try:
        kind = int(c.get("Chk_Kind")) if c.get("Chk_Kind") is not None else 0
    except (TypeError, ValueError):
        kind = 0

    is_received = (kind == 0)
    direction       = "دریافتی"  if is_received else "پرداختی"
    direction_badge = "check-in" if is_received else "check-out"
    label, badge = _VOS_PAS_MAP.get(vp, (f"نامشخص ({vp})", "unknown"))

    return {
        "label":           label,
        "badge":           badge,
        "direction":       direction,
        "direction_badge": direction_badge,
        "cash_in":         (vp == 2 and is_received),
        "cash_out":        (vp == 7 and not is_received),
        "is_settled":      vp in (2, 7),
        "vp":              vp,
        "is_received":     is_received,
    }


def split_checks(checks, d1, d2):
    """Returns (due_checks, active_checks).
       due   : ExpDate in range
       active: RecDate or Giv_Date in range
       A check may appear in both if both events are in range."""
    def in_rng(s):
        return s is not None and d1 <= str(s) <= d2
    due    = [c for c in checks if in_rng(c.get("ExpDate"))]
    active = [c for c in checks
              if in_rng(c.get("RecDate")) or in_rng(c.get("Giv_Date"))]
    return due, active


# ==============================================================
#  Transaction grouping (sanad-level)
# ==============================================================
def classify_sanad(sanad_num, rows):
    sales = [r for r in rows if r["KolCode"] in (60, 61, 62, 63)]
    if sales:
        return None

    money    = [r for r in rows if r["KolCode"] in (10, 11)]
    customer = [r for r in rows if r["KolCode"] == 12]
    expense  = [r for r in rows if r["KolCode"] == 50]

    if not (money or expense):
        return None

    date    = rows[0]["SanadDate"]
    comment = next((r["Comment"] for r in rows if r.get("Comment")), "") or ""
    fee_amount = sum(as_float(r.get("Bed")) for r in expense)

    def disp(r):
        return r.get("AccountName") or f"{int(r['KolCode'])}/{int(r['MoeenCode'])}"

    sources = []
    dests   = []
    for r in money:
        bed = as_float(r.get("Bed"))
        bes = as_float(r.get("Bes"))
        is_fee_row = (fee_amount > 0 and bed > 0 and bes > 0
                      and abs(bes - fee_amount) < 1)
        if bed > 0:
            dests.append({"name": disp(r), "amount": bed})
        if bes > 0 and not is_fee_row:
            sources.append({"name": disp(r), "amount": bes})

    if customer:
        cust_bes = sum(as_float(r.get("Bes")) for r in customer)
        cust_bed = sum(as_float(r.get("Bed")) for r in customer)

        if cust_bes > 0:
            return {
                "Sanad_Num": sanad_num, "Date": date, "Kind": "receipt",
                "Sources": [{"name": disp(customer[0]), "amount": cust_bes}],
                "Destinations": dests,
                "Amount": cust_bes,
                "Fee": fee_amount,
                "Comment": comment,
                "CustKey": (customer[0]["KolCode"], customer[0]["MoeenCode"],
                            customer[0]["TafsiliCode"]),
            }
        if cust_bed > 0:
            return {
                "Sanad_Num": sanad_num, "Date": date, "Kind": "payment",
                "Sources": sources,
                "Destinations": [{"name": disp(customer[0]), "amount": cust_bed}],
                "Amount": cust_bed,
                "Fee": fee_amount,
                "Comment": comment,
                "CustKey": (customer[0]["KolCode"], customer[0]["MoeenCode"],
                            customer[0]["TafsiliCode"]),
            }

    if sources and dests:
        return {
            "Sanad_Num": sanad_num, "Date": date, "Kind": "settlement",
            "Sources": sources, "Destinations": dests,
            "Amount": sum(s["amount"] for s in sources),
            "Fee": fee_amount,
            "Comment": comment,
        }

    if expense and sources:
        expense_items = [{"name": disp(r), "amount": as_float(r.get("Bed"))}
                         for r in expense if as_float(r.get("Bed")) > 0]
        return {
            "Sanad_Num": sanad_num, "Date": date, "Kind": "expense",
            "Sources": sources, "Destinations": expense_items,
            "Amount": sum(e["amount"] for e in expense_items),
            "Fee": 0, "Comment": comment,
        }

    return None


def group_transactions(documents):
    by_sanad = defaultdict(list)
    for r in documents:
        by_sanad[r["Sanad_Num"]].append(r)
    out = []
    for sanad_num, rows in by_sanad.items():
        t = classify_sanad(sanad_num, rows)
        if t:
            out.append(t)
    out.sort(key=lambda x: x["Sanad_Num"])
    return out


# ==============================================================
#  HTML: party breakdown renderer
# ==============================================================
def render_party_list(items):
    if not items:
        return '<span class="muted-dash">—</span>'
    if len(items) == 1:
        it = items[0]
        return (f'<div class="party-single">'
                f'<span class="pname">{esc(it["name"])}</span>'
                f'<span class="pamt">{fmt(it["amount"])}</span>'
                f'</div>')
    parts = []
    for it in items:
        parts.append(f'<div class="party-item">'
                     f'<span class="pname">{esc(it["name"])}</span>'
                     f'<span class="pamt">{fmt(it["amount"])}</span>'
                     f'</div>')
    return '<div class="party-list">' + ''.join(parts) + '</div>'


# ==============================================================
#  Sections: factors
# ==============================================================
def section_factors(factors):
    if not factors:
        return '<section><h2>خرید و فروش</h2>' \
               '<p class="empty">تراکنشی در این بازه یافت نشد.</p></section>'

    rows = []
    total_sale = 0.0
    total_buy  = 0.0

    for f in factors:
        io  = f.get("IO")
        amt = as_float(f.get("Amount"))

        if io == 1:
            kind, badge = "فروش", "sale"
            amount_html = f'<span class="pos">+ {fmt(amt)}</span>'
            total_sale += amt
        elif io == 0:
            kind, badge = "خرید", "purchase"
            amount_html = f'<span class="neg">− {fmt(amt)}</span>'
            total_buy += amt
        else:
            kind, badge = "نامشخص", "due"
            amount_html = fmt(amt)

        rows.append(f"""
            <tr>
              <td>{esc(f.get('Factor_Date'))}</td>
              <td class="num">{esc(f.get('Factor_Num'))}</td>
              <td class="num">{esc(f.get('Sanad_Num')) or '-'}</td>
              <td>{esc(f.get('PartyName')) or '-'}</td>
              <td><span class="badge {badge}">{kind}</span></td>
              <td class="num">{amount_html}</td>
              <td>{esc(f.get('Comment')) or '-'}</td>
            </tr>""")

    net = total_sale - total_buy
    if net > 0:
        net_html = f'<span class="pos">+ {fmt(net)}</span>'
    elif net < 0:
        net_html = f'<span class="neg">− {fmt(abs(net))}</span>'
    else:
        net_html = fmt(0)

    return f"""
    <section>
      <h2>خرید و فروش <span class="hint-inline">({len(factors)} فاکتور)</span></h2>
      <input class="search" placeholder="جستجو در این جدول…" oninput="filterTable(this)">
      <table>
        <thead><tr>
          <th>تاریخ</th><th>شماره فاکتور</th><th>شماره سند</th>
          <th>طرف حساب</th><th>نوع</th>
          <th>مبلغ (فروش + / خرید −)</th><th>شرح</th>
        </tr></thead>
        <tbody>
          {''.join(rows)}
          <tr class="totals-row">
            <td colspan="5">
              جمع — فروش {fmt(total_sale)} / خرید {fmt(total_buy)} / خالص
            </td>
            <td class="num">{net_html}</td>
            <td></td>
          </tr>
        </tbody>
      </table>
    </section>"""


# ==============================================================
#  Sections: transactions
# ==============================================================
def section_transactions(transactions):
    if not transactions:
        return '<section><h2>دریافت و پرداخت</h2>' \
               '<p class="empty">تراکنش پولی در این بازه یافت نشد.</p></section>'

    badge_map = {
        "receipt":    ("دریافت +",       "receipt"),
        "payment":    ("پرداخت −",       "payment"),
        "settlement": ("واریز/انتقال ⇄", "settlement"),
        "expense":    ("هزینه/کارمزد −", "expense"),
    }

    rows = []
    for t in transactions:
        label, badge = badge_map.get(t["Kind"], ("سایر", "due"))
        rows.append(f"""
            <tr>
              <td>{esc(t['Date'])}</td>
              <td class="num">{esc(t['Sanad_Num'])}</td>
              <td><span class="badge {badge}">{label}</span></td>
              <td class="cell-party">{render_party_list(t.get('Sources', []))}</td>
              <td class="cell-party">{render_party_list(t.get('Destinations', []))}</td>
              <td class="num">{fmt(t['Amount'])}</td>
              <td>{esc(t['Comment']) or '-'}</td>
            </tr>""")

    total_in       = sum(t["Amount"] for t in transactions if t["Kind"] == "receipt")
    total_paid     = sum(t["Amount"] for t in transactions if t["Kind"] == "payment")
    total_exp      = sum(t["Amount"] for t in transactions if t["Kind"] == "expense")
    total_fee      = sum(t.get("Fee", 0) for t in transactions)
    total_transfer = sum(t["Amount"] for t in transactions if t["Kind"] == "settlement")

    net_cash = total_in - total_paid - total_exp - total_fee
    net_cls  = "pos" if net_cash >= 0 else "neg"

    return f"""
    <section>
      <h2>دریافت و پرداخت <span class="hint-inline">({len(transactions)} تراکنش)</span></h2>
      <input class="search" placeholder="جستجو در این جدول…" oninput="filterTable(this)">
      <table>
        <thead><tr>
          <th>تاریخ</th><th>سند</th><th>نوع</th>
          <th>از</th><th>به</th><th>مبلغ</th><th>شرح</th>
        </tr></thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>

      <div class="cash-strip">
        <div class="cash-item cash-in">
          <span class="cash-label">دریافتی</span>
          <span class="cash-value pos">+ {fmt(total_in)}</span>
        </div>
        <div class="cash-item cash-out">
          <span class="cash-label">پرداختی</span>
          <span class="cash-value neg">− {fmt(total_paid)}</span>
        </div>
        <div class="cash-item cash-out">
          <span class="cash-label">هزینه</span>
          <span class="cash-value neg">− {fmt(total_exp)}</span>
        </div>
        <div class="cash-item cash-out">
          <span class="cash-label">کارمزد</span>
          <span class="cash-value neg">− {fmt(total_fee)}</span>
        </div>
        <div class="cash-item cash-neutral">
          <span class="cash-label">واریز / انتقال داخلی</span>
          <span class="cash-value muted">{fmt(total_transfer)}</span>
          <span class="cash-note">بدون اثر نقدی</span>
        </div>
        <div class="cash-item cash-net">
          <span class="cash-label">خالص جریان نقدی</span>
          <span class="cash-value {net_cls}">{fmt(net_cash, signed=True)}</span>
        </div>
      </div>

      <p class="hint-inline" style="margin-top:12px;">
        💡 هر ردیف معادل یک سند حسابداری است. برای مبلغ دقیق هر بانک، ستون «به» را ببینید.
        تراکنش‌های مرتبط با فاکتور (حساب 60.x) در این بخش نمایش داده نمی‌شوند.
      </p>
    </section>"""


# ==============================================================
#  Sections: checks due
# ==============================================================
def section_checks_due(checks_due, d1, d2):
    if not checks_due:
        return ''

    def sort_key(c):
        info = classify_check_full(c)
        return (0 if info["vp"] == 0 else 1, -as_float(c.get("Money")))

    rows = []
    total_pending = 0.0
    total_all     = 0.0
    count_pending = 0

    for c in sorted(checks_due, key=sort_key):
        info = classify_check_full(c)
        amt  = as_float(c.get("Money"))
        total_all += amt
        is_pending = (info["vp"] == 0)
        if is_pending:
            total_pending += amt
            count_pending += 1

        bank  = c.get("Bank") or c.get("BankAccountName") or "-"
        party = c.get("PartyName") or "-"
        row_cls = "due-pending" if is_pending else "due-done"

        rows.append(f"""
            <tr class="{row_cls}">
              <td class="num">{esc(c.get('Chk_Num'))}</td>
              <td>{esc(party)}</td>
              <td>{esc(bank)}</td>
              <td><span class="badge {info['direction_badge']}">{info['direction']}</span></td>
              <td><span class="badge {info['badge']}">{info['label']}</span></td>
              <td class="num">{fmt(amt)}</td>
              <td>{esc(c.get('Comment')) or '-'}</td>
            </tr>""")

    alert = ""
    if count_pending:
        alert = (f' <span class="due-alert">⚠️ {count_pending} چک '
                 f'بدون تسویه — {fmt(total_pending)}</span>')

    return f"""
    <section class="due-section">
      <h2>⏰ چک‌های دریافتی
          <span class="hint-inline">({len(checks_due)} چک — بازه {d1} تا {d2})</span>
          {alert}
      </h2>
      <input class="search" placeholder="جستجو در این جدول…" oninput="filterTable(this)">
      <table>
        <thead><tr>
          <th>شماره چک</th><th>طرف حساب</th><th>بانک</th>
          <th>جهت</th><th>وضعیت</th><th>مبلغ</th><th>شرح</th>
        </tr></thead>
        <tbody>
          {''.join(rows)}
          <tr class="totals-row">
            <td colspan="5">
              جمع (در انتظار: {fmt(total_pending)} — تسویه‌شده: {fmt(total_all - total_pending)})
            </td>
            <td class="num">{fmt(total_all)}</td>
            <td></td>
          </tr>
        </tbody>
      </table>
      <p class="hint-inline" style="margin-top:10px;">
        💡 چک‌های با هایلایت نارنجی هنوز تسویه نشده‌اند و نیاز به اقدام دارند.
      </p>
    </section>"""


# ==============================================================
#  Sections: checks activity (changes today)
# ==============================================================
def section_checks(checks, d1, d2):
    if not checks:
        return '<section><h2>تغییرات چک‌ها</h2>' \
               '<p class="empty">تغییری روی چک‌ها در این بازه ثبت نشده.</p></section>'

    stats = {
        "total":      0,
        "pending_in": 0, "pending_out": 0,
        "settled_in": 0, "paid_out":    0,
        "bounced":    0, "recycled":    0,
    }

    rows = []
    for c in checks:
        info = classify_check_full(c)
        vp, is_recv = info["vp"], info["is_received"]
        amt  = as_float(c.get("Money"))
        bank = c.get("Bank") or c.get("BankAccountName") or "-"

        stats["total"] += 1
        if vp == 0:               stats["pending_in" if is_recv else "pending_out"] += 1
        elif vp == 2:             stats["settled_in"] += 1
        elif vp == 7:             stats["paid_out"] += 1
        elif vp == 5:             stats["bounced"] += 1
        elif vp in (25, 28):      stats["recycled"] += 1

        delay_html = "-"
        if info["is_settled"]:
            days = jalali_diff_days(c.get("RecDate"), c.get("Giv_Date"))
            if days is not None:
                if days > 0:
                    delay_html = f'<span class="neg">+{days} روز</span>'
                elif days < 0:
                    delay_html = f'<span class="pos">{abs(days)} روز زودتر</span>'
                else:
                    delay_html = '<span class="muted-dash">به‌موقع</span>'

        rows.append(f"""
            <tr>
              <td class="num">{esc(c.get('Chk_Num'))}</td>
              <td>{esc(c.get('PartyName')) or '-'}</td>
              <td>{esc(c.get('ExpDate')) or '-'}</td>
              <td>{esc(c.get('RecDate')) or '-'}</td>
              <td>{esc(c.get('Giv_Date')) or '-'}</td>
              <td>{esc(bank)}</td>
              <td><span class="badge {info['direction_badge']}">{info['direction']}</span></td>
              <td><span class="badge {info['badge']}">{info['label']}</span></td>
              <td class="num">{fmt(amt)}</td>
              <td class="num">{delay_html}</td>
              <td>{esc(c.get('Comment')) or '-'}</td>
            </tr>""")

    total_amt = sum(as_float(c.get("Money")) for c in checks)

    return f"""
    <section>
      <h2>💳 تغییرات چک‌ها
          <span class="hint-inline">({len(checks)} چک — فعالیت در بازه {d1} تا {d2})</span>
      </h2>
      <input class="search" placeholder="جستجو در این جدول…" oninput="filterTable(this)">
      <table>
        <thead><tr>
          <th>شماره چک</th>
          <th>طرف حساب</th>
          <th>سررسید</th>
          <th>تاریخ دریافت</th>
          <th>تاریخ وصول</th>
          <th>بانک</th>
          <th>جهت</th>
          <th>وضعیت</th>
          <th>مبلغ</th>
          <th>تأخیر</th>
          <th>شرح</th>
        </tr></thead>
        <tbody>
          {''.join(rows)}
          <tr class="totals-row">
            <td colspan="8">جمع</td>
            <td class="num">{fmt(total_amt)}</td>
            <td colspan="2"></td>
          </tr>
        </tbody>
      </table>

      <div class="check-strip">
        <div class="check-stat pending"><span class="cs-num">{stats['pending_in']}</span><span class="cs-lbl">دریافتی در انتظار</span></div>
        <div class="check-stat pending"><span class="cs-num">{stats['pending_out']}</span><span class="cs-lbl">پرداختی در انتظار</span></div>
        <div class="check-stat success"><span class="cs-num">{stats['settled_in']}</span><span class="cs-lbl">وصول‌شده</span></div>
        <div class="check-stat success"><span class="cs-num">{stats['paid_out']}</span><span class="cs-lbl">پاس‌شده</span></div>
        <div class="check-stat danger"><span class="cs-num">{stats['bounced']}</span><span class="cs-lbl">برگشتی</span></div>
        <div class="check-stat warning"><span class="cs-num">{stats['recycled']}</span><span class="cs-lbl">عودتی</span></div>
      </div>

      <p class="hint-inline" style="margin-top:12px;">
        💡 وضعیت از فیلد <code>Vos_Pas</code> خوانده می‌شود:
        ۲=وصول، ۷=پاس، ۵=برگشت، ۲۵/۲۸=عودت، ۰=در انتظار.
        فقط چک‌های <strong>وصول (۲)</strong> و <strong>پاس (۷)</strong> در جریان نقدی حساب می‌شوند.
      </p>
    </section>"""


# ==============================================================
#  Sections: customer summary
# ==============================================================
def section_by_customer(factors, transactions, checks, d1, d2):
    agg = defaultdict(lambda: {
        "name": "", "sale": 0.0, "purchase": 0.0,
        "receipt": 0.0, "payment": 0.0,
        "check_in": 0.0, "check_out": 0.0,
    })

    def upd_name(key, name):
        if name and not agg[key]["name"]:
            agg[key]["name"] = name

    for f in factors:
        if f.get("Taraf_K") != 12:
            continue
        key = (f["Taraf_K"], f["Taraf_M"], f["Taraf_T"])
        upd_name(key, f.get("PartyName"))
        amt = as_float(f.get("Amount"))
        if f.get("IO") == 1:
            agg[key]["sale"] += amt
        elif f.get("IO") == 0:
            agg[key]["purchase"] += amt

    for t in transactions:
        if not t.get("CustKey"):
            continue
        key = t["CustKey"]
        if key[0] != 12:
            continue
        if t["Kind"] == "receipt" and t.get("Sources"):
            upd_name(key, t["Sources"][0]["name"])
            agg[key]["receipt"] += t["Amount"]
        elif t["Kind"] == "payment" and t.get("Destinations"):
            upd_name(key, t["Destinations"][0]["name"])
            agg[key]["payment"] += t["Amount"]

    for c in checks:
        if c.get("Taraf_K") != 12:
            continue
        key = (c["Taraf_K"], c["Taraf_M"], c["Taraf_T"])
        upd_name(key, c.get("PartyName"))

        info = classify_check_full(c)
        if info["vp"] == 5:
            continue
        amt = as_float(c.get("Money"))
        if info["is_received"]:
            agg[key]["check_in"] += amt
        else:
            agg[key]["check_out"] += amt

    active = {k: v for k, v in agg.items()
              if any([v["sale"], v["purchase"], v["receipt"],
                      v["payment"], v["check_in"], v["check_out"]])}

    if not active:
        return '<section><h2>خلاصه بر اساس طرف حساب</h2>' \
               '<p class="empty">داده‌ای یافت نشد.</p></section>'

    FIELDS = ("sale", "purchase", "receipt", "payment",
              "check_in", "check_out")
    grand = {k: 0.0 for k in FIELDS}
    grand["debit"] = grand["credit"] = grand["net"] = 0.0

    rows = []
    for key, v in sorted(active.items(), key=lambda kv: kv[1]["name"] or ""):
        debit  = v["sale"] + v["payment"] + v["check_out"]
        credit = v["purchase"] + v["receipt"] + v["check_in"]
        net    = debit - credit

        for k in FIELDS:
            grand[k] += v[k]
        grand["debit"]  += debit
        grand["credit"] += credit
        grand["net"]    += net

        disp = v["name"] or f"{key[0]}/{key[1]}/{key[2]}"
        net_cls   = "neg" if net >= 0 else "pos"
        net_label = "بدهکار" if net >= 0 else "بستانکار"
        rows.append(f"""
            <tr>
              <td>{esc(disp)}</td>
              <td class="num">{fmt(v['sale'])}</td>
              <td class="num">{fmt(v['purchase'])}</td>
              <td class="num">{fmt(v['receipt'])}</td>
              <td class="num">{fmt(v['payment'])}</td>
              <td class="num">{fmt(v['check_in'])}</td>
              <td class="num">{fmt(v['check_out'])}</td>
              <td class="num debit-col">{fmt(debit)}</td>
              <td class="num credit-col">{fmt(credit)}</td>
              <td class="num {net_cls}">
                {fmt(abs(net))} <span class="bal-tag {net_cls}">{net_label}</span>
              </td>
            </tr>""")

    grand_cls   = "neg" if grand["net"] >= 0 else "pos"
    grand_label = "بدهکار" if grand["net"] >= 0 else "بستانکار"

    return f"""
    <section>
      <h2>خلاصه بر اساس طرف حساب</h2>
      <input class="search" placeholder="جستجو…" oninput="filterTable(this)">
      <table>
        <thead><tr>
          <th>طرف حساب</th>
          <th>فروش</th><th>خرید</th>
          <th>دریافت</th><th>پرداخت</th>
          <th>چک دریافتی</th><th>چک پرداختی</th>
          <th class="debit-col">بدهکار</th>
          <th class="credit-col">بستانکار</th>
          <th>مانده</th>
        </tr></thead>
        <tbody>
          {''.join(rows)}
          <tr class="totals-row">
            <td>جمع کل</td>
            <td class="num">{fmt(grand['sale'])}</td>
            <td class="num">{fmt(grand['purchase'])}</td>
            <td class="num">{fmt(grand['receipt'])}</td>
            <td class="num">{fmt(grand['payment'])}</td>
            <td class="num">{fmt(grand['check_in'])}</td>
            <td class="num">{fmt(grand['check_out'])}</td>
            <td class="num debit-col">{fmt(grand['debit'])}</td>
            <td class="num credit-col">{fmt(grand['credit'])}</td>
            <td class="num {grand_cls}">
              {fmt(abs(grand['net']))} <span class="bal-tag {grand_cls}">{grand_label}</span>
            </td>
          </tr>
        </tbody>
      </table>

      <div class="accounting-note">
        <h4>راهنمای بدهکار / بستانکار</h4>
        <ul>
          <li><strong>بدهکار</strong> = فروش + پرداخت به مشتری + چک پرداختی</li>
          <li><strong>بستانکار</strong> = خرید + دریافت از مشتری + چک دریافتی</li>
          <li><strong>مانده = بدهکار − بستانکار</strong></li>
          <li>مانده <span class="neg">بدهکار (قرمز)</span> → مشتری به ما بدهی دارد، باید وصول شود.</li>
          <li>مانده <span class="pos">بستانکار (سبز)</span> → مشتری اعتبار/طلب دارد.</li>
        </ul>
      </div>
    </section>"""


# ==============================================================
#  Sections: bank summary
# ==============================================================
def section_by_bank(checks, d1, d2):
    if not checks:
        return '<section><h2>خلاصه بر اساس بانک</h2>' \
               '<p class="empty">چکی یافت نشد.</p></section>'

    agg = defaultdict(lambda: {"in": 0.0, "out": 0.0, "count": 0})
    for c in checks:
        info = classify_check_full(c)
        name = (c.get("Bank") or c.get("BankAccountName")
                or f"{c.get('Bank_K')}/{c.get('Bank_M')}/{c.get('Bank_T')}")
        amt = as_float(c.get("Money"))
        if info["cash_in"]:
            agg[name]["in"] += amt
        elif info["cash_out"]:
            agg[name]["out"] += amt
        else:
            continue
        agg[name]["count"] += 1

    if not agg:
        return '<section><h2>خلاصه بر اساس بانک</h2>' \
               '<p class="empty">چک وصول/پاس‌شده در بازه یافت نشد.</p></section>'

    rows = []
    tot_in = tot_out = 0.0
    for name, v in sorted(agg.items(), key=lambda kv: kv[0] or ""):
        net = v["in"] - v["out"]
        tot_in += v["in"]
        tot_out += v["out"]
        rows.append(f"""
            <tr>
              <td>{esc(name)}</td>
              <td class="num">{v['count']}</td>
              <td class="num">{fmt(v['in'])}</td>
              <td class="num">{fmt(v['out'])}</td>
              <td class="num">{fmt(net, signed=True)}</td>
            </tr>""")

    return f"""
    <section>
      <h2>خلاصه بر اساس بانک (چک‌های وصول/پاس‌شده)</h2>
      <table>
        <thead><tr>
          <th>بانک</th><th>تعداد</th><th>دریافتی</th><th>پرداختی</th><th>مانده</th>
        </tr></thead>
        <tbody>
          {''.join(rows)}
          <tr class="totals-row">
            <td>جمع کل</td>
            <td class="num">{sum(v['count'] for v in agg.values())}</td>
            <td class="num">{fmt(tot_in)}</td>
            <td class="num">{fmt(tot_out)}</td>
            <td class="num">{fmt(tot_in - tot_out, signed=True)}</td>
          </tr>
        </tbody>
      </table>
    </section>"""


# ==============================================================
#  Cards
# ==============================================================
def build_cards(factors, transactions, checks, d1, d2):
    sales     = sum(as_float(f["Amount"]) for f in factors if f.get("IO") == 1)
    purchases = sum(as_float(f["Amount"]) for f in factors if f.get("IO") == 0)

    t_tot = defaultdict(float)
    for t in transactions:
        t_tot[t["Kind"]] += t["Amount"]
    total_fees = sum(t.get("Fee", 0) for t in transactions)

    receipts    = t_tot["receipt"]
    payments    = t_tot["payment"]
    settlements = t_tot["settlement"]
    expenses    = t_tot["expense"] + total_fees

    check_in  = sum(as_float(c["Money"]) for c in checks
                    if classify_check_full(c)["cash_in"])
    check_out = sum(as_float(c["Money"]) for c in checks
                    if classify_check_full(c)["cash_out"])

    net_cash = (receipts + check_in) - (payments + expenses + check_out)

    def card(label, value, cls, hint=""):
        return f"""<div class="card {cls}">
                     <div class="label">{label}</div>
                     <div class="value">{fmt(value)}</div>
                     <div class="hint">{hint}</div>
                   </div>"""

    return f"""
    <div class="cards">
      {card("فروش", sales, "success",
            f"{sum(1 for f in factors if f.get('IO')==1)} فاکتور")}
      {card("خرید", purchases, "danger",
            f"{sum(1 for f in factors if f.get('IO')==0)} فاکتور")}
      {card("دریافت از مشتری", receipts, "primary",
            f"{sum(1 for t in transactions if t['Kind']=='receipt')} سند")}
      {card("پرداخت به مشتری", payments, "warning",
            f"{sum(1 for t in transactions if t['Kind']=='payment')} سند")}
      {card("واریز/انتقال", settlements, "info",
            f"{sum(1 for t in transactions if t['Kind']=='settlement')} سند")}
      {card("هزینه/کارمزد", expenses, "warning",
            f"{sum(1 for t in transactions if t['Kind']=='expense')} سند + کارمزد")}
      {card("چک دریافتی (وصول)", check_in, "primary",
            f"{sum(1 for c in checks if classify_check_full(c)['cash_in'])} چک")}
      {card("چک پرداختی (پاس)", check_out, "danger",
            f"{sum(1 for c in checks if classify_check_full(c)['cash_out'])} چک")}
      {card("مانده نقدی روز", net_cash,
            "success" if net_cash >= 0 else "danger",
            "دریافت + چک‌ها − هزینه‌ها")}
    </div>"""


# ==============================================================
#  HTML Template
# ==============================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>گزارش روزانه حسابداری — {from_date} تا {to_date}</title>
<link rel="stylesheet"
      href="https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/Vazirmatn-font-face.css">
<style>
  :root {{
    --bg:#f4f6fb; --card:#fff; --primary:#2563eb;
    --success:#16a34a; --danger:#dc2626; --warning:#d97706;
    --info:#0ea5e9;
    --text:#1f2937; --muted:#6b7280; --border:#e5e7eb; --radius:14px;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    font-family:'Vazirmatn',Tahoma,sans-serif; background:var(--bg);
    color:var(--text); margin:0; padding:24px; line-height:1.7;
  }}
  .container {{ max-width:1500px; margin:0 auto; }}
  header {{
    background:linear-gradient(135deg,#1e40af,#2563eb); color:#fff;
    padding:28px 32px; border-radius:var(--radius); margin-bottom:24px;
    box-shadow:0 10px 30px rgba(37,99,235,.2);
  }}
  header h1 {{ margin:0 0 8px; font-size:26px; }}
  header .subtitle {{ opacity:.9; font-size:15px; }}
  .cards {{
    display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
    gap:16px; margin-bottom:24px;
  }}
  .card {{
    background:var(--card); border-radius:var(--radius); padding:18px 20px;
    box-shadow:0 2px 10px rgba(0,0,0,.04); border:1px solid var(--border);
    position:relative; overflow:hidden;
  }}
  .card::before {{
    content:''; position:absolute; top:0; right:0; left:0; height:4px;
    background:var(--primary);
  }}
  .card.success::before {{ background:var(--success); }}
  .card.danger::before  {{ background:var(--danger); }}
  .card.warning::before {{ background:var(--warning); }}
  .card.info::before    {{ background:var(--info); }}
  .card .label {{ font-size:13px; color:var(--muted); margin-bottom:6px; }}
  .card .value {{ font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .card .hint {{ font-size:12px; color:var(--muted); margin-top:4px; min-height:16px; }}

  section {{
    background:var(--card); border-radius:var(--radius); padding:24px;
    margin-bottom:24px; box-shadow:0 2px 10px rgba(0,0,0,.04);
    border:1px solid var(--border);
  }}
  section h2 {{
    margin:0 0 16px; font-size:18px; color:var(--primary);
    display:flex; align-items:center; gap:8px; flex-wrap:wrap;
  }}
  section h2::before {{
    content:''; width:4px; height:20px; background:var(--primary); border-radius:2px;
  }}
  .hint-inline {{ font-size:13px; color:var(--muted); font-weight:400; }}

  .search {{
    width:100%; max-width:340px; padding:8px 12px; margin-bottom:12px;
    border:1px solid var(--border); border-radius:8px; font-family:inherit;
    font-size:13px; background:#fafafa;
  }}
  .search:focus {{ outline:none; border-color:var(--primary); background:#fff; }}

  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th, td {{ padding:10px 12px; text-align:right; border-bottom:1px solid var(--border); }}
  th {{ background:#f9fafb; font-weight:600; color:var(--muted); font-size:13px; }}
  tbody tr:hover {{ background:#f9fafb; }}
  td.num {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}

  td.cell-party {{ padding:6px 10px !important; vertical-align:top; }}
  .party-single {{
    display:flex; justify-content:space-between; gap:10px;
    padding:3px 8px; border-radius:6px; background:#f9fafb; font-size:13px;
  }}
  .party-list {{ display:flex; flex-direction:column; gap:4px; }}
  .party-item {{
    display:flex; justify-content:space-between; gap:10px;
    padding:3px 8px; border-radius:6px; background:#f9fafb; font-size:12.5px;
  }}
  .pname {{ color:var(--text); flex:1; }}
  .pamt {{ color:var(--primary); font-weight:700; font-variant-numeric:tabular-nums;
           white-space:nowrap; }}
  .muted-dash {{ color:var(--muted); }}

  .badge {{ display:inline-block; padding:2px 10px; border-radius:20px;
            font-size:12px; font-weight:600; white-space:nowrap; }}
  .badge.sale        {{ background:#dcfce7; color:#166534; }}
  .badge.purchase    {{ background:#fee2e2; color:#991b1b; }}
  .badge.receipt     {{ background:#dbeafe; color:#1e40af; }}
  .badge.payment     {{ background:#fef3c7; color:#92400e; }}
  .badge.settlement  {{ background:#e0f2fe; color:#075985; }}
  .badge.expense     {{ background:#ede9fe; color:#5b21b6; }}
  .badge.check-in    {{ background:#e0e7ff; color:#3730a3; }}
  .badge.check-out   {{ background:#fce7f3; color:#9d174d; }}
  .badge.due         {{ background:#f3f4f6; color:#374151; }}
  .badge.pending     {{ background:#fef3c7; color:#92400e; }}
  .badge.settled     {{ background:#dcfce7; color:#166534; }}
  .badge.paid        {{ background:#dcfce7; color:#166534; }}
  .badge.bounced     {{ background:#fee2e2; color:#991b1b;
                       box-shadow: inset 0 0 0 1px #fca5a5; }}
  .badge.recycled    {{ background:#ffedd5; color:#9a3412; }}
  .badge.unknown     {{ background:#f3f4f6; color:#374151; }}

  .totals-row {{ font-weight:700; background:#f9fafb !important; }}
  .empty {{ color:var(--muted); text-align:center; padding:20px; }}
  .pos {{ color:var(--success); }}
  .neg {{ color:var(--danger); }}

  footer {{ text-align:center; color:var(--muted); font-size:12px; padding:16px; }}

  /* Cash flow summary strip */
  .cash-strip {{
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 16px;
    padding: 16px;
    background: #f9fafb;
    border-radius: 10px;
    border: 1px solid var(--border);
  }}
  .cash-item {{
    flex: 1 1 150px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 12px 14px;
    border-radius: 8px;
    background: #fff;
    border-right: 4px solid var(--border);
  }}
  .cash-item.cash-in       {{ border-right-color: var(--success); }}
  .cash-item.cash-out      {{ border-right-color: var(--danger); }}
  .cash-item.cash-neutral  {{ border-right-color: var(--muted); }}
  .cash-item.cash-net      {{ border-right-color: var(--primary); background: #eff6ff; }}
  .cash-label {{ font-size: 12.5px; color: var(--muted); font-weight: 500; }}
  .cash-value {{ font-size: 17px; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .cash-value.muted {{ color: var(--muted); font-weight: 500; }}
  .cash-note  {{ font-size: 11px; color: var(--muted); }}

  /* Balance tag */
  .bal-tag {{
    display: inline-block;
    font-size: 10.5px;
    padding: 1px 8px;
    border-radius: 10px;
    font-weight: 600;
    vertical-align: middle;
  }}
  .bal-tag.pos {{ background: #dcfce7; color: #166534; }}
  .bal-tag.neg {{ background: #fee2e2; color: #991b1b; }}
  .debit-col  {{ background: #fef9f3 !important; }}
  .credit-col {{ background: #f0f9ff !important; }}
  th.debit-col  {{ color: #92400e; }}
  th.credit-col {{ color: #1e40af; }}

  /* Accounting note */
  .accounting-note {{
    margin-top: 20px;
    padding: 16px 20px;
    background: #fefce8;
    border-right: 4px solid #eab308;
    border-radius: 8px;
    font-size: 13px;
    line-height: 1.8;
  }}
  .accounting-note h4 {{
    margin: 0 0 8px;
    font-size: 14px;
    color: #713f12;
  }}
  .accounting-note ul {{ margin: 0; padding-right: 20px; }}
  .accounting-note li {{ margin: 4px 0; color: #78350f; }}

  /* Check status strip */
  .check-strip {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 16px;
    padding: 14px;
    background: #f9fafb;
    border-radius: 10px;
    border: 1px solid var(--border);
  }}
  .check-stat {{
    flex: 1 1 110px;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 10px 8px;
    background: #fff;
    border-radius: 8px;
    border-bottom: 3px solid var(--border);
  }}
  .check-stat.pending {{ border-bottom-color: var(--warning); }}
  .check-stat.success {{ border-bottom-color: var(--success); }}
  .check-stat.danger  {{ border-bottom-color: var(--danger); }}
  .check-stat.warning {{ border-bottom-color: #f97316; }}
  .cs-num {{ font-size: 22px; font-weight: 700; }}
  .cs-lbl {{ font-size: 11.5px; color: var(--muted); margin-top: 2px; }}

  /* Due checks section */
  .due-section {{ border-right: 4px solid #f59e0b; }}
  .due-section h2::before {{ background: #f59e0b; }}
  .due-section h2 {{ color: #b45309; }}
  .due-pending {{ background: #fff7ed !important; }}
  .due-pending:hover {{ background: #ffedd5 !important; }}
  .due-alert {{
    display: inline-block;
    margin-right: 8px;
    padding: 2px 10px;
    background: #fef3c7;
    color: #92400e;
    font-size: 12px;
    font-weight: 700;
    border-radius: 10px;
    vertical-align: middle;
  }}
  .due-done {{ opacity: 0.75; }}

  @media print {{
    body {{ background:#fff; padding:0; }}
    .search {{ display:none; }}
    section, header, .card {{ box-shadow:none; border:1px solid #ddd; }}
  }}
  @media (max-width:640px) {{
    body {{ padding:12px; }}
    th, td {{ padding:8px; font-size:12px; }}
  }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>📊 گزارش روزانه حسابداری</h1>
    <div class="subtitle">بازه: {from_date} تا {to_date} — تولید در {generated_at}</div>
  </header>

  {cards}
  {factors_section}
  {transactions_section}
  {checks_due_section}
  {checks_section}
  {customer_section}
  {bank_section}

  <footer>تولید شده با daily_accounting_report.py</footer>
</div>

<script>
function filterTable(input) {{
  const section = input.closest('section');
  const table = section.querySelector('table');
  if (!table) return;
  const q = input.value.trim().toLowerCase();
  table.querySelectorAll('tbody tr').forEach(tr => {{
    if (tr.classList.contains('totals-row')) return;
    tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}}
</script>
</body>
</html>
"""


# ==============================================================
#  Main
# ==============================================================
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--date")
    p.add_argument("--from", dest="from_date")
    p.add_argument("--to", dest="to_date")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--output")
    p.add_argument("--open", action="store_true")
    args = p.parse_args()
    if args.date:
        d1 = d2 = args.date.strip()
    elif args.from_date and args.to_date:
        d1, d2 = args.from_date.strip(), args.to_date.strip()
    else:
        p.error("Provide --date OR --from and --to.")
    return d1, d2, args.db, args.output, args.open


def main():
    d1, d2, db_path, output, open_after = parse_args()
    if output is None:
        output = f"report_{d1.replace('/', '-')}_{d2.replace('/', '-')}.html"

    check_db(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    print(f"→ Fetching data for {d1} .. {d2}")

    # 1) Raw factors
    factors = fetch_factors_raw(cur, d1, d2)

    # 2) Documents in range
    documents = fetch_documents(cur, d1, d2)

    # 3) Extra sanads for factor enrichment (in case SanadDate ≠ Factor_Date)
    factor_sanads   = {f["Sanad_Num"] for f in factors if f.get("Sanad_Num")}
    in_range_sanads = {d["Sanad_Num"] for d in documents}
    extra_sanads    = factor_sanads - in_range_sanads
    extra_docs      = fetch_sanads_by_nums(cur, list(extra_sanads)) if extra_sanads else []

    # 4) Build sanad lookup
    sanad_rows_by_num = defaultdict(list)
    for d in documents + extra_docs:
        sanad_rows_by_num[d["Sanad_Num"]].append(d)

    # 5) Enrich factors
    enrich_factors(factors, sanad_rows_by_num)

    # 6) Checks — all in range, then split
    checks_all = fetch_checks(cur, d1, d2)
    checks_due, checks_active = split_checks(checks_all, d1, d2)

    # 7) Transactions
    transactions = group_transactions(documents)

    conn.close()

    print(f"   factors={len(factors)}  transactions={len(transactions)}")
    print(f"   checks: due={len(checks_due)}  active={len(checks_active)}")

    html = HTML_TEMPLATE.format(
        from_date=d1, to_date=d2,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        cards=build_cards(factors, transactions, checks_all, d1, d2),
        factors_section=section_factors(factors),
        transactions_section=section_transactions(transactions),
        checks_due_section=section_checks_due(checks_due, d1, d2),
        checks_section=section_checks(checks_active, d1, d2),
        customer_section=section_by_customer(factors, transactions, checks_all, d1, d2),
        bank_section=section_by_bank(checks_all, d1, d2),
    )

    with open(output, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"✓ Report saved: {os.path.abspath(output)}")
    if open_after:
        webbrowser.open("file://" + os.path.abspath(output))


if __name__ == "__main__":
    main()
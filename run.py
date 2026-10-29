#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_daily_report.py
اجرای خودکار چرخه گزارش روزانه:
    1) کپی دیتابیس SQL Server → SQLite
    2) ساخت گزارش برای تاریخ امروز (شمسی)
    3) باز کردن گزارش در مرورگر

Usage:
    python run_daily_report.py
    python run_daily_report.py --days-back 1
    python run_daily_report.py --source db_1405 --no-open
    python run_daily_report.py --skip-copy       # فقط گزارش
    python run_daily_report.py --skip-report     # فقط کپی
    python run_daily_report.py --date 1405/08/07 # تاریخ دستی
"""

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, date


# ==============================================================
#  مسیرها
# ==============================================================
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
COPY_SCRIPT    = os.path.join(SCRIPT_DIR, "deepseek_copy_db.py")
REPORT_SCRIPT  = os.path.join(SCRIPT_DIR, "deepseek_make_report.py")
DEFAULT_DB     = "local_db_1405.db"


# ==============================================================
#  تبدیل میلادی → شمسی
# ==============================================================
def gregorian_to_jalali(gy, gm, gd):
    """Convert Gregorian (y, m, d) to Jalali (y, m, d)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1

    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    g_day_no += g_d_m[gm2] + gd2
    if gm2 > 1 and ((gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)):
        g_day_no += 1

    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053

    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461

    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365

    if j_day_no < 186:
        jm = 1 + j_day_no // 31
        jd = 1 + j_day_no % 31
    else:
        jm = 7 + (j_day_no - 186) // 30
        jd = 1 + (j_day_no - 186) % 30

    return jy, jm, jd


def today_jalali(days_back=0):
    """Return Jalali date string for today (minus days_back) as YYYY/MM/DD."""
    d = date.today()
    if days_back:
        from datetime import timedelta
        d -= timedelta(days=days_back)
    jy, jm, jd = gregorian_to_jalali(d.year, d.month, d.day)
    return f"{jy}/{jm:02d}/{jd:02d}"


# ==============================================================
#  اجرای اسکریپت
# ==============================================================
def run_script(label, cmd, cwd=SCRIPT_DIR):
    print(f"\n▶ [{label}]")
    print(f"   $ {' '.join(cmd)}")
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=cwd, check=False)
    except FileNotFoundError:
        print(f"✗ فایل پیدا نشد: {cmd[0]}")
        return False
    elapsed = time.time() - t0
    if proc.returncode != 0:
        print(f"✗ {label} با کد خطای {proc.returncode} متوقف شد (زمان: {elapsed:.1f}s)")
        return False
    print(f"✓ {label} کامل شد ({elapsed:.1f}s)")
    return True


def find_report_file(report_date, db_name):
    """پیدا کردن آخرین فایل HTML گزارش."""
    expected = f"report_{report_date.replace('/', '-')}_{report_date.replace('/', '-')}.html"
    path = os.path.join(SCRIPT_DIR, expected)
    if os.path.exists(path):
        return path

    # fallback: جدیدترین report_*.html
    candidates = [
        os.path.join(SCRIPT_DIR, f)
        for f in os.listdir(SCRIPT_DIR)
        if f.startswith("report_") and f.endswith(".html")
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


# ==============================================================
#  Main
# ==============================================================
def parse_args():
    p = argparse.ArgumentParser(description="اجرای خودکار گزارش روزانه")
    p.add_argument("--source",    default="db_1405",
                   help="نام دیتابیس SQL Server (default: db_1405)")
    p.add_argument("--db",        default=DEFAULT_DB,
                   help=f"فایل SQLite خروجی (default: {DEFAULT_DB})")
    p.add_argument("--date",      default=None,
                   help="تاریخ گزارش YYYY/MM/DD (پیش‌فرض: امروز شمسی)")
    p.add_argument("--days-back", type=int, default=0,
                   help="چند روز قبل از امروز (پیش‌فرض: 0 = امروز)")
    p.add_argument("--skip-copy",   action="store_true", help="از مرحله کپی صرف‌نظر کن")
    p.add_argument("--skip-report", action="store_true", help="از مرحله گزارش صرف‌نظر کن")
    p.add_argument("--no-open",     action="store_true", help="مرورگر را باز نکن")
    p.add_argument("--copy-args",   default="", help="آرگومان‌های اضافی برای کپی")
    return p.parse_args()


def main():
    args = parse_args()
    start = time.time()

    # اعتبارسنجی مسیرها
    if not args.skip_copy and not os.path.exists(COPY_SCRIPT):
        print(f"✗ اسکریپت کپی پیدا نشد: {COPY_SCRIPT}", file=sys.stderr)
        sys.exit(2)
    if not args.skip_report and not os.path.exists(REPORT_SCRIPT):
        print(f"✗ اسکریپت گزارش پیدا نشد: {REPORT_SCRIPT}", file=sys.stderr)
        sys.exit(2)

    # تعیین تاریخ گزارش
    if args.date:
        report_date = args.date.strip()
    else:
        report_date = today_jalali(days_back=args.days_back)

    print("=" * 70)
    print("📊 چرخه گزارش روزانه حسابداری")
    print("=" * 70)
    print(f"📁 پوشه اجرا:   {SCRIPT_DIR}")
    print(f"🗓  تاریخ گزارش: {report_date}")
    print(f"💾 فایل SQLite:  {args.db}")

    # ── مرحله ۱: کپی دیتابیس ─────────────────────
    if not args.skip_copy:
        copy_cmd = [
            sys.executable, COPY_SCRIPT,
            "--source", args.source,
            "--output", args.db,
        ]
        if args.copy_args:
            copy_cmd.extend(args.copy_args.split())
        if not run_script("کپی دیتابیس", copy_cmd):
            print("\n⚠️  مرحله کپی ناموفق بود. ادامه با دیتابیس موجود…",
                  file=sys.stderr)
    else:
        print("\n⏭  [کپی دیتابیس] رد شد (--skip-copy)")

    # ── مرحله ۲: ساخت گزارش ─────────────────────
    if not args.skip_report:
        report_cmd = [
            sys.executable, REPORT_SCRIPT,
            "--date", report_date,
            "--db", args.db,
        ]
        if not run_script("ساخت گزارش", report_cmd):
            print("\n✗ ساخت گزارش ناموفق بود.", file=sys.stderr)
            sys.exit(3)
    else:
        print("\n⏭  [ساخت گزارش] رد شد (--skip-report)")

    # ── مرحله ۳: باز کردن گزارش ─────────────────
    if not args.no_open and not args.skip_report:
        report_path = find_report_file(report_date, args.db)
        if not report_path or not os.path.exists(report_path):
            print("\n⚠️  فایل گزارش پیدا نشد — مرورگر باز نمی‌شود.", file=sys.stderr)
        else:
            print(f"\n🌐 باز کردن گزارش: {os.path.basename(report_path)}")
            webbrowser.open("file://" + os.path.abspath(report_path))
    else:
        print("\n⏭  [باز کردن مرورگر] رد شد")

    elapsed = time.time() - start
    print("\n" + "=" * 70)
    print(f"✅ چرخه کامل شد در {elapsed:.1f} ثانیه")
    print("=" * 70)


if __name__ == "__main__":
    main()
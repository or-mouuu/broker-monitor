#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
券商分點大額買進監控系統 v2

使用方式：
  python3 broker_monitor.py                    # 產生 index.html
  python3 broker_monitor.py --email            # 產生 + 寄信
  python3 broker_monitor.py --list-branches    # 顯示分點清單
  python3 broker_monitor.py --add "URL"        # 新增分點
  python3 broker_monitor.py --remove "名稱"    # 刪除分點
"""

import subprocess, re, os, sys, smtplib, json, time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# ════════════════════════════════════════════════════════════
# 常數設定
# ════════════════════════════════════════════════════════════

SCRIPT_DIR    = Path(__file__).parent
BRANCHES_FILE = SCRIPT_DIR / "branches.json"
DATA_DIR      = SCRIPT_DIR / "data"
BROKER_JS_URL = "https://fubon-ebrokerdj.fbs.com.tw/z/js/zbrokerjs.djjs"
FUBON_BASE    = "https://fubon-ebrokerdj.fbs.com.tw/z/zg/zgb/zgb0.djhtm"
TWSE_API      = "https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date}&stockNo={ticker}"

SPIKE_THRESHOLD = 1.5   # 今日買超 / 五日均 ≥ 150% → 爆量
MIN_NET_DISPLAY = 3_000  # 最低顯示門檻（千元）
MAX_HISTORY     = 32     # 最多回溯交易日數（含 30日確認窗口所需緩衝）

EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", "or.mouuu@gmail.com")
EMAIL_SENDER    = os.getenv("EMAIL_SENDER",    "")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD",  "")
PAGES_URL       = os.getenv("PAGES_URL", "https://or-mouuu.github.io/broker-monitor/")

DEFAULT_BRANCHES = [
    {"名稱": "元大-崇德",   "a": "9800", "b": "0039003800310053"},
    {"名稱": "台新-五權西", "a": "9B00", "b": "0039004200320035"},
    {"名稱": "富邦-仁愛",   "a": "9600", "b": "9676"},
    {"名稱": "國票-安和",   "a": "7790", "b": "003700370039005a"},
    {"名稱": "國票證券",    "a": "7790", "b": "7790"},
    {"名稱": "國泰-敦南",   "a": "8880", "b": "8888"},
    {"名稱": "大和國泰",    "a": "8890", "b": "8890"},
]

# ════════════════════════════════════════════════════════════
# 分點管理
# ════════════════════════════════════════════════════════════

def load_branches() -> list[dict]:
    if BRANCHES_FILE.exists():
        return json.loads(BRANCHES_FILE.read_text("utf-8"))
    save_branches(DEFAULT_BRANCHES.copy())
    return DEFAULT_BRANCHES.copy()

def save_branches(branches: list[dict]):
    BRANCHES_FILE.write_text(
        json.dumps(branches, ensure_ascii=False, indent=2), "utf-8"
    )

def lookup_branch_name_from_js(a: str, b: str) -> str:
    """從 Fubon DJ JS 靜態資料查詢分點中文名稱"""
    cmd = f"curl -s '{BROKER_JS_URL}' | iconv -f big5 -t utf-8 2>/dev/null"
    js  = subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
    m   = re.search(r"g_BrokerList\s*=\s*'([^']+)'", js)
    if not m:
        return f"未知分點({a})"
    lookup = {}
    for group in m.group(1).split(';'):
        for entry in group.split('!'):
            parts = entry.split(',', 1)
            if len(parts) == 2:
                lookup[parts[0].strip()] = parts[1].strip()
    return lookup.get(b, lookup.get(a, f"未知({a}/{b[:8]})"))

def cli_add_branch(url: str):
    m = re.search(r'[?&]a=([^&\s]+).*[?&]b=([^&\s]+)', url)
    if not m:
        print("❌ URL 格式錯誤（需包含 a= 和 b= 參數）")
        return
    a, b = m.group(1), m.group(2)
    print(f"  查詢分點名稱 (a={a}, b={b})…")
    name = lookup_branch_name_from_js(a, b)
    print(f"  找到：{name}")
    confirm = input(f"  確認新增「{name}」？(y/n) ").strip().lower()
    if confirm != 'y':
        print("  已取消"); return
    branches = load_branches()
    if any(br["a"] == a and br["b"] == b for br in branches):
        print(f"  ⚠️ 分點已存在"); return
    branches.append({"名稱": name, "a": a, "b": b})
    save_branches(branches)
    print(f"  ✅ 已新增：{name}")

def cli_remove_branch(name: str):
    branches = load_branches()
    if not any(br["名稱"] == name for br in branches):
        names = [br["名稱"] for br in branches]
        print(f"❌ 找不到「{name}」\n現有分點：{names}"); return
    confirm = input(f"  確認刪除「{name}」？(y/n) ").strip().lower()
    if confirm != 'y':
        print("  已取消"); return
    save_branches([br for br in branches if br["名稱"] != name])
    print(f"  ✅ 已刪除：{name}")

def cli_list_branches():
    branches = load_branches()
    print(f"\n現有監控分點（共 {len(branches)} 個）：")
    for i, b in enumerate(branches, 1):
        print(f"  {i:2}. {b['名稱']:<14}  a={b['a']}  b={b['b'][:16]}…")

# ════════════════════════════════════════════════════════════
# Fubon DJ 資料抓取
# ════════════════════════════════════════════════════════════

def fetch_html(a: str, b: str, days: int = 1) -> str:
    url = f"{FUBON_BASE}?a={a}&b={b}"
    if days > 1:
        url += f"&c=B&d={days}"
    cmd = (
        f"curl -s --compressed "
        f"-H 'Accept-Language: zh-TW,zh;q=0.9' -H 'User-Agent: Mozilla/5.0' "
        f"'{url}' | iconv -f big5 -t utf-8 2>/dev/null"
    )
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout

def parse_buy_stocks(html: str) -> tuple[list[dict], str]:
    buy_start = html.find('<tr><td class="t2" colspan="4">買超</td></tr>')
    if buy_start == -1:
        return [], ""
    sell_start = html.find('<tr><td class="t2" colspan="4">賣超</td></tr>', buy_start)
    section   = html[buy_start:sell_start] if sell_start != -1 else html[buy_start:]
    date_m    = re.search(r'資料日期：(\d{8})', html)
    data_date = date_m.group(1) if date_m else ""
    stocks = []
    for entry in re.split(r'<td class="t4t1"', section)[1:]:
        m1 = re.search(r"GenLink2stk\('AS(\w+)','([^']+)'\)", entry)
        m2 = re.search(r"Link2Stk\('(\w+)'\)[^>]*>([\w\-\+\s一-鿿\.\*（）]+)</a>", entry)
        if m1:
            ticker, name = m1.group(1), m1.group(2)
        elif m2:
            ticker, name = m2.group(1), m2.group(2).strip()
        else:
            continue
        nums = re.findall(r'class="t3n1"[^>]*>([\d,]+)</td>', entry)
        if len(nums) >= 3:
            stocks.append({
                "ticker": ticker, "name": name,
                "buy":  int(nums[0].replace(',', '')),
                "sell": int(nums[1].replace(',', '')),
                "net":  int(nums[2].replace(',', '')),
            })
    return stocks, data_date

# ════════════════════════════════════════════════════════════
# 歷史補抓（backfill）
# ════════════════════════════════════════════════════════════

def _date_param(date_str: str) -> str:
    """YYYYMMDD → YYYY-M-D（Fubon DJ 不補零）"""
    return f"{date_str[:4]}-{int(date_str[4:6])}-{int(date_str[6:])}"

def fetch_html_dated(a: str, b: str, date_str: str) -> str:
    """抓取指定交易日（YYYYMMDD）單日資料"""
    dp  = _date_param(date_str)
    url = f"{FUBON_BASE}?a={a}&b={b}&c=B&e={dp}&f={dp}"
    cmd = (
        f"curl -s --compressed "
        f"-H 'Accept-Language: zh-TW,zh;q=0.9' -H 'User-Agent: Mozilla/5.0' "
        f"'{url}' | iconv -f big5 -t utf-8 2>/dev/null"
    )
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout

def get_recent_weekdays(n: int, skip: set) -> list[str]:
    """從昨天往前找 n 個平日（週一~五），排除 skip 集合，回傳 YYYYMMDD 清單（新→舊）"""
    from datetime import date, timedelta
    result, d = [], date.today() - timedelta(days=1)
    while len(result) < n:
        s = d.strftime("%Y%m%d")
        if d.weekday() < 5 and s not in skip:
            result.append(s)
        d -= timedelta(days=1)
    return result

def cli_backfill(n_days: int = 5):
    """補抓最近 n_days 個交易日的單日快照（已有的自動跳過）"""
    DATA_DIR.mkdir(exist_ok=True)
    branches = load_branches()
    existing = {f.stem for f in DATA_DIR.glob("*.json")}
    # 候補清單（新→舊），多取一倍以應付假日
    candidates = get_recent_weekdays(n_days * 3, skip=existing)

    saved, tried = 0, 0
    for date_str in candidates:   # 由新→舊，確保取最近 n 個交易日
        if saved >= n_days:
            break
        tried += 1
        d_disp = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
        print(f"\n  📅 補抓 {d_disp}…")
        branch_data: dict[str, list] = {}
        actual_date = ""
        is_trading_day = True

        for br in branches:
            print(f"    ▶ {br['名稱']}…", end=" ", flush=True)
            html = fetch_html_dated(br["a"], br["b"], date_str)
            stocks, ret_date = parse_buy_stocks(html)

            if not ret_date:
                # 回傳空日期 → 可能是假日，整天跳過
                print("⚠️ 無資料（假日？）")
                is_trading_day = False
                break

            actual_date = ret_date
            print(f"{len(stocks)} 筆")
            branch_data[br["名稱"]] = [
                {"ticker": s["ticker"], "name": s["name"],
                 "buy": s["buy"], "sell": s["sell"], "net": s["net"]}
                for s in stocks
            ]

        if not is_trading_day:
            print(f"  ↳ {d_disp} 為假日，跳過")
            continue

        path = DATA_DIR / f"{actual_date}.json"
        path.write_text(
            json.dumps({"date": actual_date, "branches": branch_data},
                       ensure_ascii=False, separators=(',', ':')),
            "utf-8"
        )
        print(f"  ✅ 已儲存 {path.name}")
        saved += 1

    total = len(list(DATA_DIR.glob("*.json")))
    print(f"\n補抓完成：新增 {saved} 天，data/ 共 {total} 個快照")


def cli_backfill_merge():
    """對現有快照補抓缺漏分點（新增分點時使用），不刪除既有資料"""
    branches = load_branches()
    files = sorted(DATA_DIR.glob("*.json"), reverse=True)[:MAX_HISTORY]
    if not files:
        print("⚠️ 無現有快照，請先執行 --backfill"); return

    updated = 0
    for f in files:
        snap     = json.loads(f.read_text("utf-8"))
        date_str = snap["date"]
        d_disp   = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
        existing = set(snap.get("branches", {}).keys())
        missing  = [b for b in branches if b["名稱"] not in existing]

        if not missing:
            print(f"  {d_disp}：分點完整，跳過")
            continue

        print(f"\n  📅 {d_disp}：補抓 {[b['名稱'] for b in missing]}")
        any_ok = False
        for br in missing:
            print(f"    ▶ {br['名稱']}…", end=" ", flush=True)
            html = fetch_html_dated(br["a"], br["b"], date_str)
            stocks, ret_date = parse_buy_stocks(html)
            if not ret_date:
                print("⚠️ 無資料")
                continue
            snap["branches"][br["名稱"]] = [
                {"ticker": s["ticker"], "name": s["name"],
                 "buy": s["buy"], "sell": s["sell"], "net": s["net"]}
                for s in stocks
            ]
            print(f"{len(stocks)} 筆")
            any_ok = True

        if any_ok:
            f.write_text(
                json.dumps(snap, ensure_ascii=False, separators=(',', ':')), "utf-8"
            )
            print(f"  ✅ {f.name} 已更新")
            updated += 1

    print(f"\n合併補抓完成：更新 {updated} 個快照")


# ════════════════════════════════════════════════════════════
# TWSE 三大法人（外資 / 投信）
# ════════════════════════════════════════════════════════════

FINI_MIN_LOTS = 500  # 低於此張數視為中性（張 = 千股）

def fetch_fini(date_str: str) -> dict[str, dict]:
    """抓取 TWSE T86 三大法人明細，回傳 {ticker: {"fini_net": 張, "trust_net": 張}}"""
    url = (f"https://www.twse.com.tw/fund/T86?response=json"
           f"&date={date_str}&selectType=ALL")
    try:
        out = subprocess.run(
            ['curl', '-s', '--max-time', '15', '-H', 'User-Agent: Mozilla/5.0', url],
            capture_output=True, text=True, timeout=20
        ).stdout
        data = json.loads(out)
        if data.get("stat") != "OK":
            print("  ⚠️ TWSE T86 無資料")
            return {}
        result: dict[str, dict] = {}
        for row in data.get("data", []):
            try:
                tk        = row[0].strip()
                fini_net  = int(row[4].replace(",", "")) // 1000
                trust_net = int(row[10].replace(",", "")) // 1000
                result[tk] = {"fini_net": fini_net, "trust_net": trust_net}
            except Exception:
                pass
        print(f"  外資/投信資料：{len(result)} 檔")
        return result
    except Exception as e:
        print(f"  ⚠️ fetch_fini 失敗：{e}")
        return {}

# ════════════════════════════════════════════════════════════
# TWSE 成交量（佔市場比 %）
# ════════════════════════════════════════════════════════════

def _roc_to_ymd(roc: str) -> str:
    """民國日期 "115/06/26" → "20260626" """
    p = roc.split("/")
    return f"{int(p[0])+1911}{p[1]}{p[2]}"

def _fetch_twse_month(ticker: str, date_str: str) -> tuple[dict, dict, int]:
    """抓取單一 ticker 單月資料。回傳 (monthly, monthly_ohlc, last_val)
    last_val = 該月最後一筆成交金額（千元），供無精確日期比對時的 fallback。"""
    url = TWSE_API.format(date=date_str, ticker=ticker)
    monthly: dict[str, int] = {}
    monthly_ohlc: dict[str, dict] = {}
    last_val = 0
    try:
        out = subprocess.run(
            ['curl', '-s', '--max-time', '10', '-H', 'User-Agent: Mozilla/5.0', url],
            capture_output=True, text=True, timeout=15
        ).stdout
        data = json.loads(out)
        rows = data.get('data', [])
        for row in rows:
            try:
                ad = _roc_to_ymd(row[0])
                monthly[ad] = int(row[2].replace(',', '')) // 1000
                if row[3] not in ('--', ' '):
                    monthly_ohlc[ad] = {
                        "o": float(row[3].replace(',', '')),
                        "h": float(row[4].replace(',', '')),
                        "l": float(row[5].replace(',', '')),
                        "c": float(row[6].replace(',', '')),
                    }
            except Exception:
                pass
        if rows:
            try:
                last_val = int(rows[-1][2].replace(',', '')) // 1000
            except Exception:
                pass
    except Exception:
        pass
    return monthly, monthly_ohlc, last_val


def _prev_month_date(date_str: str) -> str:
    """回傳上個月月份的日期字串（TWSE STOCK_DAY 依年月回傳整月資料，日期取哪天皆可）"""
    y, m = int(date_str[:4]), int(date_str[4:6])
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y}{m:02d}01"


def _fetch_one_volume(ticker: str, date_str: str) -> tuple[str, int, dict, dict]:
    """回傳 (ticker, 當日千元, {yyyymmdd:千元}, {yyyymmdd:{o,h,l,c}})。
    單月最多僅約 20 個交易日，不足以支撐 30 日確認窗口，故當月資料不足時
    一律額外合併上個月資料，供囤貨分數 14日/30日 等跨月指標使用。"""
    monthly, monthly_ohlc, last_val = _fetch_twse_month(ticker, date_str)

    if len(monthly) < CONFIRM_WINDOW:
        time.sleep(0.1)
        prev_monthly, prev_ohlc, _ = _fetch_twse_month(ticker, _prev_month_date(date_str))
        for d, v in prev_monthly.items():
            monthly.setdefault(d, v)
        for d, v in prev_ohlc.items():
            monthly_ohlc.setdefault(d, v)

    today_val = monthly.get(date_str, last_val)
    return ticker, today_val, monthly, monthly_ohlc

def fetch_twse_volumes(
    tickers: list[str], date_str: str
) -> tuple[dict, dict, dict]:
    """並發抓取各股票 TWSE 成交金額與 OHLC。
    回傳 (twse_vol, twse_monthly, twse_ohlc)"""
    print(f"  載入 TWSE 成交量/股價（{len(tickers)} 檔，並發 10）…", end=" ", flush=True)
    twse_vol: dict[str, int] = {}
    twse_monthly: dict[str, dict[str, int]] = {}
    twse_ohlc: dict[str, dict[str, dict]] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch_one_volume, t, date_str): t for t in tickers}
        for f in as_completed(futures):
            tk, val, monthly, ohlc = f.result()
            if val > 0:
                twse_vol[tk] = val
            if monthly:
                twse_monthly[tk] = monthly
            if ohlc:
                twse_ohlc[tk] = ohlc
    print(f"成功 {len(twse_vol)}/{len(tickers)} 檔")
    return twse_vol, twse_monthly, twse_ohlc

# ════════════════════════════════════════════════════════════
# 歷史資料（連買天數）
# ════════════════════════════════════════════════════════════

def save_daily_snapshot(all_branches: list[dict], data_date: str):
    """將今日各分點買超資料存成 data/YYYYMMDD.json"""
    DATA_DIR.mkdir(exist_ok=True)
    snapshot = {
        "date": data_date,
        "branches": {
            b["名稱"]: [
                {"ticker": s["ticker"], "name": s["name"],
                 "buy": s["buy"], "sell": s["sell"], "net": s["net"]}
                for s in b["stocks"]
            ]
            for b in all_branches
        }
    }
    path = DATA_DIR / f"{data_date}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')), "utf-8")
    print(f"  已儲存快照：{path.name}")

def load_history() -> list[dict]:
    """載入最近 MAX_HISTORY 個交易日快照，由新到舊排列"""
    DATA_DIR.mkdir(exist_ok=True)
    files = sorted(DATA_DIR.glob("*.json"), reverse=True)[:MAX_HISTORY]
    history = []
    for f in files:
        try:
            history.append(json.loads(f.read_text("utf-8")))
        except Exception:
            pass
    return history

ACCUM_WINDOW   = 7                       # 積累觀察窗口（交易日）
ACCUM_MIN_DAYS = 3                       # 至少幾天才算積累
ACCUM_MIN_TOT  = MIN_NET_DISPLAY * 2    # 積累最低總量門檻

def get_accumulation_data(
    history: list[dict], branch_name: str, ticker: str,
    today_net: int, today_buy: int = 0, today_date: str = ""
) -> dict:
    """
    回傳連買資訊、累計量、折線圖資料、積累信號。
    history 由新→舊，已排除今天的快照。
    """
    # 收集最多 10 天資料（today + 前 9 天）以支援 10 日累計，但積累邏輯仍用 ACCUM_WINDOW
    COLLECT = max(ACCUM_WINDOW, 10)
    daily       = [today_net]
    daily_buy   = [today_buy]
    daily_dates = [today_date]
    for day in history[: COLLECT - 1]:
        stocks = day.get("branches", {}).get(branch_name, [])
        hit    = next((s for s in stocks if s["ticker"] == ticker), None)
        daily.append(hit["net"] if hit else 0)
        daily_buy.append(hit["buy"] if hit else 0)
        daily_dates.append(day.get("date", ""))
    # daily[0]=今, daily[-1]=最舊

    # 10日累計（用前 10 筆，不足時取全部）
    d10_net = sum(daily[:min(10, len(daily))])

    # 積累邏輯僅看 ACCUM_WINDOW 內
    daily = daily[:ACCUM_WINDOW]
    daily_buy = daily_buy[:ACCUM_WINDOW]
    daily_dates = daily_dates[:ACCUM_WINDOW]

    # 連買天數（從今天起連續正值）
    streak = 0
    for n in daily:
        if n > 0: streak += 1
        else:     break

    streak_daily      = list(reversed(daily[:streak]))       # 最舊→最新
    streak_total      = sum(streak_daily)
    streak_buy_gross  = sum(daily_buy[:streak])              # 期間買進合計（千元）
    streak_dates      = list(reversed(daily_dates[:streak])) # 最舊→最新
    buy_days          = sum(1 for n in daily if n > 0)
    window_buy_tot    = sum(n for n in daily if n > 0)
    window_buy_gross  = sum(b for b, n in zip(daily_buy, daily) if n > 0)
    window_dates      = list(reversed([d for d, n in zip(daily_dates, daily) if n > 0]))
    all_daily         = list(reversed(daily))                # 最舊→最新

    # 積累信號：窗口 ≥3 天有買、但非全連續（中間有缺口）
    is_accumulating = (
        buy_days  >= ACCUM_MIN_DAYS
        and streak < buy_days          # 至少有一天缺口
        and window_buy_tot >= ACCUM_MIN_TOT
    )

    return {
        "streak":           streak,
        "streak_total":     streak_total,
        "streak_daily":     streak_daily,
        "streak_buy_gross": streak_buy_gross,
        "streak_dates":     streak_dates,
        "buy_days":         buy_days,
        "window_days":      len(daily),
        "window_buy_tot":   window_buy_tot,
        "window_buy_gross": window_buy_gross,
        "window_dates":     window_dates,
        "is_accumulating":  is_accumulating,
        "all_daily":        all_daily,
        "d10_net":          d10_net,
    }


def make_sparkline(daily: list[float], w: int = 64, h: int = 20) -> str:
    """生成折線 SVG（daily: 最舊→最新的 net 值，千元）"""
    n = len(daily)
    if n < 2:
        return ""
    max_abs = max(abs(v) for v in daily) or 1
    mid     = h / 2

    xs = [round(1 + i / (n - 1) * (w - 2), 1) for i in range(n)]
    ys = [round(mid - (v / max_abs) * (mid - 2), 1) for v in daily]

    pts   = " ".join(f"{x},{y}" for x, y in zip(xs, ys))
    color = "#ef4444" if daily[-1] > 0 else "#16a34a"
    base  = f'<line x1="0" y1="{mid}" x2="{w}" y2="{mid}" stroke="#e2e8f0" stroke-width="0.8"/>'
    line  = (f'<polyline points="{pts}" fill="none" stroke="{color}" '
             f'stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>')
    dots  = "".join(
        f'<circle cx="{x}" cy="{y}" r="2" fill="{"#ef4444" if v>0 else "#16a34a" if v<0 else "#94a3b8"}"/>'
        for x, y, v in zip(xs, ys, daily)
    )
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'style="vertical-align:middle;margin-left:3px;flex-shrink:0" '
            f'xmlns="http://www.w3.org/2000/svg">{base}{line}{dots}</svg>')

def make_candle_svg(ohlc_list: list[dict], w: int = 72, h: int = 28) -> str:
    """生成 5 日迷你 K 線圖 SVG（ohlc_list: 最舊→最新）"""
    n = len(ohlc_list)
    if n < 1:
        return ""
    p_min = min(x["l"] for x in ohlc_list)
    p_max = max(x["h"] for x in ohlc_list)
    p_rng = (p_max - p_min) or (p_min * 0.01) or 1
    pad = 2

    def py(price: float) -> float:
        return pad + (p_max - price) / p_rng * (h - pad * 2)

    body_w = max(4, w // n - 3)
    gap    = max(1, (w - n * body_w) / (n + 1))
    parts: list[str] = []
    for i, d in enumerate(ohlc_list):
        xc = gap + i * (body_w + gap) + body_w / 2
        xl = gap + i * (body_w + gap)
        y_hi  = py(d["h"]); y_lo = py(d["l"])
        y_o   = py(d["o"]); y_c  = py(d["c"])
        y_top = min(y_o, y_c); bh = max(1.0, abs(y_o - y_c))
        col   = "#ef4444" if d["c"] >= d["o"] else "#16a34a"
        parts.append(f'<line x1="{xc:.1f}" y1="{y_hi:.1f}" x2="{xc:.1f}" y2="{y_lo:.1f}" '
                     f'stroke="{col}" stroke-width="1"/>')
        parts.append(f'<rect x="{xl:.1f}" y="{y_top:.1f}" width="{body_w}" '
                     f'height="{bh:.1f}" fill="{col}"/>')
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'style="vertical-align:middle;flex-shrink:0" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')


def _get_recent_ohlc(ticker: str, upto_date: str, twse_ohlc: dict, n: int = 5) -> list[dict]:
    """取最近 n 個交易日 OHLC（最舊→最新），上限為 upto_date"""
    ohlc_map = twse_ohlc.get(ticker, {})
    dates = sorted(d for d in ohlc_map if d <= upto_date)[-n:]
    return [ohlc_map[d] for d in dates]

# ════════════════════════════════════════════════════════════
# 整合資料
# ════════════════════════════════════════════════════════════

def fetch_branch(branch: dict) -> dict:
    """抓取今日 + 五日資料，計算爆量倍率。連買天數由 main() 另行計算。"""
    a, b, name = branch["a"], branch["b"], branch["名稱"]
    print(f"  ▶ 抓取 {name}…", end=" ", flush=True)
    today_html   = fetch_html(a, b, days=1)
    fiveday_html = fetch_html(a, b, days=5)
    today_stocks, data_date = parse_buy_stocks(today_html)
    fiveday_stocks, _       = parse_buy_stocks(fiveday_html)
    fd_map = {s["ticker"]: s for s in fiveday_stocks}

    merged = []
    for s in today_stocks:
        tk      = s["ticker"]
        fd      = fd_map.get(tk)
        avg_net = (fd["net"] / 5) if fd else 0
        spike   = (s["net"] / avg_net) if avg_net > 0 else None
        merged.append({
            **s,
            "avg_net":     avg_net,
            "fiveday_net": fd["net"] if fd else 0,  # 5日累計買超（千元）
            "spike":       spike,
            "is_spike":    spike is not None and spike >= SPIKE_THRESHOLD,
            # 以下由 apply_accumulation() 填入
            "streak": 0, "streak_total": 0, "streak_daily": [],
            "buy_days": 0, "window_days": 0, "window_buy_tot": 0,
            "is_accumulating": False, "all_daily": [], "d10_net": 0,
            "is_strong_accum": False,
        })

    print(f"今日 {len(today_stocks)} 筆，五日 {len(fiveday_stocks)} 筆")
    return {"名稱": name, "a": a, "b": b, "stocks": merged, "data_date": data_date}


def apply_accumulation(all_branches: list[dict], history: list[dict], data_date: str = ""):
    """已確認 data_date 並過濾今日後，計算各股籌碼動能資料。"""
    for br in all_branches:
        for s in br["stocks"]:
            s.update(get_accumulation_data(
                history, br["名稱"], s["ticker"],
                s["net"], s.get("buy", 0), data_date
            ))

STRONG_ACCUM_MIN_DAYS  = 5      # 至少 5 天有買
STRONG_ACCUM_MIN_VOL_PCT = 0.5  # 期間佔市場量 ≥ 0.5%

def apply_strong_accum(all_branches: list[dict], twse_monthly: dict):
    """在 TWSE 月量載入後，標記強積累（積累 + buy_days≥5 + 期間佔市場量≥0.5%）"""
    for br in all_branches:
        for s in br["stocks"]:
            s["is_strong_accum"] = False
            if not s.get("is_accumulating"):
                continue
            if s.get("buy_days", 0) < STRONG_ACCUM_MIN_DAYS:
                continue
            tk      = s["ticker"]
            _dates  = s.get("window_dates", [])
            _buy_g  = s.get("window_buy_gross", 0)
            mkt_sum = sum(twse_monthly.get(tk, {}).get(d, 0) for d in _dates)
            if mkt_sum > 0 and _buy_g / mkt_sum * 100 >= STRONG_ACCUM_MIN_VOL_PCT:
                s["is_strong_accum"] = True

HOARD_WINDOW    = 14      # 囤貨觀察窗口（交易日）
HOARD_MIN_DAYS  = 6       # 窗口內至少買超天數
HOARD_MIN_TOT   = 30_000  # 窗口累積買超下限（千元），過濾雜訊
HOARD_SCORE_MIN = 60      # 進榜門檻

CONFIRM_WINDOW   = 30      # 30日確認窗口（交易日），輔助訊號：長線是否持續佈局
CONFIRM_MIN_DAYS = 12      # 30日窗口內至少買超天數（≈40%）
CONFIRM_MIN_TOT  = 60_000  # 30日窗口累積買超下限（千元）

def _hoard_window_dates(ticker: str, twse_ohlc: dict, fallback_dates: list[str],
                        data_date: str, window: int = HOARD_WINDOW) -> tuple[list[str], bool]:
    """回傳該股近 window 個交易日（舊→新）及是否為無價格資料（如上櫃股）。
    優先用 TWSE 價格日曆；查無價格資料時退回本地快照日期序列。"""
    price_days = sorted(d for d in twse_ohlc.get(ticker, {}) if d <= data_date)
    if price_days:
        return price_days[-window:], False
    local_days = sorted(d for d in fallback_dates if d <= data_date)
    return local_days[-window:], True


def compute_hoarding(all_branches: list[dict], history: list[dict],
                     twse_monthly: dict, twse_ohlc: dict, data_date: str) -> list[dict]:
    """計算「分點 × 個股」囤貨分數（0~100）。history 由新→舊，已排除今日。
    五維計分：持續性(25) 逆勢買(25) 隱蔽性(20) 吃貨力度(20) 成本優勢(10)。
    僅回傳 score ≥ HOARD_SCORE_MIN 的配對，依分數降冪排序。"""
    if not data_date:
        return []

    # 快照索引：date → branch → ticker → {net, buy, name}
    snap_idx: dict[str, dict[str, dict[str, dict]]] = {}
    for h in history:
        d = h.get("date")
        if not d:
            continue
        snap_idx[d] = {
            br: {s["ticker"]: {"net": s["net"], "buy": s["buy"], "name": s["name"]} for s in stocks}
            for br, stocks in h.get("branches", {}).items()
        }
    snap_idx[data_date] = {
        b["名稱"]: {s["ticker"]: {"net": s["net"], "buy": s["buy"], "name": s["name"]} for s in b["stocks"]}
        for b in all_branches
    }
    all_dates = sorted(snap_idx.keys())

    # 候選配對：近 HOARD_WINDOW 個快照日內曾出現在買超榜的 (分點, 代號)
    # 僅限目前仍在監控的分點（branches.json），避免已下架分點的舊快照資料混入
    current_branch_names = {b["名稱"] for b in all_branches}
    recent_dates = all_dates[-HOARD_WINDOW:]
    candidates: set[tuple[str, str]] = set()
    for d in recent_dates:
        for br, stocks in snap_idx[d].items():
            if br not in current_branch_names:
                continue
            for tk in stocks:
                candidates.add((br, tk))

    raw: list[dict] = []
    for br, tk in candidates:
        trading_days, no_price = _hoard_window_dates(tk, twse_ohlc, all_dates, data_date)
        if len(trading_days) < HOARD_MIN_DAYS:
            continue
        name = ""
        daily_net: dict[str, int] = {}
        daily_buy: dict[str, int] = {}
        for d in trading_days:
            hit = snap_idx.get(d, {}).get(br, {}).get(tk)
            if hit:
                daily_net[d] = hit["net"]
                daily_buy[d] = hit["buy"]
                name = hit["name"]
            else:
                daily_net[d] = 0
                daily_buy[d] = 0
        buy_days  = sum(1 for v in daily_net.values() if v > 0)
        total_net = sum(v for v in daily_net.values() if v > 0)
        if buy_days < HOARD_MIN_DAYS or total_net < HOARD_MIN_TOT:
            continue

        gross_buy  = sum(daily_buy.values())
        market_sum = sum(twse_monthly.get(tk, {}).get(d, 0) for d in trading_days)
        ohlc_all   = twse_ohlc.get(tk, {})
        close_now  = ohlc_all.get(data_date, {}).get("c", 0)

        raw.append({
            "branch": br, "ticker": tk, "name": name, "no_price": no_price,
            "trading_days": trading_days, "daily_net": daily_net,
            "buy_days": buy_days, "total_net": total_net, "gross_buy": gross_buy,
            "market_sum": market_sum, "close_now": close_now, "ohlc_all": ohlc_all,
        })

    if not raw:
        return []

    # 分點內排名（前 30%，以 gross_buy 排序）供「隱蔽性」維度使用
    branch_groups: dict[str, list[float]] = defaultdict(list)
    for r in raw:
        branch_groups[r["branch"]].append(r["gross_buy"])
    branch_p70: dict[str, float] = {}
    for br, vals in branch_groups.items():
        vals_sorted = sorted(vals)
        idx = max(0, int(len(vals_sorted) * 0.7) - 1)
        branch_p70[br] = vals_sorted[idx]

    results: list[dict] = []
    for r in raw:
        trading_days = r["trading_days"]
        daily_net    = r["daily_net"]
        no_price     = r["no_price"]
        close_now    = r["close_now"]

        # 持續性：買超天數 / HOARD_WINDOW，≥0.6 拿滿分 25
        persistence_score = 25.0 * min((r["buy_days"] / HOARD_WINDOW) / 0.6, 1.0)

        dip_score = stealth_score = cost_edge_score = 0.0
        est_shares = est_cost = cost_dev_pct = 0.0

        if not no_price and trading_days:
            ohlc_all   = r["ohlc_all"]
            all_sorted = sorted(ohlc_all.keys())

            # 逆勢買：股價收跌日中仍買超的比例
            down_days = []
            for d in trading_days:
                try:
                    idx = all_sorted.index(d)
                except ValueError:
                    continue
                if idx == 0:
                    continue
                prev_c = ohlc_all[all_sorted[idx - 1]]["c"]
                cur_c  = ohlc_all[d]["c"]
                if cur_c < prev_c:
                    down_days.append(d)
            down_n      = len(down_days)
            buy_on_down = sum(1 for d in down_days if daily_net.get(d, 0) > 0)
            if down_n < 3:
                # 收跌日樣本太少：按比例縮小逆勢買配分，剩餘配分攤給持續性
                dip_weight = 25.0 * (down_n / 3)
                dip_score  = dip_weight * (buy_on_down / down_n) if down_n > 0 else 0.0
                persistence_score = min(25.0, persistence_score + (25.0 - dip_weight))
            else:
                dip_score = 25.0 * min((buy_on_down / down_n) / 0.5, 1.0)

            # 隱蔽性：分點內買超金額排前 30% 且期間漲幅未大幅反應（漲幅曲線：<=5%滿分，5~15%線性遞減，>15%為0）
            first_c = ohlc_all.get(trading_days[0], {}).get("c", 0)
            period_return_pct = (close_now - first_c) / first_c * 100 if first_c else 0.0
            rank_ok = r["gross_buy"] >= branch_p70.get(r["branch"], 0)
            if rank_ok:
                if period_return_pct <= 5:
                    stealth_score = 20.0
                elif period_return_pct >= 15:
                    stealth_score = 0.0
                else:
                    stealth_score = 20.0 * (15 - period_return_pct) / 10

            # 成本優勢：現價相對估計成本的乖離（[-3%,+5%]滿分，向兩側線性遞減至 ±15%）
            est_shares = sum(
                daily_net[d] / ohlc_all[d]["c"]
                for d in trading_days if daily_net.get(d, 0) > 0 and ohlc_all.get(d, {}).get("c")
            )
            est_cost = (r["total_net"] / est_shares) if est_shares > 0 else close_now
            cost_dev_pct = (close_now / est_cost - 1) * 100 if est_cost else 0.0
            if -3 <= cost_dev_pct <= 5:
                cost_edge_score = 10.0
            elif cost_dev_pct > 5:
                cost_edge_score = 10.0 * max(0.0, (15 - cost_dev_pct) / 10)
            else:
                cost_edge_score = 10.0 * max(0.0, (cost_dev_pct + 15) / 12)

        # 吃貨力度：累積買超（毛額）÷ 期間市場成交金額，≥1.0% 滿分 20，≤0.2% 為 0
        absorption_score = 0.0
        if r["market_sum"] > 0:
            ratio = r["gross_buy"] / r["market_sum"] * 100
            absorption_score = 20.0 * min(max((ratio - 0.2) / 0.8, 0.0), 1.0)

        total_score = min(100.0, persistence_score + dip_score + stealth_score
                                 + absorption_score + cost_edge_score)
        if total_score < HOARD_SCORE_MIN:
            continue

        results.append({
            "branch": r["branch"], "ticker": r["ticker"], "name": r["name"],
            "score": round(total_score, 1),
            "buy_days": r["buy_days"], "window_days": len(trading_days), "total_net": r["total_net"],
            "est_shares": round(est_shares), "est_cost": round(est_cost, 2),
            "close": round(close_now, 2), "cost_dev_pct": round(cost_dev_pct, 2),
            "no_price": no_price,
            "dims": {
                "persistence": round(persistence_score, 1), "dip_buying": round(dip_score, 1),
                "stealth": round(stealth_score, 1), "absorption": round(absorption_score, 1),
                "cost_edge": round(cost_edge_score, 1),
            },
        })

    ticker_branch_count: dict[str, int] = defaultdict(int)
    for r in results:
        ticker_branch_count[r["ticker"]] += 1
    for r in results:
        r["co_hoard_count"] = ticker_branch_count[r["ticker"]]

    # 30日確認：僅對已進榜的配對計算，作為長線佈局的輔助信心標記（不影響原始 14 日分數）
    for r in results:
        br, tk = r["branch"], r["ticker"]
        trading_days_30, _ = _hoard_window_dates(tk, twse_ohlc, all_dates, data_date, CONFIRM_WINDOW)
        buy_days_30  = 0
        total_net_30 = 0
        for d in trading_days_30:
            hit = snap_idx.get(d, {}).get(br, {}).get(tk)
            if hit and hit["net"] > 0:
                buy_days_30  += 1
                total_net_30 += hit["net"]
        r["confirm_30d"] = (
            len(trading_days_30) >= CONFIRM_MIN_DAYS
            and buy_days_30 >= CONFIRM_MIN_DAYS
            and total_net_30 >= CONFIRM_MIN_TOT
        )

    results.sort(key=lambda x: -x["score"])
    return results


HOARD_SCORE_FILE   = DATA_DIR / "hoard_scores.json"
HOARD_HISTORY_DAYS = 5   # 囤貨分數快照滾動保留天數（供「新進榜」比對用）

def load_hoard_scores() -> dict:
    """讀取囤貨分數歷史快照 {yyyymmdd: [{branch,ticker,score}, ...]}"""
    if not HOARD_SCORE_FILE.exists():
        return {}
    try:
        return json.loads(HOARD_SCORE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_hoard_scores(prev_scores: dict, hoarding: list[dict], data_date: str):
    """將今日囤貨分數併入歷史快照，只保留最近 HOARD_HISTORY_DAYS 個交易日。"""
    if not data_date:
        return
    prev_scores[data_date] = [
        {"branch": r["branch"], "ticker": r["ticker"], "score": r["score"]}
        for r in hoarding
    ]
    keys = sorted(prev_scores.keys(), reverse=True)[:HOARD_HISTORY_DAYS]
    trimmed = {k: prev_scores[k] for k in keys}
    DATA_DIR.mkdir(exist_ok=True)
    HOARD_SCORE_FILE.write_text(
        json.dumps(trimmed, ensure_ascii=False, separators=(',', ':')), "utf-8"
    )


def mark_new_hoarding(hoarding: list[dict], prev_scores: dict, data_date: str) -> None:
    """就地標記每筆囤貨配對的 is_new：昨日（prev_scores 中早於今日的最近一個交易日，
    自然跳過假日/非交易日）不存在該配對，或未達 HOARD_SCORE_MIN 門檻 → 視為新進榜。
    prev_scores 應為「寫入今日資料前」讀取的歷史快照；若檔案不存在（首次執行）全部標為非新進榜。"""
    prior_dates = sorted((d for d in prev_scores if d < data_date), reverse=True)
    if not prior_dates:
        for r in hoarding:
            r["is_new"] = False
        return
    yesterday = prior_dates[0]
    prev_map = {(x["branch"], x["ticker"]): x["score"] for x in prev_scores.get(yesterday, [])}
    for r in hoarding:
        prev_score = prev_map.get((r["branch"], r["ticker"]))
        r["is_new"] = prev_score is None or prev_score < HOARD_SCORE_MIN


def build_consensus(all_branches: list[dict]) -> list[dict]:
    agg = defaultdict(lambda: {
        "name": "", "branches": [], "total_buy": 0, "total_net": 0,
        "total_avg_net": 0, "spike_count": 0,
        "max_streak": 0, "max_streak_total": 0,
        "accum_branches": 0,
        "combined_daily": defaultdict(int),   # day_idx → combined net
    })
    for br in all_branches:
        for s in br["stocks"]:
            if s["net"] < MIN_NET_DISPLAY:
                continue
            tk = s["ticker"]
            agg[tk]["name"]           = s["name"]
            agg[tk]["branches"].append(br["名稱"])
            agg[tk]["total_buy"]     += s["buy"]
            agg[tk]["total_net"]     += s["net"]
            agg[tk]["total_avg_net"] += s.get("avg_net", 0)
            if s.get("is_spike"):
                agg[tk]["spike_count"] += 1
            streak = s.get("streak", 0)
            if streak > agg[tk]["max_streak"]:
                agg[tk]["max_streak"]       = streak
                agg[tk]["max_streak_total"] = s.get("streak_total", 0)
            if s.get("is_accumulating"):
                agg[tk]["accum_branches"] += 1
            for i, dn in enumerate(s.get("all_daily", [])):
                agg[tk]["combined_daily"][i] += dn

    result = []
    for tk, v in agg.items():
        if len(v["branches"]) < 2:
            continue
        cross_spike = (v["total_net"] / v["total_avg_net"]
                       if v["total_avg_net"] > 0 else None)
        n_days = max(v["combined_daily"].keys()) + 1 if v["combined_daily"] else 0
        combined = [v["combined_daily"].get(i, 0) for i in range(n_days)]
        result.append({
            "ticker":           tk,
            "name":             v["name"],
            "branch_count":     len(v["branches"]),
            "branches":         v["branches"],
            "total_buy":        v["total_buy"],
            "total_net":        v["total_net"],
            "cross_spike":      cross_spike,
            "is_spike":         cross_spike is not None and cross_spike >= SPIKE_THRESHOLD,
            "spike_count":      v["spike_count"],
            "max_streak":       v["max_streak"],
            "max_streak_total": v["max_streak_total"],
            "accum_branches":   v["accum_branches"],
            "combined_daily":   combined,
        })
    result.sort(key=lambda x: (-x["branch_count"], -x["total_net"]))
    return result

# ════════════════════════════════════════════════════════════
# HTML 渲染
# ════════════════════════════════════════════════════════════

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Noto Sans TC',system-ui,Arial,sans-serif;background:#f0f2f5;color:#1a1a2e;font-size:14px;line-height:1.4}
/* ── Header ── */
header{background:#1a1a2e;color:#fff;padding:.85rem 1.25rem;
  display:flex;justify-content:space-between;align-items:center;
  position:sticky;top:0;z-index:20;box-shadow:0 2px 8px rgba(0,0,0,.3)}
header h1{font-size:1rem;font-weight:600;letter-spacing:.01em}
.meta{font-size:.68rem;opacity:.55;text-align:right;line-height:1.65}
/* ── Layout ── */
.container{max-width:1200px;margin:0 auto;padding:.85rem 1rem 3rem}
/* ── Stats ── */
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:.55rem;margin:.75rem 0 1rem}
.stat{background:#fff;border-radius:10px;padding:.65rem .9rem;
  border:1px solid #e8eaed;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.stat .lbl{font-size:.63rem;color:#999;text-transform:uppercase;letter-spacing:.04em;margin-bottom:.15rem}
.stat .val{font-size:1.4rem;font-weight:700;line-height:1.1}
.stat .sub{font-size:.62rem;color:#bbb;margin-top:.15rem}
/* ── Tabs ── */
.tabs{display:flex;margin:.6rem 0 0;border-bottom:2px solid #e8eaed;
  overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
.tabs::-webkit-scrollbar{display:none}
.tab-btn{flex-shrink:0;background:none;border:none;padding:.5rem 1rem;cursor:pointer;
  font-size:.82rem;color:#888;border-bottom:3px solid transparent;margin-bottom:-2px;
  font-family:inherit;white-space:nowrap;transition:color .1s,border-color .1s}
.tab-btn:hover{color:#1a1a2e;background:#f7f8fa}
.tab-btn.active{color:#2563eb;border-bottom-color:#2563eb;font-weight:600}
.tab-panel{display:none;padding:.75rem 0}
.tab-panel.active{display:block}
/* ── Table wrapper ── */
.tbl-wrap{overflow-x:auto;border-radius:10px;border:1px solid #e8eaed;
  background:#fff;margin-bottom:1.25rem;box-shadow:0 1px 4px rgba(0,0,0,.04)}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{background:#f8f9fb;color:#666;font-weight:600;padding:.5rem .8rem;text-align:left;
   border-bottom:2px solid #e8eaed;white-space:nowrap;font-size:.73rem;letter-spacing:.02em}
th.r,td.r{text-align:right}
td{padding:.5rem .8rem;border-bottom:1px solid #f2f3f5;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#f8faff}
/* ── Ticker cell ── */
.tk{display:flex;flex-direction:column;gap:.05rem}
.tk-code{font-weight:700;font-size:.85rem;color:#1a1a2e;letter-spacing:.01em}
.tk-name{font-size:.7rem;color:#999}
/* ── Branch section ── */
.branch-section{margin-bottom:1.5rem}
.branch-title{font-size:.88rem;font-weight:600;padding:.4rem 0 .4rem;
  border-bottom:2px solid #2563eb;margin-bottom:.6rem;
  display:flex;align-items:center;gap:.35rem;flex-wrap:wrap}
.br-chip{font-size:.64rem;background:#f0f2f5;color:#666;padding:.12rem .45rem;
  border-radius:99px;white-space:nowrap}
/* ── Signal tags (pill style) ── */
.pill{display:inline-flex;align-items:center;padding:.12rem .45rem;
  border-radius:99px;font-size:.66rem;font-weight:600;white-space:nowrap;line-height:1.4}
.pill-blue{background:#eff6ff;color:#1d4ed8}
.pill-amber{background:#fffbeb;color:#92400e}
.pill-red{background:#fef2f2;color:#991b1b}
.pill-orange{background:#fff7ed;color:#c2410c}
.pill-green{background:#f0fdf4;color:#15803d}
.pill-gray{background:#f1f3f4;color:#5f6368}
/* ── Streak badges ── */
.streak-1{color:#bbb;font-size:.72rem}
.streak-2{display:inline-block;background:#eff6ff;color:#2563eb;font-size:.68rem;padding:.12rem .45rem;border-radius:99px;font-weight:600}
.streak-high{display:inline-block;background:#fffbeb;color:#92400e;font-size:.68rem;padding:.12rem .45rem;border-radius:99px;font-weight:700}
.streak-vhigh{display:inline-block;background:#fef2f2;color:#991b1b;font-size:.68rem;padding:.12rem .45rem;border-radius:99px;font-weight:700}
/* ── Dynamo cell ── */
.dynamo-wrap{display:flex;align-items:center;gap:4px;flex-wrap:wrap}
.dynamo-total{font-size:.64rem;color:#777;margin-top:.15rem;white-space:nowrap}
/* ── Count badge (consensus) ── */
.cnt-badge{display:inline-block;padding:.15rem .5rem;border-radius:99px;font-weight:700;font-size:.74rem}
.bc-all{background:#e8f0fe;color:#1558d6}
.bc-most{background:#e6f4ea;color:#137333}
.bc-some{background:#fef7e0;color:#996300}
.bc-few{background:#f1f3f4;color:#5f6368}
/* ── Number colors ── */
.net-pos{color:#15803d;font-weight:600}
.spike-hi{color:#dc2626;font-weight:700}
.spike-lo{color:#94a3b8}
/* ── Market vol % ── */
.vol-nd{color:#bbb;font-size:.76rem}
.vol-lo{color:#64748b;font-size:.76rem}
.vol-md{color:#1d4ed8;font-size:.76rem;font-weight:500}
.vol-hi{color:#b45309;font-size:.76rem;font-weight:700}
.vol-xh{color:#b91c1c;font-size:.76rem;font-weight:700}
/* ── Price cell (收盤+K線) ── */
.price-cell{display:flex;align-items:center;gap:5px;justify-content:flex-end}
.price-info{display:flex;flex-direction:column;align-items:flex-end;gap:1px}
.price-val{font-size:.8rem;font-weight:600;white-space:nowrap;text-align:right}
.price-chg{font-size:.65rem;white-space:nowrap;text-align:right}
.price-up{color:#ef4444}.price-dn{color:#16a34a}.price-flat{color:#94a3b8}
/* ── Buy sub (5日/10日) ── */
.buy-sub{font-size:.64rem;color:#94a3b8;margin-top:.07rem;white-space:nowrap;line-height:1.3}
/* ── Misc ── */
.hint{font-size:.73rem;color:#999;margin-bottom:.6rem;line-height:1.7;
  padding:.4rem .7rem;background:#f8f9fb;border-radius:6px;border-left:3px solid #d1d5db}
/* ── Stats clickable ── */
.stat.clickable{cursor:pointer;transition:box-shadow .15s,transform .1s}
.stat.clickable:hover{box-shadow:0 3px 14px rgba(0,0,0,.11);transform:translateY(-1px)}
/* ── Modal ── */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.46);z-index:100;
  align-items:center;justify-content:center;padding:1rem}
.modal-overlay.open{display:flex}
.modal-box{background:#fff;border-radius:12px;max-width:560px;width:100%;
  max-height:82vh;overflow:hidden;display:flex;flex-direction:column;
  box-shadow:0 20px 60px rgba(0,0,0,.27)}
.modal-hdr{padding:.65rem 1rem;border-bottom:1px solid #e8eaed;flex-shrink:0;
  display:flex;justify-content:space-between;align-items:center;font-weight:600;font-size:.88rem}
.modal-hdr button{background:none;border:none;cursor:pointer;font-size:1.05rem;
  color:#999;padding:.15rem .4rem;line-height:1;border-radius:4px}
.modal-hdr button:hover{background:#f0f2f5;color:#333}
.modal-body{padding:.55rem .75rem;overflow-y:auto}
.modal-list{display:flex;flex-direction:column;gap:.28rem}
.modal-item{display:flex;align-items:center;gap:.35rem;padding:.38rem .55rem;
  border-radius:7px;background:#f8f9fb;flex-wrap:wrap}
.modal-item .tk-code{font-weight:700;font-size:.83rem;min-width:36px}
.modal-item .tk-name{font-size:.72rem;color:#888;flex:1;min-width:60px}
.modal-right{margin-left:auto;font-size:.76rem;font-weight:600;color:#1a1a2e;
  white-space:nowrap;text-align:right}
.modal-sub{font-size:.64rem;color:#999;margin-top:.08rem}
/* ── Mobile ── */
@media(max-width:640px){
  header{flex-direction:column;gap:.3rem;align-items:flex-start;padding:.7rem 1rem}
  .meta{text-align:left}
  .container{padding:.6rem .75rem 2rem}
  .stats{grid-template-columns:repeat(3,1fr);gap:.4rem}
  .stat{padding:.5rem .6rem}
  .stat .val{font-size:1.15rem}
  .stat .sub{display:none}
  .tabs{margin:.4rem 0 0}
  .tab-btn{padding:.45rem .75rem;font-size:.78rem}
  th,td{padding:.4rem .55rem;font-size:.76rem}
  .tk-name{display:none}
  .col-hide{display:none}
}
@media(max-width:380px){
  .stats{grid-template-columns:1fr 1fr}
}
"""

JS = """
function showTab(id){
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  document.querySelector('[data-tab="'+id+'"]').classList.add('active');
}
function openSignal(key,title){
  document.getElementById('modal-title').textContent=title;
  document.getElementById('modal-body').innerHTML=document.getElementById('_md-'+key).innerHTML;
  document.getElementById('signal-modal').classList.add('open');
}
function closeModal(){document.getElementById('signal-modal').classList.remove('open');}
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeModal();});
function sortTable(th){
  var tbl=th.closest('table'), col=Array.from(th.parentElement.children).indexOf(th);
  var isNum=th.dataset.num==='1', asc=th.dataset.asc==='1';
  var rows=Array.from(tbl.querySelectorAll('tbody tr'));
  rows.sort(function(a,b){
    var av=a.cells[col]?.dataset.v??a.cells[col]?.textContent.trim()??'';
    var bv=b.cells[col]?.dataset.v??b.cells[col]?.textContent.trim()??'';
    return isNum?(asc?parseFloat(av)-parseFloat(bv):parseFloat(bv)-parseFloat(av))
               :(asc?av.localeCompare(bv,'zh-TW'):bv.localeCompare(av,'zh-TW'));
  });
  th.dataset.asc=asc?'0':'1';
  rows.forEach(r=>tbl.querySelector('tbody').appendChild(r));
}
"""

def fmt_n(n, d=0):
    if n == 0: return "–"
    try:
        return f"{n:,.{d}f}" if d else f"{int(n):,}"
    except: return str(n)

def _streak_html(streak: int) -> str:
    if streak <= 0: return '<span class="streak-1">–</span>'
    if streak == 1: return '<span class="streak-1">今</span>'
    if streak <= 3: return f'<span class="streak-2">連{streak}日</span>'
    if streak <= 5: return f'<span class="streak-high">連{streak}日</span>'
    return f'<span class="streak-vhigh">連{streak}日</span>'

def _vol_html(buy_k: int, market_k: int, title: str = "分點買進佔市場成交量") -> str:
    if market_k <= 0: return '<span class="vol-nd">–</span>'
    pct = buy_k / market_k * 100
    cls = "vol-nd" if pct < 0.1 else "vol-lo" if pct < 0.5 else "vol-md" if pct < 2 else "vol-hi" if pct < 5 else "vol-xh"
    return f'<span class="{cls}" title="{title}">{pct:.2f}%</span>'

def _spike_html(spike) -> str:
    if spike is None: return '<span class="spike-lo">–</span>'
    if spike >= SPIKE_THRESHOLD:
        return f'<span class="spike-hi">{spike:.1f}x</span> <span class="pill pill-red">爆量</span>'
    return f'<span class="spike-lo">{spike:.1f}x</span>'

def _dynamo_html(s: dict) -> str:
    streak   = s.get("streak", 0)
    is_accum = s.get("is_accumulating", False)
    buy_days = s.get("buy_days", 0)
    win_days = s.get("window_days", 0)
    parts    = []

    if streak >= 2:
        total = s.get("streak_total", 0)
        daily = s.get("streak_daily", [])
        parts.append(
            f'<div class="dynamo-wrap">{_streak_html(streak)}{make_sparkline(daily)}</div>'
            f'<div class="dynamo-total">累計 +{fmt_n(total)} 千</div>'
        )

    if is_accum and streak < 2:
        total = s.get("window_buy_tot", 0)
        daily = s.get("all_daily", [])
        badge = f'<span class="pill pill-orange">積累 {buy_days}/{win_days}日</span>'
        parts.append(
            f'<div class="dynamo-wrap">{badge}{make_sparkline(daily)}</div>'
            f'<div class="dynamo-total">窗口 +{fmt_n(total)} 千</div>'
        )

    return "".join(parts) if parts else _streak_html(streak)

def _consensus_dynamo_html(s: dict) -> str:
    streak       = s.get("max_streak", 0)
    streak_total = s.get("max_streak_total", 0)
    accum_br     = s.get("accum_branches", 0)
    combined     = s.get("combined_daily", [])
    parts        = []

    if streak >= 2:
        parts.append(
            f'<div class="dynamo-wrap">{_streak_html(streak)}{make_sparkline(combined, w=70)}</div>'
            f'<div class="dynamo-total">累計 +{fmt_n(streak_total)} 千</div>'
        )
    elif streak == 1:
        parts.append(_streak_html(1))

    if accum_br > 0:
        tag = f'<span class="pill pill-orange">積累 ×{accum_br}</span>'
        if not parts:
            parts.append(f'<div class="dynamo-wrap">{tag}{make_sparkline(combined, w=70)}</div>')
        else:
            parts.append(f' {tag}')

    return "".join(parts) if parts else _streak_html(0)

def _fini_html(info: dict | None) -> str:
    if not info:
        return '<span class="fini-nd">–</span>'
    fn, tn = info.get("fini_net", 0), info.get("trust_net", 0)
    parts = []
    if fn >= FINI_MIN_LOTS:
        parts.append(f'<span class="pill pill-green" title="外資買超 +{fn:,}張">外資↑</span>')
    elif fn <= -FINI_MIN_LOTS:
        parts.append(f'<span class="pill pill-red" title="外資賣超 {fn:,}張">外資↓</span>')
    if tn >= FINI_MIN_LOTS:
        parts.append(f'<span class="pill pill-blue" title="投信買超 +{tn:,}張">投信↑</span>')
    elif tn <= -FINI_MIN_LOTS:
        parts.append(f'<span class="pill pill-amber" title="投信賣超 {tn:,}張">投信↓</span>')
    if not parts:
        return '<span class="fini-nd">–</span>'
    # data-v：外資張數，用於排序
    fini_dv = fn
    return f'<span data-v="{fini_dv}">{"&nbsp;".join(parts)}</span>'

def _bc_class(n, total):
    return "bc-all" if n == total else "bc-most" if n >= total*0.7 else "bc-some" if n >= 3 else "bc-few"

def render_consensus_table(consensus, total_branches, twse_vol):
    rows = ""
    for s in consensus:
        br_html = " ".join(f'<span class="br-chip">{b}</span>' for b in s["branches"])
        ticker  = s["ticker"]
        mkt     = twse_vol.get(ticker, 0)
        bc_cls  = _bc_class(s["branch_count"], total_branches)
        cs      = s["cross_spike"]
        spike_s = f"{cs:.2f}" if cs else "0"
        vol_dv  = f"{s['total_buy']/mkt*100:.3f}" if mkt else "0"
        rows += f"""<tr>
          <td><div class="tk"><span class="tk-code">{ticker}</span><span class="tk-name">{s['name']}</span></div></td>
          <td class="r" data-v="{s['branch_count']}"><span class="cnt-badge {bc_cls}">{s['branch_count']}/{total_branches}</span></td>
          <td class="r net-pos" data-v="{s['total_net']}">{fmt_n(s['total_net'])}</td>
          <td class="r" data-v="{spike_s}" class="col-hide">{_spike_html(cs)}</td>
          <td data-v="{s['max_streak']}">{_consensus_dynamo_html(s)}</td>
          <td class="r col-hide" data-v="{vol_dv}">{_vol_html(s['total_buy'], mkt)}</td>
          <td class="col-hide">{br_html}</td>
        </tr>"""
    return f"""<div class="tbl-wrap"><table>
      <thead><tr>
        <th onclick="sortTable(this)">代號 / 名稱 ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">分點 ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">合計買超 ↕</th>
        <th class="r col-hide" data-num="1" onclick="sortTable(this)">跨點倍率 ↕</th>
        <th data-num="1" onclick="sortTable(this)">籌碼動能 ↕</th>
        <th class="r col-hide" data-num="1" onclick="sortTable(this)">佔市場量 ↕</th>
        <th class="col-hide">買進分點</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table></div>"""

def _period_vol_pct_html(buy_gross: int, dates: list[str], ticker: str,
                         twse_monthly: dict, label: str) -> str:
    mkt_sum = sum(twse_monthly.get(ticker, {}).get(d, 0) for d in dates)
    title   = f"{label}買進 {fmt_n(buy_gross)} ÷ 期間市場 {fmt_n(mkt_sum)} 千元"
    return _vol_html(buy_gross, mkt_sum, title + "（📅期間）")

def _hoard_score_pill(score: float, dims: dict) -> str:
    cls   = "pill-red" if score >= 80 else "pill-orange"
    label = "強 " if score >= 80 else ""
    title = (f"持續性{dims['persistence']} 逆勢買{dims['dip_buying']} "
             f"隱蔽性{dims['stealth']} 吃貨力度{dims['absorption']} 成本優勢{dims['cost_edge']}")
    return f'<span class="pill {cls}" title="{title}">{label}{score:.0f}</span>'

def render_hoarding_table(hoarding: list[dict], data_date: str, twse_ohlc: dict) -> str:
    if not hoarding:
        return '<p class="hint">目前沒有分點在觀察窗口內達到囤貨分數門檻。</p>'
    rows = ""
    for i, r in enumerate(hoarding, 1):
        tk      = r["ticker"]
        co_pill = f' <span class="pill pill-blue">{r["co_hoard_count"]}點共囤</span>' if r["co_hoard_count"] >= 2 else ""
        confirm_pill = ' <span class="pill pill-green" title="30日窗口內買超天數/總量同樣達標">長線佈局</span>' if r.get("confirm_30d") else ""
        ohlc5   = _get_recent_ohlc(tk, data_date, twse_ohlc) if not r["no_price"] else []
        if ohlc5:
            close   = r["close"]
            dev     = r["cost_dev_pct"]
            dev_cls = "price-up" if dev > 0 else "price-dn" if dev < 0 else "price-flat"
            price_html = (f'<div class="price-cell"><div class="price-info">'
                          f'<span class="price-val {dev_cls}">{close:,.1f}</span>'
                          f'<span class="price-chg {dev_cls}">({dev:+.1f}%)</span>'
                          f'</div>{make_candle_svg(ohlc5)}</div>')
        else:
            price_html = '<span class="price-flat">–</span>'
        est_cost_str = f"{r['est_cost']:.2f}" if r["est_cost"] else "–"
        rows += f"""<tr>
          <td class="r" style="color:#bbb;font-size:.72rem;width:28px">{i}</td>
          <td><div class="tk"><span class="tk-code">{tk}</span><span class="tk-name">{r['name']}</span></div>{co_pill}{confirm_pill}</td>
          <td>{r['branch']}</td>
          <td class="r" data-v="{r['score']}">{_hoard_score_pill(r['score'], r['dims'])}</td>
          <td class="r" data-v="{r['buy_days']}">{r['buy_days']}/{r['window_days']}日</td>
          <td class="r" data-v="{r['est_shares']}">{fmt_n(r['est_shares'])}</td>
          <td class="r" data-v="{r['est_cost']}">{est_cost_str}</td>
          <td data-v="{r['close']}" style="text-align:right">{price_html}</td>
        </tr>"""
    return f"""<div class="tbl-wrap"><table>
      <thead><tr>
        <th style="width:28px">#</th>
        <th onclick="sortTable(this)">代號 / 名稱 ↕</th>
        <th onclick="sortTable(this)">分點 ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">囤貨分數 ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">買超天數 ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">估計吃貨(張) ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">估計成本 ↕</th>
        <th data-num="1" onclick="sortTable(this)">收盤/K線 ↕</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table></div>"""

def render_branch_section(br: dict, twse_vol: dict,
                          twse_monthly: dict | None = None,
                          twse_ohlc: dict | None = None,
                          data_date: str = "") -> str:
    stocks = [s for s in br["stocks"]
              if s["net"] >= MIN_NET_DISPLAY or s.get("streak", 0) >= 5]
    if not stocks:
        return f'<div class="branch-section"><div class="branch-title">{br["名稱"]} <span class="br-chip">無資料</span></div></div>'

    rows = ""
    for i, s in enumerate(stocks, 1):
        tk       = s["ticker"]
        mkt      = twse_vol.get(tk, 0)
        streak   = s.get("streak", 0)
        is_accum = s.get("is_accumulating", False)
        spike_dv = f"{s['spike']:.2f}" if s.get("spike") else "0"
        pure_tag  = ' <span class="pill pill-green">純買</span>' if s["sell"] == 0 else ""
        low_tag   = ' <span class="pill pill-gray">今日低量</span>' if s["net"] < MIN_NET_DISPLAY else ""

        fiveday = s.get("fiveday_net", 0)
        d10     = s.get("d10_net", 0)

        # 收盤價 + 5日K線
        _upto = data_date or br.get("data_date", "")
        _ohlc5 = _get_recent_ohlc(tk, _upto, twse_ohlc) if twse_ohlc else []
        if _ohlc5:
            close  = _ohlc5[-1]["c"]
            prev_c = _ohlc5[-2]["c"] if len(_ohlc5) >= 2 else close
            chg_pct = (close - prev_c) / prev_c * 100 if prev_c else 0
            pcls   = "price-dn" if close > prev_c else "price-up" if close < prev_c else "price-flat"
            chg_str = f'{chg_pct:+.2f}%'
            price_dv   = f"{close:.2f}"
            price_html = (f'<div class="price-cell">'
                          f'<div class="price-info">'
                          f'<span class="price-val {pcls}">{close:,.1f}</span>'
                          f'<span class="price-chg {pcls}">{chg_str}</span>'
                          f'</div>'
                          f'{make_candle_svg(_ohlc5)}</div>')
        else:
            price_dv   = "0"
            price_html = '<span class="price-flat">–</span>'

        # 佔市場量：連買或積累中用期間合計
        if twse_monthly and (is_accum or streak >= 2):
            if is_accum:
                _buy_g = s.get("window_buy_gross", 0)
                _dates = s.get("window_dates", [])
                _label = f"積累{len(_dates)}日"
            else:
                _buy_g = s.get("streak_buy_gross", 0)
                _dates = s.get("streak_dates", [])
                _label = f"連買{streak}日"
            vol_html = _period_vol_pct_html(_buy_g, _dates, tk, twse_monthly, _label)
            _mkt_sum = sum(twse_monthly.get(tk, {}).get(d, 0) for d in _dates)
            vol_dv   = f"{_buy_g / _mkt_sum * 100:.3f}" if _mkt_sum > 0 else "0"
        else:
            vol_html = _vol_html(s["buy"], mkt)
            vol_dv   = f"{s['buy']/mkt*100:.3f}" if mkt else "0"

        rows += f"""<tr>
          <td class="r" style="color:#bbb;font-size:.72rem;width:28px">{i}</td>
          <td><div class="tk"><span class="tk-code">{tk}</span><span class="tk-name">{s['name']}</span></div></td>
          <td class="r net-pos" data-v="{s['net']}">{fmt_n(s['net'])}{pure_tag}{low_tag}</td>
          <td class="r" data-v="{fiveday}">{fmt_n(fiveday) if fiveday else '–'}</td>
          <td class="r col-hide" data-v="{d10}">{fmt_n(d10) if d10 else '–'}</td>
          <td class="r" data-v="{spike_dv}">{_spike_html(s.get('spike'))}</td>
          <td data-v="{streak}">{_dynamo_html(s)}</td>
          <td data-v="{price_dv}" style="text-align:right">{price_html}</td>
          <td class="r col-hide" data-v="{vol_dv}">{vol_html}</td>
        </tr>"""

    spk_cnt    = sum(1 for s in stocks if s.get("is_spike"))
    streak_max = max((s.get("streak", 0) for s in stocks), default=0)
    accum_cnt  = sum(1 for s in stocks if s.get("is_accumulating"))
    badges = []
    if spk_cnt:         badges.append(f'<span class="br-chip">🔥 {spk_cnt} 爆量</span>')
    if streak_max >= 3: badges.append(f'<span class="br-chip">連買 {streak_max} 日</span>')
    if accum_cnt:       badges.append(f'<span class="br-chip">積累 {accum_cnt} 檔</span>')
    return f"""<div class="branch-section">
      <div class="branch-title">{br['名稱']} {''.join(badges)}</div>
      <div class="tbl-wrap"><table>
        <thead><tr>
          <th style="width:28px">#</th>
          <th onclick="sortTable(this)">代號 / 名稱 ↕</th>
          <th class="r" data-num="1" onclick="sortTable(this)">今日買超(千) ↕</th>
          <th class="r" data-num="1" onclick="sortTable(this)">近5日(千) ↕</th>
          <th class="r col-hide" data-num="1" onclick="sortTable(this)">近10日(千) ↕</th>
          <th class="r" data-num="1" onclick="sortTable(this)">今/均倍率 ↕</th>
          <th data-num="1" onclick="sortTable(this)">籌碼動能 ↕</th>
          <th data-num="1" onclick="sortTable(this)">收盤/K線 ↕</th>
          <th class="r col-hide" data-num="1" onclick="sortTable(this)">佔市場量 ↕</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table></div>
    </div>"""

def _build_modal_divs(all_branches: list[dict]) -> str:
    """預建各 signal modal 內容（隱藏 div），由 openSignal() 複製到 modal-body"""

    def _vis(s):
        """只取今日在 tab 中可見（net ≥ MIN_NET_DISPLAY）的股票"""
        return s["net"] >= MIN_NET_DISPLAY

    # ── 爆量個股 ──
    spk_entries = sorted(
        [(b["名稱"], s["ticker"], s["name"], s.get("spike") or 0, s["net"])
         for b in all_branches for s in b["stocks"] if s.get("is_spike") and _vis(s)],
        key=lambda x: (-x[3], -x[4])
    )
    spk_items = "".join(
        f'<div class="modal-item">'
        f'<span class="tk-code">{tk}</span>'
        f'<span class="tk-name">{nm}</span>'
        f'<span class="br-chip">{br}</span>'
        f'<div class="modal-right">{sp:.1f}x<br><span class="modal-sub">+{fmt_n(net)}千</span></div>'
        f'</div>'
        for br, tk, nm, sp, net in spk_entries
    )

    # ── 連買≥5日（不限門檻，低量者另加標示）──
    s5_entries = sorted(
        [(b["名稱"], s["ticker"], s["name"], s.get("streak", 0), s.get("streak_total", 0), _vis(s))
         for b in all_branches for s in b["stocks"] if s.get("streak", 0) >= 5],
        key=lambda x: (-x[3], -x[4])
    )
    _low_pill = '<span class="pill pill-gray">今日低量</span>'
    s5_items = "".join(
        f'<div class="modal-item">'
        f'<span class="tk-code">{tk}</span>'
        f'<span class="tk-name">{nm}</span>'
        f'<span class="br-chip">{br}</span>'
        f'{"" if vis else _low_pill}'
        f'<div class="modal-right">{_streak_html(d)}<br><span class="modal-sub">累計 +{fmt_n(tot)}千</span></div>'
        f'</div>'
        for br, tk, nm, d, tot, vis in s5_entries
    )

    # ── 積累中（依密度排序：buy_days/window_days 高→低，再按 window_buy_tot）──
    ac_entries = sorted(
        [(b["名稱"], s["ticker"], s["name"],
          s.get("buy_days", 0), s.get("window_days", 1), s.get("window_buy_tot", 0),
          s.get("is_strong_accum", False))
         for b in all_branches for s in b["stocks"] if s.get("is_accumulating") and _vis(s)],
        key=lambda x: (-x[3] / x[4], -x[5])
    )
    ac_items = ""
    for br, tk, nm, bd, wd, tot, strong in ac_entries:
        tier = '<span class="pill pill-amber">強積累</span> ' if strong else ''
        ac_items += (
            f'<div class="modal-item">'
            f'<span class="tk-code">{tk}</span>'
            f'<span class="tk-name">{nm}</span>'
            f'<span class="br-chip">{br}</span>'
            f'{tier}<span class="pill pill-orange">{bd}/{wd}日</span>'
            f'<div class="modal-right">+{fmt_n(tot)}千</div>'
            f'</div>'
        )

    return (
        f'<div id="_md-spikes" style="display:none"><div class="modal-list">{spk_items}</div></div>'
        f'<div id="_md-streak5" style="display:none"><div class="modal-list">{s5_items}</div></div>'
        f'<div id="_md-accum" style="display:none"><div class="modal-list">{ac_items}</div></div>'
    )


def render_html(all_branches: list[dict], consensus: list[dict],
                twse_vol: dict, twse_monthly: dict | None = None,
                twse_ohlc: dict | None = None, hoarding: list[dict] | None = None) -> str:
    data_date  = next((b["data_date"] for b in all_branches if b.get("data_date")), "")
    date_disp  = f"{data_date[:4]}/{data_date[4:6]}/{data_date[6:]}" if len(data_date)==8 else data_date
    now_utc    = datetime.utcnow().strftime("%Y/%m/%d %H:%M UTC")
    total_br   = len(all_branches)
    spike_list  = [s for b in all_branches for s in b["stocks"] if s.get("is_spike")]
    streak_5p   = [s for b in all_branches for s in b["stocks"] if s.get("streak", 0) >= 5]
    accum_list  = [s for b in all_branches for s in b["stocks"] if s.get("is_accumulating")]
    accum_strong = sum(1 for s in accum_list if s.get("is_strong_accum"))
    total_buy_m = sum(s["buy"] for b in all_branches for s in b["stocks"]) // 1_000
    hoarding    = hoarding or []

    modal_divs = _build_modal_divs(all_branches)

    stats = f"""<div class="stats">
      <div class="stat"><div class="lbl">資料日期</div><div class="val" style="font-size:1rem">{date_disp}</div></div>
      <div class="stat clickable" onclick="showTab('consensus')" title="切換至共識買進分頁">
        <div class="lbl">共識買進</div><div class="val">{len(consensus)}</div><div class="sub">≥2分點同買</div></div>
      <div class="stat clickable" onclick="showTab('hoarding')" title="切換至囤貨追蹤分頁">
        <div class="lbl">囤貨追蹤</div><div class="val" style="color:#b91c1c">{len(hoarding)}</div><div class="sub">14日窗口 ↗</div></div>
      <div class="stat clickable" onclick="openSignal('spikes','爆量個股（{len(spike_list)}檔）')" title="點選查看名單">
        <div class="lbl">爆量個股</div><div class="val" style="color:#d93025">{len(spike_list)}</div><div class="sub">≥{int(SPIKE_THRESHOLD*100)}% 均量 ↗</div></div>
      <div class="stat clickable" onclick="openSignal('streak5','連買≥5日（{len(streak_5p)}檔）')" title="點選查看名單">
        <div class="lbl">連買≥5日</div><div class="val" style="color:#92400e">{len(streak_5p)}</div><div class="sub">強力信號 ↗</div></div>
      <div class="stat clickable" onclick="openSignal('accum','積累中（{len(accum_list)} 檔，強積累 {accum_strong}）')" title="點選查看名單，依頻率排序">
        <div class="lbl">積累中</div><div class="val" style="color:#c2410c">{len(accum_list)}</div>
        <div class="sub">強積累 {accum_strong} 檔 ↗</div></div>
    </div>"""

    tab_nav = ""
    panels  = ""
    for b in all_branches:
        tid = f"br-{b['名稱']}"
        tab_nav += f'<button class="tab-btn" data-tab="{tid}" onclick="showTab(\'{tid}\')">{b["名稱"]}</button>'
        panels  += f'<div id="{tid}" class="tab-panel">{render_branch_section(b, twse_vol, twse_monthly, twse_ohlc, data_date)}</div>'

    vol_note = f"（TWSE 成交量已載入 {len(twse_vol)} 檔，佔市場量 % 供參考，上櫃個股可能無資料）" if twse_vol else "（TWSE 成交量載入失敗，佔市場量 % 不顯示）"

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>券商分點大額買進監控 — {date_disp}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>📊 券商分點大額買進監控</h1>
  <div class="meta">資料日期：{date_disp}<br>更新：{now_utc}<br>{vol_note}</div>
</header>
<div class="container">
  {stats}
  <div class="tabs">
    <button class="tab-btn active" data-tab="consensus" onclick="showTab('consensus')">🔗 共識買進</button>
    <button class="tab-btn" data-tab="hoarding" onclick="showTab('hoarding')">🎯 囤貨追蹤</button>
    {tab_nav}
  </div>
  <div id="consensus" class="tab-panel active">
    <p class="hint">同時出現在 ≥2 個分點買超（≥{MIN_NET_DISPLAY:,}千元）的股票，按分點數排序。<br>
    佔市場量 = 各分點買進合計 ÷ TWSE當日成交金額。連買天數含今日連續正買超天數。</p>
    {render_consensus_table(consensus, total_br, twse_vol)}
  </div>
  <div id="hoarding" class="tab-panel">
    <p class="hint">囤貨分數（0~100）綜合 14 個交易日內的持續性、逆勢買、隱蔽性、吃貨力度、成本優勢五個維度，
    偵測分點可能悄悄吸籌的股票；僅 ≥{HOARD_SCORE_MIN} 分進榜。「長線佈局」標記表示 30 日窗口內買超天數/總量同樣達標，屬於信心加分，不影響主分數。<br>
    「估計吃貨/成本」為依買超榜金額反推的上限估計值（快照不含賣出資料），僅供參考，非真實持股。</p>
    {render_hoarding_table(hoarding, data_date, twse_ohlc or {})}
  </div>
  {panels}
</div>
<!-- Modal overlay -->
<div id="signal-modal" class="modal-overlay" onclick="closeModal()">
  <div class="modal-box" onclick="event.stopPropagation()">
    <div class="modal-hdr"><span id="modal-title"></span><button onclick="closeModal()">✕</button></div>
    <div id="modal-body" class="modal-body"></div>
  </div>
</div>
<!-- Modal data (hidden) -->
{modal_divs}
<script>{JS}</script>
</body>
</html>"""

# ════════════════════════════════════════════════════════════
# 電子郵件
# ════════════════════════════════════════════════════════════

def render_email_digest(all_branches: list[dict], consensus: list[dict],
                        twse_ohlc: dict, data_date: str,
                        hoarding: list[dict] | None = None) -> str:
    """生成簡潔日報 HTML（email 專用，inline style）"""
    date_disp = f"{data_date[:4]}/{data_date[4:6]}/{data_date[6:]}" if len(data_date) == 8 else data_date
    hoarding = hoarding or []

    # ── 各信號清單 ──
    strong_tickers = {s["ticker"] for b in all_branches for s in b["stocks"]
                      if s.get("is_strong_accum")}

    streak5 = sorted(
        [(b["名稱"], s) for b in all_branches for s in b["stocks"]
         if s.get("streak", 0) >= 5 and s["net"] >= MIN_NET_DISPLAY],
        key=lambda x: (-x[1]["streak"], -x[1].get("streak_total", 0))
    )
    # 每 ticker 取最強分點
    streak5_map: dict = {}
    for br, s in streak5:
        tk = s["ticker"]
        if tk not in streak5_map or s["streak"] > streak5_map[tk][1]["streak"]:
            streak5_map[tk] = (br, s)
    streak5_list = sorted(streak5_map.values(), key=lambda x: (-x[1]["streak"], -x[1].get("streak_total", 0)))

    strong_list = sorted(
        [(b["名稱"], s) for b in all_branches for s in b["stocks"]
         if s.get("is_strong_accum") and s["net"] >= MIN_NET_DISPLAY],
        key=lambda x: (-x[1].get("buy_days", 0), -x[1].get("window_buy_tot", 0))
    )
    strong_map: dict = {}
    for br, s in strong_list:
        tk = s["ticker"]
        if tk not in strong_map:
            strong_map[tk] = (br, s)
    strong_uniq = sorted(strong_map.values(), key=lambda x: (-x[1].get("buy_days", 0), -x[1].get("window_buy_tot", 0)))

    spike_list = sorted(
        [(b["名稱"], s) for b in all_branches for s in b["stocks"]
         if s.get("is_spike") and s["net"] >= MIN_NET_DISPLAY],
        key=lambda x: -(x[1].get("spike") or 0)
    )
    spike_map: dict = {}
    for br, s in spike_list:
        tk = s["ticker"]
        if tk not in spike_map or (s.get("spike") or 0) > (spike_map[tk][1].get("spike") or 0):
            spike_map[tk] = (br, s)
    spike_uniq = sorted(spike_map.values(), key=lambda x: -(x[1].get("spike") or 0))

    consensus_tickers = {c["ticker"] for c in consensus}
    n_streak5  = len(streak5_map)
    n_strong   = len(strong_map)
    n_spike    = len(spike_map)
    n_cons     = len(consensus)
    n_hoard    = len(hoarding)

    # ── Style helpers ──
    TH  = 'style="background:#f8fafc;padding:6px 10px;font-size:.72rem;color:#64748b;text-align:left;border-bottom:2px solid #e2e8f0;white-space:nowrap"'
    THR = 'style="background:#f8fafc;padding:6px 10px;font-size:.72rem;color:#64748b;text-align:right;border-bottom:2px solid #e2e8f0;white-space:nowrap"'
    TD  = 'style="padding:7px 10px;font-size:.78rem;border-bottom:1px solid #f1f5f9;vertical-align:middle"'
    TDR = 'style="padding:7px 10px;font-size:.78rem;border-bottom:1px solid #f1f5f9;text-align:right;vertical-align:middle;white-space:nowrap"'
    TBL = 'style="width:100%;border-collapse:collapse;margin-bottom:4px"'
    H3S = 'style="font-size:.82rem;font-weight:700;color:#1e293b;margin:20px 0 8px 0;padding:4px 8px;background:#f1f5f9;border-left:3px solid #3b82f6;border-radius:2px"'

    def pill_s(text, bg, fg):
        return f'<span style="background:{bg};color:{fg};padding:1px 5px;border-radius:8px;font-size:.68rem;font-weight:600;margin-left:3px">{text}</span>'

    def price_html(ticker):
        ohlc5 = _get_recent_ohlc(ticker, data_date, twse_ohlc, 2)
        if not ohlc5:
            return "–"
        c  = ohlc5[-1]["c"]
        p  = ohlc5[-2]["c"] if len(ohlc5) >= 2 else c
        pct = (c - p) / p * 100 if p else 0
        col = "#dc2626" if pct > 0 else "#16a34a" if pct < 0 else "#64748b"
        sign = "+" if pct > 0 else ""
        return f'<span style="color:{col};font-weight:600">{c:,.0f}</span> <span style="color:{col};font-size:.7rem">({sign}{pct:.1f}%)</span>'

    def tag_cons(tk):
        return pill_s("共識", "#dbeafe", "#1d4ed8") if tk in consensus_tickers else ""

    def tag_strong(tk):
        return pill_s("強積累", "#fef3c7", "#92400e") if tk in strong_tickers else ""

    def streak_badge(n):
        bg, fg = ("#fef3c7", "#92400e") if n >= 7 else ("#ffedd5", "#c2410c") if n >= 5 else ("#f1f5f9", "#475569")
        return pill_s(f"連{n}日", bg, fg)

    # ── 共識買進 ──
    rows_c = ""
    for c in consensus[:15]:
        tk    = c["ticker"]
        brs   = "、".join(c["branches"][:3]) + ("…" if len(c["branches"]) > 3 else "")
        extra = ""
        if c["max_streak"] >= 5: extra += streak_badge(c["max_streak"])
        if tk in strong_tickers:  extra += tag_strong(tk)
        if c.get("is_spike"):     extra += pill_s("爆量", "#fee2e2", "#dc2626")
        rows_c += (
            f'<tr><td {TD}><b>{tk}</b> <span style="color:#64748b">{c["name"]}</span>{extra}'
            f'<div style="font-size:.68rem;color:#94a3b8;margin-top:2px">{brs}</div></td>'
            f'<td {TDR}>{pill_s(str(c["branch_count"])+" 點", "#dbeafe", "#1d4ed8")}</td>'
            f'<td {TDR}>{fmt_n(c["total_net"])}千</td>'
            f'<td {TDR}>{price_html(tk)}</td></tr>'
        )
    cons_html = f"""
        <p {H3S}>🔗 共識買進（≥2 分點同買）</p>
        <table {TBL}><thead><tr>
          <th {TH}>代號 / 名稱</th><th {THR}>分點數</th>
          <th {THR}>合計買超</th><th {THR}>收盤價</th>
        </tr></thead><tbody>{rows_c}</tbody></table>""" if rows_c else ""

    # ── 囤貨警示（is_new 優先，再按分數降冪）──
    hoard_sorted = sorted(hoarding, key=lambda x: (not x.get("is_new", False), -x["score"]))
    rows_h = ""
    for r in hoard_sorted[:10]:
        tk    = r["ticker"]
        extra = ""
        if r.get("is_new"):
            extra += pill_s("NEW", "#dc2626", "#ffffff")
        if r.get("co_hoard_count", 0) >= 2:
            extra += pill_s(f'{r["co_hoard_count"]}點共囤', "#dbeafe", "#1d4ed8")
        if r.get("confirm_30d"):
            extra += pill_s("長線佈局", "#f0fdf4", "#15803d")
        dev      = r.get("cost_dev_pct", 0)
        dev_col  = "#dc2626" if dev > 0 else "#16a34a" if dev < 0 else "#64748b"
        cost_html = f'<span style="color:{dev_col};font-weight:600">{dev:+.1f}%</span>' if r.get("est_cost") else "–"
        score_bg = "#fef2f2" if r["score"] >= 80 else "#fff7ed"
        score_fg = "#991b1b" if r["score"] >= 80 else "#c2410c"
        score_pill = pill_s(f'{r["score"]:.0f}', score_bg, score_fg)
        rows_h += (
            f'<tr><td {TD}><b>{tk}</b> <span style="color:#64748b">{r["name"]}</span>{extra}</td>'
            f'<td {TDR}><span style="font-size:.7rem;color:#94a3b8">{r["branch"]}</span></td>'
            f'<td {TDR}>{score_pill}</td>'
            f'<td {TDR}>{cost_html}</td></tr>'
        )
    hoard_html = f"""
        <p {H3S}>🎯 囤貨警示</p>
        <table {TBL}><thead><tr>
          <th {TH}>代號 / 名稱</th><th {TH}>分點</th>
          <th {THR}>分數</th><th {THR}>成本乖離</th>
        </tr></thead><tbody>{rows_h}</tbody></table>""" if rows_h else ""

    # ── 連買≥5日 ──
    rows_s = ""
    for br, s in streak5_list[:12]:
        tk = s["ticker"]
        rows_s += (
            f'<tr><td {TD}><b>{tk}</b> <span style="color:#64748b">{s["name"]}</span>'
            f'{tag_cons(tk)}{tag_strong(tk)}</td>'
            f'<td {TDR}><span style="font-size:.7rem;color:#94a3b8">{br}</span></td>'
            f'<td {TDR}>{streak_badge(s["streak"])}</td>'
            f'<td {TDR}>{fmt_n(s.get("streak_total",0))}千</td>'
            f'<td {TDR}>{price_html(tk)}</td></tr>'
        )
    streak_html = f"""
        <p {H3S}>📈 連買≥5日強勢股</p>
        <table {TBL}><thead><tr>
          <th {TH}>代號 / 名稱</th><th {TH}>分點</th>
          <th {THR}>天數</th><th {THR}>累計買超</th><th {THR}>收盤價</th>
        </tr></thead><tbody>{rows_s}</tbody></table>""" if rows_s else ""

    # ── 強積累 ──
    rows_a = ""
    for br, s in strong_uniq[:12]:
        tk  = s["ticker"]
        bd  = s.get("buy_days", 0)
        wd  = s.get("window_days", 1)
        tot = s.get("window_buy_tot", 0)
        rows_a += (
            f'<tr><td {TD}><b>{tk}</b> <span style="color:#64748b">{s["name"]}</span>'
            f'{tag_cons(tk)}</td>'
            f'<td {TDR}><span style="font-size:.7rem;color:#94a3b8">{br}</span></td>'
            f'<td {TDR}>{pill_s(f"{bd}/{wd}日", "#fef3c7", "#92400e")}</td>'
            f'<td {TDR}>{fmt_n(tot)}千</td>'
            f'<td {TDR}>{price_html(tk)}</td></tr>'
        )
    strong_html = f"""
        <p {H3S}>🟠 強積累（≥5日 + 佔市場≥0.5%）</p>
        <table {TBL}><thead><tr>
          <th {TH}>代號 / 名稱</th><th {TH}>分點</th>
          <th {THR}>積累天數</th><th {THR}>窗口買超</th><th {THR}>收盤價</th>
        </tr></thead><tbody>{rows_a}</tbody></table>""" if rows_a else ""

    # ── 爆量 ──
    rows_sp = ""
    for br, s in spike_uniq[:8]:
        tk = s["ticker"]
        rows_sp += (
            f'<tr><td {TD}><b>{tk}</b> <span style="color:#64748b">{s["name"]}</span>'
            f'{tag_cons(tk)}{tag_strong(tk)}</td>'
            f'<td {TDR}><span style="font-size:.7rem;color:#94a3b8">{br}</span></td>'
            f'<td {TDR}><span style="color:#dc2626;font-weight:700">{s["spike"]:.1f}x</span></td>'
            f'<td {TDR}>{fmt_n(s["net"])}千</td>'
            f'<td {TDR}>{price_html(tk)}</td></tr>'
        )
    spike_html = f"""
        <p {H3S}>🔥 爆量個股</p>
        <table {TBL}><thead><tr>
          <th {TH}>代號 / 名稱</th><th {TH}>分點</th>
          <th {THR}>今/均倍率</th><th {THR}>今日買超</th><th {THR}>收盤價</th>
        </tr></thead><tbody>{rows_sp}</tbody></table>""" if rows_sp else ""

    footer = (f'<div style="text-align:center;padding:14px;color:#94a3b8;font-size:.72rem">'
              f'查看完整互動報表 → <a href="{PAGES_URL}" style="color:#3b82f6">{PAGES_URL}</a></div>') if PAGES_URL else ""

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#f1f5f9;margin:0;padding:12px">
<div style="max-width:640px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)">
  <div style="background:#1a1a2e;color:#fff;padding:16px 20px">
    <div style="font-size:.95rem;font-weight:700">📊 券商分點大額買進日報</div>
    <div style="font-size:.75rem;color:#94a3b8;margin-top:3px">{date_disp}</div>
  </div>
  <div style="background:#f8fafc;padding:8px 20px;border-bottom:1px solid #e2e8f0;display:flex;gap:16px;flex-wrap:wrap">
    <span style="font-size:.78rem">🔗 共識 <b style="color:#1d4ed8">{n_cons}</b></span>
    <span style="font-size:.78rem">🎯 囤貨 <b style="color:#b91c1c">{n_hoard}</b></span>
    <span style="font-size:.78rem">📈 連買≥5日 <b style="color:#c2410c">{n_streak5}</b></span>
    <span style="font-size:.78rem">🟠 強積累 <b style="color:#92400e">{n_strong}</b></span>
    <span style="font-size:.78rem">🔥 爆量 <b style="color:#dc2626">{n_spike}</b></span>
  </div>
  <div style="padding:12px 20px 20px">
    {cons_html}{hoard_html}{streak_html}{strong_html}{spike_html}
  </div>
  {footer}
</div></body></html>"""


def send_email(all_branches: list[dict], consensus: list[dict],
               twse_ohlc: dict, data_date: str, hoarding: list[dict] | None = None):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
        print("⚠️  EMAIL_SENDER/PASSWORD/RECIPIENT 未設定，略過寄信。")
        return
    date_disp = f"{data_date[:4]}/{data_date[4:6]}/{data_date[6:]}" if len(data_date)==8 else data_date
    digest = render_email_digest(all_branches, consensus, twse_ohlc, data_date, hoarding)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📊 券商分點日報 — {date_disp}"
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(digest, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_bytes())
        print(f"✅ 已寄送日報至 {EMAIL_RECIPIENT}")
    except Exception as e:
        print(f"❌ 寄信失敗：{e}")

# ════════════════════════════════════════════════════════════
# 主程式
# ════════════════════════════════════════════════════════════

def main():
    args = sys.argv[1:]

    # ── 分點管理 / 補抓 指令 ────────────────────────────────
    if "--list-branches" in args:
        cli_list_branches(); return
    if "--backfill-merge" in args:
        cli_backfill_merge(); return
    if "--backfill" in args:
        idx = args.index("--backfill")
        n   = int(args[idx + 1]) if idx + 1 < len(args) and args[idx + 1].isdigit() else 5
        cli_backfill(n); return
    if "--add" in args:
        idx = args.index("--add")
        url = args[idx + 1] if idx + 1 < len(args) else ""
        cli_add_branch(url); return
    if "--remove" in args:
        idx = args.index("--remove")
        name = args[idx + 1] if idx + 1 < len(args) else ""
        cli_remove_branch(name); return

    do_email  = "--email" in args or "--email-only" in args
    save_file = "--email-only" not in args

    print("═" * 55)
    print("  券商分點大額買進監控系統 v2 啟動")
    print("═" * 55)

    branches = load_branches()
    history_raw = load_history()   # 可能包含今天的快照（重跑時）
    print(f"  分點數：{len(branches)}，歷史快照（含今日）：{len(history_raw)} 日\n")

    # 抓取各分點資料（不含連買，待確認 data_date 後才計算）
    all_branches = []
    for br in branches:
        try:
            all_branches.append(fetch_branch(br))
        except Exception as e:
            print(f"  ⚠️ {br['名稱']} 失敗：{e}")
    if not all_branches:
        print("❌ 所有分點失敗"); sys.exit(1)

    data_date = next((b["data_date"] for b in all_branches if b.get("data_date")), "")

    # 過濾歷史：排除與今天同一交易日的快照（重跑時避免雙重計算）
    history = [h for h in history_raw if h.get("date") != data_date]
    print(f"  有效歷史快照（排除今日）：{len(history)} 日")

    # 計算籌碼動能（連買天數 + 積累信號）
    apply_accumulation(all_branches, history, data_date)

    # 儲存今日快照（供明日連買計算用）
    if data_date:
        save_daily_snapshot(all_branches, data_date)

    # 抓取 TWSE 成交量、OHLC（今日 + 整月歷史，供期間佔市場量計算及K線）
    all_tickers = list({s["ticker"] for b in all_branches for s in b["stocks"]})
    if data_date:
        twse_vol, twse_monthly, twse_ohlc = fetch_twse_volumes(all_tickers, data_date)
        apply_strong_accum(all_branches, twse_monthly)
    else:
        twse_vol, twse_monthly, twse_ohlc = {}, {}, {}

    # 囤貨分數（14日窗口，偵測分點吸籌行為）+ 新進榜偵測
    hoarding = compute_hoarding(all_branches, history, twse_monthly, twse_ohlc, data_date)
    prev_hoard_scores = load_hoard_scores()          # 寫入今日資料前的歷史快照
    mark_new_hoarding(hoarding, prev_hoard_scores, data_date)
    if data_date:
        save_hoard_scores(prev_hoard_scores, hoarding, data_date)

    # 共識分析
    consensus = build_consensus(all_branches)
    print(f"\n分析完成：共識 {len(consensus)} 檔，資料日期 {date_disp(data_date)}\n")

    # 爆量 + 連買摘要
    spikes  = [(b["名稱"], s["ticker"], s["name"], s["spike"])
               for b in all_branches for s in b["stocks"] if s.get("is_spike")]
    streaks = [(b["名稱"], s["ticker"], s["name"], s["streak"], s.get("streak_total",0))
               for b in all_branches for s in b["stocks"] if s.get("streak", 0) >= 3]
    accums  = [(b["名稱"], s["ticker"], s["name"], s["buy_days"], s.get("window_days",0), s.get("window_buy_tot",0))
               for b in all_branches for s in b["stocks"] if s.get("is_accumulating")]
    if spikes:
        print(f"🔥 爆量（{len(spikes)} 筆）：")
        for bn, tk, nm, sp in sorted(spikes, key=lambda x: -(x[3] or 0))[:15]:
            mkt = twse_vol.get(tk, 0)
            pct = f" | 佔市場 {spikes[0][2]}" if mkt else ""
            print(f"   {bn} | {tk} {nm} | {sp:.1f}x")
    if streaks:
        print(f"\n📈 連買≥3日（{len(streaks)} 筆）：")
        for bn, tk, nm, st, tot in sorted(streaks, key=lambda x: -x[3])[:10]:
            print(f"   {bn} | {tk} {nm} | 連{st}日 累計+{tot:,}千")
    if accums:
        print(f"\n🟠 積累中（{len(accums)} 筆）：")
        for bn, tk, nm, bd, wd, tot in sorted(accums, key=lambda x: -x[5])[:10]:
            print(f"   {bn} | {tk} {nm} | {bd}/{wd}日 累計+{tot:,}千")
    n_new = sum(1 for r in hoarding if r.get("is_new"))
    print(f"\n🎯 囤貨追蹤：{len(hoarding)} 筆（其中新進榜 {n_new} 筆）")
    for r in hoarding[:5]:
        cost_str = f"{r['est_cost']:.1f}" if r["est_cost"] else "–"
        close_str = f"{r['close']:.1f}" if r["close"] else "–"
        new_tag = " 🆕" if r.get("is_new") else ""
        print(f"   {r['branch']} | {r['ticker']} {r['name']} | 分數{r['score']:.0f} | 成本{cost_str} vs 現價{close_str}{new_tag}")

    # 產生 HTML
    html = render_html(all_branches, consensus, twse_vol, twse_monthly, twse_ohlc, hoarding)
    if save_file:
        out = SCRIPT_DIR / "index.html"
        out.write_text(html, "utf-8")
        print(f"\n✅ 已儲存：{out}")
    if do_email:
        send_email(all_branches, consensus, twse_ohlc, data_date, hoarding)

def date_disp(d: str) -> str:
    return f"{d[:4]}/{d[4:6]}/{d[6:]}" if len(d) == 8 else d

if __name__ == "__main__":
    main()

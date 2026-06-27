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

import subprocess, re, os, sys, smtplib, json
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
MAX_HISTORY     = 20     # 最多回溯交易日數

EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", "or.mouuu@gmail.com")
EMAIL_SENDER    = os.getenv("EMAIL_SENDER",    "")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD",  "")

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

def _fetch_one_volume(ticker: str, date_str: str) -> tuple[str, int, dict[str, int]]:
    """回傳 (ticker, 當日千元, {yyyymmdd: 千元} 整月)"""
    url = TWSE_API.format(date=date_str, ticker=ticker)
    try:
        out = subprocess.run(
            ['curl', '-s', '--max-time', '10', '-H', 'User-Agent: Mozilla/5.0', url],
            capture_output=True, text=True, timeout=15
        ).stdout
        data  = json.loads(out)
        rows  = data.get('data', [])
        monthly: dict[str, int] = {}
        for row in rows:
            try:
                ad = _roc_to_ymd(row[0])
                monthly[ad] = int(row[2].replace(',', '')) // 1000
            except Exception:
                pass
        today_val = monthly.get(date_str, (int(rows[-1][2].replace(',', '')) // 1000 if rows else 0))
        return ticker, today_val, monthly
    except Exception:
        pass
    return ticker, 0, {}

def fetch_twse_volumes(
    tickers: list[str], date_str: str
) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """並發抓取各股票 TWSE 成交金額。
    回傳 (twse_vol={ticker:今日千元}, twse_monthly={ticker:{yyyymmdd:千元}})"""
    print(f"  載入 TWSE 成交量（{len(tickers)} 檔，並發 10）…", end=" ", flush=True)
    twse_vol: dict[str, int] = {}
    twse_monthly: dict[str, dict[str, int]] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch_one_volume, t, date_str): t for t in tickers}
        for f in as_completed(futures):
            tk, val, monthly = f.result()
            if val > 0:
                twse_vol[tk] = val
            if monthly:
                twse_monthly[tk] = monthly
    print(f"成功 {len(twse_vol)}/{len(tickers)} 檔")
    return twse_vol, twse_monthly

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
    color = "#16a34a" if daily[-1] > 0 else "#ef4444"
    base  = f'<line x1="0" y1="{mid}" x2="{w}" y2="{mid}" stroke="#e2e8f0" stroke-width="0.8"/>'
    line  = (f'<polyline points="{pts}" fill="none" stroke="{color}" '
             f'stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>')
    dots  = "".join(
        f'<circle cx="{x}" cy="{y}" r="2" fill="{"#16a34a" if v>0 else "#ef4444" if v<0 else "#94a3b8"}"/>'
        for x, y, v in zip(xs, ys, daily)
    )
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'style="vertical-align:middle;margin-left:3px;flex-shrink:0" '
            f'xmlns="http://www.w3.org/2000/svg">{base}{line}{dots}</svg>')

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
        <th class="r" data-num="1" onclick="sortTable(this)" class="col-hide">跨點倍率 ↕</th>
        <th data-num="1" onclick="sortTable(this)">籌碼動能 ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this) col-hide">佔市場量 ↕</th>
        <th class="col-hide">買進分點</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table></div>"""

def _period_vol_pct_html(buy_gross: int, dates: list[str], ticker: str,
                         twse_monthly: dict, label: str) -> str:
    mkt_sum = sum(twse_monthly.get(ticker, {}).get(d, 0) for d in dates)
    title   = f"{label}買進 {fmt_n(buy_gross)} ÷ 期間市場 {fmt_n(mkt_sum)} 千元"
    return _vol_html(buy_gross, mkt_sum, title + "（📅期間）")

def render_branch_section(br: dict, twse_vol: dict,
                          twse_monthly: dict | None = None,
                          fini_data: dict | None = None) -> str:
    stocks = [s for s in br["stocks"] if s["net"] >= MIN_NET_DISPLAY]
    if not stocks:
        return f'<div class="branch-section"><div class="branch-title">{br["名稱"]} <span class="br-chip">無資料</span></div></div>'

    rows = ""
    for i, s in enumerate(stocks, 1):
        tk       = s["ticker"]
        mkt      = twse_vol.get(tk, 0)
        streak   = s.get("streak", 0)
        is_accum = s.get("is_accumulating", False)
        spike_dv = f"{s['spike']:.2f}" if s.get("spike") else "0"
        pure_tag = ' <span class="pill pill-green">純買</span>' if s["sell"] == 0 else ""

        fiveday = s.get("fiveday_net", 0)
        d10     = s.get("d10_net", 0)

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
          <td class="r net-pos" data-v="{s['net']}">{fmt_n(s['net'])}{pure_tag}</td>
          <td class="r" data-v="{fiveday}">{fmt_n(fiveday) if fiveday else '–'}</td>
          <td class="r col-hide" data-v="{d10}">{fmt_n(d10) if d10 else '–'}</td>
          <td class="r" data-v="{spike_dv}">{_spike_html(s.get('spike'))}</td>
          <td data-v="{streak}">{_dynamo_html(s)}</td>
          <td class="col-hide" data-v="{fini_data.get(tk, {}).get('fini_net', 0) if fini_data else 0}">{_fini_html(fini_data.get(tk) if fini_data else None)}</td>
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
          <th class="col-hide" data-num="1" onclick="sortTable(this)">外資/投信 ↕</th>
          <th class="r col-hide" data-num="1" onclick="sortTable(this)">佔市場量 ↕</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table></div>
    </div>"""

def _build_modal_divs(all_branches: list[dict]) -> str:
    """預建各 signal modal 內容（隱藏 div），由 openSignal() 複製到 modal-body"""

    # ── 爆量個股 ──
    spk_entries = sorted(
        [(b["名稱"], s["ticker"], s["name"], s.get("spike") or 0, s["net"])
         for b in all_branches for s in b["stocks"] if s.get("is_spike")],
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

    # ── 連買≥5日 ──
    s5_entries = sorted(
        [(b["名稱"], s["ticker"], s["name"], s.get("streak", 0), s.get("streak_total", 0))
         for b in all_branches for s in b["stocks"] if s.get("streak", 0) >= 5],
        key=lambda x: (-x[3], -x[4])
    )
    s5_items = "".join(
        f'<div class="modal-item">'
        f'<span class="tk-code">{tk}</span>'
        f'<span class="tk-name">{nm}</span>'
        f'<span class="br-chip">{br}</span>'
        f'<div class="modal-right">{_streak_html(d)}<br><span class="modal-sub">累計 +{fmt_n(tot)}千</span></div>'
        f'</div>'
        for br, tk, nm, d, tot in s5_entries
    )

    # ── 積累中（依密度排序：buy_days/window_days 高→低，再按 window_buy_tot）──
    ac_entries = sorted(
        [(b["名稱"], s["ticker"], s["name"],
          s.get("buy_days", 0), s.get("window_days", 1), s.get("window_buy_tot", 0),
          s.get("is_strong_accum", False))
         for b in all_branches for s in b["stocks"] if s.get("is_accumulating")],
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
                fini_data: dict | None = None) -> str:
    data_date  = next((b["data_date"] for b in all_branches if b.get("data_date")), "")
    date_disp  = f"{data_date[:4]}/{data_date[4:6]}/{data_date[6:]}" if len(data_date)==8 else data_date
    now_utc    = datetime.utcnow().strftime("%Y/%m/%d %H:%M UTC")
    total_br   = len(all_branches)
    spike_list  = [s for b in all_branches for s in b["stocks"] if s.get("is_spike")]
    streak_5p   = [s for b in all_branches for s in b["stocks"] if s.get("streak", 0) >= 5]
    accum_list  = [s for b in all_branches for s in b["stocks"] if s.get("is_accumulating")]
    accum_strong = sum(1 for s in accum_list if s.get("is_strong_accum"))
    total_buy_m = sum(s["buy"] for b in all_branches for s in b["stocks"]) // 1_000

    modal_divs = _build_modal_divs(all_branches)

    stats = f"""<div class="stats">
      <div class="stat"><div class="lbl">資料日期</div><div class="val" style="font-size:1rem">{date_disp}</div></div>
      <div class="stat"><div class="lbl">監控分點</div><div class="val">{total_br}</div></div>
      <div class="stat clickable" onclick="showTab('consensus')" title="切換至共識買進分頁">
        <div class="lbl">共識買進</div><div class="val">{len(consensus)}</div><div class="sub">≥2分點同買</div></div>
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
        panels  += f'<div id="{tid}" class="tab-panel">{render_branch_section(b, twse_vol, twse_monthly, fini_data)}</div>'

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
    {tab_nav}
  </div>
  <div id="consensus" class="tab-panel active">
    <p class="hint">同時出現在 ≥2 個分點買超（≥{MIN_NET_DISPLAY:,}千元）的股票，按分點數排序。<br>
    佔市場量 = 各分點買進合計 ÷ TWSE當日成交金額。連買天數含今日連續正買超天數。</p>
    {render_consensus_table(consensus, total_br, twse_vol)}
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

def send_email(html: str, data_date: str):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
        print("⚠️  EMAIL_SENDER/PASSWORD/RECIPIENT 未設定，略過寄信。")
        return
    date_disp = f"{data_date[:4]}/{data_date[4:6]}/{data_date[6:]}" if len(data_date)==8 else data_date
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"券商分點大額買進監控 — {date_disp}"
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_bytes())
        print(f"✅ 已寄送報告至 {EMAIL_RECIPIENT}")
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

    # 抓取 TWSE 成交量（今日 + 整月歷史，供期間佔市場量計算）
    all_tickers = list({s["ticker"] for b in all_branches for s in b["stocks"]})
    if data_date:
        twse_vol, twse_monthly = fetch_twse_volumes(all_tickers, data_date)
        fini_data = fetch_fini(data_date)
        apply_strong_accum(all_branches, twse_monthly)
    else:
        twse_vol, twse_monthly, fini_data = {}, {}, {}

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

    # 產生 HTML
    html = render_html(all_branches, consensus, twse_vol, twse_monthly, fini_data)
    if save_file:
        out = SCRIPT_DIR / "index.html"
        out.write_text(html, "utf-8")
        print(f"\n✅ 已儲存：{out}")
    if do_email:
        send_email(html, data_date)

def date_disp(d: str) -> str:
    return f"{d[:4]}/{d[4:6]}/{d[6:]}" if len(d) == 8 else d

if __name__ == "__main__":
    main()

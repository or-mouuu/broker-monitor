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
# TWSE 成交量（佔市場比 %）
# ════════════════════════════════════════════════════════════

def _fetch_one_volume(ticker: str, date_str: str) -> tuple[str, int]:
    url = TWSE_API.format(date=date_str, ticker=ticker)
    try:
        out = subprocess.run(
            ['curl', '-s', '--max-time', '10', '-H', 'User-Agent: Mozilla/5.0', url],
            capture_output=True, text=True, timeout=15
        ).stdout
        data = json.loads(out)
        rows = data.get('data', [])
        if rows:
            # 欄位: [日期, 成交股數, 成交金額, 開盤, 最高, 最低, 收盤, 漲跌, 筆數]
            val_k = int(rows[-1][2].replace(',', '')) // 1000
            return ticker, val_k
    except Exception:
        pass
    return ticker, 0

def fetch_twse_volumes(tickers: list[str], date_str: str) -> dict[str, int]:
    """並發抓取各股票當日 TWSE 成交金額（千元）"""
    print(f"  載入 TWSE 成交量（{len(tickers)} 檔，並發 10）…", end=" ", flush=True)
    volumes: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch_one_volume, t, date_str): t for t in tickers}
        for f in as_completed(futures):
            tk, val = f.result()
            if val > 0:
                volumes[tk] = val
    print(f"成功 {len(volumes)}/{len(tickers)} 檔")
    return volumes

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

def count_consecutive_buy_days(
    history: list[dict], branch_name: str, ticker: str, today_net: int
) -> int:
    """計算（含今日）連續買超天數"""
    streak = 1 if today_net > 0 else 0
    if streak == 0:
        return 0
    for day in history:          # 昨日及更早，由新至舊
        stocks = day.get("branches", {}).get(branch_name, [])
        hit = next((s for s in stocks if s["ticker"] == ticker), None)
        if hit and hit["net"] > 0:
            streak += 1
        else:
            break
    return streak

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
            "avg_net":  avg_net,
            "spike":    spike,
            "is_spike": spike is not None and spike >= SPIKE_THRESHOLD,
            "streak":   0,   # 由 main() 確認 data_date 後填入
        })

    print(f"今日 {len(today_stocks)} 筆，五日 {len(fiveday_stocks)} 筆")
    return {"名稱": name, "stocks": merged, "data_date": data_date}


def apply_streaks(all_branches: list[dict], history: list[dict]):
    """已知 data_date 並過濾完歷史後，計算各分點各股的連買天數。"""
    for br in all_branches:
        for s in br["stocks"]:
            s["streak"] = count_consecutive_buy_days(
                history, br["名稱"], s["ticker"], s["net"]
            )

def build_consensus(all_branches: list[dict]) -> list[dict]:
    agg = defaultdict(lambda: {
        "name": "", "branches": [], "total_buy": 0, "total_net": 0,
        "total_avg_net": 0, "spike_count": 0, "max_streak": 0,
    })
    for br in all_branches:
        for s in br["stocks"]:
            if s["net"] < MIN_NET_DISPLAY:
                continue
            tk = s["ticker"]
            agg[tk]["name"]          = s["name"]
            agg[tk]["branches"].append(br["名稱"])
            agg[tk]["total_buy"]    += s["buy"]
            agg[tk]["total_net"]    += s["net"]
            agg[tk]["total_avg_net"] += s.get("avg_net", 0)
            if s.get("is_spike"):
                agg[tk]["spike_count"] += 1
            agg[tk]["max_streak"] = max(agg[tk]["max_streak"], s.get("streak", 0))

    result = []
    for tk, v in agg.items():
        if len(v["branches"]) < 2:
            continue
        cross_spike = (v["total_net"] / v["total_avg_net"]
                       if v["total_avg_net"] > 0 else None)
        result.append({
            "ticker":       tk,
            "name":         v["name"],
            "branch_count": len(v["branches"]),
            "branches":     v["branches"],
            "total_buy":    v["total_buy"],
            "total_net":    v["total_net"],
            "cross_spike":  cross_spike,
            "is_spike":     cross_spike is not None and cross_spike >= SPIKE_THRESHOLD,
            "spike_count":  v["spike_count"],
            "max_streak":   v["max_streak"],
        })
    result.sort(key=lambda x: (-x["branch_count"], -x["total_net"]))
    return result

# ════════════════════════════════════════════════════════════
# HTML 渲染
# ════════════════════════════════════════════════════════════

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Noto Sans TC',Arial,sans-serif;background:#f5f6f8;color:#1a1a2e;font-size:14px}
header{background:#1a1a2e;color:#fff;padding:1.1rem 1.4rem;display:flex;justify-content:space-between;align-items:center}
header h1{font-size:1.2rem;font-weight:600}
header .meta{font-size:.78rem;opacity:.65;text-align:right;line-height:1.7}
.container{max-width:1300px;margin:0 auto;padding:.9rem 1rem 3rem}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.7rem;margin:.9rem 0}
.stat{background:#fff;border-radius:8px;padding:.8rem 1rem;border:1px solid #e8eaed}
.stat .lbl{font-size:.72rem;color:#888;margin-bottom:.2rem}
.stat .val{font-size:1.5rem;font-weight:600}
.stat .sub{font-size:.68rem;color:#aaa;margin-top:.1rem}
.tabs{display:flex;gap:.4rem;margin:1.1rem 0 0;border-bottom:2px solid #e8eaed;flex-wrap:wrap}
.tab-btn{background:none;border:none;padding:.55rem 1.1rem;cursor:pointer;font-size:.88rem;
  color:#888;border-bottom:3px solid transparent;margin-bottom:-2px;border-radius:4px 4px 0 0;
  font-family:inherit;white-space:nowrap;transition:color .12s,border-color .12s}
.tab-btn:hover{color:#1a1a2e;background:#f0f2f5}
.tab-btn.active{color:#2563eb;border-bottom-color:#2563eb;font-weight:600}
.tab-panel{display:none;padding:.8rem 0}
.tab-panel.active{display:block}
.tbl-wrap{overflow-x:auto;border-radius:8px;border:1px solid #e8eaed;background:#fff;margin-bottom:1.4rem}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{background:#f8f9fb;color:#555;font-weight:600;padding:.55rem .8rem;text-align:left;
   border-bottom:1px solid #e8eaed;white-space:nowrap}
th button{background:none;border:none;cursor:pointer;font:inherit;color:inherit;padding:0;width:100%}
th.r,td.r{text-align:right}
td{padding:.5rem .8rem;border-bottom:1px solid #f0f2f5;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafbff}
.branch-section{margin-bottom:1.8rem}
.branch-title{font-size:.95rem;font-weight:600;padding:.5rem 0 .45rem;
  border-bottom:2px solid #2563eb;margin-bottom:.65rem;display:flex;align-items:center;gap:.4rem;flex-wrap:wrap}
.br-chip{font-size:.68rem;background:#f0f2f5;color:#555;padding:.1rem .4rem;border-radius:4px;margin:.1rem .1rem 0}
/* Badges */
.cnt-badge{display:inline-block;padding:.15rem .45rem;border-radius:4px;font-weight:600;font-size:.78rem}
.bc-all{background:#e8f0fe;color:#1a73e8}.bc-most{background:#e6f4ea;color:#137333}
.bc-some{background:#fef7e0;color:#b06000}.bc-few{background:#f1efe8;color:#5f5e5a}
.tag-spike{display:inline-block;background:#fce8e6;color:#c5221f;font-size:.66rem;padding:.1rem .38rem;border-radius:4px;font-weight:700}
.tag-pure{display:inline-block;background:#e6f4ea;color:#137333;font-size:.66rem;padding:.1rem .38rem;border-radius:4px;font-weight:600}
/* Streak badges */
.streak-1{color:#aaa;font-size:.75rem}
.streak-2,.streak-3{display:inline-block;background:#dbeafe;color:#1d4ed8;font-size:.69rem;padding:.1rem .4rem;border-radius:4px;font-weight:600}
.streak-high{display:inline-block;background:#fef3c7;color:#92400e;font-size:.69rem;padding:.1rem .4rem;border-radius:4px;font-weight:700}
.streak-vhigh{display:inline-block;background:#fee2e2;color:#991b1b;font-size:.69rem;padding:.1rem .4rem;border-radius:4px;font-weight:700}
/* Market vol % */
.vol-low{color:#aaa;font-size:.78rem}
.vol-mid{color:#1d4ed8;font-size:.78rem;font-weight:500}
.vol-high{color:#b45309;font-size:.78rem;font-weight:700}
.vol-vhigh{color:#991b1b;font-size:.78rem;font-weight:700}
.net-pos{color:#0a8043}
.hint{font-size:.76rem;color:#888;margin-bottom:.65rem;line-height:1.6}
@media(max-width:600px){.stats{grid-template-columns:1fr 1fr}td,th{padding:.4rem .55rem;font-size:.76rem}}
"""

JS = """
function showTab(id){
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  document.querySelector('[data-tab="'+id+'"]').classList.add('active');
}
function sortTable(th){
  var table=th.closest('table'), idx=Array.from(th.parentElement.children).indexOf(th);
  var isNum=th.dataset.num==='1', asc=th.dataset.asc==='1';
  var rows=Array.from(table.querySelectorAll('tbody tr'));
  rows.sort(function(a,b){
    var av=a.cells[idx]?.dataset.v ?? a.cells[idx]?.textContent.trim() ?? '';
    var bv=b.cells[idx]?.dataset.v ?? b.cells[idx]?.textContent.trim() ?? '';
    return isNum?(asc?parseFloat(av)-parseFloat(bv):parseFloat(bv)-parseFloat(av))
               :(asc?av.localeCompare(bv,'zh-TW'):bv.localeCompare(av,'zh-TW'));
  });
  th.dataset.asc=asc?'0':'1';
  rows.forEach(r=>table.querySelector('tbody').appendChild(r));
}
"""

def fmt_n(n, d=0):
    if n == 0: return "–"
    try:
        return f"{n:,.{d}f}" if d else f"{int(n):,}"
    except: return str(n)

def _streak_html(streak: int) -> str:
    if streak <= 0: return '<span class="streak-1">–</span>'
    if streak == 1: return '<span class="streak-1">今1日</span>'
    if streak <= 3:  return f'<span class="streak-2">連{streak}日</span>'
    if streak <= 5:  return f'<span class="streak-high">🔥連{streak}日</span>'
    return f'<span class="streak-vhigh">🔴連{streak}日</span>'

def _vol_pct_html(buy_k: int, market_k: int) -> str:
    if market_k <= 0: return '<span class="vol-low">–</span>'
    pct = buy_k / market_k * 100
    val = f"{pct:.2f}%"
    if pct < 0.5:  cls = "vol-low"
    elif pct < 2:  cls = "vol-mid"
    elif pct < 5:  cls = "vol-high"
    else:          cls = "vol-vhigh"
    return f'<span class="{cls}" title="分點買進佔市場成交量">{val}</span>'

def _spike_html(spike) -> str:
    if spike is None: return '<span style="color:#bbb">–</span>'
    cls = "color:#d93025;font-weight:700" if spike >= SPIKE_THRESHOLD else "color:#888"
    tag = ' <span class="tag-spike">爆量</span>' if spike >= SPIKE_THRESHOLD else ""
    return f'<span style="{cls}">{spike:.1f}x</span>{tag}'

def _bc_class(n, total):
    return "bc-all" if n == total else "bc-most" if n >= total*0.7 else "bc-some" if n >= 3 else "bc-few"

def render_consensus_table(consensus, total_branches, twse_vol):
    rows = ""
    for s in consensus:
        br_html = "".join(f'<span class="br-chip">{b}</span>' for b in s["branches"])
        ticker  = s["ticker"]
        mkt     = twse_vol.get(ticker, 0)
        bc_cls  = _bc_class(s["branch_count"], total_branches)
        cs      = s["cross_spike"]
        spike_s = f"{cs:.2f}" if cs else "0"
        rows += f"""<tr>
          <td><strong>{ticker}</strong></td>
          <td>{s['name']}</td>
          <td class="r" data-v="{s['branch_count']}">
            <span class="cnt-badge {bc_cls}">{s['branch_count']}/{total_branches}</span></td>
          <td class="r net-pos" data-v="{s['total_net']}">{fmt_n(s['total_net'])}</td>
          <td class="r" data-v="{s['total_buy']}">{fmt_n(s['total_buy'])}</td>
          <td class="r" data-v="{spike_s}">{_spike_html(cs)}</td>
          <td class="r" data-v="{s['max_streak']}">{_streak_html(s['max_streak'])}</td>
          <td class="r" data-v="{s['total_net']/mkt*100 if mkt else 0:.3f}">{_vol_pct_html(s['total_buy'], mkt)}</td>
          <td>{br_html}</td>
        </tr>"""
    return f"""<div class="tbl-wrap"><table>
      <thead><tr>
        <th onclick="sortTable(this)">代號 ↕</th>
        <th onclick="sortTable(this)">名稱 ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">分點數 ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">總買超(千元) ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">總買進(千元) ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">跨分點倍率 ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">最長連買 ↕</th>
        <th class="r" data-num="1" onclick="sortTable(this)">佔市場量 ↕</th>
        <th>買進分點</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table></div>"""

def render_branch_section(br: dict, twse_vol: dict) -> str:
    stocks = [s for s in br["stocks"] if s["net"] >= MIN_NET_DISPLAY]
    if not stocks:
        return f'<div class="branch-section"><div class="branch-title">{br["名稱"]} <span class="br-chip">無顯著資料</span></div></div>'

    rows = ""
    for i, s in enumerate(stocks, 1):
        tk       = s["ticker"]
        mkt      = twse_vol.get(tk, 0)
        pure     = ' <span class="tag-pure">純買</span>' if s["sell"] == 0 else ""
        streak   = s.get("streak", 0)
        spike_dv = f"{s['spike']:.2f}" if s.get('spike') else '0'
        vol_dv   = f"{s['buy']/mkt*100:.3f}" if mkt else '0'
        rows += f"""<tr>
          <td>{i}</td>
          <td><strong>{tk}</strong></td>
          <td>{s['name']}</td>
          <td class="r" data-v="{s['buy']}">{fmt_n(s['buy'])}</td>
          <td class="r" data-v="{s['sell']}">{fmt_n(s['sell'])}{pure}</td>
          <td class="r net-pos" data-v="{s['net']}">{fmt_n(s['net'])}</td>
          <td class="r" data-v="{s.get('avg_net',0):.0f}">{fmt_n(s.get('avg_net',0))}</td>
          <td class="r" data-v="{spike_dv}">{_spike_html(s.get('spike'))}</td>
          <td class="r" data-v="{streak}">{_streak_html(streak)}</td>
          <td class="r" data-v="{vol_dv}">{_vol_pct_html(s['buy'], mkt)}</td>
        </tr>"""

    spk_cnt    = sum(1 for s in stocks if s.get("is_spike"))
    streak_max = max((s.get("streak", 0) for s in stocks), default=0)
    badges     = []
    if spk_cnt:    badges.append(f'<span class="br-chip">🔥 {spk_cnt} 爆量</span>')
    if streak_max >= 3: badges.append(f'<span class="br-chip">📈 最長連買 {streak_max} 日</span>')
    return f"""<div class="branch-section">
      <div class="branch-title">{br['名稱']} {''.join(badges)}</div>
      <div class="tbl-wrap"><table>
        <thead><tr>
          <th data-num="1" onclick="sortTable(this)"># ↕</th>
          <th onclick="sortTable(this)">代號 ↕</th>
          <th onclick="sortTable(this)">名稱 ↕</th>
          <th class="r" data-num="1" onclick="sortTable(this)">買進(千元) ↕</th>
          <th class="r" data-num="1" onclick="sortTable(this)">賣出(千元) ↕</th>
          <th class="r" data-num="1" onclick="sortTable(this)">今日買超 ↕</th>
          <th class="r" data-num="1" onclick="sortTable(this)">5日均買超 ↕</th>
          <th class="r" data-num="1" onclick="sortTable(this)">今/均倍率 ↕</th>
          <th class="r" data-num="1" onclick="sortTable(this)">連買天數 ↕</th>
          <th class="r" data-num="1" onclick="sortTable(this)">佔市場量 ↕</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table></div>
    </div>"""

def render_html(all_branches: list[dict], consensus: list[dict],
                twse_vol: dict) -> str:
    data_date  = next((b["data_date"] for b in all_branches if b.get("data_date")), "")
    date_disp  = f"{data_date[:4]}/{data_date[4:6]}/{data_date[6:]}" if len(data_date)==8 else data_date
    now_utc    = datetime.utcnow().strftime("%Y/%m/%d %H:%M UTC")
    total_br   = len(all_branches)
    spike_list = [s for b in all_branches for s in b["stocks"] if s.get("is_spike")]
    streak_5p  = [s for b in all_branches for s in b["stocks"] if s.get("streak", 0) >= 5]
    total_buy_m = sum(s["buy"] for b in all_branches for s in b["stocks"]) // 1_000

    stats = f"""<div class="stats">
      <div class="stat"><div class="lbl">資料日期</div><div class="val" style="font-size:1rem">{date_disp}</div></div>
      <div class="stat"><div class="lbl">監控分點</div><div class="val">{total_br}</div></div>
      <div class="stat"><div class="lbl">共識買進</div><div class="val">{len(consensus)}</div><div class="sub">≥2分點同買</div></div>
      <div class="stat"><div class="lbl">爆量個股</div><div class="val" style="color:#d93025">{len(spike_list)}</div><div class="sub">≥{int(SPIKE_THRESHOLD*100)}% 均量</div></div>
      <div class="stat"><div class="lbl">連買≥5日</div><div class="val" style="color:#92400e">{len(streak_5p)}</div><div class="sub">強度信號</div></div>
      <div class="stat"><div class="lbl">各分點合計買進</div><div class="val" style="font-size:1rem">{total_buy_m:,}M</div><div class="sub">百萬元</div></div>
    </div>"""

    tab_nav = ""
    panels  = ""
    for b in all_branches:
        tid = f"br-{b['名稱']}"
        tab_nav += f'<button class="tab-btn" data-tab="{tid}" onclick="showTab(\'{tid}\')">{b["名稱"]}</button>'
        panels  += f'<div id="{tid}" class="tab-panel">{render_branch_section(b, twse_vol)}</div>'

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

    # ── 分點管理指令 ────────────────────────────────────────
    if "--list-branches" in args:
        cli_list_branches(); return
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

    # 計算連買天數（用過濾後的歷史）
    apply_streaks(all_branches, history)

    # 儲存今日快照（供明日連買計算用）
    if data_date:
        save_daily_snapshot(all_branches, data_date)

    # 抓取 TWSE 成交量
    all_tickers = list({s["ticker"] for b in all_branches for s in b["stocks"]})
    twse_vol = fetch_twse_volumes(all_tickers, data_date) if data_date else {}

    # 共識分析
    consensus = build_consensus(all_branches)
    print(f"\n分析完成：共識 {len(consensus)} 檔，資料日期 {date_disp(data_date)}\n")

    # 爆量 + 連買摘要
    spikes  = [(b["名稱"], s["ticker"], s["name"], s["spike"])
               for b in all_branches for s in b["stocks"] if s.get("is_spike")]
    streaks = [(b["名稱"], s["ticker"], s["name"], s["streak"])
               for b in all_branches for s in b["stocks"] if s.get("streak", 0) >= 3]
    if spikes:
        print(f"🔥 爆量（{len(spikes)} 筆）：")
        for bn, tk, nm, sp in sorted(spikes, key=lambda x: -(x[3] or 0))[:15]:
            mkt = twse_vol.get(tk, 0)
            pct = f" | 佔市場 {spikes[0][2]}" if mkt else ""
            print(f"   {bn} | {tk} {nm} | {sp:.1f}x")
    if streaks:
        print(f"\n📈 連買≥3日（{len(streaks)} 筆）：")
        for bn, tk, nm, st in sorted(streaks, key=lambda x: -x[3])[:10]:
            print(f"   {bn} | {tk} {nm} | 連{st}日")

    # 產生 HTML
    html = render_html(all_branches, consensus, twse_vol)
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

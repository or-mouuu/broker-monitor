#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
券商分點大額買進監控系統
Broker Branch Large Buy Activity Monitor

使用方式：
  python broker_monitor.py              # 產生 index.html
  python broker_monitor.py --email      # 產生 index.html 並寄送 Email
  python broker_monitor.py --email-only # 只寄送 Email（不存檔）
"""

import subprocess
import re
import os
import sys
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict
from datetime import datetime

# ═══════════════════════════════════════════════════════════
# ★ 分點設定  — 如需新增或移除分點，請修改此處
# ═══════════════════════════════════════════════════════════

BRANCHES = [
    {"名稱": "元大-崇德",   "a": "9800", "b": "0039003800310053"},
    {"名稱": "台新",        "a": "9B00", "b": "0039004200300030"},
    {"名稱": "富邦-仁愛",   "a": "9600", "b": "9676"},
    {"名稱": "國票-安和",   "a": "7790", "b": "003700370039005a"},
    {"名稱": "國票證券",    "a": "7790", "b": "7790"},
    {"名稱": "國泰-敦南",   "a": "8880", "b": "8888"},
    {"名稱": "大和國泰",    "a": "8890", "b": "8890"},
]

SPIKE_THRESHOLD = 1.5      # 爆量門檻（今日買超 / 五日均 ≥ 150%）
MIN_NET_DISPLAY = 3_000    # 最低顯示買超門檻（千元），避免雜訊
BASE_URL = "https://fubon-ebrokerdj.fbs.com.tw/z/zg/zgb/zgb0.djhtm"

# 電子郵件設定（由環境變數讀取，或直接填寫）
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", "or.mouuu@gmail.com")
EMAIL_SENDER    = os.getenv("EMAIL_SENDER",    "")   # 你的 Gmail 帳號
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD",  "")   # Gmail App Password

# ═══════════════════════════════════════════════════════════
# 資料抓取
# ═══════════════════════════════════════════════════════════

def fetch_html(a: str, b: str, days: int = 1) -> str:
    """抓取分點買賣超頁面 HTML（自動 Big5 → UTF-8 轉換）"""
    url = f"{BASE_URL}?a={a}&b={b}"
    if days > 1:
        url += f"&c=B&d={days}"
    cmd = (
        f"curl -s --compressed "
        f"-H 'Accept-Language: zh-TW,zh;q=0.9' "
        f"-H 'User-Agent: Mozilla/5.0' "
        f"'{url}' | iconv -f big5 -t utf-8 2>/dev/null"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    return result.stdout


def parse_buy_stocks(html: str) -> tuple[list[dict], str]:
    """
    解析買超清單，回傳 (股票列表, 資料日期)
    每筆：{ticker, name, buy, sell, net}  — 單位：千元
    """
    # 只取買超區段（買超 header 到 賣超 header 之間）
    buy_start = html.find('<tr><td class="t2" colspan="4">買超</td></tr>')
    if buy_start == -1:
        return [], ""
    sell_start = html.find('<tr><td class="t2" colspan="4">賣超</td></tr>', buy_start)
    section = html[buy_start:sell_start] if sell_start != -1 else html[buy_start:]

    date_m = re.search(r'資料日期：(\d{8})', html)
    data_date = date_m.group(1) if date_m else ""

    stocks = []
    for entry in re.split(r'<td class="t4t1"', section)[1:]:
        # 匹配兩種股票連結格式
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
            buy  = int(nums[0].replace(',', ''))
            sell = int(nums[1].replace(',', ''))
            net  = int(nums[2].replace(',', ''))
            stocks.append({"ticker": ticker, "name": name,
                           "buy": buy, "sell": sell, "net": net})
    return stocks, data_date


# ═══════════════════════════════════════════════════════════
# 資料整合
# ═══════════════════════════════════════════════════════════

def fetch_branch(branch: dict) -> dict:
    """抓取單一分點今日 + 五日資料，並合併計算爆量倍率"""
    a, b, name = branch["a"], branch["b"], branch["名稱"]
    print(f"  ▶ 抓取 {name}…", end=" ", flush=True)

    today_html   = fetch_html(a, b, days=1)
    fiveday_html = fetch_html(a, b, days=5)

    today_stocks, data_date = parse_buy_stocks(today_html)
    fiveday_stocks, _       = parse_buy_stocks(fiveday_html)

    # 五日資料 → dict by ticker
    fd_map = {s["ticker"]: s for s in fiveday_stocks}

    merged = []
    for s in today_stocks:
        tk = s["ticker"]
        fd = fd_map.get(tk)
        avg_buy  = (fd["buy"]  / 5) if fd else 0
        avg_net  = (fd["net"]  / 5) if fd else 0
        spike    = (s["net"] / avg_net) if avg_net > 0 else None
        merged.append({
            "ticker":   tk,
            "name":     s["name"],
            "buy":      s["buy"],
            "sell":     s["sell"],
            "net":      s["net"],
            "avg_buy":  avg_buy,
            "avg_net":  avg_net,
            "spike":    spike,
            "is_spike": spike is not None and spike >= SPIKE_THRESHOLD,
        })

    print(f"今日 {len(today_stocks)} 筆，五日 {len(fiveday_stocks)} 筆")
    return {"名稱": name, "stocks": merged, "data_date": data_date}


def build_consensus(all_branches: list[dict]) -> list[dict]:
    """統計跨分點共識買進：同一股票被多個分點同時買超"""
    agg = defaultdict(lambda: {
        "name": "", "branches": [], "total_buy": 0, "total_net": 0,
        "total_avg_net": 0, "spike_count": 0
    })
    for br in all_branches:
        for s in br["stocks"]:
            if s["net"] < MIN_NET_DISPLAY:
                continue
            tk = s["ticker"]
            agg[tk]["name"]        = s["name"]
            agg[tk]["branches"].append(br["名稱"])
            agg[tk]["total_buy"]   += s["buy"]
            agg[tk]["total_net"]   += s["net"]
            agg[tk]["total_avg_net"] += s.get("avg_net", 0)
            if s.get("is_spike"):
                agg[tk]["spike_count"] += 1

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
        })

    result.sort(key=lambda x: (-x["branch_count"], -x["total_net"]))
    return result


# ═══════════════════════════════════════════════════════════
# HTML 產生
# ═══════════════════════════════════════════════════════════

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Noto Sans TC',Arial,sans-serif;background:#f5f6f8;color:#1a1a2e;font-size:14px}
a{color:inherit;text-decoration:none}
header{background:#1a1a2e;color:#fff;padding:1.2rem 1.5rem;display:flex;justify-content:space-between;align-items:center}
header h1{font-size:1.3rem;font-weight:600}
header .meta{font-size:.8rem;opacity:.7;text-align:right;line-height:1.6}
.container{max-width:1280px;margin:0 auto;padding:1rem 1rem 3rem}
/* Stats cards */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.75rem;margin:1rem 0}
.stat{background:#fff;border-radius:8px;padding:.9rem 1.1rem;border:1px solid #e8eaed}
.stat .lbl{font-size:.75rem;color:#888;margin-bottom:.25rem}
.stat .val{font-size:1.6rem;font-weight:600;color:#1a1a2e}
.stat .sub{font-size:.7rem;color:#aaa;margin-top:.15rem}
/* Tabs */
.tabs{display:flex;gap:.5rem;margin:1.25rem 0 0;border-bottom:2px solid #e8eaed;padding-bottom:0}
.tab-btn{background:none;border:none;padding:.6rem 1.2rem;cursor:pointer;font-size:.9rem;
  color:#888;border-bottom:3px solid transparent;margin-bottom:-2px;border-radius:4px 4px 0 0;
  font-family:inherit;transition:all .15s}
.tab-btn:hover{color:#1a1a2e;background:#f0f2f5}
.tab-btn.active{color:#2563eb;border-bottom-color:#2563eb;font-weight:600}
.tab-panel{display:none;padding:1rem 0}
.tab-panel.active{display:block}
/* Tables */
.tbl-wrap{overflow-x:auto;border-radius:8px;border:1px solid #e8eaed;background:#fff;margin-bottom:1.5rem}
table{width:100%;border-collapse:collapse;font-size:.83rem}
th{background:#f8f9fb;color:#555;font-weight:600;padding:.6rem .85rem;text-align:left;
  border-bottom:1px solid #e8eaed;white-space:nowrap;cursor:pointer;user-select:none}
th:hover{background:#eef1f5}
th.r,td.r{text-align:right}
td{padding:.55rem .85rem;border-bottom:1px solid #f0f2f5;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafbff}
/* Branch section */
.branch-section{margin-bottom:2rem}
.branch-title{font-size:1rem;font-weight:600;color:#1a1a2e;padding:.6rem 0 .5rem;
  border-bottom:2px solid #2563eb;margin-bottom:.75rem;display:flex;align-items:center;gap:.5rem}
.branch-badge{font-size:.7rem;background:#e8f0fe;color:#1a73e8;padding:.15rem .5rem;
  border-radius:12px;font-weight:500}
/* Badges & indicators */
.br-chip{display:inline-block;font-size:.7rem;background:#f0f2f5;color:#555;
  padding:.1rem .45rem;border-radius:4px;margin:.1rem .15rem 0 0;white-space:nowrap}
.spike-yes{color:#d93025;font-weight:700}
.spike-no{color:#888}
.net-pos{color:#0a8043}
.tag-spike{display:inline-block;background:#fce8e6;color:#c5221f;font-size:.68rem;
  padding:.1rem .4rem;border-radius:4px;font-weight:600}
.tag-strong{display:inline-block;background:#e6f4ea;color:#137333;font-size:.68rem;
  padding:.1rem .4rem;border-radius:4px;font-weight:600}
.bc-all{background:#e8f0fe;color:#1a73e8}
.bc-most{background:#e6f4ea;color:#137333}
.bc-some{background:#fef7e0;color:#b06000}
.cnt-badge{display:inline-block;padding:.15rem .5rem;border-radius:4px;font-weight:600;font-size:.8rem}
/* Responsive */
@media(max-width:600px){
  .stats{grid-template-columns:1fr 1fr}
  td,th{padding:.45rem .6rem;font-size:.78rem}
}
"""

JS = """
function showTab(id){
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  document.querySelector('[data-tab="'+id+'"]').classList.add('active');
}
function sortTable(btn){
  var th=btn.parentElement,table=th.closest('table');
  var idx=Array.from(th.parentElement.children).indexOf(th);
  var isNum=th.dataset.num==='1';
  var rows=Array.from(table.querySelectorAll('tbody tr'));
  var asc=th.dataset.asc==='1';
  rows.sort(function(a,b){
    var av=a.cells[idx]?.dataset.v??a.cells[idx]?.textContent.trim()??'';
    var bv=b.cells[idx]?.dataset.v??b.cells[idx]?.textContent.trim()??'';
    if(isNum){return asc?(parseFloat(av)-parseFloat(bv)):(parseFloat(bv)-parseFloat(av));}
    return asc?av.localeCompare(bv,'zh-TW'):bv.localeCompare(av,'zh-TW');
  });
  th.dataset.asc=asc?'0':'1';
  rows.forEach(r=>table.querySelector('tbody').appendChild(r));
}
"""


def fmt_n(n: float, decimal: int = 0) -> str:
    if n == 0:
        return "–"
    try:
        if decimal:
            return f"{n:,.{decimal}f}"
        return f"{int(n):,}"
    except Exception:
        return str(n)


def spike_badge(spike) -> str:
    if spike is None:
        return '<span class="spike-no">–</span>'
    cls = "spike-yes" if spike >= SPIKE_THRESHOLD else "spike-no"
    tag = f'<span class="tag-spike">爆量</span>' if spike >= SPIKE_THRESHOLD else ""
    return f'<span class="{cls}">{spike:.1f}x</span> {tag}'


def branch_count_badge(n: int, total: int) -> str:
    cls = "bc-all" if n == total else ("bc-most" if n >= total * 0.7 else
                                        ("bc-some" if n >= 3 else ""))
    return f'<span class="cnt-badge {cls}">{n}/{total}</span>'


def render_consensus_table(consensus: list[dict], total_branches: int) -> str:
    rows = ""
    for s in consensus:
        br_html = "".join(f'<span class="br-chip">{b}</span>' for b in s["branches"])
        spike_v = s["cross_spike"]
        spike_disp = spike_badge(spike_v)
        spike_sort = f'{spike_v:.2f}' if spike_v else "0"
        rows += f"""<tr>
          <td><strong>{s['ticker']}</strong></td>
          <td>{s['name']}</td>
          <td class="r" data-v="{s['branch_count']}">{branch_count_badge(s['branch_count'], total_branches)}</td>
          <td class="r net-pos" data-v="{s['total_net']}">{fmt_n(s['total_net'])}</td>
          <td class="r" data-v="{s['total_buy']}">{fmt_n(s['total_buy'])}</td>
          <td class="r" data-v="{spike_sort}">{spike_disp}</td>
          <td>{br_html}</td>
        </tr>"""

    return f"""<div class="tbl-wrap">
      <table>
        <thead><tr>
          <th><button onclick="sortTable(this)">代號 ↕</button></th>
          <th><button onclick="sortTable(this)">名稱 ↕</button></th>
          <th class="r" data-num="1"><button onclick="sortTable(this)">分點數 ↕</button></th>
          <th class="r" data-num="1"><button onclick="sortTable(this)">總買超(千元) ↕</button></th>
          <th class="r" data-num="1"><button onclick="sortTable(this)">總買進(千元) ↕</button></th>
          <th class="r" data-num="1"><button onclick="sortTable(this)">跨分點倍率 ↕</button></th>
          <th>買進分點</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def render_branch_section(br: dict) -> str:
    stocks = [s for s in br["stocks"] if s["net"] >= MIN_NET_DISPLAY]
    if not stocks:
        return f"""<div class="branch-section">
          <div class="branch-title">{br['名稱']} <span class="branch-badge">無顯著資料</span></div>
        </div>"""

    rows = ""
    for i, s in enumerate(stocks, 1):
        net_pos_cls = "net-pos" if s["net"] > 0 else ""
        spike_disp  = spike_badge(s.get("spike"))
        spike_sort  = f'{s["spike"]:.2f}' if s.get("spike") else "0"
        avg_net_disp = fmt_n(s.get("avg_net", 0))
        sell_zero = '<span class="tag-strong">純買</span>' if s["sell"] == 0 else ""

        rows += f"""<tr>
          <td>{i}</td>
          <td><strong>{s['ticker']}</strong></td>
          <td>{s['name']}</td>
          <td class="r" data-v="{s['buy']}">{fmt_n(s['buy'])}</td>
          <td class="r" data-v="{s['sell']}">{fmt_n(s['sell'])} {sell_zero}</td>
          <td class="r {net_pos_cls}" data-v="{s['net']}">{fmt_n(s['net'])}</td>
          <td class="r" data-v="{s.get('avg_net',0)}">{avg_net_disp}</td>
          <td class="r" data-v="{spike_sort}">{spike_disp}</td>
        </tr>"""

    spike_cnt = sum(1 for s in stocks if s.get("is_spike"))
    extra = f'<span class="branch-badge">🔥 {spike_cnt} 檔爆量</span>' if spike_cnt else ""

    return f"""<div class="branch-section">
      <div class="branch-title">{br['名稱']} {extra}</div>
      <div class="tbl-wrap">
        <table>
          <thead><tr>
            <th data-num="1"><button onclick="sortTable(this)"># ↕</button></th>
            <th><button onclick="sortTable(this)">代號 ↕</button></th>
            <th><button onclick="sortTable(this)">名稱 ↕</button></th>
            <th class="r" data-num="1"><button onclick="sortTable(this)">買進(千元) ↕</button></th>
            <th class="r" data-num="1"><button onclick="sortTable(this)">賣出(千元) ↕</button></th>
            <th class="r" data-num="1"><button onclick="sortTable(this)">今日買超 ↕</button></th>
            <th class="r" data-num="1"><button onclick="sortTable(this)">5日均買超 ↕</button></th>
            <th class="r" data-num="1"><button onclick="sortTable(this)">今/均量倍率 ↕</button></th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>"""


def render_html(all_branches: list[dict], consensus: list[dict]) -> str:
    data_date = next((b["data_date"] for b in all_branches if b.get("data_date")), "")
    date_disp = f"{data_date[:4]}/{data_date[4:6]}/{data_date[6:]}" if len(data_date) == 8 else data_date
    now_tw    = datetime.utcnow().strftime("%Y/%m/%d %H:%M UTC")

    total_br  = len(all_branches)
    spike_stocks = [s for b in all_branches for s in b["stocks"] if s.get("is_spike")]
    total_buy_k  = sum(s["buy"] for b in all_branches for s in b["stocks"]) // 1000

    # Stats
    stats_html = f"""<div class="stats">
      <div class="stat"><div class="lbl">資料日期</div><div class="val" style="font-size:1.1rem">{date_disp}</div></div>
      <div class="stat"><div class="lbl">監控分點數</div><div class="val">{total_br}</div></div>
      <div class="stat"><div class="lbl">共識買進股數</div><div class="val">{len(consensus)}</div></div>
      <div class="stat"><div class="lbl">爆量個股（≥{int(SPIKE_THRESHOLD*100)}%）</div>
        <div class="val" style="color:#d93025">{len(spike_stocks)}</div></div>
      <div class="stat"><div class="lbl">各分點合計買進</div>
        <div class="val" style="font-size:1.1rem">{total_buy_k:,}M</div>
        <div class="sub">（百萬元）</div></div>
    </div>"""

    # Consensus section
    consensus_html = render_consensus_table(consensus, total_br)

    # Per-branch sections
    branch_html = "".join(render_branch_section(b) for b in all_branches)

    # Tab nav
    tab_nav = ""
    for b in all_branches:
        tab_nav += f'<button class="tab-btn" data-tab="br-{b["名稱"]}" onclick="showTab(\'br-{b["名稱"]}\')">  {b["名稱"]}</button>\n'

    branch_panels = ""
    for b in all_branches:
        branch_panels += f'<div id="br-{b["名稱"]}" class="tab-panel">{render_branch_section(b)}</div>\n'

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>券商分點大額買進監控 — {date_disp}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>📊 券商分點大額買進監控</h1>
  <div class="meta">資料日期：{date_disp}<br>更新時間：{now_tw}</div>
</header>
<div class="container">
  {stats_html}
  <div class="tabs">
    <button class="tab-btn active" data-tab="consensus" onclick="showTab('consensus')">🔗 共識買進</button>
    {tab_nav}
  </div>
  <div id="consensus" class="tab-panel active">
    <p style="color:#888;font-size:.8rem;margin-bottom:.75rem">
      同時出現在 ≥2 個分點買超清單的股票，按分點數排序。買超門檻：≥ {MIN_NET_DISPLAY:,} 千元。
    </p>
    {consensus_html}
  </div>
  {branch_panels}
</div>
<script>{JS}</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════
# 電子郵件
# ═══════════════════════════════════════════════════════════

def send_email(html: str, data_date: str):
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECIPIENT:
        print("⚠️  未設定 EMAIL_SENDER / EMAIL_PASSWORD / EMAIL_RECIPIENT，略過寄信。")
        return

    date_disp = f"{data_date[:4]}/{data_date[4:6]}/{data_date[6:]}" if len(data_date) == 8 else data_date
    subject   = f"券商分點大額買進監控 — {date_disp}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
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


# ═══════════════════════════════════════════════════════════
# 主程式
# ═══════════════════════════════════════════════════════════

def main():
    args       = sys.argv[1:]
    do_email   = "--email" in args or "--email-only" in args
    save_file  = "--email-only" not in args

    print("═" * 55)
    print("  券商分點大額買進監控系統 啟動")
    print("═" * 55)

    # 抓取各分點資料
    all_branches = []
    for br in BRANCHES:
        try:
            data = fetch_branch(br)
            all_branches.append(data)
        except Exception as e:
            print(f"  ⚠️ {br['名稱']} 抓取失敗：{e}")

    if not all_branches:
        print("❌ 所有分點資料抓取失敗，結束。")
        sys.exit(1)

    # 共識分析
    consensus = build_consensus(all_branches)
    data_date = next((b["data_date"] for b in all_branches if b.get("data_date")), "")

    print(f"\n分析完成：共識買進 {len(consensus)} 檔，資料日期 {data_date}")

    # 爆量統計
    spike_list = [(b["名稱"], s["ticker"], s["name"], s["spike"])
                  for b in all_branches
                  for s in b["stocks"]
                  if s.get("is_spike")]
    if spike_list:
        print(f"\n🔥 爆量個股（今日買超 ≥ {int(SPIKE_THRESHOLD*100)}% 均量）：")
        for br_n, tk, nm, sp in sorted(spike_list, key=lambda x: -(x[3] or 0)):
            print(f"   {br_n} | {tk} {nm} | {sp:.1f}x")

    # 產生 HTML
    html = render_html(all_branches, consensus)

    if save_file:
        out_path = os.path.join(os.path.dirname(__file__), "index.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n✅ 已儲存：{out_path}")

    if do_email:
        send_email(html, data_date)


if __name__ == "__main__":
    main()

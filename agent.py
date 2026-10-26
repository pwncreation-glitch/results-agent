"""
Quarterly results agent (free stack)
  Data   : BSE announcements API (+ optional screener.in history)
  LLM    : Google Gemini free tier
  Output : Telegram bot

Usage:
  python agent.py backfill --days 7     # analyse results already declared
  python agent.py run                   # check once for new filings
  python agent.py run --loop            # keep checking every 10 min
"""
import os, sys, time, sqlite3, argparse, datetime as dt
import requests
try:
    sys.stdout.reconfigure(line_buffering=True)  # show logs live in GitHub Actions
except Exception:
    pass
try:
    import pymupdf as fitz
except ImportError:
    import fitz
import json, io, re, html
from bs4 import BeautifulSoup
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except Exception:
    PIL_OK = False

# ---------- config (set as environment variables) ----------
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()  # leave empty = auto-pick a current Flash model
WATCHLIST = [x.strip() for x in os.environ.get("WATCHLIST", "").split(",") if x.strip()]  # BSE scrip codes, empty = all
def envnum(name, default):
    v = os.environ.get(name, "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        return default

MAX_PER_RUN = int(envnum("MAX_PER_RUN", 25))  # filings analysed per run (protects free-tier limits)
# Quality filter: only results meeting ALL four thresholds are posted
FILTER_ON = os.environ.get("FILTER_ON", "1").strip() != "0"           # set FILTER_ON=0 to post every result
MIN_REV_YOY = envnum("MIN_REV_YOY", 50)    # revenue growth % vs same quarter last year
MIN_REV_QOQ = envnum("MIN_REV_QOQ", 50)    # revenue growth % vs previous quarter
MIN_PAT_YOY = envnum("MIN_PAT_YOY", 40)    # net profit growth % vs same quarter last year
MIN_PAT_QOQ = envnum("MIN_PAT_QOQ", 40)    # net profit growth % vs previous quarter
ALLOW_TURNAROUND = os.environ.get("ALLOW_TURNAROUND", "1").strip() != "0"  # loss -> profit counts as passing
SLEEP_BETWEEN = 8  # seconds between LLM calls
MAX_CHARS = 60000  # PDF text sent to the LLM
RUN_BUDGET = int(os.environ.get("RUN_BUDGET_SECONDS", "540"))  # stop starting new filings after this many seconds

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*",
      "Accept-Language": "en-US,en;q=0.9",
      "Referer": "https://www.bseindia.com/",
      "Origin": "https://www.bseindia.com"}
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
PDF_URLS = ["https://www.bseindia.com/xml-data/corpfiling/AttachLive/{}",
            "https://www.bseindia.com/xml-data/corpfiling/AttachHis/{}"]

db = sqlite3.connect("seen.db")
db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
db.execute("CREATE TABLE IF NOT EXISTS qualified (scrip TEXT PRIMARY KEY, name TEXT, day TEXT)")
sess = requests.Session()
sess.headers.update(UA)
_warmed = False

# ---------- BSE ----------
def warm_up():
    """Visit the BSE site once to pick up cookies before calling the API."""
    global _warmed
    if _warmed: return
    _warmed = True
    for url in ("https://www.bseindia.com/", "https://www.bseindia.com/corporates/ann.html"):
        try:
            r = sess.get(url, timeout=30)
            print("BSE warm-up:", url, r.status_code)
        except Exception as e:
            print("BSE warm-up error:", url, e)

ENDPOINTS = ["https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w",
             "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"]
_good_endpoint = None

def _query(endpoint, cat, d_from, d_to, page):
    params = dict(pageno=page, strCat=cat, strPrevDate=d_from, strScrip="",
                  strSearch="P", strToDate=d_to, strType="C")
    if "SubCategory" in endpoint:
        params["subcategory"] = "-1"
    try:
        r = sess.get(endpoint, params=params, timeout=30)
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            print(f"  {endpoint.split('/')[-2]} [{cat} p{page}] ->", r.status_code, r.text[:120].replace("\n", " "))
            return None
        return r.json().get("Table", []) or []
    except Exception as e:
        print("  BSE fetch error:", e)
        return None

def fetch_bse(cat, d_from, d_to):
    """Try the BSE endpoints; stick with whichever returns data."""
    global _good_endpoint
    warm_up()
    order = ([_good_endpoint] if _good_endpoint else []) + [e for e in ENDPOINTS if e != _good_endpoint]
    for ep in order:
        first = _query(ep, cat, d_from, d_to, 1)
        if not first:
            continue
        _good_endpoint = ep
        rows, page, data = list(first), 1, first
        while len(data) >= 50 and page < 10:
            page += 1
            data = _query(ep, cat, d_from, d_to, page) or []
            rows += data
        print(f"BSE {cat}: {len(rows)} rows via {ep.split('/')[-2]} ({d_from}-{d_to})")
        if rows:
            print("  sample fields:", {k: str(v)[:40] for k, v in list(rows[0].items())[:8]})
        return rows
    print(f"BSE {cat}: 0 rows from all endpoints ({d_from}-{d_to})")
    return []

def classify(row):
    text = f"{row.get('HEADLINE','')} {row.get('SUBCATNAME','')} {row.get('NEWSSUB','')}".lower()
    if row.get("CATEGORYNAME", "") == "Result":
        return "result"
    if "transcript" in text or "concall" in text or "earnings call" in text or "conference call" in text:
        return "concall"
    if "presentation" in text:
        return "presentation"
    return None

def get_new_items(days):
    today = dt.date.today()
    d_from = (today - dt.timedelta(days=days)).strftime("%Y%m%d")
    d_to = today.strftime("%Y%m%d")
    items = []
    for cat in ("Result", "Company Update"):
        for row in fetch_bse(cat, d_from, d_to):
            kind = classify(row)
            if not kind or not row.get("ATTACHMENTNAME"): continue
            if WATCHLIST and str(row.get("SCRIP_CD")) not in WATCHLIST: continue
            nid = str(row["NEWSID"])
            if db.execute("SELECT 1 FROM seen WHERE id=?", (nid,)).fetchone(): continue
            row["_kind"] = kind
            items.append(row)
    return items

# ---------- PDF ----------
def pdf_text(attachment):
    for tpl in PDF_URLS:
        url = tpl.format(attachment)
        try:
            r = sess.get(url, timeout=60)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                doc = fitz.open(stream=r.content, filetype="pdf")
                parts = [f"[Page {i+1}]\n{p.get_text()}" for i, p in enumerate(doc)]
                return "\n".join(parts)[:MAX_CHARS], url
        except Exception as e:
            print("PDF error:", e)
    return "", ""

# ---------- screener.in (optional comparison base) ----------
def screener_quarters(scrip_code):
    try:
        r = requests.get(f"https://www.screener.in/company/{scrip_code}/consolidated/", headers=UA, timeout=30)
        if r.status_code != 200: return ""
        sec = BeautifulSoup(r.text, "html.parser").find("section", id="quarters")
        if not sec: return ""
        return "\n".join(" | ".join(c.get_text(strip=True) for c in tr.find_all(["th", "td"]))
                         for tr in sec.select("table tr"))
    except Exception:
        return ""

# ---------- LLM ----------
class QuotaError(Exception):
    pass

_models = None
_bad = set()
_PREFER = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest"]
_SKIP = ("image", "tts", "live", "audio", "exp", "thinking", "embedding", "vision",
         "robotics", "computer", "native", "learnlm", "gemma")

def candidate_models():
    """Ordered list of models to try: GEMINI_MODEL if set, else known free-tier Flash models first."""
    global _models
    if _models is not None: return _models
    if GEMINI_MODEL:
        _models = [GEMINI_MODEL]
    else:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": GEMINI_KEY, "pageSize": 200}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Cannot list Gemini models: {r.status_code} {r.text[:200]}")
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])
                 and "flash" in m["name"] and not any(x in m["name"] for x in _SKIP)]
        print("Gemini Flash models available:", names)
        rest = sorted((n for n in names if n not in _PREFER and "preview" not in n and "omni" not in n), reverse=True)
        _models = [n for n in _PREFER if n in names] + rest
        if not _models:
            raise RuntimeError("No Gemini Flash model available for this API key")
    print("Gemini models, in order of use:", _models[:6])
    return _models

def llm(prompt, json_mode=False):
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if json_mode:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    for model in candidate_models():
        if model in _bad: continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            r = requests.post(url, params={"key": GEMINI_KEY}, timeout=150,
                              json=body)
            if r.status_code == 429:
                print(f"Gemini {model}: quota/rate limit -", r.text[:160].replace("\n", " "))
                if attempt == 0:
                    time.sleep(20); continue
                _bad.add(model); print(f"  skipping {model} for this run"); break
            if r.status_code in (403, 404):
                print(f"Gemini {model}: unavailable ({r.status_code})"); _bad.add(model); break
            if r.status_code in (500, 502, 503, 504):
                print(f"Gemini {model}: busy ({r.status_code}), trying next model"); break
            if r.status_code >= 400:
                print("Gemini error:", r.status_code, r.text[:300].replace("\n", " "))
            r.raise_for_status()
            print(f"   analysed with {model}")
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raise QuotaError("All Gemini models are out of quota, busy or unavailable")

# ---------- presentation (cards + captions) ----------
BRAND = os.environ.get("BRAND", "Investment ki Pathshala")
BRAND_LINK = os.environ.get("BRAND_LINK", "t.me/IKPathshala26")
DISCLAIMER = "AI-generated summary. Verify numbers with the filing. Not investment advice."

PROMPT_RESULT_JSON = """You are extracting data from an Indian company's quarterly results filing. Return ONLY a JSON object (no markdown) with exactly these keys:
- "quarter_label": quarter the results are for, Indian financial year Apr-Mar, like "Q2 FY27"
- "periods": three labels [current quarter, previous quarter, same quarter last year], like ["Sep 2026","Jun 2026","Sep 2025"]
- "basis": "Consolidated" or "Standalone" (prefer consolidated)
- "unit": the unit used for amounts in the results table: "crore", "lakh", "million", "thousand" or "rupees"
- "sales_label": "Revenue" (use "Total income" or "NII" for banks, NBFCs, insurers)
- "ebitda_label": "EBITDA" (use "PPOP" for banks/NBFCs) or null if not applicable
- "sales", "ebitda", "pat", "eps": each a list of three numbers [current, previous quarter, same quarter last year]. pat = net profit after tax (attributable to owners if shown)
- "highlights": up to 3 short factual points (segments, order book, volumes, launches, capex, dividends), each under 110 characters
- "watch": one short factual line on one-offs, exceptional items, high other income, auditor remarks or other flags, or "" if none
Rules: copy "sales", "ebitda" and "pat" EXACTLY as printed in the results table, in that table's own unit, with every decimal digit kept: do NOT round, do NOT convert units, no thousands separators. EPS in Rs as printed. If EBITDA is not stated, compute it as profit before tax + finance costs + depreciation - other income. Use null for any number missing from the filing; only if a whole period is missing from the filing may you take it from the screener history below (those values are in Rs crore, so convert them into the filing's unit first). Losses are negative numbers. Facts only: no opinions, ratings or recommendations."""

PROMPT_TEXT = {
 "result": """You are an equity research analyst. Summarise this Indian company's quarterly results filing.
Prefer CONSOLIDATED numbers; state units (Rs crore). If a number is not in the filing, write n/a. No opinions or ratings.
Plain text only (no markdown, no asterisks), max 1300 characters, in this format:
📈 Revenue: x (YoY %, QoQ %)
💰 EBITDA / margin: x
🧾 PAT: x (YoY %, QoQ %)
🧮 EPS: x
✨ Highlights:
• up to 3 short points
⚠️ Watch-points: one-offs, exceptional items, other income, auditor remarks""",
 "presentation": """You are an equity research analyst. Summarise this investor presentation. Facts only, no opinions or ratings.
Plain text only (no markdown, no asterisks), max 1300 characters. Use these sections, each starting with its emoji and short title, with "• " bullets, skipping sections with nothing to say:
📌 KEY NUMBERS
🎯 GUIDANCE & OUTLOOK (exact targets if given)
🏗️ CAPEX, ORDERS & LAUNCHES
✅ POSITIVES
⚠️ CONCERNS""",
 "concall": """You are an equity research analyst. Summarise this earnings call transcript. Facts only, no ratings or recommendations.
Plain text only (no markdown, no asterisks), max 1300 characters. Use these sections, each starting with its emoji and short title, with "• " bullets:
🎙️ TONE (one line: confident / cautious / defensive, and why)
🎯 GUIDANCE & OUTLOOK (growth, margin, capex specifics)
✅ POSITIVES
⚠️ RED FLAGS / EVASIVE ANSWERS
❓ KEY Q&A (what analysts asked, what management said)""",
}

def parse_json(t):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip())
    return json.loads(t)

def num(x):
    try:
        return None if x is None else float(x)
    except Exception:
        return None

def vec(d, key):
    v = d.get(key)
    v = list(v) if isinstance(v, (list, tuple)) else []
    return ([num(x) for x in v] + [None, None, None])[:3]

def fmt(x, eps=False):
    if x is None: return "–"
    if eps: return f"{x:,.2f}"
    a = abs(x)
    return f"{x:,.0f}" if a >= 100 else (f"{x:,.1f}" if a >= 10 else f"{x:,.2f}")

def chg(cur, base):
    """-> (text, direction) direction: 1 good, -1 bad, 0 neutral"""
    if cur is None or base is None: return ("–", 0)
    if base <= 0 and cur > 0: return ("Turnaround", 1)
    if base > 0 and cur < 0: return ("To loss", -1)
    if base < 0 and cur <= 0:
        if cur == base: return ("–", 0)
        return ("Loss narrowed", 1) if cur > base else ("Loss widened", -1)
    if base == 0: return ("–", 0)
    p = (cur - base) / abs(base) * 100
    t = f"{p:+,.0f}%" if abs(p) >= 10 else f"{p:+.1f}%"
    return (t, 1 if p > 0 else (-1 if p < 0 else 0))

def bps(cur, base):
    if cur is None or base is None: return ("–", 0)
    x = cur - base
    return (f"{x:+.1f} pp", 1 if x > 0.05 else (-1 if x < -0.05 else 0))

def margins_of(ebitda, sales):
    return [(e / s * 100) if (e is not None and s is not None and s > 0) else None for e, s in zip(ebitda, sales)]

def display_name(name):
    n = re.sub(r"\s*-\s*\$\s*$", "", name or "")
    n = re.sub(r"\b(ltd|limited)\b\.?\s*$", "", n.strip(), flags=re.I).strip()
    return n or (name or "")

def make_tag(name):
    n = re.sub(r"[^A-Za-z0-9 ]", " ", display_name(name))
    return "#" + "".join(w.capitalize() if w.islower() or w.isupper() else w for w in n.split())[:28]

def unit_factor(u):
    """Multiplier that converts the filing's unit into Rs crore."""
    u = str(u or "").lower()
    if "lakh" in u or "lac" in u: return 0.01
    if "million" in u: return 0.1
    if "thousand" in u: return 0.0001
    if "rupee" in u or u in ("inr", "rs"): return 1e-7
    return 1.0  # crore or unknown

def build_rows(d):
    f = unit_factor(d.get("unit"))
    sales, ebitda, pat = ([None if x is None else x * f for x in vec(d, k)] for k in ("sales", "ebitda", "pat"))
    eps = vec(d, "eps")
    rows = []
    def add(label, v, is_eps=False):
        if all(x is None for x in v): return
        rows.append(dict(label=label, yoy=chg(v[0], v[2]), qoq=chg(v[0], v[1]), vals=[fmt(x, is_eps) for x in v]))
    add(d.get("sales_label") or "Revenue", sales)
    mg = [None, None, None]
    if d.get("ebitda_label"):
        add(d["ebitda_label"], ebitda)
        mg = margins_of(ebitda, sales)
        if mg[0] is not None:
            rows.append(dict(label=f"{d['ebitda_label']} margin", yoy=bps(mg[0], mg[2]), qoq=bps(mg[0], mg[1]),
                             vals=[f"{m:.1f}%" if m is not None else "–" for m in mg]))
    add("Net profit", pat)
    add("EPS (₹)", eps, True)
    return rows, mg, (sales, ebitda, pat, eps)

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/", "C:/Windows/Fonts/", "/Library/Fonts/", ""]
def font(size, bold=False):
    for d in FONT_DIRS:
        for n in (("DejaVuSans-Bold.ttf", "arialbd.ttf") if bold else ("DejaVuSans.ttf", "arial.ttf")):
            try:
                return ImageFont.truetype(d + n, size)
            except Exception:
                pass
    return ImageFont.load_default()

def rtext(dr, xr, y, text, f, fill):
    dr.text((xr - dr.textlength(text, font=f), y), text, font=f, fill=fill)

def render_card(name, quarter, basis, periods, rows):
    W, H_HEAD, H_COL, H_ROW, H_FOOT = 1080, 190, 70, 92, 100
    H = H_HEAD + H_COL + H_ROW * len(rows) + H_FOOT
    NAVY, GOLD, INK, MUTE, GREEN, RED = "#0F172A", "#F59E0B", "#0F172A", "#64748B", "#15803D", "#DC2626"
    img = Image.new("RGB", (W, H), "#F1F5F9")
    dr = ImageDraw.Draw(img)
    dr.rectangle([0, 0, W, H_HEAD], fill=NAVY)
    dr.rectangle([0, H_HEAD - 8, W, H_HEAD], fill=GOLD)
    size = 50
    while size > 28 and dr.textlength(name, font=font(size, True)) > W - 80:
        size -= 2
    dr.text((40, 36), name, font=font(size, True), fill="white")
    dr.text((40, 108), f"{quarter} RESULTS  •  {basis}  •  ₹ crore", font=font(26), fill="#CBD5E1")
    xs = {"yoy": 470, "qoq": 625, "v0": 790, "v1": 925, "v2": 1055}
    y = H_HEAD
    dr.rectangle([0, y, W, y + H_COL], fill="#E2E8F0")
    heads = {"yoy": "YoY", "qoq": "QoQ", "v0": periods[0], "v1": periods[1], "v2": periods[2]}
    for k, x in xs.items():
        rtext(dr, x, y + 24, heads[k], font(20, True), MUTE)
    y += H_COL
    for i, r in enumerate(rows):
        dr.rectangle([0, y, W, y + H_ROW], fill="white" if i % 2 == 0 else "#F8FAFC")
        dr.text((40, y + 29), r["label"], font=font(29, True), fill=INK)
        for key in ("yoy", "qoq"):
            t, dn = r[key]
            col = GREEN if dn > 0 else (RED if dn < 0 else MUTE)
            rtext(dr, xs[key], y + 31, ("▲ " if dn > 0 else "▼ " if dn < 0 else "") + t, font(25, True), col)
        for j, key in enumerate(("v0", "v1", "v2")):
            rtext(dr, xs[key], y + 29, r["vals"][j], font(29, j == 0), INK if j == 0 else MUTE)
        y += H_ROW
    dr.rectangle([0, y, W, H], fill=NAVY)
    dr.text((40, y + 24), BRAND, font=font(30, True), fill=GOLD)
    dr.text((40, y + 64), "Source: BSE filing  •  AI-generated, verify before acting", font=font(20), fill="#94A3B8")
    rtext(dr, W - 40, y + 32, BRAND_LINK, font(24), "#CBD5E1")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()

def esc(t):
    return html.escape(str(t), quote=False)

def mood(pat):
    cur, yq = pat[0], pat[2]
    if cur is None: return "📊"
    if cur < 0: return "⚠️"
    if yq is not None and yq > 0 and cur / yq - 1 >= 0.5: return "🔥"
    if yq is not None and cur > yq: return "📈"
    if yq is not None and cur < yq: return "📉"
    return "📊"

def pair(v):
    yt, qt = chg(v[0], v[2])[0], chg(v[0], v[1])[0]
    parts = ([f"{yt} YoY"] if yt != "–" else []) + ([f"{qt} QoQ"] if qt != "–" else [])
    return f"  ({' · '.join(parts)})" if parts else ""

def result_caption(name, d, mg, series, url, full=True):
    sales, ebitda, pat, eps = series
    q = d.get("quarter_label") or "Quarterly"
    tag = make_tag(name)
    head = f"{mood(pat)} <b>{esc(tag)} {esc(q)} RESULTS</b>"
    if not full: return head
    L = [head, f"<i>{esc(d.get('basis') or 'Consolidated')} • ₹ crore</i>", ""]
    if sales[0] is not None:
        L.append(f"📈 <b>{esc(d.get('sales_label') or 'Revenue')}:</b> ₹{fmt(sales[0])} Cr{pair(sales)}")
    if d.get("ebitda_label") and ebitda[0] is not None:
        L.append(f"💰 <b>{esc(d['ebitda_label'])}:</b> ₹{fmt(ebitda[0])} Cr{pair(ebitda)}")
    if pat[0] is not None:
        L.append(f"🧾 <b>PAT:</b> ₹{fmt(pat[0])} Cr{pair(pat)}")
    if eps[0] is not None:
        L.append(f"🧮 <b>EPS:</b> ₹{fmt(eps[0], True)}")
    if d.get("ebitda_label") and mg[0] is not None:
        vs = f" vs {mg[2]:.1f}% last year" if mg[2] is not None else ""
        L.append(f"📐 <b>Margin:</b> {mg[0]:.1f}%{vs}")
    hl = [h for h in (d.get("highlights") or []) if str(h).strip()][:3]
    if hl:
        L += ["", "✨ <b>Highlights</b>"] + [f"• {esc(h)}" for h in hl]
    if str(d.get("watch") or "").strip():
        L += ["", f"⚠️ <b>Watch:</b> {esc(d['watch'])}"]
    L += ["", f'🔗 <a href="{esc(url)}">BSE filing</a>', f"<i>{DISCLAIMER}</i>",
          f"#Result #{re.sub(r'[^A-Za-z0-9]', '', q)}"]
    return "\n".join(L)

# ---------- quality filter ----------
def growth(cur, base, allow_turnaround=False):
    if cur is None or base is None: return None
    if base > 0: return (cur / base - 1) * 100
    if allow_turnaround and cur > 0: return float("inf")   # loss/zero -> profit
    return None

def _g(g):
    if g is None: return "n/a"
    return "turnaround" if g == float("inf") else f"{g:+.0f}%"

def passes_filter(sales, pat):
    checks = [("Rev YoY", growth(sales[0], sales[2]), MIN_REV_YOY),
              ("Rev QoQ", growth(sales[0], sales[1]), MIN_REV_QOQ),
              ("PAT YoY", growth(pat[0], pat[2], ALLOW_TURNAROUND), MIN_PAT_YOY),
              ("PAT QoQ", growth(pat[0], pat[1], ALLOW_TURNAROUND), MIN_PAT_QOQ)]
    desc = ", ".join(f"{n} {_g(g)}" for n, g, _ in checks)
    ok = all(g is not None and g >= m for _, g, m in checks)
    return ok, desc

def mark_qualified(row):
    db.execute("INSERT OR REPLACE INTO qualified VALUES (?,?,?)",
               (str(row.get("SCRIP_CD")), row.get("SLONGNAME"), dt.date.today().isoformat()))
    db.commit()

def is_qualified(scrip):
    return db.execute("SELECT 1 FROM qualified WHERE scrip=?", (str(scrip),)).fetchone() is not None

# ---------- Telegram ----------
def send_tg(text, html_mode=True):
    for i in range(0, len(text), 4000):
        data = {"chat_id": TG_CHAT, "text": text[i:i+4000], "disable_web_page_preview": True}
        if html_mode: data["parse_mode"] = "HTML"
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=30)
        if r.status_code >= 400: print("Telegram error:", r.status_code, r.text[:200])
        r.raise_for_status()

def send_photo(png, caption):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                      data={"chat_id": TG_CHAT, "caption": caption, "parse_mode": "HTML"},
                      files={"photo": ("result.png", png, "image/png")}, timeout=60)
    if r.status_code >= 400: print("Telegram photo error:", r.status_code, r.text[:200])
    r.raise_for_status()

def send_result(row, d, url):
    name = row.get("SLONGNAME") or ""
    rows, mg, series = build_rows(d)
    if not rows or (series[0][0] is None and series[2][0] is None):
        raise ValueError("no usable numbers extracted")
    if FILTER_ON:
        ok, desc = passes_filter(series[0], series[2])
        if not ok:
            print(f"   filtered out: {desc}")
            return "skip"
        print(f"   QUALIFIED: {desc}")
        mark_qualified(row)
    caption = result_caption(name, d, mg, series, url)
    png = None
    if PIL_OK:
        try:
            periods = (list(d.get("periods") or []) + ["", "", ""])[:3]
            png = render_card(display_name(name), d.get("quarter_label") or "Quarterly",
                              d.get("basis") or "Consolidated", periods, rows)
        except Exception as e:
            print("   card render failed:", e)
    if png and len(caption) <= 1024:
        send_photo(png, caption)
    elif png:
        send_photo(png, result_caption(name, d, mg, series, url, full=False))
        send_tg(caption)
    else:
        send_tg(caption)
    return True

# ---------- pipeline ----------
def process(row):
    kind = row["_kind"]
    text, url = pdf_text(row["ATTACHMENTNAME"])
    if not text.strip():
        print("no text for", row.get("SLONGNAME")); return False
    name = row.get("SLONGNAME") or ""
    ctx = f"\n\nCompany: {name}\nFiling: {row.get('HEADLINE')}\n\nFILING TEXT:\n{text}"
    if kind == "result":
        extra = "\n\nSCREENER.IN QUARTERLY HISTORY:\n" + screener_quarters(row["SCRIP_CD"])
        d = None
        try:
            d = parse_json(llm(PROMPT_RESULT_JSON + ctx + extra, json_mode=True))
            if not isinstance(d, dict): d = None
        except QuotaError:
            raise
        except Exception as e:
            print("   structured extraction failed:", e)
        if d is None and FILTER_ON:
            print("   cannot verify growth numbers - skipped")
            return "skip"
        if d is not None:
            try:
                return send_result(row, d, url)
            except ValueError as e:
                print("   ", e)
                if FILTER_ON:
                    return "skip"
                print("   using text summary")
        analysis = llm(PROMPT_TEXT["result"] + ctx + extra)
        head, tags = f"📊 <b>{esc(make_tag(name))} RESULTS</b>", "#Result"
    else:
        analysis = llm(PROMPT_TEXT[kind] + ctx)
        if kind == "presentation":
            head, tags = f"📑 <b>{esc(make_tag(name))} INVESTOR PRESENTATION</b>", "#InvestorPresentation"
        else:
            head, tags = f"🎙️ <b>{esc(make_tag(name))} EARNINGS CALL</b>", "#Concall"
    msg = (f"{head}\n\n{esc(analysis.strip())}\n\n"
           f'🔗 <a href="{esc(url)}">BSE filing</a>\n<i>{DISCLAIMER}</i>\n{tags}')
    send_tg(msg)
    return True

def run_once(days):
    start = time.time()
    items = get_new_items(days)
    if FILTER_ON:   # presentations/concalls only for companies whose result passed the filter
        items = [r for r in items if r["_kind"] == "result" or is_qualified(r.get("SCRIP_CD"))]
    print(f"{len(items)} filings to check; analysing up to {MAX_PER_RUN}")
    done = skipped = 0
    for row in items[:MAX_PER_RUN]:
        if time.time() - start > RUN_BUDGET:
            print("Time budget reached; remaining filings will be picked up next run.")
            break
        name = row.get("SLONGNAME")
        print(f"-> {row['_kind']}: {name}")
        try:
            res = process(row)
            if res:
                db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (str(row["NEWSID"]),)); db.commit()
                if res == "skip":
                    skipped += 1
                else:
                    done += 1
                    print("   sent to Telegram")
        except QuotaError as e:
            print("Stopping this run:", e, "- will retry on the next run.")
            break
        except Exception as e:
            print("failed:", name, e)
        time.sleep(SLEEP_BETWEEN)
    print(f"Done: {done} posted, {skipped} filtered out this run.")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backfill", "run"])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--loop", action="store_true")
    a = ap.parse_args()
    if not (TG_TOKEN and TG_CHAT and GEMINI_KEY):
        sys.exit("Set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY first.")
    days = a.days if a.mode == "backfill" else 2
    while True:
        run_once(days)
        if not a.loop: break
        time.sleep(600)

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
import requests, fitz  # fitz = PyMuPDF
from bs4 import BeautifulSoup

# ---------- config (set as environment variables) ----------
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")  # check AI Studio for current free models
WATCHLIST = [x.strip() for x in os.environ.get("WATCHLIST", "").split(",") if x.strip()]  # BSE scrip codes, e.g. 500325,532540. Empty = all
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "15"))  # protects free-tier limits
SLEEP_BETWEEN = 8  # seconds between LLM calls (free tier is rate limited)
MAX_CHARS = 60000  # PDF text sent to the LLM

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
PDF_URLS = ["https://www.bseindia.com/xml-data/corpfiling/AttachLive/{}",
            "https://www.bseindia.com/xml-data/corpfiling/AttachHis/{}"]

db = sqlite3.connect("seen.db")
db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
sess = requests.Session()
sess.headers.update(UA)

# ---------- BSE ----------
def fetch_bse(cat, d_from, d_to):
    rows, page = [], 1
    try:
        sess.get("https://www.bseindia.com/corporates/ann.html", timeout=30)  # pick up cookies
    except Exception as e:
        print("BSE warm-up error:", e)
    while page <= 10:
        params = dict(strCat=cat, strPrevDate=d_from, strScrip="", strSearch="P",
                      strToDate=d_to, strType="C", pageno=page)
        try:
            r = sess.get(BSE_API, params=params, timeout=30)
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                print("BSE bad response:", r.status_code, r.text[:200]); break
            data = r.json().get("Table", [])
        except Exception as e:
            print("BSE fetch error:", e); break
        if not data: break
        rows += data
        if len(data) < 50: break
        page += 1
    return rows

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
def llm(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    for i in range(4):
        r = requests.post(url, params={"key": GEMINI_KEY}, timeout=180,
                          json={"contents": [{"parts": [{"text": prompt}]}]})
        if r.status_code == 429:
            time.sleep(30 * (i + 1)); continue
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raise RuntimeError("LLM rate limit not clearing")

PROMPTS = {
 "result": """You are an equity research analyst. Analyse this Indian company's quarterly results filing.
Prefer CONSOLIDATED numbers; state units (Rs crore). Use the screener history only for YoY/QoQ comparison; if a number is not in the filing, say "n/a".
Plain text only, no markdown, max 1500 characters. Format:
RESULT SNAPSHOT
Revenue: x (YoY %, QoQ %)
EBITDA / margin: x (change in bps)
PAT: x (YoY %, QoQ %)
EPS: x
Highlights: 2-3 bullets (segments, volumes, order book)
One-offs/exceptional items: ...
Flags: anything unusual (auditor remarks, provisions, tax, other income driving profit)
Quick take: Strong / Decent / Weak, with one line why.""",
 "presentation": """You are an equity research analyst. Summarise this investor presentation.
Plain text only, no markdown, max 1500 characters. Format:
Key numbers and segment trends
Guidance / outlook (exact targets if given)
Capex, order book, new launches, debt
Strategy changes vs earlier
Biggest positive, biggest concern.""",
 "concall": """You are an equity research analyst. Analyse this earnings call transcript.
Plain text only, no markdown, max 1500 characters. Format:
Management tone: confident / cautious / defensive (one line why)
Guidance and outlook: specifics (growth, margin, capex)
Positives: 2-3 bullets
Red flags / evasive answers: 2-3 bullets
Key analyst questions and what management actually said.""",
}

# ---------- Telegram ----------
def send_tg(text):
    for i in range(0, len(text), 4000):
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": text[i:i+4000], "disable_web_page_preview": True}, timeout=30)
        r.raise_for_status()

# ---------- pipeline ----------
def process(row):
    kind = row["_kind"]
    text, url = pdf_text(row["ATTACHMENTNAME"])
    if not text.strip():
        print("no text for", row.get("SLONGNAME")); return False
    extra = ""
    if kind == "result":
        extra = "\n\nSCREENER.IN QUARTERLY HISTORY:\n" + screener_quarters(row["SCRIP_CD"])
    prompt = f"{PROMPTS[kind]}\n\nCompany: {row.get('SLONGNAME')}\nFiling: {row.get('HEADLINE')}\n\nFILING TEXT:\n{text}{extra}"
    analysis = llm(prompt)
    label = {"result": "RESULT", "presentation": "PRESENTATION", "concall": "CONCALL"}[kind]
    msg = (f"{label}: {row.get('SLONGNAME')} ({row.get('SCRIP_CD')})\n"
           f"{row.get('NEWS_DT','')}\n\n{analysis.strip()}\n\nSource: {url}\n"
           f"(AI-generated, verify numbers. Not investment advice.)")
    send_tg(msg)
    return True

def run_once(days):
    items = get_new_items(days)
    print(f"{len(items)} new filings; processing up to {MAX_PER_RUN}")
    for row in items[:MAX_PER_RUN]:
        try:
            if process(row):
                db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (str(row["NEWSID"]),)); db.commit()
        except Exception as e:
            print("failed:", row.get("SLONGNAME"), e)
        time.sleep(SLEEP_BETWEEN)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backfill", "run"])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--loop", action="store_true")
    a = ap.parse_args()
    if not (TG_TOKEN and TG_CHAT and GEMINI_KEY):
        sys.exit("Set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY first.")
    if a.mode == "backfill":
        run_once(a.days)
    else:
        while True:
            run_once(1)
            if not a.loop: break
            time.sleep(600)

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
from bs4 import BeautifulSoup

# ---------- config (set as environment variables) ----------
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()  # leave empty = auto-pick a current Flash model
WATCHLIST = [x.strip() for x in os.environ.get("WATCHLIST", "").split(",") if x.strip()]  # BSE scrip codes, empty = all
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "15"))  # protects free-tier limits
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

def llm(prompt):
    for model in candidate_models():
        if model in _bad: continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            r = requests.post(url, params={"key": GEMINI_KEY}, timeout=150,
                              json={"contents": [{"parts": [{"text": prompt}]}]})
            if r.status_code == 429:
                print(f"Gemini {model}: quota/rate limit -", r.text[:160].replace("\n", " "))
                if attempt == 0:
                    time.sleep(20); continue
                _bad.add(model); print(f"  skipping {model} for this run"); break
            if r.status_code in (403, 404):
                print(f"Gemini {model}: unavailable ({r.status_code})"); _bad.add(model); break
            if r.status_code >= 400:
                print("Gemini error:", r.status_code, r.text[:300].replace("\n", " "))
            r.raise_for_status()
            print(f"   analysed with {model}")
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raise QuotaError("All Gemini models are out of quota or unavailable")

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
    start = time.time()
    items = get_new_items(days)
    print(f"{len(items)} new filings; processing up to {MAX_PER_RUN}")
    done = 0
    for row in items[:MAX_PER_RUN]:
        if time.time() - start > RUN_BUDGET:
            print("Time budget reached; remaining filings will be picked up next run.")
            break
        name = row.get("SLONGNAME")
        print(f"-> {row['_kind']}: {name}")
        try:
            if process(row):
                db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (str(row["NEWSID"]),)); db.commit()
                done += 1
                print("   sent to Telegram")
        except QuotaError as e:
            print("Stopping this run:", e, "- will retry on the next run.")
            break
        except Exception as e:
            print("failed:", name, e)
        time.sleep(SLEEP_BETWEEN)
    print(f"Done: {done} sent this run.")

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

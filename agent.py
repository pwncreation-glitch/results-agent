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

MAX_PER_RUN = int(envnum("MAX_PER_RUN", 1000))  # filings analysed per run (protects free-tier limits)

# Quality filter
FILTER_ON = False  # Growth filter permanently disabled: send all eligible filings           # set FILTER_ON=0 to post every result
# FILTER_MODE decides how the four thresholds below are combined:
#   yoy    = revenue YoY >= MIN_REV_YOY  AND  net profit YoY >= MIN_PAT_YOY   (QoQ ignored)  <- default
#   either = (revenue YoY OR QoQ passes)  AND  (net profit YoY OR QoQ passes)
#   both   = all four checks must pass (strict)
FILTER_MODE = os.environ.get("FILTER_MODE", "yoy").strip().lower()
MIN_REV_YOY = envnum("MIN_REV_YOY", 20)    # revenue growth % vs same quarter last year
MIN_REV_QOQ = envnum("MIN_REV_QOQ", 20)    # revenue growth % vs previous quarter
MIN_PAT_YOY = envnum("MIN_PAT_YOY", 20)    # net profit growth % vs same quarter last year
MIN_PAT_QOQ = envnum("MIN_PAT_QOQ", 20)    # net profit growth % vs previous quarter
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

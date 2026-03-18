"""
scrape.py  —  Fetches the latest CFTC Cotton On-Call report and appends new
              rows to data/cotton_oncall.csv.  Runs automatically every Thursday
              via GitHub Actions; also safe to run manually anytime.

CSV columns match the Excel export exactly:
  Week #, Report #, Report Date, Futures Based On,
  Unfixed Call Sales, Chg Sales, Unfixed Call Purchases, Chg Purchases,
  At Close, Chg At Close, Yr, Month, Old/New
"""

import requests, re, os, sys, time
from bs4 import BeautifulSoup
from datetime import date, datetime, timedelta
import csv

HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; CottonOnCallBot/1.0)"}
BASE      = "https://www.cftc.gov"
BASE_PATH = "/MarketReports/CottonOnCall/HistoricalCottonOn-Call/"
CSV_PATH  = os.path.join(os.path.dirname(__file__), "data", "cotton_oncall.csv")

MONTH_MAP = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12
}

# Must match the Excel export column names exactly
CSV_COLS = [
    "Week #", "Report #", "Report Date", "Futures Based On",
    "Unfixed Call Sales", "Chg Sales",
    "Unfixed Call Purchases", "Chg Purchases",
    "At Close", "Chg At Close", "Yr", "Month", "Old/New"
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_old_new(cy, cm, ry, report_month):
    """
    Jan-Jun: same year non-Dec=old, same year Dec=new1, higher=new n
    Jul-Dec: same year all months + next year non-Dec = old
             Dec of next year = new1 (cotton crop starts with Dec)
             Then new n increments per Dec boundary
    """
    if report_month <= 6:
        if cy == ry and cm != 12:
            return "old"
        n = cy - ry + (1 if cm == 12 else 0)
        if n <= 0: return "old"
        return f"new{n}"
    else:
        if cy == ry:
            return "old"
        if cy == ry + 1 and cm != 12:
            return "old"
        n = (cy - ry - 1) + (1 if cm == 12 else 0)
        return f"new{n}"

def to_int(s):
    try:
        return int(str(s).replace(",","").replace(" ","").replace("+","").strip())
    except:
        return 0

def parse_report(html):
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    report_date_str, release_year, week_num, report_num = "", None, None, None

    if tables:
        header_text = tables[0].get_text()
        m = re.search(r"as of (\d{1,2}/\d{1,2}/\d{4})", header_text)
        if m:
            report_date_str = m.group(1)
            mo, dy, yr = map(int, report_date_str.split("/"))
            release_year = yr
            try:
                week_num = date(yr, mo, dy).isocalendar()[1]
            except Exception:
                week_num = None
        rn = re.search(r"Weekly Report\s+(\d+)", html)
        if rn:
            report_num = int(rn.group(1))

    rows_out = []
    for table in tables:
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 6:
                continue
            label = cells[0].strip()
            year_m = re.search(r"\b(20\d{2})\b", label)
            mon_m  = re.search(
                r"(january|february|march|april|may|june|july|august|"
                r"september|october|november|december)", label.lower()
            )
            is_total = "otal" in label
            if not (year_m or is_total):
                continue

            s  = to_int(cells[1]) if len(cells) > 1 else 0
            cs = to_int(cells[2]) if len(cells) > 2 else 0
            p  = to_int(cells[3]) if len(cells) > 3 else 0
            cp = to_int(cells[4]) if len(cells) > 4 else 0
            cl = to_int(cells[5]) if len(cells) > 5 else 0
            cc = to_int(cells[6]) if len(cells) > 6 else 0

            if is_total:
                rows_out.append({
                    "Week #": week_num, "Report #": report_num,
                    "Report Date": report_date_str, "Futures Based On": "Totals",
                    "Unfixed Call Sales": s, "Chg Sales": cs,
                    "Unfixed Call Purchases": p, "Chg Purchases": cp,
                    "At Close": cl, "Chg At Close": cc,
                    "Yr": "", "Month": "", "Old/New": "total",
                    "_release_year": release_year,
                })
            elif year_m and mon_m:
                cy = int(year_m.group(1))
                cm = MONTH_MAP[mon_m.group(1)]
                rows_out.append({
                    "Week #": week_num, "Report #": report_num,
                    "Report Date": report_date_str, "Futures Based On": label.strip(),
                    "Unfixed Call Sales": s, "Chg Sales": cs,
                    "Unfixed Call Purchases": p, "Chg Purchases": cp,
                    "At Close": cl, "Chg At Close": cc,
                    "Yr": cy, "Month": cm,
                    "Old/New": get_old_new(cy, cm, release_year, mo) if release_year and mo else "",
                    "_release_year": release_year,
                })
    return rows_out


def get_candidate_urls():
    print("Fetching CFTC index page...")
    r = requests.get(BASE + BASE_PATH + "index.htm", headers=HEADERS, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")

    seen, urls = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "deaoncal" not in href.lower():
            continue
        if href.startswith("http"):
            full = href
        elif href.startswith("/"):
            full = BASE + href
        else:
            full = BASE + BASE_PATH + href
        full = full.split("#")[0]
        if not full.lower().endswith(".html"):
            full += ".html"
        if full not in seen:
            seen.add(full)
            urls.append(full)

    known_2026 = [
        "deaoncall010226.html","deaoncall010826.html","deaoncall011526.html",
        "deaoncall012226.html","deaoncall012926.html","deaoncall020526.html",
        "deaoncall021226.html","deaoncall021926.html","deaoncall022626.html",
        "deaoncall030526.html","deaoncall030626.html","deaoncall031226.html","deaoncall031926.html",
        "deaoncall032626.html","deaoncall040226.html","deaoncall040926.html",
        "deaoncall041626.html","deaoncall042326.html","deaoncall043026.html",
        "deaoncall050726.html","deaoncall051426.html","deaoncall052126.html",
        "deaoncall052826.html","deaoncall060426.html","deaoncall061126.html",
        "deaoncall061826.html","deaoncall062526.html","deaoncall070226.html",
        "deaoncall070926.html","deaoncall071626.html","deaoncall072326.html",
        "deaoncall073026.html","deaoncall080626.html","deaoncall081326.html",
        "deaoncall082026.html","deaoncall082726.html","deaoncall090326.html",
        "deaoncall091026.html","deaoncall091726.html","deaoncall092426.html",
        "deaoncall100126.html","deaoncall100826.html","deaoncall101526.html",
        "deaoncall102226.html","deaoncall102926.html","deaoncall110526.html",
        "deaoncall111226.html","deaoncall111926.html","deaoncall112726.html",
        "deaoncall120326.html","deaoncall121026.html","deaoncall121726.html",
        "deaoncall122426.html","deaoncall123126.html",
    ]
    for fn in known_2026:
        u = BASE + BASE_PATH + fn
        if u not in seen:
            urls.append(u)

    print(f"Found {len(urls)} total candidate URLs")
    return urls


def read_existing_dates(csv_path):
    existing = set()
    if not os.path.exists(csv_path):
        return existing
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row.get("Report Date", "").strip()
            if d:
                existing.add(d)
    return existing


def append_rows(csv_path, new_rows):
    existing_rows = []
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_rows.append(dict(row))

    for r in new_rows:
        clean = {k: v for k, v in r.items() if not k.startswith("_")}
        existing_rows.append(clean)

    def sort_key(r):
        try:
            return datetime.strptime(r.get("Report Date", ""), "%m/%d/%Y")
        except Exception:
            return datetime.min

    existing_rows.sort(key=sort_key)

    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(existing_rows)

    print(f"CSV now has {len(existing_rows)} rows")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    existing_dates = read_existing_dates(CSV_PATH)
    print(f"Existing dates in CSV: {len(existing_dates)}")

    # Only check URLs from the last 60 days — no need to re-scan all 500 history
    cutoff = datetime.now() - timedelta(days=60)

    all_urls = get_candidate_urls()
    recent_urls = []
    for url in all_urls:
        fn = url.split("/")[-1].replace(".html", "")
        digits = re.sub(r"[^0-9]", "", fn)
        if len(digits) == 6:        # MMDDYY format
            try:
                d = datetime.strptime(digits, "%m%d%y")
                if d >= cutoff:
                    recent_urls.append(url)
            except:
                pass
        elif len(digits) == 8:      # MMDDYYYY format
            try:
                d = datetime.strptime(digits, "%m%d%Y")
                if d >= cutoff:
                    recent_urls.append(url)
            except:
                pass

    print(f"Checking {len(recent_urls)} recent URLs (last 60 days)")

    new_rows = []
    reports_added = 0

    for url in recent_urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200 or "Unfixed" not in r.text:
                continue
            rows = parse_report(r.text)
            if not rows:
                continue
            rdate_str = rows[0]["Report Date"]
            if rdate_str in existing_dates:
                print(f"⏭️  Already have {rdate_str} — skipping")
                continue
            new_rows.extend(rows)
            existing_dates.add(rdate_str)
            reports_added += 1
            print(f"✅  NEW: {url.split('/')[-1]}  →  {rdate_str}  ({len(rows)} rows)")
        except Exception as e:
            print(f"⚠️   {url.split('/')[-1]}: {e}")
        time.sleep(0.3)

    print(f"\nNew reports found: {reports_added}  |  New rows: {len(new_rows)}")

    if new_rows:
        append_rows(CSV_PATH, new_rows)
        print("✅ CSV updated successfully")
    else:
        print("Already up to date — nothing to add")
        sys.exit(0)


if __name__ == "__main__":
    main()

"""
scrape.py  —  Fetches latest CFTC Cotton On-Call report, appends new rows to
              data/cotton_oncall.csv, generates a PDF summary and emails it.

Runs automatically via GitHub Actions every Thursday (both EDT and EST timings).
Retries every 5 minutes for up to 35 minutes if report not yet published.
Only sends email when new data is found.
"""

import requests, re, os, sys, time, csv, smtplib, io
from bs4 import BeautifulSoup
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

HEADERS         = {"User-Agent": "Mozilla/5.0 (compatible; CottonOnCallBot/1.0)"}
HEADERS_NOCACHE = {**HEADERS, "Cache-Control": "no-cache, no-store, must-revalidate",
                   "Pragma": "no-cache", "Expires": "0"}

BASE      = "https://www.cftc.gov"
BASE_PATH = "/MarketReports/CottonOnCall/HistoricalCottonOn-Call/"
CSV_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cotton_oncall.csv")

MONTH_MAP = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12
}
MONTH_MAP_ABBR = {k[:3]:v for k,v in MONTH_MAP.items()}

CSV_COLS = [
    "Week #","Report #","Report Date","Futures Based On",
    "Unfixed Call Sales","Chg Sales","Unfixed Call Purchases","Chg Purchases",
    "At Close","Chg At Close","Yr","Month","Old/New","Report Year"
]

MAX_RETRIES    = 7     # 7 attempts × 5 min = 35 min window
RETRY_INTERVAL = 300   # 5 minutes

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_old_new(cy, cm, ry, report_month):
    if report_month <= 6:
        if cy == ry and cm != 12: return "old"
        n = cy - ry + (1 if cm == 12 else 0)
        if n <= 0: return "old"
        return f"new{n}"
    else:
        if cy == ry: return "old"
        if cy == ry + 1 and cm != 12: return "old"
        n = (cy - ry - 1) + (1 if cm == 12 else 0)
        return f"new{n}"

def to_int(s):
    try: return int(str(s).replace(",","").replace(" ","").replace("+","").strip())
    except: return 0

def parse_date_from_text(text):
    """Try all known date formats, return (date_str MM/DD/YYYY, mo, dy, yr) or None."""
    # Format A: as of MM/DD/YYYY
    m = re.search(r"as of\s+(\d{1,2}/\d{1,2}/\d{4})", text)
    if m:
        s = m.group(1)
        mo, dy, yr = map(int, s.split("/"))
        return s, mo, dy, yr

    # Format B: as of Month DD, YYYY  (full or abbreviated)
    m = re.search(
        r"as of\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})",
        text, re.IGNORECASE)
    if m:
        mo = MONTH_MAP_ABBR[m.group(1).lower()[:3]]
        dy, yr = int(m.group(2)), int(m.group(3))
        return f"{mo:02d}/{dy:02d}/{yr}", mo, dy, yr

    # Format C: header "Weekly Report N – Month DD, YYYY"
    m = re.search(
        r"Weekly Report[^-\n]*[-\u2013]\s*(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
        r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})",
        text, re.IGNORECASE)
    if m:
        mo = MONTH_MAP_ABBR[m.group(1).lower()[:3]]
        dy, yr = int(m.group(2)), int(m.group(3))
        return f"{mo:02d}/{dy:02d}/{yr}", mo, dy, yr

    return None

def parse_report(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    result = parse_date_from_text(text)
    if not result: return []
    report_date_str, release_month, dy, release_year = result

    try: week_num = date(release_year, release_month, dy).isocalendar()[1]
    except: week_num = None

    rn = re.search(r"Weekly Report\s+(\d+)", text, re.IGNORECASE)
    report_num = int(rn.group(1)) if rn else None

    rows_out = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td","th"])]
            if len(cells) < 6: continue
            label = cells[0].strip()
            year_m = re.search(r"\b(20\d{2})\b", label)
            mon_m  = re.search(
                r"(january|february|march|april|may|june|july|august|"
                r"september|october|november|december)", label.lower())
            is_total = bool(re.search(r"^\s*total", label, re.IGNORECASE))
            if not (year_m or is_total): continue
            if re.search(r"unfixed|futures based|call cotton|change from|open futures", label.lower()): continue

            s =to_int(cells[1]) if len(cells)>1 else 0
            cs=to_int(cells[2]) if len(cells)>2 else 0
            p =to_int(cells[3]) if len(cells)>3 else 0
            cp=to_int(cells[4]) if len(cells)>4 else 0
            cl=to_int(cells[5]) if len(cells)>5 else 0
            cc=to_int(cells[6]) if len(cells)>6 else 0

            if is_total and not year_m:
                rows_out.append({
                    "Week #":week_num,"Report #":report_num,
                    "Report Date":report_date_str,"Futures Based On":"Totals",
                    "Unfixed Call Sales":s,"Chg Sales":cs,
                    "Unfixed Call Purchases":p,"Chg Purchases":cp,
                    "At Close":cl,"Chg At Close":cc,
                    "Yr":"","Month":"","Old/New":"total",
                    "Report Year":str(release_year) if release_year else "",
                    "_release_year":release_year,
                })
            elif year_m and mon_m:
                cy = int(year_m.group(1))
                cm = MONTH_MAP[mon_m.group(1)]
                rows_out.append({
                    "Week #":week_num,"Report #":report_num,
                    "Report Date":report_date_str,"Futures Based On":label.strip(),
                    "Unfixed Call Sales":s,"Chg Sales":cs,
                    "Unfixed Call Purchases":p,"Chg Purchases":cp,
                    "At Close":cl,"Chg At Close":cc,
                    "Yr":cy,"Month":cm,
                    "Old/New":get_old_new(cy,cm,release_year,release_month) if release_year else "",
                    "Report Year":str(release_year) if release_year else "",
                    "_release_year":release_year,
                })
    return rows_out

# ── URL helpers ───────────────────────────────────────────────────────────────

def get_candidate_urls():
    print("Fetching CFTC index page...")
    try:
        r = requests.get(BASE + BASE_PATH + "index.htm", headers=HEADERS, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        seen, urls = set(), []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "deaoncal" not in href.lower(): continue
            if href.startswith("http"): full = href
            elif href.startswith("/"): full = BASE + href
            else: full = BASE + BASE_PATH + href
            full = full.split("#")[0]
            if not full.lower().endswith(".html"): full += ".html"
            if full not in seen:
                seen.add(full); urls.append(full)
    except Exception as e:
        print(f"⚠️  Index page error: {e}")
        urls, seen = [], set()

    known_2026 = [
        "deaoncall010226.html","deaoncall010826.html","deaoncall011526.html",
        "deaoncall012226.html","deaoncall012926.html","deaoncall020526.html",
        "deaoncall021226.html","deaoncall021926.html","deaoncall022626.html",
        "deaoncall030526.html","deaoncall030626.html","deaoncall031226.html",
        "deaoncall031926.html","deaoncall032626.html","deaoncall040226.html",
        "deaoncall040926.html","deaoncall041626.html","deaoncall042326.html",
        "deaoncall043026.html","deaoncall050726.html","deaoncall051426.html",
        "deaoncall052126.html","deaoncall052826.html","deaoncall060426.html",
        "deaoncall061126.html","deaoncall061826.html","deaoncall062526.html",
        "deaoncall070226.html","deaoncall070926.html","deaoncall071626.html",
        "deaoncall072326.html","deaoncall073026.html","deaoncall080626.html",
        "deaoncall081326.html","deaoncall082026.html","deaoncall082726.html",
        "deaoncall090326.html","deaoncall091026.html","deaoncall091726.html",
        "deaoncall092426.html","deaoncall100126.html","deaoncall100826.html",
        "deaoncall101526.html","deaoncall102226.html","deaoncall102926.html",
        "deaoncall110526.html","deaoncall111226.html","deaoncall111926.html",
        "deaoncall112626.html","deaoncall120326.html","deaoncall121026.html",
        "deaoncall121726.html","deaoncall122426.html","deaoncall123126.html",
    ]
    for fn in known_2026:
        u = BASE + BASE_PATH + fn
        if u not in seen: urls.append(u)

    print(f"Found {len(urls)} total candidate URLs")
    return urls

# ── CSV helpers ───────────────────────────────────────────────────────────────

def read_existing_dates(csv_path):
    existing = set()
    if not os.path.exists(csv_path): return existing
    for enc in ("utf-8-sig","utf-8","latin-1"):
        try:
            with open(csv_path, newline="", encoding=enc) as f:
                for row in csv.DictReader(f):
                    d = row.get("Report Date","").strip()
                    if d: existing.add(d)
            if existing: break
        except: pass
    print(f"Found {len(existing)} existing report dates in CSV")
    return existing

def read_all_rows(csv_path):
    if not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}"); return [], 0
    fsize = os.path.getsize(csv_path)
    print(f"CSV size: {fsize} bytes")
    if fsize < 100:
        print("⚠️  CSV too small — aborting"); sys.exit(1)
    for enc in ("utf-8-sig","utf-8","latin-1"):
        try:
            with open(csv_path, newline="", encoding=enc) as f:
                rows = [dict(r) for r in csv.DictReader(f)]
            if rows:
                print(f"Read {len(rows)} rows (encoding: {enc})")
                return rows, len(rows)
        except Exception as e:
            print(f"  {enc}: {e}")
    print("⚠️  Could not read CSV — aborting"); sys.exit(1)

def append_rows(csv_path, new_rows):
    existing_rows, rows_before = read_all_rows(csv_path)
    if os.path.exists(csv_path) and rows_before == 0:
        print("⚠️  SAFETY ABORT: file exists but 0 rows"); sys.exit(1)

    for r in new_rows:
        clean = {k:v for k,v in r.items() if not k.startswith("_")}
        if not clean.get("Report Year") and clean.get("Report Date","").count("/")==2:
            clean["Report Year"] = clean["Report Date"].split("/")[2]
        existing_rows.append(clean)

    existing_rows.sort(key=lambda r: (
        datetime.strptime(r.get("Report Date",""), "%m/%d/%Y")
        if r.get("Report Date","") else datetime.min))

    if len(existing_rows) < rows_before:
        print(f"⚠️  SAFETY ABORT: would shrink {rows_before}→{len(existing_rows)}"); sys.exit(1)

    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    tmp = csv_path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader(); w.writerows(existing_rows)
    os.replace(tmp, csv_path)
    print(f"✅ CSV saved: {rows_before} → {len(existing_rows)} rows (+{len(existing_rows)-rows_before})")

# ── PDF generation ────────────────────────────────────────────────────────────

def generate_pdf(new_rows, all_rows_for_charts):
    """Generate PDF: page 1 = current week table + old crop charts,
                     page 2 = all crop + new crop charts."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        from fpdf import FPDF
    except ImportError as e:
        print(f"⚠️  PDF library missing ({e}) — skipping PDF"); return None

    if not new_rows: return None
    report_date = new_rows[0].get("Report Date","")

    # ── Build data structure ──────────────────────────────────────────────────
    # Only data rows (no totals)
    data_rows = [r for r in new_rows if r.get("Old/New","") != "total"]
    total_rows = [r for r in new_rows if r.get("Old/New","") == "total"]

    # ── Chart data from full history ──────────────────────────────────────────
    DATA = {}
    for r in all_rows_for_charts:
        yr = r.get("Report Year","").strip()
        if not yr: yr = r.get("Report Date","").split("/")[2] if "/" in r.get("Report Date","") else ""
        try: wk = int(round(float(r.get("Week #",0))))
        except: continue
        on = r.get("Old/New","")
        if not yr or not wk or on == "total": continue
        try: mon = int(float(r.get("Month",0)))
        except: mon = 0
        try: p = int(r.get("Unfixed Call Purchases",0))
        except: p = 0
        try: s = int(r.get("Unfixed Call Sales",0))
        except: s = 0
        if yr not in DATA: DATA[yr] = {}
        if wk not in DATA[yr]: DATA[yr][wk] = {}
        if mon not in DATA[yr][wk]: DATA[yr][wk][mon] = {"oP":0,"oS":0,"aP":0,"aS":0}
        DATA[yr][wk][mon]["aP"] += p; DATA[yr][wk][mon]["aS"] += s
        if on == "old": DATA[yr][wk][mon]["oP"] += p; DATA[yr][wk][mon]["oS"] += s

    all_years = sorted(DATA.keys())
    max_wk = max((max(wks.keys()) for wks in DATA.values() if wks), default=52)
    weeks = list(range(1, max_wk+1))
    CM = [3,5,7,10,12]  # default all months

    def get_val(ci, yr, wk):
        crop = "old" if ci<3 else ("all" if ci<6 else "new")
        typ = ci % 3
        if yr not in DATA or wk not in DATA[yr]: return None
        slot = DATA[yr][wk]
        aP = sum(slot[m]["aP"] for m in CM if m in slot)
        aS = sum(slot[m]["aS"] for m in CM if m in slot)
        oP = sum(slot[m]["oP"] for m in CM if m in slot)
        oS = sum(slot[m]["oS"] for m in CM if m in slot)
        if not any(m in slot for m in CM): return None
        if crop == "old":
            return [oP, oS, oS-oP][typ]
        if crop == "all":
            return [aP, aS, aS-aP][typ]
        return [(aP-oP),(aS-oS),(aS-oS)-(aP-oP)][typ]

    COLORS = ['#1a6b3c','#c0392b','#2e86c1','#8e44ad','#d35400',
              '#16a085','#f39c12','#1a3a5c','#27ae60','#e74c3c']
    TITLES = ['Old Crop – Purchases','Old Crop – Sales','Old Crop – Imbalance',
              'All Crop – Purchases','All Crop – Sales','All Crop – Imbalance',
              'New Crop – Purchases','New Crop – Sales','New Crop – Imbalance']

    def make_chart_image(ci, w_in, h_in):
        fig, ax = plt.subplots(figsize=(w_in, h_in), dpi=120)
        fig.patch.set_facecolor('white')
        cur_yr = datetime.now().year
        def_years = [y for y in all_years if cur_yr-4 <= int(y) <= cur_yr and int(y) > 2005]
        years20   = all_years[-20:]

        # max/min over 20 years
        maxV, minV = [], []
        for wk in weeks:
            vals = [get_val(ci,y,wk) for y in years20]
            vals = [v for v in vals if v is not None]
            maxV.append(max(vals) if vals else None)
            minV.append(min(vals) if vals else None)

        ax.plot(weeks, maxV, color='#aaa', linewidth=0.8, linestyle='--', label='20yr Max')
        ax.plot(weeks, minV, color='#ccc', linewidth=0.8, linestyle='--', label='20yr Min')

        for yi, yr in enumerate(def_years):
            vals = [get_val(ci,yr,wk) for wk in weeks]
            ax.plot(weeks, vals, color=COLORS[yi%len(COLORS)], linewidth=1.2, label=yr)

        ax.set_title(TITLES[ci], fontsize=7, fontweight='bold', color='#1a3a5c', pad=3)
        ax.tick_params(labelsize=5)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f'{int(x):,}' if x==int(x) else ''))
        ax.grid(axis='y', color='#f0f0f0', linewidth=0.5)
        ax.spines['top'].set_visible(False); ax.spines['right'].set_visible(False)
        ax.legend(fontsize=4.5, loc='upper right', framealpha=0.6, ncol=2)
        plt.tight_layout(pad=0.3)
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=120, bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    # ── Build PDF ─────────────────────────────────────────────────────────────
    pdf = FPDF(orientation='L', unit='mm', format='A4')
    # A4 landscape: 297 × 210 mm
    W, H = 297, 210
    MARGIN = 8

    # ── PAGE 1: table + first 3 charts ───────────────────────────────────────
    pdf.add_page()
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    usable_w = W - 2*MARGIN  # 281mm

    # Header
    pdf.set_fill_color(26, 58, 92)
    pdf.set_text_color(255,255,255)
    pdf.rect(MARGIN, MARGIN, usable_w, 8, 'F')
    pdf.set_font('Helvetica','B',9)
    pdf.set_xy(MARGIN+2, MARGIN+1)
    pdf.cell(usable_w-4, 6, f'CFTC Cotton On-Call Report — {report_date}', ln=0)
    pdf.set_text_color(0,0,0)

    # Table
    pdf.set_xy(MARGIN, MARGIN+10)
    headers = ['Futures Based On','Sales','Chg','Purchases','Chg','At Close','Chg']
    col_w   = [50, 22, 18, 26, 18, 26, 18]  # total=178mm

    pdf.set_font('Helvetica','B',6.5)
    pdf.set_fill_color(26,58,92); pdf.set_text_color(255,255,255)
    for i,h in enumerate(headers):
        align = 'L' if i==0 else 'R'
        pdf.cell(col_w[i],5,h,border=0,ln=0,align=align,fill=True)
    pdf.ln()
    pdf.set_text_color(0,0,0)

    def fmt(v):
        try:
            n=int(v)
            return f'({abs(n):,})' if n<0 else f'{n:,}'
        except: return str(v) if v else '--'

    sorted_dr = sorted(data_rows, key=lambda r:(
        0 if r.get("Old/New")=="old" else 1,
        float(r.get("Yr",0) or 0),
        float(r.get("Month",0) or 0)))

    for i,r in enumerate(sorted_dr):
        pdf.set_font('Helvetica','',6)
        bg = (255,252,220) if r.get("Old/New")=="old" else (246,255,243)
        pdf.set_fill_color(*bg)
        pdf.cell(col_w[0],4,str(r.get("Futures Based On","")),border=0,ln=0,align='L',fill=True)
        for val,cw in [(r.get("Unfixed Call Sales"),col_w[1]),
                       (r.get("Chg Sales"),col_w[2]),
                       (r.get("Unfixed Call Purchases"),col_w[3]),
                       (r.get("Chg Purchases"),col_w[4]),
                       (r.get("At Close"),col_w[5]),
                       (r.get("Chg At Close"),col_w[6])]:
            pdf.cell(cw,4,fmt(val),border=0,ln=0,align='R',fill=True)
        pdf.ln()

    # Totals row
    if total_rows:
        tr = total_rows[0]
        pdf.set_font('Helvetica','B',6.5)
        pdf.set_fill_color(220,235,251)
        pdf.cell(col_w[0],4.5,'Totals',border=0,ln=0,align='L',fill=True)
        for val,cw in [(tr.get("Unfixed Call Sales"),col_w[1]),
                       (tr.get("Chg Sales"),col_w[2]),
                       (tr.get("Unfixed Call Purchases"),col_w[3]),
                       (tr.get("Chg Purchases"),col_w[4]),
                       (tr.get("At Close"),col_w[5]),
                       (tr.get("Chg At Close"),col_w[6])]:
            pdf.cell(cw,4.5,fmt(val),border=0,ln=0,align='R',fill=True)
        pdf.ln()

    # Charts row (ci 0,1,2 = Old Crop)
    chart_y = pdf.get_y() + 4
    chart_h = H - chart_y - MARGIN - 4
    chart_h = min(chart_h, 80)
    chart_w = usable_w / 3

    for idx, ci in enumerate([0,1,2]):
        img = make_chart_image(ci, chart_w/25.4, chart_h/25.4)
        tmp = f'/tmp/chart_{ci}.png'
        with open(tmp,'wb') as f: f.write(img)
        pdf.image(tmp, x=MARGIN+idx*chart_w, y=chart_y, w=chart_w-1, h=chart_h)

    # ── PAGE 2: remaining 6 charts ────────────────────────────────────────────
    pdf.add_page()
    pdf.set_fill_color(26,58,92); pdf.set_text_color(255,255,255)
    pdf.rect(MARGIN, MARGIN, usable_w, 8, 'F')
    pdf.set_font('Helvetica','B',9)
    pdf.set_xy(MARGIN+2, MARGIN+1)
    pdf.cell(usable_w-4, 6, f'CFTC Cotton On-Call — Historical Charts — {report_date}', ln=0)
    pdf.set_text_color(0,0,0)

    row_h = (H - 2*MARGIN - 12) / 2 - 2
    row_h = min(row_h, 88)

    for row in range(2):
        for col in range(3):
            ci = 3 + row*3 + col
            img = make_chart_image(ci, chart_w/25.4, row_h/25.4)
            tmp = f'/tmp/chart_{ci}.png'
            with open(tmp,'wb') as f: f.write(img)
            x = MARGIN + col * chart_w
            y = MARGIN + 10 + row * (row_h + 4)
            pdf.image(tmp, x=x, y=y, w=chart_w-1, h=row_h)

    return pdf.output()

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(pdf_bytes, report_date):
    smtp_host = os.environ.get('SMTP_HOST','')
    smtp_port = int(os.environ.get('SMTP_PORT', 587))
    smtp_user = os.environ.get('SMTP_USER','')
    smtp_pass = os.environ.get('SMTP_PASS','')
    email_from= os.environ.get('EMAIL_FROM','')
    email_to  = os.environ.get('EMAIL_TO','')

    if not all([smtp_host, smtp_user, smtp_pass, email_from, email_to]):
        print("⚠️  Email env vars not set — skipping email"); return

    recipients = [e.strip() for e in email_to.split(',') if e.strip()]
    fname = f"cotton_oncall_{report_date.replace('/','_')}.pdf"

    msg = MIMEMultipart()
    msg['From']    = email_from
    msg['To']      = ', '.join(recipients)
    msg['Subject'] = f"Cotton On-Call Report — {report_date}"
    msg.attach(MIMEText(
        f"Please find attached the CFTC Cotton On-Call report for {report_date}.\n\n"
        f"Dashboard: https://your-github-pages-url/", 'plain'))

    part = MIMEBase('application','pdf')
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f'attachment; filename="{fname}"')
    msg.attach(part)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as srv:
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.sendmail(email_from, recipients, msg.as_string())
        print(f"✅ Email sent to {', '.join(recipients)}")
    except Exception as e:
        print(f"⚠️  Email failed: {e}")

# ── Core scrape logic ─────────────────────────────────────────────────────────

def check_for_new_reports(existing_dates, new_rows):
    """Check live page + archive. Returns number of new reports found."""
    found = 0

    # Step 1: live main page
    live_url = f"https://www.cftc.gov/MarketReports/CottonOnCall/index.htm?_={int(time.time())}"
    print(f"Checking live page...")
    try:
        r = requests.get(live_url, headers=HEADERS_NOCACHE, timeout=15)
        if r.status_code == 200 and "Unfixed" in r.text:
            rows = parse_report(r.text)
            if rows:
                rdate = rows[0]["Report Date"]
                if rdate not in existing_dates:
                    new_rows.extend(rows)
                    existing_dates.add(rdate)
                    found += 1
                    print(f"✅ LIVE PAGE: {rdate} ({len(rows)} rows)")
                else:
                    print(f"⏭️  Live page already in CSV: {rdate}")
    except Exception as e:
        print(f"⚠️  Live page: {e}")

    # Step 2: archive last 60 days
    cutoff = datetime.now() - timedelta(days=60)
    all_urls = get_candidate_urls()
    recent = []
    for url in all_urls:
        fn = url.split("/")[-1].replace(".html","")
        digits = re.sub(r"[^0-9]","",fn)
        for fmt, dlen in [("%m%d%y",6),("%m%d%Y",8)]:
            if len(digits) == dlen:
                try:
                    if datetime.strptime(digits, fmt) >= cutoff:
                        recent.append(url); break
                except: pass

    print(f"Checking {len(recent)} recent archive URLs")
    for url in recent:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200 or "Unfixed" not in r.text: continue
            rows = parse_report(r.text)
            if not rows: continue
            rdate = rows[0]["Report Date"]
            if rdate in existing_dates:
                print(f"⏭️  Already have {rdate}"); continue
            new_rows.extend(rows)
            existing_dates.add(rdate)
            found += 1
            print(f"✅ NEW: {url.split('/')[-1]} → {rdate} ({len(rows)} rows)")
        except Exception as e:
            print(f"⚠️  {url.split('/')[-1]}: {e}")
        time.sleep(0.3)

    return found

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    existing_dates = read_existing_dates(CSV_PATH)
    new_rows = []
    found_total = 0

    for attempt in range(MAX_RETRIES):
        print(f"\n--- Attempt {attempt+1}/{MAX_RETRIES} ---")
        found = check_for_new_reports(existing_dates, new_rows)
        found_total += found
        if found_total > 0:
            print(f"✅ New data found on attempt {attempt+1}")
            break
        if attempt < MAX_RETRIES - 1:
            print(f"No new report yet — waiting 5 minutes before retry...")
            time.sleep(RETRY_INTERVAL)

    print(f"\nTotal new reports: {found_total} | New rows: {len(new_rows)}")

    if not new_rows:
        print("Nothing to add — exiting")
        sys.exit(0)

    # Save CSV
    append_rows(CSV_PATH, new_rows)

    # Generate PDF and send email
    print("Generating PDF...")
    all_rows, _ = read_all_rows(CSV_PATH)
    pdf_bytes = generate_pdf(new_rows, all_rows)
    if pdf_bytes:
        report_date = new_rows[0].get("Report Date","unknown")
        send_email(pdf_bytes, report_date)
    else:
        print("PDF generation skipped or failed")

    print("✅ Done")

if __name__ == "__main__":
    main()

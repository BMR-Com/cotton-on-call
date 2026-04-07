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
    """
    A4 Landscape (297x210mm), margins 8mm, usable 281x194mm (y:8-202)
    PAGE 1: header(8) | summary(17-53) | table(54-124) | old-charts(132-202)
    PAGE 2: header(8) | all-charts(22-105) | new-charts(111-202)
    All Y positions are CONSTANTS — never shift regardless of content length.
    """
    try:
        import matplotlib, io
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from fpdf import FPDF
    except ImportError as e:
        print(f"PDF lib missing ({e})"); return None

    if not new_rows: return None
    report_date = new_rows[0].get("Report Date","")

    # ── Constants ─────────────────────────────────────────────────────────────
    M    = 8       # margin mm
    W    = 297     # A4 landscape width
    H    = 210     # A4 landscape height
    UW   = W-2*M   # 281mm usable width
    CW   = (UW-4)/3  # 92.3mm per chart (2mm gaps x2)

    # Page 1 fixed Y positions
    Y_HDR   = M          # 8
    Y_SUM   = 17         # summary section start
    Y_TBL   = 54         # table start
    Y_CLBL1 = 127        # "OLD CROP" label
    Y_CH1   = 132        # old crop charts start
    CH1_H   = 70         # old crop chart height → ends at 202 ✓

    # Page 2 fixed Y positions
    Y_CLBL2 = 17         # "ALL CROP" label
    Y_CH2   = 22         # all crop charts start
    CH2_H   = 83         # → ends at 105
    Y_CLBL3 = 106        # "NEW CROP" label
    Y_CH3   = 111        # new crop charts start
    CH3_H   = 91         # → ends at 202 ✓

    TBL_ROW_H = 3.8
    MAX_TBL_ROWS = 16    # caps table at 16 rows so it never overflows Y_CLBL1

    CM = [3,5,7,10,12]

    # ── Build DATA ────────────────────────────────────────────────────────────
    DATA = {}
    for r in all_rows_for_charts:
        yr = r.get("Report Year","").strip()
        if not yr and "/" in r.get("Report Date",""):
            yr = r["Report Date"].split("/")[2]
        try: wk = int(round(float(r.get("Week #",0))))
        except: continue
        on = r.get("Old/New","")
        if not yr or not wk or on=="total": continue
        try: mon = int(float(r.get("Month",0)))
        except: mon=0
        try: p = int(r.get("Unfixed Call Purchases",0) or 0)
        except: p=0
        try: s = int(r.get("Unfixed Call Sales",0) or 0)
        except: s=0
        if yr not in DATA: DATA[yr]={}
        if wk not in DATA[yr]: DATA[yr][wk]={}
        if mon not in DATA[yr][wk]: DATA[yr][wk][mon]={"oP":0,"oS":0,"aP":0,"aS":0}
        DATA[yr][wk][mon]["aP"]+=p; DATA[yr][wk][mon]["aS"]+=s
        if on=="old": DATA[yr][wk][mon]["oP"]+=p; DATA[yr][wk][mon]["oS"]+=s

    all_years=sorted(DATA.keys())
    max_wk=max((max(wks.keys()) for wks in DATA.values() if wks),default=52)
    weeks=list(range(1,max_wk+1))
    years20=all_years[-20:]

    def slot_get(yr,wk):
        if yr not in DATA or wk not in DATA[yr]: return None
        slot=DATA[yr][wk]
        if not any(m in slot for m in CM): return None
        aP=sum(slot[m]["aP"] for m in CM if m in slot)
        aS=sum(slot[m]["aS"] for m in CM if m in slot)
        oP=sum(slot[m]["oP"] for m in CM if m in slot)
        oS=sum(slot[m]["oS"] for m in CM if m in slot)
        return {"aP":aP,"aS":aS,"oP":oP,"oS":oS,"nP":aP-oP,"nS":aS-oS}

    def get_val(ci,yr,wk):
        v=slot_get(yr,wk)
        if v is None: return None
        keys=[("oP","oS"),("aP","aS"),("nP","nS")][ci//3]
        p2,s2=v[keys[0]],v[keys[1]]
        return [p2,s2,s2-p2][ci%3]

    cur_wk=None
    for r in new_rows:
        try: cur_wk=int(round(float(r.get("Week #",0)))); break
        except: pass

    # ── Summary computation ───────────────────────────────────────────────────
    def cur_crop(rows, crop):
        s=p=cs=cp=0
        for r in rows:
            on=r.get("Old/New","")
            if on=="total": continue
            if crop=="old" and on!="old": continue
            if crop=="new" and on=="old": continue
            try: s +=int(r.get("Unfixed Call Sales",0) or 0)
            except: pass
            try: p +=int(r.get("Unfixed Call Purchases",0) or 0)
            except: pass
            try: cs+=int(r.get("Chg Sales",0) or 0)
            except: pass
            try: cp+=int(r.get("Chg Purchases",0) or 0)
            except: pass
        return s,p,s-p,cs,cp,cs-cp

    def hist(ci_idx, wk):
        vals=[]
        for yr in years20:
            v=get_val(ci_idx,yr,wk)
            if v is not None: vals.append(v)
        avg=round(sum(vals)/len(vals)) if vals else None
        mn=min(vals) if vals else None
        mx=max(vals) if vals else None
        pct=round(100*sum(1 for v in vals if v<=vals[0])/len(vals)) if vals else None
        return avg,mn,mx,vals

    SUM={}
    CI_MAP={"old":(1,0,2),"all":(4,3,5),"new":(7,6,8)}
    for crop in ["old","all","new"]:
        s,p,imb,cs,cp,ci_=cur_crop(new_rows,crop)
        si,pi,ii=CI_MAP[crop]
        savg,smn,smx,svals=hist(si,cur_wk)
        pavg,pmn,pmx,pvals=hist(pi,cur_wk)
        iavg,imn,imx,ivals=hist(ii,cur_wk)
        spct=round(100*sum(1 for v in svals if v<=s)/len(svals)) if svals else None
        ppct=round(100*sum(1 for v in pvals if v<=p)/len(pvals)) if pvals else None
        ipct=round(100*sum(1 for v in ivals if v<=imb)/len(ivals)) if ivals else None
        SUM[crop]={"s":s,"p":p,"imb":imb,"cs":cs,"cp":cp,"ci":ci_,
                   "savg":savg,"smn":smn,"smx":smx,"spct":spct,
                   "pavg":pavg,"pmn":pmn,"pmx":pmx,"ppct":ppct,
                   "iavg":iavg,"imn":imn,"imx":imx,"ipct":ipct}

    # ── Chart builder ─────────────────────────────────────────────────────────
    COLORS=['#1a6b3c','#c0392b','#2e86c1','#8e44ad','#d35400','#16a085','#f39c12','#1a3a5c']
    TITLES=['Old Crop – Purchases','Old Crop – Sales','Old Crop – Imbalance',
            'All Crop – Purchases','All Crop – Sales','All Crop – Imbalance',
            'New Crop – Purchases','New Crop – Sales','New Crop – Imbalance']
    cur_yr=datetime.now().year
    def_years=[y for y in all_years if cur_yr-4<=int(y)<=cur_yr and int(y)>2005]

    def make_chart(ci, w_mm, h_mm, dpi=120):
        fig,ax=plt.subplots(figsize=(w_mm/25.4, h_mm/25.4), dpi=dpi)
        fig.patch.set_facecolor('white')
        # 20yr band
        maxV,minV=[],[]
        for wk in weeks:
            vs=[get_val(ci,y,wk) for y in years20]
            vs=[v for v in vs if v is not None]
            maxV.append(max(vs) if vs else None)
            minV.append(min(vs) if vs else None)
        idx=[i for i in range(len(weeks)) if maxV[i] is not None]
        if idx:
            ax.fill_between([weeks[i] for i in idx],[minV[i] for i in idx],[maxV[i] for i in idx],
                            alpha=0.14,color='#4a90d9',zorder=0,label='20yr Range')
        # Year lines
        for yi,yr in enumerate(def_years):
            vals=[get_val(ci,yr,wk) for wk in weeks]
            ax.plot(weeks,vals,color=COLORS[yi%len(COLORS)],
                    linewidth=1.8 if yr==str(cur_yr) else 1.1,label=yr,zorder=2)
        ax.set_title(TITLES[ci],fontsize=6.5,fontweight='bold',color='#1a3a5c',pad=2)
        ax.tick_params(labelsize=4.5,pad=1)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(
            lambda x,_: f'{int(x/1000)}k' if abs(x)>=1000 else str(int(x))))
        ax.grid(axis='y',color='#efefef',linewidth=0.4)
        ax.spines['top'].set_visible(False); ax.spines['right'].set_visible(False)
        ax.set_xlim(1,max_wk)
        ax.legend(fontsize=4,loc='upper right',framealpha=0.6,ncol=3,
                  columnspacing=0.4,handlelength=0.8,handletextpad=0.3)
        plt.tight_layout(pad=0.15)
        buf=io.BytesIO()
        fig.savefig(buf,format='png',dpi=dpi,bbox_inches='tight',
                    facecolor='white',edgecolor='none')
        plt.close(fig); buf.seek(0)
        return buf.read()

    # Pre-render all 9 charts
    imgs={}
    for ci in range(9):
        h=CH1_H if ci<3 else (CH2_H if ci<6 else CH3_H)
        imgs[ci]=make_chart(ci,CW,h)
        tmp=f'/tmp/ch{ci}.png'
        with open(tmp,'wb') as f: f.write(imgs[ci])

    # ── PDF helpers ───────────────────────────────────────────────────────────
    def n(v):
        try:
            iv=int(v)
            return f'({abs(iv):,})' if iv<0 else f'{iv:,}'
        except: return '--' if (v is None or str(v).strip()=='') else str(v)

    def chg(v):
        try:
            iv=int(v)
            return ('+' if iv>0 else '')+f'{iv:,}'
        except: return '--'

    def pbar(p):
        if p is None: return '--'
        return f'{p}% '+('▲' if p>=70 else ('▼' if p<=30 else '●'))

    # ── Build PDF ─────────────────────────────────────────────────────────────
    pdf=FPDF(orientation='L',unit='mm',format='A4')

    def hdr_bar(txt):
        pdf.set_fill_color(26,58,92); pdf.set_text_color(255,255,255)
        pdf.rect(M,Y_HDR,UW,9,'F')
        pdf.set_font('Helvetica','B',9)
        pdf.set_xy(M+2,Y_HDR+1.5)
        pdf.cell(UW-4,6,txt,ln=0)
        pdf.set_text_color(0,0,0)

    def sec_bar(y,txt):
        pdf.set_fill_color(26,58,92); pdf.set_text_color(255,255,255)
        pdf.set_font('Helvetica','B',6.5)
        pdf.set_xy(M,y)
        pdf.cell(UW,4.5,f'  {txt}',border=0,ln=1,fill=True,align='L')
        pdf.set_text_color(0,0,0)

    # ════ PAGE 1 ════════════════════════════════════════════════════════════
    pdf.add_page()
    hdr_bar(f'CFTC Cotton On-Call Report  ·  {report_date}  ·  Week {cur_wk}')

    # ── Summary (y=17 to 53) ──────────────────────────────────────────────
    pdf.set_xy(M,Y_SUM)
    pdf.set_font('Helvetica','B',7); pdf.set_fill_color(235,242,250)
    pdf.cell(UW,5,f'Weekly Summary — Unfixed Call Positions vs 20-Year History  (Week {cur_wk})',
             border=0,ln=1,fill=True,align='C')

    SCOLS=[40,22,14,22,14,22,14,22,22,15,15]  # sum=222mm (<281 — fits with room)
    SHDRS=['','Sales','Chg','Purchases','Chg','Imbalance','Chg',
           '20yr Avg S','20yr Avg P','Pct S','Pct P']
    RH=4.2

    pdf.set_font('Helvetica','B',6); pdf.set_fill_color(205,218,232); pdf.set_text_color(30,30,30)
    pdf.set_xy(M,pdf.get_y())
    for h,w in zip(SHDRS,SCOLS):
        pdf.cell(w,RH,h,border=0,ln=0,align='C',fill=True)
    pdf.ln()

    CROP_LBL={"old":"OLD CROP","all":"ALL CROP","new":"NEW CROP (All − Old)"}
    BG={"old":(255,252,230),"all":(240,245,255),"new":(245,255,245)}
    for crop in ["old","all","new"]:
        d=SUM[crop]
        pdf.set_text_color(255,255,255); pdf.set_fill_color(40,80,130)
        pdf.set_font('Helvetica','B',6); pdf.set_xy(M,pdf.get_y())
        pdf.cell(UW,3.5,f'  {CROP_LBL[crop]}',border=0,ln=1,fill=True,align='L')
        pdf.set_text_color(0,0,0); pdf.set_fill_color(*BG[crop]); pdf.set_font('Helvetica','',6.5)
        pdf.set_xy(M,pdf.get_y())
        row_vals=['Current',n(d['s']),chg(d['cs']),n(d['p']),chg(d['cp']),
                  n(d['imb']),chg(d['ci']),
                  n(d['savg']),n(d['pavg']),pbar(d['spct']),pbar(d['ppct'])]
        for val,w in zip(row_vals,SCOLS):
            pdf.cell(w,RH,str(val),border=0,ln=0,align='R' if val!=row_vals[0] else 'L',fill=True)
        pdf.ln()
        # 20yr range sub-row
        pdf.set_font('Helvetica','',5.5); pdf.set_fill_color(250,252,255)
        pdf.set_xy(M,pdf.get_y())
        rng_vals=['20yr Range',
                  n(d['smn'])+'–'+n(d['smx']),'',
                  n(d['pmn'])+'–'+n(d['pmx']),'',
                  n(d['imn'])+'–'+n(d['imx']),'','','','','']
        for val,w in zip(rng_vals,SCOLS):
            pdf.cell(w,3.2,str(val),border=0,ln=0,align='R' if val!=rng_vals[0] else 'L',fill=True)
        pdf.ln()

    # ── Current week table (y=54, max 16 rows) ───────────────────────────
    pdf.set_xy(M,Y_TBL)
    TCOLS=[50,22,15,24,15,24,15]; THEADS=['Futures Based On','Sales','Chg','Purchases','Chg','At Close','Chg']
    pdf.set_font('Helvetica','B',6.5); pdf.set_fill_color(26,58,92); pdf.set_text_color(255,255,255)
    for h,w in zip(THEADS,TCOLS):
        pdf.cell(w,5,h,border=0,ln=0,align='L' if h==THEADS[0] else 'R',fill=True)
    pdf.ln(); pdf.set_text_color(0,0,0)

    data_rows=sorted([r for r in new_rows if r.get("Old/New","") not in ("total","")],
        key=lambda r:(0 if r.get("Old/New")=="old" else 1,
                      float(r.get("Yr",0) or 0),float(r.get("Month",0) or 0)))
    tot_rows=[r for r in new_rows if r.get("Old/New","")=="total"]

    for r in data_rows[:MAX_TBL_ROWS]:
        on=r.get("Old/New","")
        bg=(255,252,220) if on=="old" else (246,255,243)
        pdf.set_fill_color(*bg); pdf.set_font('Helvetica','',6)
        pdf.cell(TCOLS[0],TBL_ROW_H,str(r.get("Futures Based On","")),border=0,ln=0,align='L',fill=True)
        for val,cw in [(r.get("Unfixed Call Sales"),TCOLS[1]),(r.get("Chg Sales"),TCOLS[2]),
                       (r.get("Unfixed Call Purchases"),TCOLS[3]),(r.get("Chg Purchases"),TCOLS[4]),
                       (r.get("At Close"),TCOLS[5]),(r.get("Chg At Close"),TCOLS[6])]:
            pdf.cell(cw,TBL_ROW_H,n(val),border=0,ln=0,align='R',fill=True)
        pdf.ln()

    if tot_rows:
        tr=tot_rows[0]
        pdf.set_font('Helvetica','B',6.5); pdf.set_fill_color(220,235,251)
        pdf.cell(TCOLS[0],4.5,'Totals',border=0,ln=0,align='L',fill=True)
        for val,cw in [(tr.get("Unfixed Call Sales"),TCOLS[1]),(tr.get("Chg Sales"),TCOLS[2]),
                       (tr.get("Unfixed Call Purchases"),TCOLS[3]),(tr.get("Chg Purchases"),TCOLS[4]),
                       (tr.get("At Close"),TCOLS[5]),(tr.get("Chg At Close"),TCOLS[6])]:
            pdf.cell(cw,4.5,n(val),border=0,ln=0,align='R',fill=True)
        pdf.ln()

    # ── Old Crop charts — FIXED at Y=132 ─────────────────────────────────
    sec_bar(Y_CLBL1,'OLD CROP')
    for i,ci in enumerate([0,1,2]):
        pdf.image(f'/tmp/ch{ci}.png', x=M+i*(CW+2), y=Y_CH1, w=CW, h=CH1_H)

    # ════ PAGE 2 ════════════════════════════════════════════════════════════
    pdf.add_page()
    hdr_bar(f'CFTC Cotton On-Call — Historical Charts  ·  {report_date}')

    # ── All Crop charts — FIXED at Y=22 ──────────────────────────────────
    sec_bar(Y_CLBL2,'ALL CROP')
    for i,ci in enumerate([3,4,5]):
        pdf.image(f'/tmp/ch{ci}.png', x=M+i*(CW+2), y=Y_CH2, w=CW, h=CH2_H)

    # ── New Crop charts — FIXED at Y=111 ─────────────────────────────────
    sec_bar(Y_CLBL3,'NEW CROP (All minus Old)')
    for i,ci in enumerate([6,7,8]):
        pdf.image(f'/tmp/ch{ci}.png', x=M+i*(CW+2), y=Y_CH3, w=CW, h=CH3_H)

    return pdf.output()


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

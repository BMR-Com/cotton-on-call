#!/usr/bin/env python3
"""
weekly_brief.py — BMR-Com weekly on-call brief (descriptive).

Builds data/intel/brief.json from the latest weekly numbers:
  • CFTC Cotton On-Call (this repo): old-crop (current marketing year) and total books,
    front-month fixation. October futures are ignored.
  • CFTC COT for cotton (BMR-Com/cftc-cot-full): PMU, swap dealers, managed money
  • ICE cotton prices (Yahoo Finance): front and next contract
  • USDA WASDE (BMR-Com/wasde-analytics) and export sales (BMR-Com/usd-export-sales-email)
"""
import io, json, os
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "intel" / "brief.json"
RAW = "https://raw.githubusercontent.com/BMR-Com"
SRC = {"cot": f"{RAW}/cftc-cot-full/main/data/web/series/CT.json",
       "wasde": f"{RAW}/wasde-analytics/main/data/Cotton.json",
       "esr": f"{RAW}/usd-export-sales-email/main/cotton_history_1404.csv"}
LOCAL = os.getenv("INTEL_LOCAL")
LOCAL_MAP = {"cot": "cftc-cot-full/data/web/series/CT.json", "wasde": "bmr-com/wasde-analytics/data/Cotton.json",
             "esr": "bmr-com/usd-export-sales-email/cotton_history_1404.csv"}
BALES = 50000 / 480.0
MON = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                   "September", "October", "November", "December"], 1)}
YCODE = {3: "H", 5: "K", 7: "N", 12: "Z"}
NEXT = {3: 5, 5: 7, 7: 12, 12: 3}


def fetch(key):
    if LOCAL:
        return (Path(LOCAL) / LOCAL_MAP[key]).read_bytes()
    import requests
    r = requests.get(SRC[key], timeout=120); r.raise_for_status(); return r.content


def k(n):
    return "–" if n is None else (f"{n/1000:.1f}k" if abs(n) >= 1000 else f"{n:.0f}")


def sgn(n):
    return "–" if n is None else f"{int(n):+,}"


def ordn(p):
    r = int(round(p)); s = "th" if 11 <= r % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(r % 10, "th")
    return f"{r}{s}"


def i(v):
    return None if v is None or pd.isna(v) else int(round(float(v)))


def f(v, nd=2):
    return None if v is None or pd.isna(v) else round(float(v), nd)


def oncall():
    d = pd.read_csv(ROOT / "data" / "cotton_oncall.csv")
    d["date"] = pd.to_datetime(d["Report Date"])
    d["fb"] = d["Futures Based On"].astype(str).str.strip().str.replace(r"\s+", " ", regex=True)
    d = d.rename(columns={"Unfixed Call Sales": "S", "Unfixed Call Purchases": "P", "At Close": "OI"})
    c = d[(d.fb != "Totals") & ~d.fb.str.startswith("October")].copy()
    fm = c.fb.str.split(" ", expand=True)
    c["cm"] = pd.to_datetime(fm[1] + "-" + fm[0].map(MON).astype(str) + "-01", errors="coerce")
    c = c.dropna(subset=["cm"])
    old = c[c["Old/New"] == "old"].groupby("date")[["S", "P"]].sum()
    new = c[c["Old/New"].astype(str).str.startswith("new")].groupby("date")[["S", "P"]].sum()
    tot = c.groupby("date")[["S", "P"]].sum()
    w = pd.DataFrame({"S_old": old.S, "P_old": old.P, "S_new": new.S, "P_new": new.P, "S": tot.S, "P": tot.P})
    w = w.mask(w <= 0).interpolate(limit=2)   # a few historical weeks were scraped as zero
    t = w.index[-1]
    cc = c[c.date == t].copy()
    cc["fnd"] = cc.cm - pd.offsets.BDay(5)
    cc = cc[cc.fnd > t].sort_values("cm")
    return w, cc


def seasonal_pct(w, col):
    t = w.index[-1]; wk = w.index.isocalendar().week.astype(int); cw = int(wk.iloc[-1])
    ref = w[col][(w.index.year < t.year) & (w.index.year >= t.year - 5) & ((wk - cw).abs() <= 1)]
    return (ref < w[col].iloc[-1]).mean() * 100, ref.mean()


def cot():
    s = json.loads(fetch("cot")); a = s["ct"]["all"]
    df = pd.DataFrame({"pmu_l": a["prod_merc"]["l"], "pmu_s": a["prod_merc"]["s"], "sd_s": a["swap_dealers"]["s"],
                       "mm_l": a["managed_money"]["l"], "mm_s": a["managed_money"]["s"]}, index=pd.to_datetime(s["dates"])).astype(float)
    o = s["ct"].get("old") or a
    df["pmu_l_old"] = pd.to_numeric(pd.Series(o["prod_merc"]["l"], index=df.index), errors="coerce")
    df["pmu_s_old"] = pd.to_numeric(pd.Series(o["prod_merc"]["s"], index=df.index), errors="coerce")
    df["pmu_net_old"] = df.pmu_l_old - df.pmu_s_old
    df["ls_old"] = df.pmu_l_old / df.pmu_s_old
    df["mm_net"] = df.mm_l - df.mm_s
    lo, hi = df.mm_net.rolling(156, min_periods=52).min(), df.mm_net.rolling(156, min_periods=52).max()
    df["mm_idx"] = (df.mm_net - lo) / (hi - lo) * 100
    return df


def wasde(my):
    w = json.loads(fetch("wasde"))["data"]
    out = {}
    for name, key in {"w_trade": "World||Exports", "us_exp": "United States||Exports", "br_exp": "Brazil||Exports"}.items():
        vals = w.get(f"{key}||{my}") or []
        if vals:
            out[name] = vals[-1]["value"]; out["wasde_month"] = vals[-1]["reportDate"]
    return out


def esr():
    e = pd.read_csv(io.BytesIO(fetch("esr")), parse_dates=["weekEndingDate"], low_memory=False)
    for c in ["outstandingSales", "nextMYOutstandingSales"]:
        e[c] = pd.to_numeric(e[c], errors="coerce").fillna(0)
    t = e.weekEndingDate.max(); x = e[e.weekEndingDate == t]
    tot = (x.outstandingSales + x.nextMYOutstandingSales).sum()
    cn = x[x.countryName.str.upper().str.contains("CHINA") & ~x.countryName.str.upper().str.contains("TAIWAN|HONG")]
    return {"esr_week": t.strftime("%Y-%m-%d"), "us_outst_mb": tot / 1e6,
            "cn_share": (cn.outstandingSales + cn.nextMYOutstandingSales).sum() / tot if tot else None}


def spread(front, nxt):
    """Front and next contract closes from Yahoo Finance; returns (spread now, spread a week ago)."""
    try:
        import yfinance as yf
        syms = [f"CT{YCODE[front.month]}{front.year % 100:02d}.NYB", f"CT{YCODE[nxt.month]}{nxt.year % 100:02d}.NYB"]
        d = yf.download(syms, period="1mo", progress=False, auto_adjust=False, group_by="ticker")
        a, b = d[syms[0]]["Close"].dropna(), d[syms[1]]["Close"].dropna()
        s = (a - b).dropna()
        return {"now": float(s.iloc[-1]), "wk_ago": float(s.iloc[-6]) if len(s) > 5 else None,
                "front_px": float(a.iloc[-1]), "date": s.index[-1].strftime("%Y-%m-%d")}
    except Exception as e:
        print("spread unavailable:", e); return None


def main():
    w, cc = oncall()
    t = w.index[-1]; L = w.iloc[-1]; P1 = w.iloc[-2]; P8 = w.iloc[-9]
    y0 = t.year if t.month >= 8 else t.year - 1; my = f"{y0}/{str(y0 + 1)[-2:]}"
    sp_s, av_s = seasonal_pct(w, "S_old"); sp_p, av_p = seasonal_pct(w, "P_old")
    fr = cc.iloc[0] if len(cc) else None; nx = cc.iloc[1] if len(cc) > 1 else None
    wks_fnd = round((fr.fnd - t).days / 7, 1) if fr is not None else None
    CT = cot(); C = CT.iloc[-1]; Cp = CT.iloc[-2]; cot_date = CT.index[-1].strftime("%Y-%m-%d")
    ls3 = CT.ls_old.dropna().iloc[-156:]
    ls_pct = (ls3 < C.ls_old).mean() * 100 if len(ls3) > 50 else None
    W = wasde(my); E = esr()
    nxt_cm = pd.Timestamp(year=fr.cm.year + (1 if fr.cm.month == 12 else 0), month=NEXT[fr.cm.month], day=1) if fr is not None else None
    SP = spread(fr.cm, nxt_cm) if fr is not None else None

    cov_s = L.S_old / C.pmu_l; cov_p = L.P / (C.pmu_s + C.sd_s)
    pct_trade = L.S_old * BALES / 1e6 / W["w_trade"] if W.get("w_trade") else None
    X = {"oncall_date": t.strftime("%Y-%m-%d"), "cot_date": cot_date, "my": my,
         "S_old": i(L.S_old), "P_old": i(L.P_old), "P": i(L.P), "S_new": i(L.S_new), "P_new": i(L.P_new),
         "dS_old": i(L.S_old - P1.S_old), "dP_old": i(L.P_old - P1.P_old), "dS8_old": i(L.S_old - P8.S_old), "dP8": i(L.P - P8.P),
         "S_old_seas_pct": f(sp_s, 0), "P_old_seas_pct": f(sp_p, 0), "front": fr.fb if fr is not None else None,
         "S_front": i(fr.S) if fr is not None else None, "P_front": i(fr.P) if fr is not None else None,
         "OI_front": i(fr.OI) if fr is not None else None, "wks_to_fnd": wks_fnd,
         "front2": nx.fb if nx is not None else None, "S_2nd": i(nx.S) if nx is not None else None,
         "pmu_l": i(C.pmu_l), "pmu_s": i(C.pmu_s), "sd_s": i(C.sd_s), "mm_net": i(C.mm_net), "mm_chg": i(C.mm_net - Cp.mm_net),
         "mm_idx": f(C.mm_idx, 0), "Sold_pmuL": f(cov_s), "N_old": i(L.S_old - L.P_old),
         "pmu_l_old": i(C.pmu_l_old), "pmu_s_old": i(C.pmu_s_old), "pmu_net_old": i(C.pmu_net_old),
         "pmu_net_old_chg": i(C.pmu_net_old - Cp.pmu_net_old), "ls_old": f(C.ls_old, 3), "ls_old_pct": f(ls_pct, 0), "P_cov": f(cov_p), "Sold_pct_trade": f(pct_trade, 3),
         "px": f(SP["front_px"]) if SP else None, **{k_: W.get(k_) for k_ in ("w_trade", "us_exp", "br_exp", "wasde_month")},
         "us_outst_mb": f(E["us_outst_mb"]), "cn_share": f(E["cn_share"], 3), "esr_week": E["esr_week"]}

    B = [{"h": "On-call book (old crop)", "t":
          f"As of {X['oncall_date']}, old-crop ({my}) unfixed call **sales** are {k(X['S_old'])} contracts ({sgn(X['dS_old'])} w/w, "
          f"{sgn(X['dS8_old'])} over 8 weeks) and old-crop unfixed **purchases** {k(X['P_old'])} ({sgn(X['dP_old'])} w/w). "
          f"Against the same week of the last five seasons, sales are at the {ordn(sp_s)} percentile (avg {k(av_s)}) and purchases "
          f"at the {ordn(sp_p)} (avg {k(av_p)}). Beyond this season, merchants hold {k(X['S_new'])} of new-crop sales and "
          f"{k(X['P_new'])} of new-crop purchases."}]
    n_old = L.S_old - L.P_old
    if ls_pct is not None and ls_pct <= 30:
        ls_txt = ("commercials hold relatively little long cover, so fixing the mill-side book is more likely to need buying in the market"
                  if n_old > 0 else "commercials hold relatively little long cover")
    elif ls_pct is not None and ls_pct >= 70:
        ls_txt = ("commercials are unusually long — mills appear to have pre-bought futures, so much of the fixing can pass by EFP "
                  "with little fresh buying" if n_old > 0 else "commercials are unusually long relative to their shorts")
    else:
        ls_txt = "commercial cover is in its normal range"
    B.append({"h": "Mills, merchants and producers", "t":
              f"Net old-crop on-call is {sgn(n_old)} ({'mill' if n_old >= 0 else 'producer'} side). In old-crop COT, producers/merchants/"
              f"processors/users hold {k(X['pmu_l_old'])} long and {k(X['pmu_s_old'])} short (net {sgn(X['pmu_net_old'])}, "
              f"{sgn(X['pmu_net_old_chg'])} w/w). Their long/short ratio of {C.ls_old:.2f} sits at the {ordn(ls_pct)} percentile of the "
              f"last three years: **{ls_txt}**. The net and the ratio matter more than gross longs, which include calendar-spread legs "
              f"held by trade houses. On the other side, total unfixed purchases of {k(X['P'])} ({sgn(X['dP8'])} over 8 weeks) compare "
              f"with {k(X['pmu_s'])} PMU shorts and {k(X['sd_s'])} swap-dealer shorts — producer fixing is latent selling, whether it "
              f"runs through PMU hedges or swap structures."})
    if fr is not None:
        sp_txt = ""
        if SP:
            chg = f", {SP['now'] - SP['wk_ago']:+.2f}¢ on the week" if SP.get("wk_ago") is not None else ""
            sp_txt = f" The {fr.fb.split()[0][:3]}/{nx.fb.split()[0][:3] if nx is not None else 'next'} spread is {SP['now']:+.2f}¢{chg}."
        B.append({"h": "Fixation calendar", "t":
                  f"Front contract **{fr.fb}** is {wks_fnd} weeks from first notice, with {k(fr.S)} unfixed sales and {k(fr.P)} unfixed "
                  f"purchases against {k(fr.OI)} open interest." + (f" Next: {nx.fb} with {k(nx.S)} unfixed sales." if nx is not None else "") + sp_txt})
    if pct_trade:
        B.append({"h": "Trade flow", "t":
                  f"Old-crop unfixed sales of {L.S_old*BALES/1e6:.2f}m bales equal {pct_trade*100:.0f}% of USDA's {my} world trade forecast "
                  f"({W['w_trade']:.1f}m, {W.get('wasde_month')}). USDA sees Brazil exporting {W.get('br_exp', 0):.1f}m bales vs the US "
                  f"{W.get('us_exp', 0):.1f}m. US export sales (week of {E['esr_week']}): {E['us_outst_mb']:.2f}m running bales unshipped, "
                  f"China {E['cn_share']*100:.0f}%."})
    B.append({"h": "Speculators", "t":
              f"Managed money is net {'long' if C.mm_net >= 0 else 'short'} {k(abs(C.mm_net))} ({sgn(X['mm_chg'])} w/w; 3-yr COT index "
              f"{X['mm_idx']:.0f}). " + (("Spec length is elevated; with commercials light on long cover, mill fixing could still add buying, "
              "but a long spec book can also liquidate." if (ls_pct or 50) <= 30 else
              "Spec length is elevated while commercials are not short of cover, leaving less fresh buying from either side.") if C.mm_idx > 70
              else "Spec length is light, so mill buying would meet little long liquidation." if C.mm_idx < 30 else "Spec positioning is not at an extreme.")})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"generated": pd.Timestamp.now("UTC").strftime("%Y-%m-%d %H:%M UTC"), "latest": X,
                               "spread": SP, "brief": B}, separators=(",", ":"), default=str))
    for b in B:
        print(f"[{b['h']}] {b['t']}\n")


if __name__ == "__main__":
    main()

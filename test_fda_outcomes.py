"""Checks the FDA outcome classifier against real headlines. Run: python test_fda_outcomes.py"""
import sys, types
for m in ("yfinance", "newsrss"):  # not needed to classify
    sys.modules.setdefault(m, types.ModuleType(m))
import fda_outcomes as f

def h(t, p): return {"title": t, "publisher": p}

CASES = [
    # Speculation, previews and roundups: never a decision
    ([h("CAPR Stock Has Retail’s Attention After SLS Rally — Here Are 2 Drugs With Better FDA Approval Odds This Month", "Stocktwits")], "No news found"),
    ([h("SMMT: A $2.0 billion post-quarter investment funds expansion, but FDA approval hinges on OS evidence", "TradingView")], "No news found"),
    ([h("Agios Pharmaceuticals (AGIO) FDA Approvals, PDUFA Dates & Drug Alerts 2026", "MarketBeat")], "No news found"),
    ([h("Capricor Stock in Focus Ahead of FDA Decision on Duchenne Therapy Deramiocel", "Benzinga")], "No news found"),
    ([h("Will the FDA approve deramiocel?", "Seeking Alpha")], "No news found"),
    # One non-trusted source only: unconfirmed, re-checked later
    ([h("FDA approves label update for Cytokinetics’ heart drug MYQORZO", "Investing.com")], "No news found"),
    # Real decisions
    ([h("FDA rejects ivonescimab, lung cancer treatment from Summit Therapeutics | STAT", "STAT")], "CRL"),
    ([h("Capricor receives complete response letter from FDA for deramiocel", "Reuters")], "CRL"),
    ([h("FDA Extends Agios' Mitapivat Sickle Cell Disease Review; New PDUFA Date Set; Stock Down", "RTTNews")], "Extended"),
    ([h("Agios Announces New PDUFA Goal Date of February 1, 2027, for Mitapivat Sickle Cell Disease sNDA", "GlobeNewswire")], "Extended"),
    ([h("FDA Approves Neladalkib in Advanced ALK+ Non–Small Cell Lung Cancer", "CancerNetwork")], "Approved"),
    ([h("US FDA approves Sanofi's genetic disorder drug", "Yahoo News Singapore"), h("US FDA approves Sanofi’s genetic disorder drug", "SRN News")], "Approved"),
    ([h("U.S. FDA Approves IGALMI® as First At-Home Acute Treatment", "GlobeNewswire")], "Approved"),
    ([h("US FDA approves Inovio's treatment for rare throat disease", "TradingView"), h("US FDA approves Inovio's treatment for rare throat disease; shares surge", "Reuters")], "Approved"),
    ([h("FDA Approves Bepirovirsen for Chronic Hepatitis B", "HCPLive")], "Approved"),
    ([h("GSK scores FDA approval for Nelkarmi, its 2nd lung cancer drug from Nuvalent deal", "Fierce Pharma")], "Approved"),
    ([h("Johnson & Johnson wins FDA approval for Rybrevant cancer treatment", "Investing.com"), h("U.S. FDA Approves RYBREVANT FASPRO", "PR Newswire")], "Approved"),
    ([h("FDA Grants Approval to Taletrectinib for Advanced ROS1+ NSCLC", "Pharmacy Times"), h("FDA Approves Taletrectinib in TKI-Naive Advanced ROS1+ NSCLC", "CancerNetwork")], "Approved"),
    ([h("FDA Expands ANKTIVA® Approval for BCG-Unresponsive Non-Muscle-Invasive Bladder Cancer", "Business Wire")], "Approved"),
    ([h("PIMJETMI (pimicotinib) Developed by Abbisko and Licensed by Merck KGaA Receive U.S. FDA Approval for TGCT", "GlobeNewswire")], "Approved"),
]

if __name__ == "__main__":
    bad = 0
    for items, want in CASES:
        got, _, conf = f.classify(items)
        bad += got != want
        print("ok  " if got == want else "FAIL", f"{got:14} confirmed={conf!s:5} | {items[0]['title'][:80]}")
    print(f"{len(CASES) - bad}/{len(CASES)} passed")
    sys.exit(1 if bad else 0)

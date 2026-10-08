"""
Generates the SMALL Phase 1D development supplement (v0.12.0):
benchmark_phase1d/<company>/ — one follow-up document set per development
company plus phase1d_ground_truth.json. Synthetic, clearly labelled, and only
what the original benchmark cannot show: change over time, a same-date
contradiction, an explicitly stated risk, a new control, a new claim.

    python3 gen_phase1d_supplement.py      (re-creates the PDFs deterministically)
"""
import json
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).parent / "benchmark_phase1d"
FOOTER = "AUREVIA SYNTHETIC INSURANCE WORLD — PHASE 1D SUPPLEMENT — NOT REAL INSURANCE DATA   Page 1"


def pdf(path: Path, title: str, lines: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    w, h = A4
    y = h - 60
    c.setFont("Helvetica", 9)
    c.drawString(50, y, "SYNTHETIC / NOT REAL")
    y -= 28
    c.setFont("Helvetica-Bold", 15)
    c.drawCentredString(w / 2, y, title)
    y -= 26
    c.setFont("Helvetica", 11)
    for line in lines:
        if line.endswith(":") or line in ("Survey", "Operations", "Observations", "Declared Values", "Claims",
                                          "Policy Link", "Risk Notes"):
            c.setFont("Helvetica-Bold", 11)
            c.drawString(50, y, line)
            c.setFont("Helvetica", 11)
        else:
            c.drawString(50, y, line)
        y -= 18
    c.setFont("Helvetica", 7)
    c.drawString(50, 40, FOOTER)
    c.save()


COMPANIES = {
    "AUR-SYN-001": {
        "name": "Apex Precision Components Pvt. Ltd.",
        "docs": {"06_followup_risk_survey.pdf": ("Property Risk Survey (Follow-up)", [
            "Apex Precision Components Pvt. Ltd.", "Survey",
            "Location: 932 Technology Park, Pune, Maharashtra",
            "Additional Location: 41 Industrial Estate, Chakan, Maharashtra",
            "Survey date: 15 September 2026", "Operations",
            "The insured operates precision components. Estimated workforce: 812.",
            "Observations", "Overall risk rating: Moderate.",
            "Fire protection: hydrants, portable extinguishers and automatic sprinklers reported.",
            "Declared Values", "Plant and Machinery: 210,000,000"])},
        "expected": {
            "changes": [
                {"key": "business.workforce", "kind": "changed", "old": "786", "new": "812"},
                {"key": "control:Fire Protection", "kind": "changed"},
                {"key": "location:41 Industrial Estate, Chakan, Maharashtra", "kind": "added"},
                {"key": "asset:Plant and Machinery", "kind": "changed", "old": "180,000,000", "new": "210,000,000"},
                {"key": "business.survey_risk_rating", "kind": "unchanged"},
                {"key": "operation:precision components", "kind": "unchanged"}],
            "contradictions": [], "risks": [],
            "locations": ["932 Technology Park, Pune, Maharashtra", "41 Industrial Estate, Chakan, Maharashtra"],
            "workforce_before": 786, "workforce_after": 812,
            "before_date": "2026-09-01", "after_date": "2026-09-20"}},
    "AUR-SYN-002": {
        "name": "Northstar Pharma Systems Pvt. Ltd.",
        "docs": {
            "06_followup_risk_survey.pdf": ("Property Risk Survey (Follow-up)", [
                "Northstar Pharma Systems Pvt. Ltd.", "Survey",
                "Location: 750 Logistics Hub, Ahmedabad, Gujarat",
                "Survey date: 20 September 2026", "Operations",
                "The insured operates pharmaceutical formulations. Estimated workforce: 508.",
                "Observations", "Overall risk rating: Elevated.",
                "Fire protection: hydrants and portable extinguishers reported.",
                "Security: 24x7 guarding and CCTV reported."]),
            "07_followup_loss_run.pdf": ("Synthetic Loss Run (Update)", [
                "Northstar Pharma Systems Pvt. Ltd.", "Policy Link", "Policy: SYN-PROP-0002",
                "Insured: Northstar Pharma Systems Pvt. Ltd.", "Report date: 20 September 2026", "Claims",
                "Synthetic claims in period: 2.", "Total incurred amount: 2,750,000."])},
        "expected": {
            "changes": [
                {"key": "business.survey_risk_rating", "kind": "changed", "old": "Low", "new": "Elevated"},
                {"key": "control:Security", "kind": "added"},
                {"key": "control:Fire Protection", "kind": "unchanged"},
                {"key": "loss.claims_count", "kind": "changed", "old": "1", "new": "2"},
                {"key": "loss.total_incurred", "kind": "changed", "old": "450,000", "new": "2,750,000"},
                {"key": "business.workforce", "kind": "unchanged"}],
            "contradictions": [], "risks": [],
            "locations": ["750 Logistics Hub, Ahmedabad, Gujarat"],
            "before_date": "2026-09-01", "after_date": "2026-09-25"}},
    "AUR-SYN-003": {
        "name": "BluePeak Logistics Pvt. Ltd.",
        "docs": {"06_followup_risk_survey.pdf": ("Property Risk Survey (Follow-up)", [
            "BluePeak Logistics Pvt. Ltd.", "Survey",
            "Location: 267 Technology Park, Gurugram, Haryana",
            "Survey date: 30 September 2026", "Operations",
            "The insured operates logistics and warehousing. Estimated workforce: 462.",
            "Observations", "Overall risk rating: Low.",
            "Fire protection: hydrants and portable extinguishers reported."])},
        "expected": {
            "changes": [
                {"key": "business.workforce", "kind": "unchanged"},
                {"key": "business.survey_risk_rating", "kind": "unchanged"},
                {"key": "control:Fire Protection", "kind": "unchanged"},
                {"key": "operation:logistics and warehousing", "kind": "unchanged"}],
            "no_material_changes": True, "contradictions": [], "risks": [],
            "locations": ["267 Technology Park, Gurugram, Haryana"],
            "before_date": "2026-09-01", "after_date": "2026-10-01"}},
    "AUR-SYN-004": {
        "name": "Crestline Foods Pvt. Ltd.",
        "docs": {"06_followup_risk_survey.pdf": ("Property Risk Survey (Addendum)", [
            "Crestline Foods Pvt. Ltd.", "Survey",
            "Location: 464 Industrial Estate, Indore, Madhya Pradesh",
            "Survey date: 04 April 2026", "Operations",
            "The insured operates processed foods. Estimated workforce: 290.",
            "Risk Notes", "Identified risk: Ammonia refrigeration leak at the cold storage plant."])},
        "expected": {
            "changes": [], "contradictions": ["business.workforce"],
            "risks": ["Ammonia refrigeration leak at the cold storage plant"],
            "locations": ["464 Industrial Estate, Indore, Madhya Pradesh"],
            "before_date": "2026-04-01", "after_date": "2026-04-10"}},
}


def main():
    for cid, spec in COMPANIES.items():
        for fname, (title, lines) in spec["docs"].items():
            pdf(ROOT / cid / fname, title, lines)
        gt = {"company_id": cid, "company_name": spec["name"], "synthetic": True,
              "dataset": "phase1d-supplement-v1", "documents": sorted(spec["docs"]), **spec["expected"]}
        (ROOT / cid / "phase1d_ground_truth.json").write_text(json.dumps(gt, indent=2))
    (ROOT / "README.md").write_text(
        "# Phase 1D development supplement (synthetic)\n\n"
        "Follow-up documents for AUR-SYN-001–004 only, generated by `gen_phase1d_supplement.py`.\n"
        "They exist to test what the original benchmark cannot: change over time (001, 002),\n"
        "no false changes (003), a same-date contradiction and an explicitly stated risk (004).\n"
        "`phase1d_ground_truth.json` lists the expected changes, contradictions, risks and locations.\n"
        "Not real insurance data.\n")


if __name__ == "__main__":
    main()

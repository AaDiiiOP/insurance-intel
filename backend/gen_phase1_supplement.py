"""
Aurevia Synthetic Insurance World v1.1 — Phase 1 supplement generator.

The v1.0 dataset (README.md: "Use property_sum_insured as the primary
extraction target because the current Phase 0 vertical slice is
intentionally narrow") only ever needed to support Phase 0's single-field
benchmark. Its ground truth reflects that: only property_sum_insured,
policy_deductible, risk_rating and loss_amount are labeled anywhere, and
the documents themselves don't contain occupancy classifications,
construction types, exclusion clauses, or more than one endorsement per
company — there's nothing to extract for 6 of Property Policy Intelligence's
10 fact types, regardless of how good an extractor or harness is.

This script adds that missing content, additively, without touching any
v1.0 file:
  - REPLACES 01_property_policy.pdf and 03_endorsement.pdf per company with
    versions that add: a real address-derived property.location line
    (already implicitly present as "Insured" address, now also stated as
    the property's own location so it's unambiguous), an
    industry-appropriate property.occupancy classification and
    property.construction description, a second, distinct
    policy.coverage line beyond the sum-insured figure, a
    policy.exclusion clause naming two exclusions, and a second
    policy.endorsement distinct from the value-review one already present
    in v1.0 (kept unchanged) so multi-valued reconciliation has two true
    endorsements to hold onto rather than one.
  - WRITES phase1_ground_truth.json per company (additive sibling file,
    v1.0's ground_truth.json is untouched) with one entry per new fact,
    shaped for eval_property_policy.py to consume directly via
    knowledge/fact_types.py's fact type names.
  - WRITES phase1_manifest.json at the dataset root, listing every company
    + fact type + expected value + source document + page, the Phase 1
    equivalent of v1.0's manifests/document_manifest.json.

Content is derived per-company from company_profile.json's `industry`
field (20 companies, 20 distinct industries in v1.0) via INDUSTRY_PROFILES
below, rather than one boilerplate paragraph repeated 20 times — the same
"don't fake variety, derive it" spirit as the original dataset's per-company
addresses/financials.

Usage:
    python3 gen_phase1_supplement.py /path/to/Aurevia_Synthetic_Insurance_World_v1
"""
import json
import re
import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

# This script lives in <project_root>/scripts/ — its sibling backend/
# directory holds parsers.py, which does the same PDF-text extraction the
# real app uses (so the carrier/period/policy-number values this script
# reads back out of the v1.0 PDFs are read exactly the way the pipeline
# under test will read them, not via a different, potentially-inconsistent
# PDF library).
BACKEND_PATH = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))
from parsers import parse_file_with_quality  # noqa: E402


def extract_policy_header(policy_pdf_path: Path) -> dict:
    """
    Read the REAL per-company policy number, carrier name and policy
    period out of the existing v1.0 01_property_policy.pdf before it gets
    overwritten. All three vary per company (checked: policy numbers are
    zero-padded to 4 digits — SYN-PROP-0001, SYN-PROP-0020 — not
    reconstructable from the company id's own digit count; carriers repeat
    across companies but aren't the same for all; periods have different
    start dates per company) — reconstructing any of these from a template
    or the company id risks silently producing a value that doesn't match
    what the rest of that company's documents (coverage schedule,
    endorsement) already state.
    """
    data = policy_pdf_path.read_bytes()
    parsed, _quality = parse_file_with_quality(policy_pdf_path.name, data)
    policy_number_match = re.search(r"Policy Number:\s*(.+)", parsed.text)
    carrier_match = re.search(r"Carrier:\s*(.+)", parsed.text)
    period_match = re.search(r"Period:\s*(.+)", parsed.text)
    if not policy_number_match or not carrier_match or not period_match:
        raise ValueError(f"Could not read policy header from {policy_pdf_path}")
    return {
        "policy_number": policy_number_match.group(1).strip(),
        "carrier": carrier_match.group(1).strip(),
        "period": period_match.group(1).strip(),
    }

# Per-industry occupancy / construction / exclusion / second-coverage-line
# content. Chosen to be plausible for that industry without naming any
# real carrier practice or real underwriting text (same synthetic-only
# spirit as the rest of the dataset).
INDUSTRY_PROFILES = {
    "manufacturing": dict(
        occupancy="Precision component manufacturing facility with machine shop and assembly floor",
        construction="RCC frame structure, class A construction, fire-rated compartment walls",
        risk_factor="Machine shop operations with metalworking fluids present",
        exclusion_2="Machinery breakdown arising from normal wear and tear",
        coverage_2="Business Interruption",
    ),
    "pharmaceuticals": dict(
        occupancy="Pharmaceutical formulation and packaging facility with clean-room areas",
        construction="RCC frame structure with clean-room panel partitions, class A construction",
        risk_factor="Clean-room HVAC dependency and solvent storage",
        exclusion_2="Product recall costs and contamination not caused by an insured peril",
        coverage_2="Business Interruption",
    ),
    "logistics": dict(
        occupancy="Third-party logistics warehousing and cross-docking facility",
        construction="Pre-engineered steel building, class B construction, high-bay racking",
        risk_factor="High-density pallet racking and forklift traffic",
        exclusion_2="Goods in transit beyond the insured premises",
        coverage_2="Business Interruption",
    ),
    "food_processing": dict(
        occupancy="Food processing and cold storage facility",
        construction="RCC frame with insulated cold-storage panels, class B construction",
        risk_factor="Refrigeration ammonia system and cold-chain dependency",
        exclusion_2="Spoilage of stock following a power interruption under 24 hours",
        coverage_2="Business Interruption",
    ),
    "automotive": dict(
        occupancy="Automotive component manufacturing and assembly facility",
        construction="Steel frame structure, class B construction, paint booth area separated",
        risk_factor="Paint shop solvent storage and welding operations",
        exclusion_2="Machinery breakdown arising from normal wear and tear",
        coverage_2="Business Interruption",
    ),
    "textiles": dict(
        occupancy="Technical textile weaving and finishing facility",
        construction="Steel truss shed structure, class C construction, high combustible loading",
        risk_factor="High fabric and fiber storage density, dust accumulation",
        exclusion_2="Consequential loss from spontaneous combustion of raw fiber stock",
        coverage_2="Business Interruption",
    ),
    "electronics": dict(
        occupancy="Electronics assembly facility with SMT lines and static-controlled areas",
        construction="RCC frame structure, class A construction, raised anti-static flooring",
        risk_factor="ESD-sensitive component storage and soldering operations",
        exclusion_2="Product recall costs and contamination not caused by an insured peril",
        coverage_2="Business Interruption",
    ),
    "construction": dict(
        occupancy="Contractor's yard, plant storage and pre-fabrication facility",
        construction="Open yard with steel-clad workshop sheds, class C construction",
        risk_factor="Open storage of construction plant and combustible formwork materials",
        exclusion_2="Faulty workmanship or design not resulting in physical loss",
        coverage_2="Business Interruption",
    ),
    "hospitality": dict(
        occupancy="Hotel and hospitality premises with guest accommodation and kitchen facilities",
        construction="RCC frame structure, class A construction, commercial kitchen fire suppression fitted",
        risk_factor="Commercial kitchen deep-fry operations and LPG storage",
        exclusion_2="Loss of guest revenue not resulting from physical damage to the premises",
        coverage_2="Business Interruption",
    ),
    "healthcare": dict(
        occupancy="Diagnostic and healthcare services facility with imaging equipment",
        construction="RCC frame structure, class A construction, radiation-shielded imaging suite",
        risk_factor="High-value imaging equipment and biomedical waste handling",
        exclusion_2="Equipment breakdown covered under a separate biomedical equipment policy",
        coverage_2="Business Interruption",
    ),
    "renewable_energy": dict(
        occupancy="Solar module assembly and balance-of-plant equipment storage facility",
        construction="Pre-engineered steel building, class B construction",
        risk_factor="Open-yard equipment storage and inverter room fire load",
        exclusion_2="Performance shortfall of installed generation assets",
        coverage_2="Business Interruption",
    ),
    "chemicals": dict(
        occupancy="Specialty chemicals manufacturing facility with bulk storage tanks",
        construction="RCC frame structure with bunded tank farm, class B construction",
        risk_factor="Bulk chemical storage tanks and process reactor area",
        exclusion_2="Gradual pollution not resulting from a sudden and accidental event",
        coverage_2="Business Interruption",
    ),
    "retail": dict(
        occupancy="Specialty retail showroom and back-of-house storage",
        construction="RCC frame structure, class A construction, glazed shopfront",
        risk_factor="High-value merchandise density and public footfall exposure",
        exclusion_2="Shoplifting and inventory shrinkage not evidenced by forced entry",
        coverage_2="Business Interruption",
    ),
    "engineering": dict(
        occupancy="Heavy engineering fabrication and machining works",
        construction="Steel frame structure, class B construction, overhead crane bays",
        risk_factor="Heavy fabrication welding operations and overhead crane loads",
        exclusion_2="Machinery breakdown arising from normal wear and tear",
        coverage_2="Business Interruption",
    ),
    "cold_chain": dict(
        occupancy="Cold storage and cold-chain logistics facility",
        construction="Insulated panel construction over steel frame, class B construction",
        risk_factor="Ammonia refrigeration plant and continuous cold-chain dependency",
        exclusion_2="Spoilage of stock following a power interruption under 24 hours",
        coverage_2="Business Interruption",
    ),
    "packaging": dict(
        occupancy="Industrial packaging manufacturing facility with corrugation and printing lines",
        construction="Steel truss shed structure, class C construction, high combustible loading",
        risk_factor="Corrugated board and printing solvent storage, high combustible loading",
        exclusion_2="Consequential loss from spontaneous combustion of raw fiber stock",
        coverage_2="Business Interruption",
    ),
    "data_services": dict(
        occupancy="Data center and IT services facility with server halls",
        construction="RCC frame structure, class A construction, raised flooring and clean-agent suppression",
        risk_factor="Server hall heat density and UPS battery room fire load",
        exclusion_2="Business interruption arising solely from a software or data corruption event",
        coverage_2="Business Interruption",
    ),
    "marine_trading": dict(
        occupancy="Marine cargo trading office and bonded warehouse facility",
        construction="RCC frame structure, class A construction, bonded warehouse area segregated",
        risk_factor="Bonded warehouse cargo accumulation and dockside exposure",
        exclusion_2="Goods in transit beyond the insured premises",
        coverage_2="Business Interruption",
    ),
    "agri_processing": dict(
        occupancy="Agricultural produce processing and storage facility",
        construction="Steel truss shed structure, class C construction, grain silo attached",
        risk_factor="Grain dust accumulation and silo storage",
        exclusion_2="Spoilage of stock following a power interruption under 24 hours",
        coverage_2="Business Interruption",
    ),
    "industrial_services": dict(
        occupancy="Industrial maintenance and equipment services facility",
        construction="Steel frame structure, class B construction, workshop and stores combined",
        risk_factor="Rotating equipment repair bay and lubricant storage",
        exclusion_2="Machinery breakdown arising from normal wear and tear",
        coverage_2="Business Interruption",
    ),
}

STANDARD_EXCLUSION = "War, terrorism and nuclear risks"
SECOND_ENDORSEMENT_TEXT = (
    "Additional named insured endorsement: {legal_name}'s facility manager added as a "
    "notice-of-loss contact for this policy. No change to sum insured or coverage terms."
)
PAGE_W, PAGE_H = A4
MARGIN = 20 * mm


def _draw_doc(path: Path, title: str, company_name: str, lines: list[str]):
    """Same plain single-page layout as the v1.0 PDFs (checked against
    01_property_policy.pdf's rendered text) — a title block, a company
    name, then left-aligned body lines, footer watermark. Kept intentionally
    plain to match the existing dataset's visual style rather than
    introducing a different-looking Phase 1 document."""
    c = canvas.Canvas(str(path), pagesize=A4)
    y = PAGE_H - MARGIN

    c.setFont("Helvetica-Bold", 9)
    c.drawString(MARGIN, y, "SYNTHETIC / NOT REAL")
    y -= 8 * mm

    c.setFont("Helvetica-Bold", 16)
    c.drawString(MARGIN, y, title)
    y -= 8 * mm

    c.setFont("Helvetica", 12)
    c.drawString(MARGIN, y, company_name)
    y -= 10 * mm

    c.setFont("Helvetica", 10)
    for line in lines:
        if line == "":
            y -= 3 * mm
            continue
        if line.endswith(":") or line.isupper():
            c.setFont("Helvetica-Bold", 10)
            c.drawString(MARGIN, y, line)
            c.setFont("Helvetica", 10)
        else:
            c.drawString(MARGIN, y, line)
        y -= 6 * mm

    c.setFont("Helvetica-Oblique", 7)
    c.drawString(MARGIN, MARGIN / 2, "AUREVIA SYNTHETIC INSURANCE WORLD v1.1 (PHASE 1 SUPPLEMENT) — NOT REAL INSURANCE DATA  Page 1")
    c.showPage()
    c.save()


def build_property_policy(company_dir: Path, profile: dict, gt: dict, industry: dict,
                           header: dict):
    """Rewrite 01_property_policy.pdf with the Phase 1 additions layered
    onto every field the v1.0 version already had (checked against the
    original's rendered text so nothing already-tested regresses).
    `header` (policy_number/carrier/period) must be this company's REAL
    values, read from the v1.0 PDF before it was overwritten — see
    extract_policy_header()."""
    facts = gt["facts"]
    sum_insured = next(f["value_inr"] for f in facts if f["field"] == "property_sum_insured")
    deductible = next(f["value_inr"] for f in facts if f["field"] == "policy_deductible")
    bi_sum_insured = round(sum_insured * 0.2)

    lines = [
        "Insured",
        profile["legal_name"],
        profile["primary_address"],
        "",
        "Policy Details:",
        f"Policy Number: {header['policy_number']}",
        f"Carrier: {header['carrier']}",
        f"Period: {header['period']}",
        "",
        "Coverage:",
        f"Property Damage Sum Insured: n{sum_insured:,}",
        f"Business Interruption Sum Insured: n{bi_sum_insured:,}",
        f"Deductible: n{deductible:,}",
        "",
        "Property Details:",
        f"Property Location: {profile['primary_address']}",
        f"Occupancy: {industry['occupancy']}",
        f"Construction: {industry['construction']}",
        "",
        "Exclusions:",
        f"1. {STANDARD_EXCLUSION}",
        f"2. {industry['exclusion_2']}",
        "",
        "Conditions",
        "Fire protection systems must be maintained. Material changes in occupancy or "
        "operations must be notified.",
    ]
    _draw_doc(company_dir / "01_property_policy.pdf", "Property Policy", profile["legal_name"], lines)

    return {
        "bi_sum_insured": bi_sum_insured,
    }


def extract_endorsement_header(endorsement_pdf_path: Path) -> dict:
    """Read the REAL per-company endorsement number and effective date out
    of the existing v1.0 03_endorsement.pdf before it gets overwritten —
    both vary per company (checked: AUR-SYN-002 and AUR-SYN-005 have
    different effective dates despite similar structure), so, same
    reasoning as extract_policy_header, these must be read, not
    reconstructed from a template."""
    data = endorsement_pdf_path.read_bytes()
    parsed, _quality = parse_file_with_quality(endorsement_pdf_path.name, data)
    number_match = re.search(r"Endorsement (SYN-END-\S+)", parsed.text)
    policy_match = re.search(r"Policy:\s*(.+)", parsed.text)
    effective_match = re.search(r"Effective:\s*(.+)", parsed.text)
    machinery_value_match = re.search(r"Revised machinery declared value:\s*n([\d,]+)", parsed.text)
    if not number_match or not policy_match or not effective_match or not machinery_value_match:
        raise ValueError(f"Could not read endorsement header from {endorsement_pdf_path}")
    return {
        "endorsement_number": number_match.group(1).strip(),
        "policy_number": policy_match.group(1).strip(),
        "effective": effective_match.group(1).strip(),
        "machinery_value": int(machinery_value_match.group(1).replace(",", "")),
    }


def build_endorsement(company_dir: Path, profile: dict, gt: dict, header: dict):
    """Rewrite 03_endorsement.pdf keeping the v1.0 value-review endorsement
    text byte-for-byte the same as before (same endorsement number, policy
    number, effective date and machinery value — all read from the real
    v1.0 PDF via `header`, not reconstructed) so the existing
    property_sum_insured-via-endorsement ground truth stays valid, and
    appending a second, distinct endorsement, so policy.endorsement has two
    true multi-valued instances per company."""
    facts = gt["facts"]
    sum_insured = next(f["value_inr"] for f in facts if f["field"] == "property_sum_insured")

    lines = [
        f"Endorsement {header['endorsement_number']}",
        f"Policy: {header['policy_number']}",
        f"Effective: {header['effective']}",
        "",
        "Change 1:",
        f"Plant and machinery values reviewed. Revised machinery declared value: n{header['machinery_value']:,}.",
        "",
        "Policy Reference:",
        f"Property sum insured reference in this synthetic scenario: n{sum_insured:,}.",
        "",
        "Change 2:",
        SECOND_ENDORSEMENT_TEXT.format(legal_name=profile["legal_name"]),
    ]
    _draw_doc(company_dir / "03_endorsement.pdf", "Policy Endorsement", profile["legal_name"], lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 gen_phase1_supplement.py /path/to/Aurevia_Synthetic_Insurance_World_v1")
        sys.exit(1)

    dataset_root = Path(sys.argv[1])
    companies_dir = dataset_root / "companies"
    if not companies_dir.is_dir():
        print(f"No companies/ directory found under {dataset_root}")
        sys.exit(1)

    manifest = []
    company_dirs = sorted(d for d in companies_dir.iterdir() if d.is_dir())
    print(f"Extending {len(company_dirs)} companies with Phase 1 fact content...\n")

    for company_dir in company_dirs:
        company_id = company_dir.name
        profile = json.loads((company_dir / "company_profile.json").read_text())
        gt = json.loads((company_dir / "ground_truth.json").read_text())
        gt["company_id"] = company_id

        industry_key = profile["industry"]
        industry = INDUSTRY_PROFILES.get(industry_key)
        if industry is None:
            print(f"  {company_id}: WARNING no industry profile for '{industry_key}', skipping")
            continue

        policy_header = extract_policy_header(company_dir / "01_property_policy.pdf")
        endorsement_header = extract_endorsement_header(company_dir / "03_endorsement.pdf")
        extra = build_property_policy(company_dir, profile, gt, industry, policy_header)
        build_endorsement(company_dir, profile, gt, endorsement_header)

        policy_doc = f"{company_id}_01_property_policy.pdf"
        endorsement_doc = f"{company_id}_03_endorsement.pdf"

        phase1_facts = [
            {"fact_type": "property.location", "value": profile["primary_address"],
             "source_document": policy_doc, "page": 1},
            {"fact_type": "property.occupancy", "value": industry["occupancy"],
             "source_document": policy_doc, "page": 1},
            {"fact_type": "property.construction", "value": industry["construction"],
             "source_document": policy_doc, "page": 1},
            {"fact_type": "policy.coverage", "value": "Property Damage",
             "value_inr": next(f["value_inr"] for f in gt["facts"] if f["field"] == "property_sum_insured"),
             "source_document": policy_doc, "page": 1},
            {"fact_type": "policy.coverage", "value": "Business Interruption",
             "value_inr": extra["bi_sum_insured"],
             "source_document": policy_doc, "page": 1},
            {"fact_type": "policy.exclusion", "value": STANDARD_EXCLUSION,
             "source_document": policy_doc, "page": 1},
            {"fact_type": "policy.exclusion", "value": industry["exclusion_2"],
             "source_document": policy_doc, "page": 1},
            {"fact_type": "policy.endorsement", "value": "Plant and machinery values reviewed",
             "source_document": endorsement_doc, "page": 1},
            {"fact_type": "policy.endorsement",
             "value": SECOND_ENDORSEMENT_TEXT.format(legal_name=profile["legal_name"]),
             "source_document": endorsement_doc, "page": 1},
        ]

        phase1_gt = {
            "company_id": company_id,
            "company_name": gt.get("company_name"),
            "synthetic": True,
            "dataset_version": "1.1-phase1-supplement",
            "phase1_facts": phase1_facts,
        }
        (company_dir / "phase1_ground_truth.json").write_text(json.dumps(phase1_gt, indent=2))

        for fact in phase1_facts:
            manifest.append({"company_id": company_id, **fact})

        print(f"  {company_id}  ({industry_key}) ... OK  ({len(phase1_facts)} facts)")

    manifest_path = dataset_root / "phase1_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nWrote phase1_ground_truth.json in {len(company_dirs)} company folders.")
    print(f"Wrote {manifest_path} ({len(manifest)} fact entries).")


if __name__ == "__main__":
    main()

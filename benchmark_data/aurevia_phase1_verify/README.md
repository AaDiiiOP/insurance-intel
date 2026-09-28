# Aurevia Synthetic Insurance World v1.0

A controlled, fully fictional insurance dataset for testing Aurevia Document Intelligence Phase 0.

## Dataset
- 20 fictional commercial businesses
- 100 core PDF documents (5 per company)
- 6-7 OCR-target scanned policy variants
- per-company ground_truth.json
- global golden_facts.jsonl
- document_manifest.json
- phase0_acceptance_tests.csv

## Five documents per company
1. Property Policy
2. Coverage Schedule
3. Policy Endorsement
4. Property Risk Survey
5. Synthetic Loss Run

## Deliberate test cases
Companies AUR-SYN-004, AUR-SYN-009, AUR-SYN-015 and AUR-SYN-019 contain a deliberate policy-vs-schedule sum-insured inconsistency.

Every third company has an image-backed scanned policy variant for OCR testing.

## Important
All names, carriers, policy numbers, addresses, financial figures, claims and insurance wording are fictional. Do not present them as real insurance records or real carrier documents.

## First Phase 0 benchmark
Use `property_sum_insured` as the primary extraction target because the current Phase 0 vertical slice is intentionally narrow.

Measure:
- exact value accuracy
- evidence page accuracy
- OCR success
- confidence type
- review/override persistence
- contradiction detection
- silent guessing rate

The goal is not merely to get an answer. Aurevia should know when the evidence is weak or contradictory and route the fact to human review.

# HealthData Insight: A FHIR R4 Healthcare Data Quality, Privacy, and Public Health Analytics Platform

**Technical report · Version 1.0 · October 2026**

> **Scope.** This report describes a research prototype that was built and evaluated **exclusively on synthetic data**
> (Synthea). The system is not HIPAA compliant, not clinically validated, not endorsed by the CDC or any public-health
> agency, and not production-ready. Its privacy controls reduce risk but do not guarantee de-identification.

## Abstract

Public-health programs increasingly receive clinical data as HL7 FHIR R4. Before such data supports analysis, it must
be validated, assessed for fitness for use, and reviewed for identifiable information. HealthData Insight combines
these steps in one reproducible workflow:

- FHIR R4 validation of Patient, Condition, and Observation resources;
- four data-quality dimensions (completeness, validity, consistency, uniqueness);
- rule-based detection of protected health information (PHI) in structured fields;
- configurable sanitization whose output is re-validated;
- descriptive public-health analytics on a pseudonymized extract, with base-R confidence intervals.

The evaluation used 1,180 Synthea patients (527,113 resources) with injected defects and labelled PHI. Validation
detected 3,000 of 3,000 injected defects with no false positives. PHI detection reached 94.0% precision and 96.9%
recall overall, but **0% recall on free text**. After sanitization, prevalence, observation statistics, and monthly
trends were unchanged. The evaluation also exposed and fixed genuine privacy defects, and it documents remaining
blind spots.

## 1. Objective

The goal was a transparent tool that answers three questions about an incoming FHIR dataset:

1. *Is it valid?*
2. *Is it fit for analysis?*
3. *Where are the identifiers, and what remains after removing them?*

It should also report results a reviewer can verify. Evaluation was a design requirement, not an afterthought:
every claim in this report is backed by a reproducible experiment.

## 2. System overview

A Streamlit dashboard sits on a Streamlit-independent Python package, so the entire pipeline runs and is tested
headlessly.

1. **Parse and store.** Uploaded JSON or NDJSON is parsed (Bundles unpacked; malformed entries recorded by line)
    into a temporary per-session SQLite store.
2. **Validate.** Each resource is checked against declarative rules: structure, required and important fields, code
    sets, calendar validity of dates, date ordering, and reference resolution. Every problem becomes a coded issue
    mapped to exactly one quality dimension.
3. **Score.** Expected fields are declared once (`FIELD_SPECS`), so "missing values" and the completeness score are
    consistent by construction.
4. **Scan for PHI.** Deterministic rules on named structured fields (no machine learning or NLP) report *locations*;
    values are never displayed or stored.
5. **Pseudonymize for analytics.** Analytics read only an allow-listed extract:
    - invalid and duplicate records are excluded;
    - ids are replaced by keyed-HMAC pseudonyms with a per-run key;
    - birth dates are reduced to the year (ages 90+ grouped), addresses to state, and clinical dates to the month;
    - no free text is extracted;
    - a base-R subprocess computes exact (Clopper–Pearson) 95% confidence intervals for prevalence on this extract
      only.
6. **Sanitize (optional).** Identifiers are removed or pseudonymized, references are rewritten to stay resolvable,
    and dates are coarsened. The output is then validated and scanned again.
7. **Record.** A history database stores counts, scores, issue codes, and PHI paths, and never content, values, or
    file names.

## 3. Evaluation methods

**Data.** Synthea's published 1K FHIR R4 sample: 1,180 patients, 527,113 resources, 1.34 GB, SHA-256 verified. The
patients were processed in 8 batches of 150 through the application's own ingestion path, because the pipeline is
in-memory and the test machine had about 4 GB free. Patient bundles are self-contained, so batching affects only
cross-batch duplicate detection.

**Ground truth by construction.**

- **Validation and data quality.** 15 structure and validity defect types and 11 completeness and consistency
  types (200 instances each) were injected into copies of real resources. Each type ran in its **own pass**, so
  defects could not mask one another.

- **Duplicates.** Eight constructed cases: four true-duplicate cases, three non-duplicate cases, and one hard case
  (the same person with new identifiers).

- **PHI.** A written reference policy, modelled on the HIPAA Safe Harbor identifier categories (as a labelling
  yardstick, not a compliance test), labelled 302,269 locations independently of the detector. Planted free-text PHI
  and decoys were added under a fixed seed.

- **Sanitization and information preserved.** Two configurations were run on all resources. Statistics from the raw
  data were compared with the pseudonymized analytical dataset.

**Performance.** Batch mode over all patients, and a fresh-process scaling series (50–600 patients, 3 repeats) on an
AMD Ryzen 9 8945H with 16 GB RAM, Windows 11, Python 3.13, and R 4.6.1.

## 4. Results

| Measure | Result |
| --- | --- |
| Validation (15 defect types) | 3,000 / 3,000 detected with the expected code; 0 of 7,903,695 clean record checks flagged invalid |
| Data-quality defects (11 types) | 2,200 / 2,200 detected; missing-field accounting exact in 48 / 48 passes |
| Duplicates | Precision 100%, recall 80%; the "same person, new identifiers" case was missed (0 / 80) |
| PHI, all locations | Precision 94.0%, recall 96.9% (TP 111,104; FP 7,143; FN 3,513) |
| PHI, structured Patient fields | Precision 92.7%, recall 92.7%; all misses were address latitude/longitude |
| PHI, free text (`valueString`, `note`) | Recall 0% (2,333 planted items) |
| PHI, narratives | Recall 100%, precision 16.5% (all narratives flagged) |
| Sanitization (default) | 0 high-risk findings left; 0 new errors; 0 broken references; 527,113 resources re-parsed |
| Values remaining after sanitization | No patient names, phones, identifiers, streets, or full birth dates. The patient's postal code appeared in provider addresses for 231 of 893 patients |
| Information preserved | Identical prevalence (137 codes), observation mean and SD (104 series), and monthly counts; age-group agreement 98.98% |
| Throughput | 5,646 resources/s end to end (527,113 resources in 93.4 s); about 5,500/s in a single process |
| Memory | Linear; about 3.7 MB per patient (2.8 GB at 600 patients) |

## 5. Discussion

**What the results support.** The validation and quality rules behave as specified, with no false alarms on half a
million well-formed resources. Pseudonymization for analytics costs essentially nothing for the descriptive
statistics the dashboard reports. The only measurable loss is about 1% of patients moving up one age band, because
age is computed from birth year.

**What they do not support.** The evaluation was designed to expose weaknesses, and it did:

- **Free text is a blind spot.** Identifying text in `valueString` and `note` is never detected or rewritten. This
  is the most important limitation for real clinical data.

- **Fields without rules are missed.** Address geolocation (latitude/longitude) is one example.
- **Indirect location leakage.** Provider addresses, which the sanitizer leaves intact because they are not patient
  data, still contained the patient's postal code for about a quarter of patients.

- **Conservative narrative flagging.** Flagging every narrative costs precision (16.5%), but it guarantees review,
  and the sanitizer always removes narratives.

- **No fuzzy record linkage.** A person re-registered with new identifiers is not detected. Conversely, two
  different synthetic patients who shared an SSN were flagged as one person.

**Defects found by the evaluation itself.** Testing on realistic data revealed problems the unit tests had not:

- Exact birth dates survived sanitization through encounters, claims, and observations dated on the day of birth.
  Dates are now coarsened in every non-Patient resource, and leaked birth dates fell to 0 of 1,180.

- Grouping ages 90+ shifted the birth year of patients who died young past their date of death. Age at death is
  now used consistently.

- Per-resource "out of scope" warnings numbered 17,596 on 100 patients; they are now one note per type.
- The pipeline loaded the dataset twice. On 300 patients, fixing it cut processing time from 22.1 s to 9.9 s and
  peak memory from 2.5 GB to 1.65 GB.

Three flaws in the evaluation harness were also corrected before the final run (interfering defects, mislabelled
clinician names, colliding injected ids), and the corrections are documented.

**Threats to validity.**

- Synthea data is cleaner than real EHR exports.
- The injected defects target the implemented rules, so detection rates show correct implementation rather than
  real-world coverage.

- The PHI labels follow a policy defined for this study.
- Performance comes from one shared machine.

## 6. Conclusion and future work

HealthData Insight shows that validation, quality assessment, privacy review, and pseudonymized analytics can be
combined in one auditable, reproducible workflow. It also shows that measuring privacy controls is essential:
measurement found two real privacy defects and three documented blind spots. Moving beyond a prototype would
require:

- evaluation on authorized real-world data, with privacy and clinical review;
- profile- and terminology-aware validation (for example, the HL7 FHIR Validator);
- free-text de-identification and formal re-identification risk assessment;
- streaming or database-backed processing;
- authentication and encryption at rest.

**Availability.** The source code, the evaluation harness, generated results, and full documentation (README,
ARCHITECTURE, EVALUATION, SECURITY, LIMITATIONS) are in the project repository. The command
`python -m evaluation.run_evaluation` reproduces every result in this report.

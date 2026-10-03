# Limitations

HealthData Insight is a research and portfolio **prototype**, developed and evaluated on **synthetic data only**
(Synthea). It is **not production-ready, not clinically validated, not HIPAA compliant, and not endorsed by the CDC
or any other public-health agency**. Measured figures referenced below are in [EVALUATION.md](EVALUATION.md).

## Data and scope

- **Synthetic data only.**
  - All development and evaluation used Synthea output: the published "1K Sample" (FHIR R4, September 2019,
    1,180 patients) and the app's small bundled samples.
  - Synthetic data is cleaner and more uniform than real EHR exports. Accuracy, performance, and the size of
    privacy risks on real data are unknown.
  - The Synthea dataset is internally consistent: unmodified, it produces no validation errors. Most error-detection
    results therefore come from **defects injected by the evaluation itself**, so they measure the rules that were
    written, not unknown real-world error patterns.
- **Three resource types.** Only Patient, Condition, and Observation are validated in depth and analyzed. Other
  types are counted, checked for a valid type and id, scanned for narrative and Patient-reference display text, and
  included in duplicate detection.
- **FHIR R4 only.** Other FHIR versions aren't supported.

## Validation

- **Not a conformance validator.** There is no profile (StructureDefinition) or US Core validation, no terminology
  binding checks beyond a few status and gender code sets, and most FHIRPath invariants aren't implemented. Codes
  aren't checked against SNOMED CT, LOINC, or other code systems.
- **Partial reference resolution.** References are resolved only within the uploaded dataset, and only when the
  dataset contains resources of the target type. Absolute URLs aren't followed.
- **Exact-match duplicate detection.** Duplicates are found only by repeated ids, identical content, or a shared
  Patient identifier. There is no fuzzy or probabilistic record linkage, so the same person re-registered with new
  identifiers isn't detected (0 of 80 in E4). Conversely, two different people who share an identifier value are
  flagged as one person (seen once in the Synthea data, a shared SSN).
- **Heuristic severity choices.** For example, a missing Condition code is a warning, while a missing Observation
  code or value is an error.

## Privacy

- **Rule-based PHI detection.**
  - Detection is limited to known structured fields. It does **not** detect identifying information in free text
    (`valueString`, `note`, and similar), in non-standard extensions, or in address geolocation (latitude and
    longitude). All of these were measured as misses in E2.
  - It flags *all* narrative text and state-level addresses, which produces false positives (E2).
- **Labels were defined for this evaluation.** Precision and recall are measured against labels from a reference
  policy that was written for this evaluation (modelled on the Safe Harbor identifier categories). They don't
  validate HIPAA de-identification.
- **Sanitization reduces risk but doesn't guarantee de-identification.**
  - It doesn't rewrite free text other than narratives.
  - It keeps other parties' data, such as provider names and addresses. In the evaluation, provider addresses
    still contained the patient's postal code for 231 of 893 patients (E7).
  - It applies the chosen date precision to clinical events, but rare combinations of kept attributes (age band,
    gender, state, conditions) can still single out people in small datasets. No k-anonymity or
    differential-privacy guarantee is attempted.
- **Pseudonyms can link datasets.** Stable pseudonyms (with a user-supplied secret) are linkable across datasets by
  design. Anyone holding the secret can re-create them.
- **Analytics date limits.** The analytical dataset reduces clinical dates to months and ages to birth years. In
  the evaluation, 1.02% of patients landed one age group higher than their exact age (E8). Analyses that need
  exact dates or intervals can't be done on it.

## Analytics

- **Descriptive only.** There is no age standardization, weighting, or adjustment, and no population denominators
  beyond the uploaded patients. Prevalence is "share of uploaded patients with at least one coded condition".
- **Labels from codings.** Labels come from coding display text or codes. Free-text labels are deliberately ignored,
  so uncoded data shows as "(uncoded)".
- **R output isn't filtered.** R charts describe the full analytical dataset; the dashboard filters don't apply to
  them.

## Engineering

- **Whole dataset in memory.**
  - The pipeline loads every resource into Python objects, because duplicate detection and reference resolution
    need the whole dataset.
  - Memory grows linearly with input: about 3.7 MB of committed memory per Synthea patient, on top of about
    0.5 GB (2.8 GB at 600 patients; E5/E6). On the evaluation machine, the full 1,180-patient dataset wasn't run in
    a single process: about 6.5 GB was estimated, and other applications left only about 4 GB free.
  - The evaluation therefore processed it in batches of 150 patients. Batching is valid for this dataset because
    each Synthea patient bundle is self-contained. The exception is duplicate copies of Practitioner and
    Organization resources across batches, which batching can't see.
  - Generating a sanitized copy needs a second full copy of the data in memory.
- **Single user.** There is no authentication, access control, or multi-user isolation (see
  [SECURITY.md](SECURITY.md)). The history database has no retention policy.
- **Browser uploads.** Uploading very large numbers of files through the browser is slow, and every file name
  appears in the uploader.
- **Limited platform testing.** Tests run on Windows 11 with Python 3.13 and R 4.6.1. Other platforms and versions
  within the declared support (Python 3.11+) weren't tested.
- **Dark mode.** The dashboard forces the light theme, because the chart palette was validated for light mode only.

## What would be needed beyond a prototype

- **Data and validation:**
  - evaluation on real, authorized, de-identified data, with clinical and privacy review;
  - a conformance validator, such as the HL7 FHIR Validator, for profiles and terminology.
- **Privacy:**
  - formal de-identification (expert determination, or a verified Safe Harbor process);
  - free-text de-identification;
  - a re-identification risk assessment.
- **Engineering:**
  - streaming or database-backed processing for large datasets;
  - authentication, encryption at rest, a retention policy, and audit-log protection;
  - a security review and dependency scanning.

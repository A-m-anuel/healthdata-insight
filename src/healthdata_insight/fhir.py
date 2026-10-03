"""FHIR R4 primitives: resource types, ids, dates, and references."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# All resource types defined by FHIR R4 (4.0.1).
R4_RESOURCE_TYPES: frozenset[str] = frozenset("""
Account ActivityDefinition AdverseEvent AllergyIntolerance Appointment AppointmentResponse AuditEvent
Basic Binary BiologicallyDerivedProduct BodyStructure Bundle CapabilityStatement CarePlan CareTeam
CatalogEntry ChargeItem ChargeItemDefinition Claim ClaimResponse ClinicalImpression CodeSystem
Communication CommunicationRequest CompartmentDefinition Composition ConceptMap Condition Consent
Contract Coverage CoverageEligibilityRequest CoverageEligibilityResponse DetectedIssue Device
DeviceDefinition DeviceMetric DeviceRequest DeviceUseStatement DiagnosticReport DocumentManifest
DocumentReference EffectEvidenceSynthesis Encounter Endpoint EnrollmentRequest EnrollmentResponse
EpisodeOfCare EventDefinition Evidence EvidenceVariable ExampleScenario ExplanationOfBenefit
FamilyMemberHistory Flag Goal GraphDefinition Group GuidanceResponse HealthcareService ImagingStudy
Immunization ImmunizationEvaluation ImmunizationRecommendation ImplementationGuide InsurancePlan
Invoice Library Linkage List Location Measure MeasureReport Media Medication MedicationAdministration
MedicationDispense MedicationKnowledge MedicationRequest MedicationStatement MedicinalProduct
MedicinalProductAuthorization MedicinalProductContraindication MedicinalProductIndication
MedicinalProductIngredient MedicinalProductInteraction MedicinalProductManufactured
MedicinalProductPackaged MedicinalProductPharmaceutical MedicinalProductUndesirableEffect
MessageDefinition MessageHeader MolecularSequence NamingSystem NutritionOrder Observation
ObservationDefinition OperationDefinition OperationOutcome Organization OrganizationAffiliation
Parameters Patient PaymentNotice PaymentReconciliation Person PlanDefinition Practitioner
PractitionerRole Procedure Provenance Questionnaire QuestionnaireResponse RelatedPerson RequestGroup
ResearchDefinition ResearchElementDefinition ResearchStudy ResearchSubject RiskAssessment
RiskEvidenceSynthesis Schedule SearchParameter ServiceRequest Slot Specimen SpecimenDefinition
StructureDefinition StructureMap Subscription Substance SubstanceNucleicAcid SubstancePolymer
SubstanceProtein SubstanceReferenceInformation SubstanceSourceMaterial SubstanceSpecification
SupplyDelivery SupplyRequest Task TerminologyCapabilities TestReport TestScript ValueSet
VerificationResult VisionPrescription
""".split())

_ID_RE = re.compile(r"[A-Za-z0-9\-.]{1,64}")
_DATETIME_RE = re.compile(
    r"(?P<year>\d{4})"
    r"(?:-(?P<month>\d{2})"
    r"(?:-(?P<day>\d{2})"
    r"(?:T(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(?:\.\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2}))?)?)?"
)
_RELATIVE_REF_RE = re.compile(
    r"(?:https?://\S+/)?(?P<type>[A-Z][A-Za-z]+)/(?P<id>[A-Za-z0-9\-.]{1,64})(?:/_history/\S+)?"
)


def is_valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class DateRange:
    """The interval a (possibly partial) FHIR date covers, e.g. "2020-03" -> March 2020."""

    start: datetime
    end: datetime  # exclusive

    def definitely_before(self, other: DateRange) -> bool:
        return self.end <= other.start


def parse_fhir_datetime(value: object, *, date_only: bool = False) -> DateRange:
    """Parse a FHIR ``date`` or ``dateTime`` string.

    Raises:
        ValueError: if the value is not a valid FHIR date/dateTime.
    """
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    match = _DATETIME_RE.fullmatch(value)
    if match is None or (date_only and match["hour"] is not None):
        kind = "date (YYYY, YYYY-MM or YYYY-MM-DD)" if date_only else "date/dateTime"
        raise ValueError(f"'{value}' is not a valid FHIR {kind}")

    year = int(match["year"])
    month = int(match["month"] or 1)
    day = int(match["day"] or 1)
    if year == 0 or not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(year, month)[1]:
        raise ValueError(f"'{value}' is not a real calendar date")

    if match["hour"] is None:
        start = datetime(year, month, day, tzinfo=timezone.utc)
        if match["day"]:
            end = start + timedelta(days=1)
        elif match["month"]:
            end = start + timedelta(days=calendar.monthrange(year, month)[1])
        else:
            end = start.replace(year=year + 1) if year < 9999 else datetime.max.replace(tzinfo=timezone.utc)
        return DateRange(start, end)

    try:
        start = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError(f"'{value}' is not a real date and time") from exc
    return DateRange(start, start + timedelta(seconds=1))


@dataclass(frozen=True)
class ParsedReference:
    resource_type: str | None  # None for urn:uuid / urn:oid references
    resource_id: str
    absolute: bool  # points outside this dataset (http/https URL)


def parse_reference(value: str) -> ParsedReference | None:
    """Parse a Reference.reference string. Returns None if it is malformed.

    Contained references ("#id") are not resolvable here and also return None.
    """
    if value.startswith(("urn:uuid:", "urn:oid:")):
        target = value.split(":", 2)[2]
        return ParsedReference(None, target, absolute=False) if target else None
    match = _RELATIVE_REF_RE.fullmatch(value)
    if match is None:
        return None
    return ParsedReference(match["type"], match["id"], absolute=value.startswith("http"))


def age_reference_year(patient: dict, now: datetime) -> int:
    """The year a Patient's age is measured at: the year of death if known, otherwise ``now``.

    Without this, someone born in 1916 who died in 1918 would count as "aged 90+" today.
    """
    try:
        return parse_fhir_datetime(patient.get("deceasedDateTime")).start.year
    except ValueError:
        return now.year

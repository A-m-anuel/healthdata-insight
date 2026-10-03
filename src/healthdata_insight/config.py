"""Application-wide constants."""

APP_NAME = "HealthData Insight"

FHIR_VERSION = "R4"

# Resource types analysed by HealthData Insight. Other types are still
# counted, but flagged as out of scope.
IN_SCOPE_RESOURCE_TYPES: tuple[str, ...] = ("Patient", "Condition", "Observation")

SUPPORTED_EXTENSIONS: tuple[str, ...] = (".json", ".ndjson", ".jsonl")
NDJSON_EXTENSIONS: tuple[str, ...] = (".ndjson", ".jsonl")

# Keep in sync with server.maxUploadSize in .streamlit/config.toml.
MAX_FILE_SIZE_MB = 200
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

"""Business logic and ingestion services."""

# Imported lazily so DB-free services (e.g. watn_pipeline) can be used without
# initialising the SQLAlchemy engine that data_ingestion pulls in.
__all__ = [
    "get_default_hub",
    "get_system_user",
    "ingest_all_watn_files",
    "parse_salesforce_file",
]


def __getattr__(name):
    if name in __all__:
        from app.services import data_ingestion

        return getattr(data_ingestion, name)
    raise AttributeError(f"module 'app.services' has no attribute {name!r}")

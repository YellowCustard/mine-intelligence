"""Application configuration, read from the environment (12-factor)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Documented in ``.env.example``."""

    model_config = SettingsConfigDict(env_prefix="MM_", env_file=".env", extra="ignore")

    env: str = "dev"
    log_level: str = "INFO"
    default_site_tz: str = "Africa/Harare"

    database_url: str = "postgresql+psycopg://minemonitor:minemonitor@localhost:5432/minemonitor"

    # MQTT transport (M2).
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_topic_prefix: str = "mm"
    mqtt_ingest_client_id: str = "mm-ingestor"
    # Publisher-side store-and-forward spool (crash-safe local buffer).
    spool_path: str = "/tmp/mm-spool.sqlite"

    # Rules.
    offline_threshold_s: int = 600  # silent this long (while active) = offline
    offline_check_interval_s: int = 60  # how often the ingestor scans for offline
    default_site_id: str = "kn-zw-01"

    # Retention per data class, in days. 0 = keep forever (brief §4). Generous
    # defaults; a stricter legal answer costs configuration, not architecture.
    retain_positions_days: int = 90
    retain_metrics_days: int = 365
    retain_events_days: int = 365
    retention_interval_s: int = 86_400  # the ingestor runs retention ~daily

    # Bootstrap admin — created on start ONLY if the users table is empty, so a
    # fresh box is usable. Leave blank in production and create users via the CLI.
    bootstrap_admin_user: str = ""
    bootstrap_admin_password: str = ""

    # Present for later milestones; unused now.
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "mine-evidence"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()

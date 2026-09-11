"""Application configuration, read from the environment (12-factor)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The credentials shipped in ``.env.example`` for local development. They must
# never reach a production deployment — the ``prod`` guard below refuses to start
# if they are still in place, so a dev default cannot silently become a prod one.
_DEV_DB_URL = "postgresql+psycopg://minemonitor:minemonitor@localhost:5432/minemonitor"
_DEV_DB_CREDENTIALS = "minemonitor:minemonitor@"


class Settings(BaseSettings):
    """All runtime configuration. Documented in ``.env.example``."""

    model_config = SettingsConfigDict(env_prefix="MM_", env_file=".env", extra="ignore")

    env: str = "dev"
    log_level: str = "INFO"
    default_site_tz: str = "Africa/Harare"

    database_url: str = _DEV_DB_URL

    # MQTT transport (M2).
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_topic_prefix: str = "mm"
    mqtt_ingest_client_id: str = "mm-ingestor"
    # Strict device provisioning (brief §10/§11): when true, MQTT telemetry is
    # accepted only for assets with an enabled device row. Off by default so a
    # fresh/demo install ingests without provisioning; turn on for a hardened site.
    mqtt_require_registered_device: bool = False
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
    # Audit trail is retained longer than the data it describes — accountability
    # outlives the records (brief §4). 0 = keep forever.
    retain_audit_days: int = 730
    retention_interval_s: int = 86_400  # the ingestor runs retention ~daily

    # Bootstrap admin — created on start ONLY if the users table is empty, so a
    # fresh box is usable. Leave blank in production and create users via the CLI.
    bootstrap_admin_user: str = ""
    bootstrap_admin_password: str = ""

    # Auth hardening. Lock an account after N consecutive failures for M minutes;
    # a short in-process cache avoids re-deriving PBKDF2 on every request (SSE
    # reconnects poll continuously) while keeping lockout authoritative.
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    auth_verify_cache_ttl_s: int = 30

    # A background worker's heartbeat older than this marks it stale in /health.
    heartbeat_stale_s: int = 180

    # Teltonika TCP listener (M7). Trackers speak Codec 8/8E over raw TCP; the
    # listener decodes and republishes into MQTT like any other adapter.
    teltonika_host: str = "0.0.0.0"  # noqa: S104 - a device listener binds all interfaces
    teltonika_port: int = 5027

    # Present for later milestones; unused now.
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "mine-evidence"

    @model_validator(mode="after")
    def _guard_production_defaults(self) -> Settings:
        """Fail fast rather than ship dev defaults to production (brief §36).

        When ``MM_ENV=prod`` the process refuses to start if any known-dangerous
        development default is still in place. A misconfigured production box
        must fall over loudly at startup, not run silently with the sample
        credentials that ship in ``.env.example``.
        """
        if self.env.lower() != "prod":
            return self
        problems: list[str] = []
        if self.database_url == _DEV_DB_URL or _DEV_DB_CREDENTIALS in self.database_url:
            problems.append(
                "MM_DATABASE_URL still uses the sample dev credentials "
                "(minemonitor:minemonitor); set a real database URL with a strong password"
            )
        if self.bootstrap_admin_user and len(self.bootstrap_admin_password) < 12:
            problems.append(
                "MM_BOOTSTRAP_ADMIN_PASSWORD is set but weak (<12 chars); use a strong "
                "password or leave the bootstrap admin blank and create users via the CLI"
            )
        if problems:
            raise ValueError(
                "refusing to start with MM_ENV=prod and unsafe configuration:\n  - "
                + "\n  - ".join(problems)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()

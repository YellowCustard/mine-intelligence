"""Application configuration, read from the environment (12-factor)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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
    # Broker service account for internal clients (ingestor consumer + the simulator
    # and Teltonika-adapter publishers). When set, clients authenticate with it and
    # the broker can run with allow_anonymous=false. Blank = anonymous (dev/test).
    mqtt_username: str = ""
    mqtt_password: str = ""
    # Strict device provisioning (brief §10/§11): when true, MQTT telemetry is
    # accepted only for assets with an enabled device row. Off by default so a
    # fresh/demo install ingests without provisioning; turn on for a hardened site.
    mqtt_require_registered_device: bool = False
    # Publisher-side store-and-forward spool (crash-safe local buffer).
    spool_path: str = "/tmp/mm-spool.sqlite"

    # Rules.
    offline_threshold_s: int = 600  # silent this long (while active) = offline
    offline_check_interval_s: int = 60  # how often the ingestor scans for offline
    # A fix whose device time is this far AHEAD of server receipt is a bad device clock and
    # is rejected at ingest: stored, it would advance the asset's live-rule watermark into
    # the future and every genuine fix after it would be skipped as "late".
    max_future_skew_s: int = Field(default=120, ge=0)
    default_site_id: str = "kn-zw-01"
    # Haul cycles (brief §9). A load-to-load span longer than this is not haulage (a
    # breakdown, or a truck parked between its last dump and the next shift's first load)
    # and is not reported as a cycle. It also bounds how far back the maintenance tick's
    # incremental recompute reads when late data arrives.
    cycle_max_s: int = Field(default=14400, ge=600)
    # First-party vision (Stage B). A commissioned camera whose health heartbeat is silent this
    # long raises ``camera_offline`` (the "missed" lane).
    vision_camera_stale_s: int = 300
    # Access-control random search (FP-07). 0 = off; otherwise the % of gate passages
    # Mine Monitor deterministically selects for a physical search when the source did not.
    access_search_rate_percent: int = 0
    # A passage selected for search but not completed after this long escalates
    # (search_missed event.v1) on the maintenance tick.
    access_search_grace_s: int = 900
    # Live Dahua gate/turnstile event poller (FP-07). Blank url = off (the simulator and
    # HTTP-ingest path still work). When set, the ingestor polls the gate API each tick and
    # ingests new access events. `token` is the bearer credential; `source_system` labels them.
    access_gate_url: str = ""
    access_gate_token: str = ""
    access_gate_source_system: str = "dahua_gate"
    # On each poll, refetch this far behind the last-seen event so a boundary event is never
    # missed; idempotent ingest drops the overlap.
    access_gate_overlap_s: int = 30
    # Polls failing for this long (the gate API unreachable or erroring) raise one
    # ``source_offline`` alarm, so "no one came through" is never confused with "the feed is down".
    access_gate_offline_s: int = Field(default=600, ge=60)

    # Live DSE generator poller (FP-11). Blank host = off (the simulator and the HTTP-ingest
    # endpoint still work). When set, the ingestor polls the DSE controller(s) over Modbus TCP
    # each tick and ingests readings. The register map is confirmed per DSE model on site before
    # go-live; until then this stays off and the path is exercised simulator-first.
    generator_modbus_host: str = ""
    generator_modbus_port: int = 502

    # Laboratory anomaly bounds (FP-08). A JSON object mapping element → [min, max] in the
    # result's unit; a measured value outside its element's range raises an advisory
    # `lab_anomaly` event.v1 for review (flag, never alter). Blank = anomaly detection off.
    # e.g. {"Au": [0, 100], "Ag": [0, 500]}. Deterministic bounds first; models only later.
    lab_anomaly_bounds: str = ""

    # Retention per data class, in days. 0 = keep forever (brief §4). Generous
    # defaults; a stricter legal answer costs configuration, not architecture.
    retain_positions_days: int = 90
    retain_metrics_days: int = 365
    retain_events_days: int = 365
    # Operational annotations: incidents (closed only), delay classifications, shift
    # handovers. These hold PII-adjacent notes/assignees, so they are pruned too.
    retain_annotations_days: int = 365
    # Audit trail is retained longer than the data it describes — accountability
    # outlives the records (brief §4). 0 = keep forever.
    retain_audit_days: int = 730
    # Vision data classes (build-gate §5). Observations are the high-volume recompute basis —
    # keep at least the longest rule look-back you must support. Health is short-lived telemetry.
    # Operational events and zone rollups follow ``retain_events_days`` / ``retain_metrics_days``.
    retain_vision_observations_days: int = 30
    retain_vision_health_days: int = 30
    retention_interval_s: int = 86_400  # the ingestor runs retention ~daily

    # Personal-data breach notices (CDPA, SI 155 of 2024). The reported obligation is 24 h to
    # the regulator and 72 h to the people affected; these are settings because the legal
    # position is still being confirmed, so a different answer is a configuration change. A
    # breach's deadlines are fixed when it is recorded; changing these moves only later ones.
    breach_regulator_name: str = "POTRAZ"
    breach_regulator_notice_h: int = Field(default=24, ge=1)
    breach_individuals_notice_h: int = Field(default=72, ge=1)
    # Warn this many hours before a notice is due (0 = only the overdue alarm).
    breach_warn_before_h: int = Field(default=6, ge=0)
    # Count deadlines from when the breach was discovered, or from when it occurred (falling
    # back to discovery when the occurrence time is unknown).
    breach_deadline_from: Literal["discovered", "occurred"] = "discovered"

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

    # Notification egress (advisory alerts). Nobody watches a dashboard 24/7 at a
    # remote site, so qualifying events are pushed out via a store-and-forward outbox.
    # Blank min-severity = notifications off (default). Order: info < warning < critical.
    notify_min_severity: str = ""  # "" | "info" | "warning" | "critical"
    notify_webhook_url: str = ""  # POST the event JSON here (blank = no webhook)
    notify_smtp_host: str = ""  # blank = no email
    notify_smtp_port: int = 587
    notify_smtp_user: str = ""
    notify_smtp_password: str = ""
    notify_smtp_starttls: bool = True
    notify_email_from: str = ""
    notify_email_to: str = ""  # comma-separated recipients
    # WhatsApp Cloud API (or a compatible gateway). Blank url or recipients = no WhatsApp.
    # url = the messages endpoint, e.g. https://graph.facebook.com/v20.0/<id>/messages
    notify_whatsapp_url: str = ""
    notify_whatsapp_token: str = ""  # bearer token
    notify_whatsapp_to: str = ""  # comma-separated recipient phone numbers (E.164)
    notify_max_attempts: int = 5  # then the row is marked failed (visible in the queue)
    notify_retry_base_s: int = 60  # exponential backoff base between attempts

    # Teltonika TCP listener (M7). Trackers speak Codec 8/8E over raw TCP; the
    # listener decodes and republishes into MQTT like any other adapter.
    teltonika_host: str = "0.0.0.0"  # noqa: S104 - a device listener binds all interfaces
    teltonika_port: int = 5027
    # Close a Teltonika connection that stops sending for this long, so a half-open
    # socket cannot linger. Trackers report far more often; keep it generous.
    teltonika_idle_timeout_s: int = 900
    # TLS for the listener (plan C-2): PEM certificate chain and private key paths. Both or
    # neither; neither listens in plain TCP, and one without the other refuses to start.
    teltonika_tls_cert: str = ""
    teltonika_tls_key: str = ""

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
        if not self.mqtt_username:
            problems.append(
                "MM_MQTT_USERNAME is not set — the broker would accept anonymous "
                "publishers; set MM_MQTT_USERNAME/MM_MQTT_PASSWORD and run the broker "
                "with allow_anonymous=false (docker/mosquitto.auth.conf)"
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

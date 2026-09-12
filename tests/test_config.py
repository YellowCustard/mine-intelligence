"""Production configuration guards (brief §36): fail fast on dev defaults."""

from __future__ import annotations

import pytest

from minemonitor.config import Settings

_SAFE_DB = "postgresql+psycopg://mmuser:a-strong-secret@db.internal:5432/minemonitor"


def _settings(**over: object) -> Settings:
    # _env_file=None so the repo's .env never bleeds into the unit under test.
    return Settings(_env_file=None, **over)  # type: ignore[call-arg]


def test_dev_defaults_are_allowed_in_dev() -> None:
    # The shipped dev defaults must remain usable when MM_ENV is not prod.
    s = _settings(env="dev")
    assert "minemonitor:minemonitor" in s.database_url


def test_prod_refuses_sample_db_credentials() -> None:
    with pytest.raises(ValueError, match="sample dev credentials"):
        _settings(env="prod")


def test_prod_refuses_dev_credentials_on_custom_host() -> None:
    # Even on a non-localhost host, the shipped password must be rejected.
    with pytest.raises(ValueError, match="sample dev credentials"):
        _settings(
            env="prod",
            database_url="postgresql+psycopg://minemonitor:minemonitor@db.internal:5432/mm",
        )


def test_prod_refuses_weak_bootstrap_password() -> None:
    with pytest.raises(ValueError, match="weak"):
        _settings(
            env="prod",
            database_url=_SAFE_DB,
            bootstrap_admin_user="admin",
            bootstrap_admin_password="short",
        )


def test_prod_refuses_anonymous_broker() -> None:
    # A broker service account is mandatory in prod (no anonymous publishers).
    with pytest.raises(ValueError, match="MM_MQTT_USERNAME"):
        _settings(env="prod", database_url=_SAFE_DB)


def test_prod_starts_with_safe_configuration() -> None:
    s = _settings(env="prod", database_url=_SAFE_DB, mqtt_username="mm-ingestor")
    assert s.env == "prod"
    # A strong bootstrap password is accepted.
    s2 = _settings(
        env="prod",
        database_url=_SAFE_DB,
        mqtt_username="mm-ingestor",
        mqtt_password="a-strong-broker-secret",
        bootstrap_admin_user="admin",
        bootstrap_admin_password="a-strong-admin-secret",
    )
    assert s2.bootstrap_admin_user == "admin"


def test_prod_is_case_insensitive() -> None:
    with pytest.raises(ValueError, match="sample dev credentials"):
        _settings(env="PROD")

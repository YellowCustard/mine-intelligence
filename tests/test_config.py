"""Production configuration guards (brief §36): fail fast on dev defaults."""

from __future__ import annotations

import ast
from pathlib import Path

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


# --- Every setting is read by some code -----------------------------------------------------
#
# A setting that no code reads is worse than no setting: compose passes it, .env.example
# documents it, and an operator who sets it believes something happens. MM_GENERATOR_MODBUS_*
# shipped like that — documented as turning on a DSE Modbus poller that did not exist. A setting
# belongs in ``Settings`` only once the code that reads it does; this check holds that line.

_SRC = Path(__file__).resolve().parents[1] / "src" / "minemonitor"
_CONFIG = _SRC / "config.py"

# Fields no code read when this check was added. Each is a known gap, not a licence: remove the
# name once code reads it (or the field is deleted), and never add one to get a new setting past
# this test.
_KNOWN_UNUSED: dict[str, str] = {
    # Documented for operators to set (COMMISSIONING A2), but each site's timezone comes from
    # sites.timezone; awaiting a decision to wire it into site creation or remove it.
    "default_site_tz": "unread on main as of 2026-10-22; sites.timezone is used instead",
}


def _attribute_reads(tree: ast.AST) -> set[str]:
    """Names read as ``x.name`` or ``getattr(x, "name")``; docstrings and comments don't count."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            names.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            names.add(node.args[1].value)
    return names


def _settings_reads() -> set[str]:
    """Every attribute the package reads outside config.py, plus ``Settings``' own checks.

    A field read only by the class's validators still changes behaviour (``MM_ENV=prod`` turns
    the production guard on), so ``self.<field>`` reads inside ``Settings`` count; nothing else
    in config.py does.
    """
    names: set[str] = set()
    for path in _SRC.rglob("*.py"):
        if path != _CONFIG:
            names |= _attribute_reads(ast.parse(path.read_text(), filename=str(path)))
    for node in ast.parse(_CONFIG.read_text()).body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            names |= {
                n.attr
                for n in ast.walk(node)
                if isinstance(n, ast.Attribute)
                and isinstance(n.ctx, ast.Load)
                and isinstance(n.value, ast.Name)
                and n.value.id == "self"
            }
    return names


def test_every_setting_is_read_by_some_code() -> None:
    unread = sorted(set(Settings.model_fields) - _settings_reads() - set(_KNOWN_UNUSED))
    assert unread == [], (
        f"settings no code reads: {unread}. An operator who sets one believes something "
        "happens. Delete the field (and its compose and .env.example lines) until the code "
        "that reads it lands."
    )


def test_the_unused_allowlist_holds_only_unused_fields() -> None:
    # A stale entry would silently exempt a field that is later left unread again.
    stale = sorted(
        name
        for name in _KNOWN_UNUSED
        if name not in Settings.model_fields or name in _settings_reads()
    )
    assert stale == []

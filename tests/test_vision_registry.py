"""Vision model registry: exact weights, lifecycle, and the licence gate (no CV extra needed)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from minemonitor.storage.models import AuditLog
from minemonitor.vision import registry
from minemonitor.vision.registry import RegistryError
from tests.conftest import ADMIN, VIEWER, make_client

SHA = "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"


def _reg(db: Session, licence: str = "Apache-2.0", version: str = "0.1.1rc0") -> object:
    return registry.register_model(
        db, model_id="yolox-nano", version=version, task="detect", licence=licence,
        weights_sha256=SHA, classes=["person"],
    )


def test_register_and_promote_permissive_model(db_session: Session) -> None:
    _reg(db_session)
    for s in ("validation", "staging", "production", "retired", "production"):
        assert registry.set_status(db_session, "yolox-nano", "0.1.1rc0", s).status == s


@pytest.mark.parametrize(
    "licence", ["AGPL-3.0", "GPL-3.0", "CC-BY-NC-4.0", "Research-only", "non-commercial"]
)
def test_forbidden_licences_refused_at_any_status(db_session: Session, licence: str) -> None:
    with pytest.raises(RegistryError, match="forbidden"):
        _reg(db_session, licence=licence)


def test_unknown_or_weak_copyleft_licence_never_reaches_production(db_session: Session) -> None:
    _reg(db_session, licence="LGPL-2.1")  # registrable for evaluation …
    with pytest.raises(RegistryError, match="not cleared for production"):
        registry.set_status(db_session, "yolox-nano", "0.1.1rc0", "production")
    registry.register_model(
        db_session, model_id="custom", version="1", task="detect", licence="Vendor EULA",
        weights_sha256=SHA, classes=["person"],
    )
    with pytest.raises(RegistryError):
        registry.set_status(db_session, "custom", "1", "production")


def test_versions_immutable_and_hash_required(db_session: Session) -> None:
    _reg(db_session)
    with pytest.raises(RegistryError, match="already registered"):
        _reg(db_session)
    with pytest.raises(RegistryError, match="SHA-256"):
        registry.register_model(
            db_session, model_id="m", version="1", task="detect", licence="MIT",
            weights_sha256="abc", classes=["person"],
        )


def test_metrics_merge(db_session: Session) -> None:
    _reg(db_session)
    registry.record_metrics(db_session, "yolox-nano", "0.1.1rc0", {"cpu_4c": {"fps": 11.2}})
    row = registry.record_metrics(
        db_session, "yolox-nano", "0.1.1rc0", {"jetson": {"fps": 40.0}}
    )
    assert set(row.metrics or {}) == {"cpu_4c", "jetson"}


def test_api_admin_only_and_audited(db_session: Session) -> None:
    body = {"model_id": "yolox-nano", "version": "0.1.1rc0", "licence": "Apache-2.0",
            "weights_sha256": SHA, "classes": ["person"], "input_size": [416, 416]}
    viewer = make_client(db_session, VIEWER)
    assert viewer.post("/api/v1/vision/models", json=body).status_code == 403
    admin = make_client(db_session, ADMIN)
    r = admin.post("/api/v1/vision/models", json=body)
    assert r.status_code == 201 and r.json()["production_eligible"] is True
    assert admin.post("/api/v1/vision/models", json=body).status_code == 409
    bad = {**body, "model_id": "yolov8n", "licence": "AGPL-3.0"}
    assert admin.post("/api/v1/vision/models", json=bad).status_code == 409
    s = admin.post("/api/v1/vision/models/yolox-nano/0.1.1rc0/status", json={"status": "staging"})
    assert s.status_code == 200 and s.json()["status"] == "staging"
    m = admin.post("/api/v1/vision/models/yolox-nano/0.1.1rc0/metrics", json={"cpu": {"fps": 9}})
    assert m.json()["metrics"] == {"cpu": {"fps": 9}}
    assert [x["model_id"] for x in viewer.get("/api/v1/vision/models").json()] == ["yolox-nano"]
    actions = set(db_session.execute(select(AuditLog.action)).scalars().all())
    assert {"vision.model.register", "vision.model.status", "vision.model.metrics"} <= actions

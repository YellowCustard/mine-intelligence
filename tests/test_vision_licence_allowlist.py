"""The registry's licence gate is an allowlist of audited models and sizes (plan C-3).

The gate trusted the licence string a person typed. RF-DETR's repository is Apache-2.0, but its
XL and 2XL detection weights are PML 1.0 (``rfdetr_plus``), so "rfdetr-2xl, Apache-2.0" reached
``production``. Now a model reaches production only when its architecture is an audited model
and size whose audited licence is the one declared, and a declared licence that contradicts the
audit is refused at registration.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from minemonitor.storage.models import VisionModelRecord
from minemonitor.vision import registry
from minemonitor.vision.edge.cli import MODELS
from minemonitor.vision.registry import RegistryError
from tests.conftest import ADMIN, make_client

SHA = "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"


def _reg(
    db: Session, architecture: str | None, licence: str, model_id: str = "detector"
) -> VisionModelRecord:
    return registry.register_model(
        db,
        model_id=model_id,
        version="1",
        task="detect",
        licence=licence,
        weights_sha256=SHA,
        classes=["person"],
        architecture=architecture,
    )


def test_a_plus_size_declared_apache_is_refused(db_session: Session) -> None:
    with pytest.raises(RegistryError, match="PML 1.0"):
        _reg(db_session, "rfdetr-2xl", "Apache-2.0")


@pytest.mark.parametrize(
    ("architecture", "audited"),
    [
        ("rfdetr-xl", "PML 1.0"),
        ("rfdetr-pico", "PML 1.0"),
        ("d-fine-l-obj365", "Objects365"),
        ("d-fine-x-obj2coco", "Objects365"),
    ],
)
def test_a_declared_licence_that_contradicts_the_audit_is_refused(
    db_session: Session, architecture: str, audited: str
) -> None:
    with pytest.raises(RegistryError, match=audited):
        _reg(db_session, architecture, "Apache-2.0")


@pytest.mark.parametrize(
    "key",
    [
        "yolov8n",
        "yolo11s",
        "yolov5m",
        "fastsam-s",
        "yoloe-v8s",
        "yolov7-tiny",
        "yolo-nas-s",
        "deimv2-s",
        "edgecrafter-s",
        "depth-anything-v2-large",
    ],
)
def test_a_known_forbidden_model_is_refused_whatever_licence_is_declared(
    db_session: Session, key: str
) -> None:
    with pytest.raises(RegistryError, match="forbidden"):
        _reg(db_session, key, "MIT")
    # Named only in the model id, it is refused just the same.
    with pytest.raises(RegistryError, match="forbidden"):
        _reg(db_session, None, "MIT", model_id=key)


def test_matching_ignores_case_and_surrounding_space(db_session: Session) -> None:
    with pytest.raises(RegistryError, match="PML 1.0"):
        _reg(db_session, " RFDETR-2XL ", "Apache-2.0")


def test_an_unaudited_model_is_registrable_but_never_reaches_production(
    db_session: Session,
) -> None:
    _reg(db_session, "rtdetrv2-r50", "Apache-2.0")  # registrable for evaluation …
    with pytest.raises(RegistryError, match="not an audited model and size"):
        registry.set_status(db_session, "detector", "1", "production")
    assert registry.set_status(db_session, "detector", "1", "staging").status == "staging"


def test_a_correctly_declared_restricted_size_is_registrable_but_never_reaches_production(
    db_session: Session,
) -> None:
    row = _reg(db_session, "rfdetr-xl", "PML 1.0")
    assert row.status == "experimental"
    with pytest.raises(RegistryError, match="PML 1.0"):
        registry.set_status(db_session, "detector", "1", "production")


@pytest.mark.parametrize(
    ("architecture", "licence"),
    [
        ("yolox-nano", "Apache-2.0"),
        ("yolox-x", "Apache-2.0"),
        ("rfdetr-nano", "Apache-2.0"),
        ("rfdetr-large", "Apache-2.0"),
        ("dinov2-vitb14", "Apache-2.0"),
        ("dinov3-vitl16", "DINOv3 License"),
    ],
)
def test_an_audited_size_at_its_audited_licence_reaches_production(
    db_session: Session, architecture: str, licence: str
) -> None:
    _reg(db_session, architecture, licence)
    assert registry.set_status(db_session, "detector", "1", "production").status == "production"


def test_dinov3_satellite_weights_are_not_cleared(db_session: Session) -> None:
    # VISION_MODEL_LICENSES.md §1a: the SAT-493M checkpoints may carry separate terms.
    _reg(db_session, "dinov3-vit7b16-sat493m", "DINOv3 License")
    with pytest.raises(RegistryError, match="not an audited model and size"):
        registry.set_status(db_session, "detector", "1", "production")


def test_without_an_architecture_the_model_id_names_the_model(db_session: Session) -> None:
    _reg(db_session, None, "Apache-2.0", model_id="yolox-s")
    assert registry.set_status(db_session, "yolox-s", "1", "production").status == "production"
    _reg(db_session, None, "Apache-2.0", model_id="site-detector")
    with pytest.raises(RegistryError, match="not an audited model and size"):
        registry.set_status(db_session, "site-detector", "1", "production")


def test_a_row_registered_before_the_gate_is_checked_against_the_audit(
    db_session: Session,
) -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    db_session.add(
        VisionModelRecord(
            id="detector@1",
            model_id="detector",
            version="1",
            task="detect",
            architecture="rfdetr-2xl",
            licence="Apache-2.0",
            weights_sha256=SHA,
            classes=["person"],
            status="staging",
            created_at=now,
            updated_at=now,
        )
    )
    db_session.flush()
    with pytest.raises(RegistryError, match="PML 1.0"):
        registry.set_status(db_session, "detector", "1", "production")
    row = registry.get_model(db_session, "detector", "1")
    assert row is not None
    shown = registry.model_dict(row)
    assert shown["production_eligible"] is False
    assert "PML 1.0" in shown["production_block"]


def test_every_model_the_edge_fetches_is_cleared() -> None:
    for name, m in MODELS.items():
        assert registry.production_block(name, m["licence"]) is None, name


def test_the_api_refuses_a_contradicted_licence_and_says_why(db_session: Session) -> None:
    admin = make_client(db_session, ADMIN)
    body = {
        "model_id": "rfdetr-xl",
        "version": "1.11.2",
        "architecture": "rfdetr-xl",
        "licence": "Apache-2.0",
        "weights_sha256": SHA,
        "classes": ["person"],
    }
    r = admin.post("/api/v1/vision/models", json=body)
    assert r.status_code == 409 and "PML 1.0" in r.json()["detail"]
    small = {**body, "model_id": "rfdetr-s", "architecture": "rfdetr-small"}
    ok = admin.post("/api/v1/vision/models", json=small)
    assert ok.status_code == 201
    assert ok.json()["production_eligible"] is True and ok.json()["production_block"] is None

"""Every timestamp a caller can send must carry its UTC offset (audit N1, brief §12).

The site runs on UTC+2. A naive timestamp is ambiguous: stored as given, a delay or a
fuel dispense typed in local time lands two hours off and nothing says so. Position
fixes already reject one (#44); this holds every published contract and every API
input to the same rule, so a new field or endpoint cannot quietly reopen it.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import types
import typing
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, TypeAdapter, ValidationError
from sqlalchemy.orm import Session

import minemonitor.api.routers
import minemonitor.contracts
from tests.conftest import ADMIN, make_client

NAIVE = "2026-09-05T11:42:07"


def _mentions_datetime(annotation: Any) -> bool:
    if annotation in (datetime, AwareDatetime):
        return True
    return any(_mentions_datetime(a) for a in typing.get_args(annotation))


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        yield annotation
    for a in typing.get_args(annotation):
        yield from _models_in(a)


def _accepts_naive(annotation: Any) -> bool:
    try:
        value = TypeAdapter(annotation).validate_python(NAIVE)
    except ValidationError:
        return False
    return isinstance(value, datetime)


def _naive_fields(model: type[BaseModel], seen: set[type[BaseModel]]) -> Iterator[str]:
    if model in seen:
        return
    seen.add(model)
    for name, field in model.model_fields.items():
        if _mentions_datetime(field.annotation) and _accepts_naive(field.annotation):
            yield f"{model.__module__}.{model.__qualname__}.{name}"
        for sub in _models_in(field.annotation):
            yield from _naive_fields(sub, seen)


def _modules(package: types.ModuleType) -> Iterator[types.ModuleType]:
    for info in pkgutil.iter_modules(package.__path__):
        yield importlib.import_module(f"{package.__name__}.{info.name}")


def _contract_models() -> Iterator[type[BaseModel]]:
    for module in _modules(minemonitor.contracts):
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseModel) and obj.__module__ == module.__name__:
                yield obj


def _api_routes() -> Iterator[APIRoute]:
    for module in _modules(minemonitor.api.routers):
        router = getattr(module, "router", None)
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute):
                yield route


def test_no_contract_accepts_a_naive_timestamp() -> None:
    seen: set[type[BaseModel]] = set()
    naive = [f for m in _contract_models() for f in _naive_fields(m, seen)]
    assert naive == []


def _route_params(dependant: Any) -> Iterator[Any]:
    yield from dependant.path_params
    yield from dependant.query_params
    yield from dependant.header_params
    yield from dependant.body_params
    for sub in dependant.dependencies:
        yield from _route_params(sub)


def test_no_api_input_accepts_a_naive_timestamp() -> None:
    naive: list[str] = []
    seen: set[type[BaseModel]] = set()
    routes = list(_api_routes())
    assert len(routes) > 100  # the walk reached the routers
    for route in routes:
        for param in _route_params(route.dependant):
            annotation = param.field_info.annotation
            where = f"{','.join(sorted(route.methods))} {route.path} {param.name}"
            if _mentions_datetime(annotation) and _accepts_naive(annotation):
                naive.append(where)
            for model in _models_in(annotation):
                naive += [f"{where}: {f}" for f in _naive_fields(model, seen)]
    assert naive == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/sites/kn-zw-01/delays", {"category": "breakdown", "start_ts": NAIVE, "end_ts": NAIVE}),
        ("/sites/kn-zw-01/fuel/transactions", {"litres": 10, "ts": NAIVE}),
        ("/sites/kn-zw-01/fuel/tank-readings", {"tank_id": "t1", "level_l": 10, "ts": NAIVE}),
    ],
)
def test_a_naive_timestamp_is_rejected_not_stored(
    db_session: Session, path: str, body: dict[str, Any]
) -> None:
    # These used to answer 201 and store the time as if it were UTC.
    r = make_client(db_session, ADMIN).post(path, json=body)
    assert r.status_code == 422, r.text


def test_a_naive_report_time_is_rejected(db_session: Session) -> None:
    r = make_client(db_session, ADMIN).get(f"/sites/kn-zw-01/reports/shift?at={NAIVE}")
    assert r.status_code == 422, r.text


def test_an_offset_timestamp_is_still_accepted(db_session: Session) -> None:
    r = make_client(db_session, ADMIN).post(
        "/sites/kn-zw-01/fuel/transactions", json={"litres": 10, "ts": NAIVE + "+02:00"}
    )
    assert r.status_code == 201, r.text
    assert datetime.fromisoformat(r.json()["ts"]) == datetime(2026, 9, 5, 9, 42, 7, tzinfo=UTC)

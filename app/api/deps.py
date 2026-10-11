"""FastAPI dependencies. Everything hangs off ``app.state``, set in ``create_app``."""
from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from ..config import Settings
from ..services.storage import LocalStorage
from ..services.worker import BackgroundWorker


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_storage(request: Request) -> LocalStorage:
    return request.app.state.storage


def get_worker(request: Request) -> BackgroundWorker:
    return request.app.state.worker


def get_session(request: Request) -> Iterator[Session]:
    """One session per request, closed when the response is done."""
    with request.app.state.session_factory() as session:
        yield session

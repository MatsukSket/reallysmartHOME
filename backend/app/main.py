"""Точка входа: запуск серверной части решателя.

    .venv/bin/uvicorn app.main:app --reload
"""

from .api import app

__all__ = ["app"]
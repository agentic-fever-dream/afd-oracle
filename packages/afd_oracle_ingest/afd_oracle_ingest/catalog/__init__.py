"""Candidate catalogue (see docs/mining/07-catalogue.md): ~190 deterministic candidate kinds beyond coupling,
graveyard and pattern. Builders live in one module per family and register themselves on import."""
from __future__ import annotations

import importlib
import pkgutil

from .base import REGISTRY, Ctx, Hit, candidate, spec

_LOADED = False
NOT_BUILDERS = {"base", "difflines", "runner", "packets", "enrich"}
IMPORT_ERRORS: dict[str, str] = {}  # module -> error; a broken family module must never break ingestion


def load_all() -> dict:
    """Import every builder module in this package (idempotent). Import failures are recorded, not raised."""
    global _LOADED
    if not _LOADED:
        for m in pkgutil.iter_modules(__path__):
            if m.name in NOT_BUILDERS or m.name.startswith("_"):
                continue
            try:
                importlib.import_module(f"{__name__}.{m.name}")
            except Exception as e:  # noqa: BLE001
                IMPORT_ERRORS[m.name] = f"{type(e).__name__}: {e}"
        _LOADED = True
    return REGISTRY


__all__ = ["REGISTRY", "IMPORT_ERRORS", "Ctx", "Hit", "candidate", "spec", "load_all"]

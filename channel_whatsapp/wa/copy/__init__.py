from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from typing import Any
import yaml

_COPY_DIR = Path(__file__).parent
_SUPPORTED_LOCALES = {"en", "hi", "pa"}
_DEFAULT_LOCALE = "en"


@lru_cache(maxsize=8)
def load_copy(locale: str) -> dict[str, Any]:
    if locale not in _SUPPORTED_LOCALES:
        locale = _DEFAULT_LOCALE
    path = _COPY_DIR / f"{locale}.yaml"
    if not path.exists():
        if locale != _DEFAULT_LOCALE:
            return load_copy(_DEFAULT_LOCALE)
        raise FileNotFoundError(f"Default copy file not found: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get(locale: str, key: str, **kwargs: Any) -> str:
    copy = load_copy(locale)
    template = copy.get(key) or load_copy(_DEFAULT_LOCALE)[key]
    if kwargs:
        return template.format(**kwargs)
    return template

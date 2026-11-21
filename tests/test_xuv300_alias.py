"""0252 (2026-11-21): "XUV 300" and "3XO" name the XUV 3XO; their EV spellings the EV."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from audit_core.uc03_sku_words import pick_model, raw_tokens

_MIGRATION = Path(__file__).resolve().parents[1] / "migrations/versions/0252_xuv300_3xo_model_aliases.py"
_spec = importlib.util.spec_from_file_location("m0252", _MIGRATION)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)  # type: ignore[union-attr]

MODELS = ["XUV3XO", "XUV3XO EV", "XUV 7XO", "XEV9S"]
ALIASES = [("XUV 700", "XUV 7XO"), ("XUV 3XO", "XUV3XO"), *_m._ALIASES.items()]


@pytest.mark.parametrize(
    "text,model",
    [
        ("XUV 300", "XUV3XO"),
        ("XUV300 AX7L PAT", "XUV3XO"),
        ("XUV 300 W8(O) D MT", "XUV3XO"),
        ("3XO AX5(P)MT", "XUV3XO"),
        ("3XO EV AX7L", "XUV3XO EV"),
        ("XUV 300 EV AX5", "XUV3XO EV"),
        ("XUV 700 AX5", "XUV 7XO"),
    ],
)
def test_old_and_short_names_pick_the_3xo(text: str, model: str) -> None:
    pick = pick_model(raw_tokens(text), model_names=MODELS, aliases=ALIASES)
    assert pick is not None and pick.model_names[0] == model

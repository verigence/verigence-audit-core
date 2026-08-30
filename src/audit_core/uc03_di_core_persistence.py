from __future__ import annotations

_installed = False


def install_uc03_di_core_persistence() -> None:
    """Mark UC03 DI-to-Core persistence wiring as installed.

    Review Confirm now invokes the all-field materializer explicitly. Keeping this
    installer as a no-op compatibility hook avoids a second monkey-patched persistence
    path while existing V2 bootstrap code can continue calling it safely.
    """

    global _installed
    if _installed:
        return
    _installed = True

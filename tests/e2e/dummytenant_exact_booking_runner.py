"""Exact Booking Form E2E runner.

The Audit Core refresh endpoint reads DI fields only after DI reaches CONFIRMED.
Gemini processing is asynchronous, so this DEV E2E waits before the first
refresh instead of treating DI's documented E008 (not yet confirmed) as a
failed extraction.
"""
from __future__ import annotations

import time

import base_e2e

_ORIGINAL_POST = base_e2e.httpx.Client.post
_first_refresh = True


def _post_after_async_window(self, url, *args, **kwargs):
    global _first_refresh
    if _first_refresh and str(url).endswith("/refresh"):
        _first_refresh = False
        print("DUMMYTENANT_WAITING_FOR_ASYNC_DI=PASS|70s", flush=True)
        time.sleep(70)
    return _ORIGINAL_POST(self, url, *args, **kwargs)


base_e2e.httpx.Client.post = _post_after_async_window
base_e2e.main()

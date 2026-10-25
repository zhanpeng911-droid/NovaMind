"""Dispatch native WinForms mutations without blocking a Python worker."""
from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


def post_to_ui(window, operation: Callable[[], None]) -> bool:
    native = window.native
    if native is None or native.IsDisposed or not native.IsHandleCreated:
        return False

    def apply() -> None:
        if native.IsDisposed:
            return
        try:
            operation()
        except Exception:
            logger.exception("native desktop operation failed")

    if native.InvokeRequired:
        from System import Action

        # Do not hold the GIL/a shell lock while waiting for the UI thread.
        # In particular, a direct WinForms property setter can synchronously
        # send a window message and deadlock against a Python UI callback.
        native.BeginInvoke(Action(apply))
    else:
        apply()
    return True

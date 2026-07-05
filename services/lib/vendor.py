#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Vendored third-party packages (git submodules under external/).

Usage:
    from lib.vendor import add_vendor_path
    add_vendor_path("pybeoplay")
    from pybeoplay import BeoPlay
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def add_vendor_path(name: str) -> None:
    """Make a vendored package under external/<name> importable.

    No-op when the submodule is not checked out — callers should guard the
    subsequent import with try/except ImportError so a missing submodule
    degrades gracefully instead of crashing the service.
    """
    path = os.path.join(_REPO_ROOT, "external", name)
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

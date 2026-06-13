"""Local pytest config for the sdk_host_poc spike.

Registers the ``live`` marker so ``@pytest.mark.live`` does not raise
``PytestUnknownMarkWarning`` (and, under ``-W error``, an error), and makes
``live`` tests opt-in: they are SKIPPED in a normal run and execute only when
``-m live`` is explicitly requested.  Registering here keeps the behaviour
scoped to the spike — no edit to atelier-wide ``pyproject.toml`` is required.
"""

from __future__ import annotations

import pytest


def pytest_configure(config):
    """Register the spike-local ``live`` marker."""
    config.addinivalue_line(
        "markers",
        "live: drives the real claude CLI binary (subscription auth, no API key); "
        "opt-in (skipped unless '-m live' is requested), and skipped anyway when "
        "the binary is not on PATH.",
    )


def pytest_collection_modifyitems(config, items):
    """Skip ``live``-marked tests unless ``-m live`` was explicitly requested.

    This makes the live agent smoke OPT-IN: a plain ``pytest spikes/sdk_host_poc/``
    run reports it as skipped (never executes the body), and only ``-m live``
    actually runs it.  Combined with the test's own ``skipif`` on
    ``claude_cli_available()``, the live path is doubly gated.
    """
    markexpr = config.getoption("-m", default="")
    if "live" in markexpr:
        return  # operator explicitly asked for live tests — let them run
    skip_live = pytest.mark.skip(reason="live test is opt-in (run with '-m live')")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)

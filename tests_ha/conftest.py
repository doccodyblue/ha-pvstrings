"""Fixtures for the Home-Assistant-backed schema tests.

These run against a real ``homeassistant`` install (see requirements-ha.txt),
which the fast ``tests/`` suite deliberately does not need.  Their whole
purpose is the class of bug that only HA's own validators can catch: selector
configurations that look fine in Python and are rejected the moment a form is
rendered or submitted.

Three real outages were caused by exactly that and produced a bare
"400: Bad Request" with no log line at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeConfig:
    latitude = 53.5
    longitude = 10.0
    elevation = 5


class _FakeHass:
    """Just enough of ``hass`` for the schema builders."""

    config = _FakeConfig()


@pytest.fixture
def hass() -> _FakeHass:
    return _FakeHass()

from __future__ import annotations

from collections.abc import Iterator

import pytest
import respx


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router

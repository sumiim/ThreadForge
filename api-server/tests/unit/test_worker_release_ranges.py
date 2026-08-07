from __future__ import annotations

import pytest

from threadforge_api.api.routers.worker_releases import _parse_byte_range


def test_worker_release_range_supports_resume_and_bounded_ranges():
    assert _parse_byte_range("", 100) is None
    assert _parse_byte_range("bytes=25-", 100) == (25, 99)
    assert _parse_byte_range("bytes=25-50", 100) == (25, 50)
    assert _parse_byte_range("bytes=-10", 100) == (90, 99)
    assert _parse_byte_range("bytes=0-999", 100) == (0, 99)


@pytest.mark.parametrize(
    "value",
    ["items=0-1", "bytes=", "bytes=100-", "bytes=20-10", "bytes=0-1,4-5"],
)
def test_worker_release_range_rejects_invalid_or_unsatisfiable_values(value):
    with pytest.raises(ValueError):
        _parse_byte_range(value, 100)

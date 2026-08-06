from __future__ import annotations

import re

import pytest
from esan_gbos.domain.naming import make_gbos_name
from esan_gbos.domain.revision import RevisionConflict, next_revision


def test_business_name_uses_prefix_and_crockford_ulid() -> None:
    name = make_gbos_name("SAM")

    assert re.fullmatch(r"SAM-[0-9A-HJKMNP-TV-Z]{26}", name)


def test_names_are_unique() -> None:
    assert make_gbos_name("WRK") != make_gbos_name("WRK")


def test_matching_revision_increments() -> None:
    assert next_revision(expected=7, current=7) == 8


def test_stale_revision_is_rejected() -> None:
    with pytest.raises(RevisionConflict, match="expected revision 6, current revision 7"):
        next_revision(expected=6, current=7)

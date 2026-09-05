"""Roblox's client-side username rules.

Every rule here saves a request, so a regression is silent: the checker just
starts paying for answers it already had, or throws away names that were
legal. Both cases map to a documented validator response code.
"""

import pytest

from config import MAX_LEN, MIN_LEN, is_valid_username


@pytest.mark.parametrize("name", [
    "abc",                    # minimum length
    "a" * MAX_LEN,            # maximum length
    "Cool",                   # uppercase is legal
    "a_b",                    # one interior underscore
    "x1y2z",                  # digits
    "0123",                   # all digits
])
def test_accepts_legal_names(name):
    assert is_valid_username(name)


@pytest.mark.parametrize("name, rule", [
    ("ab", "code 3 - shorter than the minimum"),
    ("a" * (MAX_LEN + 1), "code 3 - longer than the maximum"),
    ("", "code 3 - empty"),
    ("_ab", "code 4 - leading underscore"),
    ("ab_", "code 4 - trailing underscore"),
    ("a_b_c", "code 5 - two underscores"),
    ("a-b", "code 7 - hyphen"),
    ("a b", "code 7 - space"),
    ("café", "code 7 - non-ascii"),
    ("ab\n", "code 7 - trailing newline from an unstripped file line"),
])
def test_rejects_illegal_names(name, rule):
    assert not is_valid_username(name), rule


def test_min_len_boundary_is_not_off_by_one():
    assert not is_valid_username("a" * (MIN_LEN - 1))
    assert is_valid_username("a" * MIN_LEN)

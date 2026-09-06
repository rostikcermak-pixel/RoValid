"""Username generation.

The generator feeds every proxyless run, so a bias here quietly wastes the
whole thing: you spend an hour asking Roblox about a corner of the namespace
instead of the namespace.
"""

import collections
import random

import pytest

from config import is_valid_username
from wizard import _generate, _underscored


@pytest.mark.parametrize("length", [3, 4, 5, 6])
@pytest.mark.parametrize("underscore", [False, True])
def test_names_are_the_length_that_was_asked_for(length, underscore):
    # An underscore used to be inserted into a full-length name, so ticking
    # the box turned a request for five characters into six-character names.
    # Roblox counts the underscore against its own limit; so does this.
    names = _generate(length, 400, allow_underscore=underscore)
    assert names
    wrong = {n for n in names if len(n) != length}
    assert not wrong, f"expected {length} characters, got {sorted(wrong)[:5]}"


@pytest.mark.parametrize("length", [3, 4, 5, 6])
def test_everything_generated_is_a_legal_username(length):
    for name in _generate(length, 400, allow_underscore=True):
        assert is_valid_username(name), name


@pytest.mark.parametrize("length", [3, 4, 5, 6])
def test_underscore_names_obey_roblox_placement_rules(length):
    for name in _generate(length, 600, allow_underscore=True):
        if "_" in name:
            assert name.count("_") == 1, name
            assert not name.startswith("_") and not name.endswith("_"), name


@pytest.mark.parametrize("length", [3, 4, 5])
def test_underscore_names_are_not_all_crammed_into_one_letter(length):
    # The regression this file exists for. The old generator built underscore
    # names from the first 20,000 entries of a lexicographic product; since
    # 36^3 = 46,656 four-character names start with each letter, every single
    # underscore name at length 4 began with an "a", and length 3 never got
    # past "p".
    random.seed(11)
    names = [n for n in _generate(length, 4000, allow_underscore=True)
             if "_" in n]
    assert names, "no underscore names generated at all"
    firsts = collections.Counter(n[0] for n in names)
    assert len(firsts) > 12, (
        f"underscore names start with only {len(firsts)} distinct characters: "
        f"{sorted(firsts)}"
    )


def test_no_underscores_when_they_were_not_asked_for():
    for length in (3, 4, 5):
        assert not any("_" in n for n in _generate(length, 500, False))


@pytest.mark.parametrize("length", [3, 4, 5])
def test_generated_names_are_unique(length):
    names = _generate(length, 800, allow_underscore=True)
    assert len(names) == len(set(names))


def test_a_request_larger_than_the_space_returns_the_whole_space():
    # 36^3 plain plus 36^2 underscore forms, and not one of them twice.
    names = _generate(3, 10_000_000, allow_underscore=True)
    assert len(names) == len(set(names))
    assert len(names) == 36**3 + 36**2


@pytest.mark.parametrize("stem, length", [("ab", 3), ("abc", 4), ("abcd", 5)])
def test_underscored_lands_inside_and_keeps_the_length(stem, length):
    for _ in range(50):
        out = _underscored(stem)
        assert len(out) == length
        assert out.count("_") == 1
        assert not out.startswith("_") and not out.endswith("_")

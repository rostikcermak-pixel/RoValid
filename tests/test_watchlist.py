"""The watchlist.

A release is only caught if the name was on the list, so determinism across
runs is the property that matters most - a shuffled list watches a different
slice every time and catches nothing.
"""

from config import is_valid_username
from watchlist import build


def test_is_deterministic():
    assert build([3, 4, 5]) == build([3, 4, 5])


def test_every_entry_is_a_legal_username():
    for name in build([3, 4, 5]):
        assert is_valid_username(name), name


def test_lengths_are_honoured():
    for length in (3, 4, 5):
        assert all(len(n) == length for n in build([length]))


def test_no_duplicates_across_lengths():
    names = build([3, 4, 5])
    assert len(names) == len(set(names))


def test_cap_is_per_length():
    names = build([3, 4, 5], cap_per_length=10)
    assert len(names) == 30


def test_dictionary_words_come_before_generated_shapes():
    # The cap is what makes this load-bearing: words have to survive it.
    from rarity import WORDS
    five = build([5], cap_per_length=5)
    assert all(n in WORDS for n in five)


def test_lengths_outside_roblox_rules_are_dropped():
    assert build([2]) == []
    assert build([21]) == []

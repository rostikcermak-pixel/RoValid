"""Tier scoring.

`rate` decides the order of the live board, so a change here silently
reshuffles what visitors see first.
"""

import pytest

from rarity import TIERS, WORDS, rate


@pytest.mark.parametrize("name, tier", [
    ("blaze", "legendary"),   # dictionary word
    ("aaa", "legendary"),     # single repeated character
    ("mavlo", "epic"),        # letters only, reads like a name
    ("xkqzf", "rare"),        # letters only, unpronounceable
    ("1221", "solid"),        # palindrome with digits
    ("p0xmt", "junk"),        # digits, three consonants in a row
])
def test_tiers(name, tier):
    assert rate(name)[0] == tier


def test_weight_matches_tier():
    for name in ("blaze", "mavlo", "xkqzf", "1221", "p0xmt"):
        tier, weight = rate(name)
        assert weight == TIERS[tier]


def test_rating_is_case_insensitive():
    assert rate("BLAZE") == rate("blaze")


def test_tier_weights_are_strictly_ordered():
    order = ["legendary", "epic", "rare", "solid", "plain", "junk"]
    weights = [TIERS[t] for t in order]
    assert weights == sorted(weights, reverse=True)
    assert len(set(weights)) == len(weights)


def test_every_word_is_lowercase_ascii_and_in_range():
    for word in WORDS:
        assert word.isascii() and word.isalpha() and word.islower(), word
        assert 3 <= len(word) <= 5, word


def test_no_truncated_non_words_survive():
    # These three shipped in the legendary tier, produced by slice hacks in
    # the word list - "wolf"[:3], "fierce"[:5], "spiral"[:5] - and from there
    # went onto the watchlist, which re-checks every entry on every run.
    for junk in ("wol", "fierc", "spira"):
        assert junk not in WORDS
    # The two slices that did land on real words are still there as words.
    assert {"pear", "rave", "wolf"} <= WORDS

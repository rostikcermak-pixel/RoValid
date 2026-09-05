"""Tier scoring.

`rate` decides the order of the live board, so a change here silently
reshuffles what visitors see first.
"""

import pytest

from rarity import TIERS, WORDS, is_noteworthy, rate


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


# ── board policy ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "aaaaa",        # single repeated character -> legendary
    "12321",        # palindrome -> solid
    "xkqzf",        # letters only, which no five-char find has ever been
    "blaze",        # a word
    "mud5c", "d6bug", "box8j", "6cowv", "4vhit", "9fixq", "w3jam",
])
def test_noteworthy_names_reach_the_board(name):
    assert is_noteworthy(name)


@pytest.mark.parametrize("name", [
    "p0xmt",        # three consonants, two digits, no word
    "5jk0v", "40rhx", "3pwc1", "9pruc", "gwm4o",
    "6p8zz",        # too many digits even though it has a shape
])
def test_licence_plates_do_not(name):
    assert not is_noteworthy(name)


def test_one_digit_is_the_limit_even_with_a_word():
    # "cow" survives in both; only the single-digit one is worth showing.
    assert is_noteworthy("6cowv")
    assert not is_noteworthy("6c0wv")


def test_the_filter_is_case_insensitive():
    assert is_noteworthy("MUD5C") == is_noteworthy("mud5c")


def test_it_is_selective_on_real_data():
    # Sanity bound, not an exact figure: measured 29 of 21,069 real finds.
    # If a change ever makes this loose enough to pass a licence plate, the
    # board fills with junk again, which is the bug this exists to prevent.
    from rarity import _pronounceable
    plates = ["5jk0v", "40rhx", "3pwc1", "9pruc", "plc9p", "4k8gm", "w97m1"]
    assert not any(is_noteworthy(p) for p in plates)
    assert _pronounceable("mavlo")

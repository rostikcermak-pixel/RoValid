#!/usr/bin/env python3
"""The names worth waiting for.

Random generation is the wrong tool for finding a good name. Measured: a
one-minute sample turned up 183 free five-character names and not one was
digit-free - the good ones are not sitting unclaimed waiting to be
discovered, they are taken, and they come back when someone renames.

So this is the opposite approach. Build a list of names people would
actually want, re-check that same list every run, and catch the moment one
is released. A few thousand names is a handful of batch requests, which
makes watching cheap enough to do forever.
"""

from __future__ import annotations

import itertools

from config import MAX_LEN, MIN_LEN, is_valid_username
from rarity import WORDS

VOWELS = "aeiou"
# Consonants that start and end real words cleanly. Skipping q, x and the
# awkward finals keeps the generated half of the list to shapes that read
# like names rather than filler.
LEAD = "bcdfghjklmnprstvwz"
TAIL = "bcdklmnprstvxz"


def _pronounceable(length: int) -> list[str]:
    """Consonant-vowel skeletons of *length*: mav, zuri, blaze-shaped."""
    out: list[str] = []
    if length == 3:                       # CVC
        for c1, v, c2 in itertools.product(LEAD, VOWELS, TAIL):
            out.append(c1 + v + c2)
    elif length == 4:                     # CVCV
        for c1, v1, c2, v2 in itertools.product(LEAD, VOWELS, LEAD, VOWELS):
            out.append(c1 + v1 + c2 + v2)
    elif length == 5:                     # CVCVC
        for c1, v1, c2, v2, c3 in itertools.product(
            LEAD, VOWELS, LEAD, VOWELS, TAIL,
        ):
            out.append(c1 + v1 + c2 + v2 + c3)
    return out


def build(lengths: list[int], cap_per_length: int = 1200) -> list[str]:
    """Names to watch, best first, capped so a run stays affordable.

    Dictionary words come first because they are the ones anyone would
    recognise; generated shapes fill the rest. The cap matters: every name
    here is re-checked on every single run, forever.
    """
    seen: set[str] = set()
    out: list[str] = []

    for length in lengths:
        if not (MIN_LEN <= length <= MAX_LEN):
            continue
        bucket: list[str] = []

        for word in sorted(WORDS):
            if len(word) == length and word not in seen:
                seen.add(word)
                bucket.append(word)

        # Deterministic order rather than a shuffle, so the same names are
        # watched run after run instead of a different slice each time - a
        # release is only caught if the name was actually on the list.
        for name in _pronounceable(length):
            if len(bucket) >= cap_per_length:
                break
            if name in seen or not is_valid_username(name):
                continue
            seen.add(name)
            bucket.append(name)

        out.extend(bucket[:cap_per_length])
    return out


if __name__ == "__main__":
    names = build([3, 4, 5])
    print(f"{len(names):,} names on the watchlist")
    for n in (3, 4, 5):
        sub = [x for x in names if len(x) == n]
        print(f"  len {n}: {len(sub):,}  e.g. {' '.join(sub[:8])}")

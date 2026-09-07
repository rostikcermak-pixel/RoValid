#!/usr/bin/env python3
"""Draws docs/og.png - the picture Discord, Twitter and iMessage show when
somebody pastes the board's link.

The board's whole pitch is a number that keeps moving, and a static image
throws that away: a link shared today would advertise whatever the count was
the day the file was committed. So the hunter redraws this card from the same
hits.json it just wrote, and every share carries the live figure.

Written against the stdlib on purpose. The hunt runs on a GitHub runner that
installs requirements.txt and nothing else, and a social card is not worth
adding Pillow - and a wheel that fails to build - to the one job that has to
keep working. zlib writes the PNG; the font below draws the text.
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

W, H = 1200, 630

# The site's palette, so the card and the page it links to are visibly the
# same object.
GROUND = (0x07, 0x09, 0x0D)
PANEL = (0x0E, 0x13, 0x1C)
LINE = (0x1D, 0x25, 0x32)
INK = (0xDC, 0xE3, 0xEF)
DIM = (0x8E, 0x9A, 0xB4)
MUTED = (0x67, 0x72, 0x8C)
ACCENT = (0x4C, 0x9A, 0xFF)
GREEN = (0x35, 0xD0, 0x7F)
GOLD = (0xF5, 0xB7, 0x2E)

TIER_COLOR = {
    "legendary": GOLD,
    "epic": GREEN,
    "rare": ACCENT,
    "solid": INK,
    "plain": DIM,
    "junk": MUTED,
}
TIER_ORDER = ["legendary", "epic", "rare", "solid", "plain", "junk"]

# 5x7, uppercase only. Names are drawn uppercased: Roblox treats a username
# as case-insensitive, so nothing is lost, and half the glyphs are not worth
# drawing twice.
FONT = {
    'A': ".###.#...##...#######...##...##...#",
    'B': "####.#...##...#####.#...##...#####.",
    'C': ".###.#...##....#....#....#...#.###.",
    'D': "####.#...##...##...##...##...#####.",
    'E': "######....#....####.#....#....#####",
    'F': "######....#....####.#....#....#....",
    'G': ".###.#...##....#.####...##...#.###.",
    'H': "#...##...##...#######...##...##...#",
    'I': "#####..#....#....#....#....#..#####",
    'J': "..###...#....#....#....#.#..#..##..",
    'K': "#...##..#.#.#..##...#.#..#..#.#...#",
    'L': "#....#....#....#....#....#....#####",
    'M': "#...###.###.#.##.#.##...##...##...#",
    'N': "#...###..##.#.##.#.##..###...##...#",
    'O': ".###.#...##...##...##...##...#.###.",
    'P': "####.#...##...#####.#....#....#....",
    'Q': ".###.#...##...##...##.#.##..#..##.#",
    'R': "####.#...##...#####.#.#..#..#.#...#",
    'S': ".#####....#.....###.....#....#####.",
    'T': "#####..#....#....#....#....#....#..",
    'U': "#...##...##...##...##...##...#.###.",
    'V': "#...##...##...##...##...#.#.#...#..",
    'W': "#...##...##...##.#.##.#.###.###...#",
    'X': "#...##...#.#.#...#...#.#.#...##...#",
    'Y': "#...##...#.#.#...#....#....#....#..",
    'Z': "#####....#...#...#...#...#....#####",
    '0': ".###.#...##..###.#.###..##...#.###.",
    '1': "..#...##....#....#....#....#...###.",
    '2': ".###.#...#....#...#...#...#...#####",
    '3': "####.....#....#.###.....#....#####.",
    '4': "...#...##..#.#.#..#.#####...#....#.",
    '5': "######....####.....#....##...#.###.",
    '6': "..##..#...#....####.#...##...#.###.",
    '7': "#####....#...#...#...#....#....#...",
    '8': ".###.#...##...#.###.#...##...#.###.",
    '9': ".###.#...##...#.####....#...#..##..",
    ' ': "...................................",
    '.': "...........................##...##.",
    ',': "......................##...##..#...",
    '-': "...............#####...............",
    ':': ".......##...##........##...##......",
    '/': "....#....#...#...#...#...#....#....",
    "'": "..#....#...........................",
    '%': "##...##..#....#...#...#..#..##...##",
}
GLYPH_W, GLYPH_H = 5, 7


# A miscounted row would shear every glyph after it in the same string, and
# the damage shows up as unreadable text on a card nobody looks at before it
# ships. Catch it at import instead.
for _ch, _rows in FONT.items():
    assert len(_rows) == GLYPH_W * GLYPH_H, f"glyph {_ch!r} is {len(_rows)} cells"


class Canvas:
    def __init__(self, w: int, h: int, bg: tuple[int, int, int]):
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def rect(self, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes(color) * (x1 - x0)
        for yy in range(y0, y1):
            off = (yy * self.w + x0) * 3
            self.px[off:off + len(row)] = row

    def text(self, x: int, y: int, s: str, scale: int,
             color: tuple[int, int, int], track: int = 1) -> int:
        """Draw s at scale and return the x where it ended."""
        for ch in s.upper():
            rows = FONT.get(ch, FONT[" "])
            for gy in range(GLYPH_H):
                for gx in range(GLYPH_W):
                    if rows[gy * GLYPH_W + gx] == "#":
                        self.rect(x + gx * scale, y + gy * scale, scale, scale, color)
            x += (GLYPH_W + track) * scale
        return x

    def png(self) -> bytes:
        raw = bytearray()
        stride = self.w * 3
        for y in range(self.h):
            raw.append(0)                       # filter: none
            raw += self.px[y * stride:(y + 1) * stride]

        def chunk(kind: bytes, body: bytes) -> bytes:
            return (struct.pack(">I", len(body)) + kind + body
                    + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

        head = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head)
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                + chunk(b"IEND", b""))


def width_of(s: str, scale: int, track: int = 1) -> int:
    return max(0, len(s) * (GLYPH_W + track) * scale - track * scale)


def _group(n: int) -> str:
    return f"{n:,}"


def _ago(iso: str | None) -> str:
    if not iso:
        return "just now"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return "just now"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    s = max(0, (datetime.now(timezone.utc) - then).total_seconds())
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{round(s / 60)}m ago"
    if s < 172800:
        return f"{round(s / 3600)}h ago"
    return f"{round(s / 86400)}d ago"


def _headline(data: dict) -> list[dict]:
    """The names worth putting on the card: rarest first, newest as the tie."""
    out = []
    for entries in (data.get("lengths") or {}).values():
        out.extend(entries)
    out.extend(data.get("released") or [])
    out.sort(key=lambda e: str(e.get("found", "")), reverse=True)
    out.sort(key=lambda e: TIER_ORDER.index(e["tier"])
             if e.get("tier") in TIER_ORDER else len(TIER_ORDER))
    return out


def render(data: dict) -> bytes:
    found = screened = 0
    for totals in (data.get("totals") or {}).values():
        found += totals.get("found", 0) or 0
        screened += totals.get("checked", 0) or 0

    c = Canvas(W, H, GROUND)
    c.rect(0, 0, W, 8, ACCENT)
    c.rect(64, 96, 1072, 1, LINE)

    # masthead
    x = c.text(64, 52, "ROVALID", 5, INK)
    c.text(x, 52, "LIVE", 5, ACCENT)
    c.text(64 + 1, 128, "ROBLOX USERNAMES FOUND FREE", 3, DIM)

    # the number, which is the reason to look at the card at all
    big = _group(found)
    c.text(64, 176, big, 16, GREEN if found else LINE)
    c.text(66, 176 + GLYPH_H * 16 + 26, "ON THE BOARD RIGHT NOW", 3, MUTED)

    # a taste of the actual names, in their tier colours
    names = _headline(data)[:4]
    if names:
        y = 400
        x = 64
        for e in names:
            label = str(e.get("name", "")).upper()
            color = TIER_COLOR.get(e.get("tier", "plain"), DIM)
            w = width_of(label, 6) + 44
            if x + w > W - 64:
                break
            c.rect(x, y, w, 76, PANEL)
            c.rect(x, y, 3, 76, color)
            c.text(x + 22, y + 17, label, 6, color)
            x += w + 16

    # the footer says where this came from and how fresh it is
    c.rect(64, 534, 1072, 1, LINE)
    c.text(64, 560, f"{_group(screened)} SCREENED  ::  {_ago(data.get('updated'))}",
           3, MUTED)
    tail = "ROSTIKCERMAK-PIXEL.GITHUB.IO/ROVALID"
    c.text(W - 64 - width_of(tail, 2), 564, tail, 2, ACCENT)
    return c.png()


def write(data: dict, path: Path) -> bool:
    """Redraw the card. Returns True only when the bytes actually changed.

    The hunt commits its output every couple of minutes for the length of a
    run; rewriting an identical PNG each time would add a binary blob to the
    history for no visible difference.
    """
    png = render(data)
    if path.exists() and path.read_bytes() == png:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(png)
    tmp.replace(path)
    return True


if __name__ == "__main__":
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/hits.json")
    dest = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/og.png")
    changed = write(json.loads(src.read_text(encoding="utf-8")), dest)
    print(f"{dest}: {'written' if changed else 'unchanged'} "
          f"({dest.stat().st_size:,} bytes)")

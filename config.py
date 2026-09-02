#!/usr/bin/env python3
"""RoValid v1.0 - Configuration, constants, helpers, and data models."""

from __future__ import annotations

import json
import string
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths - resolved relative to project root
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
RESULTS_DIR = PROJECT_ROOT / "results"


# ---------------------------------------------------------------------------
# Roblox API
# ---------------------------------------------------------------------------

VERSION = "1.0.0-mc"
GAME = "Minecraft"

# Bulk existence lookup. Returns only the usernames that EXIST, so anything
# absent from the response has no account behind it.
#
# Verified against the live API: the cap is 10 names per request. 11 returns
# HTTP 400 "getProfileName.profileNames: size must be between 1 and 10", and
# 25+ returns HTTP 413. That is 20x less compression than Roblox's 200, which
# is the single biggest difference between the two versions.
BATCH_ENDPOINT = "https://api.mojang.com/profiles/minecraft"
BATCH_MAX = 10

# Mojang has no signup validator to check against, so there is no second
# stage: a name missing from the bulk response is the answer. See SINGLE_STAGE
# in the README for what that cannot tell you.
SINGLE_STAGE = True

# Kept so the shared engine still imports; unused when SINGLE_STAGE is on.
VALIDATE_ENDPOINT = "https://api.mojang.com/users/profiles/minecraft/"
VALIDATE_BIRTHDAY = ""
VALIDATE_CONTEXT = ""
CODE_AVAILABLE = 0
CODE_TAKEN     = 1
CODE_CENSORED  = 2

# Minecraft username rules (enforced locally to avoid wasted requests):
# 3-16 characters, a-z A-Z 0-9 and underscore, with no restriction on how
# many underscores or where they sit.
MIN_LEN = 3
MAX_LEN = 16
USERNAME_CHARS = string.ascii_lowercase + string.digits + "_"
MAX_UNDERSCORES = MAX_LEN

MAX_CONCURRENCY = 2000  # hard cap - beyond this asyncio/aiohttp stalls


# ---------------------------------------------------------------------------
# Colour palette (Rich hex codes)
# ---------------------------------------------------------------------------

class C:
    """Semantic colour constants - vibrant terminal-optimized palette."""
    PRIMARY   = "#0A84FF"
    SUCCESS   = "#30D158"
    DANGER    = "#FF453A"
    WARNING   = "#FF9F0A"
    MUTED     = "#98989D"
    BORDER    = "#48484A"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_dir(*paths: str | Path) -> None:
    """Create directories if they don't exist."""
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)


def ensure_file(filepath: str | Path, *, clean: bool = False) -> None:
    """Create a file (and its parents). If *clean*, truncate it."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    if clean or not path.exists():
        path.write_text("", encoding="utf-8")


def load_lines(filepath: str | Path) -> list[str]:
    """Read non-empty lines from a file. Returns [] if missing."""
    path = Path(filepath)
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# Frozenset of every legal username character. `issuperset` runs the whole
# membership test in one C-level call, which beats a per-character genexp by
# roughly 4x - and this runs once per input name, so on a multi-million name
# list it is the difference between seconds and tens of seconds.
_ALLOWED_CHARS = frozenset(
    string.ascii_lowercase + string.ascii_uppercase + string.digits + "_"
)


def is_valid_username(name: str) -> bool:
    """Check a username against Roblox's client-side rules.

    Every rule here maps to a validator response code, so filtering locally
    means we never spend a request learning something we already knew:

    - 3-16 characters
    - only a-z, A-Z, 0-9 and '_'

    Unlike Roblox, Minecraft puts no limit on underscores and allows them at
    the edges, so those two checks are gone.
    """
    if not (MIN_LEN <= len(name) <= MAX_LEN):
        return False
    return _ALLOWED_CHARS.issuperset(name)


def invalid_reason(name: str) -> str:
    """Human-readable reason a username fails local validation."""
    if not (MIN_LEN <= len(name) <= MAX_LEN):
        return f"length {len(name)} (must be {MIN_LEN}-{MAX_LEN})"
    if not _ALLOWED_CHARS.issuperset(name):
        return "illegal character"
    return "unknown"


# ---------------------------------------------------------------------------
# Persistent JSON config
# ---------------------------------------------------------------------------

class Config:
    """JSON-backed persistent config with in-memory caching."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else DATA_DIR / "config.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists() and self._path.stat().st_size > 0:
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._data = {}
        else:
            self._path.write_text("{}", encoding="utf-8")

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def get_all(self) -> dict:
        return dict(self._data)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AppSettings:
    """Application-level settings (from CLI args)."""
    debug: bool = False
    no_wizard: bool = False
    # Printing each check above the live panel is on by default; --no-stream
    # turns it off. The default lives here so anything building AppSettings
    # directly agrees with what argparse produces.
    stream: bool = True


@dataclass
class RunConfig:
    """All user choices gathered during setup."""
    proxies: list[str]
    remove_bad_proxies: bool
    usernames: list[str]
    concurrency: int
    timeout: int
    scraped: bool = False
    two_stage: bool = True
    webhook_url: str | None = None
    webhook_message: str | None = None

    # Keep drawing fresh batches until something turns up. Only meaningful
    # for generated names - a file is finite, so re-running it just re-asks
    # Roblox the same questions. The gen_* fields record how the first batch
    # was made so later rounds can be drawn the same way.
    repeat_until_found: bool = False
    gen_length: int = 0          # 0 = these names did not come from the generator
    gen_count: int = 0
    gen_underscore: bool = False


@dataclass
class Stats:
    """Counter block for a run.

    Deliberately lock-free. asyncio runs one coroutine at a time on a single
    thread, and `self.x += 1` contains no await, so it cannot be interleaved -
    a lock buys nothing here but costs an acquire plus a possible suspension
    on every single counter bump. At 2000 workers each doing two or three
    bumps per request, that lock was the hottest contention point in the
    process. These are now plain synchronous methods.
    """
    requests: int = 0            # every HTTP attempt, both stages
    batch_requests: int = 0      # stage-1 requests only
    screened: int = 0            # names resolved by stage 1
    candidates: int = 0          # names that survived stage 1
    works: int = 0               # confirmed available
    taken: int = 0
    censored: int = 0
    invalid: int = 0             # rejected locally, never sent
    ratelimited: int = 0
    fellback_chunks: int = 0     # stage-1 chunks that had to go to stage 2
    circuit_opens: int = 0
    rps: float = 0.0
    checks_rps: float = 0.0
    peak_rps: float = 0.0
    best_streak: int = 0
    _streak: int = 0

    def inc(self, field_name: str, amount: int = 1) -> None:
        """Increment any counter by name."""
        setattr(self, field_name, getattr(self, field_name) + amount)

    def inc_works(self) -> None:
        self.works += 1
        self._streak += 1
        if self._streak > self.best_streak:
            self.best_streak = self._streak

    def inc_taken(self, amount: int = 1) -> None:
        self.taken += amount
        self._streak = 0

    def set_rps(self, value: float) -> None:
        self.rps = value
        if value > self.peak_rps:
            self.peak_rps = value

    def set_checks_rps(self, value: float) -> None:
        self.checks_rps = value

    def snapshot(self) -> dict:
        return {
            "requests": self.requests,
            "batch_requests": self.batch_requests,
            "screened": self.screened,
            "candidates": self.candidates,
            "works": self.works,
            "taken": self.taken,
            "censored": self.censored,
            "invalid": self.invalid,
            "ratelimited": self.ratelimited,
            "fellback_chunks": self.fellback_chunks,
            "circuit_opens": self.circuit_opens,
            "rps": self.rps,
            "checks_rps": self.checks_rps,
            "peak_rps": self.peak_rps,
            "best_streak": self.best_streak,
        }

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

VERSION = "1.0.0"

# Stage 1: bulk existence lookup. Returns only usernames that EXIST.
# Verified cap: 200 names per request (300+ -> HTTP 400 "Too many usernames").
BATCH_ENDPOINT = "https://users.roblox.com/v1/usernames/users"
BATCH_MAX = 200

# Stage 2: signup validator. Catches censored/reserved names that stage 1
# cannot see, because no *user* holds them but they still can't be registered.
VALIDATE_ENDPOINT = "https://auth.roblox.com/v1/usernames/validate"
VALIDATE_BIRTHDAY = "2000-01-01T00:00:00.000Z"
VALIDATE_CONTEXT = "Signup"

# Response codes returned by VALIDATE_ENDPOINT (all under HTTP 200).
CODE_AVAILABLE = 0   # "Username is valid"
CODE_TAKEN     = 1   # "Username is already in use"
CODE_CENSORED  = 2   # "Username not appropriate for Roblox"
CODE_LENGTH    = 3   # "Usernames can be 3 to 20 characters long"
CODE_EDGE_US   = 4   # "Username can't start or end with _"
CODE_MULTI_US  = 5   # "Usernames can have at most one _"
CODE_CHARSET   = 7   # "Only a-z, A-Z, 0-9, and _ are allowed"

# Roblox username rules (enforced locally to avoid wasted requests)
MIN_LEN = 3
MAX_LEN = 20
USERNAME_CHARS = string.ascii_lowercase + string.digits + "_"
MAX_UNDERSCORES = 1

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


def is_valid_username(name: str) -> bool:
    """Check a username against Roblox's client-side rules.

    Every rule here maps to a validator response code, so filtering locally
    means we never spend a request learning something we already knew:

    - 3-20 characters                     (code 3)
    - only a-z, A-Z, 0-9, '_'             (code 7)
    - at most one '_'                     (code 5)
    - cannot start or end with '_'        (code 4)
    """
    if not (MIN_LEN <= len(name) <= MAX_LEN):
        return False
    if name.count("_") > MAX_UNDERSCORES:
        return False
    if name.startswith("_") or name.endswith("_"):
        return False
    return all(c.isascii() and (c.isalnum() or c == "_") for c in name)


def invalid_reason(name: str) -> str:
    """Human-readable reason a username fails local validation."""
    if not (MIN_LEN <= len(name) <= MAX_LEN):
        return f"length {len(name)} (must be {MIN_LEN}-{MAX_LEN})"
    if not all(c.isascii() and (c.isalnum() or c == "_") for c in name):
        return "illegal character"
    if name.count("_") > MAX_UNDERSCORES:
        return "more than one underscore"
    if name.startswith("_") or name.endswith("_"):
        return "leading/trailing underscore"
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


@dataclass
class Stats:
    """Async-safe stats counter."""
    requests: int = 0            # every HTTP attempt, both stages
    batch_requests: int = 0      # stage-1 requests only
    screened: int = 0            # names resolved by stage 1
    candidates: int = 0          # names that survived stage 1
    works: int = 0               # confirmed available
    taken: int = 0
    censored: int = 0
    invalid: int = 0             # rejected locally, never sent
    ratelimited: int = 0
    circuit_opens: int = 0
    rps: float = 0.0
    checks_rps: float = 0.0
    peak_rps: float = 0.0
    best_streak: int = 0
    _streak: int = 0

    def __post_init__(self) -> None:
        import asyncio
        self._lock = asyncio.Lock()

    async def inc(self, field_name: str, amount: int = 1) -> None:
        """Increment any counter by name."""
        async with self._lock:
            setattr(self, field_name, getattr(self, field_name) + amount)

    async def inc_works(self) -> None:
        async with self._lock:
            self.works += 1
            self._streak += 1
            if self._streak > self.best_streak:
                self.best_streak = self._streak

    async def inc_taken(self, amount: int = 1) -> None:
        async with self._lock:
            self.taken += amount
            self._streak = 0

    async def set_rps(self, value: float) -> None:
        async with self._lock:
            self.rps = value
            if value > self.peak_rps:
                self.peak_rps = value

    async def set_checks_rps(self, value: float) -> None:
        async with self._lock:
            self.checks_rps = value

    async def snapshot(self) -> dict:
        async with self._lock:
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
                "circuit_opens": self.circuit_opens,
                "rps": self.rps,
                "checks_rps": self.checks_rps,
                "peak_rps": self.peak_rps,
                "best_streak": self.best_streak,
            }

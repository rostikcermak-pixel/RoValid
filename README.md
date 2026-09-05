# RoValid

**Roblox username availability checker.** Async, proxy-optional, two-stage.

[![ci](https://github.com/rostikcermak-pixel/RoValid/actions/workflows/ci.yml/badge.svg)](https://github.com/rostikcermak-pixel/RoValid/actions/workflows/ci.yml)
[![hunt](https://github.com/rostikcermak-pixel/RoValid/actions/workflows/hunt.yml/badge.svg)](https://github.com/rostikcermak-pixel/RoValid/actions/workflows/hunt.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)

No token, no login, no account needed — both endpoints it uses are public and
unauthenticated.

### 🔴 Live board — [rostikcermak-pixel.github.io/RoValid](https://rostikcermak-pixel.github.io/RoValid/)

Names found free, updated by a GitHub Action. Nothing to install and no PC left
running: the hunt happens on GitHub, the page just reads what it committed.
There is a chat under the board.

> **Playing Minecraft instead?** There's a Minecraft version on the
> [`minecraft`](https://github.com/rostikcermak-pixel/RoValid/tree/minecraft)
> branch — same tool, pointed at the Mojang API.

---

## Contents

- [Quick start](#quick-start) · [Usage](#usage) · [Command-line flags](#command-line-flags) · [Output files](#output-files)
- [How it works](#how-it-works) · [Why it's fast](#why-its-fast) · [Proxies](#do-you-need-proxies)
- [The live board](#the-live-board) · [Configuration](#configuration) · [Roblox rules](#roblox-username-rules)
- [Project layout](#project-layout) · [Development](#development) · [Troubleshooting](#troubleshooting)

---

## Quick start

**Requirements:** Python 3.11 or newer. CI runs the suite on 3.11, 3.12 and 3.13.

### macOS / Linux

```bash
git clone https://github.com/rostikcermak-pixel/RoValid.git
cd RoValid
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python checker.py
```

After the first setup, `./run.sh` launches it against that venv.

### Windows

Install Python from [python.org/downloads](https://python.org/downloads) —
**tick "Add Python to PATH"** during install. Then, in PowerShell:

```powershell
git clone https://github.com/rostikcermak-pixel/RoValid.git
cd RoValid
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python checker.py
```

After the first setup, **double-click `run.bat`**.

Use Windows Terminal or PowerShell rather than the old `cmd.exe` — the dashboard
draws box characters that legacy consoles render poorly.

---

## Usage

The wizard asks four things. Pressing Enter through all of them is a valid run:

1. **Proxies** — choose `n` (none). You do not need them.
2. **Usernames** — `g` to generate, pick a length, pick how many.
3. **Speed** — accept the defaults.
4. **Webhook** — `n` unless you want Discord notifications.

When you generate names there is one extra question: **keep drawing new names
until one is free?** Off by default. With it on, a round that finds nothing
draws a fresh batch and goes again, stopping at the first hit or on Ctrl+C.
Rounds share one rate-limit cooldown, so repeating costs no re-learning of the
limit, and names an earlier round already checked are never re-checked.

Each check prints above the live panel as it resolves — taken names dim out,
hits show bright — with the stats staying pinned underneath. It renders at
roughly 13,000 lines/sec (pre-built styles, batched output), so a proxyless run
or a modest proxied one never notices it: the network is far slower than the
rendering. It only becomes the bottleneck on a pool pushing past that, which is
why `--no-stream` exists.

**Ctrl+C stops cleanly.** It finishes what is in flight, prints the summary, and
writes every name it never got an answer for to `results/unresolved.txt` so you
can re-run just those. Press it a second time to quit immediately instead.

### Command-line flags

| Flag | Effect |
|---|---|
| `-n`, `--no-wizard` | Skip setup; reuse the saved config and `data/` files |
| `-d`, `--debug` | Log every request and response, and stop filtering event-loop noise |
| `--no-stream` | Don't print each check above the live panel |
| `--diag` | Sample the run into `logs/diag.csv` every 5s (deltas per interval, plus proxy-pool state) |
| `--version` | Print the version and exit |

`--diag` is the one to reach for when a run decays instead of finishing: the
column that grows as throughput falls is the cause.

### Output files

Written under `results/`, created on first run:

- `results/hits.txt` — confirmed available, appended across runs, flushed the
  moment each hit is found
- `results/unresolved.txt` — names that could not be resolved (dead proxy,
  exhausted retries, interrupted run), rewritten each run

**Nothing gets silently dropped.** Feed `unresolved.txt` back in as your input
file to retry exactly those. A name is never quietly reclassified as taken
because a request failed.

---

## How it works

A naive checker sends one request per username. Roblox's rate limiter allows only
a couple of requests per IP before throttling, so that approach needs a large paid
proxy pool to get anywhere.

RoValid checks **200 names per request** instead, using a bulk endpoint, then only
falls back to per-name checks for the handful that survive:

| Stage | Endpoint | Cost | What it answers |
|---|---|---|---|
| 1 — screen | `users.roblox.com/v1/usernames/users` | 200 names / request | Does an account already hold this name? |
| 2 — confirm | `auth.roblox.com/v1/usernames/validate` | 1 name / request | Can it actually be registered? |

**Stage 2 is not optional.** Censored and reserved names have no account behind them,
so stage 1 reports them as free when they are not. A batch-only checker gets these
wrong. Stage 1 narrows the field; stage 2 is what makes the answer trustworthy.

---

## Why it's fast

### Measured, proxyless

Two regimes, and which one you land in depends entirely on how many names
survive stage 1:

```
3,000 four-char names, almost all already registered
  stage 1:  2,599 resolved in  54 batched requests
  stage 2:    401 survivors in 401 single requests   <-- the cost
  total:    464s  (6.5 names/sec)

800 four-char names, none surviving stage 1
  stage 1:    800 resolved in  10 batched requests
  stage 2:      0 survivors
  total:     26s  (31 names/sec)
```

Stage 1 is cheap and scales beautifully. **Stage 2 is one request per surviving
name and cannot be batched** — Roblox exposes no bulk validator. So the honest
rule is: your runtime is roughly *(number of stage-2 candidates) × (your
per-request rate)*, and everything stage 1 eliminates is nearly free.

Proxyless, Roblox allows about three requests before throttling, then reopens
roughly six seconds after you stop hammering — *on the batch endpoint*. RoValid
now runs close to that rather than at the ~25% of it that blind retrying
managed.

> **This is not the whole ceiling, and the split above may be inverted on your
> IP.** `bench.py` measures the two endpoints separately and they turn out to be
> independent buckets with very different limits: the stage-2 validator
> sustained **298 names/sec with zero 429s** on the same IP where the batch
> endpoint was throttled at every pace. Which endpoint is your bottleneck is a
> per-IP question. See [BENCH.md](BENCH.md), and measure your own IP before
> assuming either number.

### Proxyless: don't fight the rate limiter

The important half of Roblox's proxyless limiter is easy to miss: the reopen
timer starts when the bucket **empties**, and a request arriving while it is
shut pushes that timer back. Retrying into a closed bucket therefore spends a
real request to rediscover a limit you already know about, *and* delays the
reopen. Measured against that limiter, the old blind-retry policy wasted 42%
of every request it sent and ran at about a quarter of the achievable rate:

```
400 names, proxyless, 2 workers
  before:  397s   (85 requests, 34 of them 429s)
  after:   108s   (83 requests, 32 of them 429s)
```

Almost the same number of 429s — what changed is what each one costs. Every
worker now parks on one shared resume time taken from the server's own
`Retry-After`, instead of each backing off on its own schedule escalating
from 7s toward 45s keyed to a per-name attempt counter. That lands within
about 1.05x of the theoretical best the bucket allows.

Note what this deliberately does *not* do: it never paces sends proactively.
Spacing requests evenly is worse here, not better — a steady trickle into a
bucket whose clock keeps resetting can starve indefinitely, and in simulation
even spacing failed to finish at any interval tried. Sending freely while the
bucket is giving is also what keeps this safe if Roblox's real limits are more
generous than the ones measured here; across every limiter shape simulated it
was 1.3x–3.6x faster than before, and never slower.

### With proxies: pool size is the lever, not worker count

Each proxy carries its own rate-limit bucket, so throughput scales with how
many proxies you have. It does not scale with workers — once there is roughly
one worker per proxy, the pool's refill rate is the bound and extra workers
only queue up behind it, spending their requests on 429s.

```
20,000 names, one worker per proxy
   25 proxies  ->  329s
   50 proxies  ->  172s
  100 proxies  ->   77s
  200 proxies  ->   42s
  400 proxies  ->   18s
```

Pushing workers past that costs results rather than buying speed. On a
25-proxy pool, 75 workers finished 9% quicker but found 1263 names where 25
workers found all 1294 — the surplus workers push names past the point where
they are abandoned, and waste climbs from 65% of requests to 80%. The default
is therefore one worker per proxy.

### Client-side throughput

Everything above is about *request economics* — how few requests the work can be
done in. Separately, the client has to be able to issue them fast enough to keep
the proxy pool busy, and with a large scraped pool that is where the real
ceiling used to sit:

```
200,000 names, 200 workers, 5,000 scraped proxies, 50ms simulated latency
  before:  22.5s   (8,900 names/sec)
  after:    3.4s  (58,200 names/sec)
```

Identical results either way — the same 11,248 hits. The difference is all
client-side scheduling:

- **Proxy selection was O(pool) per request.** Every single request rebuilt a
  filtered dict over the whole pool and did a linear weighted pick, so a
  5,000-proxy pool capped the process at roughly 1,100 selections/sec of pure
  event-loop CPU — well below what the pool could actually sustain. It now
  picks by bisect against a table rebuilt at most twice a second (and
  immediately when a cooldown lapses), which measures ~370x faster.
- **The stats counters held an asyncio lock** for every increment, two or three
  per request, on a single-threaded event loop where the lock protected nothing.
- **Stage 1 and stage 2 now overlap.** Screening publishes survivors to a queue
  that validation drains as they arrive, instead of stage 2 waiting for the last
  chunk to be screened. Both stages draw from one shared in-flight request
  budget, so the concurrency you set is still the concurrency you get.
- Duplicate input names are dropped (case-insensitively, since Roblox treats
  usernames that way), sockets are kept alive across the run, and local
  username validation is a single set operation.

---

## Do you need proxies?

**Probably not.** That is the point of the batching. Proxyless clears a few hundred
names per burst, which is enough for most lists. Add proxies only if you are
grinding tens of thousands of names.

| Mode | Setup | Good for |
|---|---|---|
| Proxyless | nothing | most lists — start here |
| Free scrape | one keypress | large lists, no budget |
| Your own proxies | file or paste | tens of thousands of names |

**If stage 2 has more than ~100 candidates, use proxies.** Each proxy carries
its own rate-limit bucket, and that is the only thing that lifts this ceiling.

The scrape pulls from 43 public lists and returns roughly 1,170,000 unique
proxies in about four seconds. Almost all of them are dead, so RoValid screens
the pool against the real endpoint before the run starts — one short timeout
per proxy, all at once — rather than discovering each corpse mid-run at the
cost of a stalled worker.

That screen is the slow part, not the scrape, so the pool is trimmed to 30,000
first (`SCRAPE_POOL_CAP`) and the screen then takes a few minutes. The trim is
not uniform: seven of the sources are unchecked dumps that make up ~98% of the
total, so sampling evenly would drown out the curated lists that publish only
validated proxies. Every curated source is kept whole — about 23,000 proxies —
and the dumps fill the remaining ~7,000 as a hedge against the curated lists
being stale on any given day. Raise the cap if you want more; the cost is
roughly one extra minute of screening per 7,000 proxies.

Survivors are written to `data/proxies.txt`, so the next run can reuse them
instead of screening again.

Free proxies carry the usual caveat: an unknown operator sees that your IP connects
to `roblox.com` and how often. They cannot see the names or the responses — traffic
is HTTPS end-to-end with certificate verification left on, so a proxy only gets a
`CONNECT` tunnel. Webhook traffic never goes through the proxy pool at all.

---

## The live board

`.github/workflows/hunt.yml` runs `hunt.py` on GitHub's own machines and commits
anything it finds to `docs/hits.json`. GitHub Pages serves that file, so the
board is live without a server, a database, or a machine of yours being switched
on. The job publishes while it runs rather than once at the end.

Two passes run each time:

| pass | what it does |
|---|---|
| **watchlist** | Re-checks 3,600 names people would actually want — real words, and shapes that read like names. All taken today; a release lands in the gold band at the top. |
| **hunt** | Draws fresh random names at 3, 4 and 5 characters and screens them. |

The watchlist is the interesting half. Free names are not scarce — a
one-minute sample found 183 free five-character names and **not one was
digit-free**. Good names are taken, and only come back when somebody renames
away from one, so the board watches for exactly that rather than hoping to
stumble on a good name at random.

### What reaches the board

Being free and being worth having are different questions, and the board
answers the second.

- **3 and 4 characters: everything.** Every name at both lengths is taken —
  an exhaustive sweep of all 1,679,616 four-character names turned up nothing
  — so anything that ever comes free is an event and goes up whatever it
  looks like.
- **5 characters: only what `rarity.is_noteworthy` accepts.** A palindrome, a
  repeat, or any digit-free name qualifies outright. Below that a name has to
  read: at most one digit, pronounceable, and carrying a real word — `mud5c`,
  `d6bug`, `box8j`, `6cowv`, `4vhit`. Measured over 21,069 real finds that
  keeps 29 of them, about one in 726.

The totals still count every free name, because that is the true number. The
column header shows both — `25 of 10,543` — rather than a find count next to
a much shorter list, which is what used to make the page look broken.

Run it yourself against a local file instead of the live one:

```bash
python hunt.py --minutes 5 --lengths 3,4,5     # writes hits.local.json
```

`hunt.py` only writes `docs/hits.json` when explicitly told to (`--out
docs/hits.json`), which is what the scheduled job passes. A local run
committing its own snapshot would roll the board's running totals backwards.

---

## Configuration

Everything the wizard learns is saved under `data/`, which is gitignored.

| File | What it holds |
|---|---|
| `data/config.json` | Saved wizard answers — reused by `--no-wizard` |
| `data/proxies.txt` | The proxy pool, one per line (`host:port` or `login:pass@host:port`) |
| `data/names_to_check.txt` | The last username list, one per line |

Keys in `data/config.json`:

| Key | Meaning |
|---|---|
| `concurrency` | In-flight request budget, shared by both stages |
| `timeout` | Per-request timeout in seconds |
| `two_stage` | `false` sends every name to the validator (~200x the requests) |
| `remove_proxies` | Drop proxies permanently once they fail |
| `reuse_proxies` | Skip the "reuse them?" prompt in either direction |
| `proxies_are_free` | Marks the saved pool as scraped, which turns on scoring, benching and pre-flight screening |
| `webhook`, `webhook_message`, `webhook_always` | Discord notification settings |

> **`data/config.json` holds your Discord webhook URL in plaintext.** That URL
> is a credential — anyone holding it can post to your channel. It is gitignored,
> but don't paste the file into an issue, and regenerate the webhook in Discord
> if you ever do.

Webhook message templates support `<name>`, `<link>`, `<time>` and `<elapsed>`.

---

## Roblox username rules

Enforced locally before anything is sent, since the API would only tell you the same
thing at the cost of a request:

- 3–20 characters
- `a-z`, `A-Z`, `0-9`, `_` only
- at most one underscore
- cannot start or end with an underscore

Names are also de-duplicated case-insensitively — `Cool` and `cool` are the same
registration on Roblox.

### Validator response codes

Both endpoints answer under HTTP 200; the meaning is in the body.

| Code | Meaning | Counted as |
|---|---|---|
| 0 | Username is valid | **available** |
| 1 | Already in use | taken |
| 2 | Not appropriate for Roblox | censored |
| 3 | Must be 3–20 characters | invalid |
| 4 | Can't start or end with `_` | invalid |
| 5 | At most one `_` | invalid |
| 7 | Only `a-z A-Z 0-9 _` allowed | invalid |

---

## Project layout

```
checker.py        entry point, two-stage runner, live dashboard
config.py         endpoints, Roblox rules, Stats, JSON config
wizard.py         interactive setup + proxy scraper
proxy.py          rotation, cooldowns, scoring, pre-flight screen
engine.py         batch screen, validator, circuit breaker, webhook
ui.py             rich rendering primitives

hunt.py           headless hunter for the live board
watchlist.py      the names worth waiting for
rarity.py         how good a name is, not just whether it's free
docs/             GitHub Pages board (index.html reads hits.json)

bench.py          measure the rate limiters directly  -> BENCH.md
probe.py          is the batch limit per request or per username?
tests/            unit tests for the pure logic
```

---

## Development

```bash
pip install -r requirements-dev.txt
pytest                 # unit tests, no network
ruff check .           # lint
```

Both run on every push and pull request via `.github/workflows/ci.yml`, across
Python 3.11–3.13, alongside a smoke job that imports every module and checks the
entry points still start.

The tests cover the deterministic logic — username rules, tier scoring, the
watchlist, the persistent config, the proxyless cooldown policy — and the two
pieces where a mistake is expensive rather than merely wrong: the shared request
loop (retries, 429 handling, the published limiter headers) and the two-stage
pipeline (chunk retries, the fallback to stage 2, and never losing a name to a
cancelled run). Both are driven by fakes, so nothing in the suite touches the
network and the whole thing runs in a couple of seconds.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Boxes and `?` instead of the dashboard border | Legacy `cmd.exe`. Use Windows Terminal or PowerShell. |
| A wall of `ConnectionResetError` tracebacks on Windows | Roblox resets idle TLS sockets; `checker.py` already suppresses these. If you see them, you are on an old copy. |
| `RuntimeError` from `asyncio.sslproto` on Python 3.13 | Known 3.13 SSL bug; `checker.py` patches it at import. |
| Run starts fast, then decays to zero | Almost always the proxy pool. Run with `--diag` and read `logs/diag.csv`: the column that grows as throughput falls is the cause. |
| Everything comes back "unresolved" | The pool could not reach Roblox. Re-run and let the pre-flight screen report how many proxies are alive; if it's zero, go proxyless. |
| Chunks falling through to stage 2 | The summary says how many. It means stage 1 ran out of attempts, which is a pool-capacity problem, not a bug. |

---

## Note

Automating Roblox's API at volume is against their Terms of Service. The exposure is
to whatever account you associate with this, not to your machine. Availability data
itself is public — this only reads it.

---

## License

GPL-3.0 — see [LICENSE](LICENSE).

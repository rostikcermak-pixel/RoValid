# RoValid

**Roblox username availability checker.** Async, proxy-optional, two-stage.

No token, no login, no account needed — both endpoints it uses are public and unauthenticated.

---

## Why it's fast

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
rule is: your runtime is roughly *(number of stage-2 candidates) x (your
per-request rate)*, and everything stage 1 eliminates is nearly free.

Proxyless, Roblox allows about three requests before throttling, then reopens
roughly six seconds after you stop hammering. That is a hard ceiling of roughly
1,800 stage-2 confirmations per hour on one IP, and RoValid now runs close to
it (see below) rather than at the ~25% of it that blind retrying managed.

**If stage 2 has more than ~100 candidates, use proxies.** Each proxy carries
its own rate-limit bucket, and that is the only thing that lifts this ceiling.
Free scraped proxies are enough — see below.

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

Almost the same number of 429s - what changed is what each one costs. Every
worker now parks on one shared resume time taken from the server's own
`Retry-After`, instead of each backing off on its own schedule escalating
from 7s toward 45s keyed to a per-name attempt counter. That lands within
about 1.05x of the theoretical best the bucket allows.

Note what this deliberately does *not* do: it never paces sends proactively.
Spacing requests evenly is worse here, not better - a steady trickle into a
bucket whose clock keeps resetting can starve indefinitely, and in simulation
even spacing failed to finish at any interval tried. Sending freely while the
bucket is giving is also what keeps this safe if Roblox's real limits are more
generous than the ones measured here; across every limiter shape simulated it
was 1.3x-3.6x faster than before, and never slower.

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

## Quick start

### Windows

Install Python from [python.org/downloads](https://python.org/downloads) — **tick "Add Python to PATH"** during install.

Then open PowerShell in the unzipped folder and run:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python checker.py
```

After the first setup, just **double-click `run.bat`** to launch it.

Use Windows Terminal or PowerShell rather than the old `cmd.exe` — the dashboard
draws box characters that legacy consoles render poorly.

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python checker.py
```

Or `./run.sh`.

### Then what

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

Results land in `results/hits.txt`, written the moment each hit is found.

Flags: `--no-wizard` reuses your last setup, `--debug` logs every request, `--version`.

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

Free proxies carry the usual caveat: an unknown operator sees that your IP connects
to `roblox.com` and how often. They cannot see the names or the responses — traffic
is HTTPS end-to-end with certificate verification left on, so a proxy only gets a
`CONNECT` tunnel. Webhook traffic never goes through the proxy pool at all.

---

## Nothing gets silently dropped

Names that could not be resolved — dead proxy, exhausted retries — are written to
`results/unresolved.txt`. Feed that file back in as your input to retry exactly
those. A name is never quietly reclassified as taken because a request failed.

- `results/hits.txt` — confirmed available, appended across runs
- `results/unresolved.txt` — needs a retry, rewritten each run

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

---

## Validator response codes

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

## Layout

```
checker.py        entry point, two-stage runner
config.py         endpoints, Roblox rules, Stats, JSON config
wizard.py         interactive setup + proxy scraper
proxy.py          rotation, cooldowns, scoring
engine.py         batch screen, validator, circuit breaker, webhook
ui.py             rich dashboard
```

---

## Note

Automating Roblox's API at volume is against their Terms of Service. The exposure is
to whatever account you associate with this, not to your machine. Availability data
itself is public — this only reads it.

---

## License

GPL-3.0 — see [LICENSE](LICENSE).

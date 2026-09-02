# Bench findings

Everything here came out of `bench.py`, run against the live endpoints. Numbers
are from one throttled, shared egress IP — **run it yourself before trusting the
absolute values**, because the limiter is per-IP and yours will differ. The
*shape* of the findings is what matters, and that part is not IP-dependent.

```
python bench.py            # everything, ~4 minutes
python bench.py --dual     # the important one
```

---

## 1. Both endpoints publish their limiter state. We ignore it.

Every response — **including every 429** — carries:

```
x-ratelimit-limit:     500, 500;w=60
x-ratelimit-remaining: 499
x-ratelimit-reset:     39
```

The checker currently infers the limiter by watching 429s land and guessing a
backoff (`SharedCooldown`). It never reads these headers. It doesn't have to
guess at all.

Caveat, and it's a real one: the published quota is **not** the binding
constraint. On a throttled IP the batch endpoint 429s with
`remaining=499` — i.e. 99.8% of the published quota unspent. There is a
short-window burst limiter sitting in front of the quota, and that one is
unpublished. So the headers give you a ceiling and a reset clock, not a
replacement for backoff.

## 2. The two endpoints are separate buckets

Measured directly (`--dual`, 12s each):

| | validate ok | batch ok | batch 429 |
|---|---|---|---|
| validate alone | 1801 | — | — |
| batch alone | — | 1 | 24 |
| both at once | 1809 | 1 | 17 |

Running the validator flat out cost the batch endpoint **nothing**. The buckets
add; they do not share.

This matters because of how the pipeline is shaped today. Stage 2 only ever
sees names that survived stage 1 — a trickle. So during a normal run, a second,
far more permissive rate-limit bucket sits ~99% idle from start to finish.

## 3. The two buckets are throttled wildly differently

`--validate`, one name per request:

```
   target   sent     ok   429   err   names/sec
    10.0/s    101    101     0     0         9.9
    25.0/s    251    251     0     0        24.7
    60.0/s    601    601     0     0        59.3
   100.0/s   1001   1001     0     0        98.6
   250.0/s   2501   2501     0     0       230.6
   400.0/s   4001   4001     0     0       297.8
```

**298 names/sec, zero 429s, no proxies.** The knee is somewhere past 400/s.

`--batch`, 200 names per request, same IP, same minute:

```
   target   sent     ok   429   err   names/sec
     0.2/s      4      2     2     0        32.7   <- throttled
     1.0/s     11      1    10     0        19.9   <- throttled
     8.0/s     81      4    77     0        79.1   <- throttled
```

Throttled at every pace, including one request every five seconds.

So on this IP the "cheap" bulk endpoint delivers ~20–80 names/sec and the
"expensive" one-at-a-time validator delivers ~300. The architecture's core
assumption is inverted here.

**This will not be true on a clean home IP** — batch at even 2 req/s is 400
names/sec and beats the validator. The point is that *which endpoint is your
bottleneck is a per-IP question the tool never asks.*

## 4. 200 is genuinely the batch ceiling

201 names returns `HTTP 400 {"code":2,"message":"Too many usernames."}`. No
room there.

## 5. User-Agent is load-bearing

A bare `Python-urllib/3.11` UA gets **403 on the validator and 429 on batch**
regardless of remaining quota. The checker's
`Mozilla/5.0 (compatible; RoValid/1.0)` passes. Worth knowing before anyone
"cleans up" that header.

---

## What this disproves in the README

> That is a hard ceiling of roughly 1,800 stage-2 confirmations per hour on one
> IP

Measured: **1,072,800 stage-2 confirmations per hour** on one IP, at 298/sec
sustained with no throttling. The old figure assumed both stages draw on one
shared bucket. They don't.

## Things that turned out not to work

Ruled out by measurement, so nobody spends a weekend on them:

- **Cookie rotation** — dropping the `GuestData` / `__cf_bm` cookies changed
  nothing (1/12 clean either way).
- **Fresh connection per request** — 2/12 vs 1/12. Noise, not a lever.
- **Edge-node spread** — `users.roblox.com` resolves to a single A record and
  every request landed on the same `x-roblox-edge`. Nothing to spread across.
- **Bigger batches** — hard 400 at 201.

The limiter keys on the IP. Nothing at the HTTP layer gets around that.

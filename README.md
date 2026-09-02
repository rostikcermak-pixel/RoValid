# RoValid — Minecraft

**Minecraft username availability checker.** Async, proxy-optional.

No account, no token, no login — the Mojang endpoint it uses is public and
unauthenticated.

> This is the **Minecraft** branch. The original Roblox version lives on
> [`main`](https://github.com/rostikcermak-pixel/RoValid).

> **Playing Minecraft instead?** There's a Minecraft version on the
> [`minecraft`](https://github.com/rostikcermak-pixel/RoValid/tree/minecraft)
> branch — same tool, pointed at the Mojang API.

---

## How it works

One endpoint, one stage:

| Endpoint | Cost | What it answers |
|---|---|---|
| `api.mojang.com/profiles/minecraft` | 10 names / request | Does an account already hold this name? |

You POST an array of usernames and Mojang returns only the ones that exist, so
anything missing from the reply has no account behind it.

**Ten is a hard cap**, verified against the live API: eleven names returns
HTTP 400 (`size must be between 1 and 10`) and anything larger returns HTTP
413. That is the main difference from the Roblox version, which clears 200
names per request — so expect roughly 20x more requests for the same list.

### What this cannot tell you

The Roblox version has a second stage, because Roblox exposes a signup
validator that catches censored and reserved names. **Mojang exposes no
equivalent**, so "no account holds this" is the whole answer available here,
and a name can still be unregisterable for reasons the API will not show you:

- names blocked or reserved by Mojang
- names in the ~37-day cooldown after being freed by a rename

So treat a hit as *probably* available rather than confirmed. The only way to
know for certain is to try to claim it.

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
2. **Usernames** — `g` to generate, pick a length (3-16 allowed, the wizard
   offers 3-8), pick how many.
3. **Speed** — accept the defaults.
4. **Webhook** — `n` unless you want Discord notifications.

When you generate names there is one extra question: **keep drawing new names
until one is free?** Off by default. With it on, a round that finds nothing
draws a fresh batch and goes again, stopping at the first hit or on Ctrl+C.
Rounds share one rate-limit cooldown, so repeating costs no re-learning of the
limit, and names an earlier round already checked are never re-checked.

Results land in `results/hits.txt`, written the moment each hit is found.

**Ctrl+C stops cleanly.** It finishes what is in flight, prints the summary, and
writes every name it never got an answer for to `results/unresolved.txt` so you
can re-run just those. Press it a second time to quit immediately instead.

**Minecraft username rules:** 3-16 characters, `a-z A-Z 0-9 _`. Unlike Roblox
there is no limit on underscores and they may sit at either end.

Each check prints above the live panel as it resolves - taken names dim out,
hits show bright - with the stats staying pinned underneath. `--no-stream`
turns it off.

It renders at roughly 13,000 lines/sec (pre-built styles, batched output), so
a proxyless run or a modest proxied one never notices it: the network is far
slower than the rendering. It only becomes the bottleneck on a pool pushing
past that, which is why the off switch exists.

Flags: `--no-wizard` reuses your last setup, `--debug` logs every request,
`--no-stream` quietens the feed, `--version`.

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

"""
llm_cache/demo.py  —  Three scenarios that each reveal a different facet
of eventual consistency in an LLM response cache.

Usage:
    python demo.py           # runs all three
    python demo.py stale     # scenario 1: basic stale-read window
    python demo.py ttl       # scenario 2: TTL expiry closes the window
    python demo.py conflict  # scenario 3: two workers cache the same prompt differently
    python demo.py fast      # all three with no sleeps (for CI / quick check)
"""

from __future__ import annotations
import sys
import time
import threading

from cache import SharedLLMCache
from timeline import render_timeline, render_stats

# ── ANSI ─────────────────────────────────────────────────────
RESET  = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
GREEN  = "\033[32m"; RED = "\033[31m"; YELLOW = "\033[33m"; CYAN = "\033[36m"

FAST = "fast" in sys.argv   # skip real-time sleeps


def sleep(s: float):
    if not FAST:
        time.sleep(s)


def header(n: int, title: str, subtitle: str = ""):
    w = 66
    print(f"\n{BOLD}{CYAN}{'─'*w}")
    print(f"  Scenario {n} — {title}")
    if subtitle:
        print(f"  {DIM}{subtitle}{RESET}{BOLD}{CYAN}")
    print(f"{'─'*w}{RESET}\n")


def step(msg: str):
    print(f"  {BOLD}▶  {msg}{RESET}")


def note(msg: str):
    print(f"  {DIM}{msg}{RESET}")


# ─────────────────────────────────────────────────────────────
# Scenario 1 — Basic stale-read window
# ─────────────────────────────────────────────────────────────

def scenario_stale():
    header(1, "Basic stale-read window",
           "Worker A caches a response. Worker B keeps reading stale data "
           "until the sync propagates.")

    SYNC_DELAY = 1.0   # seconds
    cache = SharedLLMCache(sync_delay=SYNC_DELAY)

    PROMPT    = "Explain gradient descent in one paragraph"
    RESPONSE  = ("Gradient descent is an optimisation algorithm that iteratively "
                 "moves model parameters in the direction that reduces the loss "
                 "function by following the negative gradient.")

    # ── t=0: Worker A caches the LLM response ────────────────
    step("t=0  Worker A receives LLM response and caches it")
    cache.set("A", PROMPT, RESPONSE, model="gpt-4o", tokens=42)
    note(f"Cached: {RESPONSE[:60]}…")
    sleep(0.1)

    # ── t=100ms: Worker B reads — cache MISS (hasn't synced yet)
    step("t=100ms  Worker B reads — MISS (not in B's shard yet)")
    r = cache.get("B", PROMPT)
    note(f"Result: {r.response[:50]!r}…" if r else "Result: None  ← cache miss")
    sleep(0.3)

    # ── Simulate B re-populating from the LLM with a slightly different response
    step("t=400ms  Worker B asks the LLM itself — gets a slightly different response")
    RESPONSE_B = ("Gradient descent minimises a loss function by nudging weights "
                  "opposite the gradient — small steps toward the steepest downhill "
                  "direction until convergence.")
    cache.set("B", PROMPT, RESPONSE_B, model="gpt-4o-mini", tokens=38)
    note(f"Worker B cached its own version: {RESPONSE_B[:55]}…")
    sleep(0.2)

    # ── Now both workers have diverged on the same key ────────
    step("t=600ms  Read from both workers — they disagree!")
    ra = cache.get("A", PROMPT)
    rb = cache.get("B", PROMPT)
    if ra and rb and ra.response != rb.response:
        print(f"  {RED}  A sees: {ra.response[:55]}…{RESET}")
        print(f"  {RED}  B sees: {rb.response[:55]}…{RESET}")
        note("⚠  Two workers returned different answers to the same question.")

    # ── Wait for both syncs to complete ──────────────────────
    step(f"Waiting {SYNC_DELAY}s for sync propagation…")
    # Always wait the real sync delay so the background Timer fires
    actual_wait = SYNC_DELAY + 0.3
    time.sleep(actual_wait)

    # ── Post-sync reads ───────────────────────────────────────
    step("Post-sync: both workers read")
    ra2 = cache.get("A", PROMPT)
    rb2 = cache.get("B", PROMPT)
    if ra2 and rb2:
        if ra2.response == rb2.response:
            print(f"  {GREEN}  Both agree: {ra2.response[:55]}…  ✓{RESET}")
            note(f"Converged on version v{ra2.version} (last-write-wins: higher version)")
        elif ra2.version == rb2.version:
            print(f"  {YELLOW}  Same-version tie — A and B both wrote v{ra2.version} with different content.{RESET}")
            note("Neither sync overwrote the other (same version = draw).")
            note("In production: use a tiebreaker (higher-quality model wins, or vector clocks).")
        else:
            print(f"  {RED}  Still diverged! A=v{ra2.version} B=v{rb2.version}{RESET}")

    print()
    render_timeline(cache.events())
    render_stats(cache)


# ─────────────────────────────────────────────────────────────
# Scenario 2 — TTL closes the stale window
# ─────────────────────────────────────────────────────────────

def scenario_ttl():
    header(2, "TTL as a consistency lever",
           "A short TTL forces workers to re-fetch from the LLM, shrinking "
           "the maximum possible stale window to TTL seconds.")

    TTL        = 0.8    # seconds
    SYNC_DELAY = 99.0   # effectively disabled — TTL does the work
    cache = SharedLLMCache(sync_delay=SYNC_DELAY, default_ttl=TTL)

    PROMPT   = "What is a transformer model?"
    RESP_V1  = "A transformer uses self-attention to process sequences in parallel."
    RESP_V2  = "Transformers replaced RNNs by attending to all positions simultaneously."

    step("t=0  Worker A caches v1 with TTL=0.8s")
    cache.set("A", PROMPT, RESP_V1, model="gpt-4o", tokens=15, ttl=TTL)

    sleep(0.2)
    step("t=200ms  Worker B reads — MISS (sync disabled, TTL not expired)")
    r = cache.get("B", PROMPT)
    note(f"B result: {'None ← miss' if not r else r.response[:50]}")

    step("t=200ms  Worker B caches its own v1 from the LLM")
    cache.set("B", PROMPT, RESP_V1, model="gpt-4o", tokens=15, ttl=TTL)

    sleep(0.3)
    step("t=500ms  Worker A updates the prompt with a better response (v2)")
    cache.set("A", PROMPT, RESP_V2, model="gpt-4o", tokens=17, ttl=TTL)

    step("t=500ms  Worker B reads — STALE (has old v1, sync not yet fired)")
    r = cache.get("B", PROMPT)
    note(f"B sees: {r.response[:60] if r else 'None'}  (stale={r and r.version < 2})")

    step(f"Waiting for TTL ({TTL}s) to expire…")
    sleep(TTL + 0.1)

    step("After TTL expiry — both workers read")
    ra = cache.get("A", PROMPT)
    rb = cache.get("B", PROMPT)
    note(f"A: {'MISS — expired, will re-fetch' if ra is None else ra.response[:50]}")
    note(f"B: {'MISS — expired, will re-fetch' if rb is None else rb.response[:50]}")
    note("After TTL, both workers are forced to re-fetch — the stale window is bounded.")

    print()
    render_timeline(cache.events())
    render_stats(cache)


# ─────────────────────────────────────────────────────────────
# Scenario 3 — Semantic near-miss (same prompt, different capitalisation)
# ─────────────────────────────────────────────────────────────

def scenario_conflict():
    header(3, "Write conflict — same semantic prompt, two workers",
           "Workers A and B each handle the same user question (different "
           "sessions). They race to cache it. The last write wins after sync.")

    SYNC_DELAY = 0.8
    cache = SharedLLMCache(sync_delay=SYNC_DELAY)

    PROMPT = "how does backpropagation work"

    RESP_A = ("Backpropagation computes gradients by applying the chain rule "
              "backwards through the network layers from the loss to each weight.")
    RESP_B = ("Backprop is the algorithm that calculates how much each weight "
              "contributed to the error, using the chain rule in reverse.")

    events: list[str] = []

    def worker_a():
        sleep(0.0)
        step("Worker A  →  caches response (model=gpt-4o, tokens=32)")
        cache.set("A", PROMPT, RESP_A, model="gpt-4o", tokens=32)
        events.append("A_write")
        sleep(0.2)
        step("Worker A  reads back its own entry")
        r = cache.get("A", PROMPT)
        note(f"  A sees: {r.response[:60] if r else 'None'}…")

    def worker_b():
        sleep(0.15)  # B is 150ms behind A
        step("Worker B  →  caches response (model=gpt-4o-mini, tokens=28)")
        cache.set("B", PROMPT, RESP_B, model="gpt-4o-mini", tokens=28)
        events.append("B_write")
        sleep(0.2)
        step("Worker B  reads back its own entry")
        r = cache.get("B", PROMPT)
        note(f"  B sees: {r.response[:60] if r else 'None'}…")

    t_a = threading.Thread(target=worker_a)
    t_b = threading.Thread(target=worker_b)
    t_a.start(); t_b.start()
    t_a.join();  t_b.join()

    step("t=350ms  Direct comparison — workers diverged on the same key")
    ra = cache.get("A", PROMPT)
    rb = cache.get("B", PROMPT)
    if ra and rb:
        match = ra.response == rb.response
        colour = GREEN if match else RED
        print(f"  {colour}  A (v{ra.version}): {ra.response[:55]}…{RESET}")
        print(f"  {colour}  B (v{rb.version}): {rb.response[:55]}…{RESET}")
        if not match:
            note("⚠  Diverged — each worker cached a different answer to the same question.")

    step(f"Waiting {SYNC_DELAY}s for cross-worker sync…")
    # Always use real time so the background Timer fires
    time.sleep(SYNC_DELAY + 0.3)

    step("Post-sync — checking convergence")
    ra2 = cache.get("A", PROMPT)
    rb2 = cache.get("B", PROMPT)
    if ra2 and rb2:
        match = ra2.response == rb2.response
        c = GREEN if match else RED
        print(f"  {c}  A (v{ra2.version}): {ra2.response[:55]}…{RESET}")
        print(f"  {c}  B (v{rb2.version}): {rb2.response[:55]}…{RESET}")
        if match:
            winner = "A" if ra2.written_by == "A" else "B"
            note(f"Last-write-wins: worker {winner}'s response ({ra2.model}) is now authoritative.")
        else:
            note("⚠  Both workers wrote v1 simultaneously — neither overwrites the other "
                 "(same version = tie). In production you'd resolve this with a tiebreaker "
                 "(e.g. prefer higher-quality model, or use vector clocks).")

    print()
    render_timeline(cache.events())
    render_stats(cache)


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

SCENARIOS = {
    "stale":    scenario_stale,
    "ttl":      scenario_ttl,
    "conflict": scenario_conflict,
}

def summary():
    w = 66
    print(f"\n{BOLD}{CYAN}{'─'*w}")
    print(f"  Summary — Eventual consistency in LLM caches")
    print(f"{'─'*w}{RESET}\n")
    print(f"""  {BOLD}The stale-read window{RESET}
    Exists between a WRITE on one worker and the SYNC reaching the
    other. During this window, a worker returns outdated LLM output.
    In production this manifests as two users getting different
    answers to the same question from the same service.

  {BOLD}Three ways to close (or bound) the window:{RESET}

    1. {YELLOW}Shorter sync delay{RESET}   — propagate sooner (Redis replication,
       write-through cache). Reduces window but adds latency to writes.

    2. {YELLOW}TTL expiry{RESET}           — entries self-invalidate. Worst-case stale
       window = TTL. Good for slowly-changing data (model versions,
       system prompts). Bad for real-time personalisation.

    3. {YELLOW}Explicit invalidation{RESET} — on a model update or prompt change,
       broadcast a DELETE to all workers. Consistent but requires
       coordination infrastructure (pub/sub, cache bus).

  {BOLD}LLM-specific wrinkle:{RESET}
    Unlike a database row, two LLM responses to the same prompt are
    rarely identical even from the same model. "Stale" here means
    "from an older model version" or "generated before context changed"
    — not just "value X vs value Y". This makes {CYAN}semantic caching{RESET}
    (topic 24) the natural next step: cache by embedding similarity,
    not by exact prompt hash.
""")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "fast"]
    run  = args[0].lower() if args else "all"

    print(f"\n{BOLD}LLM Response Cache — Eventual Consistency Demo{RESET}")
    print(f"  {DIM}sync_delay controls how long the stale window stays open{RESET}\n")

    if run == "all":
        for fn in SCENARIOS.values():
            fn()
    elif run in SCENARIOS:
        SCENARIOS[run]()
    else:
        print(f"  Unknown scenario {run!r}. Choose: {', '.join(SCENARIOS)} or all")
        sys.exit(1)

    summary()
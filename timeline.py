"""
llm_cache/timeline.py  —  Renders the event log as an annotated timeline.

Output looks like:

  t=0ms      t=500ms     t=1000ms    t=1500ms    t=2000ms
  |           |           |           |           |
  A ──WRITE───────────────────────────────────────────────►
              B ──STALE───────────SYNC────HIT─────────────►
              |←── stale window: 1000ms ──────────────────|

Colour key:
  GREEN  = READ_HIT  (fresh data)
  RED    = READ_STALE (stale data — the window we're demonstrating)
  YELLOW = WRITE
  CYAN   = SYNC
  DIM    = READ_MISS / EXPIRE
"""

from __future__ import annotations
import math
from cache import CacheEvent

# ANSI
RESET  = "\033[0m"; BOLD   = "\033[1m"; DIM    = "\033[2m"
GREEN  = "\033[32m"; RED    = "\033[31m"; YELLOW = "\033[33m"
CYAN   = "\033[36m"; MAGENTA= "\033[35m"; WHITE  = "\033[37m"
BG_RED = "\033[41m"; BG_YLW = "\033[43m"

OP_STYLE = {
    "READ_HIT":   (GREEN,   "HIT",   "●"),
    "READ_STALE": (RED,     "STALE", "◆"),
    "READ_MISS":  (DIM,     "MISS",  "○"),
    "WRITE":      (YELLOW,  "WRITE", "▲"),
    "SYNC":       (CYAN,    "SYNC",  "⟳"),
    "SYNC_SKIP":  (DIM,     "SKIP",  "·"),
    "EXPIRE":     (MAGENTA, "EXP",   "✕"),
    "INVALIDATE": (MAGENTA, "INVAL", "✕"),
}


def render_timeline(events: list[CacheEvent], width: int = 72):
    if not events:
        print("  (no events)")
        return

    t_start = events[0].t
    t_end   = max(e.t for e in events) + 200   # a little padding
    span    = max(t_end - t_start, 1)

    def t_to_col(t: float) -> int:
        return int((t - t_start) / span * width)

    # ── header ──────────────────────────────────────────────
    print(f"\n{BOLD}  Timeline{RESET}  (each column ≈ {span/width:.0f} ms)\n")

    # tick marks
    tick_line  = " " * 4
    label_line = " " * 4
    n_ticks    = 6
    for i in range(n_ticks + 1):
        pos  = int(i / n_ticks * width)
        t_ms = int(i / n_ticks * span + t_start)
        lbl  = f"t={t_ms}ms"
        tick_line  += (" " * (pos - len(tick_line) + 4)) + "|"
        label_line += (" " * (pos - len(label_line) + 4)) + lbl
    print("  " + tick_line)
    print("  " + label_line + "\n")

    # ── per-worker rows ──────────────────────────────────────
    workers = sorted({e.worker for e in events})
    key_groups = sorted({e.key for e in events})

    for key in key_groups:
        key_events = [e for e in events if e.key == key]
        print(f"  {DIM}key={key}{RESET}")

        for worker in workers:
            w_events = [e for e in key_events if e.worker == worker]
            if not w_events:
                continue

            row = [" "] * (width + 1)
            annotations: list[tuple[int, str]] = []

            for e in w_events:
                col   = t_to_col(e.t)
                style, label, icon = OP_STYLE.get(e.op, (WHITE, e.op[:5], "?"))
                row[min(col, width)] = style + icon + RESET
                annotations.append((col, f"{style}{label}{RESET}@{e.t:.0f}ms"))

            line = f"  {BOLD}{worker}{RESET} ──" + "".join(row) + "──►"
            print(line)

            # annotation line (op labels beneath the row)
            ann_line = " " * 5
            for col, label in sorted(annotations):
                pad = col - len(_strip_ansi(ann_line)) + 5
                if pad > 0:
                    ann_line += " " * pad
                ann_line += label + " "
            print("    " + ann_line)

        print()

    # ── stale window analysis ────────────────────────────────
    _render_stale_analysis(events, t_start)


def _render_stale_analysis(events: list[CacheEvent], t_start: float):
    """Find write→sync gaps where at least one READ_STALE occurred."""
    # Group by key
    keys = sorted({e.key for e in events})
    found_any = False

    for key in keys:
        k_events = [e for e in events if e.key == key]

        writes  = [e for e in k_events if e.op == "WRITE"]
        syncs   = [e for e in k_events if e.op == "SYNC"]
        stales  = [e for e in k_events if e.op == "READ_STALE"]

        if not stales:
            continue

        found_any = True
        print(f"  {BOLD}Stale-read window  (key={key}){RESET}")

        for write in writes:
            # find the sync that closed this write's window
            relevant_syncs = [s for s in syncs if s.t > write.t and s.worker != write.worker]
            if not relevant_syncs:
                print(f"    {YELLOW}▲ WRITE{RESET} by worker {write.worker} at t={write.t:.0f}ms  — sync not yet observed")
                continue

            sync_evt  = min(relevant_syncs, key=lambda s: s.t)
            window_ms = sync_evt.t - write.t
            stale_cnt = sum(1 for s in stales
                            if write.t <= s.t <= sync_evt.t
                            and s.worker != write.worker)

            # draw the window bar
            bar_w   = 40
            sync_pos = min(int(window_ms / 2000 * bar_w), bar_w - 1)  # scale to 2 s
            bar      = (BG_RED + " " * sync_pos + RESET +
                        CYAN + "⟳" + RESET +
                        DIM + " " * (bar_w - sync_pos) + RESET)

            print(f"    {YELLOW}▲{RESET} WRITE  by {write.worker}  t={write.t:.0f}ms")
            print(f"    {CYAN}⟳{RESET} SYNC   to {sync_evt.worker}  t={sync_evt.t:.0f}ms")
            print(f"    {RED}◆{RESET} {stale_cnt} stale read(s) during window")
            print(f"\n    |{bar}|")
            print(f"     {RED}←── stale window: {window_ms:.0f} ms ───────────────{RESET}{CYAN}synced{RESET}\n")

    if not found_any:
        print(f"  {GREEN}No stale reads observed.{RESET}  "
              f"(Try increasing read frequency or reducing sync_delay.)\n")


def render_stats(cache, workers=("A", "B")):
    print(f"  {BOLD}Cache stats{RESET}\n")
    for w in workers:
        s = cache.stats(w)
        hit_bar   = _mini_bar(s["hit_rate"],   40, GREEN)
        stale_bar = _mini_bar(s["stale_rate"], 40, RED)
        miss_bar  = _mini_bar(
            100 - s["hit_rate"] - s["stale_rate"], 40, DIM)
        bar = hit_bar + stale_bar + miss_bar
        print(f"  Worker {BOLD}{w}{RESET}  "
              f"{GREEN}hits={s['hits']}{RESET}  "
              f"{RED}stales={s['stales']}{RESET}  "
              f"{DIM}misses={s['misses']}{RESET}  "
              f"total={s['total']}")
        print(f"           [{bar}{RESET}]  "
              f"{GREEN}{s['hit_rate']}% fresh{RESET}  "
              f"{RED}{s['stale_rate']}% stale{RESET}\n")


def _mini_bar(pct: float, width: int, colour: str) -> str:
    n = max(0, min(width, int(pct / 100 * width)))
    return colour + "█" * n


def _strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)

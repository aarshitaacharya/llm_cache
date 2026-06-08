# ──────────────────────────────────────────────────────────────────
# LLM Cache — Eventual Consistency Demo
# ──────────────────────────────────────────────────────────────────
.PHONY: all stale ttl conflict fast help clean

# Default: run all three scenarios with real timing (shows ~1s stale windows)
all:
	@python demo.py all

# Run individual scenarios
stale:
	@python demo.py stale

ttl:
	@python demo.py ttl

conflict:
	@python demo.py conflict

# Fast mode: skips sleep() calls, runs in under 5s (sync timers still fire)
fast:
	@python demo.py all fast

clean:
	@find . -name "__pycache__" -type d | xargs rm -rf
	@find . -name "*.pyc" -delete
	@echo "  Cleaned."

help:
	@echo ""
	@echo "  make           — all three scenarios (real timing, ~4s)"
	@echo "  make stale     — scenario 1: basic stale-read window"
	@echo "  make ttl       — scenario 2: TTL as a consistency lever"
	@echo "  make conflict  — scenario 3: concurrent write conflict"
	@echo "  make fast      — all scenarios without sleep() pauses"
	@echo "  make clean     — remove Python cache files"
	@echo ""

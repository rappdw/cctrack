#!/usr/bin/env python3
"""cctrack — Claude Code cost reporter.

Scans JSONL logs from Claude Code and Sandy sandboxes,
tallies token usage, and prints daily + monthly cost summaries.

Usage:
    python cctrack.py                          # local only
    python cctrack.py --days 7                 # last 7 days
    python cctrack.py --remote dgx macbook-air # aggregate with remote hosts
    python cctrack.py --dirs ~/custom          # custom log directory
"""

import argparse
import base64
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Rate card (per million tokens) ──────────────────────────────────────

def _oa(family, tier, inp, cached, out, cache_write=None, lc=None):
    """Build an OpenAI rate-card row (see the OpenAI block in RATES)."""
    row = {"family": family, "tier": tier, "provider": "openai",
           "input": inp, "output": out,
           "cache_read": inp if cached is None else cached}
    row["cache_write"] = row["cache_write_1h"] = inp if cache_write is None else cache_write
    if lc:
        l_in, l_cached, l_out, *l_cw = lc
        row["long_context"] = {
            "over": 272_000, "input": l_in, "output": l_out,
            "cache_read": l_in if l_cached is None else l_cached,
        }
        row["long_context"]["cache_write"] = row["long_context"]["cache_write_1h"] = (
            l_cw[0] if l_cw else l_in)
    return row


# Every model VERSION gets its own row. Matching (see _family_matches) only lets
# a family absorb dated snapshots and non-version suffixes — never a new minor
# version — so a newly released model shows up as unknown ("?") instead of
# silently inheriting its predecessor's price. (claude-opus-5-5 once inherited
# Opus 5's $5/$25 when it actually costs $4/$20 with 0.05× cache reads.)
# cache_write is the 5-minute TTL rate; cache_write_1h the 1-hour TTL rate.
# "tier" selects the energy model (see ENERGY_WH_PER_MTOK), not the price.
# "intro" is a time-limited launch price applying to usage on/before "until".
# "long_context" rates apply to a whole request whose prompt (input + cache
# reads + cache writes) exceeds "over".
# Source: https://platform.claude.com/docs/en/about-claude/pricing (2026-11-24).
def _an(family, tier, inp, cw5, cw1h, read, out, lc=None):
    """Build an Anthropic rate-card row."""
    row = {"family": family, "tier": tier, "provider": "anthropic", "input": inp,
           "output": out, "cache_read": read, "cache_write": cw5, "cache_write_1h": cw1h}
    if lc:
        l_in, l_cw5, l_cw1h, l_read, l_out, over = lc
        row["long_context"] = {"over": over, "input": l_in, "output": l_out,
                               "cache_read": l_read, "cache_write": l_cw5,
                               "cache_write_1h": l_cw1h}
    return row


RATES = [
    _an("claude-fable-5-2",      "frontier",  8.00, 10.00, 16.00, 0.20,  40.00),
    _an("claude-mythos-5-2",     "frontier",  8.00, 10.00, 16.00, 0.20,  40.00),
    _an("claude-fable-5-1",      "frontier", 10.00, 12.50, 20.00, 0.25,  50.00),
    _an("claude-mythos-5-1",     "frontier", 10.00, 12.50, 20.00, 0.25,  50.00),
    _an("claude-fable-5",        "frontier", 10.00, 12.50, 20.00, 1.00,  50.00),
    _an("claude-mythos-5",       "frontier", 10.00, 12.50, 20.00, 1.00,  50.00),
    # Mythos Preview predates the current price list; kept at Mythos 5 rates.
    _an("claude-mythos-preview", "frontier", 10.00, 12.50, 20.00, 1.00,  50.00),
    _an("claude-opus-5-5",       "opus",      4.00,  5.00,  8.00, 0.20,  20.00),
    _an("claude-opus-5",         "opus",      5.00,  6.25, 10.00, 0.50,  25.00),
    _an("claude-opus-4-8",       "opus",      5.00,  6.25, 10.00, 0.50,  25.00),
    _an("claude-opus-4-7",       "opus",      5.00,  6.25, 10.00, 0.50,  25.00),
    _an("claude-opus-4-6",       "opus",      5.00,  6.25, 10.00, 0.50,  25.00),
    _an("claude-opus-4-5",       "opus",      5.00,  6.25, 10.00, 0.50,  25.00),
    _an("claude-opus-4-1",       "opus",     15.00, 18.75, 30.00, 1.50,  75.00),
    _an("claude-opus-4",         "opus",     15.00, 18.75, 30.00, 1.50,  75.00),
    _an("claude-sonnet-5-5",     "sonnet",    2.00,  2.50,  4.00, 0.10,  10.00),
    # Sonnet 5's $2/$10 launch price became permanent; the scheduled Sep 1 2026
    # rise to $3/$15 was cancelled.
    _an("claude-sonnet-5",       "sonnet",    2.00,  2.50,  4.00, 0.20,  10.00),
    _an("claude-sonnet-4-6",     "sonnet",    3.00,  3.75,  6.00, 0.30,  15.00),
    _an("claude-sonnet-4-5",     "sonnet",    3.00,  3.75,  6.00, 0.30,  15.00,
        lc=(6.00, 7.50, 12.00, 0.60, 22.50, 200_000)),
    _an("claude-sonnet-4",       "sonnet",    3.00,  3.75,  6.00, 0.30,  15.00,
        lc=(6.00, 7.50, 12.00, 0.60, 22.50, 200_000)),
    _an("claude-sonnet-3-7",     "sonnet",    3.00,  3.75,  6.00, 0.30,  15.00),
    _an("claude-sonnet-3-5",     "sonnet",    3.00,  3.75,  6.00, 0.30,  15.00),
    _an("claude-haiku-5-5",      "haiku",     0.10,  0.125, 0.20, 0.01,   0.50,
        lc=(0.50, 0.625, 1.00, 0.05, 2.50, 100_000)),
    _an("claude-haiku-4-5",      "haiku",     1.00,  1.25,  2.00, 0.10,   5.00),
    _an("claude-haiku-3-5",      "haiku",     0.80,  1.00,  1.60, 0.08,   4.00),
    _an("claude-haiku-3",        "haiku",     0.25,  0.30,  0.50, 0.03,   1.25),
] + [
    # ── OpenAI (Codex CLI). Source: https://developers.openai.com/api/docs/pricing
    # (Standard tier, fetched 2026-10-12; re-checked 2026-11-16 and 2026-11-24). OpenAI lists
    # 30-minute and 12-hour cache writes, but Codex logs a single
    # cache_write_input_tokens count with no TTL, so writes are priced at the
    # 30-minute rate and cache_write == cache_write_1h (12h writes, at 2x, would
    # be under-priced). Where the price list shows no cache-write
    # price, cache writes are ordinary input and bill at the input rate; where it
    # shows no cached-input price (the -pro models), cached tokens bill at input.
    # "long_context" rates apply to a whole request whose input exceeds "over".
    # Codex variants without their own row (gpt-5.1-codex, …) price as their base
    # model — consistent with the one listed variant (gpt-5.3-codex == gpt-5.2).
    # "tier" is a price-class proxy for the energy model only.
    _oa("gpt-6.2-astra", "frontier",  8.00, 0.40,  40.00, 10.00, lc=(16.00, 0.80, 60.00, 20.00)),
    _oa("gpt-6.2-sol",   "sonnet",    2.00, 0.10,  10.00,  2.50, lc=(4.00, 0.20, 15.00, 5.00)),
    _oa("gpt-6-astra",   "frontier", 10.00, 1.00,  50.00, 12.50, lc=(20.00, 2.00, 75.00, 25.00)),
    _oa("gpt-6.1-sol",   "sonnet",    2.00, 0.10,  10.00,  2.50, lc=(4.00, 0.20, 15.00, 5.00)),
    _oa("gpt-6.1-terra", "sonnet",    1.00, 0.05,   5.00,  1.25, lc=(2.00, 0.10,  7.50, 2.50)),
    _oa("gpt-6-sol",     "sonnet",    2.00, 0.20,  10.00,  2.50, lc=(4.00, 0.40, 15.00, 5.00)),
    _oa("gpt-6-luna",    "haiku",     0.10, 0.01,   0.50,  0.125, lc=(0.20, 0.02, 0.75, 0.25)),
    _oa("gpt-5.6-sol",   "opus",      4.00, 0.40,  20.00,  5.00, lc=(8.00, 0.80, 30.00, 10.00)),
    _oa("gpt-5.6-terra", "sonnet",    2.00, 0.20,  12.00,  2.50, lc=(4.00, 0.40, 18.00, 5.00)),
    _oa("gpt-5.6-luna",  "haiku",     0.20, 0.02,   1.20,  0.25, lc=(0.40, 0.04, 1.80, 0.50)),
    _oa("gpt-5.5-pro",   "frontier", 30.00, None, 180.00, lc=(60.00, None, 270.00)),
    _oa("gpt-5.5",       "opus",      5.00, 0.50,  30.00, lc=(10.00, 1.00, 45.00)),
    _oa("gpt-5.4-pro",   "frontier", 30.00, None, 180.00, lc=(60.00, None, 270.00)),
    _oa("gpt-5.4-mini",  "haiku",     0.75, 0.075,  4.50),
    _oa("gpt-5.4-nano",  "haiku",     0.20, 0.02,   1.25),
    _oa("gpt-5.4",       "sonnet",    2.50, 0.25,  15.00, lc=(5.00, 0.50, 22.50)),
    _oa("gpt-5.3-codex", "sonnet",    1.75, 0.175, 14.00),
    _oa("gpt-5.2-pro",   "frontier", 21.00, None, 168.00),
    _oa("gpt-5.2",       "sonnet",    1.75, 0.175, 14.00),
    _oa("gpt-5.1",       "sonnet",    1.25, 0.125, 10.00),
    _oa("gpt-5-pro",     "frontier", 15.00, None, 120.00),
    _oa("gpt-5-mini",    "haiku",     0.25, 0.025,  2.00),
    _oa("gpt-5-nano",    "haiku",     0.05, 0.005,  0.40),
    _oa("gpt-5",         "sonnet",    1.25, 0.125, 10.00),
    # Older models still on the price list; Codex doesn't use them by default.
    _oa("gpt-4.1-mini",  "haiku",     0.40, 0.10,   1.60),
    _oa("gpt-4.1",       "sonnet",    2.00, 0.50,   8.00),
    _oa("gpt-4o-mini",   "haiku",     0.15, 0.075,  0.60),
    _oa("gpt-4o",        "sonnet",    2.50, 1.25,  10.00),
    _oa("o3-pro",        "frontier", 20.00, None,  80.00),
    _oa("o3",            "sonnet",    2.00, 0.50,   8.00),
]

# Explicit aliases only: never guess a newly released model's price.
MODEL_ALIASES = {
    "gpt-5.1-codex-mini": "gpt-5-mini",
    "gpt-5.1-codex-max": "gpt-5.1",
    "gpt-5.6": "gpt-5.6-sol",
}

# Per-model premiums/discounts; an unlisted tier is not inferred from a peer.
# Historical Claude Opus 4.6/4.7 fast rates were 6x, later Opus fast is 2x.
FAST_MULTIPLIERS = {
    "claude-opus-4-6": 6.0, "claude-opus-4-7": 6.0,
    "claude-opus-4-8": 2.0, "claude-opus-5": 2.0, "claude-opus-5-5": 2.0,
    "claude-fable-5-2": 2.0, "claude-mythos-5-2": 2.0,
    "gpt-5.5": 2.5,
    **dict.fromkeys(("gpt-5.3-codex", "gpt-5.4", "gpt-5.6-sol", "gpt-5.6-terra",
                     "gpt-5.6-luna", "gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra",
                     "gpt-6.2-astra", "gpt-6.2-sol",
                     "gpt-6-luna"), 2.0),
}
FLEX_MODELS = {
    "gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.5-pro",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4-pro",
    "gpt-5.2", "gpt-5.1", "gpt-5", "gpt-5-mini", "gpt-5-nano",
}


def normalize_service_tier(value) -> str | None:
    if value in ("fast", "priority"):
        return "fast"
    if value in ("standard", "default"):
        return "standard"
    return "flex" if value == "flex" else None


def _canonical_model(model: str) -> str:
    model = re.sub(r"-fast$", "", model)
    # Older Claude ids put the version before the family name.
    model = re.sub(r"^claude-(\d+(?:-\d+)?)-(sonnet|haiku|opus)(?=-|$)",
                   r"claude-\2-\1", model)
    for alias in sorted(MODEL_ALIASES, key=len, reverse=True):
        if model == alias or re.fullmatch(re.escape(alias) + r"-(?:\d{8}|\d{4}-\d{2}-\d{2})", model):
            return MODEL_ALIASES[alias] + model[len(alias):]
    return model

SONNET_RATES = next(r for r in RATES if r["family"] == "claude-sonnet-4")  # fallback
# Unknown OpenAI/Codex models fall back to the base GPT-5 rates (and are flagged
# "?"), not to Claude Sonnet — the two price lists are unrelated.
OPENAI_FALLBACK_RATES = next(r for r in RATES if r["family"] == "gpt-5")

# Suffix words that denote a DIFFERENT size/price tier. A model whose id adds
# one of these past a family's prefix (e.g. an unlisted gpt-5.9-codex-mini vs gpt-5.9) must
# not silently inherit that family's price — it's treated as unknown instead.
_TIER_WORDS = {"mini", "nano", "pro", "max", "luna", "terra", "sol", "astra"}


def _family_matches(model: str, family: str) -> bool:
    """A model belongs to a family if it IS the family, or extends it at a
    hyphen boundary without naming a different minor version or size tier. So
    "gpt-5" matches "gpt-5-2025-08-07" and "gpt-5-codex" but not "gpt-5.5" or
    "gpt-5-mini", and "claude-opus-4" matches "claude-opus-4-20250514" but not
    "claude-opus-4-1" (which has its own row)."""
    if not model.startswith(family):
        return False
    rest = model[len(family):]
    if not rest:
        return True
    if not rest.startswith("-"):
        return False
    parts = rest[1:].lower().split("-")
    # A short number right after the family is a different minor version
    # (claude-opus-5 → claude-opus-5-5); a long one is a date snapshot
    # (claude-opus-4-20250514, gpt-5-2025-08-07), which belongs to the family.
    if parts[0].isdigit() and len(parts[0]) <= 2:
        return False
    return not (_TIER_WORDS & set(parts))


def _is_openai_model(model: str) -> bool:
    return model.startswith(("gpt-", "codex", "o1", "o3", "o4"))


def _find_rate_row_uncached(model: str) -> dict | None:
    model = _canonical_model(model)
    for r in sorted(RATES, key=lambda r: len(r["family"]), reverse=True):
        if _family_matches(model, r["family"]):
            return r
    return None


# Memo of _find_rate_row per model string. It rebuilds itself when RATES or
# MODEL_ALIASES is replaced by another object or changes length (tests patch
# them). The key holds strong refs so a freed object's id can't be reused.
_rate_row_memo: dict = {}
_rate_row_memo_key: tuple | None = None


def _find_rate_row(model: str) -> dict | None:
    global _rate_row_memo, _rate_row_memo_key
    key = _rate_row_memo_key
    if (key is None or key[0] is not RATES or key[1] != len(RATES)
            or key[2] is not MODEL_ALIASES or key[3] != len(MODEL_ALIASES)):
        _rate_row_memo = {}
        _rate_row_memo_key = (RATES, len(RATES), MODEL_ALIASES, len(MODEL_ALIASES))
    try:
        return _rate_row_memo[model]
    except KeyError:
        row = _rate_row_memo[model] = _find_rate_row_uncached(model)
        return row


def get_rates(model: str, date_str: str | None = None,
              request_input: int | None = None,
              service_tier: str | None = None) -> dict:
    """Match model name to a rate-card family. Unknown models fall back to
    Sonnet (Claude) or GPT-5 (OpenAI). When `date_str` (YYYY-MM-DD) falls within
    a family's introductory-pricing window the introductory rates apply; when
    `request_input` (the request's total input tokens) exceeds a family's
    long-context threshold, the long-context rates apply."""
    r = _find_rate_row(model)
    if r is None:
        return OPENAI_FALLBACK_RATES if _is_openai_model(model) else SONNET_RATES
    intro = r.get("intro")
    if intro and date_str and date_str <= intro["until"]:
        merged = dict(r)
        merged.update({k: v for k, v in intro.items() if k != "until"})
        r = merged
    lc = r.get("long_context")
    if lc and request_input is not None and request_input > lc["over"]:
        merged = dict(r)
        merged.update({k: v for k, v in lc.items() if k != "over"})
        r = merged
    tier = normalize_service_tier(service_tier)
    if tier is None and model.endswith("-fast"):
        tier = "fast"
    multiplier = (FAST_MULTIPLIERS.get(r["family"], 1.0) if tier == "fast" else
                  0.5 if tier == "flex" and r["family"] in FLEX_MODELS else 1.0)
    if multiplier != 1.0:
        r = {**r, **{k: r[k] * multiplier for k in
                    ("input", "output", "cache_read", "cache_write", "cache_write_1h")}}
    return r


def is_known_model(model: str) -> bool:
    """True if the model matches a rate-card family (i.e. wasn't priced by fallback)."""
    return _find_rate_row(model) is not None


def calculate_cost(model: str, input_t: int, output_t: int, cache_read: int,
                   cache_write: int, cache_write_1h: int = 0,
                   date_str: str | None = None,
                   request_input: int | None = None,
                   service_tier: str | None = None) -> float:
    r = get_rates(model, date_str, request_input, service_tier)
    return (
        input_t / 1_000_000 * r["input"]
        + output_t / 1_000_000 * r["output"]
        + cache_read / 1_000_000 * r["cache_read"]
        + cache_write / 1_000_000 * r["cache_write"]
        + cache_write_1h / 1_000_000 * r["cache_write_1h"]
    )


# ── Energy estimation (opt-in, order-of-magnitude) ──────────────────────
#
# Anthropic does not publish per-token energy figures or model sizes, so this
# is an ESTIMATE, not a measurement. The table below is anchored on the
# published full-stack per-prompt numbers the major labs have disclosed
# (Google: 0.24 Wh median Gemini text prompt, Aug 2025; OpenAI: ~0.34 Wh
# average ChatGPT query, Jun 2025 — both include accelerator, host, cooling
# and idle capacity), then apportioned across token classes by their relative
# compute cost:
#   - output tokens dominate: decode is sequential and memory-bandwidth bound
#   - input / cache writes are prefill: parallel, far cheaper per token
#   - cache reads skip prefill entirely (~0.1× input, mirroring the price)
# Sanity check: a 1,000-in / 300-out Sonnet-tier prompt lands at ~0.28 Wh,
# matching the published anchors. Frontier tiers scale up from there.
#
# Both cache-write TTLs use the same figure — a 1-hour TTL costs more money
# but the same compute; only the storage duration differs.
#
# Treat results as ±1 order of magnitude. Scale the whole model with the
# CCTRACK_ENERGY_SCALE environment variable if you have better numbers.
ENERGY_WH_PER_MTOK = {
    "frontier": {"input": 120.0, "output": 2400.0, "cache_read": 12.0, "cache_write": 150.0},
    "opus":     {"input": 100.0, "output": 2000.0, "cache_read": 10.0, "cache_write": 125.0},
    "sonnet":   {"input":  40.0, "output":  800.0, "cache_read":  4.0, "cache_write":  50.0},
    "haiku":    {"input":  12.0, "output":  240.0, "cache_read":  1.2, "cache_write":  15.0},
}

DEFAULT_ENERGY_TIER = "sonnet"

# A typical US household uses ~29 kWh/day (EIA, ~10,500 kWh/yr).
US_HOUSEHOLD_KWH_PER_DAY = 29.0


def energy_scale() -> float:
    """Global multiplier for the energy model, from CCTRACK_ENERGY_SCALE."""
    try:
        return float(os.environ.get("CCTRACK_ENERGY_SCALE", "1"))
    except ValueError:
        return 1.0


def get_energy_rates(model: str) -> dict:
    """Wh per million tokens for a model's compute tier."""
    r = _find_rate_row(model)
    return ENERGY_WH_PER_MTOK[r["tier"] if r else DEFAULT_ENERGY_TIER]


def estimate_energy_wh(model: str, input_t: int, output_t: int, cache_read: int,
                       cache_write: int, cache_write_1h: int = 0,
                       scale: float | None = None) -> float:
    """Estimated inference energy in watt-hours. See ENERGY_WH_PER_MTOK —
    this is an order-of-magnitude estimate, not a measurement."""
    e = get_energy_rates(model)
    return (energy_scale() if scale is None else scale) * (
        input_t / 1_000_000 * e["input"]
        + output_t / 1_000_000 * e["output"]
        + cache_read / 1_000_000 * e["cache_read"]
        + (cache_write + cache_write_1h) / 1_000_000 * e["cache_write"]
    )


def format_energy(wh: float) -> str:
    """Human-readable energy: Wh below 1 kWh, kWh above."""
    if wh < 1000:
        return f"{wh:,.1f} Wh"
    return f"{wh / 1000:,.2f} kWh"


# ── File discovery ──────────────────────────────────────────────────────

# Sandy sandboxes (https://github.com/rappdw/sandy). Per sandy's host-side path
# contract, each sandbox root holds one directory per agent home — claude/
# (mounted at ~/.claude inside the sandbox) and codex/ (~/.codex). Everything
# else under a sandbox root is private. Roots are found via `sandy
# --print-state` (sandboxes[].path, which follows $SANDY_HOME); without sandy on
# PATH we fall back to $SANDY_HOME/sandboxes/* (default ~/.sandy). Dot-entries
# are never sandboxes: an in-progress `sandy --rsync-from` pull lives in a
# hidden .rsync-from.<name>.<pid>/ copy, and counting it would double the
# sandbox's spend.

def sandy_home() -> Path:
    return Path(os.environ.get("SANDY_HOME") or (Path.home() / ".sandy"))


_SANDY_STATE_UNSET = object()
_sandy_state_cache = _SANDY_STATE_UNSET


def _sandy_print_state() -> list[str] | None:
    """sandboxes[].path from `sandy --print-state`, or None if unavailable.
    Asked once per run (cached): it's needed by several discovery steps.
    stdin is /dev/null so sandy can never wait on, or draw to, the terminal;
    on a timeout cctrack says so and falls back to scanning sandboxes/."""
    global _sandy_state_cache
    if _sandy_state_cache is not _SANDY_STATE_UNSET:
        return _sandy_state_cache
    _sandy_state_cache = _query_sandy_state()
    return _sandy_state_cache


def sandy_state_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("CCTRACK_SANDY_TIMEOUT", "15")))
    except ValueError:
        return 15.0


def _query_sandy_state() -> list[str] | None:
    import shutil
    if not shutil.which("sandy"):
        return None
    progress = Progress("Asking sandy for its sandboxes")
    progress.update(force=True)
    try:
        r = subprocess.run(["sandy", "--print-state"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=sandy_state_timeout())
        if r.returncode != 0:
            return None
        state = json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        progress.done()
        print(f"  Note: `sandy --print-state` didn't answer within "
              f"{sandy_state_timeout():.0f}s; scanning {sandy_home() / 'sandboxes'} "
              f"directly instead (CCTRACK_SANDY_TIMEOUT changes the wait).", file=sys.stderr)
        return None
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    finally:
        progress.done()
    boxes = state.get("sandboxes") if isinstance(state, dict) else None
    if not isinstance(boxes, list):
        return None
    return [b["path"] for b in boxes if isinstance(b, dict) and b.get("path")]


def _is_sandbox_name(name: str, has_workspace_json: bool | None) -> bool:
    """Which entries of sandboxes/ are real sandboxes. A sandbox is named
    <workspace basename>-<sha8> and nothing strips a leading dot, so a workspace
    like ~/.dotfiles gets a REAL sandbox ".dotfiles-1a2b3c4d". Among dot-entries:
    .rsync-from.* is an in-progress copy of another sandbox (counting it double
    counts); *.lock dirs hold only a pid; any other dot-dir is a sandbox iff it
    holds WORKSPACE.json (written at every launch since sandy 0.10.1).
    `has_workspace_json` None means "unknown" (trust the source)."""
    if name in (".", ".."):
        return False
    if not name.startswith("."):
        return True
    if name.startswith(".rsync-from.") or name.endswith(".lock"):
        return False
    return has_workspace_json is not False


def sandbox_roots() -> list[Path]:
    """Root directory of every Sandy sandbox on this machine.

    Scans $SANDY_HOME/sandboxes/ (default ~/.sandy) directly: every sandbox
    lives there, and the scan also finds dot-named sandboxes, which sandy's own
    enumeration (and so --print-state) skips today. `sandy --print-state` can
    take many seconds, so it's asked only when that directory doesn't exist."""
    base = sandy_home() / "sandboxes"
    entries = sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    if base.is_dir():
        candidates = list(entries)
    else:
        paths = _sandy_print_state()
        candidates = [Path(p) for p in paths] if paths is not None else []
    roots = {p for p in candidates
             if _is_sandbox_name(p.name, (p / "WORKSPACE.json").is_file()
                                 if p.name.startswith(".") else None)}
    return sorted(roots)


def default_dirs() -> list[str]:
    """Claude Code transcript dirs: the host's ~/.claude/projects plus each
    sandbox's claude/projects. Only projects/ holds transcripts — claude/ also
    has history.jsonl, job timelines and tool-audit logs, which aren't."""
    dirs = [str(Path.home() / ".claude" / "projects")]
    for root in sandbox_roots():
        proj = root / "claude" / "projects"
        if proj.is_dir():
            dirs.append(str(proj))
    return dirs


def discover_files(dirs: list[str]) -> list[str]:
    """Walk directories, return all .jsonl file paths."""
    files = []
    progress = Progress("Finding Claude transcripts")
    for d in dirs:
        p = Path(d)
        if not p.exists():
            continue
        for f in p.rglob("*.jsonl"):
            files.append(str(f))
            progress.update(f"{len(files):,} found")
        progress.update(f"{len(files):,} found")
    progress.done()
    return files


# ── Remote host support ────────────────────────────────────────────────

# -T: never allocate a remote pty. Together with stdin=/dev/null (see _run_ssh)
# this stops ssh from putting the local terminal into raw mode — an ssh killed
# by our timeout can't restore it, which left the report "staircased".
_SSH = ("ssh", "-T", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes")


class Progress:
    """One live status line on stderr: a bouncing (cylon) bar plus detail
    text, e.g. bytes received. Silent unless stderr is a terminal, so piped
    output, --json and tests are unaffected; CCTRACK_NO_PROGRESS=1 disables."""
    WIDTH = 16

    def __init__(self, label: str, enabled: bool | None = None):
        import time
        self.label = label
        self.start = time.monotonic()
        self._last = 0.0
        self.enabled = progress_enabled() if enabled is None else enabled

    def update(self, detail: str = "", force: bool = False) -> None:
        import time
        if _remote_progress_lines:
            # Running on a --remote host: report to the local cctrack, which
            # shows it in its own status line.
            now = time.monotonic()
            if force or now - self._last >= 0.5:
                self._last = now
                sys.stderr.write(f"{REMOTE_PROGRESS}{self.label}: {detail}\n")
                sys.stderr.flush()
            return
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self._last < 0.1:
            return
        self._last = now
        elapsed = now - self.start
        # Bounce a 3-cell block across the bar, about one sweep per second.
        span = self.WIDTH - 3
        step = int(elapsed * 2 * span) % (2 * span)
        pos = step if step <= span else 2 * span - step
        bar = " " * pos + "\u2588\u2588\u2588" + " " * (span - pos)
        sys.stderr.write(f"\r\033[K  {self.label} [{bar}] {detail}  {elapsed:.0f}s")
        sys.stderr.flush()

    def done(self) -> None:
        if self.enabled:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


def progress_enabled() -> bool:
    if os.environ.get("CCTRACK_NO_PROGRESS"):
        return False
    try:
        return sys.stderr.isatty() is True
    except (AttributeError, ValueError):
        return False


def timing_enabled() -> bool:
    """CCTRACK_TIMING=1 prints a per-phase timing block to stderr at exit."""
    return os.environ.get("CCTRACK_TIMING", "").strip().lower() not in ("", "0", "no", "false", "off")


def _file_census(paths) -> list[int]:
    """[files, bytes, files_changed_24h, bytes_changed_24h]; unreadable files skipped."""
    n = b = rn = rb = 0
    cutoff = time.time() - 86400
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        n += 1
        b += st.st_size
        if st.st_mtime >= cutoff:
            rn += 1
            rb += st.st_size
    return [n, b, rn, rb]


def _census_text(c) -> str:
    n, b, rn, rb = c
    return f"{n:,} files, {format_bytes(b)}; {rn:,} changed in last 24 h ({format_bytes(rb)})"


class _Timing:
    """Wall time per phase, printed to stderr (never stdout) when the run ends,
    including when it is interrupted. Does nothing unless enabled."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        if not enabled:
            return
        self.t0 = time.perf_counter()
        self._cur = "startup"
        self._cur_start = self.t0
        self.totals: dict[str, float] = {"startup": 0.0}
        self.details: dict[str, str] = {}
        self.subs: dict[str, list[tuple[str, float, str]]] = {}
        self.others: dict[str, tuple[str, str]] = {}
        self.notes: dict[str, str] = {}

    def phase(self, name: str) -> None:
        if not self.enabled:
            return
        now = time.perf_counter()
        self.totals[self._cur] += now - self._cur_start
        self._cur, self._cur_start = name, now
        self.totals.setdefault(name, 0.0)

    def detail(self, name: str, text: str) -> None:
        if self.enabled:
            self.details[name] = text

    def note(self, name: str, text: str) -> None:
        """Text appended to a phase's detail at finish (after the census text)."""
        if self.enabled:
            self.notes[name] = text

    def sub(self, parent: str, name: str, secs: float, text: str = "") -> None:
        if self.enabled:
            self.subs.setdefault(parent, []).append((name, secs, text))

    def other(self, parent: str, name: str, text: str = "") -> None:
        """A sub-line whose time is the parent's total minus its other sub-lines."""
        if self.enabled:
            self.others[parent] = (name, text)

    def finish(self, interrupted: bool = False, census: dict | None = None, file=None) -> None:
        if not self.enabled:
            return
        now = time.perf_counter()
        self.totals[self._cur] += now - self._cur_start
        total = now - self.t0
        # The census (stat calls) happens after the clock stops.
        for phase, paths in (census or {}).items():
            if phase in self.totals:
                try:
                    self.details[phase] = _census_text(_file_census(paths))
                except Exception:
                    pass
        for name, text in self.notes.items():
            d = self.details.get(name, "")
            self.details[name] = f"{d}; {text}" if d else text
        if interrupted:
            d = self.details.get(self._cur, "")
            self.details[self._cur] = (d + " " if d else "") + "(interrupted)"

        def line(indent, name, secs, detail):
            return (f"[timing] {indent}{name:<{30 - len(indent)}} {secs:9.3f} s"
                    + (f"  {detail}" if detail else ""))

        out = [f"[timing] # cctrack {cctrack_version()}, Python {sys.version.split()[0]}, {sys.platform}"]
        for name, secs in self.totals.items():
            out.append(line("", name, secs, self.details.get(name, "")))
            subs = list(self.subs.get(name, []))
            if name in self.others:
                oname, otext = self.others[name]
                subs.append((oname, max(0.0, secs - sum(x[1] for x in subs)), otext))
            for sname, ssecs, stext in subs:
                out.append(line("  ", sname, ssecs, stext))
        out.append(line("", "total", total, ""))
        f = file if file is not None else sys.stderr
        try:
            f.write("\n".join(out) + "\n")
            f.flush()
        except (OSError, ValueError):
            pass


def format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _run_streaming(cmd, progress: Progress, timeout: float | None, input: bytes | str | None = None,
                   text: bool = True) -> subprocess.CompletedProcess:
    """Like subprocess.run(capture_output=True, timeout=…) but reads output as
    it arrives, so the progress line can show bytes received and any
    `CCTRACK_PROGRESS` lines a remote collector writes to stderr."""
    import threading
    import time
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_chunks: list[bytes] = []
    err_lines: list[bytes] = []
    received = [0]
    remote_status = [""]
    marker = REMOTE_PROGRESS.encode()

    def pump_out():
        for chunk in iter(lambda: p.stdout.read1(65536), b""):
            out_chunks.append(chunk)
            received[0] += len(chunk)

    def pump_err():
        for line in iter(p.stderr.readline, b""):
            if line.startswith(marker):
                remote_status[0] = line[len(marker):].decode(errors="replace").strip()
            else:
                err_lines.append(line)

    def feed():
        try:
            data = input.encode() if isinstance(input, str) else input
            p.stdin.write(data)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=pump_out, daemon=True),
               threading.Thread(target=pump_err, daemon=True)]
    if input is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while p.poll() is None:
            if deadline is not None and time.monotonic() > deadline:
                p.kill()
                p.wait()
                raise subprocess.TimeoutExpired(cmd, timeout)
            got = f"{format_bytes(received[0])} received"
            progress.update(f"{remote_status[0]} · {got}" if remote_status[0] else got)
            time.sleep(0.05)
        for t in threads:
            t.join()
    finally:
        progress.done()
    out = b"".join(out_chunks)
    err = b"".join(err_lines)
    if text:
        return subprocess.CompletedProcess(cmd, p.returncode,
                                           stdout=out.decode("utf-8", errors="replace"),
                                           stderr=err.decode("utf-8", errors="replace"))
    return subprocess.CompletedProcess(cmd, p.returncode, stdout=out, stderr=err)


def _run_ssh(cmd, progress: str | None = None, **kw):
    """subprocess.run for ssh: stdin from /dev/null unless input is given, and
    the local terminal's settings restored afterwards whatever ssh did. With
    `progress` (a label) and a terminal on stderr, shows a live status line."""
    if "input" not in kw:
        kw.setdefault("stdin", subprocess.DEVNULL)
    saved = []
    try:
        import termios
        for fd in (0, 1, 2):
            try:
                if os.isatty(fd):
                    saved.append((fd, termios.tcgetattr(fd)))
            except (OSError, termios.error):
                pass
    except ImportError:
        termios = None
    try:
        if progress and progress_enabled():
            return _run_streaming(cmd, Progress(progress), kw.get("timeout"),
                                  input=kw.get("input"), text=bool(kw.get("text")))
        return subprocess.run(cmd, **kw)
    finally:
        for fd, attrs in saved:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
            except (OSError, termios.error):
                pass


def _gzip_wrapped(remote_cmd: str) -> str:
    """Compress a raw remote stream when the host has gzip (log JSON shrinks
    several-fold); _decode_stream undoes it, and accepts uncompressed output."""
    return (f"if command -v gzip >/dev/null 2>&1; then {{ {remote_cmd} ; }} | gzip -c; "
            f"else {remote_cmd} ; fi")


def _decode_stream(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if data[:2] == b"\x1f\x8b":
        import gzip
        import zlib
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError, zlib.error):
            # A stream cut short (e.g. ssh killed): keep what decompressed.
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                data = d.decompress(data)
            except zlib.error:
                data = b""
    return data.decode("utf-8", errors="replace")


def _ssh_fetch(host: str, remote_cmd: str, label: str) -> subprocess.CompletedProcess:
    """Run a raw-stream fetch (gzip-compressed in transit); text out."""
    r = _run_ssh([*_SSH, host, _gzip_wrapped(remote_cmd)], capture_output=True,
                 timeout=remote_fetch_timeout(), progress=f"{host}: {label}")
    return subprocess.CompletedProcess(r.args, r.returncode, stdout=_decode_stream(r.stdout),
                                       stderr=_decode_stream(r.stderr))


def remote_fetch_timeout() -> float:
    """Seconds allowed for one remote log fetch (CCTRACK_SSH_TIMEOUT, default
    600). A host with months of transcripts streams a lot of data."""
    try:
        return max(1.0, float(os.environ.get("CCTRACK_SSH_TIMEOUT", "600")))
    except ValueError:
        return 600.0


def _warn_timeout(host: str, what: str) -> None:
    print(f"  Warning: SSH to {host} timed out after {remote_fetch_timeout():.0f}s fetching "
          f"{what}; that data is MISSING from this report. Raise CCTRACK_SSH_TIMEOUT "
          f"(seconds) to allow longer.", file=sys.stderr)


_STATE_END = "___CCTRACK_SANDY_STATE_END___"
_remote_roots_cache: dict[str, list[str]] = {}


def remote_sandbox_roots(host: str) -> list[str]:
    """Sandy sandbox roots on a remote host, found the same way as locally:
    `sandy --print-state` if the host has sandy, else the shell glob of
    $SANDY_HOME/sandboxes/*/, plus dot-named sandboxes (dot-dirs holding
    WORKSPACE.json), filtered by _is_sandbox_name. One SSH call, cached."""
    if host in _remote_roots_cache:
        return _remote_roots_cache[host]
    # As locally: scan sandboxes/ and only ask the (slow) sandy when it's missing.
    remote_cmd = (
        'B="${SANDY_HOME:-$HOME/.sandy}/sandboxes"; '
        '[ -d "$B" ] || { command -v sandy >/dev/null 2>&1 && '
        'sandy --print-state </dev/null 2>/dev/null; }; '
        f"echo '{_STATE_END}'; "
        'ls -1d "$B"/*/ 2>/dev/null; '
        f"echo '{_STATE_END}'; "
        'for d in "$B"/.*/; do [ -f "$d/WORKSPACE.json" ] && echo "$d"; done 2>/dev/null; true'
    )
    cmd = [*_SSH, host, remote_cmd]
    try:
        out = _run_ssh(cmd, capture_output=True, text=True, timeout=60,
                       progress=f"{host}: finding sandboxes").stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        out = ""
    if not isinstance(out, str):
        out = ""
    state_part, _, rest = out.partition(_STATE_END)
    glob_part, _, dot_part = rest.partition(_STATE_END)
    paths = None
    try:
        state = json.loads(state_part) if state_part.strip() else None
        boxes = state.get("sandboxes") if isinstance(state, dict) else None
        if isinstance(boxes, list):
            paths = [b["path"] for b in boxes if isinstance(b, dict) and b.get("path")]
    except (json.JSONDecodeError, ValueError):
        paths = None
    if paths is None:
        paths = [l.strip() for l in glob_part.splitlines() if l.strip()]
    # Dot-dirs listed here were confirmed to hold WORKSPACE.json remotely.
    dot_paths = [l.strip() for l in dot_part.splitlines() if l.strip()]
    roots = set()
    for p in paths:
        p = p.rstrip("/")
        name = p.rsplit("/", 1)[-1]
        if _is_sandbox_name(name, None if not name.startswith(".") else
                            any(d.rstrip("/") == p for d in dot_paths)):
            roots.add(p)
    for d in dot_paths:
        d = d.rstrip("/")
        if _is_sandbox_name(d.rsplit("/", 1)[-1], True):
            roots.add(d)
    roots = sorted(roots)
    _remote_roots_cache[host] = roots
    return roots


def _q(path: str) -> str:
    import shlex
    return shlex.quote(path)


def fetch_remote_jsonl(host: str) -> str:
    """SSH to host and cat Claude transcripts: ~/.claude/projects plus each
    sandbox's claude/projects. Returns raw output."""
    dirs = " ".join(['"$HOME/.claude/projects"']
                    + [_q(f"{r}/claude/projects") for r in remote_sandbox_roots(host)])
    remote_cmd = (f"find {dirs} -name '*.jsonl' -exec echo '___CCTRACK_FILE_SEP___' {{}} "
                  f"\\; -exec cat {{}} \\; 2>/dev/null")
    try:
        result = _ssh_fetch(host, remote_cmd, "Claude transcripts")
        if result.returncode != 0 and not result.stdout:
            print(f"  Warning: SSH to {host} failed: {result.stderr.strip()}", file=sys.stderr)
            return ""
        return result.stdout
    except subprocess.TimeoutExpired:
        _warn_timeout(host, "Claude Code transcripts")
        return ""
    except FileNotFoundError:
        print(f"  Warning: ssh command not found", file=sys.stderr)
        return ""


def _remote_chunks(raw: str, sep: str):
    """Split a remote cat stream into (path, lines) per file. A file starts at
    a line BEGINNING with the separator (log lines start with "{", and the
    separator text can legitimately appear inside a transcript's content),
    followed by the file's path; older streams carry no path (None)."""
    path, lines, started = None, [], False
    for line in raw.splitlines():
        if line.startswith(sep):
            if started or any(l.strip() for l in lines):
                yield path, lines
            path = line[len(sep):].strip() or None
            lines, started = [], True
        else:
            lines.append(line)
    if started or any(l.strip() for l in lines):
        yield path, lines


def _remote_parse(host: str, raw: str, sep: str, parser, file_stats: dict | None) -> list[dict]:
    """Parse a remote stream file by file, dedup across the host's files, and
    tag each event with `host:path` as its source file."""
    return _parse_remote_files(host, _remote_chunks(raw, sep), parser, file_stats)


def _parse_remote_files(host: str, items, parser, file_stats: dict | None) -> list[dict]:
    """(path, lines) per file → events, exactly as for a streamed remote host.
    Shared by the raw stream and by _remote_collect running on the host, so
    both paths produce the same events."""
    kind = _log_kind(parser)
    lists = []
    for path, lines in items:
        label = f"{host}:{path}" if path else f"(remote {host})"
        r = _parse_log_lines(kind, lines, label, source=host, want_stats=file_stats is not None)
        if file_stats is not None and path:
            file_stats[label] = r["stats"]
        lists.append(r["events"])
    return dedup_events(lists)


def parse_remote_events(host: str, file_stats: dict | None = None) -> list[dict]:
    """Fetch and parse JSONL from a remote host via SSH.
    Dedup by API message identity across the host's files (resumed sessions).
    Events are tagged with `host` as their source."""
    bundle = _remote_bundle_cache.get(host)
    if bundle is not None and bundle.get("ok"):
        if file_stats is not None:
            file_stats.update(bundle.get("claude_stats", {}))
        return bundle.get("claude", [])
    if bundle is not None and bundle.get("failed"):
        return []
    print(f"  Fetching from {host}...", file=sys.stderr)
    raw = fetch_remote_jsonl(host)
    if not raw:
        return []
    return _remote_parse(host, raw, "___CCTRACK_FILE_SEP___", parse_lines, file_stats)


HOOK_FILE_SEP = "___CCTRACK_HOOK_FILE___"
MONTHLY_FILE_SEP = "___CCTRACK_MONTHLY_FILE___"
BASELINE_FILE_SEP = "___CCTRACK_BASELINE_FILE___"

# Hook data locations on remote hosts: the host's own cctrack dir plus any
# Sandy sandbox equivalents (mirrors discover_hook_data_dirs).


def fetch_remote_hook_data(host: str) -> str:
    """SSH to host and cat all statusline JSONL + monthly summary files from
    ~/.claude/cctrack and each sandbox's claude/cctrack.
    Returns raw output with file-type separators."""
    hook_dirs = " ".join(['"$HOME/.claude/cctrack"']
                         + [_q(f"{r}/claude/cctrack") for r in remote_sandbox_roots(host)])
    remote_cmd = (
        f"find {hook_dirs} -name 'statusline*.jsonl' "
        f"-exec echo '{HOOK_FILE_SEP}' \\; -exec cat {{}} \\; 2>/dev/null; "
        f"find {hook_dirs} -name 'monthly-*.json' "
        f"-exec echo '{MONTHLY_FILE_SEP}' \\; -exec cat {{}} \\; 2>/dev/null; "
        f"find {hook_dirs} -name 'session-baselines.json' "
        f"-exec echo '{BASELINE_FILE_SEP}' \\; -exec cat {{}} \\; 2>/dev/null; "
        f"true"
    )
    try:
        result = _ssh_fetch(host, remote_cmd, "statusline hook data")
        if result.returncode != 0 and not result.stdout:
            print(f"  Warning: SSH to {host} failed: {result.stderr.strip()}", file=sys.stderr)
            return ""
        return result.stdout
    except subprocess.TimeoutExpired:
        _warn_timeout(host, "statusline hook data")
        return ""
    except FileNotFoundError:
        print(f"  Warning: ssh command not found", file=sys.stderr)
        return ""


def parse_remote_hook_data(host: str) -> tuple[list[dict], dict, dict]:
    """Fetch and parse hook data from a remote host.
    Returns (hook_rows, monthly_summaries, session_baselines) in the same
    shapes as parse_hook_rows, load_monthly_hook_summaries, and
    load_session_baselines."""
    bundle = _remote_bundle_cache.get(host)
    if bundle is not None and bundle.get("ok"):
        return bundle.get("hook", ([], {}, {}))
    if bundle is not None and bundle.get("failed"):
        return [], {}, {}
    raw = fetch_remote_hook_data(host)
    if not raw:
        return [], {}, {}
    return parse_hook_blob(raw)


def parse_hook_blob(raw: str) -> tuple[list[dict], dict, dict]:
    """Parse the hook-data stream (statusline lines, then monthly summaries,
    then baselines, with file separators) — fetched raw, or built on the
    remote host by _remote_collect."""
    # Chunk order matches the find order above: statusline, monthly, baselines.
    main_part, *baseline_chunks = raw.split(BASELINE_FILE_SEP)
    head, _, monthly_raw = main_part.partition(MONTHLY_FILE_SEP)

    lines: list[str] = []
    for chunk in head.split(HOOK_FILE_SEP):
        lines.extend(chunk.splitlines())
    events = _hook_rows_from_lines(lines)

    monthly: dict[str, dict] = {}
    for chunk in monthly_raw.split(MONTHLY_FILE_SEP):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        for date_str, day_data in data.get("days", {}).items():
            entry = {
                "cost": float(day_data.get("cost", 0.0)),
                "sessions": int(day_data.get("sessions", 0)),
            }
            merge_monthly_summaries(monthly, {date_str: entry})

    baselines: dict[str, dict] = {}
    for chunk in baseline_chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            for sid, b in data.items():
                # Session ids are unique per host; keep the newer date on clash.
                if sid not in baselines or b.get("date", "") > baselines[sid].get("date", ""):
                    baselines[sid] = b
    return events, monthly, baselines


def merge_monthly_summaries(target: dict, source: dict) -> None:
    """Merge per-date {cost, sessions} summaries from source into target,
    summing where dates overlap (e.g. same day on multiple hosts)."""
    for date_str, data in source.items():
        if date_str in target:
            target[date_str]["cost"] += data["cost"]
            target[date_str]["sessions"] += data["sessions"]
        else:
            target[date_str] = dict(data)


# ── Hook infrastructure ───────────────────────────────────────────────

HOOK_SCRIPT = """\
#!/usr/bin/env python3
\"\"\"cctrack statusline hook — appends Claude Code session data to JSONL, then
runs the status line command it replaced at install time (if any), so that
status line keeps showing.\"\"\"
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CCTRACK_DIR = Path.home() / ".claude" / "cctrack"


def record(raw):
    try:
        if not raw.strip():
            return
        data = json.loads(raw)

        cost_usd = data.get("cost", {}).get("total_cost_usd")
        if cost_usd is None:
            cost_usd = 0.0

        session_id = data.get("session_id", "")
        model = data.get("model", {}).get("id", "")

        ctx = data.get("context_window", {})
        input_tokens = ctx.get("total_input_tokens", 0)
        output_tokens = ctx.get("total_output_tokens", 0)
        usage = ctx.get("current_usage", {})
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)

        rec = {
            "_ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "cost_usd": cost_usd,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read": cache_read,
            "cache_write": cache_write,
        }

        CCTRACK_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_path = CCTRACK_DIR / f"statusline-{today}.jsonl"
        with open(out_path, "a") as f:
            f.write(json.dumps(rec) + "\\n")
    except Exception:
        pass


def wrapped_command():
    try:
        cfg = json.loads((CCTRACK_DIR / "config.json").read_text())
        cmd = (cfg.get("wrapped_status_line") or {}).get("command")
    except Exception:
        return None
    if not isinstance(cmd, str) or not cmd.strip() or "cctrack-hook.py" in cmd:
        return None
    return cmd


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    record(raw)
    cmd = wrapped_command()
    if cmd is None:
        return 0
    # Claude Code runs status line commands through the shell with the session
    # JSON on stdin; do the same and pass the output through untouched.
    try:
        r = subprocess.run(cmd, shell=True, input=raw.encode("utf-8"),
                           stdout=subprocess.PIPE, timeout=30)
    except Exception:
        return 0
    sys.stdout.buffer.write(r.stdout)
    sys.stdout.flush()
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
"""

CCTRACK_DIR = Path.home() / ".claude" / "cctrack"
HOOK_SCRIPT_PATH = Path.home() / ".claude" / "hooks" / "cctrack-hook.py"
HOOK_CONFIG_PATH = CCTRACK_DIR / "config.json"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
HOOK_RETENTION_DAYS = 90


def cctrack_dir() -> Path:
    """Return the cctrack data directory path (~/.claude/cctrack)."""
    return CCTRACK_DIR


def discover_hook_data_dirs() -> list[Path]:
    """Return all directories that may contain cctrack hook data: the host's
    ~/.claude/cctrack and each Sandy sandbox's claude/cctrack."""
    dirs = []
    if CCTRACK_DIR.exists():
        dirs.append(CCTRACK_DIR)
    for root in sandbox_roots():
        candidate = root / "claude" / "cctrack"
        if candidate.is_dir() and candidate not in dirs:
            dirs.append(candidate)
    return dirs


def _hook_command() -> str:
    """Return the statusLine command string for our hook.
    Uses ~ so the path resolves correctly in both host and sandbox environments."""
    return "python3 ~/.claude/hooks/cctrack-hook.py"


def is_hook_installed() -> bool:
    """Check if the hook is fully installed (config exists + settings.json has our statusLine)."""
    if not HOOK_CONFIG_PATH.exists():
        return False
    if not CLAUDE_SETTINGS_PATH.exists():
        return False
    try:
        settings = json.loads(CLAUDE_SETTINGS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    status_line = settings.get("statusLine")
    if not isinstance(status_line, dict):
        return False
    return status_line.get("command") == _hook_command()


def read_hook_config() -> dict | None:
    """Read and return the hook config, or None if not installed."""
    if not HOOK_CONFIG_PATH.exists():
        return None
    try:
        return json.loads(HOOK_CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _apply_hook_install(settings_path, hook_path, config_path, hook_script, hook_cmd):
    """Install the hook, wrapping any existing status line. Self-contained (it
    is also shipped to remote hosts as source), so imports stay inside.

    A status line that isn't ours is saved in config.json as
    `wrapped_status_line`; the hook runs it and passes its output through, and
    uninstall puts it back. Re-installing keeps the saved one and the original
    install date (which marks where hook coverage starts). Returns a dict
    describing what happened."""
    import json
    import stat
    from datetime import datetime, timezone
    from pathlib import Path

    settings_path, hook_path, config_path = Path(settings_path), Path(hook_path), Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(hook_script)
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except (ValueError, OSError):
            settings = {}
    if not isinstance(settings, dict):
        settings = {}
    old_config = {}
    if config_path.exists():
        try:
            old_config = json.loads(config_path.read_text())
        except (ValueError, OSError):
            old_config = {}
    if not isinstance(old_config, dict):
        old_config = {}

    current = settings.get("statusLine")
    ours = isinstance(current, dict) and "cctrack-hook.py" in str(current.get("command", ""))
    if ours:
        wrapped = old_config.get("wrapped_status_line")
    elif isinstance(current, dict) and current.get("command"):
        wrapped = current
    else:
        wrapped = None

    # Keep the status line's other settings (e.g. padding): from our own
    # entry on a re-install, else from the line being wrapped.
    base = current if ours else wrapped
    new_line = dict(base) if isinstance(base, dict) else {}
    new_line.update({"type": "command", "command": hook_cmd})
    settings["statusLine"] = new_line
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    config = {"installed_at": old_config.get("installed_at")
              or datetime.now(timezone.utc).isoformat(),
              "version": "0.3.0"}
    if wrapped:
        config["wrapped_status_line"] = wrapped
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return {"wrapped": (wrapped or {}).get("command")}


def _apply_hook_uninstall(settings_path, hook_path, config_path):
    """Remove the hook. If it wrapped a status line, put that back; if the
    status line was changed to something else since, leave it alone.
    Self-contained (shipped to remote hosts). Returns what happened."""
    import json
    from pathlib import Path

    settings_path, hook_path, config_path = Path(settings_path), Path(hook_path), Path(config_path)
    wrapped = None
    if config_path.exists():
        try:
            wrapped = json.loads(config_path.read_text()).get("wrapped_status_line")
        except (ValueError, OSError, AttributeError):
            wrapped = None
    result = "none"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
            current = settings.get("statusLine")
            if isinstance(current, dict) and "cctrack-hook.py" in str(current.get("command", "")):
                if isinstance(wrapped, dict) and wrapped.get("command"):
                    settings["statusLine"] = wrapped
                    result = "restored"
                else:
                    del settings["statusLine"]
                    result = "removed"
                settings_path.write_text(json.dumps(settings, indent=2) + "\n")
            elif current is not None:
                result = "left"
        except (ValueError, OSError, AttributeError):
            pass
    if hook_path.exists():
        hook_path.unlink()
    if config_path.exists():
        config_path.unlink()
    return {"status_line": result,
            "restored": (wrapped or {}).get("command") if result == "restored" else None}


def install_hook() -> None:
    """Install the cctrack statusline hook (wrapping any existing status line)."""
    info = _apply_hook_install(CLAUDE_SETTINGS_PATH, HOOK_SCRIPT_PATH, HOOK_CONFIG_PATH,
                               HOOK_SCRIPT, _hook_command())
    print(f"cctrack hook installed successfully.")
    print(f"  Hook script: {HOOK_SCRIPT_PATH}")
    print(f"  Data dir:    {CCTRACK_DIR}")
    print(f"  Settings:    {CLAUDE_SETTINGS_PATH}")
    if info["wrapped"]:
        print(f"  Wrapping your status line: {info['wrapped']}")


def uninstall_hook() -> None:
    """Uninstall the cctrack statusline hook, keeping data."""
    info = _apply_hook_uninstall(CLAUDE_SETTINGS_PATH, HOOK_SCRIPT_PATH, HOOK_CONFIG_PATH)
    print(f"cctrack hook uninstalled.")
    _print_uninstall_note(info)
    if CCTRACK_DIR.exists():
        print(f"  Data preserved: {CCTRACK_DIR}")


def _print_uninstall_note(info: dict) -> None:
    if info["status_line"] == "restored":
        print(f"  Status line restored: {info['restored']}")
    elif info["status_line"] == "left":
        print("  Status line left as is (it no longer pointed at cctrack).")


def _remote_hook_program(func, *call_args: str) -> str:
    """Python source that defines `func` and prints its result as JSON, for
    `ssh host python3 -`. Paths are expressions evaluated on the remote."""
    import inspect
    return ("import json\nfrom pathlib import Path\nhome = Path.home()\n"
            + inspect.getsource(func)
            + f"\nprint('CCTRACK_RESULT ' + json.dumps({func.__name__}({', '.join(call_args)})))\n")


def _remote_result(stdout: str) -> dict | None:
    for line in (stdout or "").splitlines():
        if line.startswith("CCTRACK_RESULT "):
            try:
                return json.loads(line[len("CCTRACK_RESULT "):])
            except ValueError:
                return None
    return None


_REMOTE_PATHS = ('home / ".claude" / "settings.json"',
                 'home / ".claude" / "hooks" / "cctrack-hook.py"',
                 'home / ".claude" / "cctrack" / "config.json"')


def _run_remote_python(host: str, program: str) -> subprocess.CompletedProcess | None:
    cmd = [*_SSH, host, "python3", "-"]
    try:
        return _run_ssh(cmd, input=program, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print(f"SSH to {host} timed out", file=sys.stderr)
    except FileNotFoundError:
        print("ssh command not found", file=sys.stderr)
    return None


def remote_install_hook(host: str) -> None:
    """Install the cctrack statusline hook on a remote host via SSH, wrapping
    any status line already configured there."""
    encoded = base64.b64encode(HOOK_SCRIPT.encode()).decode()
    program = _remote_hook_program(
        _apply_hook_install, *_REMOTE_PATHS,
        f'__import__("base64").b64decode("{encoded}").decode()', repr(_hook_command()))
    result = _run_remote_python(host, program)
    if result is None:
        return
    info = _remote_result(result.stdout)
    if result.returncode == 0 and info is not None:
        print(f"cctrack hook installed on {host}.")
        print("  Hook script: ~/.claude/hooks/cctrack-hook.py")
        print("  Data dir:    ~/.claude/cctrack")
        print("  Settings:    ~/.claude/settings.json")
        if info.get("wrapped"):
            print(f"  Wrapping your status line: {info['wrapped']}")
    else:
        print(f"Hook install on {host} failed: {result.stderr.strip()}", file=sys.stderr)


def remote_uninstall_hook(host: str) -> None:
    """Uninstall the cctrack statusline hook on a remote host via SSH,
    restoring the status line it wrapped."""
    program = _remote_hook_program(_apply_hook_uninstall, *_REMOTE_PATHS)
    result = _run_remote_python(host, program)
    if result is None:
        return
    info = _remote_result(result.stdout)
    if result.returncode == 0 and info is not None:
        print(f"cctrack hook uninstalled on {host}.")
        _print_uninstall_note(info)
        print("  Data preserved: ~/.claude/cctrack")
    else:
        print(f"Hook uninstall on {host} failed: {result.stderr.strip()}", file=sys.stderr)


# Ratio of hook (Claude Code's own) cost to JSONL-priced cost, used only when
# it can't be measured from overlapping days. It was once 1.25 on the belief
# that logs undercount ~20-25%; that gap was entirely rate-card error, and with
# correct prices measured ratios sit at 0.99-1.02x. A stale 1.25 inflated every
# unmeasured day by a quarter.
DEFAULT_ACCURACY_FACTOR = 1.0


def prompt_hook_install() -> None:
    """Interactively offer to install the statusline hook."""
    try:
        if not sys.stdin.isatty():
            return
        print(
            "\nThe statusline hook provides more accurate cost tracking.",
            file=sys.stderr,
        )
        answer = input("Install it now? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            install_hook()
    except (EOFError, KeyboardInterrupt):
        pass


# ── JSONL parsing + dedup ───────────────────────────────────────────────

def _project_name(cwd: str) -> str:
    """Human-friendly project label from an event's working directory."""
    if not cwd:
        return "(unknown)"
    return Path(cwd).name or cwd


def new_parse_stats() -> dict:
    """Per-file parse statistics for the coverage report: unparseable lines,
    usage records, the timestamp range of every timestamped line, and
    compactions keyed by (session id, subagent id)."""
    return {"lines": 0, "bad_lines": 0, "records": 0,
            "first_ts": None, "last_ts": None, "compactions": {}}


def _note_ts(stats: dict, ts) -> None:
    if not isinstance(ts, str) or not ts:
        return
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if stats["first_ts"] is None or dt < stats["first_ts"]:
        stats["first_ts"] = dt
    if stats["last_ts"] is None or dt > stats["last_ts"]:
        stats["last_ts"] = dt


def _note_compaction(stats: dict, key: tuple, marker: str, ts: str) -> None:
    # Keyed by marker, so a resumed session that replays its compaction line
    # isn't counted twice; the timestamp lets reports window them.
    stats["compactions"].setdefault(key, {})[marker] = ts


def parse_lines(lines: list[str], source: str = "local",
                stats: dict | None = None) -> list[dict]:
    """Parse JSONL lines from a single file, retaining the fullest message usage.
    `source` labels which machine the events came from (host name, or 'local').
    Each event also carries its project (from `cwd`), session id and git branch
    so the explore view can attribute spend, plus its subagent (sidechain) id.
    When `stats` (see new_parse_stats) is given it's updated in place."""
    entries: list[dict] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if stats is not None:
            stats["lines"] += 1
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            if stats is not None:
                stats["bad_lines"] += 1
            continue
        if not isinstance(event, dict):
            continue

        if stats is not None:
            _note_ts(stats, event.get("timestamp"))
            if event.get("type") == "system" and event.get("subtype") == "compact_boundary":
                _note_compaction(stats, (event.get("sessionId", ""),
                                         event.get("agentId", "") if event.get("isSidechain") else ""),
                                 event.get("uuid") or event.get("timestamp", ""),
                                 event.get("timestamp", ""))

        if event.get("type") == "progress":
            nested = (event.get("data") or {}).get("message") or {}
            if isinstance(nested, dict) and isinstance(nested.get("message"), dict):
                event = {"sessionId": event.get("sessionId", ""), **nested}
        if event.get("type") not in (None, "assistant"):
            continue
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            continue
        usage = message["usage"]
        input_t = usage.get("input_tokens", 0)
        output_t = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)

        # Prefer nested cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens
        # — 1h cache writes cost 2× input vs 1.25× for 5m, and Claude Code
        # commonly uses the 1h tier. Fall back to flat cache_creation_input_tokens
        # as all-5m for older logs that didn't emit the split.
        cc = usage.get("cache_creation") or {}
        cache_write = cc.get("ephemeral_5m_input_tokens", 0)
        cache_write_1h = cc.get("ephemeral_1h_input_tokens", 0)
        if not cc:
            cache_write = usage.get("cache_creation_input_tokens", 0)

        req_id = event.get("requestId", "")
        # Advisor calls are separately billable; ordinary message iterations
        # are already included in the enclosing usage and must not be added.
        for i, iteration in enumerate(usage.get("iterations") or []):
            if (not isinstance(iteration, dict) or iteration.get("type") != "advisor_message"
                    or not iteration.get("model")):
                continue
            advisor = dict(event)
            advisor["message"] = {"model": iteration["model"], "usage": iteration,
                                  "id": f"{message['id']}:advisor:{i}" if message.get("id") else ""}
            advisor["requestId"] = f"{req_id}:advisor:{i}" if req_id else ""
            entries.extend(parse_lines([json.dumps(advisor)], source=source))

        if (input_t == 0 and output_t == 0 and cache_read == 0
                and cache_write == 0 and cache_write_1h == 0):
            continue

        cwd = event.get("cwd", "")
        entry = {
            "model": event.get("message", {}).get("model", ""),
            "timestamp": event.get("timestamp", ""),
            "input": input_t,
            "output": output_t,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "cache_write_1h": cache_write_1h,
            "_request_id": req_id,
            "_message_id": message.get("id", ""),
            "service_tier": normalize_service_tier(usage.get("speed") or usage.get("service_tier")),
            "source": source,
            "session_id": event.get("sessionId", ""),
            "project": _project_name(cwd),
            "cwd": cwd,
            "git_branch": event.get("gitBranch", ""),
            "agent": "claude",
            # Subagent (Task/Agent tool) turns: same sessionId as the parent,
            # flagged isSidechain and (newer Claude Code) carrying an agentId.
            "is_sidechain": bool(event.get("isSidechain")),
            "agent_id": event.get("agentId", "") or "",
            # Prompt length for per-request long-context tiers: all input,
            # including cache reads and writes (Anthropic's definition).
            "_request_input": input_t + cache_read + cache_write + cache_write_1h,
        }

        entries.append(entry)

    out = dedup_events([entries])
    if stats is not None:
        stats["records"] += len(out)
    return out


def _event_location(e: dict) -> dict:
    return {"host": e.get("source", "local"), "path": e.get("_source_file", ""),
            "session_id": e.get("session_id", ""), "cwd": e.get("cwd", "")}


def _event_hosts(e: dict) -> set[str]:
    return {e.get("source", "local"), *(loc["host"] for loc in e.get("_locations", []))}


def _tag_event_file(e: dict, path: str) -> None:
    e["_source_file"] = path
    if e.get("agent") == "codex" and not e.get("session_id"):
        # Older files can omit session_meta; a rollout's basename remains
        # stable when copied between sandbox homes or machines.
        e["session_id"] = Path(path).name.removesuffix(".zst").removesuffix(".jsonl")
        if e.get("_codex_legacy") and not e.get("_response_id"):
            e["_request_id"] = f"codex:tc:{e['session_id']}:{e['_request_id']}"
        for loc in e.get("_locations", []):
            loc["session_id"] = e["session_id"]
    for loc in e.get("_locations", []):
        if not loc["path"]:
            loc["path"] = path


def _event_identities(e: dict) -> list[tuple]:
    agent = e.get("agent", "claude")
    rid, mid = e.get("_request_id"), e.get("_message_id")
    if agent == "codex":
        keys = [(agent, rid)] if rid else []
        keys.extend((agent, "snapshot", e.get("session_id", ""), tuple(v))
                    for v in e.get("_snapshot_aliases", []))
        return keys
    if mid:
        if rid:
            keys = [(agent, "message", mid, rid)]
        else:
            keys = [(agent, "message", mid, e.get("session_id", ""),
                     "" if mid.startswith("msg_") else e.get("timestamp", ""))]
        return keys
    return [(agent, "request", rid)] if rid else []


def _event_quality(e: dict) -> tuple:
    # Prefer original parent messages over their sidechain replay, then full
    # usage over partial streaming snapshots, then explicit billing metadata.
    return (not (e.get("agent", "claude") == "claude" and e.get("is_sidechain")),
            sum(e.get(k, 0) for k in ("input", "output", "cache_read", "cache_write", "cache_write_1h")),
            bool(e.get("_response_id")), bool(e.get("_model_explicit")),
            e.get("service_tier") is not None)


def _merge_usage_copies(a: dict, b: dict) -> dict:
    def order(e):
        return (iso_z(_event_dt(e)) or "", e.get("source", "local") != "local",
                e.get("source", "local"), e.get("_source_file", ""))
    winner, other = (a, b) if (_event_quality(a) > _event_quality(b) or
                              (_event_quality(a) == _event_quality(b) and order(a) <= order(b))) else (b, a)
    out = dict(winner)
    # Record every copy's location without treating a replay as another call.
    locations = [*a.get("_locations", []), _event_location(a),
                 *b.get("_locations", []), _event_location(b)]
    out["_locations"] = [dict(v) for v in sorted(
        {tuple(sorted(loc.items())) for loc in locations})]
    out["_snapshot_aliases"] = sorted({tuple(v) for e in (a, b)
                                       for v in e.get("_snapshot_aliases", [])})
    if a.get("_fork_history") or b.get("_fork_history"):
        out["_fork_history"] = max((a.get("_fork_history", []), b.get("_fork_history", [])), key=len)
    # A fuller copied transcript may omit settings that another copy retained.
    if out.get("service_tier") is None and other.get("service_tier") is not None:
        out["service_tier"] = other["service_tier"]
    elif out.get("service_tier") != other.get("service_tier") and other.get("service_tier"):
        out["service_tier"] = min((out["service_tier"], other["service_tier"]),
                                  key={"standard": 0, "fast": 1, "flex": 2}.get)
    return out


def _remove_codex_fork_replays(events: list[dict]) -> list[dict]:
    """Compare fork prefixes using usage order, not rewritten timestamps.

    Keep a shared compact history on survivors so another merge remains
    idempotent and a grandchild can still match a parent's inherited prefix.
    """
    histories = defaultdict(list)
    for e in events:
        if e.get("_codex_legacy") and not e.get("_is_compaction"):
            histories[e.get("session_id", "")].append(e)
    full, originals = {}, {}
    for sid, history in histories.items():
        history.sort(key=lambda e: (e.get("_legacy_sequence", 0), e.get("timestamp", "")))
        usage_by_index, seen = {}, set()
        for i, e in enumerate(history):
            saved = e.get("_fork_history", [])
            if id(saved) not in seen:
                usage_by_index.update(saved)
                seen.add(id(saved))
            usage_by_index[e.get("_legacy_sequence", i)] = e["_raw_usage"]
        full[sid] = usage_by_index
        originals[sid] = {e.get("_legacy_sequence", i): e for i, e in enumerate(history)}
    suppressed = set()
    for sid, history in histories.items():
        parent = next((e.get("_fork_parent") for e in history if e.get("_fork_parent")), None)
        if not parent or parent == sid:
            continue
        prefix = full.get(parent, {})
        if not prefix:
            for e in history:
                e["_unresolved_fork"] = parent
            continue
        saved = sorted(full[sid].items())
        for e in history:
            e.pop("_unresolved_fork", None)
            e["_fork_history"] = saved
        for i, child in enumerate(history):
            index = child.get("_legacy_sequence", i)
            if prefix.get(index) != child["_raw_usage"]:
                break
            suppressed.add(id(child))
            original = originals[parent].get(index)
            if original is not None:
                original["_locations"] = _merge_usage_copies(original, child)["_locations"]
    return [e for e in events if id(e) not in suppressed]


def _expand_codex_snapshot_copies(events: list[dict]) -> list[dict]:
    # A response plus a residual can represent one larger cumulative snapshot.
    # Split any plain copied snapshot in exactly the same way BEFORE merging;
    # otherwise its larger counter would replace the response while leaving
    # the residual to be counted again.
    records = {}
    for e in events:
        if e.get("_response_id"):
            for signature in e.get("_snapshot_aliases", []):
                records[(e.get("session_id", ""), tuple(signature))] = e
    expanded = []
    for e in events:
        record = next((records[(e.get("session_id", ""), tuple(v))]
                       for v in e.get("_snapshot_aliases", [])
                       if (e.get("session_id", ""), tuple(v)) in records), None)
        if e.get("_total_only") and not e.get("_response_id") and record:
            copied = dict(record)
            copied.update({k: e[k] for k in ("source", "_source_file", "_locations") if k in e})
            expanded.append(copied)
            remainder = _codex_snapshot_remainder(e, record)
            if remainder:
                expanded.append(remainder)
        else:
            expanded.append(e)
    return expanded


def dedup_events(event_lists) -> list[dict]:
    """Normalize API identities across files, Sandy sandboxes and hosts.

    Retain the fullest snapshot and all copy locations. Codex cumulative
    identities are scoped to their thread; response ids are globally unique.
    Claude request ids alone are insufficient when one request has multiple
    messages, and Anthropic message ids can dedup logs without request ids.
    """
    indexed: dict[tuple, int] = {}
    replay_routes: dict[tuple, list[int]] = defaultdict(list)
    out: list[dict | None] = []
    for e in _expand_codex_snapshot_copies([e for group in event_lists for e in group]):
        keys = _event_identities(e)
        indexes = {indexed[k] for k in keys if k in indexed and out[indexed[k]] is not None}
        route = (e.get("_message_id"), e.get("session_id", ""))
        if e.get("agent", "claude") == "claude" and route[0]:
            for i in replay_routes[route]:
                existing = out[i]
                if existing is not None and (e.get("is_sidechain") or existing.get("is_sidechain")):
                    indexes.add(i)
        if indexes:
            index = min(indexes)
            merged = e
            for i in sorted(indexes):
                merged = _merge_usage_copies(out[i], merged)
                if i != index:
                    out[i] = None
            out[index] = merged
            # Redirect aliases of any groups this event joined.
            if len(indexes) > 1:
                indexed = {k: index if v in indexes else v for k, v in indexed.items()}
        else:
            index = len(out)
            out.append(e)
        for k in keys + _event_identities(out[index]):
            indexed[k] = index
        if route[0]:
            if index not in replay_routes[route]:
                replay_routes[route].append(index)
    return _remove_codex_fork_replays([e for e in out if e is not None])


LOG_KINDS = ("claude", "codex")


def _read_log_lines(kind: str, path: str) -> list[str] | None:
    """One log file's lines exactly as the parsers have always received them, or
    None if unreadable. Codex `.jsonl.zst`: `_read_zstd(path).splitlines()` (None
    if it can't be decoded). Everything else: `_read_text_lines(path)`
    (`readlines()`, errors="replace", None on OSError). Line splitting differs
    between the two on purpose: `splitlines()` also breaks on U+2028."""
    if kind not in LOG_KINDS:
        raise ValueError(f"unknown log kind {kind!r}")
    if kind == "codex" and path.endswith(".zst"):
        text = _read_zstd(path)
        return None if text is None else text.splitlines()
    return _read_text_lines(path)


def _parse_log_lines(kind: str, lines: list[str], label: str, *, source: str = "local",
                     want_stats: bool = False, agent_id_from_name: bool = False) -> dict:
    """Parse one file's lines -> {"events": [...], "stats": dict | None}.
    Events are the parser's in-file-deduped output, each tagged with
    `_tag_event_file(e, label)`; no cross-file step has seen them.
    `agent_id_from_name` (local Claude only) fills a sidechain event's missing
    agent_id from an `agent-<id>.jsonl` file name."""
    parser = {"claude": parse_lines, "codex": parse_codex_lines}.get(kind)
    if parser is None:
        raise ValueError(f"unknown log kind {kind!r}")
    st = new_parse_stats() if want_stats else None
    events = parser(lines, source=source, stats=st)
    for e in events:
        _tag_event_file(e, label)
        if agent_id_from_name and e.get("is_sidechain") and not e.get("agent_id"):
            # Older subagent transcripts lack agentId; the file name has it.
            name = Path(label).name
            if name.startswith("agent-") and name.endswith(".jsonl"):
                e["agent_id"] = name[len("agent-"):-len(".jsonl")]
    return {"events": events, "stats": st}


def _parse_log_file(kind: str, path: str, *, source: str = "local", label: str | None = None,
                    want_stats: bool = False, agent_id_from_name: bool = False) -> dict | None:
    """The per-file seam: read `path` and parse it. None = unreadable (OSError, or
    an undecodable .zst). `label` (default `path`) is what events are tagged with."""
    lines = _read_log_lines(kind, path)
    if lines is None:
        return None
    return _parse_log_lines(kind, lines, path if label is None else label, source=source,
                            want_stats=want_stats, agent_id_from_name=agent_id_from_name)


def _log_kind(parser) -> str:
    """Map the parser function that `_remote_parse`/`_parse_remote_files` take to its kind."""
    if parser is parse_lines:
        return "claude"
    if parser is parse_codex_lines:
        return "codex"
    raise ValueError(f"unknown log parser {parser!r}")


# ── Parse cache ──────────────────────────────────────────────────────────
# One SQLite file holds one entry per log file: the per-file parse result
# ({events, stats}), valid while the file's (size, mtime_ns) and the code key match.
# main() opens it once per run. Local transcripts, rollouts and statusline files go
# through it, and so do --remote hosts' per-file replies (protocol v2); the raw stream
# never does. `sqlite3`, `marshal`, `zlib`, `hashlib`, `io` and `codecs` are imported
# inside the functions that need them (cache-only imports).

CACHE_SCHEMA = 1                     # PRAGMA user_version
CACHE_FILENAME = "cache.sqlite3"
CACHE_BUSY_TIMEOUT = 1.0             # seconds; tests patch it smaller
CACHE_RACY_NS = 2_000_000_000        # 2 s: a file this fresh may still be written to
CACHE_PRUNE_SECONDS = 30 * 86400
CACHE_FLUSH_BYTES = 8 << 20          # flush pending rows at 8 MiB of blobs ...
CACHE_FLUSH_SECONDS = 2.0            # ... or 2 s after the last flush
CACHE_ZLIB_LEVEL = 1


def cache_dir() -> Path:
    """$CCTRACK_CACHE_DIR if set; else $XDG_CACHE_HOME/cctrack when XDG_CACHE_HOME is an
    absolute path (the XDG spec says to ignore relative ones); else ~/.cache/cctrack."""
    env = os.environ.get("CCTRACK_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "cctrack"
    return Path.home() / ".cache" / "cctrack"


def cache_code_key() -> str:
    """Everything besides a file's bytes that decides its parse and its payload format:
    this module's source hash, the interpreter, marshal's format version and the default
    text encoding `open()` uses. Raises OSError if the source can't be read."""
    import codecs
    import hashlib
    import io
    import marshal
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    enc = codecs.lookup(io.TextIOWrapper(io.BytesIO()).encoding).name
    return (f"{digest} {sys.implementation.name}-{'.'.join(map(str, sys.version_info[:3]))} "
            f"marshal{marshal.version} {enc}")


def _cache_params(source: str, label: str, path: str, agent_id_from_name: bool) -> str:
    """The parse parameters that change a file's events, as part of the entry's key."""
    return json.dumps([source, None if label == path else label, bool(agent_id_from_name)])


def _encode_cache_payload(payload: dict) -> bytes | None:
    """{"events", "stats"} -> zlib(marshal(...)); None if marshal rejects an object.
    The stats datetimes become ISO strings (on a shallow copy)."""
    import marshal
    import zlib
    try:
        st = payload["stats"]
        if st is not None:
            st = dict(st)
            for k in ("first_ts", "last_ts"):
                if isinstance(st.get(k), datetime):
                    st[k] = st[k].isoformat()
        return zlib.compress(marshal.dumps({"events": payload["events"], "stats": st}),
                             CACHE_ZLIB_LEVEL)
    except (ValueError, TypeError, KeyError):
        return None


def _decode_cache_payload(blob: bytes) -> dict | None:
    """Inverse of _encode_cache_payload. None on any error or wrong shape: zlib's
    checksum is what turns a corrupt blob into a miss."""
    import marshal
    import zlib
    try:
        p = marshal.loads(zlib.decompress(blob))
        if (not isinstance(p, dict) or set(p) != {"events", "stats"}
                or not isinstance(p["events"], list)
                or not all(isinstance(e, dict) for e in p["events"])
                or not (p["stats"] is None or isinstance(p["stats"], dict))):
            return None
        st = p["stats"]
        if st is not None:
            for k in ("first_ts", "last_ts"):
                if isinstance(st.get(k), str):
                    st[k] = datetime.fromisoformat(st[k])
        return p
    except Exception:
        return None


def _file_sig(path: str) -> tuple[int, int] | None:
    """(size, mtime_ns) of a file, None if it can't be stat'ed."""
    try:
        s = os.stat(path)
    except OSError:
        return None
    return (s.st_size, s.st_mtime_ns)


def _is_racy(sig: tuple[int, int], now_ns: int) -> bool:
    """True if the mtime is within CACHE_RACY_NS of now, or in the future."""
    return sig[1] > now_ns - CACHE_RACY_NS


def _zstd_available() -> bool:
    """Mirror of _read_zstd's decoder choice: stdlib zstd, else the `zstd` CLI."""
    try:
        from compression import zstd  # type: ignore  # noqa: F401
        return True
    except ImportError:
        pass
    import shutil
    return shutil.which("zstd") is not None


def _key_text_ok(*texts: str) -> bool:
    """True if every text encodes as strict UTF-8 (SQLite can't bind lone surrogates)."""
    try:
        for t in texts:
            t.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _cache_note(msg: str) -> None:
    print(f"cctrack: cache {msg}", file=sys.stderr)


class _CacheCorrupt(Exception):
    """The cache file opened but its contents are not a usable store."""


def _cache_unlink_files(db: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)


class CacheStore:
    """Per-file parse cache. Rows are encoded at put() and written in short
    BEGIN IMMEDIATE batches, so no write lock is held while files are parsed. The first
    SQLite error disables the store for the rest of the run, with one stderr note."""

    def __init__(self, con, db: Path, code_key: str, noted: bool = False):
        self.path = db
        self.code_key = code_key
        self.enabled = True
        self.counts = {"hits": 0, "misses": 0, "stored": 0, "bad": 0, "unstorable": 0}
        self._con = con
        self._pending: list[tuple] = []
        self._pending_bytes = 0
        self._touched: list[tuple] = []
        self._last_flush = time.monotonic()
        self._noted = noted
        self._reset_on_close = False
        self._closed = False

    def __enter__(self) -> "CacheStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _note_once(self, msg: str) -> None:
        if not self._noted:
            self._noted = True
            _cache_note(msg)

    def _fail(self, e: BaseException) -> None:
        import sqlite3
        self.enabled = False
        if type(e) is sqlite3.DatabaseError:    # exactly: OperationalError is a subclass
            self._reset_on_close = True
        self._note_once(f"error ({e}); reading the remaining log files without it")
        self._pending = []
        self._pending_bytes = 0
        self._touched = []

    def get(self, host: str, kind: str, path: str, sig: tuple[int, int],
            params: str = "") -> dict | None:
        if not self.enabled:
            return None
        try:
            row = self._con.execute(
                "SELECT size, mtime_ns, code, payload FROM entries "
                "WHERE host=? AND kind=? AND path=?", (host, kind, path)).fetchone()
            if row is None:
                return None
            size, mtime_ns, code, blob = row
            if size != sig[0] or mtime_ns != sig[1] or code != f"{self.code_key} {params}":
                return None
            p = _decode_cache_payload(blob)
            if p is None:
                self.counts["bad"] += 1
                return None
            self.counts["hits"] += 1
            self._touched.append((host, kind, path))
            return p
        except Exception as e:
            self._fail(e)
            return None

    def host_rows(self, host_key: str) -> list:
        """[(kind, path, size, mtime_ns, code)] for one host key, without payloads. Any
        error: self._fail(e) and []. Not counted as hits; doesn't touch last_used."""
        if not self.enabled:
            return []
        try:
            return [tuple(r) for r in self._con.execute(
                "SELECT kind, path, size, mtime_ns, code FROM entries WHERE host=?",
                (host_key,)).fetchall()]
        except Exception as e:
            self._fail(e)
            return []

    def put(self, host: str, kind: str, path: str, sig: tuple[int, int], payload: dict,
            params: str = "") -> None:
        if not self.enabled:
            return
        try:
            blob = _encode_cache_payload(payload)      # now: cross-file dedup mutates events
            if blob is None:
                self.counts["unstorable"] += 1
                return
            now = int(time.time())
            self._pending.append((host, kind, path, sig[0], sig[1],
                                  f"{self.code_key} {params}", blob, now, now))
            self._pending_bytes += len(blob)
            if (self._pending_bytes >= CACHE_FLUSH_BYTES
                    or time.monotonic() - self._last_flush >= CACHE_FLUSH_SECONDS):
                self.flush()
        except Exception as e:
            self._fail(e)

    def flush(self) -> None:
        if not self.enabled or not (self._pending or self._touched):
            return
        try:
            self._con.execute("BEGIN IMMEDIATE")
            self._con.executemany(
                "INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?,?,?,?)", self._pending)
            now = int(time.time())
            self._con.executemany(
                "UPDATE entries SET last_used=? WHERE host=? AND kind=? AND path=?",
                [(now, *k) for k in self._touched])
            self._con.execute("COMMIT")
            self.counts["stored"] += len(self._pending)
            self._pending = []
            self._pending_bytes = 0
            self._touched = []
            self._last_flush = time.monotonic()
        except Exception as e:
            try:
                self._con.execute("ROLLBACK")
            except Exception:
                pass
            self._fail(e)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()
        except Exception:
            pass
        try:
            self._con.close()
        except Exception:
            pass
        if self._reset_on_close:
            try:
                _cache_unlink_files(self.path)
            except OSError:
                pass


def _cache_connect(sqlite3, db: Path):
    """Open and validate the database; raises on any problem (the caller decides)."""
    os.close(os.open(db, os.O_RDWR | os.O_CREAT, 0o600))
    con = sqlite3.connect(str(db), timeout=CACHE_BUSY_TIMEOUT, isolation_level=None)
    try:
        con.execute("PRAGMA synchronous=NORMAL")
        try:
            rows = con.execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError as e:
            if type(e) is not sqlite3.DatabaseError:
                raise
            raise _CacheCorrupt(str(e))
        if rows != [("ok",)]:
            raise _CacheCorrupt("quick_check failed")
        con.execute("BEGIN IMMEDIATE")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, CACHE_SCHEMA):
            raise _CacheCorrupt(f"schema version {version}")
        if version == 0:
            con.execute(
                "CREATE TABLE IF NOT EXISTS entries ("
                "host TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL, "
                "size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, code TEXT NOT NULL, "
                "payload BLOB NOT NULL, stored_at INTEGER NOT NULL, last_used INTEGER NOT NULL, "
                "PRIMARY KEY (host, kind, path))")
        con.execute(f"PRAGMA user_version = {CACHE_SCHEMA}")     # the write probe
        con.execute("DELETE FROM entries WHERE last_used < ?",
                    (int(time.time()) - CACHE_PRUNE_SECONDS,))
        con.execute("COMMIT")
        return con
    except BaseException:
        try:
            con.close()
        except Exception:
            pass
        raise


def open_cache(*, rebuild: bool = False) -> CacheStore | None:
    """Open or create the cache. Any problem: one stderr note and None (read uncached)."""
    try:
        import sqlite3
    except ImportError:
        _cache_note("unavailable (sqlite3 module missing); reading all log files")
        return None
    try:
        code_key = cache_code_key()
    except OSError as e:
        _cache_note(f"unavailable ({e}); reading all log files")
        return None
    d = cache_dir()
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = d / CACHE_FILENAME
        if rebuild:
            _cache_unlink_files(db)
    except OSError as e:
        _cache_note(f"unavailable ({e}); reading all log files")
        return None

    def corrupt(e: BaseException) -> bool:
        return isinstance(e, _CacheCorrupt) or type(e) is sqlite3.DatabaseError

    try:
        return CacheStore(_cache_connect(sqlite3, db), db, code_key)
    except Exception as e:
        first = e
    if not corrupt(first):
        _cache_note(f"unavailable ({db}: {first}); reading all log files")
        return None
    try:
        _cache_unlink_files(db)
        con = _cache_connect(sqlite3, db)
    except Exception as e:
        _cache_note(f"unavailable ({e}); reading all log files")
        return None
    _cache_note(f"was unreadable and has been reset ({db})")
    return CacheStore(con, db, code_key, noted=True)


def _parse_log_file_cached(store: "CacheStore | None", kind: str, path: str, *,
                           host: str = "local", source: str = "local",
                           label: str | None = None, want_stats: bool = False,
                           agent_id_from_name: bool = False) -> dict | None:
    """_parse_log_file with the cache in front; same return contract. A file is stored
    only if it parsed, was not racy and did not change while it was read."""
    lbl = path if label is None else label
    if store is None or not store.enabled or not _key_text_ok(host, path, lbl, source):
        return _parse_log_file(kind, path, source=source, label=label,
                               want_stats=want_stats, agent_id_from_name=agent_id_from_name)
    params = _cache_params(source, lbl, path, agent_id_from_name)
    sig = _file_sig(path)
    zst_ok = not (kind == "codex" and path.endswith(".zst")) or _zstd_available()
    if sig is not None and zst_ok:
        p = store.get(host, kind, path, sig, params)
        if p is not None:
            return {"events": p["events"], "stats": p["stats"] if want_stats else None}
    store.counts["misses"] += 1
    r = _parse_log_file(kind, path, source=source, label=label, want_stats=True,
                        agent_id_from_name=agent_id_from_name)
    if r is None:
        return None                                   # failures are never stored
    if sig is not None and _file_sig(path) == sig and not _is_racy(sig, time.time_ns()):
        store.put(host, kind, path, sig, r, params)   # encodes now: dedup mutates events later
    return r if want_stats else {"events": r["events"], "stats": None}


_CACHE_OFF_VALUES = ("0", "no", "false", "off")


def cache_mode(no_cache: bool = False, rebuild: bool = False) -> str:
    """'off', 'rebuild' or 'on' for this run. --no-cache wins; then --rebuild-cache (a flag
    beats the environment); then CCTRACK_CACHE, which is off when its stripped, lower-cased
    value is one of _CACHE_OFF_VALUES; otherwise on (unset and empty included)."""
    if no_cache:
        return "off"
    if rebuild:
        return "rebuild"
    if os.environ.get("CCTRACK_CACHE", "").strip().lower() in _CACHE_OFF_VALUES:
        return "off"
    return "on"


def _cache_hits(store) -> int:
    """store.counts["hits"], or 0 for None."""
    return 0 if store is None else store.counts["hits"]


def _note_cache_use(timing: "_Timing", phase: str, store, hits_before: int, n_files: int) -> None:
    """Add 'cache: N reused, M read' to a timing phase's detail (nothing without a store)."""
    if store is None:
        return
    reused = _cache_hits(store) - hits_before
    timing.note(phase, f"cache: {reused:,} reused, {n_files - reused:,} read")


def _files_progress(i: int, n: int, cache, hits0: int) -> str:
    if cache is None:
        return f"{i:,}/{n:,} files"
    return f"{i:,}/{n:,} files, {(i - 1) - (_cache_hits(cache) - hits0):,} changed"


def parse_events(files: list[str], file_stats: dict | None = None,
                 cache: "CacheStore | None" = None) -> list[dict]:
    """Parse all local JSONL files. Dedup API message identities across files
    so resumed sessions aren't double counted. Each event
    is tagged with the file it came from (`_source_file`) for traceability.
    `file_stats`, if given, receives {path: parse stats} (unreadable files get
    {"unreadable": True}). `cache`, if given, serves unchanged files from the parse cache."""
    event_lists = []
    progress = Progress("Reading Claude transcripts")
    n = len(files)
    hits0 = _cache_hits(cache)
    for i, path in enumerate(files, 1):
        progress.update(_files_progress(i, n, cache, hits0))
        r = _parse_log_file_cached(cache, "claude", path, want_stats=file_stats is not None,
                                   agent_id_from_name=True)
        if r is None:
            if file_stats is not None:
                file_stats[path] = {**new_parse_stats(), "unreadable": True}
            continue
        if file_stats is not None:
            file_stats[path] = r["stats"]
        event_lists.append(r["events"])
    if cache is not None:
        progress.update(f"{n - (_cache_hits(cache) - hits0):,} changed of {n:,} files", force=True)
    progress.update("removing duplicates", force=True)
    try:
        return dedup_events(event_lists)
    finally:
        progress.done()


# ── Codex CLI logs ──────────────────────────────────────────────────────
#
# Codex writes one rollout file per thread under $CODEX_HOME (default ~/.codex):
#   sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl   (and archived_sessions/)
# Older rollouts are compressed in place to `.jsonl.zst`.
# Each line is {"timestamp", "type": <item>, "payload": {...}}. Usage comes from:
#   * "token_usage_record" — identified request usage, including compactions.
#   * "event_msg" / payload.type == "token_count" — latest-response usage and/or
#     running totals. These coexist with request records: reconcile matching
#     snapshots, retain later requests, and ignore unchanged re-emissions.
# Token semantics (from the Codex source and its own test fixtures):
#   input_tokens INCLUDES cached_input_tokens and cache_write_input_tokens;
#   output_tokens INCLUDES reasoning_output_tokens. So billable uncached input
#   is input − cached − cache_write, and reasoning is never added on top.
# Model and cwd come from the most recent "turn_context"; thread id and git
# branch from "session_meta".

AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex"}


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def codex_homes() -> list[Path]:
    """The host's Codex home plus each Sandy sandbox's codex/ (sandy mounts it
    at ~/.codex inside the sandbox and never sets CODEX_HOME). A sandbox only
    has codex/ once Codex has run in it. Deduplicated by real path, in case
    CODEX_HOME points into a sandbox."""
    homes, seen = [], set()
    for h in [codex_home()] + [r / "codex" for r in sandbox_roots()]:
        if not h.is_dir():
            continue
        key = h.resolve()
        if key not in seen:
            seen.add(key)
            homes.append(h)
    return homes


def discover_codex_files(home: Path | list[Path] | None = None) -> list[str]:
    """Rollout files (plain and .zst) under sessions/ and archived_sessions/ of
    one Codex home, a list of them, or (default) every home on this machine.
    When both a plain file and its .zst sibling exist, the plain one wins
    (Codex materialises a .zst back to plain when it appends)."""
    if home is None:
        homes = codex_homes()
    elif isinstance(home, (list, tuple)):
        homes = list(home)
    else:
        homes = [home]
    found: dict[str, str] = {}
    progress = Progress("Finding Codex rollouts")
    for sub, h in ((sub, h) for h in homes for sub in ("sessions", "archived_sessions")):
        root = h / sub
        if not root.exists():
            continue
        progress.update(f"{len(found):,} found")
        for f in root.rglob("rollout-*.jsonl*"):
            progress.update(f"{len(found):,} found")
            name = f.name
            if name.endswith(".jsonl"):
                found[str(f)] = str(f)
            elif name.endswith(".jsonl.zst"):
                found.setdefault(str(f)[: -len(".zst")], str(f))
    progress.done()
    return sorted(found.values())


def _read_zstd(path: str) -> str | None:
    """Decompress a .zst rollout. Stdlib only gained zstd in Python 3.14, so
    older interpreters fall back to the `zstd` CLI; returns None if neither."""
    try:
        from compression import zstd  # type: ignore  # Python 3.14+
        with zstd.open(path, "rt", errors="replace") as f:
            return f.read()
    except ImportError:
        pass
    except OSError:
        return None
    import shutil
    if not shutil.which("zstd"):
        return None
    try:
        r = subprocess.run(["zstd", "-dc", path], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None


def _normalize_codex_usage(usage: dict) -> dict:
    """Normalize rollout/API aliases; caches and reasoning are inclusive."""
    def count(*keys):
        for key in keys:
            if usage.get(key) is not None:
                try:
                    return max(0, int(usage[key]))
                except (TypeError, ValueError, OverflowError):
                    return 0
        return 0
    inp = count("input_tokens", "prompt_tokens", "input")
    cached = min(inp, count("cached_input_tokens", "cache_read_input_tokens", "cached_tokens"))
    write = min(inp - cached, count("cache_write_input_tokens", "cache_creation_input_tokens"))
    out = count("output_tokens", "completion_tokens", "output")
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "cache_write_input_tokens": write, "output_tokens": out,
            "reasoning_output_tokens": min(out, count("reasoning_output_tokens", "reasoning_tokens")),
            "total_tokens": count("total_tokens") or inp + out}


def _usage_signature(usage: dict) -> tuple:
    return tuple(usage[k] for k in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                                    "output_tokens", "reasoning_output_tokens", "total_tokens"))


def _codex_usage_event(usage: dict, ts: str, key: str, ctx: dict, source: str) -> dict | None:
    if _event_dt({"timestamp": ts}) is None:
        return None
    usage = _normalize_codex_usage(usage)
    inp = usage["input_tokens"]
    cached = usage["cached_input_tokens"]
    cache_write = usage["cache_write_input_tokens"]
    out = usage["output_tokens"]
    uncached = max(0, inp - cached - cache_write)
    if not (uncached or cached or cache_write or out):
        return None
    cwd = ctx.get("cwd", "")
    session_id = ctx.get("session_id", "")
    return {
        "model": ctx.get("model") or "codex-unknown",
        "timestamp": ts,
        "input": uncached,
        "output": out,
        "cache_read": cached,
        "cache_write": cache_write,
        "cache_write_1h": 0,
        "_request_id": key,
        "_request_input": inp,
        "source": source,
        "session_id": session_id,
        "project": _project_name(cwd),
        "cwd": cwd,
        "git_branch": ctx.get("git_branch", ""),
        "agent": "codex",
        "service_tier": ctx.get("service_tier"),
        "_raw_usage": _usage_signature(usage),
        # A spawned subagent is its own thread; link it to its parent.
        "parent_session_id": ctx.get("parent_session_id", ""),
        "is_sidechain": bool(ctx.get("parent_session_id")),
        "agent_id": "",
    }


def _codex_parent_id(meta: dict) -> str:
    """Parent thread id of a spawned/forked Codex thread, from session_meta
    (`source: {"subagent": {"thread_spawn": {"parent_thread_id": …}}}` or a
    fork's `forked_from_id`). Empty for a top-level thread."""
    stack = [meta.get("source")]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("parent_thread_id", "parent_session_id") and isinstance(v, str) and v:
                    return v
                stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return ""


def _codex_snapshot_remainder(snapshot: dict, record: dict) -> dict | None:
    """A first total-only snapshot can include spend before the known record.

    Preserve that remainder rather than replacing the entire cumulative delta
    with the single response (400 cumulative minus a 300-token record leaves
    100 real earlier tokens). A last-usage snapshot already names one request.
    """
    if not snapshot.get("_total_only"):
        return None
    usage = dict(zip(("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                      "output_tokens", "reasoning_output_tokens", "total_tokens"),
                     (max(0, a - b) for a, b in zip(snapshot["_raw_usage"], record["_raw_usage"]))))
    ctx = {"model": snapshot.get("model", ""), "session_id": snapshot.get("session_id", ""),
           "cwd": snapshot.get("cwd", ""), "git_branch": snapshot.get("git_branch", ""),
           "service_tier": snapshot.get("service_tier"),
           "parent_session_id": snapshot.get("parent_session_id", "")}
    remainder = _codex_usage_event(usage, snapshot["timestamp"],
                                    snapshot["_request_id"] + ":remainder", ctx, snapshot["source"])
    if remainder:
        remainder.update({"_codex_legacy": True, "_codex_position": snapshot.get("_codex_position", 0),
                          "_legacy_sequence": snapshot.get("_legacy_sequence", 0),
                          "_fork_parent": snapshot.get("_fork_parent", "")})
        for key in ("_source_file", "_locations"):
            if key in snapshot:
                remainder[key] = snapshot[key]
    return remainder


def parse_codex_lines(lines, source: str = "local", stats: dict | None = None) -> list[dict]:
    """Reconcile request records with cumulative snapshots, including compactions.

    A request record replaces its matching snapshot, not every later snapshot.
    Legacy last-only and total-only formats remain supported. Preserve thread
    and counter identities for replay detection after all sources are loaded.
    """
    ctx: dict = {}
    events: list[dict | None] = []
    previous = None
    pending_record = None
    latest_snapshot = None
    fork_parent = ""
    response_indexes = {}
    compacted_ids = set()
    legacy_sequence = 0

    for position, line in enumerate(lines):
        if not line.strip():
            continue
        if stats is not None:
            stats["lines"] += 1
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            if stats is not None:
                stats["bad_lines"] += 1
            continue
        if not isinstance(item, dict):
            continue
        kind, payload, ts = item.get("type"), item.get("payload") or {}, item.get("timestamp", "")
        if not isinstance(payload, dict):
            continue
        if stats is not None:
            _note_ts(stats, ts)
            if kind == "compacted" or (kind == "event_msg" and payload.get("type") == "context_compacted"):
                _note_compaction(stats, (ctx.get("session_id", ""), ""),
                                 payload.get("compaction_response_id") or ts[:19], ts)

        if kind == "session_meta":
            ctx["session_id"] = payload.get("id") or payload.get("session_id") or ""
            parent = (_codex_parent_id(payload) or payload.get("forked_from_id")
                      or (payload.get("session_id") if payload.get("id") else "") or "")
            if parent and parent != ctx["session_id"]:
                ctx["parent_session_id"] = parent
            fork_parent = payload.get("forked_from_id") or ""
            ctx.setdefault("cwd", payload.get("cwd", ""))
            git = payload.get("git") or {}
            if isinstance(git, dict) and git.get("branch"):
                ctx["git_branch"] = git["branch"]
            if payload.get("model"):
                ctx["model"] = payload["model"]
        elif kind == "compacted":
            response_id = payload.get("compaction_response_id")
            if response_id:
                compacted_ids.add(response_id)
                if response_id in response_indexes:
                    events[response_indexes[response_id]]["_is_compaction"] = True
        elif kind == "turn_context":
            for key in ("model", "cwd"):
                if payload.get(key):
                    ctx[key] = payload[key]
            if "service_tier" in payload:
                ctx["service_tier"] = normalize_service_tier(payload["service_tier"])
        elif kind == "event_msg" and payload.get("type") == "thread_settings_applied":
            settings = payload.get("thread_settings") or {}
            if "service_tier" in settings:
                ctx["service_tier"] = normalize_service_tier(settings["service_tier"])
            if settings.get("model"):
                ctx["model"] = settings["model"]
        elif kind == "token_usage_record":
            usage = payload.get("usage")
            if not isinstance(usage, dict):
                continue
            rid = (payload.get("response_id") or "").strip()
            owner = dict(ctx)
            if payload.get("thread_id"):
                owner["session_id"] = payload["thread_id"]
                if payload["thread_id"] != ctx.get("session_id"):
                    owner.pop("parent_session_id", None)
            if (payload.get("session_id") and payload.get("thread_id")
                    and payload["session_id"] != payload["thread_id"]):
                owner["parent_session_id"] = payload["session_id"]
            if payload.get("model"):
                owner["model"] = payload["model"]
            if "service_tier" in payload:
                owner["service_tier"] = normalize_service_tier(payload["service_tier"])
            key = f"codex:resp:{rid}" if rid else f"codex:rec:{owner.get('session_id', '')}:{ts}"
            ev = _codex_usage_event(usage, ts, key, owner, source)
            if not ev:
                continue
            ev["_response_id"] = rid
            ev["_is_compaction"] = rid in compacted_ids
            ev["_model_explicit"] = bool(payload.get("model"))
            thread_total = payload.get("thread_token_usage")
            ev["_thread_total"] = (_usage_signature(_normalize_codex_usage(thread_total))
                                   if isinstance(thread_total, dict) else None)
            if rid and rid in response_indexes:
                i = response_indexes[rid]
                events[i] = _merge_usage_copies(events[i], ev)
                continue
            # Some rollouts write the record after its cumulative snapshot.
            if latest_snapshot is not None:
                snapshot = events[latest_snapshot]
                if snapshot and ev["_thread_total"] in snapshot.get("_snapshot_aliases", []):
                    ev["_snapshot_aliases"] = snapshot["_snapshot_aliases"]
                    remainder = _codex_snapshot_remainder(snapshot, ev)
                    events[latest_snapshot] = remainder
                    # A split residual must keep its own identity: aliases for
                    # the whole cumulative snapshot belong to the request.
            events.append(ev)
            pending_record = len(events) - 1
            if rid:
                response_indexes[rid] = pending_record
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            if not isinstance(info, dict):
                continue
            total = info.get("total_token_usage")
            total = _normalize_codex_usage(total) if isinstance(total, dict) else None
            signature = _usage_signature(total) if total is not None else None
            if total is not None and previous is not None and signature == _usage_signature(previous):
                continue
            last = info.get("last_token_usage")
            delta = ({k: max(0, v - (previous or {}).get(k, 0)) for k, v in total.items()}
                     if total is not None else None)
            if isinstance(last, dict):
                usage = _normalize_codex_usage(last)
            elif total is not None:
                usage = delta
            else:
                continue
            if total is not None:
                previous = total
            owner = dict(ctx)
            if info.get("model") or payload.get("model"):
                owner["model"] = payload.get("model") or info["model"]
            if "service_tier" in payload:
                owner["service_tier"] = normalize_service_tier(payload["service_tier"])
            sid = owner.get("session_id", "")
            identity = signature if signature is not None else (ts, *_usage_signature(usage))
            key = f"codex:tc:{sid}:{identity}"
            ev = _codex_usage_event(usage, ts, key, owner, source)
            if not ev:
                continue
            ev.update({"_codex_legacy": True, "_codex_position": position,
                       "_legacy_sequence": legacy_sequence,
                       "_fork_parent": fork_parent,
                       "_total_only": not isinstance(last, dict),
                       "_snapshot_aliases": [signature] if signature else []})
            legacy_sequence += 1
            record = events[pending_record] if pending_record is not None else None
            if record and (record["_raw_usage"] == ev["_raw_usage"] or
                           (signature is not None and record.get("_thread_total") == signature)):
                record["_snapshot_aliases"] = list({tuple(v) for v in
                    [*record.get("_snapshot_aliases", []), *ev["_snapshot_aliases"]]})
                record["_codex_legacy"] = True
                record["_codex_position"] = position
                record["_legacy_sequence"] = ev["_legacy_sequence"]
                record["_fork_parent"] = fork_parent
                # A response id remains the primary identity; keep its recorded
                # model and timestamp while adopting the snapshot's counter key.
                remainder = _codex_snapshot_remainder(ev, record)
                if remainder:
                    events.append(remainder)
                latest_snapshot = pending_record
            else:
                events.append(ev)
                latest_snapshot = len(events) - 1
            pending_record = None

    out = dedup_events([[e for e in events if e is not None]])
    if stats is not None:
        stats["records"] += len(out)
    return out


def parse_codex_events(files: list[str], source: str = "local",
                       file_stats: dict | None = None,
                       cache: "CacheStore | None" = None) -> tuple[list[dict], int]:
    """Parse Codex rollouts. Dedups by response id across files (forked and
    resumed threads replay their parent's history). Returns (events, skipped)
    where `skipped` counts .zst files that couldn't be decompressed."""
    event_lists = []
    skipped = 0
    progress = Progress("Reading Codex rollouts")
    n = len(files)
    hits0 = _cache_hits(cache)
    for i, path in enumerate(files, 1):
        progress.update(_files_progress(i, n, cache, hits0))
        r = _parse_log_file_cached(cache, "codex", path, source=source,
                                   want_stats=file_stats is not None)
        if r is None:
            if path.endswith(".zst"):
                skipped += 1
            if file_stats is not None:
                file_stats[path] = {**new_parse_stats(), "unreadable": True}
            continue
        if file_stats is not None:
            file_stats[path] = r["stats"]
        event_lists.append(r["events"])
    if cache is not None:
        progress.update(f"{n - (_cache_hits(cache) - hits0):,} changed of {n:,} files", force=True)
    progress.done()
    return dedup_events(event_lists), skipped


REMOTE_CODEX_SEP = "___CCTRACK_CODEX_FILE___"


def fetch_remote_codex(host: str) -> str:
    """SSH to host and stream its Codex rollouts — its own Codex home plus each
    sandbox's codex/ — decompressing .zst remotely when the host has `zstd`."""
    homes = ['"${CODEX_HOME:-$HOME/.codex}"'] + [_q(f"{r}/codex") for r in remote_sandbox_roots(host)]
    dirs = " ".join(f"{h}/sessions {h}/archived_sessions" for h in homes)
    remote_cmd = (
        f'find {dirs} -name "rollout-*.jsonl" '
        f"-exec echo '{REMOTE_CODEX_SEP}' {{}} \\; -exec cat {{}} \\; 2>/dev/null; "
        'if command -v zstd >/dev/null 2>&1; then '
        f'find {dirs} -name "rollout-*.jsonl.zst" '
        f"-exec echo '{REMOTE_CODEX_SEP}' {{}} \\; -exec zstd -dc {{}} \\; 2>/dev/null; fi; true"
    )
    try:
        result = _ssh_fetch(host, remote_cmd, "Codex rollouts")
        return result.stdout or ""
    except subprocess.TimeoutExpired:
        _warn_timeout(host, "Codex rollouts")
        return ""
    except FileNotFoundError:
        return ""


def parse_remote_codex_events(host: str, file_stats: dict | None = None) -> list[dict]:
    bundle = _remote_bundle_cache.get(host)
    if bundle is not None and bundle.get("ok"):
        if file_stats is not None:
            file_stats.update(bundle.get("codex_stats", {}))
        return bundle.get("codex", [])
    if bundle is not None and bundle.get("failed"):
        return []
    raw = fetch_remote_codex(host)
    if not raw:
        return []
    return _remote_parse(host, raw, REMOTE_CODEX_SEP, parse_codex_lines, file_stats)


# ── Remote parsing ──────────────────────────────────────────────────────
#
# Streaming a host's raw logs moves gigabytes of conversation content that is
# thrown away on arrival. Instead cctrack sends its own source to the host
# (`ssh host python3 -`), parses there with exactly the per-file code a raw
# stream would get (_parse_remote_files, parse_hook_blob), and ships back the
# usage events, parse stats and hook rows: zlib-compressed, tagged JSON (see
# _tag_json) so tuples, datetimes and non-string keys round-trip exactly. The
# collector is read-only on the host (no hook rollup). Hosts without Python
# 3.10+ fall back to the raw (gzip-compressed) stream. CCTRACK_REMOTE_PARSE=0
# forces the raw stream.

REMOTE_PROTOCOL = b"CCTRACK_REMOTE_V2\n"
REMOTE_TIMING = "CCTRACK_REMOTE_TIMING "
REMOTE_UNSUPPORTED = "CCTRACK_REMOTE_UNSUPPORTED"
REMOTE_PROGRESS = "CCTRACK_PROGRESS "
_remote_bundle_cache: dict[str, dict] = {}
_remote_progress_lines = False  # set on the remote host: Progress → stderr lines


def _tag_json(o):
    """JSON-safe form of parser output that _untag_json restores exactly."""
    if isinstance(o, dict):
        if all(isinstance(k, str) for k in o):
            if "__cct" in o:
                return {"__cct": "d", "v": [[_tag_json(k), _tag_json(v)] for k, v in o.items()]}
            return {k: _tag_json(v) for k, v in o.items()}
        return {"__cct": "d", "v": [[_tag_json(k), _tag_json(v)] for k, v in o.items()]}
    if isinstance(o, tuple):
        return {"__cct": "t", "v": [_tag_json(v) for v in o]}
    if isinstance(o, (set, frozenset)):
        return {"__cct": "s", "v": [_tag_json(v) for v in o]}
    if isinstance(o, list):
        return [_tag_json(v) for v in o]
    if isinstance(o, datetime):
        return {"__cct": "dt", "v": o.isoformat()}
    return o


def _untag_json(o):
    if isinstance(o, list):
        return [_untag_json(v) for v in o]
    if isinstance(o, dict):
        kind = o.get("__cct")
        if kind == "t":
            return tuple(_untag_json(v) for v in o["v"])
        if kind == "s":
            return {_untag_json(v) for v in o["v"]}
        if kind == "dt":
            return datetime.fromisoformat(o["v"])
        if kind == "d":
            return {_untag_json(k): _untag_json(v) for k, v in o["v"]}
        return {k: _untag_json(v) for k, v in o.items()}
    return o


def _read_text_lines(path: str):
    try:
        with open(path, "r", errors="replace") as f:
            return f.readlines()
    except OSError:
        return None


def _parse_env_key() -> str:
    """What decides a parse on the machine running it, besides the source and the
    file: interpreter and default text encoding. Runs on the remote (the 'rkey')."""
    import codecs
    import io
    enc = codecs.lookup(io.TextIOWrapper(io.BytesIO()).encoding).name
    return f"{sys.implementation.name}-{'.'.join(map(str, sys.version_info[:3]))} {enc}"


def _remote_host_key(host: str) -> str:
    """The `host` column for a --remote host's cache rows. Local rows are "local", so
    a host literally named "local" can never collide with them."""
    return "remote:" + host


def _remote_cache_params(host: str, rkey: str) -> str:
    """The params part of every remote row's code. `host` fixes source (= host) and
    label (= f"{host}:{path}"); remote parses never use agent_id_from_name; the kind
    is in the key."""
    return json.dumps(["remote", host, rkey])


def _remote_manifest(store, host: str, kinds: tuple) -> dict | None:
    """The manifest to send to `host`: {"rkey", "files": {kind: {path: [size, mtime_ns]}}}
    from the rows this store holds for it under the current code key, or None for an
    empty one. If several rkeys qualify, the one with the most rows wins (ties: the
    greatest string)."""
    if store is None or not store.enabled:
        return None
    prefix = f"{store.code_key} "
    by_rkey: dict[str, list] = {}
    for kind, path, size, mtime_ns, code in store.host_rows(_remote_host_key(host)):
        if kind not in kinds or not code.startswith(prefix):
            continue
        try:
            tail = json.loads(code[len(prefix):])
        except ValueError:
            continue
        if not (isinstance(tail, list) and len(tail) == 3 and isinstance(tail[2], str)):
            continue
        if code[len(prefix):] != _remote_cache_params(host, tail[2]):
            continue
        by_rkey.setdefault(tail[2], []).append((kind, path, size, mtime_ns))
    if not by_rkey:
        return None
    rkey = max(by_rkey, key=lambda k: (len(by_rkey[k]), k))
    files: dict[str, dict] = {}
    for kind, path, size, mtime_ns in by_rkey[rkey]:
        files.setdefault(kind, {})[path] = [size, mtime_ns]
    return {"rkey": rkey, "files": files}


def _hook_rows_from_blob_text(text: str) -> list[dict]:
    """One statusline file's rows exactly as parse_hook_blob would split it."""
    return _hook_rows_from_lines([l for ch in text.split(HOOK_FILE_SEP) for l in ch.splitlines()])


def _hook_text_breaks_blob(text: str) -> bool:
    """True if this statusline text holds a separator that makes parse_hook_blob's
    partitioning cross file boundaries (it can't be reproduced per file)."""
    return MONTHLY_FILE_SEP in text or BASELINE_FILE_SEP in text


def _remote_hook_blob(hook_dirs, patterns=None) -> str:
    """The whole-blob hook stream (v1's): statusline, monthly and baseline files, each
    behind its separator, joined with newlines ("" if there are none)."""
    parts: list[str] = []
    for pattern, sep in patterns or (("statusline*.jsonl", HOOK_FILE_SEP),
                                     ("monthly-*.json", MONTHLY_FILE_SEP),
                                     ("session-baselines.json", BASELINE_FILE_SEP)):
        for d in hook_dirs:
            for f in sorted(d.rglob(pattern)):
                lines = _read_text_lines(str(f))
                if lines is not None:
                    parts.append(sep + "\n" + "".join(lines))
    return "\n".join(parts)


def _remote_hook_extra(hook_dirs) -> list:
    """[monthly, baselines] from the monthly and baseline files only."""
    blob = _remote_hook_blob(hook_dirs, (("monthly-*.json", MONTHLY_FILE_SEP),
                                         ("session-baselines.json", BASELINE_FILE_SEP)))
    if not blob:
        return [{}, {}]
    _, monthly, baselines = parse_hook_blob(blob)
    return [monthly, baselines]


def _remote_stat_item(path: str, known: dict) -> tuple:
    """(sig, hit): the file's current stat, and whether the manifest has exactly it."""
    sig = _file_sig(path)
    k = known.get(path)
    return sig, (sig is not None and isinstance(k, (list, tuple)) and list(k) == [sig[0], sig[1]])


def _remote_file_item(host: str, kind: str, path: str, known: dict) -> list | None:
    """One Claude/Codex file's wire item: a hit marker (stat matches the manifest and the
    file can be decoded here) or its own per-file parse. None = unreadable (no item)."""
    sig, hit = _remote_stat_item(path, known)
    if hit and (not (kind == "codex" and path.endswith(".zst")) or _zstd_available()):
        return [path, sig[0], sig[1], "h"]
    lines = _read_log_lines(kind, path)
    if lines is None:
        return None
    r = _parse_log_lines(kind, lines, f"{host}:{path}", source=host, want_stats=True)
    ok = sig is not None and _file_sig(path) == sig and not _is_racy(sig, time.time_ns())
    return [path, *(sig or (0, 0)), "s" if ok else "n", r]


def _remote_hook_items(host: str, hook_dirs, known: dict) -> list | None:
    """Per-file wire items for the host's statusline files (remote blob semantics), or
    None when a missed file holds separator text (the caller falls back to the blob)."""
    items = []
    for d in hook_dirs:
        for f in sorted(d.rglob("statusline*.jsonl")):
            path = str(f)
            sig, hit = _remote_stat_item(path, known)
            if hit:
                items.append([path, sig[0], sig[1], "h"])
                continue
            lines = _read_text_lines(path)
            if lines is None:
                continue
            text = "".join(lines)
            if _hook_text_breaks_blob(text):
                return None
            payload = {"events": _hook_rows_from_blob_text(text), "stats": None}
            ok = sig is not None and _file_sig(path) == sig and not _is_racy(sig, time.time_ns())
            items.append([path, *(sig or (0, 0)), "s" if ok else "n", payload])
    return items


def _remote_collect_files(host: str, want_claude: bool, want_codex: bool,
                          timing: bool = False, manifest: dict | None = None) -> dict:
    """Runs ON the remote host; read-only; never opens a cache. Same discovery as the
    raw stream. Returns the v2 wire data: per file, in discovery order, a hit marker or
    that file's own per-file parse; never deduped across files."""
    rkey = _parse_env_key()
    out: dict = {"python": sys.version.split()[0], "rkey": rkey, "files": {}, "counts": {}}
    known: dict = {}
    if isinstance(manifest, dict) and manifest.get("rkey") == rkey \
            and isinstance(manifest.get("files"), dict):
        known = {k: v for k, v in manifest["files"].items() if isinstance(v, dict)}
    phases: dict[str, float] = {"discovery": 0.0}
    census_paths: dict[str, list] = {}
    t = time.perf_counter()

    def collect(kind: str, paths: list, progress: Progress) -> list:
        items, skipped = [], 0
        kk = known.get(kind, {})
        for i, path in enumerate(paths, 1):
            progress.update(f"{i:,}/{len(paths):,} files")
            it = _remote_file_item(host, kind, path, kk)
            if it is None:
                if kind == "codex" and path.endswith(".zst"):
                    skipped += 1
                continue
            items.append(it)
        reused = sum(1 for it in items if it[3] == "h")
        out["files"][kind] = items
        out["counts"][kind] = [reused, len(paths) - reused]
        if kind == "codex":
            out["zst_skipped"] = skipped
        return items

    if want_claude:
        files = discover_files(default_dirs())
        if timing:
            now = time.perf_counter()
            phases["discovery"] += now - t
            t = now
            census_paths["claude parse"] = files
        collect("claude", files, Progress("parsing Claude transcripts"))
        if timing:
            now = time.perf_counter()
            phases["claude parse"] = now - t
            t = now

        hook_dirs = discover_hook_data_dirs()
        hook_files = [f for d in hook_dirs for f in sorted(d.rglob("statusline*.jsonl"))]
        if timing:
            now = time.perf_counter()
            phases["discovery"] += now - t
            t = now
            census_paths["hook read"] = hook_files
        items = _remote_hook_items(host, hook_dirs, known.get("hook", {}))
        if items is None:
            blob = _remote_hook_blob(hook_dirs)
            out["hook"] = parse_hook_blob(blob) if blob else ([], {}, {})
            out["counts"]["hook"] = [0, len(hook_files)]
        else:
            out["files"]["hook"] = items
            out["hook_extra"] = _remote_hook_extra(hook_dirs)
            reused = sum(1 for it in items if it[3] == "h")
            out["counts"]["hook"] = [reused, len(hook_files) - reused]
        if timing:
            now = time.perf_counter()
            phases["hook read"] = now - t
            t = now
    if want_codex:
        files = discover_codex_files()
        if timing:
            now = time.perf_counter()
            phases["discovery"] += now - t
            t = now
            census_paths["codex parse"] = files
        collect("codex", files, Progress("parsing Codex rollouts"))
        if timing:
            phases["codex parse"] = time.perf_counter() - t
    if timing:
        out["timing"] = {"phases": phases,
                         "census": {k: _file_census(v) for k, v in census_paths.items()}}
    return out


def _assemble_remote_bundle(host: str, data: dict, store) -> dict | None:
    """v2 wire data -> the bundle fetch_remote_bundle caches. Hits are loaded from the
    store and misses flagged "s" are put (encoded at put, before any dedup). None if a
    hit can't be supplied: the caller refetches everything."""
    t0 = time.perf_counter()
    hk = _remote_host_key(host)
    params = _remote_cache_params(host, data["rkey"])
    st = store if store is not None and store.enabled else None
    res: dict = {}

    def walk(kind: str) -> tuple | None:
        lists, stats = [], {}
        for item in data["files"][kind]:
            path, sig = item[0], (item[1], item[2])
            if item[3] == "h":
                p = st.get(hk, kind, path, sig, params) if st else None
                if p is None:
                    return None
            else:
                p = item[4]
                if item[3] == "s" and st is not None and _key_text_ok(hk, path, f"{host}:{path}"):
                    st.put(hk, kind, path, sig, p, params)
            lists.append(p["events"])
            stats[f"{host}:{path}"] = p["stats"]
        return lists, stats

    for kind in ("claude", "codex"):
        if kind in data["files"]:
            got = walk(kind)
            if got is None:
                return None
            res[kind] = dedup_events(got[0])
            res[f"{kind}_stats"] = got[1]
    if "hook" in data["files"]:
        got = walk("hook")
        if got is None:
            return None
        rows = [r for lst in got[0] for r in lst]
        rows.sort(key=lambda r: (r["session_id"], r["ts"]))
        res["hook"] = (rows, *data["hook_extra"])
    elif "hook" in data:
        res["hook"] = tuple(data["hook"])
    for k in ("python", "zst_skipped", "timing"):
        if k in data:
            res[k] = data[k]
    counts = data["counts"].values()
    res["cache_counts"] = {"reused": sum(c[0] for c in counts), "read": sum(c[1] for c in counts)}
    res["_cached"] = st is not None
    res["_local_merge"] = time.perf_counter() - t0
    return res


def _remote_collect(host: str, want_claude: bool, want_codex: bool, timing: bool = False) -> dict:
    """The v1-shaped per-host result (claude, claude_stats, codex, codex_stats, zst_skipped,
    hook, timing, python), as the local side assembles it from the v2 wire data with no
    cache. Kept for tests and as the definition of "the per-host result"."""
    res = _assemble_remote_bundle(host, _remote_collect_files(host, want_claude, want_codex, timing),
                                  None)
    for k in ("cache_counts", "_cached", "_local_merge"):
        res.pop(k, None)
    return res


def _remote_main(host: str, want_claude: bool, want_codex: bool, timing: bool = False,
                 manifest: str = "") -> None:
    """Entry point of the program fetch_remote_bundle sends to the host."""
    global _remote_progress_lines
    _remote_progress_lines = True
    import zlib
    known = None
    try:
        if manifest:
            known = json.loads(zlib.decompress(base64.b64decode(manifest)))
            if not isinstance(known, dict) or not isinstance(known.get("files"), dict):
                known = None
    except Exception:
        known = None
    data = _remote_collect_files(host, want_claude, want_codex, timing, known)
    t = time.perf_counter()
    payload = zlib.compress(json.dumps(_tag_json(data), separators=(",", ":")).encode(), 6)
    secs = time.perf_counter() - t
    sys.stdout.buffer.write(REMOTE_PROTOCOL + payload)
    sys.stdout.flush()
    if timing:
        sys.stderr.write(f"{REMOTE_TIMING}encode={secs:.6f}\n")
        sys.stderr.flush()


def _remote_program(host: str, want_claude: bool, want_codex: bool, timing: bool = False,
                    manifest: dict | None = None) -> bytes:
    import zlib
    src = Path(__file__).read_bytes()
    blob = base64.b64encode(zlib.compress(src, 9)).decode()
    extra = ", timing=True" if timing else ""
    if manifest:
        m = base64.b64encode(zlib.compress(
            json.dumps(manifest, separators=(",", ":")).encode(), 9)).decode()
        extra += f", manifest='{m}'"
    return (
        "import sys\n"
        "if sys.version_info < (3, 10):\n"
        f"    sys.stdout.write('{REMOTE_UNSUPPORTED} %d.%d\\n' % sys.version_info[:2])\n"
        "    sys.exit(0)\n"
        "import base64, types, zlib\n"
        "mod = types.ModuleType('cctrack_remote')\n"
        "mod.__file__ = '<cctrack>'\n"
        f"exec(compile(zlib.decompress(base64.b64decode('{blob}')), 'cctrack', 'exec'),"
        " mod.__dict__)\n"
        f"mod._remote_main({host!r}, {bool(want_claude)!r}, {bool(want_codex)!r}{extra})\n"
    ).encode()


def _remote_exchange(host: str, want_claude: bool, want_codex: bool,
                     manifest: dict | None) -> dict:
    """One `ssh host python3 -` call and its reply handling. Returns {"data", "wire",
    "decode", "encode"} on success, else {"failed": True} or {"fallback": True}."""
    import zlib
    cmd = [*_SSH, host, "python3", "-"]
    try:
        r = _run_ssh(cmd, input=_remote_program(host, want_claude, want_codex,
                                              timing=timing_enabled(), manifest=manifest),
                     capture_output=True, timeout=remote_fetch_timeout(),
                     progress=f"{host}: parsing logs on {host}")
    except subprocess.TimeoutExpired:
        _warn_timeout(host, "remotely parsed logs")
        return {"failed": True}
    except FileNotFoundError:
        print("  Warning: ssh command not found", file=sys.stderr)
        return {"failed": True}
    out = r.stdout if isinstance(r.stdout, bytes) else (r.stdout or "").encode()
    err = r.stderr.decode(errors="replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
    if out.startswith(REMOTE_PROTOCOL):
        try:
            t0 = time.perf_counter()
            data = _untag_json(json.loads(zlib.decompress(out[len(REMOTE_PROTOCOL):])))
            decode = time.perf_counter() - t0
            if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
                raise ValueError("unexpected reply")
        except (zlib.error, ValueError, TypeError, KeyError) as exc:
            print(f"  Note: couldn't read {host}'s parsed results ({exc}); "
                  f"streaming raw logs instead.", file=sys.stderr)
            return {"fallback": True}
        encode = None
        for line in err.splitlines():
            if line.startswith(REMOTE_TIMING):
                try:
                    encode = float(line.split("encode=", 1)[1])
                except (IndexError, ValueError):
                    pass
        return {"data": data, "wire": len(out), "decode": decode, "encode": encode}
    if r.returncode == 255:
        print(f"  Warning: SSH to {host} failed: {err.strip()}", file=sys.stderr)
        return {"failed": True}
    why = out.decode(errors="replace").strip()
    if why.startswith(REMOTE_UNSUPPORTED):
        why = f"its python3 is {why.split()[-1]}; cctrack needs 3.10+"
    else:
        tail = err.strip().splitlines()[-1:] or ["python3 not available"]
        why = tail[0]
    print(f"  Note: couldn't parse on {host} ({why}); streaming raw logs "
          f"instead (slower).", file=sys.stderr)
    return {"fallback": True}


def fetch_remote_bundle(host: str, want_claude: bool = True, want_codex: bool = True,
                        cache: "CacheStore | None" = None) -> dict:
    """Parse a host's logs on the host itself and fetch the results (cached).
    Returns {"ok": True, ...} on success, {"failed": True} when the host is
    unreachable or timed out (nothing more to try), or {"fallback": True}
    when it lacks a usable Python — callers then stream raw logs. With `cache`, the
    host skips files whose stat the cache already holds (protocol v2)."""
    if host in _remote_bundle_cache:
        return _remote_bundle_cache[host]
    if os.environ.get("CCTRACK_REMOTE_PARSE", "1").strip() in ("0", "no", "false", "off"):
        res = {"fallback": True}
        _remote_bundle_cache[host] = res
        return res
    print(f"  Fetching from {host} (parsing on {host})...", file=sys.stderr)
    st = cache if cache is not None and cache.enabled else None
    if st is not None:
        # The shipped source must be the source the cache key was made from.
        try:
            import hashlib
            now_key = hashlib.sha256(Path(__file__).read_bytes()).hexdigest() + " "
        except OSError:
            now_key = None
        if now_key is None or not st.code_key.startswith(now_key):
            st = None
    kinds = ("claude", "hook") if want_claude else ()
    if want_codex:
        kinds += ("codex",)
    manifest = _remote_manifest(st, host, kinds) if st is not None else None

    ex = _remote_exchange(host, want_claude, want_codex, manifest)
    if "data" not in ex:
        res = ex
    else:
        exchanges, res = [ex], None
        try:
            bundle = _assemble_remote_bundle(host, ex["data"], st)
            if bundle is None:
                print(f"cctrack: cache couldn't supply {host}'s unchanged files; "
                      f"fetching everything from {host} again", file=sys.stderr)
                ex = _remote_exchange(host, want_claude, want_codex, None)
                if "data" not in ex:
                    res = ex
                else:
                    exchanges.append(ex)
                    bundle = _assemble_remote_bundle(host, ex["data"], st)
                    if bundle is None:
                        raise ValueError("a hit marker in the reply to an empty manifest")
                    bundle["_refetched"] = True
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            print(f"  Note: couldn't read {host}'s parsed results ({exc}); "
                  f"streaming raw logs instead.", file=sys.stderr)
            res = {"fallback": True}
        if res is None:
            res = {"ok": True, **bundle, "_wire_bytes": sum(x["wire"] for x in exchanges),
                   "_local_decode": sum(x["decode"] for x in exchanges)}
            if exchanges[-1]["encode"] is not None:
                res["_remote_encode"] = exchanges[-1]["encode"]
    if res.get("ok") and res.get("zst_skipped"):
        print(f"  Note: {host}: skipped {res['zst_skipped']} compressed Codex rollout(s) "
              f"(.jsonl.zst) — install `zstd` there (or Python 3.14+) to include them.",
              file=sys.stderr)
    _remote_bundle_cache[host] = res
    return res


# ── Hook data parsing ──────────────────────────────────────────────────

def _hook_rows_from_lines(lines) -> list[dict]:
    """Parse statusline JSONL lines into per-render rows sorted by timestamp.

    Only `cost_usd` is meaningful here: it is the session's running cumulative
    spend. The token fields the hook records (`input_tokens`, `output_tokens`,
    `cache_read`, `cache_write`) are SNAPSHOTS of the current context window and
    the most recent message — `input_tokens` is the context size (≈ cache_read +
    cache_write) and `output_tokens` is just the last message's output. They rise
    and fall between renders, so they are deliberately NOT carried forward here;
    all token accounting comes from the JSONL logs instead.

    Returns list of {session_id, ts, date_str, cost_usd, model}."""
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        session_id = entry.get("session_id", "")
        ts = entry.get("_ts", "")
        if not session_id or not ts:
            continue

        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        rows.append({
            "session_id": session_id,
            "ts": dt.isoformat(),
            "date_str": dt.strftime("%Y-%m-%d"),
            "cost_usd": float(entry.get("cost_usd", 0.0) or 0.0),
            "model": entry.get("model", ""),
        })

    rows.sort(key=lambda r: (r["session_id"], r["ts"]))
    return rows


_HOOK_LOCAL_PARAMS = json.dumps(["hook", "local"])


def _hook_files(cctrack_dir: Path) -> list[Path]:
    """The local statusline files, in the order parse_hook_rows reads them: sorted
    statusline-*.jsonl, then the legacy statusline.jsonl."""
    files = sorted(cctrack_dir.glob("statusline-*.jsonl"))
    legacy = cctrack_dir / "statusline.jsonl"
    if legacy.exists():
        files.append(legacy)
    return files


def _hook_file_rows_cached(store, path: Path) -> list[dict] | None:
    """One local hook file's rows, `_hook_rows_from_lines(_read_text_lines(p))`, through
    the cache (host "local", kind "hook"). None = unreadable (never stored). Stored only
    if not racy and unchanged while read, like _parse_log_file_cached."""
    sp = str(path)

    def read():
        lines = _read_text_lines(sp)
        return None if lines is None else _hook_rows_from_lines(lines)
    if store is None or not store.enabled or not _key_text_ok("local", sp):
        return read()
    sig = _file_sig(sp)
    if sig is not None:
        p = store.get("local", "hook", sp, sig, _HOOK_LOCAL_PARAMS)
        if p is not None:
            return p["events"]
    store.counts["misses"] += 1
    rows = read()
    if rows is None:
        return None
    if sig is not None and _file_sig(sp) == sig and not _is_racy(sig, time.time_ns()):
        store.put("local", "hook", sp, sig, {"events": rows, "stats": None}, _HOOK_LOCAL_PARAMS)
    return rows


def parse_hook_rows(cctrack_dir: Path | None = None, cache=None) -> list[dict]:
    """Parse statusline-*.jsonl files from a local hook data directory into
    per-render rows. Also reads legacy statusline.jsonl for migration. With `cache`,
    each file's rows come through the parse cache; they are joined in file order and
    stable-sorted again, which equals the single sort over all lines."""
    if cctrack_dir is None:
        cctrack_dir = CCTRACK_DIR
    if not cctrack_dir.exists():
        return []

    files = _hook_files(cctrack_dir)
    if cache is not None:
        rows: list[dict] = []
        for filepath in files:
            r = _hook_file_rows_cached(cache, filepath)
            if r is not None:
                rows.extend(r)
        rows.sort(key=lambda r: (r["session_id"], r["ts"]))
        return rows

    lines: list[str] = []
    for filepath in files:
        try:
            with open(filepath, "r", errors="replace") as f:
                lines.extend(f.readlines())
        except (OSError, IOError):
            continue

    return _hook_rows_from_lines(lines)


def _hook_streams(session_rows: list[dict], order) -> list[list[dict]]:
    """One session's rows (sorted by `order`) as the sequences to difference:
    all of them as one sequence unless two machines' readings overlap in time,
    in which case each machine's readings are their own sequence."""
    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in session_rows:
        by_source[r.get("_source", "")].append(r)
    if len(by_source) < 2:
        return [session_rows]
    spans = sorted((order(rs[0])[0], order(rs[-1])[0]) for rs in by_source.values())
    if all(spans[i][1] < spans[i + 1][0] for i in range(len(spans) - 1)):
        return [session_rows]                                # a handoff between hosts
    return [rs for _, rs in sorted(by_source.items())]


def compute_hook_deltas(rows: list[dict], baselines: dict | None = None) -> list[dict]:
    """Convert per-render cumulative cost readings into per-(session, date)
    spend, walking every row of each session in timestamp order.

    The hook's `total_cost_usd` is a session-lifetime cumulative, and this only
    ever books the INCREMENTS observed between renders — never the standing
    level of the counter. Two consequences, both load-bearing:

    * First observation establishes a baseline that is NOT counted. If a
      long-running session first appears at $1,304 (its earlier days having
      never been captured — e.g. an unattended run that only rendered its
      statusline partway through), that $1,304 was spent on days we have no
      data for; booking it onto the first-seen day is wrong. We can only
      attribute what we watch accumulate, so we start from the first reading
      and count upward from there. A session that genuinely starts on the
      observed day first renders near $0, so nothing real is lost.

    * A drop rebaselines WITHOUT counting the new level. The counter falls back
      on resume/compaction (or when parallel processes under one session id
      interleave their own cumulatives). The pre-drop dollars were already
      spent — not new — so re-adding the post-drop level would let a day's
      booked spend exceed the counter's own peak, which is impossible for real
      money (observed: a session whose counter never passed $1,086 was booked
      at $2,083). After a drop we resume counting increments from the lower
      level.

    * Machines (`_source`) that were live at the same time are differenced
      separately. A sandbox rsynced to another host and started there too
      (sandy starts Claude with `--continue`) runs one session id in two
      processes, each with its own cumulative. Interleaving them booked every
      switch from the lower counter to the higher one as new spend (observed:
      $11,612 booked for one day with $86 of real usage). Per-machine streams
      whose time ranges don't overlap are still walked as one sequence, so a
      session that moved hosts keeps its running total across the move.
      Callers drop exact copied readings first (reconcile_sources), or the
      copied history would make every pair of streams overlap.

    A rolled-up baseline (see load_session_baselines) seeds `prev` so days
    already folded into a monthly summary aren't recounted — the same "count
    only increments above the baseline" rule. Records with zero spend are
    dropped. Rows lacking `ts` sort by `date_str`, so callers may pass one
    aggregated reading per day.

    Only cost is derived from hook data — see _hook_rows_from_lines for why the
    hook's token fields are unusable."""
    by_session: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_session[r["session_id"]].append(r)

    day_cost: dict[tuple, float] = defaultdict(float)
    day_model: dict[tuple, str] = {}

    def order(r):
        return (r.get("ts") or r["date_str"], r.get("_source", ""))

    for sid, session_rows in by_session.items():
        session_rows.sort(key=order)
        base = (baselines or {}).get(sid)
        for stream in _hook_streams(session_rows, order):
            # None → not yet observed; the first render sets the baseline and is
            # not counted. A rolled-up baseline is a real prior level to count from.
            prev = float(base["cost_usd"]) if base else None
            for r in stream:
                cur = float(r.get("cost_usd", 0.0))
                key = (sid, r["date_str"], r.get("_source", ""))
                if r.get("model"):
                    day_model.setdefault(key, r["model"])
                if prev is None:
                    prev = cur                               # first observation → baseline
                elif cur < prev - max(1e-6, abs(prev) * 0.001):
                    prev = cur                               # counter dropped → rebaseline
                else:
                    day_cost[key] += cur - prev              # normal climb → count increment
                    prev = cur

    result = []
    for (sid, date_str, source), cost in day_cost.items():
        if cost <= 0.0:
            continue
        result.append({
            "date_str": date_str,
            "session_id": sid,
            "cost_usd": cost,
            "model": day_model.get((sid, date_str, source), ""),
            **({"source": source} if source else {}),
        })
    return result


BASELINES_FILENAME = "session-baselines.json"


def load_session_baselines(cctrack_dir: Path | None = None) -> dict:
    """Load per-session cumulative baselines written by rollup_old_hook_files.
    Returns {session_id: {date, cost_usd}}."""
    if cctrack_dir is None:
        cctrack_dir = CCTRACK_DIR
    path = cctrack_dir / BASELINES_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_session_baselines(cctrack_dir: Path, baselines: dict) -> None:
    try:
        (cctrack_dir / BASELINES_FILENAME).write_text(json.dumps(baselines, indent=2) + "\n")
    except OSError:
        pass


def load_monthly_hook_summaries(cctrack_dir: Path | None = None) -> dict:
    """Load pre-aggregated monthly-YYYY-MM.json summary files.
    Returns {date_str: {cost: float, sessions: int}}."""
    if cctrack_dir is None:
        cctrack_dir = CCTRACK_DIR
    if not cctrack_dir.exists():
        return {}

    result: dict[str, dict] = {}
    for path in sorted(cctrack_dir.glob("monthly-*.json")):
        try:
            data = json.loads(path.read_text())
            for date_str, day_data in data.get("days", {}).items():
                result[date_str] = {
                    "cost": float(day_data.get("cost", 0.0)),
                    "sessions": int(day_data.get("sessions", 0)),
                }
        except (json.JSONDecodeError, OSError):
            continue
    return result


def rollup_old_hook_files(cctrack_dir: Path | None = None,
                          retention_days: int = HOOK_RETENTION_DAYS) -> None:
    """Roll up daily JSONL files older than retention_days into monthly summaries,
    then delete them. Uses the same reset-aware per-session walk as
    compute_hook_deltas, so a session spanning multiple days contributes its
    true spend to each day and a restarted cost counter isn't lost."""
    if cctrack_dir is None:
        cctrack_dir = CCTRACK_DIR
    if not cctrack_dir.exists():
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d")

    old_paths: list[tuple[str, Path]] = []
    for path in sorted(cctrack_dir.glob("statusline-*.jsonl")):
        date_str = path.stem.replace("statusline-", "")
        if not date_str or date_str >= cutoff:
            continue
        old_paths.append((date_str, path))

    if not old_paths:
        return

    # Parse every row of the expiring files (not one sample per day).
    rows: list[dict] = []
    empty_paths: list[Path] = []
    for date_str, path in old_paths:
        try:
            with open(path, "r", errors="replace") as f:
                file_rows = _hook_rows_from_lines(f.readlines())
        except (OSError, IOError):
            continue
        if not file_rows:
            empty_paths.append(path)
        rows.extend(file_rows)

    # Baselines carry each session's cumulative across rollup passes, so a
    # session with days on both sides of the cutoff isn't re-counted from zero.
    baselines = load_session_baselines(cctrack_dir)

    day_costs: dict[str, float] = defaultdict(float)
    day_session_ids: dict[str, set] = defaultdict(set)
    for d in compute_hook_deltas(rows, baselines):
        day_costs[d["date_str"]] += d["cost_usd"]
        day_session_ids[d["date_str"]].add(d["session_id"])

    # Record each session's final cumulative so the next pass — and any live
    # statusline data for the same session — resumes from the right point.
    by_session: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_session[r["session_id"]].append(r)
    for sid, session_rows in by_session.items():
        session_rows.sort(key=lambda r: r["ts"])
        last = session_rows[-1]
        baselines[sid] = {"date": last["date_str"], "cost_usd": last["cost_usd"]}

    # Prune baselines for sessions long past the cutoff (no live data can
    # reference them anymore), then persist.
    prune_before = (datetime.now(timezone.utc)
                    - timedelta(days=retention_days + 30)).strftime("%Y-%m-%d")
    baselines = {sid: b for sid, b in baselines.items()
                 if b.get("date", "") >= prune_before}
    _write_session_baselines(cctrack_dir, baselines)

    # Write per-day deltas into the appropriate monthly file and delete dailies.
    by_month: dict[str, dict] = {}
    for date_str in sorted(day_costs.keys()):
        month_str = date_str[:7]
        if month_str not in by_month:
            monthly_path = cctrack_dir / f"monthly-{month_str}.json"
            if monthly_path.exists():
                try:
                    by_month[month_str] = json.loads(monthly_path.read_text())
                except (json.JSONDecodeError, OSError):
                    by_month[month_str] = {"month": month_str, "days": {}}
            else:
                by_month[month_str] = {"month": month_str, "days": {}}
            by_month[month_str].setdefault("days", {})
        by_month[month_str]["days"][date_str] = {
            "cost": day_costs[date_str],
            "sessions": len(day_session_ids[date_str]),
        }

    for month_str, data in by_month.items():
        monthly_path = cctrack_dir / f"monthly-{month_str}.json"
        try:
            monthly_path.write_text(json.dumps(data, indent=2) + "\n")
        except OSError:
            pass

    for _, path in old_paths:
        try:
            path.unlink()
        except OSError:
            pass
    for path in empty_paths:
        try:
            path.unlink()
        except OSError:
            pass


def aggregate_hook_data(events: list[dict], monthly_summaries: dict | None = None) -> dict:
    """Aggregate hook cost deltas by date, merging with pre-aggregated monthly
    data. Live events ADD INTO monthly-seeded buckets — a date can legitimately
    have both (e.g. one host rolled it up while another host still has live
    data). Same-host overlap can't double count because rollup deletes the daily
    files it rolls. Returns {date_str: {cost, sessions, rolled_sessions,
    session_ids, models}}.

    Cost is the only quantity taken from hook data; token counts come from the
    JSONL logs (see _hook_rows_from_lines)."""
    def _empty() -> dict:
        return {"cost": 0.0, "sessions": 0, "rolled_sessions": 0,
                "session_ids": set(), "models": {}}

    by_date: dict[str, dict] = {}

    if monthly_summaries:
        for date_str, data in monthly_summaries.items():
            bucket = _empty()
            bucket["cost"] = data["cost"]
            bucket["rolled_sessions"] = data["sessions"]
            by_date[date_str] = bucket

    for e in events:
        bucket = by_date.setdefault(e["date_str"], _empty())
        bucket["cost"] += e["cost_usd"]
        sid = e.get("session_id", "")
        if sid:
            bucket["session_ids"].add(sid)
        model = e.get("model", "")
        if model:
            bucket["models"][model] = bucket["models"].get(model, 0) + 1

    # Per-day session count: distinct live sessions + rolled-up count.
    for bucket in by_date.values():
        bucket["sessions"] = len(bucket["session_ids"]) + bucket["rolled_sessions"]

    return by_date


# ── Aggregation ─────────────────────────────────────────────────────────

def new_bucket() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "cache_write_1h": 0, "cost": 0.0, "energy_wh": 0.0, "models": {}}


def aggregate(events: list[dict]) -> tuple[dict, dict]:
    """
    Returns:
        daily: {date_str: {input, output, cache_read, cache_write, cache_write_1h,
                           cost, energy_wh, models}}
        monthly: same shape, keyed by month_str
    Cost is priced with the rates in effect on the event's own date, so
    time-limited introductory pricing is applied correctly for past usage.
    """
    daily = defaultdict(new_bucket)
    monthly = defaultdict(new_bucket)
    scale = energy_scale()

    for e in events:
        ts = e["timestamp"]
        if not ts:
            continue

        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        if dt.year >= 1000:
            date_str = f"{dt.year}-{dt.month:02d}-{dt.day:02d}"
            month_str = date_str[:7]
        else:  # glibc's %Y doesn't zero-pad; keep strftime's keys exactly
            date_str = dt.strftime("%Y-%m-%d")
            month_str = dt.strftime("%Y-%m")

        cache_1h = e.get("cache_write_1h", 0)
        cost = calculate_cost(e["model"], e["input"], e["output"],
                              e["cache_read"], e["cache_write"], cache_1h,
                              date_str=date_str,
                              request_input=e.get("_request_input"),
                              service_tier=e.get("service_tier"))
        energy = estimate_energy_wh(e["model"], e["input"], e["output"],
                                    e["cache_read"], e["cache_write"], cache_1h,
                                    scale=scale)
        model = e.get("model", "")

        for bucket in (daily[date_str], monthly[month_str]):
            bucket["input"] += e["input"]
            bucket["output"] += e["output"]
            bucket["cache_read"] += e["cache_read"]
            bucket["cache_write"] += e["cache_write"]
            bucket["cache_write_1h"] += cache_1h
            bucket["cost"] += cost
            bucket["energy_wh"] += energy
            if model:
                bucket["models"][model] = bucket["models"].get(model, 0) + 1

    return dict(daily), dict(monthly)


# ── Output ──────────────────────────────────────────────────────────────

def reconcile_sources(events: list[dict], sources: list[dict], timing=None) -> list[dict]:
    """Deduplicate collected usage and rebuild every report's source totals.

    Matching identities are billed once, with all locations retained. Hook
    histories are merged before differencing so rsynced statusline files do
    not add the same cumulative increments on multiple machines.
    """
    events = dedup_events([events])
    grouped = defaultdict(list)
    for e in events:
        grouped[(e.get("source", "local"), e.get("agent", "claude"))].append(e)
    if timing is not None:
        timing.phase("aggregate")
    for src in sources:
        src["daily"], _ = aggregate(grouped[(src["name"], src.get("agent", "claude"))])
    if timing is not None:
        timing.phase("reconcile")
    hook_sources = [src for src in sources if "_hook_rows" in src]
    if hook_sources:
        rows, baselines = [], {}
        for src in hook_sources:
            rows.extend({**r, "_source": src["name"]} for r in src["_hook_rows"])
            for sid, base in src.get("_hook_baselines", {}).items():
                if sid not in baselines or base.get("date", "") < baselines[sid].get("date", ""):
                    baselines[sid] = base
        # Other hosts can retain raw copies from before a rolled baseline.
        rows = [r for r in rows if not baselines.get(r["session_id"], {}).get("date")
                or r["date_str"] > baselines[r["session_id"]]["date"]]
        rows = _drop_copied_hook_rows(rows)
        deltas = compute_hook_deltas(rows, baselines)
        owners = defaultdict(set)
        for e in events:
            if e.get("agent", "claude") == "claude":
                owners[(e.get("session_id", ""), _event_date(e))].add(e.get("source", "local"))
        for d in deltas:
            hosts = owners[(d["session_id"], d["date_str"])]
            if hosts and d.get("source") not in hosts:
                d["source"] = min(hosts, key=lambda h: (h != "local", h))
        _cap_implausible_hook_costs(deltas, events)
        for src in hook_sources:
            src["deltas"] = [d for d in deltas if d.get("source") == src["name"]]
            src["hook_daily"] = aggregate_hook_data(src["deltas"], src.get("_hook_monthly"))
    return events


def _drop_copied_hook_rows(rows: list[dict]) -> list[dict]:
    """Keep one of each exact reading (session, timestamp, cost). rsync copies a
    sandbox's statusline files byte for byte, so the copied history appears
    under both machines; the local copy (else the first host by name) is kept."""
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["session_id"], r.get("ts") or "", r["date_str"], float(r.get("cost_usd", 0.0)))
        if not k[1]:
            best[(id(r),)] = r                               # no timestamp: never a copy
            continue
        cur = best.get(k)
        rank = (r.get("_source", "") != "local", r.get("_source", ""))
        if cur is None or rank < (cur.get("_source", "") != "local", cur.get("_source", "")):
            best[k] = r
    return list(best.values())


# A session-day whose hook cost exceeds this multiple of its transcript-priced
# cost (and by at least HOOK_CAP_MIN_USD) is priced from the transcripts. The
# two agree within a few percent when both are sound; a gap this large is a
# counter artefact (e.g. two processes writing one session id on one machine),
# not real spend. Days with no transcript at all keep the hook cost.
HOOK_CAP_RATIO = 5.0
HOOK_CAP_MIN_USD = 1.0


def _cap_implausible_hook_costs(deltas: list[dict], events: list[dict]) -> None:
    """Replace implausible hook session-days with the transcript estimate, in
    place; each replaced delta keeps its original cost in `_hook_uncapped` and
    the day's transcript estimate in `_hook_estimate`."""
    est: dict[tuple, float] = defaultdict(float)
    for e in events:
        if e.get("agent", "claude") != "claude":
            continue
        date_str = _event_date(e)
        est[(e.get("session_id", ""), date_str)] += calculate_cost(
            e["model"], e["input"], e["output"], e["cache_read"], e["cache_write"],
            e.get("cache_write_1h", 0), date_str=date_str,
            request_input=e.get("_request_input"), service_tier=e.get("service_tier"))
    hook: dict[tuple, float] = defaultdict(float)
    for d in deltas:
        hook[(d["session_id"], d["date_str"])] += d["cost_usd"]
    for d in deltas:
        k = (d["session_id"], d["date_str"])
        h, j = hook[k], est.get(k, 0.0)
        if j > 0 and h > HOOK_CAP_RATIO * j and h - j >= HOOK_CAP_MIN_USD:
            d["_hook_uncapped"] = d["cost_usd"]
            d["_hook_estimate"] = j
            d["cost_usd"] = d["cost_usd"] * j / h


def capped_hook_days(sources: list[dict]) -> list[dict]:
    """[{date_str, session_id, hook, estimate}] for every hook session-day that
    _cap_implausible_hook_costs replaced, largest first."""
    found: dict[tuple, dict] = {}
    for src in sources:
        for d in src.get("deltas", []):
            if "_hook_uncapped" in d:
                f = found.setdefault((d["date_str"], d["session_id"]), {
                    "date_str": d["date_str"], "session_id": d["session_id"],
                    "hook": 0.0, "estimate": d["_hook_estimate"]})
                f["hook"] += d["_hook_uncapped"]
    return sorted(found.values(), key=lambda f: -f["hook"])


def _print_capped_note(capped: list[dict], limit: int = 5) -> None:
    if not capped:
        return
    print()
    print(f"  ! {len(capped)} session-day(s) had a hook cost more than {HOOK_CAP_RATIO:g}x their "
          f"transcript estimate; priced from the transcripts instead:")
    for f in capped[:limit]:
        print(f"      {f['date_str']}  {f['session_id'][:12]}…  hook ${f['hook']:,.2f} -> "
              f"${f['estimate']:,.2f}")
    if len(capped) > limit:
        print(f"      … and {len(capped) - limit} more (see --explore DATE)")


def mismatch_thresholds() -> tuple[float, float]:
    """(ratio, minimum dollar gap) for flagging a report row whose cost differs
    from its tokens at list price: CCTRACK_MISMATCH_RATIO (default 1.25, i.e.
    25%) and CCTRACK_MISMATCH_MIN_USD (default 5)."""
    def num(name, default, low):
        try:
            return max(low, float(os.environ.get(name, default)))
        except ValueError:
            return default
    return num("CCTRACK_MISMATCH_RATIO", 1.25, 1.0), num("CCTRACK_MISMATCH_MIN_USD", 5.0, 0.0)


def cost_mismatch(shown: float, tokens: float) -> float | None:
    """How far a row's shown cost is from its token-priced cost, as a ratio
    (>1: shown is higher; <1: lower), when that's beyond the thresholds; else
    None. A missing side counts as infinitely far."""
    ratio, min_usd = mismatch_thresholds()
    if abs(shown - tokens) < min_usd:
        return None
    if shown <= 0 or tokens <= 0:
        return float("inf") if shown > tokens else 0.0
    r = shown / tokens
    return r if (r > ratio or r < 1 / ratio) else None


def _print_mismatch_note(flagged: list[dict]) -> None:
    """Footer for rows marked `!` by cost_mismatch."""
    if not flagged:
        return
    ratio, min_usd = mismatch_thresholds()
    print()
    print(f"  ! = the cost shown differs from the row's tokens at list price by more than "
          f"{(ratio - 1) * 100:.0f}% (and ${min_usd:,.0f}):")
    for f in flagged:
        r = f["ratio"]
        how = ("no transcripts" if r == float("inf") else
               "no cost recorded" if r == 0.0 else f"{r:.2f}x")
        hint = f"; see --explore {f['label']}" if f["daily"] else ""
        guess = "; includes unrecognized models, so the token price is a guess" if f["unknown"] else ""
        print(f"      {f['label']:<10}  ${f['shown']:,.2f} shown, ${f['tokens']:,.2f} from tokens "
              f"({how}){hint}{guess}")


def format_tokens(n: int) -> str:
    return f"{n:,}"


def format_model(models: dict) -> str:
    """Format a model counter dict as 'short-name' or 'short-name+N' when
    multiple models appear. Returns empty string for no models.
    Strips the 'claude-' prefix and any trailing YYYYMMDD date suffix."""
    if not models:
        return ""
    ranked = sorted(models.items(), key=lambda kv: (-kv[1], kv[0]))
    primary = ranked[0][0]
    short = primary[len("claude-"):] if primary.startswith("claude-") else primary
    parts = short.split("-")
    if len(parts) > 1 and parts[-1].isdigit() and len(parts[-1]) == 8:
        short = "-".join(parts[:-1])
    if len(ranked) > 1:
        short += f"+{len(ranked) - 1}"
    return short


def total_tokens(b: dict) -> int:
    return (b["input"] + b["output"] + b["cache_read"]
            + b["cache_write"] + b.get("cache_write_1h", 0))


def _compute_accuracy_factor_sources(sources: list[dict]) -> tuple[float, bool]:
    """Compute ratio of hook cost to JSONL cost over (source, date) pairs where
    both exist. Dates strictly AFTER each source's install date only — the
    install day itself has partial hook coverage and would bias the factor low.
    Comparing per source keeps a hook-less machine's JSONL out of the
    denominator. Returns (factor, is_measured); falls back to
    DEFAULT_ACCURACY_FACTOR when no overlapping data exists."""
    jsonl_total = 0.0
    hook_total = 0.0
    for src in sources:
        install = src.get("install_date")
        if not install:
            continue
        for date_str, d in src["daily"].items():
            if date_str <= install:
                continue
            hook_cost = src["hook_daily"].get(date_str, {}).get("cost", 0.0)
            if d["cost"] > 0 and hook_cost > 0:
                jsonl_total += d["cost"]
                hook_total += hook_cost

    if jsonl_total > 0 and hook_total > 0:
        return hook_total / jsonl_total, True
    return DEFAULT_ACCURACY_FACTOR, False


def _compute_accuracy_factor(daily: dict, hook_daily: dict, hook_install_date: str | None) -> tuple[float, bool]:
    """Single-source convenience wrapper around _compute_accuracy_factor_sources."""
    return _compute_accuracy_factor_sources([
        {"daily": daily, "hook_daily": hook_daily, "install_date": hook_install_date}
    ])


def print_report(daily: dict, monthly: dict, days: int | None = None,
                 hook_daily: dict | None = None,
                 hook_install_date: str | None = None,
                 sources: list[dict] | None = None,
                 show_energy: bool = False):
    """Print the report. `sources` is a list of {name, daily, hook_daily,
    install_date} — one per machine — enabling per-source cost composition
    (hook cost where covered, adjusted JSONL where not) so a hook-less machine
    isn't dropped on days another machine has hook data. When omitted, the
    legacy (daily, hook_daily, hook_install_date) args form a single source."""
    if hook_daily is None:
        hook_daily = {}
    if sources is None:
        sources = [{"name": "all", "daily": daily, "hook_daily": hook_daily,
                    "install_date": hook_install_date}]

    if not daily and not monthly and not any(src["hook_daily"] for src in sources):
        print("cctrack — no log data found")
        print()
        print("Looked in:")
        for d in default_dirs():
            print(f"  {d}")
        print(f"  {codex_home() / 'sessions'}  (Codex)")
        return

    hooks_exist = any(src.get("install_date") or src["hook_daily"] for src in sources)

    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_month_name = now.strftime("%B %Y")

    all_dates: set[str] = set()
    for src in sources:
        all_dates |= set(src["daily"].keys()) | set(src["hook_daily"].keys())
    all_dates |= set(daily.keys())
    all_months_set: set[str] = set()
    for d in all_dates:
        all_months_set.add(d[:7])
    for m in monthly:
        all_months_set.add(m)

    sorted_days = sorted(all_dates, reverse=True)
    sorted_months = sorted(all_months_set, reverse=True)

    accuracy_factor, factor_is_measured = _compute_accuracy_factor_sources(sources)

    def _source_date_cost(src: dict, date_str: str) -> tuple[float, str | None]:
        """Returns (cost, kind) for one source; kind is 'auth', 'est', or None.
        Hook cost is authoritative for dates after that source's hook install;
        the install day itself takes max(hook, adjusted JSONL) since the hook
        only captured post-install sessions; JSONL-only days are adjusted by
        the accuracy factor whenever any hook data exists anywhere."""
        hook_cost = src["hook_daily"].get(date_str, {}).get("cost", 0.0)
        jsonl_cost = src["daily"].get(date_str, {}).get("cost", 0.0)
        install = src.get("install_date")

        if src.get("exact"):
            # Priced directly from per-response usage the agent logs itself
            # (Codex). There is no hook to defer to, and the Claude-derived
            # accuracy factor says nothing about these logs.
            return (jsonl_cost, "auth") if jsonl_cost > 0 else (0.0, None)

        if install and date_str == install:
            adjusted = jsonl_cost * accuracy_factor if jsonl_cost > 0 else 0.0
            cost = max(hook_cost, adjusted)
            return (cost, "est") if cost > 0 else (0.0, None)
        if install and date_str > install and hook_cost > 0:
            return hook_cost, "auth"
        if jsonl_cost > 0:
            if hooks_exist:
                return jsonl_cost * accuracy_factor, "est"
            return jsonl_cost, "auth"
        if hook_cost > 0:
            return hook_cost, "auth"
        return 0.0, None

    def _date_cost(date_str: str) -> tuple[float, bool]:
        """Sum of per-source costs; authoritative only if every contributing
        source component is hook-authoritative."""
        total = 0.0
        kinds = []
        for src in sources:
            cost, kind = _source_date_cost(src, date_str)
            total += cost
            if kind:
                kinds.append(kind)
        is_auth = bool(kinds) and all(k == "auth" for k in kinds)
        return total, is_auth

    def _date_tokens(date_str: str) -> tuple[int, int]:
        """Input/output per date, summed across sources. Always JSONL-derived:
        the hook's token fields are context-window snapshots, not cumulative
        usage (see _hook_rows_from_lines)."""
        ti = to = 0
        for src in sources:
            d = src["daily"].get(date_str)
            if d:
                ti += d["input"]
                to += d["output"]
        return ti, to

    def _date_sessions(date_str: str) -> int:
        return sum(src["hook_daily"].get(date_str, {}).get("sessions", 0) for src in sources)

    def _date_hook_models(date_str: str) -> dict:
        merged: dict = {}
        for src in sources:
            for mname, cnt in src["hook_daily"].get(date_str, {}).get("models", {}).items():
                merged[mname] = merged.get(mname, 0) + cnt
        return merged

    def _date_energy_wh(date_str: str) -> float:
        """Estimated energy per date, summed across sources. JSONL-derived —
        it needs the per-model token split, which only the logs carry."""
        return sum(src["daily"].get(date_str, {}).get("energy_wh", 0.0)
                   for src in sources)

    saw_unknown_model = False

    def _model_str(models: dict) -> str:
        nonlocal saw_unknown_model
        s = format_model(models)
        if any(not is_known_model(mn) for mn in models):
            saw_unknown_model = True
            s += "?"
        return s

    # Header
    agents_present = {src.get("agent", "claude") for src in sources
                      if src["daily"] or src["hook_daily"]}
    if agents_present == {"codex"}:
        title = "cctrack — Codex Cost Report"
    elif "codex" in agents_present:
        title = "cctrack — Claude Code + Codex Cost Report"
    else:
        title = "cctrack — Claude Code Cost Report"
    print(title)
    print("═" * len(title))
    print("All dates and day boundaries are UTC.")
    print()

    # Current month-to-date
    current_days_all = [d for d in sorted_days if d.startswith(current_month)]
    if current_days_all:
        days_active = len(current_days_all)
        day_of_month = now.day

        mtd_cost = 0.0
        auth_days = 0
        est_days = 0
        for d in current_days_all:
            cost, is_auth = _date_cost(d)
            mtd_cost += cost
            if is_auth:
                auth_days += 1
            else:
                est_days += 1

        m = monthly.get(current_month)

        print(f"{current_month_name} — month to date (day {day_of_month}, {days_active} active)")
        print("───────────────────────────────────────")
        if m:
            input_t = output_t = 0
            for d in current_days_all:
                ti, to = _date_tokens(d)
                input_t += ti
                output_t += to
            print(f"  Input tokens:   {format_tokens(input_t):>15}")
            print(f"  Output tokens:  {format_tokens(output_t):>15}")
            cache_w_total = m['cache_write'] + m.get('cache_write_1h', 0)
            if m['cache_read'] or cache_w_total:
                print(f"  Cache read:     {format_tokens(m['cache_read']):>15}")
                print(f"  Cache write:    {format_tokens(cache_w_total):>15}")
            total = input_t + output_t + m['cache_read'] + cache_w_total
            print(f"  Total tokens:   {format_tokens(total):>15}")
            mtd_model = _model_str(m.get("models", {}))
            if mtd_model:
                print(f"  Model:          {mtd_model:>15}")

        # Distinct sessions for the month: union of live session ids (a session
        # spanning several days counts once) + rolled-up per-day counts.
        mtd_ids: set = set()
        mtd_counted = 0
        for d in current_days_all:
            for src in sources:
                h = src["hook_daily"].get(d)
                if not h:
                    continue
                ids = h.get("session_ids")
                if ids:
                    mtd_ids |= ids
                    mtd_counted += h.get("rolled_sessions", 0)
                else:
                    mtd_counted += h.get("sessions", 0)
        mtd_sessions = len(mtd_ids) + mtd_counted
        if mtd_sessions:
            print(f"  Sessions:       {mtd_sessions:>15}")

        if hooks_exist and est_days > 0:
            factor_source = "measured" if factor_is_measured else "default"
            cost_note = f" ({accuracy_factor:.2f}x adjustment, {factor_source})"
            if auth_days > 0:
                cost_note = f" ({auth_days}d authoritative, {est_days}d adjusted {accuracy_factor:.2f}x)"
            print(f"  Total cost:     ${mtd_cost:.2f}{cost_note}")
        elif hooks_exist and auth_days > 0:
            print(f"  Total cost:     ${mtd_cost:.2f} (authoritative)")
        else:
            print(f"  Total cost:     ${mtd_cost:.2f}")

        # Per-agent split, shown once more than one coding agent has spend.
        agent_cost: dict[str, float] = defaultdict(float)
        for src in sources:
            for d in current_days_all:
                agent_cost[src.get("agent", "claude")] += _source_date_cost(src, d)[0]
        if sum(1 for v in agent_cost.values() if v > 0) > 1:
            parts = [f"{AGENT_LABELS.get(a, a)} ${c:,.2f}"
                     for a, c in sorted(agent_cost.items(), key=lambda kv: -kv[1]) if c > 0]
            print(f"  By agent:       {' · '.join(parts)}")

        if days_active > 0:
            avg_daily = mtd_cost / days_active
            projected = avg_daily * 30
            print(f"  Avg/day:        ${avg_daily:.2f}")
            print(f"  Projected/mo:   ${projected:.2f} (based on {days_active}-day avg)")

        if show_energy:
            mtd_wh = sum(_date_energy_wh(d) for d in current_days_all)
            print(f"  Energy (est):   {format_energy(mtd_wh):>15}")
            household_hours = mtd_wh / 1000 / (US_HOUSEHOLD_KWH_PER_DAY / 24)
            if household_hours >= 24:
                equiv = f"{household_hours / 24:,.1f} days"
            else:
                equiv = f"{household_hours:,.1f} hours"
            print(f"  {'':<15} ≈ {equiv} of a typical US household's use")

        print()

    # Daily breakdown (current month first, then previous)
    current_month_days = [d for d in sorted_days if d.startswith(current_month)]
    prev_month_days = [d for d in sorted_days if not d.startswith(current_month)]

    if current_month_days:
        energy_head = f" {'Energy':>10}" if show_energy else ""
        energy_rule = f" {'─'*10}" if show_energy else ""
        print("Daily breakdown (UTC):")
        if hooks_exist:
            print(f"  {'Date':<12} {'Input':>11} {'Output':>11} {'Cache R':>11} {'Cache W':>11} {'Sess':>5} {'Model':<12} {'Cost':>9}{energy_head} {'':>2}")
            print(f"  {'─'*12} {'─'*11} {'─'*11} {'─'*11} {'─'*11} {'─'*5} {'─'*12} {'─'*9}{energy_rule} {'─'*2}")
        else:
            print(f"  {'Date':<12} {'Input':>11} {'Output':>11} {'Cache R':>11} {'Cache W':>11} {'Model':<12} {'Cost':>9}{energy_head}")
            print(f"  {'─'*12} {'─'*11} {'─'*11} {'─'*11} {'─'*11} {'─'*12} {'─'*9}{energy_rule}")
        display_days = current_month_days
        if days is not None:
            display_days = display_days[:days]
        flagged: list[dict] = []
        for date_str in display_days:
            cost, is_auth = _date_cost(date_str)
            d = daily.get(date_str)
            input_t, output_t = _date_tokens(date_str)
            cache_r = d["cache_read"] if d else 0
            cache_w = (d["cache_write"] + d.get("cache_write_1h", 0)) if d else 0
            models = (d.get("models", {}) if d else {}) or _date_hook_models(date_str)
            model_str = _model_str(models)
            energy_col = f" {format_energy(_date_energy_wh(date_str)):>10}" if show_energy else ""
            mismatch = cost_mismatch(cost, d["cost"] if d else 0.0)
            if mismatch is not None:
                flagged.append({"label": date_str, "daily": True, "shown": cost,
                                "tokens": d["cost"] if d else 0.0, "ratio": mismatch,
                                "unknown": any(not is_known_model(m) for m in (d or {}).get("models", {}))})
            if hooks_exist:
                marker = ("" if is_auth else "~") + ("!" if mismatch is not None else "")
                sessions = _date_sessions(date_str)
                sess_str = str(sessions) if sessions else "-"
                print(f"  {date_str:<12} {format_tokens(input_t):>11} {format_tokens(output_t):>11} {format_tokens(cache_r):>11} {format_tokens(cache_w):>11} {sess_str:>5} {model_str:<12} ${cost:>7.2f}{energy_col} {marker:>2}")
            else:
                print(f"  {date_str:<12} {format_tokens(input_t):>11} {format_tokens(output_t):>11} {format_tokens(cache_r):>11} {format_tokens(cache_w):>11} {model_str:<12} ${cost:>7.2f}{energy_col}")

    if hooks_exist and current_month_days:
        has_estimated = any(not _date_cost(d)[1] for d in current_month_days)
        if has_estimated:
            if factor_is_measured:
                print(f"  ~ = estimated (JSONL-derived, {accuracy_factor:.1f}x undercount measured from hook data)")
            else:
                print("  ~ = estimated (JSONL-derived; no hook overlap yet to measure accuracy)")

    # Previous months summary
    month_flagged: list[dict] = []
    prev_months = [m for m in sorted_months if m != current_month]
    if prev_months:
        print()
        prev_energy_head = f" {'Energy':>10}" if show_energy else ""
        prev_energy_rule = f" {'─'*10}" if show_energy else ""
        print("Previous months:")
        print(f"  {'Month':<12} {'Input':>11} {'Output':>11} {'Cache R':>11} {'Cache W':>11} {'Model':<12} {'Cost':>9}{prev_energy_head}")
        print(f"  {'─'*12} {'─'*11} {'─'*11} {'─'*11} {'─'*11} {'─'*12} {'─'*9}{prev_energy_rule}")
        for month_str in prev_months:
            m = monthly.get(month_str)
            month_days_list = [d for d in prev_month_days if d.startswith(month_str)]
            days_str = f" ({len(month_days_list)}d)" if month_days_list else ""

            month_cost = 0.0
            month_has_hook = False
            month_has_est = False
            for d in month_days_list:
                cost, is_auth = _date_cost(d)
                month_cost += cost
                if is_auth:
                    month_has_hook = True
                else:
                    month_has_est = True

            if not month_days_list and m:
                month_cost = m["cost"]
                if hooks_exist:
                    month_cost *= accuracy_factor
                month_has_est = True

            input_t = m["input"] if m else 0
            output_t = m["output"] if m else 0
            cache_r = m["cache_read"] if m else 0
            cache_w = (m["cache_write"] + m.get("cache_write_1h", 0)) if m else 0
            model_str = _model_str(m.get("models", {})) if m else ""

            cost_str = f"${month_cost:>7.2f}"
            month_energy = f" {format_energy(m.get('energy_wh', 0.0) if m else 0.0):>10}" if show_energy else ""
            if hooks_exist and month_has_est and not month_has_hook:
                cost_str += "  ~"
            mismatch = cost_mismatch(month_cost, m["cost"] if m else 0.0)
            if mismatch is not None:
                month_flagged.append({"label": month_str, "daily": False, "shown": month_cost,
                                      "tokens": m["cost"] if m else 0.0, "ratio": mismatch,
                                      "unknown": any(not is_known_model(x)
                                                     for x in (m or {}).get("models", {}))})
                cost_str += "  !" if not cost_str.endswith("~") else "!"
            print(f"  {month_str + days_str:<12} {format_tokens(input_t):>11} {format_tokens(output_t):>11} {format_tokens(cache_r):>11} {format_tokens(cache_w):>11} {model_str:<12} {cost_str}{month_energy}")

    _print_capped_note(capped_hook_days(sources))
    _print_mismatch_note((flagged if current_month_days else []) + month_flagged)

    if saw_unknown_model:
        print()
        print("  ? = unrecognized model — priced at default Sonnet rates; update cctrack's rate card")

    if show_energy:
        print()
        print("  Energy is a rough ESTIMATE, not a measurement — Anthropic does not publish")
        print("  per-token energy figures. Modelled from logged tokens using published")
        print("  per-prompt figures (~0.24-0.34 Wh/query) apportioned across token classes,")
        print("  scaled by model tier. Treat as ±1 order of magnitude. Includes datacenter")
        print("  overhead (cooling, host); excludes training and your own device.")
        scale = energy_scale()
        if scale != 1.0:
            print(f"  CCTRACK_ENERGY_SCALE={scale:g} applied.")

    print()


# ── Explore a single day ────────────────────────────────────────────────

def _event_cost_breakdown(e: dict, date_str: str) -> dict:
    """Per-token-class dollar cost of one event, at the rates in effect on its
    own date. Keys: input, output, cache_read, cache_write, cache_write_1h."""
    r = get_rates(e.get("model", ""), date_str, e.get("_request_input"),
                  e.get("service_tier"))
    return {
        "input": e.get("input", 0) / 1_000_000 * r["input"],
        "output": e.get("output", 0) / 1_000_000 * r["output"],
        "cache_read": e.get("cache_read", 0) / 1_000_000 * r["cache_read"],
        "cache_write": e.get("cache_write", 0) / 1_000_000 * r["cache_write"],
        "cache_write_1h": e.get("cache_write_1h", 0) / 1_000_000 * r["cache_write_1h"],
    }


def _event_date(e: dict) -> str:
    dt = _event_dt(e)
    return dt.strftime("%Y-%m-%d") if dt else ""


def _pct(part: float, whole: float) -> str:
    return f"{(100 * part / whole):.0f}%" if whole > 0 else "0%"


def _bar(part: float, whole: float, width: int = 20) -> str:
    filled = int(round(width * part / whole)) if whole > 0 else 0
    return "█" * filled + "·" * (width - filled)


def print_explore(events: list[dict], date_str: str,
                  hook_by_source: dict | None = None,
                  hook_by_session: dict | None = None,
                  show_energy: bool = False,
                  project_filter: str | None = None,
                  capped: list[dict] | None = None):
    """Drill into a single day: where the money went (by token class), and
    which models, projects, sessions and machines drove it. Costs are
    JSONL-derived estimates; where the statusline hook covered a source its
    authoritative cost is shown alongside so an inflated estimate is obvious.

    With `project_filter`, the view is restricted to sessions whose project
    (the basename of each event's working directory) matches, and a detail
    block prints the full cwd, git branch and source file per session so an
    unfamiliar project label can be traced to a real location on disk."""
    hook_by_source = hook_by_source or {}
    hook_by_session = hook_by_session or {}
    capped = [f for f in (capped or []) if f["date_str"] == date_str]
    capped_sids = {f["session_id"] for f in capped}

    day_events = [e for e in events if _event_date(e) == date_str]

    title = "cctrack — Explore " + date_str
    if project_filter:
        title += f"  ·  project '{project_filter}'"
    print(title)
    print("══════════════════════════════════")
    print()

    def _matches_project(e: dict) -> bool:
        if not project_filter:
            return True
        proj = e.get("project", "")
        cwd = e.get("cwd", "")
        return (proj == project_filter or proj.endswith(project_filter)
                or project_filter in cwd)

    if project_filter:
        day_events = [e for e in day_events if _matches_project(e)]

    if not day_events and (project_filter or not hook_by_source):
        scope = f" for project '{project_filter}'" if project_filter else ""
        print(f"No usage found for {date_str}{scope}.")
        print()
        return

    # ── Totals: JSONL estimate vs authoritative hook, per source ──
    jsonl_by_source: dict[str, float] = defaultdict(float)
    # The statusline hook only covers Claude Code, so hook comparisons use the
    # Claude-only slice of the JSONL estimate.
    claude_by_source: dict[str, float] = defaultdict(float)
    by_agent: dict[str, float] = defaultdict(float)
    classes = {"input": 0.0, "output": 0.0, "cache_read": 0.0,
               "cache_write": 0.0, "cache_write_1h": 0.0}
    tokens = {"input": 0, "output": 0, "cache_read": 0,
              "cache_write": 0, "cache_write_1h": 0}
    by_model: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "energy": 0.0, "events": 0})
    by_project: dict[str, float] = defaultdict(float)
    by_session: dict[tuple, dict] = defaultdict(
        lambda: {"cost": 0.0, "models": set(), "project": "", "source": "",
                 "cwds": set(), "branches": set(), "files": set(), "events": 0})
    energy_total = 0.0

    for e in day_events:
        bd = _event_cost_breakdown(e, date_str)
        cost = sum(bd.values())
        src = e.get("source", "local")
        jsonl_by_source[src] += cost
        agent = e.get("agent", "claude")
        by_agent[agent] += cost
        if agent == "claude":
            claude_by_source[src] += cost
        for k in classes:
            classes[k] += bd[k]
            tokens[k] += e.get(k, 0)
        wh = estimate_energy_wh(e.get("model", ""), e.get("input", 0), e.get("output", 0),
                                e.get("cache_read", 0), e.get("cache_write", 0),
                                e.get("cache_write_1h", 0))
        energy_total += wh
        model = e.get("model", "") or "(unknown)"
        by_model[model]["cost"] += cost
        by_model[model]["energy"] += wh
        by_model[model]["events"] += 1
        by_project[e.get("project", "(unknown)")] += cost
        skey = (src, e.get("session_id", ""))
        s = by_session[skey]
        s["cost"] += cost
        s["events"] += 1
        if e.get("model"):
            s["models"].add(e["model"])
        s["project"] = e.get("project", "(unknown)")
        s["source"] = src
        if e.get("cwd"):
            s["cwds"].add(e["cwd"])
        if e.get("git_branch"):
            s["branches"].add(e["git_branch"])
        if e.get("_source_file"):
            s["files"].add(e["_source_file"])

    jsonl_total = sum(jsonl_by_source.values())

    claude_total = sum(claude_by_source.values())
    print(f"Total (JSONL-estimated):  ${jsonl_total:,.2f}")
    if hook_by_source:
        hook_total = sum(hook_by_source.values())
        mixed = claude_total < jsonl_total - 0.005
        label = "Claude Code (hook-authoritative)" if mixed else "Total (hook-authoritative)"
        print(f"{label}: ${hook_total:,.2f}")
        if claude_total > 0 and hook_total > 0:
            ratio = claude_total / hook_total
            if ratio > 1.15 or ratio < 0.87:
                print(f"  ⚠ Claude JSONL estimate is {ratio:.2f}× the authoritative hook cost —")
                print(f"    the breakdown below is JSONL-derived and may be distorted.")
    if show_energy:
        print(f"Energy (est):             {format_energy(energy_total)}")
    print()

    # ── Where the money went: dollar decomposition by token class ──
    print("Where it went (by token class):")
    order = [("output", "Output"), ("cache_write_1h", "Cache write 1h"),
             ("cache_write", "Cache write 5m"), ("input", "Input"),
             ("cache_read", "Cache read")]
    for key, label in sorted(order, key=lambda kv: -classes[kv[0]]):
        amt = classes[key]
        if amt <= 0:
            continue
        print(f"  {label:<15} ${amt:>10,.2f}  {_pct(amt, jsonl_total):>4}  "
              f"{_bar(amt, jsonl_total)}  {format_tokens(tokens[key])} tok")
    print()

    # ── By agent ──
    if len([a for a, c in by_agent.items() if c > 0]) > 1:
        print("By agent:")
        for agent, cost in sorted(by_agent.items(), key=lambda kv: -kv[1]):
            print(f"  {AGENT_LABELS.get(agent, agent):<22} ${cost:>10,.2f}  {_pct(cost, jsonl_total):>4}")
        print()

    # ── By model ──
    print("By model:")
    saw_unknown = False
    for model, d in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"]):
        mark = "" if is_known_model(model) else " ?"
        if mark:
            saw_unknown = True
        short = model[len("claude-"):] if model.startswith("claude-") else model
        energy_col = f"  {format_energy(d['energy']):>10}" if show_energy else ""
        print(f"  {short + mark:<22} ${d['cost']:>10,.2f}  {_pct(d['cost'], jsonl_total):>4}  "
              f"{d['events']:>5} req{energy_col}")
    print()

    # ── By project ──
    if len(by_project) > 1 or (by_project and "(unknown)" not in by_project):
        print("By project:")
        for proj, cost in sorted(by_project.items(), key=lambda kv: -kv[1]):
            print(f"  {proj:<28} ${cost:>10,.2f}  {_pct(cost, jsonl_total):>4}")
        print()

    # ── By source (machine) ──
    if len(jsonl_by_source) > 1:
        print("By machine:")
        for src in sorted(jsonl_by_source, key=lambda s: -jsonl_by_source[s]):
            j = jsonl_by_source[src]
            h = hook_by_source.get(src)
            if h is not None:
                hook_note = f"   (Claude hook: ${h:,.2f})"
            elif claude_by_source.get(src, 0) > 0:
                hook_note = "   (no hook — estimate)"
            else:
                hook_note = ""
            print(f"  {src:<16} ${j:>10,.2f}  {_pct(j, jsonl_total):>4}{hook_note}")
        print()

    # ── Top sessions ──
    ranked = sorted(by_session.items(), key=lambda kv: -kv[1]["cost"])
    if ranked:
        print(f"Top sessions ({min(len(ranked), 10)} of {len(ranked)}):")
        print(f"  {'Session':<14} {'Project':<20} {'Model':<14} {'JSONL $':>10} {'Hook $':>10}")
        print(f"  {'─'*14} {'─'*20} {'─'*14} {'─'*10} {'─'*10}")
        for (src, sid), d in ranked[:10]:
            hook_cost = hook_by_session.get((src, sid))
            hook_str = f"${hook_cost:>8,.2f}" if hook_cost is not None else "     —"
            if sid in capped_sids and hook_cost is not None:
                hook_str += "!"
            models = "+".join(sorted(
                m[len("claude-"):] if m.startswith("claude-") else m for m in d["models"]
            )) or "(unknown)"
            sid_short = (sid[:12] + "…") if len(sid) > 13 else (sid or "(none)")
            print(f"  {sid_short:<14} {d['project'][:20]:<20} {models[:14]:<14} "
                  f"${d['cost']:>8,.2f} {hook_str:>10}")
        print()

    if capped:
        _print_capped_note(capped, limit=len(capped))
        print("  (! in the Hook $ column marks these sessions)")
        print()

    if saw_unknown:
        print("  ? = unrecognized model — priced at default Sonnet rates; update the rate card")
        print()

    # ── Session sources (where the events physically live) ──
    # Always show for a filtered view; otherwise only when a project's label is
    # ambiguous (its cwd is a container path, or one basename spans several dirs).
    def _needs_tracing() -> bool:
        cwds_by_project: dict[str, set] = defaultdict(set)
        for d in by_session.values():
            cwds_by_project[d["project"]].update(d["cwds"])
        for cwds in cwds_by_project.values():
            if len(cwds) > 1:
                return True
            for c in cwds:
                # container/automation paths rarely match a host project dir
                if c.startswith(("/workspace", "/root", "/tmp", "/mnt")):
                    return True
        return False

    if ranked and (project_filter or _needs_tracing()):
        print("Session sources (trace a project label to its real location):")
        for (src, sid), d in ranked:
            if not project_filter and not any(
                c.startswith(("/workspace", "/root", "/tmp", "/mnt")) for c in d["cwds"]
            ) and len(d["cwds"]) <= 1:
                continue
            sid_short = (sid[:12] + "…") if len(sid) > 13 else (sid or "(none)")
            branch = ", ".join(sorted(d["branches"])) or "-"
            print(f"  {sid_short}  ${d['cost']:,.2f}  ({d['events']} ev, branch {branch})")
            for cwd in sorted(d["cwds"]) or ["(no cwd recorded)"]:
                print(f"     cwd:  {cwd}")
            for fpath in sorted(d["files"]):
                print(f"     file: {fpath}")
        print()

    print("Costs are JSONL-derived estimates. Where a machine runs the statusline")
    print("hook, its authoritative cost is shown for comparison; large gaps mean the")
    print("estimate (not the hook) is off.")
    if not project_filter and ranked and _needs_tracing():
        print("Re-run with --explore <date> --project <name> to trace one project.")
    print()


# ── Detailed reports: sessions, time buckets, coverage, JSON/CSV ────────
#
# For per-session / per-hour cost attribution (e.g. a study of compute cost per
# role). Everything here is UTC, contains ids, paths, counts and money only —
# never prompt/response content — and can hash paths with --redact.

SCHEMA_VERSION = 1
# Date the rate card was last verified against both providers' price pages.
RATE_CARD_VERSION = "2026-11-24"
RATE_CARD_SOURCES = {
    "anthropic": "https://platform.claude.com/docs/en/about-claude/pricing",
    "openai": "https://developers.openai.com/api/docs/pricing",
}
TOKEN_CLASSES = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
_SANDBOX_PATH_RE = re.compile(r"/sandboxes/([^/]+)/(?:claude|codex)/")


def cctrack_version() -> str:
    try:
        from importlib.metadata import version
        return version("cctrack")
    except Exception:
        return "unknown"


def parse_utc(value: str, end: bool = False) -> datetime:
    """Parse a --since/--until value as UTC. Accepts ISO-8601 with a zone
    (converted to UTC), a naive date-time (taken as UTC), or a bare
    YYYY-MM-DD (start of that UTC day; with `end`, the start of the NEXT day,
    so `--until 2026-10-12` includes all of the 12th)."""
    v = value.strip()
    if len(v) == 10:
        dt = datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt + timedelta(days=1) if end else dt
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    dt = dt.astimezone(timezone.utc)
    s = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        s += f".{dt.microsecond // 1000:03d}"
    return s + "Z"


def _event_dt(e: dict) -> datetime | None:
    try:
        dt = datetime.fromisoformat(e.get("timestamp", "").replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def filter_window(events: list[dict], since: datetime | None,
                  until: datetime | None) -> list[dict]:
    """Events with since <= timestamp < until (UTC). Untimestamped events are
    dropped whenever a bound is set."""
    if since is None and until is None:
        return list(events)
    out = []
    for e in events:
        dt = _event_dt(e)
        if dt is None:
            continue
        if since is not None and dt < since:
            continue
        if until is not None and dt >= until:
            continue
        out.append(e)
    return out


def sandbox_of(path: str, roots: list[Path] | None = None) -> str:
    """Sandy sandbox name a log file lives in, or "" for the host's own logs.
    Remote paths are "host:/path"."""
    if not path:
        return ""
    local = path.split(":", 1)[1] if ":" in path and not path.startswith("/") else path
    for r in roots or []:
        rs = str(r).rstrip("/") + "/"
        if local.startswith(rs):
            return Path(rs).name
    m = _SANDBOX_PATH_RE.search(local)
    return m.group(1) if m else ""


def make_redactor(enabled: bool):
    """Stable hash for paths and names. Set CCTRACK_REDACT_SALT so the hashes
    can't be reversed by hashing guessed repo names."""
    if not enabled:
        return lambda v: v
    import hashlib
    salt = os.environ.get("CCTRACK_REDACT_SALT", "")

    def redact(v):
        if not v:
            return v
        return "h:" + hashlib.sha256((salt + str(v)).encode()).hexdigest()[:12]
    return redact


def parse_billing_mode(spec: str | None) -> dict:
    """'api', 'subscription', or per agent 'claude=subscription,codex=api'.
    Logs can't tell; default 'unknown'."""
    modes = {"claude": "unknown", "codex": "unknown"}
    spec = (spec or os.environ.get("CCTRACK_BILLING_MODE") or "").strip()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "=" in part:
            agent, mode = (x.strip() for x in part.split("=", 1))
            modes[agent] = mode
        else:
            modes = {a: part for a in modes}
    return modes


def _event_classes(e: dict) -> dict:
    return {"input": e.get("input", 0), "output": e.get("output", 0),
            "cache_read": e.get("cache_read", 0),
            "cache_write_5m": e.get("cache_write", 0),
            "cache_write_1h": e.get("cache_write_1h", 0)}


def _event_class_costs(e: dict) -> dict:
    c = _event_cost_breakdown(e, _event_date(e))
    return {"input": c["input"], "output": c["output"], "cache_read": c["cache_read"],
            "cache_write_5m": c["cache_write"], "cache_write_1h": c["cache_write_1h"]}


def _session_key(e: dict, roots) -> tuple:
    sub = ""
    if e.get("agent") == "claude" and e.get("is_sidechain"):
        sub = e.get("agent_id") or "sidechain"
    return (e.get("agent", "claude"), e.get("source", "local"),
            sandbox_of(e.get("_source_file", ""), roots), e.get("session_id", ""), sub)


def build_session_rows(events: list[dict], compactions: dict | None = None,
                       hook_by_session: dict | None = None,
                       billing: dict | None = None, roots=None,
                       redact=lambda v: v) -> list[dict]:
    """One row per session per location; Claude subagent (sidechain) turns get
    their own row linked by parent_session_id. Codex subagent threads are
    already separate sessions and carry their parent's id.

    compactions: {(session_id, subagent_id): count}.
    hook_by_session: {(host, session_id): hook cost} — authoritative Claude
    spend from the statusline hook, day-granular, for the main session row."""
    compactions = compactions or {}
    hook_by_session = hook_by_session or {}
    billing = billing or parse_billing_mode(None)
    acc: dict[tuple, dict] = {}
    for e in events:
        dt = _event_dt(e)
        key = _session_key(e, roots)
        r = acc.get(key)
        if r is None:
            r = acc[key] = {"tokens": dict.fromkeys(TOKEN_CLASSES, 0),
                            "cost_by_class": dict.fromkeys(TOKEN_CLASSES, 0.0),
                            "requests": 0, "models": {}, "first": None, "last": None,
                            "cr_sum": 0, "cr_max": 0, "ctx_sum": 0, "ctx_max": 0,
                            "cwds": {}, "branch": "", "files": set(), "fallback": set(),
                            "parent": e.get("parent_session_id", ""),
                            "locations": set(), "service_tiers": {}}
        for k, v in _event_classes(e).items():
            r["tokens"][k] += v
        for k, v in _event_class_costs(e).items():
            r["cost_by_class"][k] += v
        r["requests"] += 1
        tier = e.get("service_tier") or ("fast" if e.get("model", "").endswith("-fast") else "unknown")
        r["service_tiers"][tier] = r["service_tiers"].get(tier, 0) + 1
        for loc in [*e.get("_locations", []), _event_location(e)]:
            r["locations"].add(tuple(sorted(loc.items())))
            if loc["path"]:
                r["files"].add(loc["path"])
        m = e.get("model", "")
        r["models"][m] = r["models"].get(m, 0) + 1
        if m and not is_known_model(m):
            r["fallback"].add(m)
        if dt is not None:
            r["first"] = dt if r["first"] is None or dt < r["first"] else r["first"]
            r["last"] = dt if r["last"] is None or dt > r["last"] else r["last"]
        cr = e.get("cache_read", 0)
        ctx = e.get("_request_input") or (e.get("input", 0) + cr + e.get("cache_write", 0)
                                          + e.get("cache_write_1h", 0))
        r["cr_sum"] += cr
        r["cr_max"] = max(r["cr_max"], cr)
        r["ctx_sum"] += ctx
        r["ctx_max"] = max(r["ctx_max"], ctx)
        if e.get("cwd"):
            r["cwds"][e["cwd"]] = r["cwds"].get(e["cwd"], 0) + 1
        if e.get("git_branch"):
            r["branch"] = e["git_branch"]
        if e.get("_source_file"):
            r["files"].add(e["_source_file"])

    rows = []
    for (agent, host, sandbox, sid, sub), r in acc.items():
        cwd = max(r["cwds"], key=r["cwds"].get) if r["cwds"] else ""
        n = r["requests"]
        is_sub = bool(sub) or bool(r["parent"])
        if agent == "claude" and sub:
            parent = sid
            row_id = f"{sid}/{sub}"
        else:
            parent = r["parent"]
            row_id = sid
        comp = compactions.get((sid, sub)) if agent == "claude" else compactions.get((sid, ""))
        hook = None
        if agent == "claude" and not sub:
            hook = hook_by_session.get((host, sid))
        rows.append({
            "row_id": row_id,
            "session_id": sid,
            "subagent_id": sub or None,
            "is_subagent": is_sub,
            "parent_session_id": parent or None,
            "agent": agent,
            "agent_label": AGENT_LABELS.get(agent, agent),
            "host": host,
            "sandbox": redact(sandbox) or None,
            "project": redact(_project_name(cwd)) if cwd else None,
            "cwd": redact(cwd) or None,
            "git_branch": redact(r["branch"]) or None,
            "first_ts": iso_z(r["first"]),
            "last_ts": iso_z(r["last"]),
            "models": dict(sorted(r["models"].items(), key=lambda kv: -kv[1])),
            "requests": n,
            "tokens": r["tokens"],
            "cost_usd": round(sum(r["cost_by_class"].values()), 6),
            "cost_by_class_usd": {k: round(v, 6) for k, v in r["cost_by_class"].items()},
            "cost_basis": "list_price",
            "priced_by_fallback": sorted(r["fallback"]),
            "billing_mode": billing.get(agent, "unknown"),
            "hook_cost_usd": None if hook is None else round(hook, 6),
            "cache_read_per_request_mean": round(r["cr_sum"] / n, 1) if n else 0,
            "cache_read_per_request_max": r["cr_max"],
            "context_tokens_per_request_mean": round(r["ctx_sum"] / n, 1) if n else 0,
            "context_tokens_per_request_max": r["ctx_max"],
            "compactions": comp if comp is not None else 0,
            "source_files": sorted(redact(f) for f in r["files"]),
            "service_tiers": dict(sorted(r["service_tiers"].items())),
            "source_locations": [{k: redact(v) if k in ("path", "cwd") else v
                                  for k, v in dict(loc).items()} for loc in sorted(r["locations"])],
        })
    rows.sort(key=lambda x: (x["first_ts"] or "", x["row_id"]))
    return rows


def bucket_start(dt: datetime, bucket: str) -> str:
    dt = dt.astimezone(timezone.utc)
    if bucket == "hour":
        return dt.strftime("%Y-%m-%dT%H:00:00Z")
    return dt.strftime("%Y-%m-%dT00:00:00Z")


def build_bucket_rows(events: list[dict], bucket: str = "day",
                      hook_by_day: dict | None = None, roots=None,
                      redact=lambda v: v) -> list[dict]:
    """Time series keyed by (UTC bucket start, agent, host, sandbox).
    hook_by_day: {(host, YYYY-MM-DD): hook cost}, attached to the host-level
    Claude row of each day bucket (the hook doesn't know sandboxes or hours)."""
    acc: dict[tuple, dict] = {}
    for e in events:
        dt = _event_dt(e)
        if dt is None:
            continue
        key = (bucket_start(dt, bucket), e.get("agent", "claude"), e.get("source", "local"),
               sandbox_of(e.get("_source_file", ""), roots))
        r = acc.setdefault(key, {"tokens": dict.fromkeys(TOKEN_CLASSES, 0), "cost": 0.0,
                                 "requests": 0, "sessions": set()})
        for k, v in _event_classes(e).items():
            r["tokens"][k] += v
        r["cost"] += sum(_event_class_costs(e).values())
        r["requests"] += 1
        r["sessions"].add(e.get("session_id", ""))
    rows = []
    for (start, agent, host, sandbox), r in sorted(acc.items()):
        hook = None
        if bucket == "day" and agent == "claude" and hook_by_day is not None:
            # Booked once per host/day: on the row with the sandbox sorting first.
            first = min(k[3] for k in acc if k[:3] == (start, agent, host))
            if sandbox == first:
                hook = hook_by_day.get((host, start[:10]))
        rows.append({"bucket_start": start, "bucket": bucket, "agent": agent,
                     "host": host, "sandbox": redact(sandbox) or None,
                     "requests": r["requests"], "sessions": len(r["sessions"]),
                     "tokens": r["tokens"], "cost_usd": round(r["cost"], 6),
                     "hook_cost_usd_host_day": None if hook is None else round(hook, 6)})
    return rows


def rate_card_rows(models) -> list[dict]:
    """The rates applied to each model seen, $/Mtok per token class."""
    out = []
    for m in sorted(set(models)):
        row = _find_rate_row(m)
        base = row or (OPENAI_FALLBACK_RATES if _is_openai_model(m) else SONNET_RATES)

        def per_class(r):
            return {"input": r["input"], "output": r["output"], "cache_read": r["cache_read"],
                    "cache_write_5m": r["cache_write"], "cache_write_1h": r["cache_write_1h"]}
        entry = {"model": m, "provider": base["provider"],
                 "rate_family": base["family"], "priced_by_fallback": row is None,
                 "usd_per_mtok": per_class(get_rates(m)),
                 "service_tier_multipliers": {
                     "standard": 1.0, "fast": FAST_MULTIPLIERS.get(base["family"], 1.0),
                     "flex": 0.5 if base["family"] in FLEX_MODELS else 1.0}}
        if base.get("long_context"):
            lc = base["long_context"]
            entry["long_context"] = {"applies_when_prompt_tokens_over": lc["over"],
                                     "usd_per_mtok": per_class(get_rates(m, request_input=lc["over"] + 1)),
                                     "semantics": "whole_request"}
        if base.get("intro"):
            entry["intro"] = dict(base["intro"])
        out.append(entry)
    return out


def _source_key(path: str, roots) -> tuple:
    """(host, sandbox, data dir) for a log file."""
    host = path.split(":", 1)[0] if ":" in path and not path.startswith("/") else "local"
    local = path.split(":", 1)[1] if host != "local" else path
    sb = sandbox_of(path, roots)
    for marker in ("/projects/", "/sessions/", "/archived_sessions/"):
        if marker in local:
            return host, sb, local.split(marker, 1)[0] + marker.rstrip("/")
    return host, sb, str(Path(local).parent)


def _day_range(first: str, last: str) -> list[str]:
    d = datetime.strptime(first, "%Y-%m-%d")
    end = datetime.strptime(last, "%Y-%m-%d")
    out = []
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _ranges(days: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    for d in sorted(days):
        if out and (datetime.strptime(d, "%Y-%m-%d")
                    - datetime.strptime(out[-1][1], "%Y-%m-%d")).days == 1:
            out[-1][1] = d
        else:
            out.append([d, d])
    return out


def build_coverage(file_stats: dict, events: list[dict], sources: list[dict],
                   since: datetime | None, until: datetime | None, roots=None,
                   redact=lambda v: v, zst_skipped: int = 0) -> dict:
    """Per data source (log dir, per host/sandbox): files, records, earliest and
    latest timestamps, unparseable lines and unreadable files. Per host and
    agent: days in the window with no records at all (UNKNOWN — idle and lost
    logs look the same), and for Claude, days/sessions where the statusline
    hook saw spend but no transcript exists (logs missing → tokens UNKNOWN)."""
    by_src: dict[tuple, dict] = {}
    for path, st in file_stats.items():
        host, sb, d = _source_key(path, roots)
        agent = "codex" if ("/sessions" in d or "/archived_sessions" in d) else "claude"
        s = by_src.setdefault((host, sb, d, agent), {
            "files": 0, "unreadable_files": 0, "lines": 0, "bad_lines": 0,
            "records": 0, "first": None, "last": None})
        s["files"] += 1
        s["unreadable_files"] += 1 if st.get("unreadable") else 0
        for k in ("lines", "bad_lines", "records"):
            s[k] += st.get(k, 0)
        for k, f in (("first", min), ("last", max)):
            v = st.get(k + "_ts")
            if v is not None:
                s[k] = v if s[k] is None else f(s[k], v)
    data_sources = [{
        "host": host, "sandbox": redact(sb) or None, "agent": agent, "path": redact(d),
        "files": s["files"], "unreadable_files": s["unreadable_files"],
        "lines": s["lines"], "unparseable_lines": s["bad_lines"],
        "usage_records": s["records"],
        "first_ts": iso_z(s["first"]), "last_ts": iso_z(s["last"]),
        "status": ("UNKNOWN" if s["unreadable_files"] or s["bad_lines"] else "ok"),
    } for (host, sb, d, agent), s in sorted(by_src.items(), key=lambda kv: kv[0])]

    # Window for gap detection: the requested one, else the span of the data.
    all_days = sorted({_event_date(e) for e in events if _event_date(e)})
    first_day = since.strftime("%Y-%m-%d") if since else (all_days[0] if all_days else None)
    last_day = ((until - timedelta(microseconds=1)).strftime("%Y-%m-%d") if until
                else datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    window_days = _day_range(first_day, last_day) if first_day and first_day <= last_day else []

    gaps = []
    hosts = sorted({(src["name"], src["agent"]) for src in sources}
                   | {(e.get("source", "local"), e.get("agent", "claude")) for e in events})
    for host, agent in hosts:
        days_with = {_event_date(e) for e in events
                     if host in _event_hosts(e) and e.get("agent", "claude") == agent}
        no_records = [d for d in window_days if d not in days_with]
        entry = {"host": host, "agent": agent,
                 "days_without_records": _ranges(no_records),
                 "note": "no records: UNKNOWN (idle and missing logs are indistinguishable)"}
        if agent == "claude":
            src = next((s for s in sources if s["name"] == host and s["agent"] == "claude"), None)
            deltas = [d for d in (src or {}).get("deltas", [])
                      if d["date_str"] in set(window_days) and d["cost_usd"] > 0]
            jsonl_sessions = {e.get("session_id") for e in events
                              if host in _event_hosts(e) and e.get("agent") == "claude"}
            hook_only: dict[str, dict] = {}
            for d in deltas:
                if d["session_id"] not in jsonl_sessions:
                    h = hook_only.setdefault(d["session_id"], {"session_id": d["session_id"],
                                                               "hook_cost_usd": 0.0, "dates": set()})
                    h["hook_cost_usd"] += d["cost_usd"]
                    h["dates"].add(d["date_str"])
            hook_days = {d["date_str"] for d in deltas}
            entry["hook_spend_days_without_records"] = sorted(hook_days - days_with)
            entry["sessions_with_hook_spend_but_no_logs"] = [
                {"session_id": h["session_id"], "hook_cost_usd": round(h["hook_cost_usd"], 6),
                 "dates": sorted(h["dates"]), "tokens": "UNKNOWN"}
                for h in sorted(hook_only.values(), key=lambda h: -h["hook_cost_usd"])]
            if src and src.get("install_date"):
                entry["hook_coverage_from"] = src["install_date"]
        gaps.append(entry)
    return {"data_sources": data_sources, "gaps": gaps,
            "window_days": [window_days[0], window_days[-1]] if window_days else None,
            "compressed_rollouts_skipped": zst_skipped,
            "unresolved_codex_forks": [
                {"session_id": sid, "parent_session_id": parent, "status": "UNKNOWN",
                 "note": "parent history unavailable; possible replay retained in totals"}
                for sid, parent in sorted({(e.get("session_id", ""), e["_unresolved_fork"])
                                           for e in events if e.get("_unresolved_fork")})]}


def compaction_counts(file_stats: dict, since: datetime | None = None,
                      until: datetime | None = None) -> dict:
    """{(session_id, subagent_id): compactions within the window}."""
    merged: dict[tuple, dict] = {}
    for st in file_stats.values():
        for k, markers in st.get("compactions", {}).items():
            merged.setdefault(k, {}).update(markers)
    out = {}
    for k, markers in merged.items():
        n = len(filter_window([{"timestamp": ts} for ts in markers.values()], since, until))
        if n:
            out[k] = n
    return out


def build_document(*, sessions, buckets, bucket, coverage, rates, since, until,
                   billing, redacted) -> dict:
    return {
        "schema": "cctrack.report",
        "schema_version": SCHEMA_VERSION,
        "cctrack_version": cctrack_version(),
        "generated_at": iso_z(datetime.now(timezone.utc)),
        "timezone": "UTC",
        "window": {"since": iso_z(since), "until": iso_z(until),
                   "semantics": "since <= record timestamp < until"},
        "redacted": redacted,
        "cost_basis": {
            "method": "list_price",
            "description": ("cost_usd = tokens x the provider's published API list price "
                            "per token class, including the recorded service tier and whole-request "
                            "context tier, at the rate in effect on the record's date. "
                            "hook_cost_usd is Claude Code's own statusline cost counter "
                            "(Claude only; booked per UTC day, so null unless the window is "
                            "day-aligned)."),
            "rate_card_version": RATE_CARD_VERSION,
            "rate_card_sources": RATE_CARD_SOURCES,
            "billing_mode": billing,
        },
        "rate_card": rates,
        "bucket": bucket,
        "sessions": sessions,
        "buckets": buckets,
        "coverage": coverage,
    }


SESSION_CSV_FIELDS = [
    "row_id", "session_id", "subagent_id", "is_subagent", "parent_session_id", "agent",
    "host", "sandbox", "project", "cwd", "git_branch", "first_ts", "last_ts", "models",
    "requests", *[f"tokens_{c}" for c in TOKEN_CLASSES], "cost_usd",
    *[f"cost_{c}_usd" for c in TOKEN_CLASSES], "cost_basis", "rate_card_version",
    "priced_by_fallback", "billing_mode", "hook_cost_usd",
    "cache_read_per_request_mean", "cache_read_per_request_max",
    "context_tokens_per_request_mean", "context_tokens_per_request_max", "compactions",
    "service_tiers", "source_files", "source_locations",
]
BUCKET_CSV_FIELDS = [
    "bucket_start", "bucket", "agent", "host", "sandbox", "requests", "sessions",
    *[f"tokens_{c}" for c in TOKEN_CLASSES], "cost_usd", "hook_cost_usd_host_day",
]


def _flat(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if k == "tokens":
            out.update({f"tokens_{c}": n for c, n in v.items()})
        elif k == "cost_by_class_usd":
            out.update({f"cost_{c}_usd": n for c, n in v.items()})
        elif k in ("service_tiers", "source_locations"):
            out[k] = json.dumps(v, separators=(",", ":"), sort_keys=True)
        elif k == "models":
            out[k] = ";".join(f"{m}:{n}" for m, n in v.items())
        elif isinstance(v, list):
            out[k] = ";".join(map(str, v))
        elif isinstance(v, bool):
            out[k] = "true" if v else "false"
        else:
            out[k] = "" if v is None else v
    return out


def write_csv(rows: list[dict], fields: list[str], out=None) -> None:
    import csv
    out = out or sys.stdout
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["schema_version", *fields])
    for r in rows:
        f = _flat({**r, "rate_card_version": RATE_CARD_VERSION})
        w.writerow([SCHEMA_VERSION, *(f.get(k, "") for k in fields)])


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def _short(v, n):
    v = "" if v is None else str(v)
    return v if len(v) <= n else v[:n - 1] + "…"


def print_sessions(rows: list[dict], since, until) -> None:
    print()
    print(f"  Sessions (UTC)  window: {iso_z(since) or 'start'} → {iso_z(until) or 'now'}"
          f"   cost = API list price, rate card {RATE_CARD_VERSION}")
    print()
    hdr = (f"  {'Session':<14} {'Agent':<6} {'Where':<22} {'Project':<16} {'First (UTC)':<17}"
           f" {'Last (UTC)':<17} {'Req':>5} {'Input':>9} {'Output':>9} {'CacheRd':>12}"
           f" {'CacheWr':>11} {'CR/req':>8} {'Cmp':>4} {'Cost':>10}")
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    parents = {r["session_id"] for r in rows if not r["is_subagent"]}
    order, children = [], defaultdict(list)
    for r in rows:
        if r["is_subagent"] and r["parent_session_id"] in parents:
            children[r["parent_session_id"]].append(r)
        else:
            order.append(r)
    total = 0.0

    def line(r, indent):
        nonlocal total
        total += r["cost_usd"]
        t = r["tokens"]
        sid = ("↳ " + (r["subagent_id"] or r["session_id"])) if indent else r["session_id"]
        where = r["host"] + (f"/{r['sandbox']}" if r["sandbox"] else "")
        hook = "" if r["hook_cost_usd"] is None else f"  (hook ${r['hook_cost_usd']:.2f})"
        flag = "?" if r["priced_by_fallback"] else ""
        print(f"  {_short(sid, 14):<14} {r['agent']:<6} {_short(where, 22):<22}"
              f" {_short(r['project'], 16):<16} {(r['first_ts'] or '')[:16]:<17}"
              f" {(r['last_ts'] or '')[:16]:<17} {r['requests']:>5,} {t['input']:>9,}"
              f" {t['output']:>9,} {t['cache_read']:>12,}"
              f" {t['cache_write_5m'] + t['cache_write_1h']:>11,}"
              f" {r['cache_read_per_request_mean']:>8,.0f} {r['compactions']:>4}"
              f" {_usd(r['cost_usd']):>10}{flag}{hook}")

    for r in order:
        line(r, False)
        for c in children.get(r["session_id"], []):
            line(c, True)
    print("  " + "─" * (len(hdr) - 2))
    print(f"  {len(rows)} row(s)   total ${total:,.2f}   "
          f"(↳ = subagent; CR/req = mean cache-read tokens per request; Cmp = compactions)")
    print()


def print_buckets(rows: list[dict], bucket: str) -> None:
    print()
    print(f"  Cost by {bucket} (UTC), API list price, rate card {RATE_CARD_VERSION}")
    print()
    hdr = (f"  {'Start (UTC)':<20} {'Agent':<6} {'Where':<24} {'Req':>6} {'Sess':>5}"
           f" {'Input':>10} {'Output':>10} {'CacheRd':>13} {'CacheWr':>12} {'Cost':>10}")
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    total = 0.0
    for r in rows:
        t = r["tokens"]
        total += r["cost_usd"]
        where = r["host"] + (f"/{r['sandbox']}" if r["sandbox"] else "")
        print(f"  {r['bucket_start']:<20} {r['agent']:<6} {_short(where, 24):<24}"
              f" {r['requests']:>6,} {r['sessions']:>5} {t['input']:>10,} {t['output']:>10,}"
              f" {t['cache_read']:>13,} {t['cache_write_5m'] + t['cache_write_1h']:>12,}"
              f" {_usd(r['cost_usd']):>10}")
    print("  " + "─" * (len(hdr) - 2))
    print(f"  total ${total:,.2f}")
    print()


def print_coverage(cov: dict) -> None:
    print()
    print("  Coverage (UTC)")
    print()
    for s in cov["data_sources"]:
        where = s["host"] + (f"/{s['sandbox']}" if s["sandbox"] else "")
        print(f"  [{s['status']:<7}] {s['agent']:<6} {where}  {s['path']}")
        print(f"            {s['files']} file(s), {s['usage_records']:,} usage records, "
              f"{s['first_ts'] or '-'} → {s['last_ts'] or '-'}")
        if s["unparseable_lines"] or s["unreadable_files"]:
            print(f"            {s['unparseable_lines']} unparseable line(s), "
                  f"{s['unreadable_files']} unreadable file(s)")
    if cov.get("compressed_rollouts_skipped"):
        print(f"  [UNKNOWN] {cov['compressed_rollouts_skipped']} compressed Codex rollout(s) "
              f"could not be read")
    if cov.get("window_days"):
        print()
        print(f"  Gaps, {cov['window_days'][0]} → {cov['window_days'][1]}:")
    for g in cov["gaps"]:
        rng = ", ".join(a if a == b else f"{a}..{b}" for a, b in g["days_without_records"])
        print(f"    {g['agent']:<6} {g['host']}: no records (UNKNOWN) on: {rng or 'none'}")
        if g.get("hook_spend_days_without_records"):
            print(f"           hook saw spend but no transcripts on: "
                  f"{', '.join(g['hook_spend_days_without_records'])}")
        for h in g.get("sessions_with_hook_spend_but_no_logs", []):
            print(f"           session {h['session_id']}: hook ${h['hook_cost_usd']:.2f} on "
                  f"{', '.join(h['dates'])}, transcripts missing (tokens UNKNOWN)")
    print()

    for fork in cov.get("unresolved_codex_forks", []):
        print(f"  UNKNOWN Codex fork {fork['session_id']}: parent {fork['parent_session_id']} "
              "history unavailable; possible replay retained in totals")


def run_detail_report(args, all_events, sources, file_stats, since, until,
                      zst_skipped=0, out=None) -> None:
    """--sessions / --bucket / --coverage / --json / --csv."""
    out = out or sys.stdout
    redact = make_redactor(args.redact)
    billing = parse_billing_mode(args.billing_mode)
    roots = sandbox_roots()
    events = filter_window(all_events, since, until)
    first_day = since.strftime("%Y-%m-%d") if since else None
    last_day = (until - timedelta(microseconds=1)).strftime("%Y-%m-%d") if until else None
    hook_by_session: dict = defaultdict(float)
    hook_by_day: dict = defaultdict(float)
    # Hook cost is booked per UTC day, so it's only comparable for windows
    # that start and end on day boundaries.
    day_aligned = all(b is None or b.time() == datetime.min.time() for b in (since, until))
    for src in (sources if day_aligned else []):
        for d in src.get("deltas", []):
            if (first_day and d["date_str"] < first_day) or (last_day and d["date_str"] > last_day):
                continue
            hook_by_session[(src["name"], d["session_id"])] += d["cost_usd"]
            hook_by_day[(src["name"], d["date_str"])] += d["cost_usd"]
    bucket = args.bucket or "day"
    sessions = build_session_rows(events, compaction_counts(file_stats or {}, since, until),
                                  dict(hook_by_session), billing, roots, redact)
    buckets = build_bucket_rows(events, bucket, dict(hook_by_day) if day_aligned else None,
                                roots, redact)
    coverage = build_coverage(file_stats or {}, events, sources, since, until, roots,
                              redact, zst_skipped)
    rates = rate_card_rows(e.get("model", "") for e in events)

    if args.json:
        doc = build_document(sessions=sessions, buckets=buckets, bucket=bucket,
                             coverage=coverage, rates=rates, since=since, until=until,
                             billing=billing, redacted=bool(args.redact))
        json.dump(doc, out, indent=2)
        out.write("\n")
        return
    if args.csv == "sessions":
        write_csv(sessions, SESSION_CSV_FIELDS, out)
        return
    if args.csv == "buckets":
        write_csv(buckets, BUCKET_CSV_FIELDS, out)
        return
    if args.csv == "coverage":
        fields = ["host", "sandbox", "agent", "path", "files", "unreadable_files", "lines",
                  "unparseable_lines", "usage_records", "first_ts", "last_ts", "status"]
        write_csv(coverage["data_sources"], fields, out)
        return
    if args.csv == "rates":
        flat = [{"model": r["model"], "provider": r["provider"], "rate_family": r["rate_family"],
                 "priced_by_fallback": r["priced_by_fallback"],
                 **{f"usd_per_mtok_{c}": v for c, v in r["usd_per_mtok"].items()},
                 "long_context_over": r.get("long_context", {}).get(
                     "applies_when_prompt_tokens_over", "")} for r in rates]
        fields = ["model", "provider", "rate_family", "priced_by_fallback",
                  *[f"usd_per_mtok_{c}" for c in TOKEN_CLASSES], "long_context_over",
                  "rate_card_version"]
        write_csv(flat, fields, out)
        return
    if args.sessions:
        print_sessions(sessions, since, until)
    if args.bucket:
        print_buckets(buckets, bucket)
    if args.coverage:
        print_coverage(coverage)


# ── CLI ─────────────────────────────────────────────────────────────────

def resolve_date(token: str) -> str:
    """Resolve a --explore argument to a YYYY-MM-DD string.
    Accepts an explicit date, or 'today'/'yesterday'."""
    t = token.strip().lower()
    now = datetime.now(timezone.utc)
    if t in ("today", "now"):
        return now.strftime("%Y-%m-%d")
    if t == "yesterday":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    # Validate an explicit date; raise for anything else so the CLI can report it.
    datetime.strptime(token.strip(), "%Y-%m-%d")
    return token.strip()


def _note_remote_timing(timing: "_Timing", host: str) -> None:
    """Fill the `remote <host>` timing line from the fetched bundle."""
    if not timing.enabled:
        return
    phase = f"remote {host}"
    b = _remote_bundle_cache.get(host) or {}
    if b.get("ok"):
        detail = f"{format_bytes(b.get('_wire_bytes', 0))} received"
        if b.get("_cached"):
            c = b.get("cache_counts", {})
            detail += f"; cache: {c.get('reused', 0):,} reused, {c.get('read', 0):,} read"
        if b.get("_refetched"):
            detail += "; refetched"
        timing.detail(phase, detail)
        if phase not in timing.subs and b.get("timing"):
            census = b["timing"].get("census", {})
            for name, secs in b["timing"]["phases"].items():
                timing.sub(phase, f"on {host}: {name}", secs,
                           _census_text(census[name]) if name in census else "")
            if "_remote_encode" in b:
                timing.sub(phase, f"on {host}: encode", b["_remote_encode"])
            timing.sub(phase, "local: decode", b.get("_local_decode", 0.0))
            timing.sub(phase, "local: merge", b.get("_local_merge", 0.0))
            timing.other(phase, f"on {host}: other", "ssh, transfer and the rest")
    elif b.get("fallback"):
        timing.detail(phase, "raw stream")
    elif b.get("failed"):
        timing.detail(phase, "failed")


def main():
    timing = _Timing(timing_enabled())
    parser = argparse.ArgumentParser(
        prog="cctrack",
        description="Claude Code cost reporter — scans JSONL logs and reports token usage + cost"
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Limit daily breakdown to last N days"
    )
    parser.add_argument(
        "--dirs", nargs="+", default=None,
        help="Custom Claude transcript directories to scan (default: ~/.claude/projects plus each Sandy sandbox's claude/projects)"
    )
    parser.add_argument(
        "--remote", nargs="+", default=None, metavar="HOST",
        help="SSH hosts to fetch logs from (e.g. dgx macbook-air user@server)"
    )
    parser.add_argument(
        "--energy", action="store_true",
        help="Show estimated inference energy (kWh). Rough estimate — see the "
             "note printed with the report. Scale with CCTRACK_ENERGY_SCALE."
    )
    parser.add_argument(
        "--explore", metavar="DATE",
        help="Break down a single day (YYYY-MM-DD, or 'yesterday'/'today') by "
             "token class, model, project, session and machine."
    )
    parser.add_argument(
        "--project", metavar="NAME",
        help="With --explore, restrict to one project and show each session's "
             "full working directory, git branch and source file (traces an "
             "unfamiliar project label to a real location)."
    )
    parser.add_argument(
        "--install-hook", action="store_true",
        help="Install the cctrack statusline hook for Claude Code"
    )
    parser.add_argument(
        "--uninstall-hook", action="store_true",
        help="Uninstall the cctrack statusline hook"
    )
    parser.add_argument(
        "--agent", choices=("all", "claude", "codex"), default="all",
        help="Which coding agents to include (default: all found). Codex logs "
             "are read from $CODEX_HOME or ~/.codex."
    )
    cache_group = parser.add_argument_group(
        "parse cache",
        "Repeat runs re-read only the log files that changed and take the rest from the "
        "cache: cache.sqlite3 in $CCTRACK_CACHE_DIR, else $XDG_CACHE_HOME/cctrack, else "
        "~/.cache/cctrack. It holds parsed usage records, never prices. The first run after "
        "any change to cctrack or Python reads everything.")
    cache_opts = cache_group.add_mutually_exclusive_group()
    cache_opts.add_argument(
        "--no-cache", action="store_true",
        help="Read every log file; don't read or write the parse cache (or set CCTRACK_CACHE=0).")
    cache_opts.add_argument(
        "--rebuild-cache", action="store_true",
        help="Read every log file, including on --remote hosts, and replace the parse cache.")
    detail = parser.add_argument_group(
        "detailed / machine-readable reports (all UTC; ids, paths, counts and money only)")
    detail.add_argument("--since", metavar="UTC",
                        help="Window start, ISO-8601 (2026-10-11T02:30:04Z) or YYYY-MM-DD. "
                             "Naive times are UTC. Inclusive.")
    detail.add_argument("--until", metavar="UTC",
                        help="Window end, same formats. Exclusive; a bare date includes that whole day.")
    detail.add_argument("--sessions", action="store_true",
                        help="Per-session breakdown (subagents as linked rows), with token "
                             "classes, requests, models, context size and compactions.")
    detail.add_argument("--bucket", choices=("hour", "day"),
                        help="Cost/tokens per UTC hour or day, by agent, host and sandbox.")
    detail.add_argument("--coverage", action="store_true",
                        help="Per log source: earliest/latest record, unparseable data, and "
                             "days/sessions with missing logs flagged UNKNOWN.")
    detail.add_argument("--json", action="store_true",
                        help="Emit one JSON document (schema_version'd) with sessions, buckets, "
                             "coverage, rate card and cost basis.")
    detail.add_argument("--csv", choices=("sessions", "buckets", "coverage", "rates"),
                        help="Emit one table as CSV.")
    detail.add_argument("--redact", action="store_true",
                        help="Hash paths, project, sandbox and branch names "
                             "(salt with CCTRACK_REDACT_SALT).")
    detail.add_argument("--billing-mode", metavar="MODE",
                        help="Recorded per row: 'api', 'subscription', or "
                             "'claude=subscription,codex=api' (or set CCTRACK_BILLING_MODE). "
                             "Doesn't change cost, which is always list price.")
    args = parser.parse_args()
    want_claude = args.agent in ("all", "claude")
    detail_mode = bool(args.sessions or args.bucket or args.coverage or args.json or args.csv)
    since = until = None
    try:
        since = parse_utc(args.since) if args.since else None
        until = parse_utc(args.until, end=True) if args.until else None
    except ValueError as exc:
        parser.error(f"invalid --since/--until: {exc}")
    if (since or until) and not detail_mode:
        parser.error("--since/--until apply to --sessions, --bucket, --coverage, --json and --csv")
    file_stats: dict = {} if detail_mode else None
    zst_total = 0
    want_codex = args.agent in ("all", "codex")

    # Hook management (run and exit)
    if args.install_hook:
        if args.remote:
            for host in args.remote:
                remote_install_hook(host)
        else:
            install_hook()
        return
    if args.uninstall_hook:
        if args.remote:
            for host in args.remote:
                remote_uninstall_hook(host)
        else:
            uninstall_hook()
        return

    files: list = []
    codex_files: list = []
    hook_dirs: list = []
    unwinding = False
    store = None
    def _census():
        try:
            hook_files = [f for d in hook_dirs for f in d.rglob("statusline*.jsonl")]
        except OSError:
            hook_files = []
        return {"claude parse": files, "codex parse": codex_files, "hook read": hook_files}
    try:
        # ── Local source: JSONL events + hook data ──
        mode = cache_mode(args.no_cache, args.rebuild_cache)
        if mode != "off":
            timing.phase("cache")
            store = open_cache(rebuild=mode == "rebuild")
        timing.phase("discovery")
        dirs = args.dirs if args.dirs else default_dirs()
        files = discover_files(dirs) if want_claude else []
        timing.phase("claude parse")
        h0 = _cache_hits(store)
        local_events = parse_events(files, file_stats=file_stats, cache=store)
        _note_cache_use(timing, "claude parse", store, h0, len(files))

        timing.phase("discovery")
        local_install = None
        config = read_hook_config()
        if config and "installed_at" in config:
            try:
                dt = datetime.fromisoformat(config["installed_at"].replace("Z", "+00:00"))
                local_install = dt.strftime("%Y-%m-%d")
            except (ValueError, AttributeError):
                pass

        local_hook_events: list[dict] = []
        local_hook_rows: list[dict] = []
        local_baselines: dict = {}
        local_hook_monthly: dict[str, dict] = {}
        hook_dirs = discover_hook_data_dirs() if want_claude else []
        timing.phase("hook read")
        hook_progress = Progress("Reading statusline hook data")
        h0 = _cache_hits(store)
        n_hook = 0
        for i, hdir in enumerate(hook_dirs, 1):
            hook_progress.update(f"{i}/{len(hook_dirs)} folders", force=True)
            rollup_old_hook_files(hdir)
            n_hook += len(_hook_files(hdir))
            baselines = load_session_baselines(hdir)
            rows = parse_hook_rows(hdir, cache=store)
            local_hook_rows.extend(rows)
            for sid, base in baselines.items():
                if sid not in local_baselines or base.get("date", "") < local_baselines[sid].get("date", ""):
                    local_baselines[sid] = base
            local_hook_events.extend(compute_hook_deltas(rows, baselines))
            merge_monthly_summaries(local_hook_monthly, load_monthly_hook_summaries(hdir))
        hook_progress.done()
        _note_cache_use(timing, "hook read", store, h0, n_hook)

        local_hook_daily: dict = {}
        if local_hook_events or local_hook_monthly:
            local_hook_daily = aggregate_hook_data(local_hook_events, local_hook_monthly)

        # Hook coverage may start earlier than the configured install date
        # (e.g. data restored from another machine); the earliest observed hook
        # date is the effective install date for this source.
        if local_hook_daily:
            earliest = min(local_hook_daily.keys())
            if local_install is None or earliest < local_install:
                local_install = earliest

        all_events = list(local_events)
        sources = [{"name": "local", "agent": "claude", "daily": {},
                    "hook_daily": local_hook_daily, "install_date": local_install,
                    "deltas": local_hook_events, "_hook_rows": local_hook_rows,
                    "_hook_baselines": local_baselines, "_hook_monthly": local_hook_monthly}]

        # Codex is its own source on each machine: it's priced from the per-response
        # usage Codex logs, and must never be displaced by Claude's hook coverage.
        def _add_codex_source(name: str, events: list[dict]) -> None:
            if not events:
                return
            all_events.extend(events)
            sources.append({"name": name, "agent": "codex", "exact": True,
                            "daily": {}, "hook_daily": {},
                            "install_date": None, "deltas": []})

        if want_codex:
            timing.phase("discovery")
            codex_files = discover_codex_files()
            timing.phase("codex parse")
            h0 = _cache_hits(store)
            codex_events, zst_skipped = parse_codex_events(codex_files, file_stats=file_stats,
                                                           cache=store)
            _note_cache_use(timing, "codex parse", store, h0, len(codex_files))
            zst_total += zst_skipped
            _add_codex_source("local", codex_events)
            if zst_skipped:
                print(f"  Note: skipped {zst_skipped} compressed Codex rollout(s) (.jsonl.zst) — "
                      f"install `zstd` (or use Python 3.14+) to include them.", file=sys.stderr)

        # ── One source per remote host. Deltas are computed per host so a
        # session's cumulative history never mixes across hosts. ──
        if args.remote:
            for host in args.remote:
                # One ssh call parses everything on the host; the per-kind fetches
                # below then read from it, or stream raw logs if it fell back.
                rphase = f"remote {host}"
                timing.phase(rphase)
                fetch_remote_bundle(host, want_claude, want_codex, cache=store)
                if want_codex:
                    remote_codex = parse_remote_codex_events(host, file_stats)
                    _add_codex_source(host, remote_codex)
                    timing.phase(rphase)
                if not want_claude:
                    _note_remote_timing(timing, host)
                    continue
                remote_events = parse_remote_events(host, file_stats)
                all_events.extend(remote_events)

                remote_hook_events, remote_monthly, remote_baselines = parse_remote_hook_data(host)
                remote_deltas = compute_hook_deltas(remote_hook_events, remote_baselines)
                remote_hook_daily: dict = {}
                if remote_deltas or remote_monthly:
                    remote_hook_daily = aggregate_hook_data(remote_deltas, remote_monthly)
                remote_install = min(remote_hook_daily.keys()) if remote_hook_daily else None

                sources.append({"name": host, "agent": "claude", "daily": {},
                                "hook_daily": remote_hook_daily, "install_date": remote_install,
                                "deltas": remote_deltas, "_hook_rows": remote_hook_events,
                                "_hook_baselines": remote_baselines, "_hook_monthly": remote_monthly})
                _note_remote_timing(timing, host)

        timing.phase("reconcile")
        all_events = reconcile_sources(all_events, sources, timing=timing)

        timing.phase("report")
        if detail_mode:
            run_detail_report(args, all_events, sources, file_stats, since, until, zst_total)
            return

        # Suggest hook if not installed (it only matters for Claude Code)
        if want_claude and not is_hook_installed():
            prompt_hook_install()

        # ── Explore a single day (run and exit) ──
        if args.explore:
            try:
                target = resolve_date(args.explore)
            except ValueError:
                print(f"Invalid --explore date: {args.explore!r} "
                      f"(use YYYY-MM-DD, 'today', or 'yesterday')", file=sys.stderr)
                return
            hook_by_source: dict[str, float] = defaultdict(float)
            hook_by_session: dict[tuple, float] = defaultdict(float)
            for src in sources:
                for d in src.get("deltas", []):
                    if d["date_str"] == target:
                        hook_by_source[src["name"]] += d["cost_usd"]
                        hook_by_session[(src["name"], d["session_id"])] += d["cost_usd"]
            print_explore(all_events, target,
                          hook_by_source=dict(hook_by_source),
                          hook_by_session=dict(hook_by_session),
                          show_energy=args.energy,
                          project_filter=args.project,
                          capped=capped_hook_days(sources))
            return

        # Global aggregates feed the token columns; cost is composed per source.
        timing.phase("aggregate")
        daily, monthly = aggregate(all_events)

        timing.phase("report")
        print_report(daily, monthly, days=args.days, sources=sources,
                     show_energy=args.energy)

    except BaseException:
        unwinding = True
        raise
    finally:
        # (a `return` inside the try skips try/else, hence the flag + finally)
        if store is not None:
            if not unwinding:
                timing.phase("cache")     # never while unwinding: the marker stays on the phase
            store.close()                 # never raises; flushes pending rows
        timing.finish(interrupted=unwinding, census=_census())

if __name__ == "__main__":
    main()

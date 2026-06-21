"""Truth-vs-myth quests: turning a legend into something you *play*.

When a ruin is excavated we derive a set of **claims** about its builder. Each
claim has a definite truth value grounded in the event log (the ground truth) —
NOT in the AI's prose, which only provides atmosphere. Some claims restate what
really happened; others are oral-tradition embellishments (inflated numbers,
flat-out myths). The player judges which are true and digs for rumored hoards;
being right builds Loremaster renown, and true hoards pay out in loot.

Claims are generated deterministically from (location + builder), so a given
ruin tells everyone the same tale — consistent with the cached legend.
"""
from __future__ import annotations

import random
from typing import Any

# Pure-myth flourishes: always false, no mortal record could support them.
_DIVINE = [
    "commanded the frost to do their bidding",
    "made the trembling earth stand still",
    "spoke with the spirits of beasts",
    "walked the world in a single stride",
    "called the rivers to part before them",
    "wrestled the storm and won",
]


def summarize_deeds(deeds: list[dict]) -> dict[str, Any]:
    """Reduce a builder's raw event log into the facts claims are checked against."""
    builds = sum(1 for e in deeds if e["kind"] == "build")
    gathers = sum(1 for e in deeds if e["kind"] == "gather")
    return {"builds": max(builds, 1), "gathers": gathers}


def build_claims(builder: str, deeds: list[dict], x: int, y: int) -> list[dict]:
    """Return the ordered list of claims for one ruin. Each claim is a dict:

    {id, mode, text, truth, basis, reward}
      mode  : "judge" (decide true/embellished) or "hoard" (dig to find out)
      truth : the real answer, derived from the log / a seeded roll
      basis : the explanation revealed once the claim is resolved
      reward: items granted when a hoard is real (empty otherwise)
    """
    facts = summarize_deeds(deeds)
    builds, gathers = facts["builds"], facts["gathers"]
    rng = random.Random((x * 92821) ^ (y * 68917) ^ (hash(builder) & 0xFFFFFFFF))

    # Draw distinct flourishes so two claims never repeat the same myth.
    flourishes = _DIVINE[:]
    rng.shuffle(flourishes)

    claims: list[dict] = []

    def add(mode: str, text: str, truth: bool, basis: str, reward=None) -> None:
        claims.append({
            "id": len(claims), "mode": mode, "text": text,
            "truth": truth, "basis": basis, "reward": reward or {},
        })

    # 1) Structures raised — true count, or inflated.
    if rng.random() < 0.5:
        add("judge", f"that they raised {builds} great work(s) across the land.",
            True, f"The record shows {builds} structure(s) by their hand.")
    else:
        inflated = builds + rng.randint(2, 4)
        add("judge", f"that they raised {inflated} great works across the land.",
            False, f"The record shows only {builds} structure(s) by their hand.")

    # 2) Bounty drawn — true count / inflated, or (if they never gathered) a myth.
    if gathers > 0:
        if rng.random() < 0.5:
            add("judge", f"that they drew the land's bounty {gathers} times.",
                True, f"The record shows {gathers} gathering(s).")
        else:
            inflated = gathers + rng.randint(3, 7)
            add("judge", f"that they drew the land's bounty {inflated} times.",
                False, f"The record shows only {gathers} gathering(s).")
    else:
        add("judge", f"that they {flourishes.pop()}.",
            False, "No mortal record bears this out — pure legend.")

    # 3) A pure-myth flourish (always false) — color, and an easy debunk.
    add("judge", f"that they {flourishes.pop()}.",
        False, "No mortal record bears this out — pure legend.")

    # 4) The gamble: a rumored buried hoard. Dig to find out.
    hoard = rng.random() < 0.4
    add("hoard", "that a hoard of treasures lies buried beneath this very place.",
        hoard,
        "You sink a deep shaft beneath the ruin and sift the spoil…",
        reward={"artifact": rng.randint(2, 4)} if hoard else {})

    return claims


def public_claim(claim: dict, verdict: dict | None) -> dict:
    """Strip the answer before sending to the client. If resolved, reveal it."""
    out = {"id": claim["id"], "mode": claim["mode"], "text": claim["text"]}
    if verdict is not None:
        out["resolved"] = True
        out.update(verdict)  # truth, basis, correct, result_text
    else:
        out["resolved"] = False
    return out

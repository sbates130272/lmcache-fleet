#!/usr/bin/env python3
"""Assertions on fleet_summary: what makes aggregators stackable.

An aggregator's output has to be the same kind of thing as its input, and the
answer must not depend on how the coordinators are grouped beneath it. If either
fails nothing breaks loudly -- a deeper tree just reports different numbers --
so both are asserted here, along with the accuracy the sketches claim.

    python3 scripts/check_fleet_summary.py     # standard library only
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "monitoring", "exporter"))
import fleet_summary as fs  # noqa: E402

rng = random.Random(0)


def block(n):
    return f"{n:064x}"


# Eight coordinators. Every one holds the 2,000 "shared" blocks (a system
# prompt, in effect); each also holds 30,000 of its own.
SHARED, OWN, PODS = 2_000, 30_000, 8
truth, leaves = set(), []
for pod in range(PODS):
    builder = fs.LeafBuilder(bloom_bits=1 << 22, top_n=50)
    held = list(range(SHARED)) + list(range(10**6 * (pod + 1), 10**6 * (pod + 1) + OWN))
    for n in held:
        # Shared blocks are the hot ones, and equally hot under every pod.
        accesses = 1000 - n if n < SHARED else rng.randrange(50)
        placements = [("l1", "dram", 8192)] if n % 3 == 0 else []
        placements += [("l2", "plugin", 8192), ("l2", "plugin-shared", 8192)]
        builder.add(block(n), accesses, placements)
    truth.update(held)
    leaves.append(builder.finish(f"pod-{pod}", f"http://coordinator-{pod}:9300", servers=10,
                                 now=1000.0 + pod))

# A leaf counts its own blocks exactly, and survives the wire unchanged.
assert all(leaf["blocks"] == SHARED + OWN for leaf in leaves)
again = fs.decode(fs.encode(leaves[0]))
assert again["sketches"]["all"].registers == leaves[0]["sketches"]["all"].registers
assert again["bloom"].data == leaves[0]["bloom"].data
assert {k: v for k, v in again.items() if k not in ("sketches", "bloom")} == \
    {k: v for k, v in leaves[0].items() if k not in ("sketches", "bloom")}


def comparable(summary):
    """Everything a summary says, minus who said it and when it was merged."""
    return {**{k: v for k, v in summary.items()
               if k not in ("sketches", "bloom", "source", "level", "ts")},
            "sketches": {p: bytes(s.registers) for p, s in summary["sketches"].items()},
            "bloom": bytes(summary["bloom"].data)}


# One aggregator over all eight, against trees of every other shape: pairs of
# pairs of pairs, an uneven split, and a chain eight levels deep. Each level
# goes over the wire, as it would between containers.
def over_the_wire(summary):
    return fs.decode(fs.encode(summary))


flat = fs.merge(leaves, "root", 1, top_n=50)
pairs = [over_the_wire(fs.merge(leaves[i:i + 2], f"a{i}", 1, top_n=50)) for i in range(0, 8, 2)]
quads = [over_the_wire(fs.merge(pairs[i:i + 2], f"b{i}", 2, top_n=50)) for i in range(0, 4, 2)]
binary = fs.merge(quads, "root", 3, top_n=50)
uneven = fs.merge([over_the_wire(fs.merge(leaves[:5], "x", 1, top_n=50)),
                   over_the_wire(fs.merge(leaves[5:], "y", 1, top_n=50))], "root", 2, top_n=50)
chain = leaves[0]
for depth, leaf in enumerate(leaves[1:], 1):
    chain = fs.merge([over_the_wire(chain), leaf], "root", depth, top_n=50)
shuffled = leaves[:]
rng.shuffle(shuffled)
for name, tree in (("binary", binary), ("uneven", uneven), ("chain", chain),
                   ("shuffled", fs.merge(shuffled, "root", 1, top_n=50))):
    assert comparable(tree) == comparable(flat), f"{name} tree disagrees with the flat merge"

# What it says. Counts add; distinct blocks do not, and the difference is the
# copies of blocks held under more than one coordinator.
assert flat["coordinators"] == PODS and flat["servers"] == 10 * PODS
assert flat["blocks_sum"] == PODS * (SHARED + OWN)
assert flat["placements"]["l2|plugin"] == PODS * (SHARED + OWN)
assert flat["bytes"]["l2|plugin-shared"] == PODS * (SHARED + OWN) * 8192
assert flat["ts_oldest"] == 1000.0
error = abs(flat["blocks"] - len(truth)) / len(truth)
assert error < 0.02, f"distinct blocks off by {error:.1%}"
duplicated = flat["blocks_sum"] - flat["blocks"]
assert abs(duplicated - (PODS - 1) * SHARED) < 0.02 * len(truth), duplicated
l1_truth = sum(1 for n in truth if n % 3 == 0)
assert abs(flat["sketches"]["l1"].count() - l1_truth) / l1_truth < 0.02

# The hottest blocks are the shared ones, each counted across all eight pods.
assert [e["hash"] for e in flat["top"][:3]] == [block(0), block(1), block(2)]
assert flat["top"][0] == {"hash": block(0), "accesses": 8 * 1000, "pods": 8, "copies": 8 * 3}

# The filter never denies a block that is there, and rarely claims one that is not.
bloom = flat["bloom"]
assert all(fs.block_hash(block(n)) in bloom for n in rng.sample(sorted(truth), 5000))
false_positives = sum(fs.block_hash(block(n)) in bloom for n in range(5 * 10**7, 5 * 10**7 + 20000))
assert false_positives < 0.02 * 20000, false_positives
assert abs(bloom.fill() ** bloom.k - false_positives / 20000) < 0.01
# Each leaf's own filter answers for that leaf alone: this is what lets a
# lookup be routed down a tree instead of asked of everyone.
own = fs.block_hash(block(3 * 10**6 + 7))
assert [own in leaf["bloom"] for leaf in leaves] == [False, False, True] + [False] * 5

# Merging a summary with itself: sketches and the filter do not move.
twice = fs.merge([leaves[0], leaves[0]], "root", 1)
assert twice["blocks"] == fs.merge([leaves[0]], "root", 1)["blocks"]
assert twice["bloom"].data == leaves[0]["bloom"].data

# Geometry mismatches are refused, not merged into nonsense.
for bad in (lambda: fs.merge([leaves[0], fs.LeafBuilder(bloom_bits=1 << 20).finish("z", "", 1)],
                             "root", 1),
            lambda: fs.HLL(16).merge(fs.HLL(12)),
            lambda: fs.decode(b'{"v": 99}')):
    try:
        bad()
    except ValueError:
        continue
    raise AssertionError("a mismatch was accepted")

assert fs.merge([], "empty", 1)["blocks"] == 0
print(f"fleet_summary ok: {len(truth)} distinct blocks estimated as {flat['blocks']} "
      f"({error:.2%} off), {len(fs.encode(flat)) / 1024:.0f} KiB on the wire")

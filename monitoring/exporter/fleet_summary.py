"""Summaries of a key directory that can be added together, level upon level.

A coordinator knows every block its own servers hold and nothing about anyone
else's. To say anything about the fleet -- how many distinct blocks there are,
which are reused most, which coordinator to ask about one -- something has to
combine what the coordinators know, and it cannot do that by collecting their
keys: that is the single directory the fleet outgrew, one level up.

A summary is what a directory can say about itself in a bounded number of
bytes, built so that the summary of several directories is computed from their
summaries alone:

  counts    servers, placements and bytes by tier and backend: these add.
  sketches  HyperLogLog registers over block hashes, for "how many distinct
            blocks", overall and per place a block can be held. Two sketches
            merge by taking the larger of each register, and the result is the
            sketch of the union -- a block held under ten coordinators counts
            once. About 0.4% error at 64 KB a sketch.
  top       the most accessed blocks, with their counts. Lists merge by adding
            the counts of blocks that appear in more than one. This one is
            approximate in a way the others are not: a block outside every
            child's list but large in total is missed, so lists are kept much
            longer than what is finally shown.
  filter    a Bloom filter over block hashes, for "who might hold this
            block?". Filters of one geometry merge by OR.

Every operation is associative and commutative, and merging a summary with
itself changes nothing but the counts. So the result does not depend on how the
coordinators are grouped, and an aggregator's output is the same kind of thing
as its input: aggregators can be stacked to any depth.

Standard library only, so it runs in the exporter's bare Python image.
"""
import base64
import hashlib
import heapq
import json
import math
import time
import zlib

VERSION = 1
HLL_P = 16                    # 2^16 one-byte registers: 64 KB, ~0.4% error
BLOOM_BITS = 1 << 23          # 1 MiB of bits: ~1% false positives at 870,000 blocks
BLOOM_K = 7
TOP_N = 100
# Where a block can be held, as far as distinct counts go. "all" is every block
# with a placement anywhere.
PLACES = ("all", "l1", "l2", "l2_shared")


def block_hash(chunk_hash_hex):
    """64 well-mixed bits for a block, from the chunk hash every key carries.

    Hashed again rather than sliced: nothing here should depend on how long
    LMCache's own hash is or how it is distributed.
    """
    return int.from_bytes(
        hashlib.blake2b(chunk_hash_hex.encode(), digest_size=8).digest(), "big")


def place_of(tier, backend):
    """Which PLACES entry a placement counts under, beside "all"."""
    if tier == "l1":
        return "l1"
    return "l2_shared" if backend.endswith("-shared") else "l2"


def _pack(raw):
    return base64.b64encode(zlib.compress(bytes(raw), 1)).decode()


def _unpack(text):
    return bytearray(zlib.decompress(base64.b64decode(text)))


class HLL:
    """HyperLogLog over 64-bit hashes."""

    __slots__ = ("p", "registers")

    def __init__(self, p=HLL_P, registers=None):
        self.p = p
        self.registers = bytearray(1 << p) if registers is None else bytearray(registers)
        if len(self.registers) != 1 << p:
            raise ValueError(f"a p={p} sketch has {1 << p} registers, not {len(self.registers)}")

    def add(self, h):
        width = 64 - self.p
        index = h >> width
        rank = width - (h & ((1 << width) - 1)).bit_length() + 1
        if rank > self.registers[index]:
            self.registers[index] = rank

    def merge(self, other):
        if other.p != self.p:
            raise ValueError(f"cannot merge sketches of p={self.p} and p={other.p}")
        self.registers = bytearray(map(max, self.registers, other.registers))

    def count(self):
        m = 1 << self.p
        estimate = (0.7213 / (1 + 1.079 / m)) * m * m / sum(2.0 ** -r for r in self.registers)
        zeros = self.registers.count(0)
        if estimate <= 2.5 * m and zeros:
            # Few enough blocks that empty registers say more than full ones.
            estimate = m * math.log(m / zeros)
        return round(estimate)


class Bloom:
    """Bloom filter over 64-bit hashes; filters of one geometry merge by OR."""

    __slots__ = ("bits", "k", "data")

    def __init__(self, bits=BLOOM_BITS, k=BLOOM_K, data=None):
        if bits % 8:
            raise ValueError("a filter's size must be a whole number of bytes")
        self.bits, self.k = bits, k
        self.data = bytearray(bits // 8) if data is None else bytearray(data)
        if len(self.data) != bits // 8:
            raise ValueError(f"a {bits}-bit filter is {bits // 8} bytes, not {len(self.data)}")

    def _positions(self, h):
        # Two halves of one hash stand in for k independent ones.
        low, high = h & 0xFFFFFFFF, (h >> 32) | 1
        return ((low + i * high) % self.bits for i in range(self.k))

    def add(self, h):
        data = self.data
        for position in self._positions(h):
            data[position >> 3] |= 1 << (position & 7)

    def __contains__(self, h):
        data = self.data
        return all(data[position >> 3] & (1 << (position & 7))
                   for position in self._positions(h))

    def merge(self, other):
        if (other.bits, other.k) != (self.bits, self.k):
            raise ValueError(
                f"cannot merge a {self.bits}-bit k={self.k} filter with a "
                f"{other.bits}-bit k={other.k} one: every level of a tree must "
                "use the same filter geometry")
        merged = int.from_bytes(self.data, "little") | int.from_bytes(other.data, "little")
        self.data = bytearray(merged.to_bytes(len(self.data), "little"))

    def fill(self):
        """Fraction of bits set. Raised to the k, it is the false-positive rate."""
        return int.from_bytes(self.data, "little").bit_count() / self.bits


class LeafBuilder:
    """Builds the summary of one coordinator's directory, a key at a time."""

    def __init__(self, bloom_bits=BLOOM_BITS, top_n=TOP_N):
        self.sketches = {place: HLL() for place in PLACES}
        self.bloom = Bloom(bloom_bits)
        self.top_n = top_n
        self._top = []                # min-heap of (accesses, hash, copies)
        self.blocks = 0
        self.placements, self.bytes = {}, {}

    def add(self, chunk_hash_hex, accesses, placements):
        """One block: `placements` is [(tier, backend, size_bytes), ...]."""
        h = block_hash(chunk_hash_hex)
        self.blocks += 1
        self.sketches["all"].add(h)
        self.bloom.add(h)
        for tier, backend, size in placements:
            self.sketches[place_of(tier, backend)].add(h)
            where = f"{tier}|{backend}"
            self.placements[where] = self.placements.get(where, 0) + 1
            self.bytes[where] = self.bytes.get(where, 0) + size
        entry = (accesses, chunk_hash_hex, len(placements))
        if len(self._top) < self.top_n:
            heapq.heappush(self._top, entry)
        elif entry > self._top[0]:
            heapq.heapreplace(self._top, entry)

    def finish(self, source, coordinator, servers, now=None):
        now = time.time() if now is None else now
        return {
            "v": VERSION, "source": source, "level": 0, "coordinator": coordinator,
            "ts": now, "ts_oldest": now,
            "coordinators": 1, "coordinators_stale": 0, "servers": servers,
            # Exact for one directory; an estimate from the sketch once merged.
            "blocks": self.blocks, "blocks_sum": self.blocks,
            "placements": self.placements, "bytes": self.bytes,
            "sketches": self.sketches, "bloom": self.bloom,
            "top": [{"hash": chunk_hash, "accesses": accesses, "pods": 1, "copies": copies}
                    for accesses, chunk_hash, copies in self._top],
        }


def merge(summaries, source, level, top_n=TOP_N, now=None):
    """The summary of everything under `summaries`, from the summaries alone."""
    now = time.time() if now is None else now
    out = {
        "v": VERSION, "source": source, "level": level, "coordinator": None,
        "ts": now, "ts_oldest": min((s["ts_oldest"] for s in summaries), default=now),
        "coordinators": sum(s["coordinators"] for s in summaries),
        "coordinators_stale": sum(s["coordinators_stale"] for s in summaries),
        "servers": sum(s["servers"] for s in summaries),
        "blocks_sum": sum(s["blocks_sum"] for s in summaries),
        "placements": {}, "bytes": {},
        "sketches": {place: HLL() for place in PLACES},
        "bloom": None,
    }
    top = {}
    for s in summaries:
        for field in ("placements", "bytes"):
            for where, value in s[field].items():
                out[field][where] = out[field].get(where, 0) + value
        for place in PLACES:
            out["sketches"][place].merge(s["sketches"][place])
        if out["bloom"] is None:
            out["bloom"] = Bloom(s["bloom"].bits, s["bloom"].k, s["bloom"].data)
        else:
            out["bloom"].merge(s["bloom"])
        for entry in s["top"]:
            seen = top.setdefault(entry["hash"], {"hash": entry["hash"], "accesses": 0,
                                                  "pods": 0, "copies": 0})
            for field in ("accesses", "pods", "copies"):
                seen[field] += entry[field]
    if out["bloom"] is None:
        out["bloom"] = Bloom()
    out["blocks"] = out["sketches"]["all"].count()
    # Most accessed first; the hash breaks ties so the order is the same
    # whatever order the children were merged in.
    out["top"] = sorted(top.values(), key=lambda e: (-e["accesses"], e["hash"]))[:top_n]
    return out


def encode(summary):
    """A summary as JSON bytes: what `/summary` serves."""
    wire = dict(summary)
    wire["sketches"] = {"p": HLL_P, **{place: _pack(sketch.registers)
                                       for place, sketch in summary["sketches"].items()}}
    bloom = summary["bloom"]
    wire["bloom"] = {"bits": bloom.bits, "k": bloom.k, "data": _pack(bloom.data)}
    return json.dumps(wire, separators=(",", ":")).encode()


def decode(raw):
    summary = json.loads(raw)
    if summary.get("v") != VERSION:
        raise ValueError(f"summary version {summary.get('v')!r}, this reads {VERSION}")
    p = summary["sketches"]["p"]
    summary["sketches"] = {place: HLL(p, _unpack(summary["sketches"][place]))
                           for place in PLACES}
    bloom = summary["bloom"]
    summary["bloom"] = Bloom(bloom["bits"], bloom["k"], _unpack(bloom["data"]))
    return summary

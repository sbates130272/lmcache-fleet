#!/usr/bin/env python3
"""Build and analyse token traces for `stim.py --op trace`.

  convert    Exgentic agent-llm-traces parquet -> aiperf mooncake_trace JSONL
             (full messages and tools, so `aiperf profile --custom-dataset-type
             mooncake_trace` can replay the same file against a real engine)
  tokenize   mooncake_trace JSONL -> token trace, through a model's chat template
  synth      an artificial token trace: shared system prompt, per-group context,
             growing turns, compaction
  analyze    what an unbounded cache could reuse, by where the reuse comes from
             and by how many servers share a tier -- no fleet needed
  report     merge the per-request logs of a fleet run into one table

Runs inside the lmcache image, which already carries transformers; `convert`
also needs pyarrow (`make trace-deps` installs it under data/traces/cache).
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import random
import re
import sys

import numpy as np
import tracelib

# ---------------------------------------------------------------- convert ----

def _openai_messages(otel_messages, system):
    """OTel GenAI multi-part messages -> OpenAI chat messages.

    Thinking parts are dropped: a client does not send reasoning back, so it is
    not part of the prompt the next request is cached under.
    """
    out = [{"role": "system", "content": system}] if system else []
    for message in otel_messages:
        text, calls, results = [], [], []
        for part in message.get("parts", []):
            kind = part.get("type")
            if kind == "text":
                text.append(part.get("content") or "")
            elif kind == "tool_call":
                calls.append({"id": part.get("id") or "", "type": "function",
                              "function": {"name": part.get("name") or "",
                                           "arguments": part.get("arguments") or {}}})
            elif kind == "tool_call_response":
                result = part.get("result")
                results.append({"role": "tool", "tool_call_id": part.get("id") or "",
                                "content": result if isinstance(result, str)
                                else json.dumps(result)})
        role = message.get("role", "user")
        if calls:
            out.append({"role": "assistant", "content": "".join(text), "tool_calls": calls})
        elif text or not results:
            out.append({"role": role, "content": "".join(text)})
        out.extend(results)
    return out


def _openai_tools(definitions):
    return [{"type": "function",
             "function": {k: d[k] for k in ("name", "description", "parameters") if k in d}}
            for d in definitions or []]


def _seconds(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


def cmd_convert(args):
    import pyarrow.parquet as pq

    shards = sorted(glob.glob(os.path.join(args.input, "*.parquet")))
    if not shards:
        sys.exit(f"no .parquet shards under {args.input}")
    os.makedirs(args.out, exist_ok=True)
    kept = requests = 0
    groups = collections.Counter()
    with open(os.path.join(args.out, "mooncake.jsonl"), "w") as rows, \
            open(os.path.join(args.out, "sessions.jsonl"), "w") as sessions:
        for shard in shards:
            parquet = pq.ParquetFile(shard)
            for row_group in range(parquet.num_row_groups):
                for row in parquet.read_row_group(row_group).to_pylist():
                    if args.sessions and kept >= args.sessions:
                        break
                    if args.benchmark and row["benchmark"] not in args.benchmark:
                        continue
                    if args.harness and row["harness"] not in args.harness:
                        continue
                    spans = [s for s in row["spans"]
                             if s["status"]["code"] != 2 and not s["attributes"]["error.type"]]
                    spans.sort(key=lambda s: s["start_time"])
                    if len(spans) < args.min_turns:
                        continue
                    peak = max(s["attributes"]["gen_ai.usage.input_tokens"] or 0 for s in spans)
                    if args.max_input_tokens and peak > args.max_input_tokens:
                        continue
                    group = f'{row["benchmark"]}/{row["harness"]}'
                    start = _seconds(spans[0]["start_time"])
                    for span in spans:
                        a = span["attributes"]
                        line = {
                            "session_id": row["session_id"],
                            # Start times, relative to the session's first
                            # request: start-to-start, so a replay needs no
                            # model of how long each response took.
                            "timestamp": round((_seconds(span["start_time"]) - start) * 1000),
                            "messages": _openai_messages(
                                json.loads(a["gen_ai.input.messages"]),
                                a["gen_ai.system_instructions"]),
                            "output_length": a["gen_ai.usage.output_tokens"] or 1,
                        }
                        tools = _openai_tools(json.loads(a["gen_ai.tool.definitions"] or "[]"))
                        if tools:
                            line["tools"] = tools
                        rows.write(json.dumps(line, separators=(",", ":")) + "\n")
                        requests += 1
                    sessions.write(json.dumps(
                        {"session_id": row["session_id"], "group": group,
                         "benchmark": row["benchmark"], "harness": row["harness"],
                         "models": row["models"], "requests": len(spans),
                         "peak_input_tokens": peak}) + "\n")
                    kept += 1
                    groups[group] += 1
                if args.sessions and kept >= args.sessions:
                    break
            if args.sessions and kept >= args.sessions:
                break
    print(f"wrote {args.out}/mooncake.jsonl: {kept} sessions, {requests} requests")
    for group, count in groups.most_common():
        print(f"  {group}: {count} sessions")


# --------------------------------------------------------------- tokenize ----

class SegmentTokenizer:
    """Tokenize rendered prompts, remembering every segment between special tokens.

    A session resends its history each turn, and many sessions open with the
    same system prompt and tools, so almost all of the text of a request has
    been tokenized before. Special tokens are hard split points for a
    tokenizer, so the pieces between them tokenize the same alone as in
    context; caching by piece turns a quadratic job into a linear one.
    """

    def __init__(self, name):
        from transformers import AutoTokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(name)
        specials = sorted(set(self.tokenizer.all_special_tokens), key=len, reverse=True)
        self._split = re.compile("(" + "|".join(map(re.escape, specials)) + ")") \
            if specials else None
        self._cache = {}
        self._cached_tokens = 0
        self.hits = self.misses = 0

    def render(self, messages, tools):
        return self.tokenizer.apply_chat_template(
            messages, tools=tools or None, tokenize=False, add_generation_prompt=True)

    def encode_whole(self, text):
        return np.asarray(self.tokenizer.encode(text, add_special_tokens=False), dtype="<u4")

    def encode(self, text, cache_limit=64_000_000):
        if self._split is None:
            return self.encode_whole(text)
        pieces = []
        for segment in self._split.split(text):
            if not segment:
                continue
            key = hashlib.blake2b(segment.encode(), digest_size=16).digest()
            tokens = self._cache.get(key)
            if tokens is None:
                self.misses += 1
                tokens = self.encode_whole(segment)
                if self._cached_tokens > cache_limit:
                    self._cache.clear()
                    self._cached_tokens = 0
                self._cache[key] = tokens
                self._cached_tokens += len(tokens)
            else:
                self.hits += 1
            pieces.append(tokens)
        return np.concatenate(pieces) if pieces else np.zeros(0, "<u4")


def cmd_tokenize(args):
    source = os.path.join(args.input, "mooncake.jsonl") if os.path.isdir(args.input) \
        else args.input
    groups = {}
    sidecar = os.path.join(os.path.dirname(source), "sessions.jsonl")
    if os.path.exists(sidecar):
        with open(sidecar) as f:
            groups = {s["session_id"]: s["group"] for s in map(json.loads, f)}
    tok = SegmentTokenizer(args.tokenizer)
    writer = tracelib.TraceWriter(args.out, {
        "source": os.path.abspath(source), "tokenizer": args.tokenizer, "kind": "tokenized"})
    current, clock, verified, failed = None, 0.0, 0, 0
    with open(source) as f:
        for line in f:
            row = json.loads(line)
            sid = row.get("session_id") or f"row-{writer.requests}"
            if sid != current:
                if current is not None:
                    writer.end_session(current)
                current, clock = sid, 0.0
            # mooncake allows an absolute timestamp or a delay since the last turn
            if "delay" in row and "timestamp" not in row:
                clock += row["delay"] / 1000.0
            else:
                clock = row.get("timestamp", 0) / 1000.0
            try:
                text = tok.render(row["messages"], row.get("tools"))
            except Exception as e:      # a template that rejects this message shape
                failed += 1
                if failed <= 3:
                    print(f"  skipped a request of {sid}: {type(e).__name__}: {e}",
                          file=sys.stderr)
                continue
            tokens = tok.encode(text)
            if verified < args.verify:
                verified += 1
                whole = tok.encode_whole(text)
                if len(whole) != len(tokens) or (whole != tokens).any():
                    sys.exit("segment-wise tokenization disagrees with whole-prompt "
                             f"tokenization for {args.tokenizer}; its special tokens "
                             "are not clean split points")
            if args.max_tokens and len(tokens) > args.max_tokens:
                tokens = tokens[:args.max_tokens]
            writer.add(sid, groups.get(sid, "default"), clock, tokens,
                       row.get("output_length", 0))
            if writer.requests % 500 == 0:
                print(f"  {writer.requests} requests, "
                      f"{writer.total_tokens / 1e6:.1f}M tokens", flush=True)
    meta = writer.close()
    print(f"wrote {args.out}: {meta['sessions']} sessions, {meta['requests']} requests, "
          f"{meta['total_tokens'] / 1e6:.1f}M prompt tokens "
          f"({meta['stored_tokens'] / 1e6:.1f}M stored), {failed} skipped; "
          f"segment cache {tok.hits} hits / {tok.misses} misses; "
          f"{verified} prompts verified against whole-prompt tokenization")


# ------------------------------------------------------------------ synth ----

def cmd_synth(args):
    """An artificial trace with the structure agent sessions have.

    Every session opens with one system prompt (shared by all), then a context
    its group shares (a repo, a tool set), then its own turns. Each turn adds
    tokens; now and then a session compacts, replacing its history with a short
    summary, which is what throws away a long cached prefix in real agents.
    """
    rng = random.Random(args.seed)
    nprng = np.random.default_rng(args.seed)

    def fresh(n):
        return nprng.integers(1000, 100_000, size=max(int(n), 0), dtype="<u4")

    system = fresh(args.system_tokens)
    group_context = [fresh(args.group_tokens) for _ in range(args.groups)]
    writer = tracelib.TraceWriter(args.out, {
        "kind": "synthetic",
        "args": {k: v for k, v in vars(args).items() if k not in ("func", "cmd", "out")}})
    for s in range(args.sessions):
        group = rng.randrange(args.groups)
        sid = f"synth-{s:05d}"
        base = np.concatenate((system, group_context[group]))
        tokens, t = base, 0.0
        for _turn in range(max(1, round(rng.expovariate(1 / args.turns)))):
            if len(tokens) > len(base) and rng.random() < args.compact_prob:
                tokens = np.concatenate((base, fresh(args.turn_tokens)))
            tokens = np.concatenate((tokens, fresh(rng.expovariate(1 / args.turn_tokens))))
            if len(tokens) > args.max_tokens:
                tokens = np.concatenate((base, fresh(args.turn_tokens)))
            writer.add(sid, f"group-{group}", t, tokens, out=0)
            t += rng.expovariate(1 / args.think_seconds)
        writer.end_session(sid)
    meta = writer.close()
    print(f"wrote {args.out}: {meta['sessions']} sessions, {meta['requests']} requests, "
          f"{meta['total_tokens'] / 1e6:.1f}M prompt tokens")


# ---------------------------------------------------------------- analyze ----

def _replay_order(trace, args):
    """Yield (due, session index, request index, tokens, keys) in replay order."""
    state = {}
    for due, s, i in tracelib.schedule(trace, args.concurrency, args.idle_cap,
                                       args.speedup, max_sessions=args.sessions):
        request = trace.sessions[s][2][i]
        previous_tokens, previous_keys = state.get(s, (None, ()))
        tokens = trace.tokens(request, previous_tokens)
        keys = tracelib.chunk_keys(tokens, args.chunk_size, previous_keys, request["keep"])
        state[s] = (tokens, keys)
        if i == len(trace.sessions[s][2]) - 1:
            del state[s]
        yield due, s, i, keys


def _prefix_hits(keys, present):
    """How many leading chunks are present: a lookup stops at the first miss."""
    for n, key in enumerate(keys):
        if key not in present:
            return n
    return len(keys)


def cmd_analyze(args):
    trace = tracelib.Trace(args.trace)
    labels = tracelib.gap_labels()
    first = {}                         # key -> (session, group) that created it
    last = {}                          # key -> replay time it was last used
    by_source = collections.Counter()
    gaps = collections.Counter()
    total = requests = 0
    # One store per (route, sharing) case: which chunks each server can reach.
    servers = sorted({1, *args.servers})
    cases = {(r, n, shared): [set() for _ in range(1 if shared else n)]
             for r in tracelib.ROUTES for n in servers for shared in (False, True)
             if n > 1 or (r == "session" and not shared)}
    case_hits = collections.Counter()

    for due, s, i, keys in _replay_order(trace, args):
        sid, group, _ = trace.sessions[s]
        requests += 1
        total += len(keys)
        hit = _prefix_hits(keys, first)
        for key in keys[:hit]:
            owner_session, owner_group = first[key]
            by_source["same session" if owner_session == s else
                      "another session, same group" if owner_group == group else
                      "another group"] += 1
            gaps[tracelib.gap_bucket((due - last[key]) * args.speedup)] += 1
        by_source["new"] += len(keys) - hit
        for key in keys:
            first.setdefault(key, (s, group))
            last[key] = due
        for (policy, n, shared), stores in cases.items():
            store = stores[0 if shared else tracelib.route(policy, sid, group, i, n)]
            case_hits[policy, n, shared] += _prefix_hits(keys, store)
            store.update(keys)

    if not total:
        sys.exit("the trace has no full chunks at this chunk size")
    print(f"{args.trace}: {requests} requests, {total} chunks of {args.chunk_size} tokens, "
          f"{len(first)} distinct ({len(first) / total:.1%})")
    print("\nWith an unbounded cache every server can reach, a chunk is:")
    for source in ("same session", "another session, same group", "another group", "new"):
        print(f"  {source:<30} {by_source[source]:>10}  {by_source[source] / total:6.1%}")
    hits = total - by_source["new"]
    print("\nTime since a reused chunk was last touched (trace seconds):")
    for bucket, label in enumerate(labels):
        if hits:
            print(f"  {label:<10} {gaps[bucket]:>10}  {gaps[bucket] / hits:6.1%}")
    print("\nHit rate by routing and by what the servers share (unbounded cache):")
    print(f"  {'servers':>7}  {'route':<8} {'local L2 only':>14} {'shared L2':>10}")
    for n in servers:
        for policy in tracelib.ROUTES:
            if (policy, n, False) in cases:
                local = case_hits[policy, n, False] / total
                shared = case_hits[policy, n, True] / total if (policy, n, True) in cases \
                    else local
                print(f"  {n:>7}  {policy:<8} {local:>13.1%} {shared:>10.1%}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"requests": requests, "chunks": total, "distinct": len(first),
                       "by_source": dict(by_source),
                       "gap_histogram": {labels[b]: c for b, c in gaps.items()},
                       "hit_rate": {f"{p}/{n}/{'shared' if sh else 'local'}": h / total
                                    for (p, n, sh), h in case_hits.items()}}, f, indent=2)


# ----------------------------------------------------------------- report ----

def cmd_report(args):
    logs = sorted(glob.glob(os.path.join(args.run, "*.jsonl")))
    if not logs:
        sys.exit(f"no per-request logs under {args.run}")
    fields = ("chunks", "hit", "hit_same_session", "hit_other_session", "hit_remote",
              "lost", "new")
    fleet = collections.Counter()
    print(f"  {'client':<12} {'requests':>8} {'chunks':>9} {'hit':>7} "
          f"{'same sess':>9} {'other sess':>10} {'remote':>7} {'lost':>7}")
    for log in logs:
        tally = collections.Counter()
        with open(log) as f:
            for row in map(json.loads, f):
                tally["requests"] += 1
                for field in fields:
                    tally[field] += row.get(field, 0)
        fleet.update(tally)
        _report_row(os.path.basename(log)[:-6], tally)
    _report_row("fleet", fleet)
    if args.require_remote and not fleet["hit_remote"]:
        sys.exit("FAIL: no hit came from another server; the shared tier served nothing")


def _report_row(name, t):
    chunks = t["chunks"] or 1
    print(f"  {name:<12} {t['requests']:>8} {t['chunks']:>9} {t['hit'] / chunks:>7.1%} "
          f"{t['hit_same_session'] / chunks:>9.1%} {t['hit_other_session'] / chunks:>10.1%} "
          f"{t['hit_remote'] / chunks:>7.1%} {t['lost'] / chunks:>7.1%}")


# ------------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("convert", help="Exgentic parquet -> mooncake_trace JSONL")
    p.add_argument("input", help="directory of .parquet shards")
    p.add_argument("out", help="output directory")
    p.add_argument("--sessions", type=int, default=0, help="keep the first N that qualify")
    p.add_argument("--benchmark", action="append", help="keep only this benchmark (repeatable)")
    p.add_argument("--harness", action="append", help="keep only this harness (repeatable)")
    p.add_argument("--min-turns", type=int, default=2)
    p.add_argument("--max-input-tokens", type=int, default=0,
                   help="drop sessions whose recorded peak input exceeds this")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("tokenize", help="mooncake_trace JSONL -> token trace")
    p.add_argument("input", help="mooncake JSONL, or a directory holding mooncake.jsonl")
    p.add_argument("out", help="token trace directory")
    p.add_argument("--tokenizer", default="Qwen/Qwen2.5-7B-Instruct",
                   help="Hugging Face tokenizer of the model you would serve")
    p.add_argument("--max-tokens", type=int, default=0, help="truncate longer prompts")
    p.add_argument("--verify", type=int, default=20,
                   help="check this many prompts against whole-prompt tokenization")
    p.set_defaults(func=cmd_tokenize)

    p = sub.add_parser("synth", help="generate an artificial token trace")
    p.add_argument("out")
    p.add_argument("--sessions", type=int, default=40)
    p.add_argument("--groups", type=int, default=4)
    p.add_argument("--turns", type=float, default=12, help="mean requests per session")
    p.add_argument("--system-tokens", type=int, default=4096, help="shared by every session")
    p.add_argument("--group-tokens", type=int, default=8192, help="shared within a group")
    p.add_argument("--turn-tokens", type=float, default=1500, help="mean tokens added per turn")
    p.add_argument("--think-seconds", type=float, default=20, help="mean gap between requests")
    p.add_argument("--compact-prob", type=float, default=0.03)
    p.add_argument("--max-tokens", type=int, default=120_000)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_synth)

    p = sub.add_parser("analyze", help="reuse an unbounded cache could achieve")
    p.add_argument("trace")
    p.add_argument("--chunk-size", type=int, default=256)
    p.add_argument("--servers", type=int, nargs="*", default=[2, 4, 8])
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--idle-cap", type=float, default=60.0)
    p.add_argument("--speedup", type=float, default=1.0)
    p.add_argument("--sessions", type=int, default=None)
    p.add_argument("--json", help="also write the numbers here")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("report", help="merge a fleet run's per-request logs")
    p.add_argument("run", help="directory of trace-N.jsonl logs")
    p.add_argument("--require-remote", action="store_true",
                   help="exit non-zero unless some hit came from another server")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

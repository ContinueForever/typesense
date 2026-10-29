#!/usr/bin/env python3
"""Reproducible Typesense prefix study. Python standard library only."""
from __future__ import annotations

import argparse
import collections
import contextlib
import gzip
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
UPSTREAM_SHA = "5dbf03ca8d44845746562cf59487698eb73161b9"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def checked_output(*command, cwd=None):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    return {"command": list(command), "exit_code": result.returncode,
            "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def environment(source=None, binary=None):
    info = {"recorded_at_utc": utc_now(), "platform": platform.platform(),
            "python": sys.version, "cpu_count": os.cpu_count(),
            "cpu": checked_output("lscpu"),
            "memory": Path("/proc/meminfo").read_text(),
            "storage": dict(zip(("total", "used", "free"), shutil.disk_usage(ROOT))),
            "compiler": checked_output("g++", "--version"),
            "os_release": Path("/etc/os-release").read_text()}
    if source:
        info["source"] = {"path": str(Path(source).resolve()),
                          "head": checked_output("git", "rev-parse", "HEAD", cwd=source),
                          "status": checked_output("git", "status", "--short", cwd=source),
                          "diff": checked_output("git", "diff", "--", "src", "include", "test", cwd=source)}
    if binary:
        info["binary"] = {"path": str(Path(binary).resolve()), "sha256": sha256(binary)}
    return info


def document_stream(documents, target_documents, target_matches, other_unique, hot_terms, seed):
    """Target documents stay byte-identical when only foreign cardinality grows.

    Codes are synthetic 12 digit strings. Disjoint ranges avoid accidental sharing.
    The seed permutes each numeric range bijectively, without random module versions.
    """
    for i in range(target_documents):
        hot = i < target_matches
        suffix = (i * 104729 + seed) % 100_000_000
        yield {"id": f"target-{i:09d}", "orgId": "target",
               "item_code": ("918" if hot else "625") + f"{suffix:09d}",
               "title": "synthetic target item", "arc": False, "uOn": i + 1}
    for i in range(documents - target_documents):
        term = i % other_unique
        suffix = 100_000_000 + (term * 104729 + seed) % 100_000_000
        yield {"id": f"foreign-{i:09d}", "orgId": "other",
               "item_code": ("918" if term < hot_terms else "625") + f"{suffix:09d}",
               "title": "synthetic foreign item", "arc": i % 2 == 0,
               "uOn": 1_000_000_000 + i}


def multiset_digest(documents):
    # Order independent export verification, including duplicate multiplicities.
    total, count = 0, 0
    for document in documents:
        total = (total + int(hashlib.sha256(canonical(document).encode()).hexdigest(), 16)) % (1 << 256)
        count += 1
    return {"count": count, "sha256_sum_mod_2_256": f"{total:064x}"}


def query_matrix(documents, unique_terms):
    fixed = {"query_by": "item_code", "prefix": "true", "num_typos": 0,
             "infix": "off", "use_cache": "false", "enable_synonyms": "false",
             "split_join_tokens": "off", "drop_tokens_threshold": 0,
             "typo_tokens_threshold": 0, "per_page": 30, "page": 1,
             "sort_by": "arc:asc,uOn:desc", "search_cutoff_ms": 5000}
    cases = []
    for prefix in ("9", "91", "918", "9180", "918000", "777"):
        for label, filter_by in (("empty", "orgId:=absent"), ("narrow", "orgId:=target"),
                                  ("wide", "orgId:=other"), ("unfiltered", None)):
            for candidates in (1, 4, 16):
                params = dict(fixed, q=prefix, max_candidates=candidates)
                if filter_by:
                    params["filter_by"] = filter_by
                cases.append({"id": f"p{prefix}-{label}-k{candidates}", "params": params})
    cases.append({"id": "exact-control", "params": dict(fixed, q="918", prefix="false", max_candidates=4)})
    for sort_label, sort_by in (("score", "_text_match:desc,uOn:desc"), ("default", None)):
        params = dict(fixed, q="918", filter_by="orgId:=target", max_candidates=4)
        if sort_by:
            params["sort_by"] = sort_by
        else:
            params.pop("sort_by")
        cases.append({"id": f"narrow-{sort_label}", "params": params})
    # Only these explicitly full-enumeration cases are eligible for the independent oracle.
    if documents <= 5000:
        for label, filter_by in (("narrow", "orgId:=target"), ("empty", "orgId:=absent"), ("all", None)):
            params = dict(fixed, q="918", sort_by="uOn:desc", max_candidates=unique_terms + 1)
            if filter_by:
                params["filter_by"] = filter_by
            cases.append({"id": f"oracle-{label}", "oracle": True, "params": params})
    return cases


def generate(args):
    foreign = args.documents - args.target_documents
    other_unique = foreign if args.other_unique is None else args.other_unique
    hot_terms = other_unique if args.hot_terms is None else args.hot_terms
    if not (0 <= args.target_matches <= args.target_documents <= args.documents and
            0 <= hot_terms <= other_unique <= foreign and
            (foreign == 0 or other_unique > 0) and
            args.target_documents < 100_000_000 and other_unique < 100_000_000):
        raise ValueError("Invalid N/F/matches/U/V: require 0<=matches<=F<=N, 0<=V<=U<=N-F, U>0 for foreign docs")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    config = {"documents": args.documents, "target_documents": args.target_documents,
              "target_matches": args.target_matches, "other_unique": other_unique,
              "hot_terms": hot_terms, "seed": args.seed}
    with (output / "documents.jsonl").open("w") as stream:
        for doc in document_stream(**config):
            stream.write(canonical(doc) + "\n")
    schema = {"name": "assigned-at-run-time", "default_sorting_field": "uOn", "fields": [
        {"name": "orgId", "type": "string", "facet": True},
        {"name": "item_code", "type": "string", "infix": args.infix_index},
        {"name": "title", "type": "string"}, {"name": "arc", "type": "bool"},
        {"name": "uOn", "type": "int64"}]}
    save_json(output / "schema.json", schema)
    save_json(output / "queries.json", query_matrix(args.documents, args.target_documents + other_unique))
    save_json(output / "manifest.json", {"format": 1, "generator": "affine-v1",
        "config": config, "infix_index": args.infix_index,
        "documents_sha256": sha256(output / "documents.jsonl"),
        "documents_bytes": (output / "documents.jsonl").stat().st_size,
        "content_digest": multiset_digest(document_stream(**config)),
        "note": "Synthetic identifiers; target set is constant across foreign cardinalities."})
    print(output.resolve())


class Client:
    def __init__(self, url, key, timeout=30):
        self.url, self.key, self.timeout = url, key, timeout

    def request(self, method, path, body=None, params=None):
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if isinstance(body, (dict, list)):
            body = canonical(body).encode()
        req = urllib.request.Request(url, data=body, method=method,
                    headers={"X-TYPESENSE-API-KEY": self.key, "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=self.timeout)

    def json(self, method, path, body=None, params=None):
        with self.request(method, path, body, params) as response:
            return json.load(response)


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def owned_server(binary, output, timeout, cpu_set=None):
    output = Path(output)
    data = output / "server-data"
    data.mkdir()
    key = uuid.uuid4().hex
    api_port, peer_port = unused_port(), unused_port()
    while peer_port == api_port:
        peer_port = unused_port()
    command = [str(Path(binary).resolve()), "--data-dir", str(data.resolve()), "--api-key", key,
               "--api-address", "127.0.0.1", "--api-port", str(api_port),
               "--peering-address", "127.0.0.1", "--peering-port", str(peer_port), "--thread-pool-size", "2"]
    if cpu_set is not None:
        if not cpu_set or not set(cpu_set) <= os.sched_getaffinity(0):
            raise ValueError("Server CPU set must be nonempty and available to this process")
        command = ["taskset", "--cpu-list", ",".join(str(cpu) for cpu in sorted(cpu_set))] + command
    save_json(output / "server.json", {"command": ["<ephemeral-key>" if x == key else x for x in command],
                                      "ownership": "Only this Popen child is terminated."})
    log = (output / "server.log").open("w")
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    client = Client(f"http://127.0.0.1:{api_port}", key, timeout)
    client.process = process
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Server exited ({process.returncode}); see {output / 'server.log'}")
            try:
                if client.json("GET", "/health").get("ok"):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("Server did not become healthy in 90s")
        yield client
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.close()
        save_json(output / "server-exit.json", {"exit_code": process.returncode})


def normalise_response(body):
    def hit(h):
        return {"id": h["document"]["id"], **{k: h[k] for k in ("text_match", "text_match_info") if k in h}}
    result = {k: body[k] for k in ("found", "out_of", "search_cutoff", "facet_counts", "code", "message", "error") if k in body}
    if "hits" in body:
        result["hits"] = [hit(h) for h in body["hits"]]
    if "grouped_hits" in body:
        result["grouped_hits"] = [{"group_key": g["group_key"], "found": g.get("found"),
                                   "hits": [hit(h) for h in g["hits"]]} for g in body["grouped_hits"]]
    return result


def sample(client, path, case):
    started = time.perf_counter_ns()
    record = {"query_id": case["id"], "params": case["params"], "started_at_utc": utc_now()}
    try:
        with client.request("GET", path, params=case["params"]) as response:
            record["status"] = response.status
            payload = response.read()
            body = json.loads(payload)
        record["outcome"] = "cutoff" if body.get("search_cutoff") else "success"
        record["engine_ms"] = body.get("search_time_ms")
        record["response"] = body
        record["normalised"] = normalise_response(body)
    except urllib.error.HTTPError as error:
        record.update(status=error.code, outcome="http_error", error_body=error.read().decode(errors="replace"))
    except (TimeoutError, socket.timeout) as error:
        record.update(status=None, outcome="timeout", error=str(error))
    except urllib.error.URLError as error:
        record.update(status=None, outcome="timeout" if isinstance(error.reason, TimeoutError) else "transport_error", error=str(error))
    except (ValueError, KeyError, TypeError) as error:
        record.update(outcome="invalid_response", error=str(error))
        if "payload" in locals():
            record["error_body"] = payload.decode(errors="replace")
    finally:
        record["elapsed_ms"] = (time.perf_counter_ns() - started) / 1_000_000
    return record


def jsonl(path):
    path = Path(path)
    if not path.exists() and Path(str(path) + '.gz').exists():
        path = Path(str(path) + '.gz')
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def process_resources(pid):
    status = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("VmRSS", "VmHWM", "VmSwap", "Threads"):
            status[key] = value.strip()
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return {"recorded_at_utc": utc_now(), "status": status,
            "user_cpu_seconds": int(stat[11]) / os.sysconf("SC_CLK_TCK"),
            "system_cpu_seconds": int(stat[12]) / os.sysconf("SC_CLK_TCK")}


def run(args):
    dataset, output = Path(args.dataset), Path(args.output)
    manifest = json.loads((dataset / "manifest.json").read_text())
    if sha256(dataset / "documents.jsonl") != manifest["documents_sha256"]:
        raise ValueError("Dataset hash mismatch")
    cases = json.loads((dataset / "queries.json").read_text())
    if args.query_id:
        cases = [c for c in cases if c["id"] in args.query_id]
        if {c["id"] for c in cases} != set(args.query_id):
            raise ValueError("Unknown query id")
    output.mkdir(parents=True, exist_ok=False)
    save_json(output / "environment.json", environment(args.source, args.binary))
    save_json(output / "run.json", {"dataset": str(dataset.resolve()), "dataset_manifest": manifest,
        "queries": cases, "warmup": args.warmup, "repeats": args.repeats,
        "load_model": "serial closed loop; client and server share host; not a capacity measurement"})
    with owned_server(args.binary, output, args.timeout) as client:
        save_json(output / "debug.json", client.json("GET", "/debug"))
        schema = json.loads((dataset / "schema.json").read_text())
        schema["name"] = "retrieval_study_" + uuid.uuid4().hex[:12]
        client.json("POST", "/collections", schema)
        base = "/collections/" + schema["name"]
        imported = 0
        with (output / "import.jsonl").open("w") as log, (dataset / "documents.jsonl").open("rb") as source:
            while True:
                batch = []
                for _ in range(args.batch_size):
                    line = source.readline()
                    if not line:
                        break
                    batch.append(line)
                if not batch:
                    break
                with client.request("POST", base + "/documents/import", b"".join(batch), {"action": "create"}) as response:
                    results = [json.loads(line) for line in response if line.strip()]
                for result in results:
                    log.write(canonical(result) + "\n")
                if len(results) != len(batch) or any(r.get("success") is not True for r in results):
                    raise RuntimeError("Import failed; individual responses saved in import.jsonl")
                imported += len(results)
        collection = client.json("GET", base)
        if imported != manifest["config"]["documents"] or collection["num_documents"] != imported:
            raise RuntimeError("Collection count does not match successful imports")
        with client.request("GET", base + "/documents/export") as response:
            exported = multiset_digest(json.loads(line) for line in response if line.strip())
        if exported != manifest["content_digest"]:
            raise RuntimeError("Exported contents differ from generated dataset")
        save_json(output / "import-verification.json", {"imported": imported, "collection": collection,
                                                       "export_digest": exported, "verified": True})
        with (output / "requests.jsonl").open("w", buffering=1) as stream, \
                (output / "resources.jsonl").open("w", buffering=1) as resources:
            for phase, count in (("warmup", args.warmup), ("measure", args.repeats)):
                for round_number in range(count):
                    for case in cases:
                        record = sample(client, base + "/documents/search", case)
                        record.update(phase=phase, round=round_number)
                        stream.write(canonical(record) + "\n")
                    resources.write(canonical({"phase": phase, "round": round_number,
                                               **process_resources(client.process.pid)}) + "\n")
        save_json(output / "complete.json", {"completed_at_utc": utc_now(), "queries": len(cases),
                  "measured_requests": len(cases) * args.repeats})
    summarise_file(output / "requests.jsonl", output / "summary.json")
    print(output.resolve())


def summarise_file(path, output):
    groups = collections.defaultdict(list)
    for record in jsonl(path):
        if record.get("phase") == "measure":
            groups[record["query_id"]].append(record)
    result = {}
    for label, rows in groups.items():
        completed = [r["elapsed_ms"] for r in rows if r["outcome"] == "success"]
        stats = {"attempted": len(rows), "outcomes": dict(collections.Counter(r["outcome"] for r in rows)),
                 "completed_samples": len(completed)}
        if completed:
            ordered = sorted(completed)
            stats.update(median_ms=statistics.median(ordered), min_ms=ordered[0], max_ms=ordered[-1])
            if len(completed) >= 100:
                stats["p95_ms"] = ordered[math.ceil(len(ordered) * 0.95) - 1]
            if len(completed) >= 10_000:
                stats["p99_ms"] = ordered[math.ceil(len(ordered) * 0.99) - 1]
            else:
                stats["p99_note"] = "Not reported: fewer than 10000 successful samples; errors/cutoffs counted separately."
        result[label] = stats
    save_json(output, result)
    return result


def oracle_results(documents, params):
    if params.get("sort_by") != "uOn:desc" or params.get("num_typos") != 0:
        raise ValueError("Oracle supports only the explicitly restricted exact enumeration contract")
    filter_by = params.get("filter_by")
    if filter_by and not filter_by.startswith("orgId:="):
        raise ValueError("Unsupported oracle filter")
    org = filter_by.removeprefix("orgId:=") if filter_by else None
    matches = [doc for doc in documents if (org is None or doc["orgId"] == org) and
               (doc["item_code"].startswith(params["q"]) if params["prefix"] == "true" else doc["item_code"] == params["q"])]
    matches.sort(key=lambda doc: doc["uOn"], reverse=True)
    start = (int(params["page"]) - 1) * int(params["per_page"])
    return {"found": len(matches), "ids": [doc["id"] for doc in matches[start:start + int(params["per_page"])]]}


def verify_oracle(args):
    run_path = Path(args.run)
    metadata = json.loads((run_path / "run.json").read_text())
    if not (run_path / "complete.json").exists():
        raise ValueError("Run is incomplete (complete.json is absent)")
    cases = {c["id"]: c for c in metadata["queries"] if c.get("oracle")}
    if not cases:
        raise ValueError("No full-enumeration oracle cases in this run; truncated candidate modes must not use this oracle")
    data_path = Path(metadata["dataset"]) / "documents.jsonl"
    if sha256(data_path) != metadata["dataset_manifest"]["documents_sha256"]:
        raise ValueError("Dataset changed after the run")
    docs = list(jsonl(data_path))
    for case in cases.values():
        if int(case["params"]["max_candidates"]) < len({d["item_code"] for d in docs}):
            raise ValueError("Candidate limit is too small for full-enumeration oracle")
    checked, failures = 0, []
    seen = set()
    for row in jsonl(run_path / "requests.jsonl"):
        if row["phase"] != "measure" or row["query_id"] not in cases:
            continue
        key = (row["round"], row["query_id"])
        if key in seen:
            raise ValueError("Duplicate oracle sample")
        seen.add(key)
        if row["params"] != cases[row["query_id"]]["params"]:
            raise ValueError("Oracle query parameters differ from the run manifest")
        expected = oracle_results(docs, cases[row["query_id"]]["params"])
        observed = {"found": row.get("normalised", {}).get("found"),
                    "ids": [h["id"] for h in row.get("normalised", {}).get("hits", [])]}
        checked += 1
        if row["outcome"] != "success" or expected != observed:
            failures.append({"query_id": row["query_id"], "expected": expected, "observed": observed, "outcome": row["outcome"]})
    expected = {(r, q) for r in range(metadata["repeats"]) for q in cases}
    if seen != expected:
        raise ValueError("Missing or unexpected oracle samples")
    result = {"checked": checked, "failures": failures, "passed": checked > 0 and not failures}
    save_json(run_path / "oracle.json", result)
    print(canonical(result))
    return 0 if result["passed"] else 1


def compare(args):
    left_dir, right_dir = Path(args.left), Path(args.right)
    left_meta = json.loads((left_dir / "run.json").read_text())
    right_meta = json.loads((right_dir / "run.json").read_text())
    if left_meta["dataset_manifest"] != right_meta["dataset_manifest"] or left_meta["queries"] != right_meta["queries"]:
        raise ValueError("Cannot compare runs with different datasets or queries")
    def collect(folder):
        metadata = json.loads((folder / "run.json").read_text())
        if not (folder / "complete.json").exists():
            raise ValueError("Run is incomplete (complete.json is absent)")
        cases = {c["id"]: c["params"] for c in metadata["queries"]}
        expected = {(r, q) for r in range(metadata["repeats"]) for q in cases}
        rows = {}
        for row in jsonl(folder / "requests.jsonl"):
            if row["phase"] == "measure":
                if row["outcome"] != "success":
                    raise ValueError("Incomplete/cutoff/error responses require separate deadline analysis")
                key = (row["round"], row["query_id"])
                if key in rows:
                    raise ValueError("Duplicate request key in raw samples")
                if row["params"] != cases.get(row["query_id"]):
                    raise ValueError("Recorded query parameters differ from the run manifest")
                rows[key] = {"status": row["status"], "normalised": row["normalised"]}
        if set(rows) != expected:
            raise ValueError("Measured requests do not match the planned query rounds")
        return rows
    left, right = collect(left_dir), collect(right_dir)
    differences = [{"request": key, "left": left.get(key), "right": right.get(key)}
                   for key in sorted(left.keys() | right.keys()) if left.get(key) != right.get(key)]
    result = {"left_requests": len(left), "right_requests": len(right), "differences": differences,
              "passed": bool(left) and not differences}
    save_json(args.output, result)
    print(canonical({k: v for k, v in result.items() if k != "differences"}))
    return 0 if result["passed"] else 1


def positive(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate")
    gen.add_argument("--output", required=True)
    gen.add_argument("--documents", type=positive, default=10_000)
    gen.add_argument("--target-documents", type=int, default=8)
    gen.add_argument("--target-matches", type=int, default=1)
    gen.add_argument("--other-unique", type=int)
    gen.add_argument("--hot-terms", type=int)
    gen.add_argument("--seed", type=int, default=3130)
    gen.add_argument("--infix-index", action="store_true")
    gen.set_defaults(func=generate)
    env = commands.add_parser("environment")
    env.add_argument("--source", default=str(ROOT.parent / "typesense"))
    env.add_argument("--binary")
    env.add_argument("--output", default=str(ROOT / "manifests" / "environment.json"))
    env.set_defaults(func=lambda a: save_json(a.output, environment(a.source, a.binary)))
    runner = commands.add_parser("run")
    runner.add_argument("--binary", required=True)
    runner.add_argument("--source")
    runner.add_argument("--dataset", required=True)
    runner.add_argument("--output", required=True)
    runner.add_argument("--repeats", type=positive, default=5)
    runner.add_argument("--warmup", type=int, default=1)
    runner.add_argument("--batch-size", type=positive, default=1000)
    runner.add_argument("--timeout", type=positive, default=30)
    runner.add_argument("--query-id", action="append")
    runner.set_defaults(func=run)
    summary = commands.add_parser("summarise")
    summary.add_argument("input")
    summary.add_argument("--output", required=True)
    summary.set_defaults(func=lambda a: summarise_file(a.input, a.output))
    oracle = commands.add_parser("oracle")
    oracle.add_argument("run")
    oracle.set_defaults(func=verify_oracle)
    diff = commands.add_parser("compare")
    diff.add_argument("left")
    diff.add_argument("right")
    diff.add_argument("--output", required=True)
    diff.set_defaults(func=compare)
    args = parser.parse_args()
    try:
        result = args.func(args)
        return result if isinstance(result, int) else 0
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())

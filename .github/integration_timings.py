"""Validate complete Ubuntu attempts and explicitly regenerate median test timings."""

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import integration_jobs


def validate_workflow(manifest):
    run = manifest["workflow"]
    if (
        run["status"] != "completed"
        or run["conclusion"] != "success"
        or str(run["id"]) != manifest["run_id"]
        or run["run_attempt"] != manifest["attempt"]
        or run["head_sha"] != manifest["head_sha"]
    ):
        raise ValueError("workflow metadata must identify this complete successful attempt")


def aggregate(samples, expected):
    groups = defaultdict(list)
    for manifest, rows in samples:
        groups[manifest["run_id"], manifest["attempt"]].append((manifest, rows))
    if not groups:
        raise ValueError("no timing samples")
    reference = samples[0][0]
    values, sources = defaultdict(list), []
    for key, group in sorted(groups.items()):
        first = group[0][0]
        if any(
            first.get(field) != reference.get(field)
            for field in ("sha", "head_sha", "runner", "architecture", "python", "tmux")
        ):
            raise ValueError(f"{key}: attempts must use the same commit and environment")
        if first["workflow"]["event"] != reference["workflow"]["event"]:
            raise ValueError(f"{key}: attempts must use the same workflow event")
        labels = first["shards"]
        if not labels or len(labels) != len(set(labels)):
            raise ValueError(f"{key}: invalid shard labels")
        if Counter(row[0]["shard"] for row in group) != Counter(labels):
            raise ValueError(f"{key}: missing or duplicate shard samples")
        seen = []
        for manifest, rows in group:
            validate_workflow(manifest)
            fields = (
                "sha",
                "head_sha",
                "runner",
                "architecture",
                "python",
                "tmux",
                "shards",
                "discovered",
                "workflow",
            )
            if any(manifest[field] != first[field] for field in fields):
                raise ValueError(f"{key}: inconsistent shard metadata")
            if any(manifest.get(field) != first.get(field) for field in ("matrix_jobs", "cpus")):
                raise ValueError(f"{key}: inconsistent matrix metadata")
            if manifest["runner"] != "ubuntu-24.04" or manifest["exit_code"] != 0:
                raise ValueError(f"{key}: requires successful ubuntu-24.04 samples")
            if Counter(manifest["discovered"]) != Counter(expected):
                raise ValueError(f"{key}: discovered tests differ from this checkout")
            ids = [row["test"] for row in rows]
            if Counter(ids) != Counter(manifest["tests"]) or len(ids) != len(set(ids)):
                raise ValueError(f"{key}: missing or duplicate test measurements")
            durations = {row["test"]: row["seconds"] for row in rows}
            integration_jobs.weights([], durations)
            for test, seconds in durations.items():
                values[test].append(seconds)
            seen.extend(ids)
        if Counter(seen) != Counter(expected):
            raise ValueError(f"{key}: every discovered test must occur exactly once")
        sources.append(
            {
                field: first[field]
                for field in first
                if field
                not in {
                    "shard",
                    "matrix_job",
                    "tests",
                    "discovered",
                    "expected_seconds",
                    "exit_code",
                    "elapsed_seconds",
                    "workflow",
                }
            }
        )
        sources[-1].update(
            url=f"{first['workflow']['html_url']}/attempts/{first['attempt']}",
            event=first["workflow"]["event"],
            shard_count=len(labels),
        )
    durations = {test: statistics.median(values[test]) for test in sorted(values)}
    integration_jobs.weights([], durations)
    return durations, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", nargs="+", type=Path)
    parser.add_argument(
        "--output", type=Path, help="write table and .sources.json; otherwise validate"
    )
    args = parser.parse_args()
    samples = []
    for directory in args.samples:
        directory = directory.resolve()
        for path in sorted(directory.rglob("manifest.json")):
            metadata = next(
                (
                    parent / "run.json"
                    for parent in path.parents
                    if parent.is_relative_to(directory) and (parent / "run.json").is_file()
                ),
                None,
            )
            if metadata is None:
                raise ValueError(f"{path}: missing workflow metadata run.json")
            rows = [
                json.loads(line)
                for line in path.with_name("durations.jsonl").read_text().splitlines()
            ]
            manifest = json.loads(path.read_text())
            manifest["workflow"] = json.loads(metadata.read_text())
            samples.append((manifest, rows))
    found = integration_jobs.run_tests.discover(
        integration_jobs.ROOT, integration_jobs.directories(integration_jobs.ROOT)
    )
    expected = {test.id() for test in integration_jobs.run_tests.tests(found)}
    durations, sources = aggregate(samples, expected)
    if args.output:
        args.output.write_text(json.dumps(durations, indent=2, sort_keys=True) + "\n")
        args.output.with_suffix(".sources.json").write_text(
            json.dumps(
                {
                    "aggregation": "per-test median across complete successful attempts",
                    "sources": sources,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    print(f"validated {len(sources)} complete attempts, {len(durations)} tests")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(str(error)) from error

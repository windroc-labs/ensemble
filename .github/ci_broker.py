"""Require both broker legs, allowing only macOS cancellation before execution."""

import json
import os
import re
import sys
import time
import urllib.request

LINUX = "Broker ubuntu-24.04"
MACOS = "Broker macos-15"


def _api(url: str, token: str, *, cancel=False) -> dict:
    request = urllib.request.Request(
        url,
        method="POST" if cancel else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    result = json.loads(body) if body else {}
    if not isinstance(result, dict):
        raise ValueError("GitHub returned a non-object response")
    return result


def _jobs(url: str, token: str, run: int) -> dict[str, dict]:
    jobs, total, page = [], None, 1
    while total is None or len(jobs) < total:
        data = _api(f"{url}?per_page=100&page={page}", token)
        count, batch = data.get("total_count"), data.get("jobs")
        if (
            type(count) is not int
            or not 1 <= count <= 1000
            or (total is not None and total != count)
            or not isinstance(batch, list)
            or len(batch) != min(100, count - len(jobs))
        ):
            raise ValueError("Incomplete GitHub jobs response")
        total = count
        jobs.extend(batch)
        page += 1
    if any(
        not isinstance(job, dict) or type(job.get("id")) is not int or job.get("run_id") != run
        for job in jobs
    ) or len({job["id"] for job in jobs}) != len(jobs):
        raise ValueError("Invalid or duplicate jobs in current attempt")
    result = {}
    for name in (LINUX, MACOS):
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one {name} in current attempt")
        result[name] = matches[0]
        if matches[0].get("status") not in ("queued", "waiting", "in_progress", "completed"):
            raise ValueError(f"Invalid {name} status")
    return result


def _unstarted(job: dict) -> bool:
    # started_at can be populated while queued; runner/step evidence is decisive.
    return (
        all(key in job for key in ("runner_id", "runner_name", "steps"))
        and not isinstance(job["runner_id"], bool)
        and job["runner_id"] in (None, 0)
        and job["runner_name"] in (None, "")
        and job["steps"] == []
    )


def main() -> int:
    try:
        token = os.environ.get("GITHUB_TOKEN", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        run = int(os.environ.get("GITHUB_RUN_ID", ""))
        attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", ""))
        if not token or re.fullmatch(r"[\w.-]+/[\w.-]+", repo) is None or min(run, attempt) < 1:
            raise ValueError("Missing GitHub token or invalid run identity")
        api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        url = f"{api}/repos/{repo}/actions/runs/{run}"
        jobs_url = f"{url}/attempts/{attempt}/jobs"
        deadline = time.monotonic() + 1200
        cancelling, started = False, False
        while time.monotonic() < deadline:
            jobs = _jobs(jobs_url, token, run)
            linux, mac = jobs[LINUX], jobs[MACOS]
            started = started or not _unstarted(mac) or mac["status"] == "in_progress"
            if linux["status"] == "completed" and linux.get("conclusion") != "success":
                raise ValueError(f"{LINUX}: {linux.get('conclusion')}")
            if mac["status"] == "completed":
                if mac.get("conclusion") == "success":
                    if linux["status"] == "completed":
                        print("Both broker legs passed")
                        return 0
                elif mac.get("conclusion") == "cancelled" and not started:
                    if linux["status"] == "completed":
                        print(
                            f"{MACOS} never started; confirmed cancelled without a runner or steps"
                        )
                        return 0
                else:
                    raise ValueError(f"{MACOS}: {mac.get('conclusion')}; started={started}")
            elif cancelling and started:
                raise ValueError(f"{MACOS} started during cancellation; rerun CI")
            elif (
                not cancelling
                and not started
                and mac["status"] == "queued"
                and linux["status"] == "completed"
            ):
                # Caller has already required every non-broker job. Ordinary cancel
                # stops queued work; CI's always() job AND step keep this gate alive.
                print(
                    f"{MACOS} still queued after every other job; requesting ordinary cancellation"
                )
                _api(f"{url}/cancel", token, cancel=True)
                cancelling = True
                deadline = min(deadline, time.monotonic() + 120)
                continue
            time.sleep(10)
        raise ValueError("Timed out waiting for definitive broker results")
    except (OSError, ValueError) as error:
        print(f"Broker gate failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

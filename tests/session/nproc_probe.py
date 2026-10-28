"""Prints the uid's tasks as RLIMIT_NPROC counts them, across containers ps cannot see: the lowest
soft limit at which a fork succeeds, minus one. Run by path under -I.
"""

import os
import resource

PID_MAX_LIMIT = 1 << 22


def count() -> int:
    soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    top = next((n for n in (soft, hard) if n != resource.RLIM_INFINITY), PID_MAX_LIMIT)
    low, high = 1, top + 1
    while low < high:
        mid = (low + high) // 2
        if _forks(mid, hard):
            high = mid
        else:
            low = mid + 1
    return low - 1


def _forks(limit: int, hard: int) -> bool:
    resource.setrlimit(resource.RLIMIT_NPROC, (limit, hard))
    try:
        pid = os.fork()
    except BlockingIOError:
        return False
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return True


if __name__ == "__main__":
    print(count())

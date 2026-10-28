"""nproc_probe counts the uid's tasks as the kernel's RLIMIT_NPROC check does, by forking."""

import errno
import resource
from unittest import mock

from tests.session import nproc_probe
from tests.session.case import Case

INFINITY = resource.RLIM_INFINITY


class Kernel:
    """A uid with `used` tasks: a fork fails while they reach the soft RLIMIT_NPROC."""

    def __init__(self, used, soft=INFINITY, hard=INFINITY, *, enforced=True):
        self.used, self.limits, self.enforced = used, (soft, hard), enforced
        self.set, self.children, self.forks = [], set(), 0

    def getrlimit(self, which):
        assert which == resource.RLIMIT_NPROC
        return self.limits

    def setrlimit(self, which, limits):
        assert which == resource.RLIMIT_NPROC
        self.set.append(limits)
        self.limits = limits

    def fork(self):
        if self.enforced and self.used + len(self.children) >= self.limits[0]:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        self.forks += 1
        self.children.add(self.forks)
        return self.forks

    def waitpid(self, pid, options):
        self.children.remove(pid)
        return pid, 0


def counted(kernel):
    with (
        mock.patch("resource.getrlimit", kernel.getrlimit),
        mock.patch("resource.setrlimit", kernel.setrlimit),
        mock.patch("os.fork", kernel.fork),
        mock.patch("os.waitpid", kernel.waitpid),
    ):
        return nproc_probe.count()


class CountTest(Case):
    def test_unlimited_counts_every_task_of_the_uid(self):
        for used in (1, 2, 82, 5000):
            with self.subTest(used=used):
                kernel = Kernel(used)
                self.assertEqual(counted(kernel), used)
                self.assertEqual(kernel.children, set())

    def test_a_finite_limit_bounds_the_search_and_keeps_the_hard_limit(self):
        for used, soft, hard in ((10, 64, 128), (63, 64, 128), (100, INFINITY, 4096)):
            with self.subTest(used=used, soft=soft, hard=hard):
                kernel = Kernel(used, soft, hard)
                self.assertEqual(counted(kernel), used)
                self.assertTrue(kernel.set)
                self.assertEqual({h for _, h in kernel.set}, {hard})
                self.assertTrue(
                    all(s <= (hard if soft == INFINITY else soft) for s, _ in kernel.set)
                )

    def test_tasks_at_the_soft_limit_count_as_the_limit(self):
        self.assertEqual(counted(Kernel(64, 64, 128)), 64)

    def test_an_unenforced_limit_counts_none(self):
        self.assertEqual(counted(Kernel(30, enforced=False)), 0)

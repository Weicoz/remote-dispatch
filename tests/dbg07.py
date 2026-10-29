import unittest
from remote_dispatch import cli as rd
from remote_dispatch import rd_reclaim_schedule as sched
from test_rd_reclaim_schedule import *


class D(ScheduleFixture):
    def test_d(self):
        self.dispatch(BUILD)
        self.configure()
        host = self.cfg.hosts()[0]
        import sys
        print("probe", rd.probe(host), file=sys.__stderr__)
        print("sizes", sched.worktree_sizes(host), sched.host_limit(host), host.get("max_worktree_gb"), file=sys.__stderr__)


unittest.main()

"""CPU model of Combine's two-stage prefetch and prefix publication.

Checks scheduling/count invariants only; does not emulate Jetty instructions,
memory visibility, CQE layout, or NPU execution.
"""
import itertools
import unittest


def check_schedule(remote_tasks, tokens_per_task, sq_depth, cq_depth):
    stage_count, publication_interval = 2, 8
    task_bb = tokens_per_task * 4
    reclaim = (sum(remote_tasks) * task_bb + 2 > sq_depth or
               sum(remote_tasks) >= cq_depth)
    issued = ready = released = published = 0
    snapshots = {}

    def prefetch(index):
        nonlocal issued
        if index < len(remote_tasks) and remote_tasks[index]:
            issued += 1
            snapshots[index] = issued
            assert (issued - released) * task_bb + 2 <= sq_depth
            assert issued - released < cq_depth

    for i in range(stage_count):
        prefetch(i)
    for i, remote in enumerate(remote_tasks):
        if remote:
            ready += 1
        prefetch(i + stage_count)
        if remote and ready % publication_interval == 0:
            published = snapshots[i]
            assert published == ready
            assert published <= issued
            if reclaim:
                released = published
    # Every prefetched task has now had its payload processed.
    published = issued
    assert published == ready == sum(remote_tasks)
    return reclaim


class CombineScheduleTest(unittest.TestCase):
    def test_every_local_remote_interleaving(self):
        for count in range(13):
            for tasks in itertools.product((False, True), repeat=count):
                check_schedule(tasks, 8, 512, 16)

    def test_many_queue_wraps(self):
        self.assertTrue(check_schedule([True] * 10000, 8, 512, 16))

    def test_cq_capacity_alone_triggers_reclaim(self):
        self.assertTrue(check_schedule([True] * 100, 8, 32768, 16))

    def test_small_call_preserves_no_intermediate_drain(self):
        self.assertFalse(check_schedule([True] * 12, 8, 32768, 1024))


if __name__ == "__main__":
    unittest.main()

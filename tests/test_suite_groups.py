"""The suite's own map has to stay true.

`scripts/run-tests.py` groups the core modules by what makes them fail. A map
that drifts is worse than no map: a module missing from every group is a module
that "run everything" quietly skips, and one listed twice runs twice while
reading as two different kinds of risk.

So adding a test module is a decision, not a discovery -- this fails until the
new file is placed in exactly one group.
"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "run-tests.py"


def _load_groups():
    # The filename has a hyphen, so it cannot be imported by name.
    spec = importlib.util.spec_from_file_location("_run_tests", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.GROUPS


class SuiteGroupTests(unittest.TestCase):
    def setUp(self):
        self.groups = _load_groups()
        self.on_disk = {p.stem for p in (ROOT / "tests").glob("test_*.py")}
        self.mapped = [m for _why, modules in self.groups.values() for m in modules]

    def test_every_test_module_belongs_to_exactly_one_group(self):
        missing = self.on_disk - set(self.mapped)
        self.assertEqual(missing, set(),
                         "test module in no group -- add it to scripts/run-tests.py")

    def test_no_module_is_listed_twice(self):
        duplicated = {m for m in self.mapped if self.mapped.count(m) > 1}
        self.assertEqual(duplicated, set(), "module listed in more than one group")

    def test_no_group_names_a_module_that_does_not_exist(self):
        phantom = set(self.mapped) - self.on_disk
        self.assertEqual(phantom, set(), "group names a module that is not on disk")

    def test_every_group_explains_when_it_fails(self):
        # The description is the whole point of the grouping; an empty one turns
        # the map back into a list of filenames.
        for name, (why, modules) in self.groups.items():
            with self.subTest(group=name):
                self.assertTrue(modules, "group has no modules")
                self.assertGreater(len(why), 40, "group needs a real description")


if __name__ == "__main__":
    unittest.main()

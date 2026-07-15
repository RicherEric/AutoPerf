import unittest

from autoperf.collectors import BatteryCollector, CpuCollector, MemoryCollector, default_collectors


class ScriptedAdb:
    def __init__(self, response: str):
        self.response = response

    def shell(self, serial, command, timeout=10):
        return self.response


class CollectorErrorTests(unittest.TestCase):
    def test_cpu_collector_raises_on_unparsable_output(self):
        with self.assertRaises(ValueError):
            CpuCollector().collect(ScriptedAdb("no numbers here"), "device", "run")

    def test_memory_collector_raises_when_fields_missing(self):
        with self.assertRaises(ValueError):
            MemoryCollector().collect(ScriptedAdb("MemTotal: 100 kB\n"), "device", "run")

    def test_battery_collector_returns_partial_samples_when_only_level_present(self):
        samples = BatteryCollector().collect(ScriptedAdb(" level: 42\n"), "device", "run")
        self.assertEqual([(s.name, s.value) for s in samples], [("battery.level", 42.0)])

    def test_battery_collector_raises_when_nothing_parseable(self):
        with self.assertRaises(ValueError):
            BatteryCollector().collect(ScriptedAdb("no battery fields"), "device", "run")

    def test_default_collectors_have_expected_names(self):
        names = {collector.name for collector in default_collectors()}
        self.assertEqual(names, {"cpu", "memory", "battery"})


if __name__ == "__main__":
    unittest.main()

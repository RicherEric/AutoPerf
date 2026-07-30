"""The device profile: one platform's parts, and no way to mix two platforms'.

What these guard is not that a profile returns objects -- it is that the
combinations which used to be writable, and wrong, no longer are.
"""

import unittest

from autoperf.adapters import AndroidAdapter, AndroidTvAdapter
from autoperf.profiles import PROFILES, AndroidTvProfile, DeviceProfile, PhoneProfile, select_profile
from autoperf.scenarios import selectors
from autoperf.uiauto import Target
from tests.support import DeviceAdb


class SelectProfileTests(unittest.TestCase):
    def test_a_tv_reports_the_tv_family(self):
        profile = select_profile(DeviceAdb(characteristics="tv"), "S1")
        self.assertIsInstance(profile, AndroidTvProfile)
        self.assertIsInstance(profile.adapter(), AndroidTvAdapter)

    def test_everything_else_reports_the_phone_family(self):
        for value in ("phone", "default", "nosdcard", ""):
            with self.subTest(characteristics=value):
                profile = select_profile(DeviceAdb(characteristics=value), "S1")
                self.assertIsInstance(profile, PhoneProfile)
                self.assertNotIsInstance(profile.adapter(), AndroidTvAdapter)

    def test_an_unreadable_property_falls_back_to_phone(self):
        # Not a reason to fail a run, and phone is right for most devices.
        adb = DeviceAdb(fail_first={"getprop": 1})
        self.assertIsInstance(select_profile(adb, "S1"), PhoneProfile)

    def test_the_probe_is_shared_with_select_adapter(self):
        """One probe, so the two cannot disagree about what a device is.

        They disagreed by construction before: a caller could take the TV
        adapter and the phone's everything-else without writing anything that
        looked wrong.
        """
        from autoperf.adapters import select_adapter

        for value, adapter_type in (("tv", AndroidTvAdapter), ("phone", AndroidAdapter)):
            with self.subTest(characteristics=value):
                adb = DeviceAdb(characteristics=value)
                self.assertIsInstance(select_adapter(adb, "S1"), adapter_type)
                self.assertIsInstance(select_profile(adb, "S1").adapter(), adapter_type)


class ProductFamilyTests(unittest.TestCase):
    def test_a_tv_is_not_asked_for_a_battery_it_does_not_have(self):
        """The concrete cost of the old arrangement.

        `dumpsys battery` on a mains-powered device has no parseable level, and
        BatteryCollector raises on unparseable output -- so the phone's
        collector set produced a collector error on every single TV run, for a
        metric that cannot exist there.
        """
        names = {c.name for c in AndroidTvProfile().collectors()}
        self.assertEqual(names, {"cpu", "memory"})
        self.assertEqual({c.name for c in PhoneProfile().collectors()},
                         {"cpu", "memory", "battery"})

    def test_the_phone_target_table_is_the_selector_table_itself(self):
        # Read from the table rather than listed again here: a second list is a
        # second thing to forget.
        declared = {t for t in vars(selectors).values() if isinstance(t, Target)}
        self.assertEqual(set(PhoneProfile().targets()), declared)
        self.assertTrue(len(declared) > 15)

    def test_a_tv_has_no_locatable_targets_and_says_so(self):
        """Measured, not assumed: the TV build draws its own UI.

        An empty table is the honest answer, and it is what stops a checker
        reporting every target as a decayed selector.
        """
        profile = AndroidTvProfile()
        self.assertEqual(profile.targets(), ())
        self.assertFalse(profile.ui_is_introspectable)
        self.assertTrue(PhoneProfile().ui_is_introspectable)

    def test_package_identity_is_answered_without_building_an_adapter(self):
        # Callers ask this to report on a device as often as to drive one.
        self.assertEqual(AndroidTvProfile().package("com.google.android.youtube"),
                         "com.google.android.youtube.tv")
        self.assertEqual(PhoneProfile().package("com.google.android.youtube"),
                         "com.google.android.youtube")

    def test_an_unmapped_package_is_returned_unchanged(self):
        for profile in (PhoneProfile(), AndroidTvProfile()):
            with self.subTest(profile=profile.name):
                self.assertEqual(profile.package("com.example.app"), "com.example.app")

    def test_the_profile_and_its_adapter_agree_on_package_names(self):
        """Two readers of one map. They were two maps' worth of code before."""
        profile = AndroidTvProfile()
        adapter = profile.adapter()
        for logical in ("com.google.android.youtube", "com.android.settings", "com.example.app"):
            with self.subTest(package=logical):
                self.assertEqual(profile.package(logical), adapter.mapped_package(logical))

    def test_every_profile_answers_the_whole_family(self):
        # A profile missing a product is a platform that silently inherits a
        # phone's assumption for it -- which is the bug this module removes.
        for name, profile_type in PROFILES.items():
            with self.subTest(profile=name):
                profile = profile_type()
                self.assertTrue(profile.adapter())
                self.assertTrue(profile.collectors())
                self.assertIsInstance(profile.targets(), tuple)
                self.assertIsInstance(profile.describe(), dict)

    def test_the_factory_stays_out_of_what_does_not_vary(self):
        """A factory that produces everything is the pattern misapplied.

        Storage, the adb client and the analyzer are identical on every
        platform; routing them through here would add a layer for no benefit
        and make each of them harder to reach.
        """
        surface = {name for name in vars(DeviceProfile) if not name.startswith("_")}
        self.assertEqual(surface, {
            "name", "ui_is_introspectable", "adapter", "collectors", "targets",
            "package", "PACKAGE_MAP", "describe",
        })


if __name__ == "__main__":
    unittest.main()

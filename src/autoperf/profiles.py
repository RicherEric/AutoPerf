"""One platform's whole family of parts, consistent by construction.

Five things vary between a phone and a TV, and until now exactly one of them
knew it: the adapter. The selector table, the scenario library, the collector
set and the package names all silently assumed a phone, which is how a
Chromecast ended up measured against a phone's assumptions while being driven
correctly by the TV adapter.

The point of the factory is not indirection, it is that the wrong combination
stops being expressible. Handing a TV adapter the phone's target table is
currently one line of ordinary-looking code; through a profile there is no line
to write.

Deliberately narrow. `Storage`, `AdbClient`, the analyzer and the batch writer
do *not* vary by platform and must not be pulled in here -- a factory that
produces everything is the classic over-application of the pattern, and it
would make every one of those harder to reach for no benefit.

What still assumes a phone, and lands in later steps: the scenario library
(`scenarios/youtube.py` hard-references `selectors`), and the element strategy
(`AndroidTvAdapter.tap_element` re-implements the resolve loop rather than
reading a policy flag from here).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .adapters import Adapter, AndroidAdapter, AndroidTvAdapter, is_tv
from .adb import AdbClientProtocol
from .collectors import BatteryCollector, Collector, CpuCollector, MemoryCollector
from .scenarios import selectors
from .uiauto import Target


class DeviceProfile(ABC):
    """The abstract factory: everything that varies with what a device *is*."""

    name: str = "profile"

    # Whether this platform exposes its UI to the accessibility tree at all.
    # Measured, not assumed: on a phone YouTube publishes a full hierarchy; on
    # the TV build the app draws its entire interface itself and a dump returns
    # nothing a selector could match. A checker that does not know this reports
    # every target as a decayed selector, which is 19 findings that are all
    # false and none of which name the actual reason.
    ui_is_introspectable: bool = True

    @abstractmethod
    def adapter(self) -> Adapter:
        """A fresh adapter for this platform."""

    @abstractmethod
    def collectors(self) -> list[Collector]:
        """The metrics worth sampling on this platform."""

    @abstractmethod
    def targets(self) -> tuple[Target, ...]:
        """Every UI target that can be located on this platform, possibly none."""

    def package(self, logical: str) -> str:
        """What `logical` is actually called here.

        Answered without building an adapter, because callers ask this to
        *report* on a device (preflight naming what it will launch) as often as
        to drive one.
        """
        return self.PACKAGE_MAP.get(logical, (logical, None))[0]

    PACKAGE_MAP: dict[str, tuple[str, str | None]] = {}

    def describe(self) -> dict:
        return {
            "profile": self.name,
            "adapter": self.adapter().name,
            "collectors": [c.name for c in self.collectors()],
            "targets": len(self.targets()),
            "ui_is_introspectable": self.ui_is_introspectable,
        }


def _selector_table() -> tuple[Target, ...]:
    """Every Target in the selector table, read from the table itself.

    Scanned rather than listed so the table stays the single place a target is
    declared -- a second list here would be a second thing to forget.
    """
    return tuple(value for value in vars(selectors).values() if isinstance(value, Target))


class PhoneProfile(DeviceProfile):
    """Handsets and tablets: touch input, a full accessibility tree, a battery."""

    name = "phone"
    ui_is_introspectable = True

    def adapter(self) -> Adapter:
        return AndroidAdapter()

    def collectors(self) -> list[Collector]:
        return [CpuCollector(), MemoryCollector(), BatteryCollector()]

    def targets(self) -> tuple[Target, ...]:
        return _selector_table()


class AndroidTvProfile(DeviceProfile):
    """Android TV: a remote control, no battery, and a UI a dump cannot see."""

    name = "android-tv"

    # Measured on a Chromecast running Android 14: the YouTube TV build renders
    # its own interface, so `uiautomator dump` returns nothing a selector can
    # match -- the same app publishes a full hierarchy on a phone. Deep links
    # plus outcome verification are the working strategy here, which is why an
    # empty target table is the honest answer rather than a gap to fill.
    ui_is_introspectable = False

    PACKAGE_MAP = AndroidTvAdapter.PACKAGE_MAP

    def adapter(self) -> Adapter:
        return AndroidTvAdapter()

    def collectors(self) -> list[Collector]:
        # No battery. `dumpsys battery` on a mains-powered device has no
        # parseable level, and BatteryCollector's contract is to raise on
        # unparseable output -- so asking for it produces a collector error on
        # every single TV run, for a metric that cannot exist.
        return [CpuCollector(), MemoryCollector()]

    def targets(self) -> tuple[Target, ...]:
        return ()


PROFILES: dict[str, type[DeviceProfile]] = {
    PhoneProfile.name: PhoneProfile,
    AndroidTvProfile.name: AndroidTvProfile,
}


def select_profile(adb: AdbClientProtocol, serial: str) -> DeviceProfile:
    """The profile matching what the device reports itself to be.

    Goes through `adapters.is_tv`, the one place the platform is probed, so
    this and `select_adapter` cannot disagree about a device.
    """
    return AndroidTvProfile() if is_tv(adb, serial) else PhoneProfile()

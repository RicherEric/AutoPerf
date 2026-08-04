"""Device listing, registration and per-device detail.

Split out of one 1120-line module; the shared setUp lives in support.ApiTestCase.
"""

import json
from unittest.mock import patch

from autoperf.adb import AdbError
from autoperf.models import Device

from dashboard.tests.support import ApiTestCase


class DeviceApiTests(ApiTestCase):
    def test_devices_lists_registered_devices(self):
        self.storage.register_device(Device("S1", "device", "Pixel", "pixel"))
        response = self.client.get("/api/devices")
        self.assertEqual(response.status_code, 200)
        serials = {d["serial"] for d in response.json()}
        self.assertEqual(serials, {"S1"})

    @patch("dashboard.views.AdbClient")
    def test_devices_connected_excludes_a_device_that_is_not_attached(self, mock_adb_client):
        # The stored list keeps a device forever, because its runs still name
        # it. "Start on every device" needs the other question answered.
        self.storage.register_device(Device("HERE", "device", "Pixel", "pixel"))
        self.storage.register_device(Device("GONE", "device", "Galaxy", "galaxy"))
        mock_adb_client.return_value.devices.return_value = [Device("HERE", "device", "Pixel", "pixel")]

        response = self.client.get("/api/devices?connected=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([d["serial"] for d in response.json()], ["HERE"])

    @patch("dashboard.views.AdbClient")
    def test_devices_connected_ignores_a_device_adb_lists_as_unauthorized(self, mock_adb_client):
        self.storage.register_device(Device("S1", "device", "Pixel", "pixel"))
        mock_adb_client.return_value.devices.return_value = [Device("S1", "unauthorized", "", "")]

        response = self.client.get("/api/devices?connected=1")

        self.assertEqual(response.json(), [])

    @patch("dashboard.views.AdbClient")
    def test_devices_connected_collapses_one_phone_reached_by_usb_and_wifi(self, mock_adb_client):
        # Same phone, two adb serials. try_start_run excludes by serial, so
        # left as two rows they would both be driven at once.
        self.storage.register_device(
            Device("R5CX", "device", "Galaxy", "galaxy"),
            connection="usb", extra_info={"hardware_serial": "R5CX"})
        self.storage.register_device(
            Device("192.168.0.106:39235", "device", "Galaxy", "galaxy"),
            connection="wifi", extra_info={"hardware_serial": "R5CX"})
        mock_adb_client.return_value.devices.return_value = [
            Device("R5CX", "device", "Galaxy", "galaxy"),
            Device("192.168.0.106:39235", "device", "Galaxy", "galaxy"),
        ]

        response = self.client.get("/api/devices?connected=1")

        self.assertEqual([d["serial"] for d in response.json()], ["R5CX"])

    @patch("dashboard.views.AdbClient")
    def test_devices_connected_keeps_wifi_when_that_is_the_only_way_in(self, mock_adb_client):
        self.storage.register_device(
            Device("192.168.0.106:39235", "device", "Galaxy", "galaxy"),
            connection="wifi", extra_info={"hardware_serial": "R5CX"})
        mock_adb_client.return_value.devices.return_value = [
            Device("192.168.0.106:39235", "device", "Galaxy", "galaxy")]

        response = self.client.get("/api/devices?connected=1")

        self.assertEqual([d["serial"] for d in response.json()], ["192.168.0.106:39235"])

    @patch("dashboard.views.AdbClient")
    def test_devices_connected_keeps_two_devices_that_share_no_hardware_serial(self, mock_adb_client):
        for serial in ("S1", "S2"):
            self.storage.register_device(Device(serial, "device", "Pixel", "pixel"), connection="usb")
        mock_adb_client.return_value.devices.return_value = [
            Device("S1", "device", "Pixel", "pixel"), Device("S2", "device", "Pixel", "pixel")]

        response = self.client.get("/api/devices?connected=1")

        self.assertEqual({d["serial"] for d in response.json()}, {"S1", "S2"})

    def test_devices_without_the_flag_still_lists_everything_stored(self):
        self.storage.register_device(Device("HERE", "device", "Pixel", "pixel"))
        self.storage.register_device(Device("GONE", "device", "Galaxy", "galaxy"))

        response = self.client.get("/api/devices")

        self.assertEqual({d["serial"] for d in response.json()}, {"HERE", "GONE"})

    @patch("dashboard.views.AdbClient")
    def test_devices_connected_reports_an_adb_failure_rather_than_an_empty_list(self, mock_adb_client):
        # An empty list would read as "nothing is plugged in", which is the
        # same answer a broken adb gives and a completely different fact.
        self.storage.register_device(Device("S1", "device", "Pixel", "pixel"))
        mock_adb_client.return_value.devices.side_effect = AdbError("adb not found")

        response = self.client.get("/api/devices?connected=1")

        self.assertEqual(response.status_code, 400)
        self.assertIn("adb not found", response.json()["error"])

    @patch("dashboard.views.AdbClient")
    def test_devices_refresh_registers_and_returns_devices(self, mock_adb_client):
        mock_adb_client.return_value.devices.return_value = [Device("S2", "device", "Galaxy", "galaxy")]
        mock_adb_client.return_value.shell.side_effect = RuntimeError("device offline")
        response = self.client.post("/api/devices/refresh")
        self.assertEqual(response.status_code, 200)
        serials = {d["serial"] for d in response.json()}
        self.assertEqual(serials, {"S2"})

    @patch("dashboard.views.AdbClient")
    def test_device_control_sends_allowlisted_key_event(self, mock_adb_client):
        response = self.client.post(
            "/api/devices/S1/control",
            data=json.dumps({"action": "home"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        mock_adb_client.return_value.shell.assert_called_once_with("S1", "input keyevent KEYCODE_HOME")

    @patch("dashboard.views.AdbClient")
    def test_device_control_rejects_unknown_action(self, mock_adb_client):
        response = self.client.post(
            "/api/devices/S1/control",
            data=json.dumps({"action": "shell", "command": "reboot"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        mock_adb_client.return_value.shell.assert_not_called()

    @patch("dashboard.views.AdbClient")
    def test_devices_connect_requires_address(self, mock_adb_client):
        response = self.client.post("/api/devices/connect", data=json.dumps({}), content_type="application/json")
        self.assertEqual(response.status_code, 400)

    @patch("dashboard.views.AdbClient")
    def test_devices_connect_success_refreshes_device_list(self, mock_adb_client):
        mock_adb_client.return_value.connect.return_value = "connected to 192.168.1.50:5555"
        mock_adb_client.return_value.devices.return_value = [Device("192.168.1.50:5555", "device", "Pixel", "pixel")]
        mock_adb_client.return_value.shell.side_effect = RuntimeError("skip enrichment in this test")
        response = self.client.post(
            "/api/devices/connect",
            data=json.dumps({"address": "192.168.1.50:5555"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("connected to", payload["message"])
        self.assertEqual({d["serial"] for d in payload["devices"]}, {"192.168.1.50:5555"})

    @patch("dashboard.views.AdbClient")
    def test_devices_connect_reports_adb_failure(self, mock_adb_client):
        mock_adb_client.return_value.connect.side_effect = AdbError("cannot connect to 1.2.3.4:5555")
        response = self.client.post(
            "/api/devices/connect", data=json.dumps({"address": "1.2.3.4:5555"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot connect", response.json()["error"])

    @patch("dashboard.views.AdbClient")
    def test_devices_mdns_returns_discovered_services(self, mock_adb_client):
        mock_adb_client.return_value.mdns_services.return_value = {
            "raw": "List of discovered mdns services",
            "services": [{
                "name": "adb-S1-code",
                "service_type": "_adb-tls-connect._tcp",
                "address": "192.168.1.50:5555",
                "kind": "connect",
            }],
        }
        response = self.client.get("/api/devices/mdns")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["services"][0]["kind"], "connect")

    @patch("dashboard.views.AdbClient")
    def test_devices_connect_discovered_connects_only_paired_services(self, mock_adb_client):
        adb = mock_adb_client.return_value
        adb.mdns_services.return_value = {
            "raw": "",
            "services": [
                {"name": "one", "address": "192.168.1.50:5555", "kind": "connect"},
                {"name": "two", "address": "192.168.1.51:37000", "kind": "pairing"},
            ],
        }
        adb.connect.return_value = "connected to 192.168.1.50:5555"
        adb.devices.return_value = []
        response = self.client.post("/api/devices/connect-discovered")
        self.assertEqual(response.status_code, 200)
        adb.connect.assert_called_once_with("192.168.1.50:5555")
        self.assertTrue(response.json()["results"][0]["ok"])

    @patch("dashboard.views.AdbClient")
    def test_devices_pair_requires_address_and_code(self, mock_adb_client):
        response = self.client.post(
            "/api/devices/pair", data=json.dumps({"address": "192.168.1.50:37251"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    @patch("dashboard.views.AdbClient")
    def test_devices_pair_success(self, mock_adb_client):
        mock_adb_client.return_value.pair.return_value = "Successfully paired to 192.168.1.50:37251"
        response = self.client.post(
            "/api/devices/pair",
            data=json.dumps({"address": "192.168.1.50:37251", "code": "123456"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("paired", response.json()["message"].lower())

    @patch("dashboard.views.AdbClient")
    def test_devices_pair_reports_failure(self, mock_adb_client):
        mock_adb_client.return_value.pair.side_effect = AdbError("Failed: wrong pairing code")
        response = self.client.post(
            "/api/devices/pair",
            data=json.dumps({"address": "192.168.1.50:37251", "code": "000000"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_device_nickname_sets_and_persists(self):
        self.storage.register_device(Device("S1", "device", "Pixel", "pixel"))
        response = self.client.post(
            "/api/devices/S1/nickname", data=json.dumps({"nickname": "Alice's phone"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        devices = {d["serial"]: d for d in self.client.get("/api/devices").json()}
        self.assertEqual(devices["S1"]["nickname"], "Alice's phone")

    @patch("dashboard.views.AdbClient")
    def test_devices_refresh_labels_wireless_connection_from_serial_shape(self, mock_adb_client):
        mock_adb_client.return_value.devices.return_value = [Device("192.168.1.50:5555", "device", "Pixel", "pixel")]
        mock_adb_client.return_value.shell.side_effect = RuntimeError("skip enrichment in this test")
        response = self.client.post("/api/devices/refresh")
        payload = response.json()
        self.assertEqual(payload[0]["connection"], "wifi")

    @patch("dashboard.views.AdbClient")
    def test_devices_refresh_parses_extended_identity_info(self, mock_adb_client):
        # Realistic mocked output shaped like real `adb shell` responses,
        # captured against a real Galaxy A55 during development.
        mock_adb_client.return_value.devices.return_value = [Device("S1", "device", "SM_A5560", "a55xzh")]
        mock_adb_client.return_value.shell.side_effect = lambda serial, command, timeout=10: {
            "getprop": (
                "[ro.build.version.release]: [14]\n"
                "[ro.product.manufacturer]: [samsung]\n"
                "[ro.product.brand]: [samsung]\n"
                "[ro.build.version.sdk]: [34]\n"
                "[ro.build.display.id]: [UP1A.231005.007.A5560ZHS7AYC6]\n"
                "[ro.product.cpu.abi]: [arm64-v8a]\n"
            ),
            "dumpsys battery": " level: 79\n temperature: 300\n",
            "settings get global device_name": "Galaxy A55 5G\n",
            "dumpsys package com.android.chrome": "    versionName=150.0.7871.114\n",
            "ip -f inet addr show wlan0": "    inet 192.168.1.217/24 brd 192.168.1.255 scope global wlan0\n",
        }[command]

        response = self.client.post("/api/devices/refresh")
        payload = response.json()[0]
        self.assertEqual(payload["android_version"], "14")
        self.assertEqual(payload["nickname"], "Galaxy A55 5G")
        self.assertEqual(payload["manufacturer"], "samsung")
        self.assertEqual(payload["brand"], "samsung")
        self.assertEqual(payload["sdk_version"], "34")
        self.assertEqual(payload["build_id"], "UP1A.231005.007.A5560ZHS7AYC6")
        self.assertEqual(payload["cpu_abi"], "arm64-v8a")
        self.assertEqual(payload["chrome_version"], "150.0.7871.114")
        self.assertEqual(payload["wifi_ip"], "192.168.1.217")
        self.assertIn("Android 14", payload["user_agent"])
        self.assertIn("SM_A5560", payload["user_agent"])
        self.assertIn("Chrome/150.0.7871.114", payload["user_agent"])

    @patch("dashboard.views.AdbClient")
    def test_devices_refresh_omits_user_agent_when_build_id_unavailable(self, mock_adb_client):
        mock_adb_client.return_value.devices.return_value = [Device("S1", "device", "Pixel", "pixel")]
        mock_adb_client.return_value.shell.side_effect = RuntimeError("device unresponsive")

        response = self.client.post("/api/devices/refresh")
        payload = response.json()[0]
        self.assertIsNone(payload["user_agent"])
        self.assertIsNone(payload["manufacturer"])

    @patch("dashboard.views.AdbClient")
    def test_device_nickname_survives_a_later_refresh(self, mock_adb_client):
        # register_device's COALESCE keeps a manually-set nickname even when
        # a fresh refresh's auto-detected device_name would otherwise fill it.
        self.storage.register_device(Device("S1", "device", "Pixel", "pixel"))
        self.storage.set_device_nickname("S1", "Eric's phone")

        mock_adb_client.return_value.devices.return_value = [Device("S1", "device", "Pixel", "pixel")]
        mock_adb_client.return_value.shell.side_effect = lambda serial, command, timeout=10: {
            "getprop": "",
            "dumpsys battery": " level: 50\n",
            "settings get global device_name": "Some Other Name\n",
            "dumpsys package com.android.chrome": "",
            "ip -f inet addr show wlan0": "",
        }[command]

        response = self.client.post("/api/devices/refresh")
        self.assertEqual(response.json()[0]["nickname"], "Eric's phone")

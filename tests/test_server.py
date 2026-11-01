import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opensock.server import (
    BUILD_DEPENDENCIES,
    _extract_kernel_archive,
    _split_nmcli_row,
    kernel_archive_url,
    missing_build_dependencies,
    network_status,
    system_status,
    validate_network_request,
    validate_grub_settings,
    validate_kernel_args,
    validate_version,
)
from opensock.privileged import (
    BUILD_DEPENDENCIES as HELPER_BUILD_DEPENDENCIES,
    PACKAGE_PATTERN,
    _replace_grub_variables,
    _configure_network,
    _validate_color,
    _validate_kernel_args,
    _validate_network_request,
    _keyfile_escape,
    install_build_dependencies,
)


class ValidationTests(unittest.TestCase):
    def test_accepts_upstream_kernel_versions(self):
        self.assertEqual(validate_version("6.12.56"), "6.12.56")
        self.assertEqual(validate_version("6.13-rc1"), "6.13-rc1")

    def test_rejects_non_upstream_or_unsafe_kernel_versions(self):
        with self.assertRaises(ValueError):
            validate_version("6.8.0-31.31")
        with self.assertRaises(ValueError):
            validate_version("6.12; touch /tmp/pwned")

    def test_kernel_archive_uses_official_kernel_org_url(self):
        self.assertEqual(
            kernel_archive_url("6.12.56"),
            "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.56.tar.xz",
        )
        self.assertIn("/v10.x/", kernel_archive_url("10.0"))

    def test_kernel_archive_extraction_rejects_paths_outside_source_tree(self):
        version = "6.12.56"
        with tempfile.TemporaryDirectory() as temporary_directory:
            job_dir = Path(temporary_directory)
            archive_path = job_dir / "source.tar.xz"
            with tarfile.open(archive_path, "w:xz") as archive:
                member = tarfile.TarInfo(f"linux-{version}/../../outside")
                member.size = 4
                archive.addfile(member, io.BytesIO(b"nope"))
            with patch("opensock.server._append_log"), self.assertRaises(RuntimeError):
                _extract_kernel_archive("a" * 32, archive_path, job_dir, version)
            self.assertFalse((job_dir.parent / "outside").exists())

    def test_build_dependencies_match_privileged_allowlist(self):
        self.assertEqual(BUILD_DEPENDENCIES, HELPER_BUILD_DEPENDENCIES)

    def test_upstream_image_and_headers_packages_are_installable(self):
        self.assertRegex("linux-image-6.12.56-opensock_1_amd64.deb", PACKAGE_PATTERN)
        self.assertRegex("linux-headers-6.12.56-opensock_1_all.deb", PACKAGE_PATTERN)

    def test_validates_static_network_settings(self):
        request = validate_network_request({
            "operation": "configure",
            "interface": "enp3s0",
            "connection_uuid": "01234567-89ab-cdef-0123-456789abcdef",
            "ipv4_method": "manual",
            "ipv4_address": "192.168.1.20/24",
            "ipv4_gateway": "192.168.1.1",
            "dns": ["1.1.1.1", "2606:4700:4700::1111", "1.1.1.1"],
            "mac_mode": "custom",
            "mac_address": "02:11:22:33:44:55",
        })
        self.assertEqual(request["ipv4_address"], "192.168.1.20/24")
        self.assertEqual(request["dns"], ["1.1.1.1", "2606:4700:4700::1111"])
        self.assertEqual(request["mac_address"], "02:11:22:33:44:55")

    def test_rejects_invalid_network_settings(self):
        request = {
            "operation": "configure",
            "interface": "enp3s0",
            "connection_uuid": "01234567-89ab-cdef-0123-456789abcdef",
            "ipv4_method": "manual",
            "ipv4_address": "192.168.1.20",
            "ipv4_gateway": "not-an-ip",
            "dns": [],
        }
        with self.assertRaises(ValueError):
            validate_network_request(request)
        request["ipv4_address"] = "192.168.1.20/24"
        request["mac_mode"] = "custom"
        request["mac_address"] = "01:11:22:33:44:55"
        with self.assertRaises(ValueError):
            validate_network_request(request)

    def test_validates_wifi_credentials_without_accepting_unsafe_text(self):
        request = {
            "operation": "connect_wifi",
            "interface": "wlp2s0",
            "ssid": "Home Wi-Fi",
            "security": "wpa-psk",
            "password": "valid passphrase",
            "mac_mode": "random",
        }
        self.assertEqual(validate_network_request(request)["security"], "wpa-psk")
        self.assertEqual(
            validate_network_request({**request, "security": "sae", "password": "sae-pass"})["security"],
            "sae",
        )
        with self.assertRaises(ValueError):
            validate_network_request({**request, "password": "short"})
        with self.assertRaises(ValueError):
            validate_network_request({**request, "security": "sae", "password": ""})
        with self.assertRaises(ValueError):
            validate_network_request({**request, "ssid": "bad\nssid"})

    def test_privileged_helper_repeats_network_validation(self):
        request = {
            "operation": "connect_wifi",
            "interface": "wlp2s0",
            "ssid": "Open",
            "security": "open",
            "password": "",
            "mac_mode": "permanent",
        }
        self.assertEqual(_validate_network_request(request)["operation"], "connect_wifi")
        self.assertEqual(
            _validate_network_request({
                **request,
                "security": "sae",
                "password": "sae-pass",
            })["security"],
            "sae",
        )
        with self.assertRaises(ValueError):
            _validate_network_request({**request, "interface": "../../etc"})

    def test_network_status_reports_missing_networkmanager(self):
        with patch("opensock.server.shutil.which", return_value=None):
            status = network_status()
        self.assertFalse(status["available"])
        self.assertEqual(status["interfaces"], [])

    def test_network_status_parses_active_nmcli_data(self):
        outputs = iter([
            "01234567-89ab-cdef-0123-456789abcdef:enp3s0\n",
            "enp3s0:ethernet:connected:Home\\:LAN\n",
            "GENERAL.HWADDR: 02:11:22:33:44:55\nIP4.ADDRESS[1]: 192.168.1.20/24\nIP4.GATEWAY: 192.168.1.1\nIP4.DNS[1]: 1.1.1.1\n",
            "connection.id:Home\\:LAN\nconnection.interface-name:enp3s0\nipv4.method:manual\nipv4.addresses:192.168.1.20/24\nipv4.gateway:192.168.1.1\nipv4.dns:1.1.1.1\nipv6.dns:--\n802-3-ethernet.cloned-mac-address:permanent\n",
        ])
        with patch("opensock.server.shutil.which", return_value="/usr/bin/nmcli"), \
                patch("opensock.server._network_command", side_effect=lambda *args: next(outputs)):
            status = network_status()
        device = status["interfaces"][0]
        self.assertEqual(device["connection"], "Home:LAN")
        self.assertEqual(device["ipv4_addresses"], ["192.168.1.20/24"])
        self.assertEqual(device["settings"]["ipv4_method"], "manual")

    def test_network_status_scans_and_decodes_wifi_networks(self):
        outputs = iter([
            "01234567-89ab-cdef-0123-456789abcdef:wlan0\n",
            "wlan0:wifi:connected:Home\\:WiFi\n",
            "GENERAL.HWADDR: 02:11:22:33:44:55\n",
            "connection.id:Home\\:WiFi\nconnection.interface-name:wlan0\nipv4.method:auto\n",
            "",
            "*:Home\\:WiFi:02\\:11\\:22\\:33\\:44\\:55:88:WPA2\n:Cafe:02\\:11\\:22\\:33\\:44\\:66:42:--\n",
        ])
        with patch("opensock.server.shutil.which", return_value="/usr/bin/nmcli"), \
                patch("opensock.server._network_command", side_effect=lambda *args: next(outputs)):
            status = network_status("wlan0")
        self.assertEqual(status["wifi_networks"][0]["ssid"], "Home:WiFi")
        self.assertEqual(status["wifi_networks"][0]["security"], "WPA2")
        self.assertEqual(status["wifi_networks"][1]["security"], "")
        self.assertEqual(status["wifi_networks"][1]["signal"], 42)

    def test_nmcli_terse_row_supports_colons_in_escaped_fields(self):
        self.assertEqual(_split_nmcli_row(r"wlan0:Home\:Wi-Fi:02\:11:22"), [
            "wlan0", "Home:Wi-Fi", "02:11", "22",
        ])

    def test_keyfile_escaping_keeps_wifi_credentials_on_single_line(self):
        self.assertEqual(_keyfile_escape("line\\name "), "line\\\\name\\s")

    def test_network_profile_changes_are_rolled_back_if_reconnect_fails(self):
        request = {
            "interface": "enp3s0",
            "connection_uuid": "01234567-89ab-cdef-0123-456789abcdef",
            "ipv4_method": "manual",
            "ipv4_address": "192.168.1.20/24",
            "ipv4_gateway": "192.168.1.1",
            "dns": ["1.1.1.1"],
            "mac_mode": "permanent",
            "mac_address": "",
        }
        original_values = {
            "ipv4.method": "auto",
            "ipv4.addresses": "",
            "ipv4.gateway": "",
            "ipv4.dns": "",
            "ipv4.ignore-auto-dns": "no",
            "ipv6.dns": "",
            "ipv6.ignore-auto-dns": "no",
            "802-3-ethernet.cloned-mac-address": "permanent",
        }
        with patch("opensock.privileged._assert_network_device", return_value="ethernet"), \
                patch("opensock.privileged._assert_active_connection"), \
                patch("opensock.privileged._nmcli_get", side_effect=original_values.values()), \
                patch("opensock.privileged._nmcli_modify") as modify, \
                patch("opensock.privileged._reconnect_profile", side_effect=[RuntimeError("no route"), None]), \
                self.assertRaisesRegex(RuntimeError, "исходный профиль восстановлен"):
            _configure_network(request)
        self.assertEqual(modify.call_count, 2)
        self.assertEqual(modify.call_args_list[1].args[1], original_values)

    def test_dependency_install_is_fixed_and_does_not_upgrade_packages(self):
        with patch("opensock.privileged._require_supported_platform"), \
                patch("opensock.privileged._requesting_uid"), \
                patch("opensock.privileged.shutil.which", return_value="/usr/bin/apt-get"), \
                patch("opensock.privileged._run") as run:
            install_build_dependencies()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ["/usr/bin/apt-get", "update"])
        install_command = run.call_args_list[1].args[0]
        self.assertEqual(install_command[:6], [
            "/usr/bin/apt-get", "install", "--assume-yes",
            "--no-install-recommends", "--no-remove", "--no-upgrade",
        ])
        self.assertEqual(tuple(install_command[6:]), BUILD_DEPENDENCIES)

    def test_normalizes_kernel_arguments(self):
        self.assertEqual(validate_kernel_args("quiet   splash"), "quiet splash")

    def test_rejects_unsafe_kernel_arguments(self):
        with self.assertRaises(ValueError):
            validate_kernel_args("quiet; reboot")

    def test_privileged_helper_repeats_kernel_argument_validation(self):
        with self.assertRaises(ValueError):
            _validate_kernel_args("quiet $(id)")

    def test_system_status_includes_overview_sections(self):
        status = system_status()
        self.assertIn("hostname", status)
        self.assertIn("running_kernel", status)
        self.assertIn("model", status["cpu"])
        self.assertIn("total_bytes", status["memory"])
        self.assertIn("free_bytes", status["root_disk"])
        self.assertIn("available", status["bootloader"])
        self.assertEqual(status["build_dependencies"], missing_build_dependencies())
        self.assertEqual(
            set(status["build_tools"]),
            {"apt-get", "dpkg-query", "pkexec"},
        )

    def test_validates_grub_settings(self):
        settings = validate_grub_settings({
            "timeout": 12,
            "timeout_style": "menu",
            "default": "saved",
            "color_normal": "white/blue",
            "color_highlight": "yellow/red",
            "remove_wallpaper": False,
        })
        self.assertEqual(settings["timeout"], "12")
        self.assertEqual(settings["default"], "saved")

    def test_rejects_out_of_range_grub_timeout(self):
        with self.assertRaises(ValueError):
            validate_grub_settings({
                "timeout": 601,
                "timeout_style": "menu",
                "default": "0",
                "color_normal": "white/blue",
                "color_highlight": "yellow/red",
            })

    def test_rejects_grub_config_injection(self):
        with self.assertRaises(ValueError):
            validate_grub_settings({
                "timeout": 5,
                "timeout_style": "menu",
                "default": "0; reboot",
                "color_normal": "white/blue",
                "color_highlight": "yellow/red",
            })

    def test_helper_limits_grub_colors(self):
        self.assertEqual(_validate_color("white/blue"), "white/blue")
        with self.assertRaises(ValueError):
            _validate_color("white;reboot/blue")

    def test_replaces_grub_assignments_without_touching_comments(self):
        result = _replace_grub_variables(
            "# GRUB_TIMEOUT=99\nGRUB_TIMEOUT=5\nGRUB_TIMEOUT=10",
            {"GRUB_TIMEOUT": "12"},
        )
        self.assertIn("# GRUB_TIMEOUT=99", result)
        self.assertEqual(result.count("GRUB_TIMEOUT="), 2)
        self.assertIn("GRUB_TIMEOUT=12", result)


if __name__ == "__main__":
    unittest.main()

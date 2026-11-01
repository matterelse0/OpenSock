from __future__ import annotations

import json
import base64
import binascii
import ipaddress
import os
import platform
import posixpath
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

from opensock import __version__

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
STATE_ROOT = Path.home() / ".local" / "state" / "opensock"
BUILD_ROOT = STATE_ROOT / "builds"
GRUB_IMAGE_ROOT = STATE_ROOT / "grub-images"
MAX_PATCH_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = MAX_PATCH_BYTES + 16 * 1024
MAX_GRUB_REQUEST_BYTES = 11 * 1024 * 1024
MAX_GRUB_IMAGE_BYTES = 8 * 1024 * 1024
MAX_KERNEL_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_NETWORK_REQUEST_BYTES = 16 * 1024
KERNEL_VERSION_PATTERN = re.compile(
    r"^((?:[3-9]|[1-9][0-9]+))\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?(?:-rc([1-9][0-9]*))?$"
)
BUILD_DEPENDENCIES = (
    "bc",
    "bison",
    "build-essential",
    "ca-certificates",
    "cpio",
    "dwarves",
    "dpkg-dev",
    "fakeroot",
    "flex",
    "kmod",
    "libelf-dev",
    "libncurses-dev",
    "libssl-dev",
    "patch",
    "rsync",
    "xz-utils",
    "zstd",
)
JOB_PATTERN = re.compile(r"^[0-9a-f]{32}$")
INTERFACE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
CONNECTION_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
MAC_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
ARG_PATTERN = re.compile(r"^[A-Za-z0-9_.=,:/+@%-]+$")
GRUB_COLORS = {
    "black", "blue", "green", "cyan", "red", "magenta", "brown",
    "light-gray", "dark-gray", "light-blue", "light-green", "light-cyan",
    "light-red", "light-magenta", "yellow", "white",
}

_csrf_token = secrets.token_urlsafe(32)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_build_lock = threading.Lock()
_grub_job: dict | None = None
_grub_lock = threading.Lock()
_network_job: dict | None = None
_network_lock = threading.Lock()
_network_operation_lock = threading.Lock()


def read_grub_settings() -> dict:
    settings = {
        "timeout": "5",
        "timeout_style": "menu",
        "default": "0",
        "color_normal": "light-gray/black",
        "color_highlight": "black/light-gray",
        "background": "",
    }
    variable_names = {
        "GRUB_TIMEOUT": "timeout",
        "GRUB_TIMEOUT_STYLE": "timeout_style",
        "GRUB_DEFAULT": "default",
        "GRUB_COLOR_NORMAL": "color_normal",
        "GRUB_COLOR_HIGHLIGHT": "color_highlight",
        "GRUB_BACKGROUND": "background",
    }
    try:
        lines = Path("/etc/default/grub").read_text(encoding="utf-8").splitlines()
    except OSError:
        return settings
    for line in lines:
        match = re.match(r"^\s*(GRUB_[A-Z_]+)\s*=\s*(.*?)\s*$", line)
        if not match or match.group(1) not in variable_names:
            continue
        try:
            value = shlex.split(match.group(2), comments=True)
        except ValueError:
            continue
        if value:
            settings[variable_names[match.group(1)]] = value[0]
    return settings


def validate_grub_settings(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Ожидался объект настроек GRUB.")
    timeout = payload.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 <= timeout <= 600:
        raise ValueError("Таймаут GRUB должен быть числом от 0 до 600 секунд.")
    timeout_style = payload.get("timeout_style")
    if timeout_style not in {"menu", "hidden", "countdown"}:
        raise ValueError("Выберите допустимый режим отображения меню GRUB.")
    default = payload.get("default")
    if not isinstance(default, str) or not re.fullmatch(r"[A-Za-z0-9_.:() -]{1,120}", default):
        raise ValueError("Некорректный пункт загрузки GRUB.")
    normal = payload.get("color_normal")
    highlight = payload.get("color_highlight")
    for value in (normal, highlight):
        if (
            not isinstance(value, str)
            or value.count("/") != 1
            or any(color not in GRUB_COLORS for color in value.split("/"))
        ):
            raise ValueError("Выберите допустимые цвета меню GRUB.")
    remove_wallpaper = payload.get("remove_wallpaper", False)
    if not isinstance(remove_wallpaper, bool):
        raise ValueError("Некорректное значение параметра удаления обоев.")
    return {
        "timeout": str(timeout),
        "timeout_style": timeout_style,
        "default": default,
        "color_normal": normal,
        "color_highlight": highlight,
        "remove_wallpaper": remove_wallpaper,
    }


def validate_version(version: object) -> str:
    if not isinstance(version, str) or not KERNEL_VERSION_PATTERN.fullmatch(version):
        raise ValueError("Укажите версию ядра kernel.org, например 6.12.56 или 6.13-rc1.")
    return version


def kernel_archive_url(version: str) -> str:
    version = validate_version(version)
    major = version.split(".", 1)[0]
    return f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x/linux-{version}.tar.xz"


def missing_build_dependencies() -> list[str]:
    if not shutil.which("dpkg-query"):
        return list(BUILD_DEPENDENCIES)
    result = subprocess.run(
        [
            "dpkg-query",
            "-W",
            "-f=${binary:Package}\t${db:Status-Status}\n",
            *BUILD_DEPENDENCIES,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    installed = {
        line.split("\t", 1)[0].split(":", 1)[0]
        for line in result.stdout.splitlines()
        if "\tinstalled" in line
    }
    return [package for package in BUILD_DEPENDENCIES if package not in installed]


def validate_kernel_args(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Параметры ядра должны быть строкой не длиннее 2048 символов.")
    try:
        args = shlex.split(value)
    except ValueError as exc:
        raise ValueError("Не удалось разобрать параметры ядра.") from exc
    if any(not ARG_PATTERN.fullmatch(arg) for arg in args):
        raise ValueError("Параметры содержат неподдерживаемые символы.")
    return " ".join(args)


def _validate_interface_name(value: object) -> str:
    if not isinstance(value, str) or not INTERFACE_PATTERN.fullmatch(value):
        raise ValueError("Выберите допустимый сетевой интерфейс.")
    return value


def _validate_mac_address(value: object, mode: object) -> tuple[str, str]:
    if mode not in {"permanent", "random", "custom"}:
        raise ValueError("Выберите допустимый режим MAC-адреса.")
    if mode == "permanent":
        return "permanent", ""
    if mode == "random":
        return "random", ""
    if not isinstance(value, str) or not MAC_PATTERN.fullmatch(value):
        raise ValueError("Введите MAC-адрес в формате 02:11:22:33:44:55.")
    octets = bytes.fromhex(value.replace(":", ""))
    if octets[0] & 1 or not any(octets):
        raise ValueError("Укажите допустимый индивидуальный (unicast) MAC-адрес.")
    return "custom", value.lower()


def _validate_dns_servers(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 6:
        raise ValueError("Укажите не более шести DNS-серверов.")
    addresses = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError("Некорректный адрес DNS-сервера.")
        try:
            addresses.append(str(ipaddress.ip_address(entry.strip())))
        except ValueError as exc:
            raise ValueError(f"Некорректный адрес DNS-сервера: {entry!r}.") from exc
    return list(dict.fromkeys(addresses))


def validate_network_request(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Ожидались настройки сети.")
    operation = payload.get("operation")
    interface = _validate_interface_name(payload.get("interface"))
    if operation == "disconnect":
        return {"operation": operation, "interface": interface}
    if operation == "configure":
        connection_uuid = payload.get("connection_uuid")
        if not isinstance(connection_uuid, str) or not CONNECTION_UUID_PATTERN.fullmatch(connection_uuid):
            raise ValueError("Не удалось определить активный профиль NetworkManager.")
        method = payload.get("ipv4_method")
        if method not in {"auto", "manual"}:
            raise ValueError("Выберите DHCP или ручную настройку IPv4.")
        address = payload.get("ipv4_address", "")
        gateway = payload.get("ipv4_gateway", "")
        if not isinstance(address, str) or not isinstance(gateway, str):
            raise ValueError("Некорректные адрес или шлюз IPv4.")
        address = address.strip()
        gateway = gateway.strip()
        if method == "manual":
            try:
                address = str(ipaddress.IPv4Interface(address))
            except ValueError as exc:
                raise ValueError("Введите IPv4-адрес с префиксом, например 192.168.1.20/24.") from exc
            if gateway:
                try:
                    gateway = str(ipaddress.IPv4Address(gateway))
                except ValueError as exc:
                    raise ValueError("Введите корректный IPv4-шлюз.") from exc
        elif address or gateway:
            raise ValueError("При DHCP поля адреса и шлюза должны быть пустыми.")
        dns = _validate_dns_servers(payload.get("dns", []))
        mac_mode, mac_address = _validate_mac_address(
            payload.get("mac_address", ""),
            payload.get("mac_mode", "permanent"),
        )
        return {
            "operation": operation,
            "interface": interface,
            "connection_uuid": connection_uuid.lower(),
            "ipv4_method": method,
            "ipv4_address": address,
            "ipv4_gateway": gateway,
            "dns": dns,
            "mac_mode": mac_mode,
            "mac_address": mac_address,
        }
    if operation == "connect_wifi":
        ssid = payload.get("ssid")
        if (
            not isinstance(ssid, str)
            or not ssid.strip()
            or len(ssid.encode("utf-8")) > 32
            or any(ord(char) < 32 or ord(char) == 127 for char in ssid)
        ):
            raise ValueError("Имя Wi-Fi сети должно содержать от 1 до 32 байт без управляющих символов.")
        security = payload.get("security")
        password = payload.get("password", "")
        if security not in {"open", "wpa-psk", "sae"}:
            raise ValueError("Поддерживаются открытые сети и Wi-Fi с WPA/WPA2/WPA3-Personal.")
        if security == "open" and password:
            raise ValueError("Для открытой Wi-Fi сети пароль не требуется.")
        if security == "wpa-psk":
            if not isinstance(password, str):
                raise ValueError("Введите пароль Wi-Fi.")
            if not (
                re.fullmatch(r"[0-9a-fA-F]{64}", password)
                or (
                    8 <= len(password) <= 63
                    and all(0x20 <= ord(char) <= 0x7E for char in password)
                )
            ):
                raise ValueError("Пароль WPA должен содержать 8–63 печатных ASCII-символа или 64 шестнадцатеричных знака.")
        elif security == "sae":
            if (
                not isinstance(password, str)
                or not 1 <= len(password) <= 63
                or not all(0x20 <= ord(char) <= 0x7E for char in password)
            ):
                raise ValueError("Пароль WPA3-Personal должен содержать от 1 до 63 печатных ASCII-символов.")
        mac_mode, mac_address = _validate_mac_address(
            payload.get("mac_address", ""),
            payload.get("mac_mode", "permanent"),
        )
        return {
            "operation": operation,
            "interface": interface,
            "ssid": ssid,
            "security": security,
            "password": password,
            "mac_mode": mac_mode,
            "mac_address": mac_address,
        }
    raise ValueError("Неизвестная операция с сетью.")


def _split_nmcli_row(line: str) -> list[str]:
    fields = []
    field = []
    escaped = False
    for char in line:
        if escaped:
            field.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(field))
            field = []
        else:
            field.append(char)
    if escaped:
        field.append("\\")
    fields.append("".join(field))
    return fields


def _network_command(arguments: list[str], timeout: int = 15) -> str:
    nmcli = shutil.which("nmcli")
    if not nmcli:
        raise RuntimeError("NetworkManager (команда nmcli) не установлен.")
    try:
        result = subprocess.run(
            [nmcli, *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("NetworkManager не ответил вовремя.") from exc
    except OSError as exc:
        raise RuntimeError(f"Не удалось обратиться к NetworkManager: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(detail or "Команда NetworkManager завершилась с ошибкой.")
    return result.stdout


def network_status(scan_interface: str | None = None) -> dict:
    if not shutil.which("nmcli"):
        return {
            "available": False,
            "error": "NetworkManager (команда nmcli) не установлен.",
            "interfaces": [],
            "wifi_networks": [],
        }
    active_connections = {}
    for line in _network_command([
        "--terse", "--escape", "yes", "--fields", "UUID,DEVICE",
        "connection", "show", "--active",
    ]).splitlines():
        fields = _split_nmcli_row(line)
        if len(fields) >= 2 and fields[0] and fields[1] != "--":
            active_connections[fields[1]] = fields[0]

    interfaces = []
    for line in _network_command([
        "--terse", "--escape", "yes", "--fields",
        "DEVICE,TYPE,STATE,CONNECTION", "device", "status",
    ]).splitlines():
        fields = _split_nmcli_row(line)
        if len(fields) < 4 or fields[1] not in {"ethernet", "wifi"}:
            continue
        name, device_type, state, connection_name = fields[:4]
        if not INTERFACE_PATTERN.fullmatch(name):
            continue
        device = {
            "name": name,
            "type": device_type,
            "state": state,
            "connection": "" if connection_name == "--" else connection_name,
            "connection_uuid": active_connections.get(name, ""),
            "settings": None,
        }
        details = _network_command([
            "--terse", "--escape", "yes", "--fields",
            "GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.GATEWAY,IP6.DNS",
            "device", "show", name,
        ])
        values = {}
        for detail in details.splitlines():
            key, separator, value = detail.partition(":")
            if separator:
                normalized_key = re.sub(r"\[\d+\]$", "", key.strip())
                values.setdefault(normalized_key, []).append(value.strip())
        device["hw_address"] = (values.get("GENERAL.HWADDR") or [""])[0]
        device["ipv4_addresses"] = values.get("IP4.ADDRESS", [])
        device["ipv4_gateway"] = (values.get("IP4.GATEWAY") or [""])[0]
        device["ipv4_dns"] = values.get("IP4.DNS", [])
        device["ipv6_addresses"] = values.get("IP6.ADDRESS", [])
        device["ipv6_gateway"] = (values.get("IP6.GATEWAY") or [""])[0]
        device["ipv6_dns"] = values.get("IP6.DNS", [])
        if device["connection_uuid"]:
            profile = _network_command([
                "--terse", "--escape", "yes", "--fields",
                "connection.id,connection.interface-name,ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns,ipv6.dns,802-3-ethernet.cloned-mac-address,802-11-wireless.cloned-mac-address",
                "connection", "show", device["connection_uuid"],
            ])
            settings = {}
            for setting in profile.splitlines():
                key, separator, value = setting.partition(":")
                if separator:
                    settings[key.strip()] = value.strip()
            device["settings"] = {
                "id": settings.get("connection.id", device["connection"]),
                "interface": settings.get("connection.interface-name", ""),
                "ipv4_method": settings.get("ipv4.method", "auto"),
                "ipv4_address": settings.get("ipv4.addresses", "").split(",")[0].strip(),
                "ipv4_gateway": settings.get("ipv4.gateway", ""),
                "dns": settings.get("ipv4.dns", ""),
                "ipv6_dns": settings.get("ipv6.dns", ""),
                "mac_override": settings.get("802-3-ethernet.cloned-mac-address")
                or settings.get("802-11-wireless.cloned-mac-address", "permanent"),
            }
        interfaces.append(device)

    wifi_networks = []
    if scan_interface:
        scan_interface = _validate_interface_name(scan_interface)
        selected = next(
            (device for device in interfaces if device["name"] == scan_interface and device["type"] == "wifi"),
            None,
        )
        if not selected:
            raise ValueError("Выбранный интерфейс Wi-Fi не найден.")
        _network_command(["device", "wifi", "rescan", "ifname", scan_interface])
        for line in _network_command([
            "--terse", "--escape", "yes", "--fields",
            "IN-USE,SSID,BSSID,SIGNAL,SECURITY",
            "device", "wifi", "list", "ifname", scan_interface,
        ]).splitlines():
            fields = _split_nmcli_row(line)
            if len(fields) < 5 or not fields[1] or fields[1] == "--":
                continue
            try:
                signal = max(0, min(100, int(fields[3])))
            except ValueError:
                signal = 0
            security = "" if fields[4] == "--" else fields[4]
            wifi_networks.append({
                "in_use": fields[0] == "*",
                "ssid": fields[1],
                "bssid": fields[2],
                "signal": signal,
                "security": security,
            })
    return {
        "available": True,
        "interfaces": interfaces,
        "wifi_networks": wifi_networks,
    }


def system_status() -> dict:
    distro = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                distro[key] = value.strip().strip('"')
    except OSError:
        pass

    grub_args = ""
    try:
        for line in Path("/etc/default/grub").read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*GRUB_CMDLINE_LINUX_DEFAULT\s*=\s*(.*)$", line)
            if match:
                values = shlex.split(match.group(1), comments=True)
                grub_args = " ".join(values)
                break
    except (OSError, ValueError):
        pass

    cpu_model = "Неизвестно"
    cpu_info = Path("/proc/cpuinfo")
    try:
        for line in cpu_info.read_text(encoding="utf-8").splitlines():
            if line.lower().startswith(("model name", "hardware")) and ":" in line:
                cpu_model = line.partition(":")[2].strip()
                break
    except OSError:
        pass

    memory = {"total_bytes": None, "available_bytes": None}
    try:
        memory_values = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            if separator and key in {"MemTotal", "MemAvailable"}:
                memory_values[key] = int(value.strip().split()[0]) * 1024
        memory["total_bytes"] = memory_values.get("MemTotal")
        memory["available_bytes"] = memory_values.get("MemAvailable")
    except (OSError, ValueError, IndexError):
        pass

    uptime_seconds = None
    try:
        uptime_seconds = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass

    disk = {"total_bytes": None, "used_bytes": None, "free_bytes": None}
    try:
        disk_usage = shutil.disk_usage("/")
        disk = {
            "total_bytes": disk_usage.total,
            "used_bytes": disk_usage.used,
            "free_bytes": disk_usage.free,
        }
    except OSError:
        pass

    secure_boot = "Неизвестно"
    efi_variables = Path("/sys/firmware/efi/efivars")
    if not Path("/sys/firmware/efi").exists():
        secure_boot = "Legacy BIOS"
    else:
        try:
            secure_boot_file = next(efi_variables.glob("SecureBoot-*"), None)
            if secure_boot_file:
                secure_boot_value = secure_boot_file.read_bytes()
                if len(secure_boot_value) >= 5:
                    secure_boot = "Включён" if secure_boot_value[4] else "Выключен"
        except OSError:
            pass

    bootloader_path = shutil.which("update-grub") or shutil.which("grub-mkconfig")
    bootloader = {
        "name": "GRUB" if bootloader_path or Path("/boot/grub/grub.cfg").exists() else "Не определён",
        "available": bool(bootloader_path and Path("/boot/grub/grub.cfg").exists()),
    }

    return {
        "application_version": __version__,
        "distribution": distro.get("PRETTY_NAME", "Неизвестный дистрибутив"),
        "distribution_id": distro.get("ID", "").lower(),
        "hostname": socket.gethostname(),
        "architecture": platform.machine(),
        "running_kernel": platform.release(),
        "cpu": {
            "model": cpu_model,
            "threads": os.cpu_count(),
        },
        "memory": memory,
        "root_disk": disk,
        "uptime_seconds": uptime_seconds,
        "secure_boot": secure_boot,
        "bootloader": bootloader,
        "grub_kernel_args": grub_args,
        "grub_settings": read_grub_settings(),
        "build_tools": {
            name: shutil.which(name) is not None
            for name in ("apt-get", "dpkg-query", "pkexec")
        },
        "build_dependencies": missing_build_dependencies(),
        "supported": distro.get("ID", "").lower() in {"ubuntu", "debian"}
        and platform.machine() in {"x86_64", "amd64"},
    }


def _append_log(job_id: str, message: str) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        job["logs"].append(message.rstrip())
        job["logs"] = job["logs"][-250:]
        job["updated_at"] = time.time()


def _run_logged(job_id: str, command: list[str], cwd: Path, timeout: int) -> None:
    _append_log(job_id, "$ " + " ".join(command))
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )
    assert process.stdout is not None

    def drain_output() -> None:
        for line in process.stdout:
            _append_log(job_id, line)

    reader = threading.Thread(target=drain_output, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        reader.join()
        raise RuntimeError("Команда превысила допустимое время выполнения.") from exc
    reader.join()
    if code != 0:
        raise RuntimeError(f"Команда завершилась с кодом {code}. Проверьте журнал.")


def _download_kernel_archive(job_id: str, version: str, job_dir: Path) -> Path:
    url = kernel_archive_url(version)
    archive_path = job_dir / f"linux-{version}.tar.xz"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"OpenSock/{__version__}"},
    )
    _append_log(job_id, f"Загрузка официальных исходников: {url}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if urlparse(response.geturl()).hostname != "cdn.kernel.org":
                raise RuntimeError("Загрузка перенаправлена с официального сервера kernel.org.")
            length = response.headers.get("Content-Length")
            expected_length = int(length) if length else None
            if expected_length is not None and expected_length < 0:
                raise RuntimeError("kernel.org вернул некорректный размер архива.")
            if expected_length is not None and expected_length > MAX_KERNEL_ARCHIVE_BYTES:
                raise RuntimeError("Архив исходников превышает допустимый размер 1 ГиБ.")
            downloaded = 0
            next_log = 10 * 1024 * 1024
            with archive_path.open("wb") as archive:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_KERNEL_ARCHIVE_BYTES:
                        raise RuntimeError("Архив исходников превышает допустимый размер 1 ГиБ.")
                    archive.write(chunk)
                    if downloaded >= next_log:
                        _append_log(
                            job_id,
                            f"Загружено {downloaded / 1024 / 1024:.0f} МиБ исходников.",
                        )
                        next_log = downloaded + 10 * 1024 * 1024
    except urllib.error.URLError as exc:
        archive_path.unlink(missing_ok=True)
        raise RuntimeError(f"Не удалось загрузить исходники с kernel.org: {exc}") from exc
    except (OSError, ValueError, RuntimeError):
        archive_path.unlink(missing_ok=True)
        raise
    if expected_length is not None and downloaded != expected_length:
        archive_path.unlink(missing_ok=True)
        raise RuntimeError("Загрузка архива kernel.org завершилась не полностью.")
    if downloaded == 0:
        archive_path.unlink(missing_ok=True)
        raise RuntimeError("kernel.org вернул пустой архив исходников.")
    _append_log(job_id, f"Загрузка завершена: {downloaded / 1024 / 1024:.0f} МиБ.")
    return archive_path


def _extract_kernel_archive(
    job_id: str,
    archive_path: Path,
    job_dir: Path,
    version: str,
) -> Path:
    expected_root = f"linux-{version}"
    source_dir = job_dir / expected_root
    with tarfile.open(archive_path, mode="r:xz") as archive:
        members = archive.getmembers()
        if not members:
            raise RuntimeError("Архив исходников Linux пуст.")
        for member in members:
            member_path = PurePosixPath(member.name)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or not member_path.parts
                or member_path.parts[0] != expected_root
                or member.mode & 0o6000
            ):
                raise RuntimeError("Архив kernel.org содержит недопустимый путь или режим файла.")
            if member.issym():
                target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(member.name), member.linkname)
                )
                if target != expected_root and not target.startswith(expected_root + "/"):
                    raise RuntimeError("Символическая ссылка в архиве выходит за каталог исходников.")
            elif member.islnk():
                target = posixpath.normpath(member.linkname)
                if target != expected_root and not target.startswith(expected_root + "/"):
                    raise RuntimeError("Жёсткая ссылка в архиве выходит за каталог исходников.")
            elif not (member.isfile() or member.isdir()):
                raise RuntimeError("Архив kernel.org содержит неподдерживаемый тип файла.")
        _append_log(job_id, f"Распаковка {len(members)} файлов исходного кода.")
        archive.extractall(path=job_dir, members=members)
    if not source_dir.is_dir():
        raise RuntimeError("В архиве kernel.org не найден каталог исходников.")
    return source_dir


def _build_worker(
    job_id: str,
    version: str,
    patch_text: str,
    install_dependencies: bool,
) -> None:
    job_dir = BUILD_ROOT / job_id
    archive_path: Path | None = None
    try:
        if install_dependencies:
            with _jobs_lock:
                _jobs[job_id]["status"] = "installing_dependencies"
            _append_log(job_id, "Запрос системного подтверждения для установки сборочных пакетов через APT.")
            _run_logged(
                job_id,
                [
                    "pkexec",
                    sys.executable,
                    str(ROOT / "privileged.py"),
                    "install-build-deps",
                ],
                job_dir,
                timeout=30 * 60,
            )
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
        archive_path = _download_kernel_archive(job_id, version, job_dir)
        source_dir = _extract_kernel_archive(job_id, archive_path, job_dir, version)
        archive_path.unlink(missing_ok=True)

        if patch_text:
            patch_file = job_dir / "opensock.patch"
            patch_file.write_text(patch_text, encoding="utf-8")
            _run_logged(
                job_id,
                ["patch", "--dry-run", "-p1", "-i", str(patch_file)],
                source_dir,
                timeout=60,
            )
            _run_logged(
                job_id,
                ["patch", "-p1", "-i", str(patch_file)],
                source_dir,
                timeout=60,
            )

        current_config = Path("/boot") / f"config-{platform.release()}"
        if current_config.is_file():
            shutil.copyfile(current_config, source_dir / ".config")
            _append_log(job_id, "Используется конфигурация текущего ядра.")
        else:
            _append_log(
                job_id,
                "Конфигурация текущего ядра не найдена; создаётся конфигурация по умолчанию.",
            )
            _run_logged(job_id, ["make", "defconfig"], source_dir, timeout=5 * 60)

        config_tool = source_dir / "scripts" / "config"
        if config_tool.is_file():
            _run_logged(
                job_id,
                [
                    str(config_tool),
                    "--set-str",
                    "SYSTEM_TRUSTED_KEYS",
                    "",
                    "--set-str",
                    "SYSTEM_REVOCATION_KEYS",
                    "",
                ],
                source_dir,
                timeout=60,
            )
        _run_logged(job_id, ["make", "olddefconfig"], source_dir, timeout=5 * 60)

        try:
            total_memory = (
                int(Path("/proc/meminfo").read_text().split("MemTotal:", 1)[1].split()[0])
                * 1024
            )
        except (OSError, ValueError, IndexError):
            total_memory = 2 * 1024 * 1024 * 1024
        memory_jobs = max(1, total_memory // (2 * 1024 * 1024 * 1024))
        parallel_jobs = max(1, min(os.cpu_count() or 1, memory_jobs, 8))
        with _jobs_lock:
            _jobs[job_id]["status"] = "building"
        _append_log(job_id, f"Компиляция ядра ({parallel_jobs} параллельных задач).")
        _run_logged(
            job_id,
            ["make", f"-j{parallel_jobs}", "bindeb-pkg", "LOCALVERSION=-opensock"],
            source_dir,
            timeout=12 * 60 * 60,
        )

        packages = sorted(job_dir.glob("*.deb"))
        if not packages:
            raise RuntimeError("Сборка завершилась без .deb-пакетов.")
        with _jobs_lock:
            _jobs[job_id]["status"] = "completed"
            _jobs[job_id]["packages"] = [
                {"name": package.name, "size": package.stat().st_size}
                for package in packages
            ]
        _append_log(job_id, f"Сборка завершена. Найдено пакетов: {len(packages)}.")
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)
        _append_log(job_id, f"Ошибка: {exc}")
    finally:
        try:
            if archive_path:
                archive_path.unlink(missing_ok=True)
        except OSError as exc:
            _append_log(job_id, f"Не удалось удалить временный архив: {exc}")
        finally:
            _build_lock.release()


class OpenSockHandler(BaseHTTPRequestHandler):
    server_version = f"OpenSock/{__version__}"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[opensock] {self.address_string()} - {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data: blob:; base-uri 'none'; "
            "frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _valid_host(self) -> bool:
        host = self.headers.get("Host", "").split(":")[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    def _read_json(self, max_bytes: int = MAX_REQUEST_BYTES) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Ожидался Content-Type: application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Некорректный Content-Length.") from exc
        if length <= 0 or length > max_bytes:
            raise ValueError("Размер запроса недопустим.")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("Ожидался JSON-объект.")
        return value

    def _check_origin_and_token(self) -> bool:
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                self._json(403, {"error": "Запросы с внешних сайтов запрещены."})
                return False
        return self._check_token()

    def _check_token(self) -> bool:
        if not secrets.compare_digest(
            self.headers.get("X-OpenSock-Token", ""), _csrf_token
        ):
            self._json(403, {"error": "Недействительный токен запроса."})
            return False
        return True

    def do_GET(self) -> None:
        if not self._valid_host():
            self._json(400, {"error": "Поддерживается только localhost."})
            return
        path = urlparse(self.path).path
        if path == "/":
            html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif path in {"/app.css", "/app.js"}:
            file_name = path.lstrip("/")
            content_type = "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
            self._send(200, (WEB_ROOT / file_name).read_bytes(), content_type)
        elif path == "/api/status":
            if not self._check_token():
                return
            self._json(200, system_status())
        elif path == "/api/network":
            if not self._check_token():
                return
            query = parse_qs(urlparse(self.path).query)
            scan_values = query.get("scan", [])
            if len(scan_values) > 1:
                self._json(400, {"error": "Укажите только один интерфейс Wi-Fi."})
                return
            try:
                self._json(200, network_status(scan_values[0] if scan_values else None))
            except (RuntimeError, ValueError) as exc:
                self._json(503, {"error": str(exc)})
        elif path == "/api/network-job":
            if not self._check_token():
                return
            with _network_lock:
                self._json(200, {"job": _network_job})
        elif path == "/api/grub-job":
            if not self._check_token():
                return
            with _jobs_lock:
                self._json(200, {"job": _grub_job})
        elif path == "/api/job":
            if not self._check_token():
                return
            with _jobs_lock:
                job = next(reversed(_jobs.values()), None)
                self._json(200, {"job": job})
        else:
            self._json(404, {"error": "Маршрут не найден."})

    def do_POST(self) -> None:
        if not self._valid_host():
            self._json(400, {"error": "Поддерживается только localhost."})
            return
        if not self._check_origin_and_token():
            return
        try:
            path = urlparse(self.path).path
            payload = self._read_json(
                MAX_GRUB_REQUEST_BYTES
                if path == "/api/grub/apply"
                else MAX_NETWORK_REQUEST_BYTES
                if path == "/api/network/apply"
                else MAX_REQUEST_BYTES
            )
            if path == "/api/build":
                self._start_build(payload)
            elif path == "/api/install":
                self._start_install(payload)
            elif path == "/api/network/apply":
                self._start_network_apply(payload)
            elif path == "/api/grub/apply":
                self._start_grub_apply(payload)
            else:
                self._json(404, {"error": "Маршрут не найден."})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._json(503, {"error": str(exc)})
        except OSError as exc:
            self._json(500, {"error": f"Не удалось выполнить операцию: {exc}"})

    def _start_build(self, payload: dict) -> None:
        status = system_status()
        if not status["supported"]:
            self._json(
                400,
                {"error": "Сборка поддерживается только на Ubuntu/Debian x86_64."},
            )
            return
        version = validate_version(payload.get("version"))
        patch_text = payload.get("patch", "")
        if not isinstance(patch_text, str) or len(patch_text.encode("utf-8")) > MAX_PATCH_BYTES:
            raise ValueError("Патч должен быть текстом размером не более 1 МиБ.")
        if patch_text and not patch_text.startswith(("--- ", "diff ")):
            raise ValueError("Патч должен быть в формате unified diff.")
        install_dependencies = payload.get("install_dependencies", False)
        if not isinstance(install_dependencies, bool):
            raise ValueError("Некорректное подтверждение установки сборочных пакетов.")

        missing_tools = [name for name, found in status["build_tools"].items() if not found]
        if missing_tools:
            raise ValueError(
                "Необходимые системные инструменты отсутствуют: "
                + ", ".join(missing_tools)
                + ". Установите их вручную, чтобы OpenSock мог запрашивать подтверждение."
            )
        missing_dependencies = status["build_dependencies"]
        if missing_dependencies and not install_dependencies:
            self._json(
                409,
                {
                    "error": "Для сборки нужны дополнительные пакеты APT.",
                    "missing_dependencies": missing_dependencies,
                },
            )
            return
        if missing_dependencies and not shutil.which("pkexec"):
            raise ValueError("Для установки сборочных пакетов через APT необходим pkexec.")
        if not _build_lock.acquire(blocking=False):
            self._json(409, {"error": "Сборка уже выполняется."})
            return

        job_id = uuid.uuid4().hex
        job_dir = BUILD_ROOT / job_id
        try:
            job_dir.mkdir(parents=True, mode=0o700)
        except OSError:
            _build_lock.release()
            raise
        job = {
            "id": job_id,
            "version": version,
            "status": "queued",
            "logs": [],
            "packages": [],
            "created_at": time.time(),
        }
        with _jobs_lock:
            _jobs[job_id] = job
        threading.Thread(
            target=_build_worker,
            args=(job_id, version, patch_text, bool(missing_dependencies)),
            daemon=True,
        ).start()
        self._json(202, {"job": job})

    def _start_network_apply(self, payload: dict) -> None:
        global _network_job
        if not shutil.which("pkexec"):
            raise ValueError("Для изменения сетевых настроек требуется pkexec.")
        request = validate_network_request(payload)
        status = network_status()
        if not status["available"]:
            raise ValueError(status["error"])
        device = next(
            (item for item in status["interfaces"] if item["name"] == request["interface"]),
            None,
        )
        if not device:
            raise ValueError("Сетевой интерфейс не найден в NetworkManager.")
        if request["operation"] == "configure":
            if (
                device["type"] not in {"ethernet", "wifi"}
                or device["connection_uuid"] != request["connection_uuid"]
                or not device["settings"]
            ):
                raise ValueError("Профиль больше не активен на выбранном интерфейсе. Обновите список сетей.")
        elif request["operation"] == "disconnect":
            if not device["connection_uuid"]:
                raise ValueError("На выбранном интерфейсе нет активного соединения.")
        elif request["operation"] == "connect_wifi" and device["type"] != "wifi":
            raise ValueError("Для подключения Wi-Fi выберите беспроводной интерфейс.")

        if not _network_operation_lock.acquire(blocking=False):
            self._json(409, {"error": "Другая сетевая операция уже выполняется."})
            return
        try:
            with _network_lock:
                _network_job = {
                    "id": uuid.uuid4().hex,
                    "status": "running",
                    "operation": request["operation"],
                    "logs": ["Ожидание системного подтверждения…"],
                    "updated_at": time.time(),
                }
                job_id = _network_job["id"]
        except Exception:
            _network_operation_lock.release()
            raise
        try:
            threading.Thread(
                target=self._network_worker,
                args=(job_id, request),
                daemon=True,
            ).start()
        except RuntimeError:
            _network_operation_lock.release()
            with _network_lock:
                _network_job = {
                    "id": job_id,
                    "status": "failed",
                    "logs": ["Не удалось запустить сетевую операцию."],
                    "updated_at": time.time(),
                }
            raise
        self._json(202, {"message": "Запрошено системное подтверждение."})

    @staticmethod
    def _network_worker(job_id: str, request: dict) -> None:
        global _network_job
        helper = ROOT / "privileged.py"
        try:
            process = subprocess.run(
                ["pkexec", sys.executable, str(helper), "network-apply"],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=3 * 60,
                check=False,
            )
            output = (process.stdout + process.stderr).strip()
            with _network_lock:
                _network_job = {
                    "id": job_id,
                    "status": "completed" if process.returncode == 0 else "failed",
                    "logs": output.splitlines()[-100:]
                    or [
                        "Сетевые настройки применены."
                        if process.returncode == 0
                        else f"Системный помощник завершился с кодом {process.returncode}."
                    ],
                    "updated_at": time.time(),
                }
        except (OSError, subprocess.SubprocessError) as exc:
            with _network_lock:
                _network_job = {
                    "id": job_id,
                    "status": "failed",
                    "logs": [str(exc)],
                    "updated_at": time.time(),
                }
        finally:
            _network_operation_lock.release()

    def _start_install(self, payload: dict) -> None:
        if not shutil.which("pkexec"):
            raise ValueError("Для подтверждаемой установки требуется pkexec.")
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not JOB_PATTERN.fullmatch(job_id):
            raise ValueError("Некорректный идентификатор сборки.")
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job or job["status"] != "completed":
                self._json(409, {"error": "Сначала завершите сборку пакетов."})
                return
            if job.get("install_status") == "running":
                self._json(409, {"error": "Установка уже выполняется."})
                return
            job["install_status"] = "running"
            job["install_logs"] = []

        threading.Thread(
            target=self._install_worker,
            args=(job_id,),
            daemon=True,
        ).start()
        self._json(202, {"message": "Запрошено системное подтверждение."})

    @staticmethod
    def _install_worker(job_id: str) -> None:
        helper = ROOT / "privileged.py"
        command = [
            "pkexec",
            sys.executable,
            str(helper),
            "install-kernel",
            "--job-id",
            job_id,
        ]
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15 * 60,
                check=False,
            )
            output = (process.stdout + process.stderr).strip()
            with _jobs_lock:
                job = _jobs[job_id]
                job["install_status"] = (
                    "completed" if process.returncode == 0 else "failed"
                )
                job["install_logs"] = output.splitlines()[-100:]
                if process.returncode:
                    job["install_error"] = (
                        output or f"Системный помощник завершился с кодом {process.returncode}."
                    )
        except (OSError, subprocess.TimeoutExpired) as exc:
            with _jobs_lock:
                _jobs[job_id]["install_status"] = "failed"
                _jobs[job_id]["install_error"] = str(exc)

    def _start_grub_apply(self, payload: dict) -> None:
        global _grub_job
        if not shutil.which("pkexec"):
            raise ValueError("Для изменения GRUB требуется pkexec.")
        status = system_status()
        if not status["supported"]:
            raise ValueError("Настройка GRUB поддерживается на Ubuntu/Debian x86_64.")
        if not status["bootloader"]["available"]:
            raise ValueError("Не найдены GRUB и update-grub.")
        settings = validate_grub_settings(payload.get("settings"))
        wallpaper = payload.get("wallpaper")
        if settings["remove_wallpaper"] and wallpaper:
            raise ValueError("Нельзя одновременно загрузить и удалить обои.")
        kernel_args = validate_kernel_args(payload.get("kernel_args", ""))

        wallpaper_id = None
        staged_path = None
        if wallpaper:
            if not isinstance(wallpaper, dict):
                raise ValueError("Некорректный файл обоев.")
            image_type = wallpaper.get("type")
            encoded = wallpaper.get("data")
            if image_type not in {"png", "jpeg"} or not isinstance(encoded, str):
                raise ValueError("Поддерживаются только файлы PNG и JPEG.")
            if len(encoded) > ((MAX_GRUB_IMAGE_BYTES + 2) // 3) * 4:
                raise ValueError("Размер обоев не должен превышать 8 МиБ.")
            try:
                image_data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("Не удалось прочитать файл обоев.") from exc
            if len(image_data) > MAX_GRUB_IMAGE_BYTES:
                raise ValueError("Размер обоев не должен превышать 8 МиБ.")
            is_png = image_data.startswith(b"\x89PNG\r\n\x1a\n")
            is_jpeg = image_data.startswith(b"\xff\xd8\xff")
            if (image_type == "png" and not is_png) or (image_type == "jpeg" and not is_jpeg):
                raise ValueError("Содержимое файла не совпадает с форматом изображения.")
            GRUB_IMAGE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
            GRUB_IMAGE_ROOT.chmod(0o700)
            wallpaper_id = uuid.uuid4().hex
            staged_path = GRUB_IMAGE_ROOT / f"{wallpaper_id}.{image_type}"
            staged_path.write_bytes(image_data)
            staged_path.chmod(0o600)

        if not _grub_lock.acquire(blocking=False):
            if staged_path:
                staged_path.unlink(missing_ok=True)
            self._json(409, {"error": "Применение настроек GRUB уже выполняется."})
            return

        job_id = uuid.uuid4().hex
        with _jobs_lock:
            _grub_job = {
                "id": job_id,
                "status": "running",
                "logs": ["Запрошено системное подтверждение."],
                "created_at": time.time(),
            }
        threading.Thread(
            target=self._grub_worker,
            args=(
                job_id,
                settings,
                kernel_args,
                wallpaper_id,
                wallpaper and wallpaper["type"],
                staged_path,
            ),
            daemon=True,
        ).start()
        self._json(202, {"job": _grub_job})

    @staticmethod
    def _grub_worker(
        job_id: str,
        settings: dict,
        kernel_args: str,
        wallpaper_id: str | None,
        wallpaper_type: str | None,
        staged_path: Path | None,
    ) -> None:
        global _grub_job
        helper = ROOT / "privileged.py"
        command = [
            "pkexec",
            sys.executable,
            str(helper),
            "grub-apply",
            "--timeout",
            settings["timeout"],
            "--timeout-style",
            settings["timeout_style"],
            "--default",
            settings["default"],
            "--color-normal",
            settings["color_normal"],
            "--color-highlight",
            settings["color_highlight"],
            "--kernel-args",
            kernel_args,
        ]
        if wallpaper_id and wallpaper_type:
            command.extend(["--wallpaper-id", wallpaper_id, "--wallpaper-type", wallpaper_type])
        elif settings["remove_wallpaper"]:
            command.append("--remove-wallpaper")

        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10 * 60,
                check=False,
            )
            output = (process.stdout + process.stderr).strip()
            with _jobs_lock:
                _grub_job = {
                    "id": job_id,
                    "status": "completed" if process.returncode == 0 else "failed",
                    "logs": output.splitlines()[-100:] or [
                        "Настройки GRUB применены."
                        if process.returncode == 0
                        else f"Помощник завершился с кодом {process.returncode}."
                    ],
                    "updated_at": time.time(),
                }
        except (OSError, subprocess.TimeoutExpired) as exc:
            with _jobs_lock:
                _grub_job = {
                    "id": job_id,
                    "status": "failed",
                    "logs": [str(exc)],
                    "updated_at": time.time(),
                }
        finally:
            if staged_path:
                staged_path.unlink(missing_ok=True)
            _grub_lock.release()


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit(
            "Не запускайте веб-сервер от root. Установку выполняет отдельный помощник pkexec."
        )
    BUILD_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    BUILD_ROOT.chmod(0o700)
    GRUB_IMAGE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    GRUB_IMAGE_ROOT.chmod(0o700)
    server = ThreadingHTTPServer(("127.0.0.1", 8765), OpenSockHandler)
    url = f"http://127.0.0.1:8765/#{_csrf_token}"
    print(f"OpenSock запущен: {url}")
    if not webbrowser.open(url, new=2):
        print("Не удалось открыть браузер автоматически. Перейдите по ссылке выше.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nOpenSock остановлен.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

JOB_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ARG_PATTERN = re.compile(r"^[A-Za-z0-9_.=,:/+@%-]+$")
PACKAGE_PATTERN = re.compile(r"^linux-(image|modules|modules-extra|headers)-[A-Za-z0-9.+:~_-]+\.deb$")
INTERFACE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
CONNECTION_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
MAC_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
MAX_NETWORK_REQUEST_BYTES = 16 * 1024
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
GRUB_COLORS = {
    "black", "blue", "green", "cyan", "red", "magenta", "brown",
    "light-gray", "dark-gray", "light-blue", "light-green", "light-cyan",
    "light-red", "light-magenta", "yellow", "white",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _validate_kernel_args(value: str) -> str:
    if len(value) > 2048:
        raise ValueError("Параметры ядра слишком длинные.")
    args = shlex.split(value)
    if any(not ARG_PATTERN.fullmatch(arg) for arg in args):
        raise ValueError("Параметры ядра содержат неподдерживаемые символы.")
    return " ".join(args)


def _requesting_uid() -> int:
    if os.geteuid() != 0:
        raise RuntimeError("Помощник должен быть запущен через pkexec.")
    try:
        return int(os.environ["PKEXEC_UID"])
    except (KeyError, ValueError) as exc:
        raise RuntimeError("Не удалось определить пользователя, запросившего операцию.") from exc


def _require_supported_platform() -> None:
    distro_id = ""
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("ID="):
                distro_id = line.partition("=")[2].strip().strip('"').lower()
                break
    except OSError as exc:
        raise RuntimeError(f"Не удалось определить дистрибутив: {exc}") from exc
    if distro_id not in {"ubuntu", "debian"} or platform.machine() not in {
        "x86_64",
        "amd64",
    }:
        raise RuntimeError("Операция поддерживается только на Ubuntu/Debian x86_64.")


def _require_networkmanager() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Помощник должен быть запущен через pkexec.")
    distro_id = ""
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("ID="):
                distro_id = line.partition("=")[2].strip().strip('"').lower()
                break
    except OSError as exc:
        raise RuntimeError(f"Не удалось определить дистрибутив: {exc}") from exc
    if distro_id not in {"ubuntu", "debian"}:
        raise RuntimeError("Управление сетью поддерживается на Ubuntu/Debian с NetworkManager.")
    _requesting_uid()
    if not shutil.which("nmcli"):
        raise RuntimeError("NetworkManager (команда nmcli) не установлен.")


def _validate_network_request(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Ожидались настройки сети.")
    operation = payload.get("operation")
    interface = payload.get("interface")
    if not isinstance(interface, str) or not INTERFACE_PATTERN.fullmatch(interface):
        raise ValueError("Выберите допустимый сетевой интерфейс.")
    if operation == "disconnect":
        return {"operation": operation, "interface": interface}
    mac_mode = payload.get("mac_mode", "permanent")
    if mac_mode not in {"permanent", "random", "custom"}:
        raise ValueError("Выберите допустимый режим MAC-адреса.")
    mac_address = payload.get("mac_address", "")
    if mac_mode == "custom":
        if not isinstance(mac_address, str) or not MAC_PATTERN.fullmatch(mac_address):
            raise ValueError("Введите MAC-адрес в формате 02:11:22:33:44:55.")
        octets = bytes.fromhex(mac_address.replace(":", ""))
        if octets[0] & 1 or not any(octets):
            raise ValueError("Укажите допустимый индивидуальный (unicast) MAC-адрес.")
        mac_address = mac_address.lower()
    else:
        mac_address = ""
    if operation == "configure":
        connection_uuid = payload.get("connection_uuid")
        if not isinstance(connection_uuid, str) or not CONNECTION_UUID_PATTERN.fullmatch(connection_uuid):
            raise ValueError("Некорректный профиль NetworkManager.")
        method = payload.get("ipv4_method")
        if method not in {"auto", "manual"}:
            raise ValueError("Выберите DHCP или ручную настройку IPv4.")
        address = payload.get("ipv4_address", "")
        gateway = payload.get("ipv4_gateway", "")
        if not isinstance(address, str) or not isinstance(gateway, str):
            raise ValueError("Некорректные адрес или шлюз IPv4.")
        address, gateway = address.strip(), gateway.strip()
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
        dns = payload.get("dns", [])
        if not isinstance(dns, list) or len(dns) > 6:
            raise ValueError("Укажите не более шести DNS-серверов.")
        normalized_dns = []
        for value in dns:
            if not isinstance(value, str):
                raise ValueError("Некорректный адрес DNS-сервера.")
            try:
                normalized_dns.append(str(ipaddress.ip_address(value.strip())))
            except ValueError as exc:
                raise ValueError("Некорректный адрес DNS-сервера.") from exc
        return {
            "operation": operation,
            "interface": interface,
            "connection_uuid": connection_uuid.lower(),
            "ipv4_method": method,
            "ipv4_address": address,
            "ipv4_gateway": gateway,
            "dns": list(dict.fromkeys(normalized_dns)),
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
            raise ValueError("Недопустимое имя Wi-Fi сети.")
        security = payload.get("security")
        password = payload.get("password", "")
        if security not in {"open", "wpa-psk", "sae"}:
            raise ValueError("Поддерживаются открытые сети и WPA/WPA2/WPA3-Personal.")
        if security == "open" and password:
            raise ValueError("Для открытой сети пароль не требуется.")
        if security == "wpa-psk" and (
            not isinstance(password, str)
            or not (
                re.fullmatch(r"[0-9a-fA-F]{64}", password)
                or (
                    8 <= len(password) <= 63
                    and all(0x20 <= ord(char) <= 0x7E for char in password)
                )
            )
        ):
            raise ValueError("Некорректный пароль WPA.")
        if security == "sae" and (
            not isinstance(password, str)
            or not 1 <= len(password) <= 63
            or not all(0x20 <= ord(char) <= 0x7E for char in password)
        ):
            raise ValueError("Некорректный пароль WPA3-Personal.")
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


def _nmcli(command: list[str], timeout: int = 60) -> str:
    nmcli = shutil.which("nmcli")
    if not nmcli:
        raise RuntimeError("Не найдена команда nmcli.")
    print("$ nmcli " + " ".join(command), flush=True)
    result = subprocess.run(
        [nmcli, *command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(detail or f"nmcli завершился с кодом {result.returncode}.")
    return result.stdout.strip()


def _nmcli_rows(output: str) -> list[list[str]]:
    rows = []
    for line in output.splitlines():
        fields, field = [], []
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
        rows.append(fields)
    return rows


def _assert_network_device(interface: str, expected_type: str | None = None) -> str:
    output = _nmcli([
        "--terse", "--escape", "yes", "--fields", "DEVICE,TYPE",
        "device", "status",
    ])
    for fields in _nmcli_rows(output):
        if len(fields) >= 2 and fields[0] == interface:
            if fields[1] not in {"ethernet", "wifi"}:
                raise ValueError("OpenSock изменяет только Ethernet и Wi-Fi интерфейсы.")
            if expected_type and fields[1] != expected_type:
                raise ValueError("Тип выбранного сетевого интерфейса не совпадает.")
            return fields[1]
    raise ValueError("Интерфейс отсутствует в NetworkManager.")


def _assert_active_connection(connection_uuid: str, interface: str) -> str:
    output = _nmcli([
        "--terse", "--escape", "yes", "--fields", "UUID,DEVICE",
        "connection", "show", "--active",
    ])
    for fields in _nmcli_rows(output):
        if len(fields) >= 2 and fields[0].lower() == connection_uuid.lower():
            if fields[1] != interface:
                raise ValueError("Профиль больше не активен на выбранном интерфейсе.")
            return fields[1]
    raise ValueError("Профиль NetworkManager больше не активен. Обновите список сетей.")


def _nmcli_get(connection_uuid: str, setting: str) -> str:
    value = _nmcli(["--get-values", setting, "connection", "show", connection_uuid])
    if value == "--":
        return ""
    if setting in {"ipv4.addresses", "ipv4.dns", "ipv6.dns"}:
        return ",".join(item for item in value.splitlines() if item and item != "--")
    return value


def _nmcli_modify(connection_uuid: str, settings: dict[str, str]) -> None:
    arguments = ["connection", "modify", connection_uuid]
    for key, value in settings.items():
        arguments.extend([key, value])
    _nmcli(arguments)


def _reconnect_profile(connection_uuid: str, interface: str) -> None:
    _nmcli(["connection", "up", connection_uuid, "ifname", interface], timeout=120)


def _configure_network(request: dict) -> None:
    interface = request["interface"]
    connection_uuid = request["connection_uuid"]
    device_type = _assert_network_device(interface)
    _assert_active_connection(connection_uuid, interface)
    clone_key = (
        "802-11-wireless.cloned-mac-address"
        if device_type == "wifi"
        else "802-3-ethernet.cloned-mac-address"
    )
    settings_to_modify = [
        "ipv4.method",
        "ipv4.addresses",
        "ipv4.gateway",
        "ipv4.dns",
        "ipv4.ignore-auto-dns",
        "ipv6.dns",
        "ipv6.ignore-auto-dns",
        clone_key,
    ]
    original = {key: _nmcli_get(connection_uuid, key) for key in settings_to_modify}
    dns4 = [address for address in request["dns"] if ipaddress.ip_address(address).version == 4]
    dns6 = [address for address in request["dns"] if ipaddress.ip_address(address).version == 6]
    cloned_mac = request["mac_address"] if request["mac_mode"] == "custom" else request["mac_mode"]
    updated = {
        "ipv4.method": request["ipv4_method"],
        "ipv4.addresses": request["ipv4_address"] if request["ipv4_method"] == "manual" else "",
        "ipv4.gateway": request["ipv4_gateway"] if request["ipv4_method"] == "manual" else "",
        "ipv4.dns": ",".join(dns4),
        "ipv4.ignore-auto-dns": "yes" if request["dns"] else "no",
        "ipv6.dns": ",".join(dns6),
        "ipv6.ignore-auto-dns": "yes" if request["dns"] else "no",
        clone_key: cloned_mac,
    }
    _nmcli_modify(connection_uuid, updated)
    try:
        _reconnect_profile(connection_uuid, interface)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        try:
            _nmcli_modify(connection_uuid, original)
            _reconnect_profile(connection_uuid, interface)
        except (OSError, RuntimeError, subprocess.SubprocessError) as rollback_error:
            raise RuntimeError(
                f"Не удалось применить настройки сети ({exc}); откат тоже завершился ошибкой ({rollback_error})."
            ) from exc
        raise RuntimeError(f"Настройки сети отменены, исходный профиль восстановлен: {exc}") from exc
    print("Сетевой профиль обновлён и переподключён.", flush=True)


def _keyfile_escape(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    if escaped.startswith(" "):
        escaped = "\\s" + escaped[1:]
    if escaped.endswith(" "):
        escaped = escaped[:-1] + "\\s"
    return escaped


def _connect_wifi(request: dict) -> None:
    interface = request["interface"]
    _assert_network_device(interface, "wifi")
    connection_uuid = str(uuid.uuid4())
    connection_name = f"OpenSock WiFi {connection_uuid[:8]}"
    cloned_mac = request["mac_address"] if request["mac_mode"] == "custom" else request["mac_mode"]
    lines = [
        "[connection]",
        f"id={connection_name}",
        f"uuid={connection_uuid}",
        "type=wifi",
        f"interface-name={interface}",
        "",
        "[wifi]",
        "mode=infrastructure",
        f"ssid={_keyfile_escape(request['ssid'])}",
        f"cloned-mac-address={cloned_mac}",
    ]
    if request["security"] in {"wpa-psk", "sae"}:
        lines.extend([
            "",
            "[wifi-security]",
            f"key-mgmt={request['security']}",
            f"psk={_keyfile_escape(request['password'])}",
        ])
    lines.extend(["", "[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""])

    profile_directory = Path("/etc/NetworkManager/system-connections")
    if profile_directory.is_symlink() or not profile_directory.is_dir():
        raise RuntimeError("Не найден безопасный каталог системных профилей NetworkManager.")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="opensock-", suffix=".nmconnection", dir=profile_directory
    )
    profile_path = Path(temporary_name)
    loaded = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as profile:
            profile.write("\n".join(lines))
            profile.flush()
            os.fsync(profile.fileno())
        _nmcli(["connection", "load", str(profile_path)])
        loaded = True
        try:
            _reconnect_profile(connection_uuid, interface)
        except (OSError, RuntimeError, subprocess.SubprocessError) as connection_error:
            try:
                _nmcli(["connection", "delete", connection_uuid])
                profile_path.unlink(missing_ok=True)
            except (OSError, RuntimeError, subprocess.SubprocessError) as cleanup_error:
                raise RuntimeError(
                    f"Не удалось подключиться к Wi-Fi ({connection_error}); профиль сохранён в NetworkManager."
                ) from cleanup_error
            raise
    except Exception:
        if profile_path.exists() and not loaded:
            profile_path.unlink(missing_ok=True)
        raise
    print(f"Wi-Fi подключён. Профиль сохранён в NetworkManager ({connection_name}).", flush=True)


def _apply_network_request(payload: object) -> None:
    _require_networkmanager()
    request = _validate_network_request(payload)
    _assert_network_device(
        request["interface"],
        "wifi" if request["operation"] == "connect_wifi" else None,
    )
    if request["operation"] == "configure":
        _configure_network(request)
    elif request["operation"] == "disconnect":
        _assert_active_connection(
            _active_connection_for_device(request["interface"]),
            request["interface"],
        )
        _nmcli(["device", "disconnect", request["interface"]], timeout=120)
        print("Сетевой интерфейс отключён.", flush=True)
    elif request["operation"] == "connect_wifi":
        _connect_wifi(request)


def _active_connection_for_device(interface: str) -> str:
    output = _nmcli([
        "--terse", "--escape", "yes", "--fields", "UUID,DEVICE",
        "connection", "show", "--active",
    ])
    for fields in _nmcli_rows(output):
        if len(fields) >= 2 and fields[1] == interface:
            return fields[0]
    raise ValueError("На выбранном интерфейсе нет активного соединения.")


def _read_network_request() -> object:
    raw = sys.stdin.buffer.read(MAX_NETWORK_REQUEST_BYTES + 1)
    if len(raw) > MAX_NETWORK_REQUEST_BYTES:
        raise ValueError("Размер настроек сети превышает допустимый предел.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Не удалось прочитать настройки сети.") from exc


def _job_directory(uid: int, job_id: str) -> Path:
    if not JOB_PATTERN.fullmatch(job_id):
        raise ValueError("Некорректный идентификатор сборки.")
    home = Path(pwd.getpwuid(uid).pw_dir).resolve(strict=True)
    state_root = home / ".local" / "state" / "opensock" / "builds"
    job_dir = state_root / job_id
    resolved_root = state_root.resolve(strict=True)
    resolved_job = job_dir.resolve(strict=True)
    if resolved_job.parent != resolved_root or job_dir.is_symlink():
        raise ValueError("Каталог сборки находится вне разрешённого расположения.")
    if job_dir.stat().st_uid != uid:
        raise ValueError("Владелец каталога сборки не совпадает с текущим пользователем.")
    return job_dir


def _run(
    command: list[str],
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> None:
    print("$ " + " ".join(command), flush=True)
    subprocess.run(command, check=True, timeout=timeout, env=env)


def install_build_dependencies() -> None:
    _require_supported_platform()
    _requesting_uid()
    apt_get = shutil.which("apt-get")
    if not apt_get:
        raise RuntimeError("Не найдена команда apt-get.")
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    _run([apt_get, "update"], timeout=15 * 60, env=env)
    _run(
        [
            apt_get,
            "install",
            "--assume-yes",
            "--no-install-recommends",
            "--no-remove",
            "--no-upgrade",
            *BUILD_DEPENDENCIES,
        ],
        timeout=30 * 60,
        env=env,
    )
    print("Инструменты сборки установлены через APT.", flush=True)


def install_kernel(job_id: str) -> None:
    _require_supported_platform()
    uid = _requesting_uid()
    job_dir = _job_directory(uid, job_id)

    packages = []
    for package in sorted(job_dir.iterdir()):
        if not PACKAGE_PATTERN.fullmatch(package.name):
            continue
        info = package.lstat()
        if not package.is_file() or package.is_symlink() or info.st_uid != uid:
            raise ValueError(f"Недопустимый пакет: {package.name}")
        packages.append(package)
    if not any(path.name.startswith("linux-image-") for path in packages):
        raise ValueError("В сборке не найден пакет linux-image.")

    _run(["dpkg", "-i", *(str(path) for path in packages)], timeout=15 * 60)
    print("Пакеты ядра установлены. Настройки GRUB находятся в отдельном разделе.", flush=True)


def _validate_color(value: str) -> str:
    parts = value.split("/")
    if len(parts) != 2 or any(part not in GRUB_COLORS for part in parts):
        raise ValueError("Недопустимый цвет GRUB.")
    return value


def _replace_grub_variables(text: str, values: dict[str, str]) -> str:
    lines = text.splitlines()
    for key, value in values.items():
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        replacement = f"{key}={shlex.quote(value)}"
        indexes = [index for index, line in enumerate(lines) if pattern.match(line)]
        if indexes:
            lines[indexes[0]] = replacement
            for index in reversed(indexes[1:]):
                del lines[index]
        else:
            lines.append(replacement)
    return "\n".join(lines) + "\n"


def _read_staged_image(uid: int, image_id: str, image_type: str) -> bytes:
    if not JOB_PATTERN.fullmatch(image_id) or image_type not in {"png", "jpeg"}:
        raise ValueError("Некорректный идентификатор обоев.")
    home = Path(pwd.getpwuid(uid).pw_dir).resolve(strict=True)
    image_root = home / ".local" / "state" / "opensock" / "grub-images"
    resolved_root = image_root.resolve(strict=True)
    if resolved_root != image_root or image_root.stat().st_uid != uid:
        raise ValueError("Каталог обоев находится вне разрешённого расположения.")
    source = image_root / f"{image_id}.{image_type}"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_size > MAX_IMAGE_BYTES:
            raise ValueError("Недопустимый файл обоев.")
        with os.fdopen(descriptor, "rb", closefd=False) as image:
            data = image.read(MAX_IMAGE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Размер обоев не должен превышать 8 МиБ.")
    if image_type == "png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Файл не является PNG-изображением.")
    if image_type == "jpeg" and not data.startswith(b"\xff\xd8\xff"):
        raise ValueError("Файл не является JPEG-изображением.")
    return data


def _atomic_write(path: Path, data: bytes, reference: Path) -> None:
    stat = reference.stat()
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, stat.st_mode)
        os.chown(temporary_path, stat.st_uid, stat.st_gid)
        os.replace(temporary_path, path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def apply_grub(
    timeout_value: int,
    timeout_style_value: str,
    default_value: str,
    color_normal_value: str,
    color_highlight_value: str,
    wallpaper_id: str | None,
    wallpaper_type: str | None,
    remove_wallpaper: bool,
    kernel_args_value: str,
) -> None:
    _require_supported_platform()
    uid = _requesting_uid()
    if isinstance(timeout_value, bool) or not 0 <= timeout_value <= 600:
        raise ValueError("Таймаут GRUB должен быть от 0 до 600 секунд.")
    if timeout_style_value not in {"menu", "hidden", "countdown"}:
        raise ValueError("Недопустимый режим отображения меню GRUB.")
    if not re.fullmatch(r"[A-Za-z0-9_.:() -]{1,120}", default_value):
        raise ValueError("Некорректный пункт загрузки GRUB.")
    color_normal = _validate_color(color_normal_value)
    color_highlight = _validate_color(color_highlight_value)
    kernel_args = _validate_kernel_args(kernel_args_value)
    if not isinstance(remove_wallpaper, bool):
        raise ValueError("Некорректное значение удаления обоев.")
    if bool(wallpaper_id) != bool(wallpaper_type) or (remove_wallpaper and wallpaper_id):
        raise ValueError("Несовместимые настройки обоев.")

    update_grub = shutil.which("update-grub")
    if not update_grub:
        raise RuntimeError("Команда update-grub не найдена.")
    grub_file = Path("/etc/default/grub")
    original_config = grub_file.read_bytes()
    config_values = {
        "GRUB_TIMEOUT": str(timeout_value),
        "GRUB_TIMEOUT_STYLE": timeout_style_value,
        "GRUB_DEFAULT": default_value,
        "GRUB_COLOR_NORMAL": color_normal,
        "GRUB_COLOR_HIGHLIGHT": color_highlight,
        "GRUB_CMDLINE_LINUX_DEFAULT": kernel_args,
        "GRUB_TERMINAL_OUTPUT": "gfxterm",
        "GRUB_GFXMODE": "auto",
    }

    wallpaper_target = None
    wallpaper_data = None
    old_wallpaper = None
    if wallpaper_id and wallpaper_type:
        wallpaper_data = _read_staged_image(uid, wallpaper_id, wallpaper_type)
        extension = "png" if wallpaper_type == "png" else "jpg"
        wallpaper_target = Path("/boot/grub") / f"opensock-background.{extension}"
        config_values["GRUB_BACKGROUND"] = str(wallpaper_target)
    elif remove_wallpaper:
        config_values["GRUB_BACKGROUND"] = ""

    updated_config = _replace_grub_variables(
        original_config.decode("utf-8"), config_values
    ).encode("utf-8")
    backup_id = uuid.uuid4().hex
    backup_config = grub_file.with_name(f"grub.opensock-{backup_id}.bak")
    shutil.copy2(grub_file, backup_config)
    generated_config = Path("/boot/grub/grub.cfg")
    generated_backup = generated_config.with_name(f"grub.opensock-{backup_id}.cfg.bak")
    generated_original = generated_config.read_bytes() if generated_config.is_file() else None
    if generated_original is not None:
        shutil.copy2(generated_config, generated_backup)
    wallpaper_backup = None
    if wallpaper_target and wallpaper_target.exists():
        old_wallpaper = wallpaper_target.read_bytes()
        wallpaper_backup = wallpaper_target.with_name(
            f"{wallpaper_target.name}.opensock-{backup_id}.bak"
        )
        shutil.copy2(wallpaper_target, wallpaper_backup)

    try:
        if wallpaper_target and wallpaper_data is not None:
            wallpaper_target.parent.mkdir(parents=True, exist_ok=True)
            reference = wallpaper_target if wallpaper_target.exists() else grub_file
            _atomic_write(wallpaper_target, wallpaper_data, reference)
        _atomic_write(grub_file, updated_config, grub_file)
        _run([update_grub], timeout=300)
    except Exception:
        _atomic_write(grub_file, original_config, grub_file)
        if wallpaper_target:
            if old_wallpaper is None:
                wallpaper_target.unlink(missing_ok=True)
            else:
                _atomic_write(wallpaper_target, old_wallpaper, grub_file)
        try:
            _run([update_grub], timeout=300)
        except Exception as rollback_error:
            if generated_original is not None:
                _atomic_write(generated_config, generated_original, grub_file)
            print(f"ОШИБКА: откат GRUB также завершился ошибкой: {rollback_error}", file=sys.stderr)
        raise

    print(f"Настройки GRUB сохранены. Резервная копия: {backup_config}", flush=True)
    if generated_original is not None:
        print(f"Резервная копия меню: {generated_backup}", flush=True)
    if wallpaper_target:
        print(f"Обои установлены: {wallpaper_target}", flush=True)
    if wallpaper_backup:
        print(f"Старая версия обоев сохранена: {wallpaper_backup}", flush=True)
    if remove_wallpaper:
        print("Обои GRUB отключены.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ограниченный системный помощник OpenSock")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("network-apply")
    install_parser = subparsers.add_parser("install-kernel")
    install_parser.add_argument("--job-id", required=True)
    subparsers.add_parser("install-build-deps")
    grub_parser = subparsers.add_parser("grub-apply")
    grub_parser.add_argument("--timeout", type=int, required=True)
    grub_parser.add_argument("--timeout-style", choices=("menu", "hidden", "countdown"), required=True)
    grub_parser.add_argument("--default", required=True)
    grub_parser.add_argument("--color-normal", required=True)
    grub_parser.add_argument("--color-highlight", required=True)
    grub_parser.add_argument("--kernel-args", default="")
    grub_parser.add_argument("--wallpaper-id")
    grub_parser.add_argument("--wallpaper-type", choices=("png", "jpeg"))
    grub_parser.add_argument("--remove-wallpaper", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "network-apply":
            _apply_network_request(_read_network_request())
        elif args.action == "install-kernel":
            install_kernel(args.job_id)
        elif args.action == "install-build-deps":
            install_build_dependencies()
        elif args.action == "grub-apply":
            apply_grub(
                args.timeout,
                args.timeout_style,
                args.default,
                args.color_normal,
                args.color_highlight,
                args.wallpaper_id,
                args.wallpaper_type,
                args.remove_wallpaper,
                args.kernel_args,
            )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Ошибка OpenSock: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

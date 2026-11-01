const suppliedToken = window.location.hash.slice(1);
if (suppliedToken) sessionStorage.setItem("opensock-token", suppliedToken);
const token = suppliedToken || sessionStorage.getItem("opensock-token") || "";
const $ = (id) => document.getElementById(id);
let activeJobId = null;
let pollTimer = null;
let grubPollTimer = null;
let networkPollTimer = null;
let wallpaperPreviewUrl = null;
let networkInterfaces = [];

async function api(path, body) {
  const headers = {"X-OpenSock-Token": token};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || `Ошибка запроса: ${response.status}`);
    error.status = response.status;
    error.data = result;
    throw error;
  }
  return result;
}

function showError(message) {
  const notice = $("app-error");
  notice.textContent = message;
  notice.classList.remove("hidden");
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "Недоступно";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ", "ЭБ"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(unitIndex ? 1 : 0)} ${units[unitIndex]}`;
}

function formatUptime(seconds) {
  if (!Number.isFinite(seconds)) return "Недоступно";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return days ? `${days} д ${hours} ч` : `${hours} ч ${minutes} мин`;
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
    $("kernel").textContent = status.running_kernel;
    $("distribution").textContent = status.distribution;
    $("architecture").textContent = `Архитектура: ${status.architecture}`;
    $("hostname").textContent = status.hostname || "Недоступно";
    $("architecture-detail").textContent = status.architecture || "Недоступно";
    $("secure-boot").textContent = status.secure_boot || "Неизвестно";
    $("cpu-model").textContent = status.cpu?.model || "Недоступно";
    $("cpu-threads").textContent = status.cpu?.threads ?? "Недоступно";
    $("uptime").textContent = formatUptime(status.uptime_seconds);

    const memoryTotal = status.memory?.total_bytes;
    const memoryAvailable = status.memory?.available_bytes;
    const memoryUsed = Number.isFinite(memoryTotal) && Number.isFinite(memoryAvailable)
      ? Math.max(0, memoryTotal - memoryAvailable)
      : null;
    $("memory-used").textContent = formatBytes(memoryUsed);
    $("memory-total").textContent = formatBytes(memoryTotal);
    $("memory-available").textContent = formatBytes(memoryAvailable);
    $("memory-bar").style.width = memoryTotal ? `${Math.min(100, memoryUsed / memoryTotal * 100)}%` : "0%";

    const disk = status.root_disk || {};
    $("disk-used").textContent = formatBytes(disk.used_bytes);
    $("disk-total").textContent = formatBytes(disk.total_bytes);
    $("disk-free").textContent = formatBytes(disk.free_bytes);
    $("disk-bar").style.width = disk.total_bytes ? `${Math.min(100, disk.used_bytes / disk.total_bytes * 100)}%` : "0%";

    $("bootloader").textContent = status.bootloader?.name || "Не определён";
    $("bootloader-state").textContent = status.bootloader?.available
      ? "Конфигурация найдена"
      : "Утилита или конфигурация не найдена";
    $("bootloader-badge").textContent = status.bootloader?.available ? "● ДОСТУПЕН" : "● НЕ НАЙДЕН";
    $("bootloader-badge").classList.toggle("bootloader-missing", !status.bootloader?.available);
    $("apply-grub-button").disabled = !status.bootloader?.available;
    $("grub-notice").textContent = status.bootloader?.available
      ? ""
      : "Для настройки GRUB нужны установленный загрузчик и команда update-grub.";
    $("grub-notice").classList.toggle("hidden", Boolean(status.bootloader?.available));

    const grub = status.grub_settings || {};
    $("grub-timeout").value = grub.timeout ?? "5";
    $("grub-timeout-style").value = grub.timeout_style || "menu";
    $("grub-default").value = grub.default || "0";
    $("grub-kernel-args").value = status.grub_kernel_args || "";
    setGrubColor("grub-color-normal", grub.color_normal || "light-gray/black");
    setGrubColor("grub-color-highlight", grub.color_highlight || "black/light-gray");
    $("wallpaper-name").textContent = grub.background
      ? `Текущий фон: ${grub.background.split("/").pop()}`
      : "Файл не выбран";
    updateGrubPreview();
    const missing = Object.entries(status.build_tools).filter(([, ok]) => !ok).map(([name]) => name);
    const kernelNotice = $("kernel-notice");
    const kernelNoticeMessage = !status.supported
      ? "Управление ядром поддерживается только на Ubuntu/Debian x86_64."
      : missing.length
        ? `Необходимые системные инструменты отсутствуют: ${missing.join(", ")}.`
        : "";
    kernelNotice.textContent = kernelNoticeMessage;
    kernelNotice.classList.toggle("hidden", !kernelNoticeMessage);
  } catch (error) {
    showError(error.message);
  }
}

function setGrubColor(id, value) {
  const select = $(id);
  if (![...select.options].some((option) => option.value === value)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = `${value} · текущая настройка`;
    select.add(option);
  }
  select.value = value;
}

function showView(name) {
  const views = {
    menu: "menu-view",
    info: "info-view",
    kernel: "kernel-view",
    grub: "grub-view",
    network: "network-view"
  };
  Object.entries(views).forEach(([viewName, id]) => {
    $(id).classList.toggle("hidden", viewName !== name);
  });
  document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
    const active = item.dataset.view === name;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  const titles = {
    menu: "Меню",
    info: "Информация",
    kernel: "Ядро",
    grub: "Загрузчик GRUB",
    network: "Сеть и Wi-Fi"
  };
  const title = titles[name] || titles.menu;
  $("breadcrumb-current").textContent = title;
  document.title = `OpenSock — ${title.toLowerCase()}`;

  const activeView = $(views[name] || views.menu);
  activeView.classList.remove("animate-in");
  void activeView.offsetWidth;
  activeView.classList.add("animate-in");
  if (name === "network") {
    loadNetwork();
    clearTimeout(networkPollTimer);
    pollNetworkJob();
  }
}

let scrollFrame = null;
function updateScrollBackground() {
  if (scrollFrame !== null) return;
  scrollFrame = requestAnimationFrame(() => {
    const scrollable = document.documentElement.scrollHeight - window.innerHeight;
    const progress = scrollable > 0 ? Math.min(1, window.scrollY / scrollable) : 0;
    document.documentElement.style.setProperty("--scroll-x", `${progress * 100}%`);
    document.documentElement.style.setProperty("--scroll-shift", `${-window.scrollY * 0.08}px`);
    scrollFrame = null;
  });
}

window.addEventListener("scroll", updateScrollBackground, {passive: true});
window.addEventListener("resize", updateScrollBackground, {passive: true});
updateScrollBackground();

function showNetworkError(message) {
  const notice = $("network-notice");
  notice.textContent = message;
  notice.classList.remove("hidden");
}

function selectedNetworkInterface() {
  return networkInterfaces.find((device) => device.name === $("network-interface").value) || null;
}

function updateNetworkMacFields() {
  $("network-mac-address").classList.toggle("hidden", $("network-mac-mode").value !== "custom");
}

function updateNetworkAddressFields() {
  $("network-static-fields").classList.toggle(
    "hidden",
    $("network-ipv4-method").value !== "manual"
  );
}

function renderNetworkInterface() {
  const device = selectedNetworkInterface();
  const summary = $("network-interface-status");
  const hasProfile = Boolean(device?.connection_uuid && device.settings);
  $("network-apply").disabled = !hasProfile;
  $("network-disconnect").disabled = !device?.connection_uuid;
  $("wifi-scan").disabled = device?.type !== "wifi";
  $("wifi-connect").disabled = device?.type !== "wifi";
  if (!device) {
    summary.textContent = "NetworkManager не обнаружил Ethernet или Wi-Fi интерфейсы.";
    $("network-settings-form").reset();
    $("network-ipv4-method").value = "auto";
    updateNetworkAddressFields();
    $("wifi-network-list").textContent = "Выберите интерфейс Wi-Fi для поиска сетей.";
    return;
  }
  const addresses = [
    ...(device.ipv4_addresses || []),
    ...(device.ipv6_addresses || [])
  ];
  summary.textContent = [
    `${device.name} · ${device.type === "wifi" ? "Wi-Fi" : "Ethernet"} · ${device.state}`,
    device.connection ? `Профиль: ${device.connection}` : "Профиль не подключён",
    `MAC устройства: ${device.hw_address || "неизвестен"}`,
    addresses.length ? `Адреса: ${addresses.join(", ")}` : "IP-адреса не назначены"
  ].join("\n");
  if (device.settings) {
    const settings = device.settings;
    $("network-ipv4-method").value = settings.ipv4_method === "manual" ? "manual" : "auto";
    $("network-ipv4-address").value = settings.ipv4_address || "";
    $("network-ipv4-gateway").value = settings.ipv4_gateway || "";
    $("network-dns").value = [settings.dns, settings.ipv6_dns].filter(Boolean).join(", ");
    const mac = settings.mac_override || "permanent";
    if (mac === "random" || mac === "stable" || mac.startsWith("stable-")) {
      $("network-mac-mode").value = "random";
      $("network-mac-address").value = "";
    } else if (/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(mac)) {
      $("network-mac-mode").value = "custom";
      $("network-mac-address").value = mac;
    } else {
      $("network-mac-mode").value = "permanent";
      $("network-mac-address").value = "";
    }
  } else {
    $("network-ipv4-method").value = "auto";
    $("network-ipv4-address").value = "";
    $("network-ipv4-gateway").value = "";
    $("network-dns").value = "";
    $("network-mac-mode").value = "permanent";
    $("network-mac-address").value = "";
  }
  updateNetworkAddressFields();
  updateNetworkMacFields();
  $("wifi-network-list").textContent = device.type === "wifi"
    ? "Нажмите «Найти сети Wi-Fi» для сканирования."
    : "Выбранный интерфейс не поддерживает Wi-Fi.";
}

function renderWifiNetworks(networks) {
  const list = $("wifi-network-list");
  if (!networks.length) {
    list.textContent = "Сети не найдены. Проверьте, включён ли Wi-Fi, и запустите поиск ещё раз.";
    return;
  }
  list.replaceChildren(...networks.map((network) => {
    const button = document.createElement("button");
    const open = !network.security;
    const security = network.security.toUpperCase();
    const supported = open || (
      (security.includes("WPA") || security.includes("SAE"))
      && !security.includes("802.1X")
      && !security.includes("WEP")
    );
    const securityType = (security.includes("SAE") || security.includes("WPA3"))
      && !security.includes("WPA2")
      ? "sae"
      : "wpa-psk";
    button.type = "button";
    button.className = "wifi-network-option";
    button.disabled = !supported;
    button.dataset.ssid = network.ssid;
    button.dataset.security = open ? "open" : securityType;
    const name = document.createElement("span");
    name.textContent = network.ssid;
    const details = document.createElement("small");
    details.textContent = `${network.in_use ? "Подключено · " : ""}${network.security || "Открытая сеть"} · ${network.bssid}`;
    name.append(details);
    const signal = document.createElement("span");
    signal.className = "wifi-network-signal";
    signal.textContent = `${network.signal}%`;
    button.append(name, signal);
    if (!supported) button.title = "Поддерживаются открытые сети и WPA/WPA2/WPA3-Personal.";
    return button;
  }));
}

async function loadNetwork(scanInterface = null) {
  const notice = $("network-notice");
  notice.classList.add("hidden");
  $("network-interface-status").textContent = "Загрузка интерфейсов…";
  const query = scanInterface ? `?scan=${encodeURIComponent(scanInterface)}` : "";
  try {
    const status = await api(`/api/network${query}`);
    if (!status.available) {
      networkInterfaces = [];
      $("network-interface").replaceChildren();
      $("network-interface-status").textContent = status.error;
      $("network-settings-form").querySelectorAll("input,select,button").forEach((item) => {
        item.disabled = true;
      });
      $("network-disconnect").disabled = true;
      $("wifi-scan").disabled = true;
      $("wifi-connect").disabled = true;
      $("wifi-network-list").textContent = "Установите и запустите NetworkManager, чтобы управлять подключениями.";
      return;
    }
    const previousInterface = $("network-interface").value;
    networkInterfaces = status.interfaces;
    const options = networkInterfaces.map((device) => {
      const option = document.createElement("option");
      option.value = device.name;
      option.textContent = `${device.name} · ${device.type === "wifi" ? "Wi-Fi" : "Ethernet"}`;
      return option;
    });
    $("network-interface").replaceChildren(...options);
    if (networkInterfaces.some((device) => device.name === (scanInterface || previousInterface))) {
      $("network-interface").value = scanInterface || previousInterface;
    }
    $("network-settings-form").querySelectorAll("input,select,button").forEach((item) => {
      item.disabled = false;
    });
    renderNetworkInterface();
    if (scanInterface) renderWifiNetworks(status.wifi_networks);
  } catch (error) {
    $("network-interface-status").textContent = "Не удалось получить состояние сети.";
    showNetworkError(error.message);
  }
}

function renderNetworkJob(job) {
  if (!job) return;
  $("network-job-panel").classList.remove("hidden");
  const status = $("network-job-status");
  status.textContent = job.status === "running" ? "ОЖИДАНИЕ / ВЫПОЛНЕНИЕ" :
    job.status === "completed" ? "ГОТОВО" : "ОШИБКА";
  status.classList.toggle("network-job-success", job.status === "completed");
  status.classList.toggle("network-job-failed", job.status === "failed");
  $("network-job-log").textContent = (job.logs || []).join("\n");
  $("network-job-log").scrollTop = $("network-job-log").scrollHeight;
  const busy = job.status === "running";
  $("network-apply").disabled = busy || !selectedNetworkInterface()?.connection_uuid;
  $("network-disconnect").disabled = busy || !selectedNetworkInterface()?.connection_uuid;
  $("wifi-connect").disabled = busy || selectedNetworkInterface()?.type !== "wifi";
  $("wifi-scan").disabled = busy || selectedNetworkInterface()?.type !== "wifi";
  if (busy) {
    clearTimeout(networkPollTimer);
    networkPollTimer = setTimeout(pollNetworkJob, 1200);
  } else if (job.status === "completed") {
    loadNetwork();
  }
}

async function pollNetworkJob() {
  try {
    const {job} = await api("/api/network-job");
    renderNetworkJob(job);
  } catch (error) {
    showNetworkError(error.message);
  }
}

async function startNetworkOperation(request, confirmation) {
  if (!window.confirm(confirmation)) return;
  $("network-apply").disabled = true;
  $("network-disconnect").disabled = true;
  $("wifi-connect").disabled = true;
  $("wifi-scan").disabled = true;
  $("network-notice").classList.add("hidden");
  $("network-job-panel").classList.remove("hidden");
  $("network-job-status").textContent = "ОЖИДАНИЕ СИСТЕМНОГО ПОДТВЕРЖДЕНИЯ";
  $("network-job-log").textContent = "Запрос системного подтверждения через pkexec…";
  try {
    await api("/api/network/apply", request);
    clearTimeout(networkPollTimer);
    pollNetworkJob();
  } catch (error) {
    showNetworkError(error.message);
    renderNetworkInterface();
  }
}

document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
  item.addEventListener("click", () => showView(item.dataset.view));
});
document.querySelectorAll("[data-open-view]").forEach((item) => {
  item.addEventListener("click", () => showView(item.dataset.openView));
});
$("network-refresh").addEventListener("click", () => loadNetwork());
$("network-interface").addEventListener("change", () => {
  $("wifi-ssid").value = "";
  $("wifi-password").value = "";
  document.querySelectorAll(".wifi-network-option.selected").forEach((item) => {
    item.classList.remove("selected");
  });
  renderNetworkInterface();
});
$("network-ipv4-method").addEventListener("change", updateNetworkAddressFields);
$("network-mac-mode").addEventListener("change", updateNetworkMacFields);
$("wifi-scan").addEventListener("click", () => {
  const device = selectedNetworkInterface();
  if (device?.type === "wifi") loadNetwork(device.name);
});
$("wifi-network-list").addEventListener("click", (event) => {
  const button = event.target.closest(".wifi-network-option");
  if (!button || button.disabled) return;
  document.querySelectorAll(".wifi-network-option.selected").forEach((item) => {
    item.classList.remove("selected");
  });
  button.classList.add("selected");
  $("wifi-ssid").value = button.dataset.ssid;
  $("wifi-password").required = button.dataset.security === "wpa-psk";
  $("wifi-password").value = "";
  $("wifi-password").placeholder = button.dataset.security === "open"
    ? "Для открытой сети пароль не нужен"
    : "Пароль сети WPA/WPA2/WPA3-Personal";
});
$("network-settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const device = selectedNetworkInterface();
  if (!device?.connection_uuid) {
    showNetworkError("Для изменения настроек нужен активный профиль NetworkManager.");
    return;
  }
  const method = $("network-ipv4-method").value;
  const dns = $("network-dns").value.split(/[,\s]+/).filter(Boolean);
  const request = {
    operation: "configure",
    interface: device.name,
    connection_uuid: device.connection_uuid,
    ipv4_method: method,
    ipv4_address: method === "manual" ? $("network-ipv4-address").value.trim() : "",
    ipv4_gateway: method === "manual" ? $("network-ipv4-gateway").value.trim() : "",
    dns,
    mac_mode: $("network-mac-mode").value,
    mac_address: $("network-mac-mode").value === "custom" ? $("network-mac-address").value.trim() : ""
  };
  startNetworkOperation(
    request,
    `Изменить профиль ${device.connection} на интерфейсе ${device.name} и переподключиться? ` +
    "Связь может временно оборваться. Потребуется системное подтверждение."
  );
});
$("network-disconnect").addEventListener("click", () => {
  const device = selectedNetworkInterface();
  if (!device?.connection_uuid) return;
  startNetworkOperation(
    {operation: "disconnect", interface: device.name},
    `Отключить ${device.name} (${device.connection})? Интернет на этом интерфейсе пропадёт до повторного подключения.`
  );
});
$("wifi-connect-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const device = selectedNetworkInterface();
  if (device?.type !== "wifi") {
    showNetworkError("Выберите беспроводной интерфейс Wi-Fi.");
    return;
  }
  const selected = document.querySelector(".wifi-network-option.selected");
  const security = selected?.dataset.security || "wpa-psk";
  const ssid = $("wifi-ssid").value;
  const password = $("wifi-password").value;
  startNetworkOperation(
    {
      operation: "connect_wifi",
      interface: device.name,
      ssid,
      security,
      password: security === "open" ? "" : password,
      mac_mode: $("network-mac-mode").value,
      mac_address: $("network-mac-mode").value === "custom" ? $("network-mac-address").value.trim() : ""
    },
    `Подключиться к Wi-Fi «${ssid}» через ${device.name}? ` +
    "NetworkManager сохранит профиль и пароль в системном хранилище; потребуется подтверждение."
  );
});

const grubColorMap = {
  black: "#080b09", blue: "#183f82", green: "#20733b", cyan: "#147c86",
  red: "#922d2d", magenta: "#783b82", brown: "#79552e",
  "light-gray": "#d2d8d3", "dark-gray": "#505953", "light-blue": "#72a5f2",
  "light-green": "#a4f083", "light-cyan": "#7ed9de", "light-red": "#ff8b79",
  "light-magenta": "#df91ed", yellow: "#f4d761", white: "#f6f8f6"
};

function updateGrubPreview() {
  const normal = $("grub-color-normal").value.split("/");
  const highlight = $("grub-color-highlight").value.split("/");
  const preview = $("grub-preview-screen");
  preview.style.setProperty("--grub-fg", grubColorMap[normal[0]]);
  preview.style.setProperty("--grub-bg", grubColorMap[normal[1]]);
  preview.style.setProperty("--grub-highlight-fg", grubColorMap[highlight[0]]);
  preview.style.setProperty("--grub-highlight-bg", grubColorMap[highlight[1]]);
  preview.style.backgroundImage = wallpaperPreviewUrl ? `url("${wallpaperPreviewUrl}")` : "none";
  preview.classList.toggle("has-wallpaper", Boolean(wallpaperPreviewUrl));
  $("grub-preview-args").textContent = $("grub-kernel-args").value.trim() || " ";
}

function renderJob(job) {
  if (!job) return;
  activeJobId = job.id;
  $("job-panel").classList.remove("hidden");
  $("job-title").textContent = `Сборка ядра ${job.version}`;
  const labels = {queued: "В ОЧЕРЕДИ", installing_dependencies: "УСТАНОВКА APT-ПАКЕТОВ", running: "ЗАГРУЗКА ИСХОДНИКОВ", building: "СБОРКА", completed: "СБОРКА ЗАВЕРШЕНА", failed: "ОШИБКА"};
  $("job-status").textContent = labels[job.status] || job.status.toUpperCase();
  $("progress-bar").style.width = job.status === "completed" ? "100%" : job.status === "building" ? "68%" : job.status === "failed" ? "100%" : "25%";
  $("progress-bar").style.background = job.status === "failed" ? "var(--red)" : "var(--green)";
  $("job-log").textContent = (job.logs || []).join("\n");
  $("job-log").scrollTop = $("job-log").scrollHeight;
  $("package-list").replaceChildren(...(job.packages || []).map((pkg) => {
    const chip = document.createElement("span");
    chip.className = "package-chip";
    chip.textContent = `${pkg.name} · ${(pkg.size / 1024 / 1024).toFixed(1)} MiB`;
    return chip;
  }));
  const installStatus = job.install_status;
  $("install-button").disabled = job.status !== "completed" || installStatus === "running";
  $("install-hint").textContent = installStatus === "running" ? "Ожидается системное подтверждение…" :
    installStatus === "completed" ? "Пакеты ядра установлены. Настройки GRUB не изменялись." :
    installStatus === "failed" ? (job.install_error || "Установка завершилась ошибкой.") :
    job.status === "completed" ? "Проверьте список пакетов и подтвердите установку." : "Сначала соберите пакеты ядра.";
  if (installStatus) {
    $("install-result").classList.remove("hidden");
    $("install-result").textContent = (job.install_logs || []).join("\n") || job.install_error || "";
  }
  if (job.status === "completed" || job.status === "failed") $("build-button").disabled = false;
}

async function pollJob() {
  try {
    const {job} = await api("/api/job");
    if (job) {
      renderJob(job);
      if (job.status === "queued" || job.status === "installing_dependencies" || job.status === "running" || job.status === "building" || job.install_status === "running") {
        pollTimer = setTimeout(pollJob, 1500);
      }
    }
  } catch (error) {
    showError(error.message);
  }
}

$("build-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("build-button").disabled = true;
  $("job-panel").classList.remove("hidden");
  $("job-log").textContent = "Создание задачи…";
  try {
    const request = {
      version: $("version").value.trim(),
      patch: $("patch").value
    };
    let result;
    try {
      result = await api("/api/build", request);
    } catch (error) {
      const missing = error.data?.missing_dependencies;
      if (error.status !== 409 || !Array.isArray(missing) || !missing.length) throw error;
      const confirmed = window.confirm(
        `Не хватает пакетов для сборки:\n\n${missing.join(", ")}\n\n` +
        "Установить их через APT? Потребуется системное подтверждение."
      );
      if (!confirmed) {
        $("job-log").textContent = "Сборка отменена: установка недостающих пакетов не подтверждена.";
        $("build-button").disabled = false;
        return;
      }
      result = await api("/api/build", {...request, install_dependencies: true});
    }
    renderJob(result.job);
    clearTimeout(pollTimer);
    pollJob();
  } catch (error) {
    $("build-button").disabled = false;
    showError(error.message);
  }
});

$("install-button").addEventListener("click", async () => {
  if (!activeJobId) return;
  const confirmed = window.confirm("Установить собранные пакеты ядра? Настройки GRUB эта операция не меняет.");
  if (!confirmed) return;
  $("install-button").disabled = true;
  try {
    await api("/api/install", {job_id: activeJobId});
    clearTimeout(pollTimer);
    pollJob();
  } catch (error) {
    $("install-button").disabled = false;
    showError(error.message);
  }
});

$("grub-color-normal").addEventListener("change", updateGrubPreview);
$("grub-color-highlight").addEventListener("change", updateGrubPreview);
$("grub-kernel-args").addEventListener("input", updateGrubPreview);
$("grub-wallpaper").addEventListener("change", () => {
  const file = $("grub-wallpaper").files[0];
  $("wallpaper-name").textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "Файл не выбран";
  if (file) $("remove-wallpaper").checked = false;
  if (wallpaperPreviewUrl) URL.revokeObjectURL(wallpaperPreviewUrl);
  if (file) {
    wallpaperPreviewUrl = URL.createObjectURL(file);
    $("wallpaper-preview").src = wallpaperPreviewUrl;
    $("wallpaper-preview").classList.remove("hidden");
  } else {
    wallpaperPreviewUrl = null;
    $("wallpaper-preview").removeAttribute("src");
    $("wallpaper-preview").classList.add("hidden");
  }
  updateGrubPreview();
});
$("remove-wallpaper").addEventListener("change", () => {
  if ($("remove-wallpaper").checked) {
    $("grub-wallpaper").value = "";
    $("grub-wallpaper").dispatchEvent(new Event("change"));
  }
});

function fileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Не удалось прочитать файл обоев."));
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
    reader.readAsDataURL(file);
  });
}

$("apply-grub-button").addEventListener("click", async () => {
  const file = $("grub-wallpaper").files[0];
  if (file && file.size > 8 * 1024 * 1024) {
    $("grub-notice").textContent = "Размер обоев не должен превышать 8 МиБ.";
    $("grub-notice").classList.remove("hidden");
    return;
  }
  const confirmed = window.confirm("Сохранить параметры меню, цвета и выбранные обои в GRUB, затем пересоздать конфигурацию загрузчика?");
  if (!confirmed) return;
  $("apply-grub-button").disabled = true;
  $("grub-notice").classList.add("hidden");
  $("grub-apply-hint").textContent = "Подготовка настроек и запрос системного подтверждения…";
  try {
    const wallpaper = file ? {
      type: file.type === "image/png" ? "png" : "jpeg",
      data: await fileAsBase64(file)
    } : null;
    await api("/api/grub/apply", {
      settings: {
        timeout: Number($("grub-timeout").value),
        timeout_style: $("grub-timeout-style").value,
        default: $("grub-default").value.trim(),
        color_normal: $("grub-color-normal").value,
        color_highlight: $("grub-color-highlight").value,
        remove_wallpaper: $("remove-wallpaper").checked
      },
      kernel_args: $("grub-kernel-args").value,
      wallpaper
    });
    $("grub-job-panel").classList.remove("hidden");
    $("grub-job-log").textContent = "Ожидание системного подтверждения…";
    clearTimeout(grubPollTimer);
    pollGrubJob();
  } catch (error) {
    $("grub-notice").textContent = error.message;
    $("grub-notice").classList.remove("hidden");
    $("grub-apply-hint").textContent = "Настройки не применены.";
    $("apply-grub-button").disabled = false;
  }
});

async function pollGrubJob() {
  try {
    const {job} = await api("/api/grub-job");
    if (!job) return;
    $("grub-job-panel").classList.remove("hidden");
    $("grub-job-status").textContent = job.status === "running" ? "ВЫПОЛНЯЕТСЯ" :
      job.status === "completed" ? "ГОТОВО" : "ОШИБКА";
    $("grub-job-log").textContent = (job.logs || []).join("\n");
    $("grub-job-log").scrollTop = $("grub-job-log").scrollHeight;
    if (job.status === "running") {
      $("grub-apply-hint").textContent = "Ожидается системное подтверждение и обновление GRUB…";
      grubPollTimer = setTimeout(pollGrubJob, 1500);
    } else {
      $("apply-grub-button").disabled = false;
      $("grub-apply-hint").textContent = job.status === "completed"
        ? "Настройки GRUB обновлены."
        : "Не удалось обновить GRUB. Подробности — в журнале.";
      if (job.status === "completed") loadStatus();
    }
  } catch (error) {
    $("grub-notice").textContent = error.message;
    $("grub-notice").classList.remove("hidden");
    $("apply-grub-button").disabled = false;
  }
}
$("refresh-status").addEventListener("click", loadStatus);
showView("menu");
if (token) {
  history.replaceState(null, "", "/");
  loadStatus();
  pollJob();
  pollGrubJob();
} else {
  showError("Для доступа откройте ссылку OpenSock из терминала, где запущен сервер.");
  $("build-button").disabled = true;
  $("apply-grub-button").disabled = true;
}

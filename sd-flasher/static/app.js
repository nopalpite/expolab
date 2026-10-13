"use strict";

const $ = (id) => document.getElementById(id);
const NEW_MACHINE = "__new__";

function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
        if (key === "text") node.textContent = value;
        else if (key === "class") node.className = value;
        else node.setAttribute(key, value);
    }
    children.forEach((child) => child && node.appendChild(child));
    return node;
}

function formatBytes(n) {
    if (!n) return "0 o";
    const units = ["o", "Ko", "Mo", "Go", "To"];
    let i = 0;
    let value = n;
    while (value >= 1000 && i < units.length - 1) { value /= 1000; i += 1; }
    return `${value >= 100 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

function formatTime(epoch) {
    return new Date(epoch * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "medium" });
}

function showStatus(node, message, kind) {
    node.hidden = false;
    node.className = "status status-" + kind;
    node.textContent = message;
}

async function api(url, options) {
    const res = await fetch(url, options);
    let data = {};
    try { data = await res.json(); } catch (e) { /* corps vide */ }
    if (!res.ok) throw new Error(data.error || `Erreur ${res.status}`);
    return data;
}

const state = { status: null, formDevice: null, previewFor: null };

// --- carte -----------------------------------------------------------------------------

function selectedDevice() {
    const eligible = state.status.devices.eligible;
    const chosen = $("device-select").value;
    return eligible.find((d) => d.path === chosen) || eligible[0];
}

function selectedHostname() {
    const value = $("host-select").value;
    return value === NEW_MACHINE ? $("host-new").value.trim() : value;
}

function renderDeviceInfo() {
    const dev = selectedDevice();
    const info = $("device-info");
    info.textContent = "";
    if (!dev) return;
    info.appendChild(el("strong", { text: dev.model || "Carte SD" }));
    info.appendChild(el("span", { text: ` · ${formatBytes(dev.size)} · ${dev.path}` }));
}

function buildForm(devices) {
    const pick = $("device-select");
    pick.textContent = "";
    devices.forEach((d) => pick.appendChild(el("option", { value: d.path, text: `${d.model || "Carte"} — ${formatBytes(d.size)} (${d.path})` })));
    $("device-pick").hidden = devices.length < 2;

    const hosts = $("host-select");
    hosts.textContent = "";
    state.status.machines.forEach((m) => hosts.appendChild(el("option", { value: m.hostname, text: `${m.hostname} — ${m.ip}` })));
    hosts.appendChild(el("option", { value: NEW_MACHINE, text: "Nouvelle machine…" }));
    $("host-new").hidden = hosts.value !== NEW_MACHINE;
    $("confirm").value = "";
    renderDeviceInfo();
    onHostChange();
}

function renderImages() {
    const images = state.status.images;
    const pick = $("image-select");
    const previous = pick.value;
    pick.textContent = "";
    images.forEach((i) => pick.appendChild(el("option", { value: i.name, text: i.name })));
    if (previous && images.some((i) => i.name === previous)) pick.value = previous;
    $("image-pick").hidden = images.length < 2;

    const body = $("images-body");
    body.textContent = "";
    if (!images.length) {
        body.appendChild(el("p", { class: "warn", text: "Aucune image. Copie l'image master dans server/stacks/sd-flasher/images/ sur le serveur (.img, .img.gz, .img.xz ou .img.zst)." }));
        $("image-info").textContent = "";
        return;
    }
    const table = el("table");
    table.appendChild(el("thead", {}, [el("tr", {}, ["Image", "Taille", "Date"].map((t) => el("th", { text: t })))]));
    const tbody = el("tbody");
    images.forEach((i) => tbody.appendChild(el("tr", {}, [
        el("td", { text: i.name }), el("td", { text: formatBytes(i.size) }), el("td", { text: formatTime(i.mtime) }),
    ])));
    table.appendChild(tbody);
    body.appendChild(table);
    const current = images.find((i) => i.name === pick.value) || images[0];
    $("image-info").textContent = `Image : ${current.name} (${formatBytes(current.size)}${images.length > 1 ? "" : ", la plus récente"}).`;
}

async function onHostChange() {
    const hosts = $("host-select");
    const isNew = hosts.value === NEW_MACHINE;
    $("host-new").hidden = !isNew;
    const hint = $("host-hint");
    const machine = state.status.machines.find((m) => m.hostname === hosts.value);
    hint.textContent = isNew
        ? "Aucune adresse réservée à ce nom : crée la réservation dans « DHCP et DNS »."
        : machine ? `Adresse réservée : ${machine.ip}` : "";
    await refreshSummary();
    updateFlashButton();
}

async function refreshSummary() {
    const hostname = selectedHostname();
    const box = $("summary");
    if (!hostname) { box.hidden = true; state.previewFor = null; return; }
    try {
        const data = await api("/api/preview?hostname=" + encodeURIComponent(hostname));
        const list = $("summary-list");
        list.textContent = "";
        list.appendChild(el("li", { text: `Image master vers ${selectedDevice().path}, puis vérification` }));
        list.appendChild(el("li", { text: `Hostname : ${data.hostname}` }));
        list.appendChild(el("li", { text: data.wifi ? `Wi-Fi : ${data.wifi}` : "Pas de Wi-Fi" }));
        list.appendChild(el("li", { text: `${data.keys} clé(s) SSH autorisée(s)` }));
        list.appendChild(el("li", { text: "Identité unique : machine-id vidé, clés SSH d'hôte supprimées" }));
        list.appendChild(el("li", { text: "Éjection de la carte à la fin" }));
        box.hidden = false;
        state.previewFor = hostname;
        $("flash-status").hidden = true;
    } catch (err) {
        box.hidden = true;
        state.previewFor = null;
        if (hostname) showStatus($("flash-status"), err.message, "error");
    }
}

function updateFlashButton() {
    const hostname = selectedHostname();
    $("flash-btn").disabled = !(hostname && state.previewFor === hostname && $("confirm").value === hostname);
}

async function startFlash() {
    const hostname = selectedHostname();
    $("flash-btn").disabled = true;
    try {
        await api("/api/flash", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                device: selectedDevice().path, hostname, confirm: $("confirm").value,
                image: $("image-pick").hidden ? null : $("image-select").value,
            }),
        });
        state.formDevice = null;
        refresh();
    } catch (err) {
        showStatus($("flash-status"), err.message, "error");
        updateFlashButton();
    }
}

function renderJob(job) {
    $("job-title").textContent = `${job.hostname} — ${job.model || job.device}`;
    const order = job.steps.map((s) => s.id);
    const current = order.indexOf(job.step);
    const list = $("job-steps");
    list.textContent = "";
    job.steps.forEach((step, i) => {
        let cls = "pending";
        if (job.state === "done" || i < current) cls = "done";
        else if (i === current) cls = job.state === "error" ? "failed" : "active";
        list.appendChild(el("li", { class: cls, text: step.label }));
    });
    const bar = $("job-bar");
    bar.style.width = (job.percent || 0) + "%";
    bar.className = job.state === "error" ? "full" : "";
    $("job-message").textContent = job.state === "running" ? (job.message || "") : "";
    $("job-done").hidden = job.state !== "done";
    $("job-error").hidden = job.state !== "error";
    if (job.state === "done") {
        const where = job.ip ? ` — ${job.hostname} / ${job.ip}` : ` — ${job.hostname}`;
        $("done-text").textContent = `Carte prête. Tu peux la retirer${where}.` + (job.result && job.result.dry_run ? " (simulation)" : "");
    }
    if (job.state === "error") $("error-text").textContent = job.error;
}

function renderCard() {
    const { devices, job } = state.status;
    const busy = job.state && job.state !== "idle";
    $("view-job").hidden = !busy;
    $("view-waiting").hidden = busy || devices.eligible.length > 0;
    $("view-ready").hidden = busy || devices.eligible.length === 0;
    if (busy) { renderJob(job); return; }

    if (devices.eligible.length === 0) {
        state.formDevice = null;
        const ignored = devices.ignored || [];
        $("ignored-hint").textContent = devices.error
            ? devices.error
            : ignored.length ? `${ignored.length} disque(s) ignoré(s) : ${ignored.map((d) => `${d.path} (${d.reason})`).join(", ")}.` : "";
        return;
    }
    const key = devices.eligible.map((d) => d.path + d.size).join(",");
    if (state.formDevice !== key) {   // ne reconstruit le formulaire que si la carte change : on ne perd pas la saisie
        state.formDevice = key;
        buildForm(devices.eligible);
    }
}

// --- reglages et historique -----------------------------------------------------------------

async function loadSettings() {
    const s = await api("/api/settings");
    $("wifi-ssid").value = s.wifi_ssid;
    $("wifi-country").value = s.wifi_country;
    $("wifi-psk").value = "";
    $("wifi-psk").placeholder = s.has_wifi_psk ? "(inchangé)" : "";
    $("extra-keys").value = s.extra_keys.join("\n");
    $("use-automation").checked = s.use_automation_key;
    $("use-automation").disabled = !s.automation_key_available;
    $("automation-hint").textContent = s.automation_key_available ? "" : "Clé d'automatisation introuvable sur le serveur.";
}

async function saveSettings(extra = {}) {
    const status = $("settings-status");
    try {
        await api("/api/settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                wifi_ssid: $("wifi-ssid").value, wifi_psk: $("wifi-psk").value, wifi_country: $("wifi-country").value,
                use_automation_key: $("use-automation").checked,
                extra_keys: $("extra-keys").value.split("\n"), ...extra,
            }),
        });
        showStatus(status, "Réglages enregistrés.", "ok");
        await loadSettings();
        refreshSummary();
    } catch (err) {
        showStatus(status, err.message, "error");
    }
}

const EVENT_LABELS = { "flash-start": "Flash démarré", "flash-done": "Flash terminé", "flash-error": "Échec", settings: "Réglages modifiés" };

async function loadAudit() {
    const rows = await api("/api/audit");
    const body = $("audit-body");
    body.textContent = "";
    if (!rows.length) {
        body.appendChild(el("tr", {}, [el("td", { colspan: "3", class: "empty", text: "Aucun flash pour l'instant." })]));
        return;
    }
    rows.forEach((r) => {
        const detail = r.hostname ? `${r.hostname}${r.model ? " · " + r.model : ""}${r.error ? " · " + r.error : ""}` : (r.wifi_ssid || "");
        body.appendChild(el("tr", {}, [el("td", { text: formatTime(r.time) }), el("td", { text: EVENT_LABELS[r.event] || r.event }), el("td", { text: detail })]));
    });
}

// --- boucle ------------------------------------------------------------------------------------

let timer = null;

async function refresh() {
    clearTimeout(timer);
    let running = false;
    try {
        state.status = await api("/api/status");
        $("dry-banner").hidden = !state.status.dry_run;
        renderImages();
        renderCard();
        running = state.status.job.state === "running";
        if (!running) loadAudit();
    } catch (err) {
        $("ignored-hint").textContent = err.message;
    }
    timer = setTimeout(refresh, running ? 1000 : 3000);
}

$("device-select").addEventListener("change", () => { renderDeviceInfo(); refreshSummary().then(updateFlashButton); });
$("host-select").addEventListener("change", onHostChange);
$("host-new").addEventListener("input", async () => { await refreshSummary(); updateFlashButton(); });
$("confirm").addEventListener("input", updateFlashButton);
$("flash-btn").addEventListener("click", startFlash);
$("settings-save").addEventListener("click", () => saveSettings());
$("wifi-clear").addEventListener("click", () => saveSettings({ clear_wifi: true }));
for (const id of ["dismiss-btn", "dismiss-error-btn"]) {
    $(id).addEventListener("click", async () => { await api("/api/job/dismiss", { method: "POST" }); state.formDevice = null; refresh(); });
}

loadSettings();
refresh();

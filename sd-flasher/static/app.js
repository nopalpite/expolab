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
    $("images-free").textContent = `Espace libre sur le serveur : ${formatBytes(state.status.images_free)}.`;
    if (!images.length) {
        body.appendChild(el("p", { class: "warn", text: "Aucune image : envoie-en une ci-dessous." }));
        $("image-info").textContent = "";
        return;
    }
    const table = el("table");
    table.appendChild(el("thead", {}, [el("tr", {}, ["Image", "Taille", "Date", "SHA-256", ""].map((t) => el("th", { text: t })))]));
    const tbody = el("tbody");
    images.forEach((i) => {
        const del = el("button", { type: "button", class: "secondary small", text: "Supprimer" });
        del.addEventListener("click", () => deleteImage(i.name));
        // Lien direct : le navigateur gere le telechargement (et sa reprise) lui-meme.
        const get = el("a", { class: "button secondary small", href: "/api/images/" + encodeURIComponent(i.name) + "/file", download: i.name, text: "Télécharger" });
        const sha = el("td", { class: "mono", text: i.sha256 ? i.sha256.slice(0, 12) + "…" : "—" });
        if (i.sha256) sha.setAttribute("title", i.sha256);
        tbody.appendChild(el("tr", {}, [
            el("td", { text: i.name }), el("td", { text: formatBytes(i.size) }), el("td", { text: formatTime(i.mtime) }), sha,
            el("td", { class: "row-actions" }, [get, del]),
        ]));
    });
    table.appendChild(tbody);
    body.appendChild(table);
    const current = images.find((i) => i.name === pick.value) || images[0];
    $("image-info").textContent = `Image : ${current.name} (${formatBytes(current.size)}${images.length > 1 ? "" : ", la plus récente"}).`;
}

// --- envoi et suppression d'images ----------------------------------------------------------

const UPLOAD_SUFFIXES = [".img.gz", ".img.xz", ".img.zst", ".img"];
const MAGIC = { ".gz": [0x1f, 0x8b], ".xz": [0xfd, 0x37, 0x7a, 0x58, 0x5a, 0x00], ".zst": [0x28, 0xb5, 0x2f, 0xfd] };
let uploadRequest = null;

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._+=@,-]{0,120}$/;

// Nom propose pour le serveur : sans accent, caracteres non autorises remplaces par "_".
function sanitizeName(name) {
    return name.normalize("NFD").replace(/[̀-ͯ]/g, "")
        .replace(/[^A-Za-z0-9._+=@,-]/g, "_").replace(/^[^A-Za-z0-9]+/, "");
}

// Meme controle que le serveur, fait avant d'envoyer des Go pour rien.
async function checkImageFile(file, name) {
    if (!UPLOAD_SUFFIXES.some((s) => name.endsWith(s))) return "Type non reconnu : .img, .img.gz, .img.xz ou .img.zst.";
    if (!NAME_RE.test(name) || name.includes("..")) return "Nom invalide : lettres, chiffres et . _ - + = @ , uniquement, sans espace ni accent.";
    if (state.status.images.some((i) => i.name === name)) return "Une image de ce nom existe déjà : supprime-la d'abord.";
    const head = new Uint8Array(await file.slice(0, 512).arrayBuffer());
    const ext = Object.keys(MAGIC).find((e) => name.endsWith(e));
    if (ext) {
        if (!MAGIC[ext].every((b, i) => head[i] === b)) return `Ce fichier n'est pas une archive ${ext} valide.`;
    } else if (!(head.length >= 512 && head[510] === 0x55 && head[511] === 0xaa)) {
        return "Ce fichier ne ressemble pas à une image de carte SD (pas de table de partitions).";
    }
    if (file.size + 1024 ** 3 > state.status.images_free) return `Espace insuffisant : ${formatBytes(file.size)} à envoyer, ${formatBytes(Math.max(0, state.status.images_free - 1024 ** 3))} disponibles.`;
    return null;
}

async function validateUpload() {
    const file = $("upload-file").files[0];
    const status = $("upload-status");
    const name = $("upload-name").value.trim();
    $("upload-btn").disabled = true;
    if (!file) { status.hidden = true; return; }
    const problem = await checkImageFile(file, name);
    if (problem) { showStatus(status, problem, "error"); return; }
    const renamed = name !== file.name ? ` (enregistrée sous le nom ${name})` : "";
    showStatus(status, `${file.name} : ${formatBytes(file.size)}, prête à envoyer${renamed}.`, "ok");
    $("upload-btn").disabled = false;
}

async function onUploadFileChange() {
    const file = $("upload-file").files[0];
    $("upload-name").value = file ? sanitizeName(file.name) : "";   // modifiable avant l'envoi
    await validateUpload();
}

function setUploading(on) {
    $("upload-btn").disabled = on;
    $("upload-file").disabled = on;
    $("upload-sha").disabled = on;
    $("upload-name").disabled = on;
    $("upload-cancel").hidden = !on;
    $("upload-bar-wrap").hidden = !on;
    window.onbeforeunload = on ? () => "Un envoi d'image est en cours." : null;
}

function startUpload() {
    const file = $("upload-file").files[0];
    if (!file) return;
    const sha = $("upload-sha").value.trim();
    const url = "/api/images/" + encodeURIComponent($("upload-name").value.trim()) + (sha ?"?sha256=" + encodeURIComponent(sha) : "");
    const status = $("upload-status");
    const started = Date.now();
    const xhr = new XMLHttpRequest();
    uploadRequest = xhr;
    setUploading(true);
    $("upload-bar").style.width = "0%";
    xhr.open("PUT", url);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = (e.loaded / e.total) * 100;
        const speed = e.loaded / Math.max(1, (Date.now() - started) / 1000);
        const eta = speed ? Math.round((e.total - e.loaded) / speed) : 0;
        $("upload-bar").style.width = pct.toFixed(1) + "%";
        showStatus(status, `Envoi : ${pct.toFixed(0)} % (${formatBytes(e.loaded)} / ${formatBytes(e.total)}, ${formatBytes(speed)}/s, reste ~${Math.floor(eta / 60)} min ${eta % 60} s)`, "ok");
    };
    const finish = (message, ok) => {
        uploadRequest = null;
        setUploading(false);
        showStatus(status, message, ok ? "ok" : "error");
        if (ok) { $("upload-file").value = ""; $("upload-name").value = ""; $("upload-sha").value = ""; $("upload-btn").disabled = true; }
        refresh();
    };
    xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (e) { /* corps vide */ }
        if (xhr.status === 201) finish(`${data.name} envoyée (${formatBytes(data.size)}, SHA-256 ${data.sha256.slice(0, 12)}…).`, true);
        else finish(data.error || `Erreur ${xhr.status}`, false);
    };
    xhr.onerror = () => finish("Envoi interrompu : connexion perdue.", false);
    xhr.onabort = () => finish("Envoi annulé.", false);
    xhr.send(file);
}

async function deleteImage(name) {
    if (!confirm(`Supprimer l'image ${name} ?`)) return;
    try {
        await api("/api/images/" + encodeURIComponent(name), { method: "DELETE" });
    } catch (err) {
        showStatus($("upload-status"), err.message, "error");
    }
    refresh();
}

// --- import depuis un lien ---------------------------------------------------------------------

let fetchNameAuto = true;   // le nom suit le lien tant qu'il n'a pas ete modifie a la main

function fetchNameFromUrl(url) {
    try {
        const last = decodeURIComponent(new URL(url).pathname.split("/").pop() || "");
        return sanitizeName(last);
    } catch (e) {
        return "";
    }
}

function validateFetch() {
    const url = $("fetch-url").value.trim();
    const name = $("fetch-name").value.trim();
    const running = state.status && state.status.fetch.state === "running";
    let problem = null;
    if (!url) problem = "";
    else if (!/^https?:\/\/[^/]+/i.test(url)) problem = "Lien invalide : une adresse http:// ou https:// est attendue.";
    else if (!UPLOAD_SUFFIXES.some((s) => name.endsWith(s))) problem = "Le nom doit finir par .img, .img.gz, .img.xz ou .img.zst.";
    else if (!NAME_RE.test(name) || name.includes("..")) problem = "Nom invalide : lettres, chiffres et . _ - + = @ , uniquement, sans espace ni accent.";
    else if (state.status && state.status.images.some((i) => i.name === name)) problem = "Une image de ce nom existe déjà : supprime-la d'abord.";
    $("fetch-btn").disabled = running || problem !== null;
    if (!running && problem) showStatus($("fetch-status"), problem, "error");
    else if (!running && problem === "" && state.status && state.status.fetch.state === "idle") $("fetch-status").hidden = true;
    return problem === null;
}

async function startFetch() {
    if (!validateFetch()) return;
    try {
        await api("/api/images/fetch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: $("fetch-url").value.trim(), name: $("fetch-name").value.trim(), sha256: $("fetch-sha").value.trim() }),
        });
    } catch (err) {
        showStatus($("fetch-status"), err.message, "error");
    }
    refresh();
}

function renderFetch() {
    const f = state.status.fetch;
    const running = f.state === "running";
    $("fetch-cancel").hidden = !running;
    $("fetch-bar-wrap").hidden = !running;
    ["fetch-url", "fetch-name", "fetch-sha"].forEach((id) => { $(id).disabled = running; });
    const status = $("fetch-status");
    if (running) {
        const pct = f.total ? (f.received / f.total) * 100 : 0;
        $("fetch-bar").style.width = (f.total ? pct.toFixed(1) : 100) + "%";
        const speed = f.received / Math.max(1, Date.now() / 1000 - f.started);
        const of = f.total ? ` / ${formatBytes(f.total)} (${pct.toFixed(0)} %)` : "";
        showStatus(status, `Téléchargement de ${f.name} : ${formatBytes(f.received)}${of}, ${formatBytes(speed)}/s`, "ok");
    } else if (f.state === "done") {
        showStatus(status, `${f.name} téléchargée (${formatBytes(f.received)}, SHA-256 ${f.sha256.slice(0, 12)}…).`, "ok");
    } else if (f.state === "error") {
        showStatus(status, f.error, "error");
    }
    validateFetch();
}

// --- capture d'une carte ---------------------------------------------------------------------

function renderCapture() {
    const { devices, job } = state.status;
    const busy = job.state === "running";
    const eligible = devices.eligible;
    $("capture-waiting").hidden = eligible.length > 0;
    $("capture-form").hidden = eligible.length === 0;
    const pick = $("capture-device");
    const key = eligible.map((d) => d.path + d.size).join(",");
    if (pick.dataset.key !== key) {
        const previous = pick.value;
        pick.textContent = "";
        eligible.forEach((d) => pick.appendChild(el("option", { value: d.path, text: `${d.model || "Carte"} — ${formatBytes(d.size)} (${d.path})` })));
        if (eligible.some((d) => d.path === previous)) pick.value = previous;
        pick.dataset.key = key;
    }
    $("capture-device-pick").hidden = eligible.length < 2;
    const name = $("capture-name").value.trim();
    $("capture-btn").disabled = busy || !eligible.length || !name;
    $("capture-btn").textContent = busy ? "Une opération est en cours…" : "Capturer la carte";
}

async function startCapture() {
    const dev = state.status.devices.eligible.find((d) => d.path === $("capture-device").value) || state.status.devices.eligible[0];
    if (!dev) return;
    if (!confirm(`Capturer ${dev.model || "la carte"} (${formatBytes(dev.size)}, ${dev.path}) ?\n\nLa carte est seulement lue. Selon la quantité de données, cela peut prendre de quelques minutes à plus d'une demi-heure.`)) return;
    $("capture-btn").disabled = true;
    try {
        await api("/api/capture", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ device: dev.path, name: $("capture-name").value.trim(), compression: $("capture-compression").value,
                                   prepare: $("capture-prepare").checked }),
        });
        $("capture-status").hidden = true;
        $("card-panel").scrollIntoView({ behavior: "smooth" });
    } catch (err) {
        showStatus($("capture-status"), err.message, "error");
    }
    refresh();
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
        list.appendChild(el("li", { text: data.wifi ? `Wi-Fi : ${data.wifi} (un redémarrage automatique au premier démarrage l'active)` : "Pas de Wi-Fi" }));
        let account = "Aucun compte défini";
        if (data.user) account = `Compte : ${data.user} (${data.password ? "mot de passe" : "sans mot de passe"}${data.sudo ? ", sudo sans mot de passe" : ""})`;
        list.appendChild(el("li", { text: account }));
        list.appendChild(el("li", { text: `${data.keys} clé(s) SSH autorisée(s)` }));
        data.warnings.forEach((w) => list.appendChild(el("li", { class: "warn", text: "Attention : " + w })));
        list.appendChild(el("li", { text: "Identité unique : machine-id vidé, clés SSH d'hôte supprimées" }));
        list.appendChild(el("li", { text: data.time_server ? `Serveur de temps : ${data.time_server}` : "Pas de serveur de temps du lab" }));
        list.appendChild(el("li", { text: data.apt_proxy ? `Proxy apt : ${data.apt_proxy}` : "Pas de proxy apt (apt direct sur internet)" }));
        list.appendChild(el("li", { text: data.keyboard_layout ? `Clavier : ${data.keyboard_layout}` : "Clavier de l'image (qwerty)" }));
        list.appendChild(el("li", { text: data.locale ? `Langue : ${data.locale}` : "Langue de l'image (anglais)" }));
        list.appendChild(el("li", { text: data.timezone ? `Fuseau horaire : ${data.timezone}` : "Fuseau de l'image (UTC)" }));
        if (data.packages.length) list.appendChild(el("li", { text: `Paquets installés au démarrage (cache apt, réessai jusqu'à réussite) : ${data.packages.join(", ")}` }));
        list.appendChild(el("li", { text: "Mises à jour automatiques désactivées (elles bloquent apt au premier démarrage)" }));
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
    const capturing = job.kind === "capture";
    $("job-title").textContent = capturing ? `Capture de ${job.model || job.device} → ${job.image}` : `${job.hostname} — ${job.model || job.device}`;
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
    $("dismiss-btn").textContent = capturing ? "Fermer" : "Carte suivante";
    if (job.state === "done" && capturing) {
        const r = job.result || {};
        const kind = r.prepared ? "préparée en master" : "copie brute";
        $("done-text").textContent = `Image ${job.image} prête (${kind}) : ${formatBytes(r.bytes)} (${formatBytes(r.used_bytes)} de données, image décompressée ${formatBytes(r.image_bytes)}). Tu peux retirer la carte.`;
    } else if (job.state === "done") {
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
    $("time-server").value = s.time_server;
    $("apt-proxy").value = s.apt_proxy;
    $("keyboard-layout").value = s.keyboard_layout;
    $("locale").value = s.locale;
    $("timezone").value = s.timezone;
    $("packages").value = s.packages.join("\n");
    $("user-name").value = s.user_name;
    $("user-password").value = "";
    $("user-password").placeholder = s.has_user_password ? "(inchangé)" : "";
    $("user-sudo").checked = s.user_sudo;
    $("user-pwauth").checked = s.user_pwauth;
    $("user-password-clear").hidden = !s.has_user_password;
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
                time_server: $("time-server").value, apt_proxy: $("apt-proxy").value,
                keyboard_layout: $("keyboard-layout").value, locale: $("locale").value, timezone: $("timezone").value,
                packages: $("packages").value,
                user_name: $("user-name").value, user_password: $("user-password").value,
                user_sudo: $("user-sudo").checked, user_pwauth: $("user-pwauth").checked,
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

const EVENT_LABELS = {
    "flash-start": "Flash démarré", "flash-done": "Flash terminé", "flash-error": "Échec", settings: "Réglages modifiés",
    "image-upload": "Image envoyée", "image-delete": "Image supprimée", "image-fetch": "Image téléchargée depuis un lien",
    "image-download": "Image récupérée", "capture-start": "Capture démarrée", "capture-done": "Capture terminée",
    "capture-error": "Échec de capture",
};

async function loadAudit() {
    const rows = await api("/api/audit");
    const body = $("audit-body");
    body.textContent = "";
    if (!rows.length) {
        body.appendChild(el("tr", {}, [el("td", { colspan: "3", class: "empty", text: "Aucun flash pour l'instant." })]));
        return;
    }
    rows.forEach((r) => {
        const detail = r.hostname ? `${r.hostname}${r.model ? " · " + r.model : ""}${r.error ? " · " + r.error : ""}` : (r.image || r.wifi_ssid || "");
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
        renderCapture();
        renderFetch();
        running = state.status.job.state === "running" || state.status.fetch.state === "running";
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
$("upload-file").addEventListener("change", onUploadFileChange);
$("upload-name").addEventListener("input", validateUpload);
$("upload-btn").addEventListener("click", startUpload);
$("upload-cancel").addEventListener("click", () => uploadRequest && uploadRequest.abort());
$("flash-btn").addEventListener("click", startFlash);
$("capture-btn").addEventListener("click", startCapture);
$("capture-name").addEventListener("input", () => state.status && renderCapture());
$("fetch-url").addEventListener("input", () => {
    if (fetchNameAuto) $("fetch-name").value = fetchNameFromUrl($("fetch-url").value.trim());
    validateFetch();
});
$("fetch-name").addEventListener("input", () => { fetchNameAuto = $("fetch-name").value === ""; validateFetch(); });
$("fetch-btn").addEventListener("click", startFetch);
$("fetch-cancel").addEventListener("click", async () => { await api("/api/images/fetch", { method: "DELETE" }); refresh(); });
$("settings-save").addEventListener("click", () => saveSettings());
$("wifi-clear").addEventListener("click", () => saveSettings({ clear_wifi: true }));
$("user-password-clear").addEventListener("click", () => saveSettings({ clear_user_password: true }));
for (const id of ["dismiss-btn", "dismiss-error-btn"]) {
    $(id).addEventListener("click", async () => { await api("/api/job/dismiss", { method: "POST" }); state.formDevice = null; refresh(); });
}

loadSettings();
refresh();

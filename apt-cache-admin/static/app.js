"use strict";

const $ = (id) => document.getElementById(id);

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
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return `${value >= 100 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

function formatDate(iso) {
    return iso ? iso.replace("T", " ") : "—";
}

function showStatus(node, message, ok) {
    node.hidden = false;
    node.className = "status " + (ok ? "status-ok" : "status-error");
    node.textContent = message;
}

async function api(url, options) {
    const res = await fetch(url, options);
    let data = {};
    try { data = await res.json(); } catch (e) { /* corps vide */ }
    if (!res.ok) throw new Error(data.error || `Erreur ${res.status}`);
    return data;
}

// --- etat et efficacite ------------------------------------------------------

let currentMode = "online";

function renderStatus(data) {
    currentMode = data.mode;
    const mode = $("mode-value");
    mode.textContent = "";
    mode.appendChild(el("span", { "class": "badge " + data.mode, text: data.mode === "online" ? "En ligne" : "Hors ligne" }));
    $("mode-hint").textContent = data.mode === "online"
        ? "Télécharge ce qui manque sur internet."
        : "Ne sert que ce qu'il a déjà : ne contacte jamais internet.";
    $("mode-btn").textContent = data.mode === "online" ? "Passer hors ligne" : "Passer en ligne";

    const running = $("running-value");
    running.textContent = "";
    const up = data.container.running;
    running.appendChild(el("span", { "class": "badge " + (up ? "up" : "down"), text: up === null ? "Inconnu" : up ? "En marche" : "Arrêté" }));

    const { total, used, free } = data.disk;
    const percent = total ? Math.round((used / total) * 100) : 0;
    $("disk-value").textContent = `${percent} % utilisé`;
    const bar = $("disk-bar");
    bar.style.width = percent + "%";
    bar.className = percent >= 90 ? "full" : percent >= 75 ? "warn" : "";
    $("disk-hint").textContent = `Cache : ${formatBytes(data.cache.bytes)} · libre : ${formatBytes(free)} sur ${formatBytes(total)}`;

    $("packages-value").textContent = String(data.cache.packages);
    $("packages-hint").textContent = `${formatBytes(data.cache.bytes)} au total`;

    const eff = data.efficiency;
    $("ratio-value").textContent = eff.hit_ratio === null ? "—" : Math.round(eff.hit_ratio * 100) + " %";
    $("ratio-hint").textContent = `${eff.hits} paquet(s) sur ${eff.requests} demandé(s) par les machines`;
    $("downloads-value").textContent = String(eff.downloads);
    $("downloads-hint").textContent = `${formatBytes(eff.downloaded_bytes)} récupérés pour le cache`;
    $("errors-value").textContent = String(eff.errors);
    $("errors-value").style.color = eff.errors ? "var(--error)" : "";
    $("errors-hint").textContent = eff.errors
        ? "paquet(s) demandé(s) mais indisponible(s) : cache hors ligne ou fichier introuvable"
        : "aucun paquet indisponible";
    $("saved-value").textContent = formatBytes(eff.saved_bytes);
    $("period-hint").textContent = eff.since ? `Du ${formatDate(eff.since)} au ${formatDate(eff.until)}` : "Journal vide";

    const body = $("clients-body");
    body.textContent = "";
    if (!data.clients.length) {
        body.appendChild(el("tr", {}, [el("td", { colspan: "6", "class": "empty", text: "Aucune requête de paquet dans le journal." })]));
    }
    data.clients.forEach((c) => {
        body.appendChild(el("tr", {}, [
            el("td", { text: c.name || "(nom inconnu)" }),
            el("td", { text: c.client }),
            el("td", { "class": "num", text: String(c.requests) }),
            el("td", { "class": "num" + (c.errors ? " ev-error" : ""), text: String(c.errors) }),
            el("td", { "class": "num", text: formatBytes(c.bytes) }),
            el("td", { text: formatDate(c.last) }),
        ]));
    });
}

async function loadStatus() {
    try {
        renderStatus(await api("/api/status"));
    } catch (e) {
        showStatus($("action-status"), e.message, false);
    }
}

async function doAction(label, url, options) {
    const status = $("action-status");
    const buttons = ["mode-btn", "restart-btn"].map($);
    buttons.forEach((b) => { b.disabled = true; });
    showStatus(status, `${label}…`, true);
    try {
        await api(url, options);
        showStatus(status, `${label} : terminé.`, true);
    } catch (e) {
        showStatus(status, e.message, false);
    } finally {
        buttons.forEach((b) => { b.disabled = false; });
        loadStatus();
    }
}

$("mode-btn").addEventListener("click", () => {
    const next = currentMode === "online" ? "offline" : "online";
    const question = next === "offline"
        ? "Passer le cache HORS LIGNE ? Il ne servira plus que ce qu'il a déjà en cache, et les paquets manquants échoueront sur les machines."
        : "Repasser le cache EN LIGNE ?";
    if (!confirm(question + "\nLe cache redémarre (quelques secondes).")) return;
    doAction("Changement de mode", "/api/mode", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: next }),
    });
});

$("restart-btn").addEventListener("click", () => {
    if (!confirm("Redémarrer le cache ? Les téléchargements en cours sur les machines seront interrompus.")) return;
    doAction("Redémarrage", "/api/restart", { method: "POST" });
});

// --- miroirs HTTPS --------------------------------------------------------------

let mirrors = [];

function renderMirrors() {
    const list = $("mirrors-list");
    list.textContent = "";
    if (!mirrors.length) {
        list.appendChild(el("li", { "class": "empty", text: "Aucun miroir : seuls les dépôts en HTTP (Debian, Raspberry Pi OS) passent." }));
    }
    mirrors.forEach((url, index) => {
        const remove = el("button", { type: "button", "class": "secondary", "aria-label": "Retirer " + url, text: "Retirer" });
        remove.addEventListener("click", () => { mirrors.splice(index, 1); renderMirrors(); });
        list.appendChild(el("li", {}, [el("span", { text: url }), remove]));
    });
}

async function loadMirrors() {
    try {
        mirrors = (await api("/api/mirrors")).mirrors;
        renderMirrors();
    } catch (e) {
        showStatus($("mirrors-status"), e.message, false);
    }
}

$("mirror-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("mirror-url");
    const url = input.value.trim().replace(/\/+$/, "");
    if (!url) return;
    if (mirrors.includes(url)) return showStatus($("mirrors-status"), "Ce miroir est déjà dans la liste.", false);
    mirrors.push(url);
    input.value = "";
    $("mirrors-status").hidden = true;
    renderMirrors();
});

async function saveMirrors(apply) {
    const status = $("mirrors-status");
    const buttons = ["mirrors-save", "mirrors-apply"].map($);
    buttons.forEach((b) => { b.disabled = true; });
    showStatus(status, apply ? "Enregistrement et redémarrage du cache…" : "Enregistrement…", true);
    try {
        await api("/api/mirrors", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mirrors, apply }),
        });
        showStatus(status, apply ? "Miroirs enregistrés et cache redémarré." : "Miroirs enregistrés. Appliquer pour redémarrer le cache et les prendre en compte.", true);
        loadStatus();
    } catch (e) {
        showStatus(status, e.message, false);
    } finally {
        buttons.forEach((b) => { b.disabled = false; });
    }
}

$("mirrors-save").addEventListener("click", () => saveMirrors(false));
$("mirrors-apply").addEventListener("click", () => saveMirrors(true));

// --- journal ----------------------------------------------------------------------

// Une machine ne parle QU'AU cache : "telecharge" designe toujours le CACHE qui va
// chercher le fichier sur internet, jamais la machine.
const EVENT_LABELS = {
    hit: "Servi depuis le cache",
    miss: "Cache : téléchargé sur internet, puis servi",
    fetch: "Cache : récupéré sur internet (sans machine en attente)",
    error: "Échec : le cache n'a pas pu fournir le fichier (hors ligne ou introuvable)",
};

async function loadLog() {
    const params = new URLSearchParams({ limit: "100" });
    if ($("log-client").value.trim()) params.set("client", $("log-client").value.trim());
    if ($("log-q").value.trim()) params.set("q", $("log-q").value.trim());
    if ($("log-all").checked) params.set("all", "1");
    try {
        const data = await api("/api/log?" + params.toString());
        const body = $("log-body");
        body.textContent = "";
        if (!data.entries.length) {
            body.appendChild(el("tr", {}, [el("td", { colspan: "5", "class": "empty", text: "Aucune entrée." })]));
        }
        data.entries.forEach((e) => {
            body.appendChild(el("tr", {}, [
                el("td", { text: formatDate(e.time) }),
                el("td", { "class": "ev-" + e.kind, text: EVENT_LABELS[e.kind] || e.kind }),
                el("td", { text: e.name ? `${e.name} (${e.client})` : e.client }),
                el("td", { "class": "num", text: formatBytes(e.bytes) }),
                el("td", { "class": "path", text: e.path }),
            ]));
        });
    } catch (err) {
        /* on garde l'affichage precedent */
    }
}

["log-client", "log-q"].forEach((id) => $(id).addEventListener("input", loadLog));
$("log-all").addEventListener("change", loadLog);
$("log-form").addEventListener("submit", (e) => { e.preventDefault(); loadLog(); });

loadStatus();
loadMirrors();
loadLog();
setInterval(() => { if ($("log-auto").checked) { loadLog(); loadStatus(); } }, 10000);

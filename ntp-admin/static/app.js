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

function formatAgo(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    if (seconds < 5) return "à l'instant";
    if (seconds < 90) return `il y a ${Math.round(seconds)} s`;
    if (seconds < 5400) return `il y a ${Math.round(seconds / 60)} min`;
    if (seconds < 172800) return `il y a ${Math.round(seconds / 3600)} h`;
    return `il y a ${Math.round(seconds / 86400)} j`;
}

function formatMs(ms) {
    if (ms === null || ms === undefined) return "—";
    if (ms < 0.01) return "< 0,01 ms";
    if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
    return `${ms < 10 ? ms.toFixed(2) : ms.toFixed(0)} ms`;
}

const state = { status: null, fetchedAt: 0, formLoaded: false };

// --- etat -------------------------------------------------------------------------------

function badge(label, kind) {
    return el("span", { class: "badge " + kind, text: label });
}

function renderSync(data) {
    const value = $("sync-value");
    const hint = $("sync-hint");
    value.textContent = "";
    const t = data.tracking;
    if (!t) {
        value.appendChild(badge("Injoignable", "down"));
        hint.textContent = data.container.running === false ? "Le service de temps est arrêté." : (data.error || "chronyd ne répond pas (démarrage en cours ?).");
        return;
    }
    if (t.state === "synced") {
        value.appendChild(badge("Synchronisé", "up"));
        hint.textContent = `Référence : ${t.ref_name || t.ref_id}, strate ${t.stratum}.`;
    } else if (t.state === "local") {
        value.appendChild(badge("Horloge locale", "offline"));
        hint.textContent = data.config.mode === "offline"
            ? "Mode hors ligne : les machines reçoivent l'heure du serveur."
            : "Aucun serveur amont joignable : les machines reçoivent l'heure du serveur, qui peut dériver.";
    } else {
        value.appendChild(badge("Non synchronisé", "down"));
        hint.textContent = "Le serveur n'a encore aucune heure de référence.";
    }
}

function renderOffset(data) {
    const t = data.tracking;
    if (!t) { $("offset-value").textContent = "—"; $("offset-hint").textContent = ""; return; }
    $("offset-value").textContent = t.state === "synced" ? `± ${formatMs(t.offset_ms)}` : "—";
    const last = t.ref_time ? `Dernière mise à l'heure : ${formatAgo(data.now - t.ref_time)}.` : "";
    $("offset-hint").textContent = t.state === "synced" ? `Écart avec la référence. ${last}` : "Pas de référence externe.";
}

function renderClock() {
    const data = state.status;
    if (!data) return;
    const serverNow = data.now + (Date.now() - state.fetchedAt) / 1000;
    const date = new Date(serverNow * 1000);
    $("clock-value").textContent = date.toLocaleTimeString("fr-FR");
    const drift = data.now - state.fetchedAt / 1000;   // serveur - navigateur, a la latence pres
    const abs = Math.abs(drift);
    $("clock-hint").textContent = `${date.toLocaleDateString("fr-FR", { dateStyle: "long" })} · ` +
        (abs < 2 ? "identique à ton navigateur" : `${abs < 90 ? abs.toFixed(0) + " s" : formatAgo(abs).replace("il y a ", "")} ${drift > 0 ? "d'avance" : "de retard"} sur ton navigateur`);
}

function renderRunning(data) {
    const running = $("running-value");
    running.textContent = "";
    const up = data.container.running;
    running.appendChild(badge(up === null ? "Inconnu" : up ? "En marche" : "Arrêté", up ? "up" : "down"));
}

const SOURCE_LABELS = {
    selected: ["Retenue", "up"], combined: ["Utilisée", "up"], excluded: ["Exclue", "offline"],
    unreachable: ["Injoignable", "down"], falseticker: ["Heure fausse", "down"], unstable: ["Instable", "offline"],
};

function renderSources(data) {
    const body = $("sources-body");
    body.textContent = "";
    if (!data.sources.length) {
        const why = data.config.mode === "offline" ? "Mode hors ligne : aucun serveur amont." : (data.tracking ? "Aucun serveur amont pour l'instant." : "Service injoignable.");
        body.appendChild(el("tr", {}, [el("td", { colspan: "5", class: "empty", text: why })]));
        return;
    }
    data.sources.forEach((s) => {
        const [label, kind] = SOURCE_LABELS[s.state] || ["?", "offline"];
        body.appendChild(el("tr", {}, [
            el("td", { text: s.kind === "local" ? `${s.address} (horloge locale)` : s.address }),
            el("td", {}, [badge(label, kind)]),
            el("td", { class: "num", text: String(s.stratum) }),
            el("td", { class: "num", text: s.reach === "0" ? "jamais" : formatAgo(s.last_rx_s) }),   // portee 0 : aucune reponse reçue
            el("td", { class: "num", text: formatMs(Math.abs(s.offset_ms)) }),
        ]));
    });
}

function renderClients(data) {
    const body = $("clients-body");
    body.textContent = "";
    if (!data.clients.length) {
        body.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "empty", text: "Aucune machine n'a encore demandé l'heure." })]));
        return;
    }
    data.clients.forEach((c) => body.appendChild(el("tr", {}, [
        el("td", { text: c.name || "—" }),
        el("td", { class: "mono", text: c.ip }),
        el("td", { class: "num", text: String(c.requests) }),
        el("td", { class: "num", text: formatAgo(c.last_s) }),
    ])));
}

// --- reglages --------------------------------------------------------------------------------

function fillForm(config) {
    $("mode-online").checked = config.mode === "online";
    $("mode-offline").checked = config.mode === "offline";
    $("upstream").value = config.upstream.join("\n");
    $("allow").value = config.allow.join("\n");
    $("fallback").checked = config.local_fallback;
    state.formLoaded = true;
}

function readForm() {
    return {
        mode: $("mode-offline").checked ? "offline" : "online",
        upstream: $("upstream").value.split("\n"),
        allow: $("allow").value.split("\n"),
        local_fallback: $("fallback").checked,
    };
}

async function saveConfig(apply) {
    const status = $("config-status");
    $("save-btn").disabled = $("apply-btn").disabled = true;
    try {
        const config = await api("/api/config", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ...readForm(), apply }),
        });
        fillForm(config);
        showStatus(status, apply ? "Réglages appliqués : le serveur de temps a redémarré." : "Réglages enregistrés (pas encore appliqués : « Enregistrer et appliquer » pour redémarrer).", true);
    } catch (err) {
        showStatus(status, err.message, false);
    } finally {
        $("save-btn").disabled = $("apply-btn").disabled = false;
    }
    setTimeout(refresh, apply ? 2500 : 0);
}

async function restart() {
    const status = $("action-status");
    $("restart-btn").disabled = true;
    try {
        await api("/api/restart", { method: "POST" });
        showStatus(status, "Serveur de temps redémarré.", true);
    } catch (err) {
        showStatus(status, err.message, false);
    } finally {
        $("restart-btn").disabled = false;
    }
    setTimeout(refresh, 2500);
}

async function makestep() {
    if (!confirm("Remettre l'horloge du serveur à l'heure immédiatement ? Elle fera un saut : à n'utiliser que si l'heure est très fausse.")) return;
    const status = $("action-status");
    try {
        await api("/api/makestep", { method: "POST" });
        showStatus(status, "Mise à l'heure demandée.", true);
    } catch (err) {
        showStatus(status, err.message, false);
    }
    setTimeout(refresh, 1500);
}

// --- boucle ---------------------------------------------------------------------------------------

async function refresh() {
    try {
        const data = await api("/api/status");
        state.status = data;
        state.fetchedAt = Date.now();
        renderSync(data);
        renderRunning(data);
        renderOffset(data);
        renderClock();
        renderSources(data);
        renderClients(data);
        if (!state.formLoaded) fillForm(data.config);   // jamais pendant la saisie : on ne perd pas ce qui est tape
    } catch (err) {
        showStatus($("action-status"), err.message, false);
    }
}

$("save-btn").addEventListener("click", () => saveConfig(false));
$("apply-btn").addEventListener("click", () => saveConfig(true));
$("restart-btn").addEventListener("click", restart);
$("makestep-btn").addEventListener("click", makestep);

refresh();
setInterval(refresh, 5000);
setInterval(renderClock, 1000);

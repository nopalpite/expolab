// Prises connectees : etat lu toutes les 3 s (/api/state, deja tenu a jour cote serveur),
// actions par l'API. Les noms viennent de l'utilisateur ou des prises : toujours en TEXTE.
"use strict";

const $ = (id) => document.getElementById(id);
let state = null;

function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v);
    }
    children.flat().forEach((c) => c != null && node.append(c));
    return node;
}

async function api(url, options = {}) {
    const res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
    let data = {};
    try { data = await res.json(); } catch (e) { /* vide */ }
    if (!res.ok) throw new Error(data.error || `Erreur ${res.status}`);
    return data;
}

const fmtW = (w) => (w == null ? "—" : `${Math.round(w)}`);
const fmtWh = (wh) => (wh == null ? "—" : wh >= 1000 ? `${(wh / 1000).toFixed(2).replace(".", ",")} kWh` : `${Math.round(wh)} Wh`);

// --- onglets ------------------------------------------------------------------------------

function showTab() {
    const name = location.hash === "#ajout" ? "ajout" : "tableau";
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.tab === name));
    $("tab-tableau").hidden = name !== "tableau";
    $("tab-ajout").hidden = name !== "ajout";
}
window.addEventListener("hashchange", showTab);

// --- fenetres -----------------------------------------------------------------------------

function openModal(id) { $(id).hidden = false; }
function closeModals() { document.querySelectorAll(".modal").forEach((m) => { m.hidden = true; }); stopScan(); }
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeModals));

function confirmAction(title, text, run) {
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    $("confirm-go").onclick = async () => { closeModals(); try { await run(); } catch (e) { alert(e.message); } refresh(); };
    openModal("confirm-modal");
}

// --- tableau de bord -----------------------------------------------------------------------

function tile(p) {
    const offline = !p.available;
    const switchCls = p.busy ? "switch busy" : p.on ? "switch on" : "switch";
    const sw = el("button", { type: "button", class: switchCls, title: p.on ? "Éteindre" : "Allumer",
                              "aria-label": `${p.on ? "Éteindre" : "Allumer"} ${p.module || p.name}` });
    sw.disabled = offline || !p.can_switch || Boolean(p.busy);
    sw.addEventListener("click", () => (p.on ? askOff(p) : api(`/api/plugs/${p.node_id}/power`, { method: "POST", body: JSON.stringify({ on: true }) })
        .then(refresh).catch((e) => alert(e.message))));
    const max = Math.max(1, ...p.history.filter((v) => v != null));
    const spark = p.measures ? el("div", { class: "spark", title: "Puissance sur 24 h" },
        p.history.map((v) => el("span", { class: v == null ? "none" : "", style: `height:${v == null ? 0 : Math.max(3, (v / max) * 100)}%` }))) : null;
    let line = null;
    if (p.busy) line = el("div", { class: "state-line warn", text: p.busy });
    else if (p.notice) line = el("div", { class: "state-line bad", text: p.notice });
    else if (offline) line = el("div", { class: "state-line bad", text: "Prise injoignable (débranchée ? hors Wi-Fi ?)" });
    return el("div", { class: `tile${offline ? " offline" : ""}` },
        el("div", { class: "top" }, el("span", { class: `dot ${offline ? "err" : p.on ? "ok" : "off"}` }),
            el("div", {}, el("div", { class: "name", text: p.module || p.name }), el("div", { class: "sub2", text: p.module ? p.name : "aucun module associé" })), sw),
        el("div", { class: "watts" }, p.measures ? fmtW(offline ? null : p.power_w) : "—", el("small", { text: p.measures ? " W" : " pas de mesure" })),
        spark, line,
        el("div", { class: "meta" }, el("span", { text: p.measures ? `${fmtWh(p.energy_today_wh)} aujourd'hui` : "" }),
            el("span", { text: p.energy_wh != null ? `total ${fmtWh(p.energy_wh)}` : "" })));
}

function askOff(p) {
    $("off-title").textContent = `Éteindre ${p.module || p.name} ?`;
    $("off-noip").hidden = Boolean(p.module_ip);
    document.querySelector("input[name=off-mode][value=clean]").checked = true;
    $("off-go").onclick = async () => {
        const clean = document.querySelector("input[name=off-mode]:checked").value === "clean";
        closeModals();
        try { await api(`/api/plugs/${p.node_id}/power`, { method: "POST", body: JSON.stringify({ on: false, clean }) }); }
        catch (e) { alert(e.message); }
        refresh();
    };
    openModal("off-modal");
}

function renderDashboard() {
    const s = state.summary;
    $("k-power").textContent = `${fmtW(s.power_w)} W`;
    $("k-energy").textContent = fmtWh(s.energy_today_wh);
    $("k-on").textContent = `${s.on} / ${s.total}`;
    $("k-down").textContent = s.unreachable;
    $("k-down").classList.toggle("bad", s.unreachable > 0);
    $("no-plug").hidden = state.plugs.length > 0;
    $("tiles").replaceChildren(...state.plugs.map(tile));
}

$("all-on").addEventListener("click", () => confirmAction("Tout allumer ?", "Toutes les prises joignables sont allumées : les modules démarrent.",
    () => api("/api/all", { method: "POST", body: JSON.stringify({ action: "on" }) })));
$("all-off").addEventListener("click", () => {
    const n = state ? state.plugs.filter((p) => p.available && p.on).length : 0;
    confirmAction("Tout éteindre proprement ?", `Les ${n} module(s) allumé(s) s'arrêtent proprement, puis leurs prises sont coupées. Les prises injoignables sont ignorées.`,
        () => api("/api/all", { method: "POST", body: JSON.stringify({ action: "off" }) }));
});

// --- approvisionnement ------------------------------------------------------------------------

function fillModules(select, current) {
    const keep = current ?? select.value;
    select.replaceChildren(el("option", { value: "", text: "— aucun —" }), ...state.modules.map((m) => el("option", { value: m, text: m })));
    select.value = state.modules.includes(keep) ? keep : "";
}

function codeValid(code) {
    const c = code.trim();
    if (/^MT:[A-Z0-9.\-]+$/i.test(c)) return true;
    const digits = c.replace(/[\s-]/g, "");
    return /^[\d\s-]+$/.test(c) && (digits.length === 11 || digits.length === 21);
}

function updateAddButton() {
    const ok = codeValid($("code").value);
    $("code-hint").textContent = $("code").value && !ok ? "Code incomplet ou invalide." : "";
    $("add-btn").disabled = !ok || (state && state.job.state === "running");
}

const STEPS = [["wifi", "Transmission du Wi-Fi de l'expo"], ["commission", "Bluetooth, vérification de la prise, connexion au Wi-Fi"], ["done", "Prise ajoutée"]];

function renderJob() {
    const job = state.job;
    $("job-panel").hidden = job.state === "idle";
    if (job.state === "idle") return;
    const order = STEPS.map((s) => s[0]);
    const current = job.state === "done" ? order.length : Math.max(0, order.indexOf(job.step));
    $("job-title").textContent = `Ajout : ${job.name || job.module || "nouvelle prise"}`;
    $("job-steps").replaceChildren(...STEPS.map(([id, label], i) =>
        el("li", { class: i < current || job.state === "done" ? "done" : i === current && job.state === "running" ? "active" : "", text: label })));
    const elapsed = job.started ? Math.round(Date.now() / 1000 - job.started) : 0;
    $("job-hint").textContent = job.state === "running" ? `En cours depuis ${elapsed} s. Une minute ou deux, parfois plus.`
        : job.state === "done" ? job.message : "";
    $("job-error").hidden = job.state !== "error";
    $("job-error").textContent = job.state === "error"
        ? `${job.error} — Vérifie que la prise est en mode appairage (réinitialisée) et près du serveur, puis recommence.` : "";
    $("job-close").hidden = job.state === "running";
}

$("add-btn").addEventListener("click", async () => {
    $("add-error").hidden = true;
    try {
        await api("/api/plugs", { method: "POST", body: JSON.stringify({ code: $("code").value, module: $("module").value, name: $("name").value }) });
        $("code").value = ""; $("name").value = "";
    } catch (e) {
        $("add-error").hidden = false; $("add-error").textContent = e.message;
    }
    refresh();
});
$("code").addEventListener("input", updateAddButton);
$("job-close").addEventListener("click", () => api("/api/job/dismiss", { method: "POST" }).then(refresh));

function renderRows() {
    const c = state.controller;
    $("ctl-info").textContent = c ? `Contrôleur Matter ${c.sdk || ""} · Bluetooth ${c.bluetooth ? "actif" : "INACTIF"} · Wi-Fi ${c.wifi ? "transmis" : "pas encore transmis (fait au premier ajout)"}` : "";
    $("plug-rows").replaceChildren(...(state.plugs.length ? state.plugs.map((p) => el("tr", {},
        el("td", {}, el("span", { class: `dot ${p.available ? "ok" : "err"}` }), p.name),
        el("td", { text: p.module || "—" }),
        el("td", { text: !p.available ? "injoignable" : `en ligne · ${p.on ? "allumée" : "éteinte"}` }),
        el("td", {}, p.measures ? "W" + (p.energy_wh != null ? ", Wh" : "") : el("span", { class: "chip warn", text: "pas de mesure" })),
        el("td", { class: "mono", text: `${p.vendor} ${p.product} · ${p.version}`.trim() }),
        el("td", { class: "actions" },
            el("button", { type: "button", class: "secondary small", text: "Identifier", onclick: () => api(`/api/plugs/${p.node_id}/identify`, { method: "POST" }).catch((e) => alert(e.message)) }),
            el("button", { type: "button", class: "secondary small", text: "Modifier", onclick: () => editPlug(p) }),
            el("button", { type: "button", class: "danger small", text: "Retirer", onclick: () => confirmAction(`Retirer ${p.name} ?`,
                "La prise est désappairée du serveur. Pour la réutiliser, il faudra la réinitialiser (bouton maintenu).",
                () => api(`/api/plugs/${p.node_id}`, { method: "DELETE" })) })))) : [el("tr", {}, el("td", { colspan: "6", class: "hint", text: "Aucune prise." }))]));
}

function editPlug(p) {
    $("edit-name").value = p.name;
    fillModules($("edit-module"), p.module);
    $("edit-go").onclick = async () => {
        closeModals();
        try { await api(`/api/plugs/${p.node_id}`, { method: "PUT", body: JSON.stringify({ name: $("edit-name").value, module: $("edit-module").value }) }); }
        catch (e) { alert(e.message); }
        refresh();
    };
    openModal("edit-modal");
}

// --- scan du QR code (camera arriere du telephone, webcam du PC) ------------------------------------
// BarcodeDetector quand le navigateur l'a (Chrome, Android) ; sinon jsQR, copie locale (pas d'internet).

let stream = null, scanTimer = null, cameras = [], camIndex = 0;
const canvas = document.createElement("canvas");
const ctx = canvas.getContext("2d", { willReadFrequently: true });
const detector = "BarcodeDetector" in window ? new window.BarcodeDetector({ formats: ["qr_code"] }) : null;

async function startScan() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        alert("Caméra indisponible : la page doit être ouverte en HTTPS, sur un appareil avec caméra. Saisis le code à la main.");
        return;
    }
    openModal("scan-modal");
    $("scan-hint").textContent = "Présente le QR code de la prise devant la caméra.";
    try {
        const constraints = cameras.length ? { deviceId: { exact: cameras[camIndex].deviceId } } : { facingMode: { ideal: "environment" } };
        stream = await navigator.mediaDevices.getUserMedia({ video: constraints, audio: false });
        $("video").srcObject = stream;
        await $("video").play();
        if (!cameras.length) cameras = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
        $("cam-switch").hidden = cameras.length < 2;
        scanTimer = setInterval(scanFrame, 250);
    } catch (e) {
        $("scan-hint").textContent = `Caméra refusée ou absente (${e.name}). Saisis le code à la main.`;
    }
}

function stopScan() {
    clearInterval(scanTimer); scanTimer = null;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
}

async function scanFrame() {
    const video = $("video");
    if (!video.videoWidth) return;
    let text = null;
    try {
        if (detector) {
            const codes = await detector.detect(video);
            text = codes.length ? codes[0].rawValue : null;
        } else if (window.jsQR) {
            canvas.width = video.videoWidth; canvas.height = video.videoHeight;
            ctx.drawImage(video, 0, 0);
            const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
            const found = window.jsQR(img.data, img.width, img.height, { inversionAttempts: "dontInvert" });
            text = found ? found.data : null;
        }
    } catch (e) { return; }
    if (!text) return;
    if (!/^MT:/i.test(text.trim())) { $("scan-hint").textContent = "Ce QR code n'est pas un code Matter (il doit commencer par MT:)."; return; }
    $("code").value = text.trim();
    closeModals();
    updateAddButton();
    $("code-hint").textContent = "✓ QR code lu : code Matter reconnu.";
}

$("scan-btn").addEventListener("click", startScan);
$("scan-cancel").addEventListener("click", closeModals);
$("cam-switch").addEventListener("click", () => { stopScan(); camIndex = (camIndex + 1) % cameras.length; startScan(); });

// --- boucle ------------------------------------------------------------------------------------

async function refresh() {
    try {
        state = await api("/api/state");
        $("ctl-error").hidden = !state.error;
        $("ctl-error").textContent = state.error ? `Contrôleur Matter : ${state.error}` : "";
        if (document.activeElement !== $("module")) fillModules($("module"));
        renderDashboard();
        renderJob();
        renderRows();
        updateAddButton();
    } catch (e) {
        $("ctl-error").hidden = false; $("ctl-error").textContent = `Page injoignable : ${e.message}`;
    }
}

showTab();
refresh();
setInterval(refresh, 3000);

// Console audio de l'administrateur. Les noms de machines viennent du reseau : toujours inseres
// comme TEXTE (textContent / value), jamais en HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const k = (s) => String(s).replace(/[^A-Za-z0-9_-]/g, (c) => "_" + c.charCodeAt(0).toString(16));
const S = { data: null, sel: null, sig: "", dragging: false, timers: {} };

function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") n.className = v;
        else if (k === "text") n.textContent = v;
        else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v);
    }
    kids.forEach((c) => c && n.append(c));
    return n;
}

async function api(path, body, method = "POST") {
    const res = await fetch(path, { method, headers: { "Content-Type": "application/json", "X-Requested-With": "expolab" },
                                    body: body === undefined ? undefined : JSON.stringify(body) });
    if (res.status === 401) { location.href = "/"; return {}; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Erreur");
    return data;
}

// Envoie au plus toutes les 80 ms pendant qu'on deplace un fader.
function send(key, path, body) {
    clearTimeout(S.timers[key]);
    S.timers[key] = setTimeout(() => api(path, body).catch(showError), 80);
}

function showError(e) { $("perr").textContent = e.message; setTimeout(() => { $("perr").textContent = ""; }, 4000); }

function fmt(db) { return db <= -59.5 ? "-∞" : `${Math.round(db * 10) / 10} dB`; }
function height(db, mute) { return mute ? 0 : Math.max(0, Math.min(100, (db + 60) / 60 * 100)); }

// --- structure : reconstruite seulement quand la liste des machines / salles change ---------------

function signature(d) {
    return JSON.stringify([d.multi, d.rooms.map((r) => r.id), d.devices.map((x) => [x.name, x.room]), d.pending.map((x) => x.name)]);
}

function stripFor(dev) {
    const input = el("input", { type: "range", class: "v", min: "-60", max: "0", step: "0.5", "aria-label": `Volume ${dev.label}` });
    input.addEventListener("input", () => { S.dragging = true; send(`d:${dev.name}`, `/api/devices/${encodeURIComponent(dev.name)}`, { db: parseFloat(input.value) }); $(`db-${dev.name}`).textContent = fmt(parseFloat(input.value)); });
    input.addEventListener("change", () => { S.dragging = false; });
    const mute = el("button", { type: "button", id: `mu-${dev.name}`, text: "Mute", "aria-label": `Mute ${dev.label}`, onclick: (e) => {
        e.stopPropagation();
        const cur = S.data.devices.find((x) => x.name === dev.name);
        api(`/api/devices/${encodeURIComponent(dev.name)}`, { mute: !cur.mute }).then(refresh).catch(showError);
    } });
    const st = el("div", { class: "st", id: `st-${dev.name}`, onclick: () => { S.sel = dev.name; render(); } },
        el("div", { class: "nm", id: `nm-${dev.name}` }),
        el("div", { class: "sd", id: `sd-${dev.name}` }, el("span", { class: "dot" }), el("span", { id: `sdt-${dev.name}` })),
        el("div", { class: "fw" }, input, el("div", { class: "bar" }, el("i", { id: `bar-${dev.name}` }))),
        el("div", { class: "db", id: `db-${dev.name}` }), mute);
    st.querySelector("input").id = `in-${dev.name}`;
    return st;
}

function busFor(room) {
    const input = el("input", { type: "range", class: "v", min: "-60", max: "0", step: "0.5", "aria-label": `Volume ${room.name}` });
    input.addEventListener("input", () => { S.dragging = true; send(`r:${room.id}`, `/api/rooms/${encodeURIComponent(room.id)}`, { db: parseFloat(input.value) }); $(`rdb-${k(room.id)}`).textContent = fmt(parseFloat(input.value)); });
    input.addEventListener("change", () => { S.dragging = false; });
    input.id = `rin-${k(room.id)}`;
    const mute = el("button", { type: "button", id: `rmu-${k(room.id)}`, text: "Mute", onclick: () => {
        const cur = S.data.rooms.find((x) => x.id === room.id);
        api(`/api/rooms/${encodeURIComponent(room.id)}`, { mute: !cur.mute }).then(refresh).catch(showError);
    } });
    return el("div", { class: "bus" }, el("div", { class: "nm", text: room.name }), el("div", { class: "sd", text: "volume de la salle" }),
        el("div", { class: "fw" }, input, el("div", { class: "bar" }, el("i", { id: `rbar-${k(room.id)}` }))),
        el("div", { class: "db", id: `rdb-${k(room.id)}` }), mute);
}

function build() {
    const d = S.data, bay = $("bay");
    bay.replaceChildren();
    const groups = d.multi ? d.rooms.map((r) => ({ room: r, devices: d.devices.filter((x) => x.room === r.id) }))
                           : [{ room: null, devices: d.devices }];
    groups.forEach((g) => {
        const strips = el("div", { class: "strips" });
        g.devices.forEach((dev) => strips.append(stripFor(dev)));
        bay.append(el("div", { class: "blk" }, g.room ? busFor(g.room) : null, strips));
    });
    $("empty").hidden = d.devices.length > 0 || d.pending.length > 0;
    $("console").hidden = d.devices.length === 0;
    const pend = $("pending");
    pend.replaceChildren();
    d.pending.forEach((p) => pend.append(el("div", { class: "pending" },
        el("span", { class: "grow", text: `Nouvelle machine détectée : ${p.name}` }),
        el("button", { type: "button", text: "Valider", onclick: () => api(`/api/devices/${encodeURIComponent(p.name)}/validate`).then(refresh).catch(showError) }),
        el("button", { type: "button", text: "Ignorer", onclick: () => api(`/api/devices/${encodeURIComponent(p.name)}`, undefined, "DELETE").then(refresh).catch(showError) }))));
}

// --- valeurs : mises a jour a chaque rafraichissement, sans toucher a un fader en cours de deplacement --

function update() {
    const d = S.data;
    d.devices.forEach((dev) => {
        if (!$(`st-${dev.name}`)) return;
        $(`nm-${dev.name}`).textContent = dev.label;
        const sdt = $(`sdt-${dev.name}`), box = $(`sd-${dev.name}`);
        sdt.textContent = !dev.online ? "hors ligne" : (dev.error ? "pas de carte son" : "en ligne");
        box.classList.toggle("warn", dev.online && Boolean(dev.error));
        $(`st-${dev.name}`).className = `st${dev.online ? "" : " off"}${S.sel === dev.name ? " sel" : ""}`;
        if (!S.dragging) $(`in-${dev.name}`).value = dev.db;
        if (!S.dragging) $(`db-${dev.name}`).textContent = dev.mute ? "mute" : fmt(dev.db);
        $(`bar-${dev.name}`).style.height = `${height(dev.effective_db, dev.effective_mute)}%`;
        $(`mu-${dev.name}`).className = dev.mute ? "on" : "";
    });
    d.rooms.forEach((r) => {
        if (!$(`rin-${k(r.id)}`)) return;
        if (!S.dragging) { $(`rin-${k(r.id)}`).value = r.db; $(`rdb-${k(r.id)}`).textContent = r.mute ? "mute" : fmt(r.db); }
        $(`rbar-${k(r.id)}`).style.height = `${height(r.db, r.mute || d.expo.mute)}%`;
        $(`rmu-${k(r.id)}`).className = r.mute ? "on" : "";
    });
    $("m-title").textContent = d.multi ? "Exposition" : "Volume général";
    $("m-sub").textContent = d.multi ? "toutes les salles" : "";
    if (!S.dragging) { $("m-fader").value = d.expo.db; $("m-db").textContent = d.expo.mute ? "mute" : fmt(d.expo.db); }
    $("m-bar").style.height = `${height(d.expo.db, d.expo.mute)}%`;
    $("m-mute").className = `big${d.expo.mute ? " on" : ""}`;
    $("m-mute").textContent = d.expo.mute ? "Rétablir" : "Mute";
    const on = d.devices.filter((x) => x.online).length;
    $("stat").textContent = `${on} en ligne sur ${d.devices.length}` + (d.devices.length - on ? " · une machine hors ligne appliquera son niveau à son retour" : "");
    renderPresets();
    renderDetail();
}

function renderPresets() {
    const d = S.data, box = $("presets");
    box.replaceChildren(el("span", { class: "hint", text: "Presets" }));
    d.presets.forEach((name) => {
        const act = name === d.current;
        box.append(el("button", { type: "button", class: act ? "act" : "", text: name + (act && d.modified ? " (modifié)" : ""),
                                  onclick: () => api(`/api/presets/${encodeURIComponent(name)}/recall`).then(refresh).catch(showError) }),
                   el("button", { type: "button", class: "danger", text: "×", title: `Supprimer le preset ${name}`, "aria-label": `Supprimer ${name}`,
                                  onclick: () => { if (confirm(`Supprimer le preset « ${name} » ?`)) api(`/api/presets/${encodeURIComponent(name)}`, undefined, "DELETE").then(refresh).catch(showError); } }));
    });
}

function renderDetail() {
    const box = $("detail"), d = S.data;
    const dev = d.devices.find((x) => x.name === S.sel);
    if (!dev) { box.textContent = "Clique sur une machine pour voir sa carte son et ses options."; return; }
    if (box.dataset.for === dev.name && box.contains(document.activeElement)) { return; }   // ne pas casser une saisie en cours
    box.dataset.for = dev.name;
    const label = el("input", { type: "text", value: dev.label === dev.name ? "" : dev.label, placeholder: dev.name, maxlength: "40" });
    label.addEventListener("change", () => api(`/api/devices/${encodeURIComponent(dev.name)}`, { label: label.value }).then(refresh).catch(showError));
    const free = el("input", { type: "checkbox" });
    free.checked = dev.free;
    free.addEventListener("change", () => api(`/api/devices/${encodeURIComponent(dev.name)}`, { free: free.checked }).then(refresh).catch(showError));
    box.replaceChildren(
        el("b", { text: dev.name }),
        el("div", { class: "grid" },
            el("div", {}, el("label", { text: "Nom affiché" }), label,
                el("div", { class: "hint", text: `Salle : ${dev.room_name || "—"} (se règle dans Bastion)` })),
            el("div", {}, el("label", {}, free, el("span", { text: "Hors volume de l'exposition" })),
                el("span", { class: "hint", text: "ne suit pas le volume général (ex. annonces) ; le mute général la coupe quand même" })),
            el("div", {}, el("div", { text: dev.sink ? `Sortie : ${dev.sink}` : "Sortie : inconnue" }),
                el("div", { class: "hint", text: dev.error || (dev.online ? `contact il y a ${dev.seen_ago ?? 0} s` : dev.seen_ago == null ? "jamais vue" : "hors ligne") }),
                el("button", { type: "button", class: "danger", text: "Oublier cette machine",
                               onclick: () => { if (confirm(`Oublier ${dev.name} ?`)) { S.sel = null; api(`/api/devices/${encodeURIComponent(dev.name)}`, undefined, "DELETE").then(refresh).catch(showError); } } }))));
}

function render() {
    const sig = signature(S.data);
    if (sig !== S.sig) { S.sig = sig; build(); }
    update();
}

async function refresh() {
    try {
        S.data = await (await fetch("/api/state", { cache: "no-store" })).json();
        render();
    } catch (e) { /* hors ligne : on reessaie au prochain tour */ }
}

// --- commandes generales ---------------------------------------------------------------------------

$("m-fader").addEventListener("input", () => { S.dragging = true; send("expo", "/api/expo", { db: parseFloat($("m-fader").value) }); $("m-db").textContent = fmt(parseFloat($("m-fader").value)); });
$("m-fader").addEventListener("change", () => { S.dragging = false; });
$("m-mute").addEventListener("click", () => api("/api/expo", { mute: !S.data.expo.mute }).then(refresh).catch(showError));
$("kill").addEventListener("click", () => api("/api/kill").then(refresh).catch(showError));
$("psave").addEventListener("click", () => api("/api/presets", { name: $("pname").value }).then(() => { $("pname").value = ""; return refresh(); }).catch(showError));
$("pname").addEventListener("input", () => { $("perr").textContent = ""; });
document.addEventListener("pointerup", () => { S.dragging = false; });

refresh();
setInterval(refresh, 1000);

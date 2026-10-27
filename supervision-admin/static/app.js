// Page de supervision. Les noms (machines, salles, canaux) viennent de Bastion ou de la saisie : toujours inseres
// comme TEXTE (textContent / value), jamais en HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const S = { state: null, sel: null, tab: "state", settings: null, fields: null, channels: null, editing: null,
            graph: { machine: null, hours: 24, data: null, at: 0, loading: false } };
const STATUS = {
    ok: ["s-ok", "En ordre"], alerte: ["s-alerte", "En alerte"], avertissement: ["s-avertissement", "À surveiller"],
    "démarrage": ["s-sleep", "Démarre…"], "fermée": ["s-sleep", "Éteinte (expo fermée)"],
    maintenance: ["s-off", "En maintenance"], "en attente": ["s-sleep", "Première vérification…"],
};

function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") n.className = v;
        else if (k === "text") n.textContent = v;
        else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v);
    }
    kids.forEach((c) => c !== null && c !== undefined && c !== false && n.append(c));
    return n;
}

async function api(path, body, method = "POST") {
    const res = await fetch(path, { method,
                                    headers: { "Content-Type": "application/json", "X-Requested-With": "expolab" },
                                    body: body === undefined ? undefined : JSON.stringify(body) });
    if (res.status === 401) { location.href = "/"; return {}; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Erreur");
    return data;
}

function flash(id, text, ms = 4000) { $(id).textContent = text; setTimeout(() => { $(id).textContent = ""; }, ms); }

const pad = (n) => String(n).padStart(2, "0");
function when(ts) {
    const d = new Date(ts * 1000), now = new Date();
    const day = d.toDateString() === now.toDateString() ? "Auj." :
        d.toDateString() === new Date(now - 86400000).toDateString() ? "Hier" : `${pad(d.getDate())}/${pad(d.getMonth() + 1)}`;
    return `${day} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function duration(s) {
    s = Math.max(0, Math.round(s));
    if (s < 60) return `${s} s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ${pad(Math.round((s % 3600) / 60))}`;
    return `${Math.floor(s / 86400)} j ${Math.floor((s % 86400) / 3600)} h`;
}

// --- onglets ---------------------------------------------------------------------------------------

function showTab(tab) {
    S.tab = tab;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    document.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== tab; });
    if (tab === "history") loadHistory();
    if (tab === "settings") loadSettings();
}
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// --- etat ------------------------------------------------------------------------------------------

async function refresh() {
    try { S.state = await api("/api/state", undefined, "GET"); } catch (e) { $("updated").textContent = `Erreur : ${e.message}`; return; }
    renderState();
}

function renderExpo(st) {
    const { expo, now } = st, grace = st.machines.some((m) => m.grace_until);
    const graceLeft = Math.max(0, ...st.machines.map((m) => m.grace_until ? m.grace_until - now : 0));
    let dot = "s-ok", title = "Exposition ouverte", sub = `Surveillance active · source : ${expo.source}`;
    if (!expo.open) { dot = "s-sleep"; title = "Exposition fermée"; sub = `Rien n'est vérifié ni envoyé · depuis ${when(expo.since)} (${expo.source})`; }
    else if (grace && expo.since + 60 * st.settings.startup_grace_minutes > now) {
        dot = "s-avertissement"; title = "Exposition en cours d'ouverture";
        sub = `Délai de démarrage : surveillance complète dans ${duration(graceLeft)}`;
    }
    $("expo-dot").className = `dot big-dot ${dot}`;
    $("expo-title").textContent = title;
    $("expo-sub").textContent = sub;
    $("expo-open").hidden = expo.open;
    $("expo-close").hidden = !expo.open;
    $("bastion-error").hidden = !st.bastion_error;
    $("bastion-error").textContent = `${st.bastion_error} : dernière liste de machines connue.`;
    $("planned-error").hidden = !st.planned_error;
    $("planned-error").textContent = `${st.planned_error} : programmes prévus non affichés.`;
}

function failing(m) { return m.checks.filter((c) => c.level).sort((a, b) => (b.level === "alerte") - (a.level === "alerte")); }

function reason(m) {
    if (m.status === "maintenance") return m.maintenance.note ? `En maintenance · ${m.maintenance.note}` : "En maintenance";
    if (["fermée", "en attente"].includes(m.status)) return STATUS[m.status][1];
    const bad = failing(m);
    if (m.status === "démarrage") return bad.length ? `Démarre… (${bad[0].label.toLowerCase()} : ${bad[0].detail})` : "Démarrée";
    if (bad.length) return bad.map((c) => `${c.label} : ${c.detail}`).join(" · ") + (m.status === "ok" ? " (à confirmer)" : "");
    const temp = m.checks.find((c) => c.id === "temperature");
    return `En ordre${temp ? ` · ${temp.detail}` : ""}`;
}

function renderState() {
    const st = S.state;
    $("updated").textContent = `Mis à jour à ${new Date(st.now * 1000).toLocaleTimeString("fr-FR")}`;
    renderExpo(st);
    $("empty").hidden = st.machines.length > 0;

    const count = (s) => st.machines.filter((m) => m.status === s).length;
    $("summary").replaceChildren(...(st.expo.open
        ? [["ok", "en ordre"], ["avertissement", "à surveiller"], ["alerte", "en alerte"], ["maintenance", "en maintenance"]]
            .filter(([s], i) => i < 3 || count(s)).map(([s, label]) => el("span", { class: "pill" }, el("span", { class: `dot ${STATUS[s][0]}` }), `${count(s)} ${label}`))
        : [el("span", { class: "pill" }, el("span", { class: "dot s-sleep" }), "Surveillance suspendue : rien n'est vérifié ni envoyé")]));

    const rooms = new Map();
    st.machines.forEach((m) => { const r = m.room || "Sans salle"; if (!rooms.has(r)) rooms.set(r, []); rooms.get(r).push(m); });
    $("rooms").replaceChildren(...[...rooms].map(([room, machines]) => {
        const cards = el("div", { class: "cards" }, ...machines.map((m) => el("div", {
            class: `card ${["alerte", "avertissement"].includes(m.status) ? m.status : ""} ${S.sel === m.name ? "sel" : ""}`,
            role: "button", tabindex: "0", onclick: () => { S.sel = S.sel === m.name ? null : m.name; renderState(); },
        },
            el("div", { class: "top" }, el("span", { text: m.name }), el("span", { class: `dot ${STATUS[m.status][0]}`, title: STATUS[m.status][1] })),
            el("div", { class: "why", text: reason(m) }),
            el("div", { class: "row" }, ...m.tags.map((t) => el("span", { class: "tag", text: t }))))));
        const selected = machines.find((m) => m.name === S.sel);
        return el("div", { class: "room" },
            el("h2", {}, room, el("span", { class: "hint", text: `${machines.length} machine${machines.length > 1 ? "s" : ""}` })),
            cards, selected ? detail(selected) : null);
    }));
}

function detail(m) {
    const now = S.state.now;
    const checks = m.checks.map((c) => el("div", { class: `ck ${c.level ? "pending" : ""}` },
        el("span", { class: `dot ${c.level ? STATUS[c.level][0] : "s-ok"}` }),
        el("span", { text: c.label }),
        el("span", { class: "val", text: [c.detail, c.level && c.failing_since ? `depuis ${duration(now - c.failing_since)}` : ""].filter(Boolean).join(" · ") || "ok" })));
    const note = el("input", { type: "text", placeholder: "Note (facultative) : écran en réparation…", maxlength: "200", class: "grow" });
    const err = el("span", { class: "err" });
    const toggle = (on) => api("/api/maintenance", { machine: m.name, on, note: note.value }).then(refresh).catch((e) => { err.textContent = e.message; });
    const actions = m.maintenance
        ? el("div", { class: "row" }, el("span", { class: "hint grow", text: `En maintenance depuis ${when(m.maintenance.since)}${m.maintenance.note ? ` · ${m.maintenance.note}` : ""}` }),
            el("button", { type: "button", class: "act", text: "Fin de maintenance", onclick: () => toggle(false) }), err)
        : el("div", { class: "row" }, note,
            el("button", { type: "button", text: "Mettre en maintenance", onclick: () => toggle(true) }), err);
    return el("div", { class: "panel detail" },
        el("h2", { text: m.name }),
        m.polled_at ? el("p", { class: "hint", text: `Dernière vérification ${when(m.polled_at)}` }) : null,
        ...(checks.length ? checks : [el("p", { class: "hint", text: m.maintenance ? "Pas vérifiée pendant la maintenance." : "Pas encore vérifiée." })]),
        m.program ? programBlock(m) : null,
        graphBlock(m),
        el("h2", { text: "Maintenance" }),
        el("p", { class: "hint", text: "Machine éteinte volontairement : plus aucune vérification ni alerte ; à la sortie, délai de démarrage." }),
        actions);
}

$("expo-close").addEventListener("click", () => {
    if (confirm("Fermer l'exposition ? Plus rien ne sera vérifié ni envoyé jusqu'à sa réouverture.")) api("/api/expo", { open: false }).then(refresh).catch((e) => alert(e.message));
});
$("expo-open").addEventListener("click", () => api("/api/expo", { open: true }).then(refresh).catch((e) => alert(e.message)));

// --- historique ------------------------------------------------------------------------------------

async function loadHistory() {
    const [data, settings] = await Promise.all([api(`/api/history?days=${$("h-days").value}`, undefined, "GET"), api("/api/settings", undefined, "GET")]);
    $("h-retention").textContent = `Conservé ${settings.settings.retention_days} jours`;
    const machines = [...new Set(data.incidents.map((i) => i.machine).filter(Boolean))].sort();
    const current = $("h-machine").value;
    $("h-machine").replaceChildren(el("option", { value: "", text: "Toutes les machines" }), ...machines.map((m) => el("option", { value: m, text: m })));
    $("h-machine").value = machines.includes(current) ? current : "";
    const rows = data.incidents.filter((i) => !$("h-machine").value || i.machine === $("h-machine").value);
    $("h-empty").hidden = rows.length > 0;
    const now = Date.now() / 1000;
    $("h-rows").replaceChildren(...rows.map((i) => {
        const info = i.level === "info";
        const end = i.closed_at === null ? "en cours" : info ? "—" : duration(i.closed_at - i.opened_at);
        const motive = info ? `${i.label}${i.detail ? ` · ${i.detail}` : ""}` :
            `${i.label} : ${i.detail}${i.closed_at !== null && i.close_reason !== "rétabli" ? ` (clos : ${i.close_reason})` : ""}`;
        return el("tr", {},
            el("td", { text: when(i.opened_at) }),
            el("td", { text: i.closed_at === null ? `en cours (${duration(now - i.opened_at)})` : end }),
            el("td", { text: i.machine || "Exposition" }),
            el("td", { class: `lvl-${i.level}`, text: motive }),
            el("td", { text: info ? "—" : (i.notified || "personne") }));
    }));
}
$("h-days").addEventListener("change", loadHistory);
$("h-machine").addEventListener("change", loadHistory);

// --- reglages --------------------------------------------------------------------------------------

const THRESHOLDS = [["Température CPU", "temp", "°C"], ["Disque utilisé", "disk", "%"], ["Mémoire utilisée", "mem", "%"],
                    ["Injoignable depuis", null, "min", "unreachable_minutes"], ["Service arrêté depuis", null, "min", "service_minutes"]];

function numberInput(key) {
    const f = S.fields[key];
    const input = el("input", { type: "number", min: f.min, max: f.max, step: "1", placeholder: f.default, "data-key": key, "aria-label": f.label });
    input.value = S.settings[key] === f.default ? "" : S.settings[key];
    input.addEventListener("input", () => input.classList.toggle("changed", input.value !== "" && Number(input.value) !== f.default));
    input.classList.toggle("changed", input.value !== "");
    return input;
}

async function loadSettings() {
    const [s, ch] = await Promise.all([api("/api/settings", undefined, "GET"), api("/api/channels", undefined, "GET")]);
    S.settings = s.settings; S.fields = s.fields;
    $("s-thresholds").replaceChildren(...THRESHOLDS.map(([label, prefix, unit, single]) => {
        const keys = prefix ? [`${prefix}_warn`, `${prefix}_alert`] : [null, single];
        const inputs = keys.map((k) => k && numberInput(k));
        const reset = el("button", { type: "button", class: "small", text: "Rétablir", onclick: () => inputs.forEach((i) => { if (i) { i.value = ""; i.classList.remove("changed"); } }) });
        return el("tr", {}, el("td", { text: label }),
            ...inputs.map((i) => el("td", {}, i ? i : "—", i ? ` ${unit}` : "")), el("td", {}, reset));
    }));
    $("s-grace").replaceChildren("Démarrage : surveiller après ", numberInput("startup_grace_minutes"), " min");
    const retention = String(S.settings.retention_days);
    if (![...$("s-retention").options].some((o) => o.value === retention)) $("s-retention").append(el("option", { value: retention, text: `${retention} jours` }));
    $("s-retention").value = retention;
    const graphDays = String(S.settings.graph_days);
    if (![...$("s-graph-days").options].some((o) => o.value === graphDays)) $("s-graph-days").append(el("option", { value: graphDays, text: `${graphDays} jours` }));
    $("s-graph-days").value = graphDays;
    renderNight(ch.night);
    S.kindOrder = ch.kinds.map((k) => k.id);
    S.kinds = Object.fromEntries(ch.kinds.map((k) => [k.id, k]));
    renderChannels(ch.channels);
}

function renderNight(night) {
    $("n-enabled").checked = night.enabled;
    $("n-start").value = night.start;
    $("n-end").value = night.end;
    $("n-hint").textContent = `Rien n'est envoyé la nuit ; à ${night.end}, si l'expo est ouverte, un récapitulatif part sur tous les canaux actifs (problèmes de la nuit et problèmes encore en cours).`;
}

$("s-save").addEventListener("click", async () => {
    const changes = { retention_days: Number($("s-retention").value), graph_days: Number($("s-graph-days").value) };
    document.querySelectorAll("input[data-key]").forEach((i) => { changes[i.dataset.key] = i.value === "" ? S.fields[i.dataset.key].default : Number(i.value); });
    try {
        await api("/api/settings", changes);
        renderNight((await api("/api/night", { enabled: $("n-enabled").checked, start: $("n-start").value, end: $("n-end").value })).night);
        flash("s-ok", "Enregistré.");
        loadSettings();
    } catch (e) { flash("s-err", e.message); }
});

// --- canaux ----------------------------------------------------------------------------------------

function renderChannels(channels) {
    S.channels = channels;
    if (!channels.length) { $("channels").replaceChildren(el("p", { class: "hint", text: "Aucun canal : personne ne sera prévenu." })); return; }
    $("channels").replaceChildren(...channels.map((c) => {
        const levels = c.levels.map((l) => (l === "alerte" ? "alertes" : "avertissements")).join(" + ");
        const last = !c.last ? "jamais utilisé" : c.last.ok ? `dernier envoi réussi ${when(c.last.at)}` : `échec ${when(c.last.at)} : ${c.last.error}`;
        const out = el("span", { class: "err" });
        return el("div", { class: "channel" },
            el("span", { class: `dot ${!c.enabled ? "s-off" : !c.last || c.last.ok ? "s-ok" : "s-alerte"}` }),
            el("div", { class: "grow" }, el("b", { text: c.name }), ` · ${S.kinds[c.kind].label} · ${levels}${c.enabled ? "" : " · coupé"}`,
                el("span", { class: "hint", text: last })),
            el("button", { type: "button", class: "small", text: "Tester", onclick: async (e) => {
                e.target.disabled = true;
                try { const r = await api(`/api/channels/${c.id}/test`, {}); out.textContent = r.last.ok ? "" : r.last.error; } catch (err) { out.textContent = err.message; }
                e.target.disabled = false;
                renderChannels((await api("/api/channels", undefined, "GET")).channels);
            } }),
            el("button", { type: "button", class: "small", text: c.enabled ? "Couper" : "Activer", onclick: () => saveChannel({ ...c, fields: {}, enabled: !c.enabled }) }),
            el("button", { type: "button", class: "small", text: "Modifier", onclick: () => openForm(c) }),
            el("button", { type: "button", class: "small danger", text: "Supprimer", onclick: () => {
                if (confirm(`Supprimer le canal « ${c.name} » ?`)) api(`/api/channels/${c.id}`, {}, "DELETE").then(reloadChannels).catch((e) => alert(e.message));
            } }), out);
    }));
}

async function reloadChannels() { renderChannels((await api("/api/channels", undefined, "GET")).channels); }

async function saveChannel(data) {
    await api("/api/channels", data);
    await reloadChannels();
}

function renderFields(kind, channel) {
    $("c-fields").replaceChildren(...S.kinds[kind].fields.map((f) => {
        const input = el("input", { type: f.secret ? "password" : "text", "data-field": f.name });
        if (channel && !f.secret) input.value = channel.fields[f.name] || "";
        if (channel && f.secret && channel.fields[f.name]) input.placeholder = "inchangé (laisser vide pour garder)";
        return el("label", { class: "field" }, f.label, input);
    }));
}

function openForm(channel) {
    S.editing = channel || null;
    $("c-kind").replaceChildren(...S.kindOrder.map((k) => el("option", { value: k, text: S.kinds[k].label })));
    $("c-kind").value = channel ? channel.kind : "telegram";
    $("c-kind").disabled = !!channel;
    $("c-name").value = channel ? channel.name : "";
    $("c-alert").checked = channel ? channel.levels.includes("alerte") : true;
    $("c-warn").checked = channel ? channel.levels.includes("avertissement") : false;
    $("c-enabled").checked = channel ? channel.enabled : true;
    renderFields($("c-kind").value, channel);
    $("c-form").hidden = false;
}
$("c-add").addEventListener("click", () => openForm(null));
$("c-cancel").addEventListener("click", () => { $("c-form").hidden = true; });
$("c-kind").addEventListener("change", () => renderFields($("c-kind").value, null));
$("c-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fields = {};
    document.querySelectorAll("#c-fields input").forEach((i) => { fields[i.dataset.field] = i.value; });
    const levels = [$("c-alert").checked && "alerte", $("c-warn").checked && "avertissement"].filter(Boolean);
    try {
        await saveChannel({ id: S.editing && S.editing.id, kind: $("c-kind").value, name: $("c-name").value, fields, levels, enabled: $("c-enabled").checked });
        $("c-form").hidden = true;
    } catch (err) { flash("c-err", err.message); }
});

refresh();
setInterval(() => { if (S.tab === "state" && !document.hidden) refresh(); }, 5000);

// --- programme du kiosque (sonde de la machine / onglet Applications d'ansible-web) ---------------

const PROGRAM_STATUS = { "à jour": "s-ok", "différent": "s-avertissement", absent: "s-avertissement", "non prévu": "s-sleep", inconnu: "s-sleep" };

function programBlock(m) {
    const p = m.program, now = S.state.now, inst = p.installed, plan = p.planned;
    const installed = inst ? [inst.name, inst.version].filter(Boolean).join(" ") : m.polled_at ? "aucun programme" : "—";
    const planned = !plan ? "rien de choisi" : plan.version === "latest"
        ? `${plan.app} · dernière version${plan.last_run ? ` (${plan.last_run} au dernier run)` : ""}` : `${plan.app} ${plan.version}`;
    let running = "—";
    if (inst) {
        running = inst.running ? `oui${inst.started ? `, depuis ${duration(now - inst.started)}` : ""}` : "non";
        if (p.restarts !== null) running += ` · ${p.restarts} relance${p.restarts > 1 ? "s" : ""} du kiosque`;
    }
    const cell = (label, ...value) => el("div", {}, el("div", { class: "hint", text: label }), el("div", { class: "pval" }, ...value));
    return el("div", { class: "program" },
        el("h2", { text: "Programme" }),
        el("div", { class: "pgrid" },
            cell("Installé sur la machine", installed),
            cell("Prévu dans ansible-web", planned, p.status ? el("span", { class: "pill small" }, el("span", { class: `dot ${PROGRAM_STATUS[p.status]}` }), p.status) : null),
            cell("En marche", el("span", { class: inst && !inst.running ? "bad" : "", text: running }))),
        el("p", { class: "hint", text: "Programme arrêté ou qui plante en boucle : alerte « Programme ». Version différente de celle prévue : seulement affichée." }));
}

// --- courbes (un point par minute, moyennes par pas ; trous = pas de mesure) ------------------------

const GRAPHS = [
    ["Température", 1, "°C", [20, 100], "temp", "g-temp"],
    ["Processeur", 2, "%", [0, 100], null, "g-cpu"],
    ["Mémoire utilisée", 3, "%", [0, 100], "mem", "g-mem"],
    ["Disque utilisé", 4, "%", [0, 100], "disk", "g-disk"],
];
function svg(tag, attrs = {}) {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
}

async function loadGraph(machine, hours) {
    const g = S.graph;
    if (g.loading) return;
    g.loading = true;
    try {
        g.data = await api(`/api/metrics?machine=${encodeURIComponent(machine)}&hours=${hours}`, undefined, "GET");
    } catch (e) { g.data = { error: e.message }; }
    Object.assign(g, { machine, hours, at: Date.now(), loading: false });
    if (S.sel === machine) renderState();
}

function graphBlock(m) {
    const g = S.graph;
    if (g.machine !== m.name || Date.now() - g.at > 60000) loadGraph(m.name, g.hours);
    const ranges = el("div", { class: "row ranges" }, ...[[6, "6 h"], [24, "24 h"], [168, "7 j"]].map(([h, label]) =>
        el("button", { type: "button", class: `small ${g.hours === h ? "on" : ""}`, text: label,
                       onclick: () => { g.hours = h; g.at = 0; loadGraph(m.name, h); } })));
    const head = el("div", { class: "row" }, el("h2", { class: "grow", text: "Courbes" }), ranges);
    const box = (...kids) => el("div", { class: "graphs" }, head, ...kids);
    if (g.machine !== m.name || !g.data) return box(el("p", { class: "hint", text: "Chargement…" }));
    if (g.data.error) return box(el("p", { class: "err", text: g.data.error }));
    if (!g.data.points.length) return box(el("p", { class: "hint", text: "Pas encore de mesure sur cette période." }));
    return box(el("div", { class: "ggrid" }, ...GRAPHS.map((spec) => chart(spec, g.data))),
        el("p", { class: "hint", text: "Zones grises : pas de mesure (expo fermée, maintenance ou machine injoignable). Zones colorées : problème signalé. Tirets : seuils d'avertissement et d'alerte." }));
}

function chart([title, idx, unit, [lo, hi], threshold, cls], d) {
    const W = 300, H = 90, span = d.until - d.since, maxGap = 2 * d.step + 60;
    const points = d.points.filter((p) => p[idx] !== null), values = points.map((p) => p[idx]);
    if (!values.length) return el("div", { class: "chart" }, el("div", { class: "row" }, el("span", { class: "hint grow", text: title }), el("span", { class: "hint", text: "non publié" })));
    lo = Math.min(lo, Math.floor(Math.min(...values) / 10) * 10);
    hi = Math.max(hi, Math.ceil(Math.max(...values) / 10) * 10);
    const x = (t) => ((t - d.since) / span) * W, y = (v) => H - ((v - lo) / (hi - lo)) * H;
    const root = svg("svg", { viewBox: `0 0 ${W} ${H + 14}`, class: "plot", role: "img", "aria-label": title });
    // trous : pas de mesure pendant plus de deux pas, y compris avant la premiere et apres la derniere
    const edges = [d.since - d.step, ...points.map((p) => p[0]), d.until];
    for (let i = 1; i < edges.length; i++) {
        if (edges[i] - edges[i - 1] > maxGap) {
            const a = Math.max(0, x(edges[i - 1] + d.step)), b = Math.min(W, x(edges[i]));
            if (b > a) root.append(svg("rect", { x: a, y: 0, width: b - a, height: H, class: "gap" }));
        }
    }
    d.incidents.forEach((i) => {
        const a = Math.max(0, x(i.opened_at)), b = Math.min(W, x(i.closed_at ?? d.until));
        if (b > a) root.append(svg("rect", { x: a, y: 0, width: Math.max(1, b - a), height: H, class: `band band-${i.level}` }));
    });
    if (threshold) [["warn", "th-warn"], ["alert", "th-alert"]].forEach(([k, c]) => {
        const v = S.state.settings[`${threshold}_${k}`];
        if (v > lo && v < hi) root.append(svg("line", { x1: 0, x2: W, y1: y(v), y2: y(v), class: c }));
    });
    let path = "", prev = null;
    points.forEach((p) => {
        path += `${prev !== null && p[0] - prev <= maxGap ? "L" : "M"}${x(p[0]).toFixed(1)} ${y(p[idx]).toFixed(1)}`;
        prev = p[0];
    });
    root.append(svg("path", { d: path, class: `line ${cls}` }));
    for (let k = 0; k < 4; k++) {
        const dt = new Date((d.since + (span * k) / 4) * 1000);
        const text = svg("text", { x: (W * k) / 4 + 2, y: H + 12, class: "axis" });
        text.textContent = span > 2 * 86400 ? `${pad(dt.getDate())}/${pad(dt.getMonth() + 1)}` : `${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
        root.append(text);
    }
    return el("div", { class: "chart" },
        el("div", { class: "row" }, el("span", { class: "hint grow", text: title }), el("b", { text: `${Math.round(values.at(-1))} ${unit}` })),
        root, el("div", { class: "hint", text: `min ${Math.round(Math.min(...values))} · max ${Math.round(Math.max(...values))} ${unit}` }));
}

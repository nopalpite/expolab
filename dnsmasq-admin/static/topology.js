// Onglet "Topologie" : etat complet recu par SSE (/api/topology/stream) a chaque
// changement - arrivee/depart d'une machine, bail, reservation. Les noms viennent
// des machines elles-memes (DHCP) : toujours inseres comme texte, jamais en HTML.
const topo = { data: null, filter: "all", search: "", selected: null, offset: 0 };

function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else node.setAttribute(k, v);
    }
    children.flat().forEach(c => c && node.append(c));
    return node;
}

function ago(epoch) {
    if (!epoch) return "";
    const s = Math.max(0, Math.round(Date.now() / 1000 + topo.offset - epoch));
    if (s < 60) return `${s} s`;
    if (s < 3600) return `${Math.floor(s / 60)} min`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
    return `${Math.floor(s / 86400)} j`;
}

const STATE = {
    online: { dot: "ok", text: () => "en ligne" },
    offline: { dot: "off", text: m => `hors ligne depuis ${ago(m.since)}` },
    unknown: { dot: "unk", text: () => "presence inconnue" },
    absent: { dot: "off", text: () => "aucun bail" },
};

function matches(m) {
    if (topo.filter === "online" && m.state !== "online") return false;
    if (topo.filter === "offline" && !(m.state === "offline" || m.state === "absent")) return false;
    if (topo.filter === "alerts" && !m.alerts.length) return false;
    const q = topo.search;
    return !q || [m.hostname, m.ip, m.mac].some(v => (v || "").toLowerCase().includes(q));
}

function machineRow(m) {
    const st = STATE[m.state] || STATE.unknown;
    const chips = el("span", { class: "n-chips" },
        el("span", { class: `chip ${m.state === "online" ? "ok" : ""}`, text: st.text(m) }),
        m.reserved ? el("span", { class: "chip", text: m.reserved === "nom" ? "reservee par nom" : "reservee par MAC" }) : null,
        m.alerts.map(a => el("span", { class: "chip bad", text: a })),
    );
    const node = el("button", { type: "button", class: `node ${m.state === "absent" ? "ghost" : ""} ${topo.selected === m.id ? "sel" : ""}` },
        el("span", { class: `dot ${m.alerts.length ? "bad" : st.dot}` }),
        el("span", { class: "n-name", text: m.hostname }),
        el("span", { class: "n-meta", text: m.mac ? `${m.ip} - ${m.mac}` : m.ip }),
        chips,
    );
    node.addEventListener("click", () => { topo.selected = m.id; render(); });
    return el("div", { class: "row" }, node);
}

function renderDetail() {
    const m = topo.data && topo.data.machines.find(x => x.id === topo.selected);
    const list = document.getElementById("d-list");
    const actions = document.getElementById("d-actions");
    document.getElementById("d-empty").hidden = !!m;
    list.hidden = !m;
    list.replaceChildren();
    actions.replaceChildren();
    document.getElementById("d-title").textContent = m ? m.hostname : "Machine";
    if (!m) return;
    const st = STATE[m.state] || STATE.unknown;
    const rows = [
        ["Etat", st.text(m)],
        ["Vue pour la derniere fois", m.state === "online" ? "a l'instant" : (m.last_seen ? `il y a ${ago(m.last_seen)}` : "-")],
        ["IP", m.ip],
        ["MAC", m.mac || "-"],
        ["Reservation", m.reserved === "nom" ? "par nom" : m.reserved === "mac" ? "par MAC" : "aucune"],
        ["Bail", m.lease_expires_in_min == null ? "aucun" : `expire dans ${m.lease_expires_in_min} min`],
        ["Alertes", m.alerts.length ? m.alerts.join(", ") : "aucune"],
    ];
    rows.forEach(([k, v]) => list.append(el("dt", { text: k }), el("dd", { text: v })));

    const go = fn => () => { location.hash = "#baux"; showTab("baux"); loadReservations().then(() => { loadLeases(); fn(); }); };
    if (!m.reserved && m.mac) {
        const name = m.hostname === "(inconnu)" ? "" : m.hostname;
        actions.append(el("button", { type: "button", text: "Reserver" }));
        actions.lastChild.addEventListener("click", go(() => startReservationFromLease(m.mac, m.ip, name)));
    }
    if (m.reservation_id) {
        actions.append(el("button", { type: "button", class: "secondary", text: "Modifier la reservation" }));
        actions.lastChild.addEventListener("click", go(() => startEditReservation(m.reservation_id)));
    }
    if (m.lease_expires_in_min != null) {
        actions.append(el("button", { type: "button", class: "danger", text: "Liberer le bail" }));
        actions.lastChild.addEventListener("click", () => releaseLease(m.mac, m.ip));
    }
}

function renderFeed() {
    const feed = document.getElementById("feed");
    const events = (topo.data && topo.data.events) || [];
    feed.replaceChildren(...(events.length
        ? events.map(e => el("div", { class: `ev ev-${e.kind}` }, el("time", { text: e.time.slice(11) }), el("span", { text: e.message })))
        : [el("p", { class: "empty", text: "Aucun evenement." })]));
}

function render() {
    const d = topo.data;
    if (!d) return;
    document.getElementById("k-online").textContent = `${d.summary.online} / ${d.summary.known}`;
    document.getElementById("k-absent").textContent = d.summary.absent;
    const alerts = document.getElementById("k-alerts");
    alerts.textContent = d.summary.alerts;
    alerts.classList.toggle("warn", d.summary.alerts > 0);
    document.getElementById("presence-warning").hidden = d.presence_ok;
    document.getElementById("server-meta").textContent = `${d.server.ip} - ${d.server.interface} (${d.server.network})`;

    const box = document.getElementById("topo-machines");
    const shown = d.machines.filter(matches);
    box.replaceChildren(...(shown.length
        ? shown.map(machineRow)
        : [el("p", { class: "empty", text: d.machines.length ? "Aucune machine pour ce filtre." : "Aucune machine sur le reseau." })]));
    renderDetail();
    renderFeed();
}

function setLive(ok, text) {
    document.getElementById("live").classList.toggle("down", !ok);
    document.getElementById("live-text").textContent = text;
}

function connect() {
    const source = new EventSource("/api/topology/stream");
    source.onmessage = (e) => {
        topo.data = JSON.parse(e.data);
        topo.offset = topo.data.now - Date.now() / 1000;
        setLive(true, `En direct - ${new Date().toLocaleTimeString("fr-FR")}`);
        render();
        if (currentTab() === "baux") loadReservations().then(loadLeases);
    };
    // EventSource se reconnecte seul ; on le signale seulement.
    source.onerror = () => setLive(false, "Reconnexion...");
}

function currentTab() {
    return location.hash === "#baux" ? "baux" : "topologie";
}

function showTab(name) {
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("on", t.dataset.tab === name));
    document.getElementById("tab-topologie").hidden = name !== "topologie";
    document.getElementById("tab-baux").hidden = name !== "baux";
    document.body.classList.toggle("wide", name === "topologie");
}

window.addEventListener("hashchange", () => showTab(currentTab()));
document.getElementById("topo-search").addEventListener("input", e => { topo.search = e.target.value.trim().toLowerCase(); render(); });
document.querySelectorAll(".filter").forEach(b => b.addEventListener("click", () => {
    topo.filter = b.dataset.filter;
    document.querySelectorAll(".filter").forEach(x => x.classList.toggle("on", x === b));
    render();
}));
setInterval(render, 5000);   // durees ("hors ligne depuis...") a jour entre deux evenements
showTab(currentTab());
connect();

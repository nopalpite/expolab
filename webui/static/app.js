const fleetBody = document.getElementById("fleet-body");
const createForm = document.getElementById("create-form");
const createBtn = document.getElementById("create-btn");
const createStatus = document.getElementById("create-status");

function statusClass(status) {
    const s = (status || "").toLowerCase();
    if (s === "running") return "badge badge-ok";
    if (s === "absent") return "badge badge-muted";
    return "badge badge-pending";
}

function renderFleet(fleet) {
    if (!fleet.length) {
        fleetBody.innerHTML = '<tr><td colspan="7" class="empty">Aucun faux Pi pour l\'instant.</td></tr>';
        return;
    }
    fleetBody.innerHTML = fleet.map((pi) => {
        const live = pi.live || { status: "Absent", ips: [] };
        const ip = live.ips && live.ips.length ? live.ips[0] : "-";
        const running = (live.status || "").toLowerCase() === "running";
        const powerLabel = running ? "Arreter" : "Demarrer";
        const powerAction = running ? "stop" : "start";
        return `
            <tr>
                <td data-label="Nom">${pi.name}</td>
                <td data-label="Statut"><span class="${statusClass(live.status)}">${live.status}</span></td>
                <td data-label="IP">${ip}</td>
                <td data-label="Role">${pi.role}</td>
                <td data-label="Identifiants"><code>${pi.username} / ${pi.password}</code></td>
                <td data-label="MAC"><code>${pi.mac}</code></td>
                <td data-label="" class="actions">
                    <button class="secondary" data-name="${pi.name}" data-action="${powerAction}">${powerLabel}</button>
                    <button class="danger" data-name="${pi.name}">Supprimer</button>
                </td>
            </tr>
        `;
    }).join("");

    fleetBody.querySelectorAll("button.danger").forEach((btn) => {
        btn.addEventListener("click", () => deletePi(btn.dataset.name));
    });
    fleetBody.querySelectorAll("button.secondary").forEach((btn) => {
        btn.addEventListener("click", () => powerPi(btn.dataset.name, btn.dataset.action));
    });
}

async function refreshFleet() {
    try {
        const res = await fetch("/api/fleet");
        const data = await res.json();
        renderFleet(data.fleet || []);
        renderTopology(data.fleet || []);
    } catch (err) {
        fleetBody.innerHTML = `<tr><td colspan="7" class="empty">Erreur de chargement : ${err}</td></tr>`;
    }
}

const SVG_NS = "http://www.w3.org/2000/svg";
const XLINK_NS = "http://www.w3.org/1999/xlink";

function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
}

function topoStatusKey(status) {
    const s = (status || "").toLowerCase();
    if (s === "running") return "running";
    if (s === "absent") return "muted";
    return "pending";
}

function topoDotClass(key) {
    if (key === "running") return "topo-dot-ok";
    if (key === "muted") return "topo-dot-muted";
    return "topo-dot-pending";
}

// Vue topologie : le serveur d'expo (hote, 10.42.0.1) au centre en haut,
// chaque faux Pi de la flotte en dessous - reconstruite a chaque
// rafraichissement (les IP/statuts changent), pas de diff DOM incrementale
// puisque la flotte entiere est deja re-fetchee toutes les 5s.
function renderTopology(fleet) {
    const svg = document.getElementById("topology-svg");
    svg.innerHTML = "";

    if (!fleet.length) {
        svg.setAttribute("viewBox", "0 0 600 70");
        const t = svgEl("text", { x: 300, y: 38, "text-anchor": "middle", class: "topo-sub" });
        t.textContent = "Aucun faux Pi pour l'instant.";
        svg.appendChild(t);
        return;
    }

    const NODE_W = 150, NODE_H = 56, GAP = 24;
    const width = Math.max(fleet.length * (NODE_W + GAP) + GAP, 300);
    const hubW = 200, hubH = 56;
    const hubX = (width - hubW) / 2, hubY = 16;
    const childY = 140;
    const height = childY + NODE_H + 20;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

    // Lignes + points animes d'abord (arriere-plan), hub et noeuds ensuite
    // (premier plan) - l'ordre des elements SVG determine l'empilement.
    fleet.forEach((pi, i) => {
        const cx = GAP + i * (NODE_W + GAP);
        const key = topoStatusKey((pi.live || {}).status);
        const hubBX = hubX + hubW / 2, hubBY = hubY + hubH;
        const childTX = cx + NODE_W / 2, childTY = childY;
        const pathId = `topo-line-${i}`;

        // Ligne droite depuis le MEME point du hub vers chaque noeud : des
        // rayons partis d'un point unique ne peuvent jamais se croiser,
        // quel que soit le nombre de noeuds - contrairement a des courbes
        // qui partagent toutes la meme hauteur mediane (celles des noeuds
        // extremes traversaient alors visuellement celles du milieu des
        // que la flotte depassait 4 machines).
        svg.appendChild(svgEl("path", {
            id: pathId,
            class: "topo-line",
            d: `M ${hubBX} ${hubBY} L ${childTX} ${childTY}`,
        }));

        if (key === "running") {
            const dot = svgEl("circle", { r: 3.5, class: "topo-dot-ok" });
            const anim = svgEl("animateMotion", { dur: "2.2s", begin: `${i * 0.3}s`, repeatCount: "indefinite" });
            const mpath = svgEl("mpath", {});
            mpath.setAttributeNS(XLINK_NS, "href", `#${pathId}`);
            anim.appendChild(mpath);
            dot.appendChild(anim);
            svg.appendChild(dot);
        }
    });

    const hub = svgEl("g", { class: "topo-hub" });
    hub.appendChild(svgEl("rect", { x: hubX, y: hubY, width: hubW, height: hubH, rx: 10 }));
    const hubTitle = svgEl("text", { x: hubX + hubW / 2, y: hubY + 24, "text-anchor": "middle", class: "topo-title" });
    hubTitle.textContent = "serveur d'expo";
    const hubSub = svgEl("text", { x: hubX + hubW / 2, y: hubY + 42, "text-anchor": "middle", class: "topo-sub" });
    hubSub.textContent = "10.42.0.1";
    hub.appendChild(hubTitle);
    hub.appendChild(hubSub);
    svg.appendChild(hub);

    fleet.forEach((pi, i) => {
        const cx = GAP + i * (NODE_W + GAP);
        const live = pi.live || { status: "Absent", ips: [] };
        const key = topoStatusKey(live.status);
        const ip = live.ips && live.ips.length ? live.ips[0] : "-";

        const node = svgEl("g", { class: `topo-node status-${key}` });
        node.appendChild(svgEl("rect", { x: cx, y: childY, width: NODE_W, height: NODE_H, rx: 10 }));
        node.appendChild(svgEl("circle", { cx: cx + 16, cy: childY + 20, r: 4, class: topoDotClass(key) }));
        const title = svgEl("text", { x: cx + 28, y: childY + 24, class: "topo-title" });
        title.textContent = pi.name;
        node.appendChild(title);
        const sub = svgEl("text", { x: cx + NODE_W / 2, y: childY + 42, "text-anchor": "middle", class: "topo-sub" });
        sub.textContent = ip;
        node.appendChild(sub);
        svg.appendChild(node);
    });
}

async function pollJob(jobId, onDone) {
    const res = await fetch(`/api/jobs/${jobId}`);
    const job = await res.json();
    if (job.status === "running") {
        setTimeout(() => pollJob(jobId, onDone), 3000);
    } else {
        onDone(job);
    }
}

createForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    createBtn.disabled = true;
    createStatus.hidden = false;
    createStatus.className = "status";
    createStatus.textContent = "Creation en cours (1 a 4 min)...";

    const payload = Object.fromEntries(new FormData(createForm).entries());

    try {
        const res = await fetch("/api/fleet", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.error || "erreur inconnue");
        }
        refreshFleet();
        pollJob(data.job_id, (job) => {
            createBtn.disabled = false;
            if (job.status === "done") {
                createStatus.className = "status status-ok";
                createStatus.textContent = `${payload.name} pret.`;
                createForm.reset();
                document.getElementById("username").value = "pi";
                document.getElementById("password").value = "raspberry";
            } else {
                createStatus.className = "status status-error";
                createStatus.textContent = `Echec : ${job.log.slice(-400)}`;
            }
            refreshFleet();
        });
    } catch (err) {
        createBtn.disabled = false;
        createStatus.className = "status status-error";
        createStatus.textContent = `Erreur : ${err.message}`;
    }
});

async function powerPi(name, action) {
    try {
        const res = await fetch(`/api/fleet/${name}/${action}`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "erreur inconnue");
        refreshFleet();
        pollJob(data.job_id, () => refreshFleet());
    } catch (err) {
        alert(`Erreur : ${err.message}`);
    }
}

async function deletePi(name) {
    if (!confirm(`Supprimer ${name} ?`)) return;
    try {
        const res = await fetch(`/api/fleet/${name}`, { method: "DELETE" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "erreur inconnue");
        refreshFleet();
        pollJob(data.job_id, () => refreshFleet());
    } catch (err) {
        alert(`Erreur : ${err.message}`);
    }
}

refreshFleet();
setInterval(refreshFleet, 5000);

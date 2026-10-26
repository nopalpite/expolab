// Onglets Runs / Applications, et l'onglet Applications : programme (depot du mirroir) et
// version de chaque machine. Les textes venant de Bastion ou du mirroir sont echappes.

function esc(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// --- onglets -------------------------------------------------------------------------------
function showTab(name) {
    if (name !== "runs" && name !== "applications") name = "runs";
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("on", t.dataset.tab === name));
    document.querySelectorAll("[data-pane]").forEach(p => { p.hidden = p.dataset.pane !== name; });
    if (name === "applications") loadApps();
}
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

// --- onglet Applications -------------------------------------------------------------------
let appsData = null;
let appsTimer = null;

const STATE_BADGES = {
    none: ["badge-muted", "aucun programme"],
    uptodate: ["badge-ok", "a jour"],
    outdated: ["badge-warn", "a mettre a jour"],
    queued: ["badge-pending", "en attente de son tour"],
    running: ["badge-pending", "installation en cours"],
    failed: ["badge-error", "echec du dernier run"],
    missing: ["badge-error", "release introuvable"],
};

function stateBadge(row) {
    if (row.state === "none" && !row.kiosk) return '<span class="badge badge-muted">pas de tag kiosk</span>';
    const [cls, label] = STATE_BADGES[row.state] || ["badge-muted", row.state];
    const spin = row.state === "running" || row.state === "queued" ? '<span class="spin"></span>' : "";
    return `<span class="badge ${cls}">${spin}${label}</span>`;
}

function programOptions(row) {
    const names = appsData.programs.map(p => p.name);
    if (row.app && !names.includes(row.app)) names.unshift(row.app);   // programme sans release : garde la valeur
    return `<option value="">— aucun —</option>` +
        names.map(n => `<option value="${esc(n)}"${n === row.app ? " selected" : ""}>${esc(n)}</option>`).join("");
}

function versionOptions(row) {
    const prog = appsData.programs.find(p => p.name === row.app);
    if (!row.app || !prog) return "";
    const latest = prog.releases[0] ? ` (${prog.releases[0].tag})` : "";
    const current = row.version || "latest";
    return `<option value="latest"${current === "latest" ? " selected" : ""}>derniere${esc(latest)}</option>` +
        prog.releases.map(r => `<option value="${esc(r.tag)}"${r.tag === current ? " selected" : ""}>${esc(r.tag)}</option>`).join("");
}

function installedCell(inst) {
    if (!inst) return "—";
    const when = inst.at ? new Date(inst.at).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) : "";
    return `${esc(inst.version)}<small>${esc(inst.app)} · ${esc(when)}</small>`;
}

function actionCell(row) {
    const parts = [];
    if (row.run_id) parts.push(`<button type="button" class="secondary small" data-log="${esc(row.run_id)}">Voir le log</button>`);
    if (row.app && row.state !== "running" && row.state !== "queued" && row.state !== "missing") {
        const label = row.state === "uptodate" ? "Reinstaller" : row.state === "failed" ? "Reessayer" : `Installer ${row.wanted || ""}`;
        const cls = row.state === "uptodate" ? "secondary small" : "small";
        parts.push(`<button type="button" class="${cls}" data-install="${esc(row.name)}">${esc(label.trim())}</button>`);
    }
    return parts.join(" ");
}

function renderApps() {
    const body = document.getElementById("apps-body");
    const rows = appsData.machines.map(row => `
        <tr>
            <td class="machine" data-label="Machine"><b>${esc(row.short)}</b><span>${esc(row.site || "sans salle")} · tags : ${esc(row.tags.join(", ") || "aucun")}</span></td>
            <td data-label="Programme"><select data-app="${esc(row.name)}">${programOptions(row)}</select></td>
            <td data-label="Version"><select data-version="${esc(row.name)}"${row.app ? "" : " disabled"}>${versionOptions(row)}</select></td>
            <td class="installed" data-label="Installee">${installedCell(row.installed)}</td>
            <td data-label="Etat">${stateBadge(row)}</td>
            <td class="actions">${actionCell(row)}</td>
        </tr>`);
    const orphans = appsData.orphans.map(o => `
        <tr class="orphan">
            <td class="machine" data-label="Machine"><b>${esc(o.name.split(".")[0])}</b><span>n'existe plus dans Bastion</span></td>
            <td data-label="Programme">${esc(o.app || "—")}</td>
            <td data-label="Version">${esc(o.version || "—")}</td>
            <td class="installed" data-label="Installee">${installedCell(o.installed)}</td>
            <td data-label="Etat"><span class="badge badge-warn">machine inconnue</span></td>
            <td class="actions"><button type="button" class="secondary small" data-forget="${esc(o.name)}">Retirer</button></td>
        </tr>`);
    body.innerHTML = rows.concat(orphans).join("") || '<tr><td colspan="6" class="empty">Aucune machine dans Bastion</td></tr>';

    document.getElementById("programs-body").innerHTML = appsData.programs.length
        ? appsData.programs.map(p => {
            const last = p.releases[0];
            const date = last && last.published_at ? new Date(last.published_at).toLocaleDateString("fr-FR") : "";
            return `<tr><td><b>${esc(p.name)}</b></td><td>${esc(last ? last.tag : "—")} <small>· ${esc(date)}</small></td>
                    <td>${esc(p.releases.map(r => r.tag).join(", "))}</td><td>${p.machines}</td></tr>`;
        }).join("")
        : '<tr><td colspan="4" class="empty">Aucun depot du mirroir n\'a de release</td></tr>';
}

function showAppsError(message) {
    const el = document.getElementById("apps-error");
    el.hidden = !message;
    el.textContent = message || "";
}

async function appsRequest(url, options) {
    try {
        const res = await fetch(url, options);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { showAppsError(data.error || `Erreur ${res.status}`); return null; }
        showAppsError(data.mirror_error || "");
        return data;
    } catch (e) {
        showAppsError("Erreur reseau");
        return null;
    }
}

async function loadApps() {
    clearTimeout(appsTimer);
    const data = await appsRequest("/api/apps");
    if (data) { appsData = data; renderApps(); }
    // Rafraichi tant que l'onglet est visible : vite pendant une installation.
    const busy = appsData && appsData.machines.some(r => r.state === "running" || r.state === "queued");
    if (!document.querySelector('[data-pane="applications"]').hidden) {
        appsTimer = setTimeout(loadApps, busy ? 3000 : 20000);
    }
}

async function setApp(machine, app, version) {
    const data = await appsRequest(`/api/apps/${encodeURIComponent(machine)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ app, version }),
    });
    if (data) { appsData = data; renderApps(); }
    else loadApps();
}

document.getElementById("apps-body").addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.app !== undefined) setApp(t.dataset.app, t.value, "latest");
    else if (t.dataset.version !== undefined) {
        const row = appsData.machines.find(r => r.name === t.dataset.version);
        setApp(t.dataset.version, row ? row.app : "", t.value);
    }
});

document.getElementById("apps-body").addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    if (btn.dataset.log) {
        const row = appsData.machines.find(r => r.run_id === btn.dataset.log);
        viewLog(btn.dataset.log, row && (row.state === "running" || row.state === "queued"));
        return;
    }
    btn.disabled = true;
    if (btn.dataset.install) {
        await appsRequest(`/api/apps/${encodeURIComponent(btn.dataset.install)}/install`, { method: "POST" });
    } else if (btn.dataset.forget) {
        await appsRequest(`/api/apps/${encodeURIComponent(btn.dataset.forget)}`, { method: "DELETE" });
    }
    loadApps();
});

showTab(location.hash.slice(1));

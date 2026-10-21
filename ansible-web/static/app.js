async function loadSettings() {
    try {
        const res = await fetch("/api/settings");
        const data = await res.json();
        document.getElementById("mirror_name").value = data.mirror_name || "ansible";
    } catch (e) {
        // Formulaire reste sur son placeholder si le chargement echoue.
    }
}

document.getElementById("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("settings-save-btn");
    const status = document.getElementById("settings-status");
    btn.disabled = true;
    status.hidden = true;

    try {
        const res = await fetch("/api/settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mirror_name: document.getElementById("mirror_name").value.trim() }),
        });
        const data = await res.json();
        status.hidden = false;
        if (!res.ok) {
            status.className = "status status-error";
            status.textContent = data.error || "Erreur";
        } else {
            status.className = "status status-ok";
            status.textContent = "Mirroir enregistre - prochain chargement des tags = nouveau clone.";
            loadTags();
        }
    } catch (e) {
        status.hidden = false;
        status.className = "status status-error";
        status.textContent = "Erreur reseau";
    } finally {
        btn.disabled = false;
    }
});

function statusBadge(status) {
    // empty : ansible-playbook a reussi sans traiter aucune machine (rien n'a ete fait).
    // queued : en file, attend que le run en cours se termine.
    const map = { queued: "badge-muted", running: "badge-pending", ok: "badge-ok", failed: "badge-error",
                  empty: "badge-warn", cancelled: "badge-muted" };
    const label = { queued: "en attente", running: "en cours", ok: "ok", failed: "echec",
                    empty: "aucune machine traitee", cancelled: "annule" };
    return `<span class="badge ${map[status] || "badge-muted"}">${label[status] || status}</span>`;
}

function roleBadge(t) {
    if (!t.has_role) return '<span class="badge badge-muted">role absent</span>';
    // Role present mais pas dans order.yaml : site.yml n'a aucun play pour lui, un run ne ferait rien.
    if (t.in_order === false) return '<span class="badge badge-warn" title="Ajouter le role a order.yaml (et regenerer site.yml) dans bastion-ansible">absent de order.yaml</span>';
    return '<span class="badge badge-ok">role present</span>';
}

async function loadTags() {
    const body = document.getElementById("tags-body");
    const select = document.getElementById("run_limit");
    body.innerHTML = '<tr><td colspan="3" class="empty">Chargement...</td></tr>';
    try {
        const res = await fetch("/api/tags");
        const data = await res.json();
        if (!res.ok) {
            body.innerHTML = `<tr><td colspan="3" class="status-error">${data.error || "Erreur"}</td></tr>`;
            return;
        }
        const tags = data.tags || [];
        const machines = data.machines || [];
        // La valeur d'un tag est son nom de groupe Ansible (normalise) :
        // c'est ce que `--limit` reconnait, pas necessairement le tag brut.
        select.innerHTML = '<option value="">Tout le parc</option>' +
            (tags.length ? `<optgroup label="Tags">${tags.map(t => `<option value="${t.role}">${t.name}</option>`).join("")}</optgroup>` : "") +
            (machines.length ? `<optgroup label="Machines">${machines.map(m => `<option value="${m.name}">${m.name}${m.host ? ` (${m.host})` : ""}</option>`).join("")}</optgroup>` : "");
        if (!tags.length) {
            body.innerHTML = '<tr><td colspan="3" class="empty">Aucun tag dans Bastion</td></tr>';
            return;
        }
        body.innerHTML = tags.map(t => `
            <tr>
                <td data-label="Tag">${t.name}</td>
                <td data-label="Role"><code>${t.role}</code></td>
                <td>${roleBadge(t)}</td>
            </tr>
        `).join("");
    } catch (e) {
        body.innerHTML = '<tr><td colspan="3" class="status-error">Erreur reseau</td></tr>';
    }
}

document.getElementById("refresh-btn").addEventListener("click", loadTags);

document.getElementById("run-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("run-btn");
    const status = document.getElementById("run-status");
    btn.disabled = true;
    status.hidden = true;

    const limit = document.getElementById("run_limit").value || null;
    try {
        const res = await fetch("/api/runs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ limit }),
        });
        const data = await res.json();
        status.hidden = false;
        if (!res.ok) {
            status.className = "status status-error";
            status.textContent = data.error || "Erreur";
        } else {
            status.className = "status status-ok";
            status.textContent = data.queued
                ? `Run '${data.id}' ajoute a la file : il demarrera apres le(s) run(s) en cours.`
                : `Run '${data.id}' lance.`;
            viewLog(data.id, true);   // le log du run qu'on vient de lancer s'affiche et se suit
            loadRuns();
        }
    } catch (e) {
        status.hidden = false;
        status.className = "status status-error";
        status.textContent = "Erreur reseau";
    } finally {
        btn.disabled = false;
    }
});

// Suivi en direct : l'historique est relu toutes les POLL_RUNNING_MS tant qu'un run tourne
// (POLL_IDLE_MS sinon, pour voir un run lance depuis un autre onglet), et le log affiche
// est relu tant que son run tourne, plus une derniere fois a sa fin.
const POLL_RUNNING_MS = 2000;
const POLL_IDLE_MS = 15000;
let viewedRunId = null;
let viewedRunRunning = false;
let pollTimer = null;

async function refreshLog(runId, initial) {
    const content = document.getElementById("log-content");
    // Reste colle en bas si on y etait deja (on suit la fin du run), sinon ne bouge pas.
    const atBottom = content.scrollHeight - content.scrollTop - content.clientHeight < 40;
    try {
        const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/log`);
        const data = await res.json();
        if (runId !== viewedRunId) return;   // un autre log a ete ouvert entre-temps
        const text = res.ok ? data.log : (data.error || "Erreur");
        if (content.textContent !== text) {
            content.textContent = text;
            if (initial || atBottom) content.scrollTop = content.scrollHeight;
        }
    } catch (e) {
        if (initial) content.textContent = "Erreur reseau";
    }
}

async function viewLog(runId, running) {
    const panel = document.getElementById("log-panel");
    const content = document.getElementById("log-content");
    viewedRunId = runId;
    viewedRunRunning = !!running;
    document.getElementById("log-title").textContent = `Log ${runId}`;
    panel.hidden = false;
    content.textContent = "Chargement...";
    panel.scrollIntoView({ behavior: "smooth", block: "center" });
    await refreshLog(runId, true);
}

function schedulePoll(delay) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(loadRuns, delay);
}

async function loadRuns() {
    const body = document.getElementById("runs-body");
    let anyRunning = false;
    try {
        const res = await fetch("/api/runs");
        const data = await res.json();
        const runs = data.runs || [];
        anyRunning = runs.some(r => r.status === "running" || r.status === "queued");
        const viewed = runs.find(r => r.id === viewedRunId);
        if (viewed && (viewed.status === "running" || viewed.status === "queued" || viewedRunRunning)) {
            viewedRunRunning = viewed.status === "running" || viewed.status === "queued";
            refreshLog(viewed.id, false);   // derniere lecture comprise, quand il vient de finir
        }
        if (!runs.length) {
            body.innerHTML = '<tr><td colspan="4" class="empty">Aucun run</td></tr>';
            return;
        }
        body.innerHTML = runs.map(r => `
            <tr>
                <td data-label="Demarre">${new Date(r.started_at || r.queued_at).toLocaleString()}</td>
                <td data-label="Cible">${r.limit || "tout le parc"}</td>
                <td data-label="Statut">${statusBadge(r.status)}</td>
                <td class="actions">${r.status === "queued" ? `<button class="secondary" data-cancel-id="${r.id}">Annuler</button> ` : ""}<button class="secondary" data-run-id="${r.id}" data-running="${r.status === "running" || r.status === "queued" ? "1" : ""}">Voir le log</button></td>
            </tr>
        `).join("");
        body.querySelectorAll("button[data-run-id]").forEach(btn => {
            btn.addEventListener("click", () => viewLog(btn.dataset.runId, btn.dataset.running === "1"));
        });
        body.querySelectorAll("button[data-cancel-id]").forEach(btn => {
            btn.addEventListener("click", async () => {
                btn.disabled = true;
                const res = await fetch(`/api/runs/${encodeURIComponent(btn.dataset.cancelId)}/cancel`, { method: "POST" });
                if (!res.ok) alert((await res.json().catch(() => ({}))).error || "Erreur");
                loadRuns();
            });
        });
    } catch (e) {
        body.innerHTML = '<tr><td colspan="4" class="status-error">Erreur de chargement</td></tr>';
    } finally {
        schedulePoll(anyRunning ? POLL_RUNNING_MS : POLL_IDLE_MS);
    }
}

loadSettings();
loadTags();
loadRuns();

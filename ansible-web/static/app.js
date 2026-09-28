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
    const map = { running: "badge-pending", ok: "badge-ok", failed: "badge-error" };
    const label = { running: "en cours", ok: "ok", failed: "echec" };
    return `<span class="badge ${map[status] || "badge-muted"}">${label[status] || status}</span>`;
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
        select.innerHTML = '<option value="">Tout le parc</option>' +
            tags.map(t => `<option value="${t.name}">${t.name}</option>`).join("");
        if (!tags.length) {
            body.innerHTML = '<tr><td colspan="3" class="empty">Aucun tag dans Bastion</td></tr>';
            return;
        }
        body.innerHTML = tags.map(t => `
            <tr>
                <td data-label="Tag">${t.name}</td>
                <td data-label="Role"><code>${t.role}</code></td>
                <td>${t.has_role ? '<span class="badge badge-ok">role present</span>' : '<span class="badge badge-muted">role absent</span>'}</td>
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
            status.textContent = `Run '${data.id}' lance.`;
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

async function viewLog(runId) {
    const panel = document.getElementById("log-panel");
    const content = document.getElementById("log-content");
    panel.hidden = false;
    content.textContent = "Chargement...";
    panel.scrollIntoView({ behavior: "smooth", block: "center" });
    try {
        const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/log`);
        const data = await res.json();
        content.textContent = res.ok ? data.log : (data.error || "Erreur");
    } catch (e) {
        content.textContent = "Erreur reseau";
    }
}

async function loadRuns() {
    const body = document.getElementById("runs-body");
    try {
        const res = await fetch("/api/runs");
        const data = await res.json();
        const runs = data.runs || [];
        if (!runs.length) {
            body.innerHTML = '<tr><td colspan="4" class="empty">Aucun run</td></tr>';
            return;
        }
        body.innerHTML = runs.map(r => `
            <tr>
                <td data-label="Demarre">${new Date(r.started_at).toLocaleString()}</td>
                <td data-label="Cible">${r.limit || "tout le parc"}</td>
                <td data-label="Statut">${statusBadge(r.status)}</td>
                <td class="actions"><button class="secondary" data-run-id="${r.id}">Voir le log</button></td>
            </tr>
        `).join("");
        body.querySelectorAll("button[data-run-id]").forEach(btn => {
            btn.addEventListener("click", () => viewLog(btn.dataset.runId));
        });
    } catch (e) {
        body.innerHTML = '<tr><td colspan="4" class="status-error">Erreur de chargement</td></tr>';
    }
}

loadSettings();
loadTags();
loadRuns();

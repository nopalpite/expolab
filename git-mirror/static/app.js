let editingName = null;
let mirrorsByName = {};

function formatDate(iso) {
    if (!iso) return "jamais";
    const d = new Date(iso);
    return d.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
}

function statusBadge(m) {
    if (m.up_to_date === null) {
        return '<span class="badge badge-muted">injoignable</span>';
    }
    if (m.up_to_date) {
        return '<span class="badge badge-ok">a jour</span>';
    }
    return '<span class="badge badge-pending">en retard</span>';
}

function intervalLabel(minutes) {
    const labels = { 0: "Manuel", 60: "1h", 360: "6h", 1440: "24h" };
    return labels[minutes] || "Manuel";
}

function escapeHtml(str) {
    // remote_url vient de l'utilisateur et n'est pas restreint a un jeu
    // de caracteres sur (contrairement a name, valide par NAME_RE cote
    // serveur) - echappe avant insertion dans innerHTML/attributs pour
    // eviter une injection HTML via une URL distante malveillante.
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

async function copyCloneUrl(btn, url) {
    try {
        await navigator.clipboard.writeText(url);
        const original = btn.textContent;
        btn.textContent = "Copie !";
        setTimeout(() => { btn.textContent = original; }, 1500);
    } catch (e) {
        prompt("Copier manuellement :", url);
    }
}

async function loadMirrors() {
    const list = document.getElementById("mirrors-list");
    try {
        const res = await fetch("/api/mirrors");
        const data = await res.json();
        const mirrors = data.mirrors || [];
        mirrorsByName = Object.fromEntries(mirrors.map(m => [m.name, m]));
        if (!mirrors.length) {
            list.innerHTML = '<p class="empty">Aucun mirroir</p>';
            return;
        }
        list.innerHTML = mirrors.map(m => `
            <div class="mirror-card">
                <div class="mirror-card-header">
                    <div class="mirror-card-title">
                        <span>${escapeHtml(m.name)}</span>
                        ${statusBadge(m)}
                    </div>
                    <div class="mirror-card-actions">
                        <button class="secondary" data-sync-name="${escapeHtml(m.name)}">Sync maintenant</button>
                        <button class="secondary" data-edit-name="${escapeHtml(m.name)}">Modifier</button>
                        <button class="danger" data-name="${escapeHtml(m.name)}">Retirer</button>
                    </div>
                </div>
                <div class="mirror-card-row">
                    <span class="field-label">Source</span>
                    <code title="${escapeHtml(m.remote_url)}">${escapeHtml(m.remote_url)}</code>
                </div>
                <div class="mirror-card-row">
                    <span class="field-label">Cloner</span>
                    <code title="${escapeHtml(m.clone_url)}">${escapeHtml(m.clone_url)}</code>
                    <button type="button" class="secondary" data-copy-url="${escapeHtml(m.clone_url)}">Copier</button>
                </div>
                <div class="mirror-card-meta">
                    <span>Derniere sync : ${formatDate(m.last_synced)}</span>
                    <span>Auto : ${intervalLabel(m.interval_minutes)}</span>
                </div>
            </div>
        `).join("");
        list.querySelectorAll("button[data-copy-url]").forEach(btn => {
            btn.addEventListener("click", () => copyCloneUrl(btn, btn.dataset.copyUrl));
        });
        list.querySelectorAll("button[data-sync-name]").forEach(btn => {
            btn.addEventListener("click", () => syncMirror(btn.dataset.syncName));
        });
        list.querySelectorAll("button[data-edit-name]").forEach(btn => {
            btn.addEventListener("click", () => startEditMirror(btn.dataset.editName));
        });
        list.querySelectorAll("button.danger").forEach(btn => {
            btn.addEventListener("click", () => deleteMirror(btn.dataset.name));
        });
    } catch (e) {
        list.innerHTML = '<p class="status-error">Erreur de chargement</p>';
    }
}

async function syncMirror(name) {
    const res = await fetch(`/api/mirrors/${encodeURIComponent(name)}/sync`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
        alert(data.error || "Erreur");
        return;
    }
    loadMirrors();
}

async function deleteMirror(name) {
    if (!confirm(`Retirer le mirroir '${name}' (supprime aussi le clone local) ?`)) return;
    const res = await fetch(`/api/mirrors/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) {
        alert(data.error || "Erreur");
        return;
    }
    if (editingName === name) stopEditMirror();
    loadMirrors();
}

function startEditMirror(name) {
    const m = mirrorsByName[name];
    if (!m) return;
    editingName = name;
    const nameField = document.getElementById("name");
    nameField.value = name;
    nameField.disabled = true;
    document.getElementById("remote_url").value = m.remote_url;
    document.getElementById("interval_minutes").value = m.interval_minutes;
    document.getElementById("create-btn").textContent = "Modifier";
    document.getElementById("create-cancel").hidden = false;
    document.getElementById("create-status").hidden = true;
}

function stopEditMirror() {
    editingName = null;
    const nameField = document.getElementById("name");
    nameField.disabled = false;
    document.getElementById("create-form").reset();
    document.getElementById("create-btn").textContent = "Ajouter";
    document.getElementById("create-cancel").hidden = true;
}

document.getElementById("create-cancel").addEventListener("click", stopEditMirror);

document.getElementById("create-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("create-btn");
    const status = document.getElementById("create-status");
    btn.disabled = true;
    status.hidden = true;

    const name = document.getElementById("name").value.trim().toLowerCase();
    const remote_url = document.getElementById("remote_url").value.trim();
    const interval_minutes = parseInt(document.getElementById("interval_minutes").value, 10);

    try {
        const res = editingName
            ? await fetch(`/api/mirrors/${encodeURIComponent(editingName)}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ remote_url, interval_minutes }),
            })
            : await fetch("/api/mirrors", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name, remote_url, interval_minutes }),
            });
        const data = await res.json();
        status.hidden = false;
        if (!res.ok) {
            status.className = "status status-error";
            status.textContent = data.error || "Erreur";
        } else {
            status.className = "status status-ok";
            status.textContent = editingName ? `Mirroir '${name}' modifie.` : `Mirroir '${name}' ajoute (clone en cours termine).`;
            stopEditMirror();
            loadMirrors();
        }
    } catch (e) {
        status.hidden = false;
        status.className = "status status-error";
        status.textContent = "Erreur reseau";
    } finally {
        btn.disabled = false;
    }
});

loadMirrors();

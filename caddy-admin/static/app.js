let editingName = null;

async function loadServices() {
    const body = document.getElementById("services-body");
    try {
        const res = await fetch("/api/services");
        const data = await res.json();
        const services = data.services || [];
        if (!services.length) {
            body.innerHTML = '<tr><td colspan="4" class="empty">Aucun service</td></tr>';
            return;
        }
        body.innerHTML = services.map(s => `
            <tr>
                <td data-label="Nom">${s.name}</td>
                <td data-label="URL"><code>${s.name}.web.expolab.lan</code></td>
                <td data-label="Port backend">${s.backend_port}</td>
                <td class="actions">
                    <button class="secondary" data-edit-name="${s.name}" data-edit-port="${s.backend_port}">Modifier</button>
                    <button class="danger" data-name="${s.name}">Retirer</button>
                </td>
            </tr>
        `).join("");
        body.querySelectorAll("button.danger").forEach(btn => {
            btn.addEventListener("click", () => deleteService(btn.dataset.name));
        });
        body.querySelectorAll("button[data-edit-name]").forEach(btn => {
            btn.addEventListener("click", () => startEditService(btn.dataset.editName, btn.dataset.editPort));
        });
    } catch (e) {
        body.innerHTML = '<tr><td colspan="4" class="status-error">Erreur de chargement</td></tr>';
    }
}

async function deleteService(name) {
    if (!confirm(`Retirer '${name}' du reverse-proxy ?`)) return;
    const res = await fetch(`/api/services/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) {
        alert(data.error || "Erreur");
        return;
    }
    if (editingName === name) stopEditService();
    loadServices();
}

function startEditService(name, port) {
    editingName = name;
    const nameField = document.getElementById("name");
    nameField.value = name;
    nameField.disabled = true;
    document.getElementById("backend_port").value = port;
    document.getElementById("create-btn").textContent = "Modifier";
    document.getElementById("create-cancel").hidden = false;
    document.getElementById("create-status").hidden = true;
}

function stopEditService() {
    editingName = null;
    const nameField = document.getElementById("name");
    nameField.disabled = false;
    document.getElementById("create-form").reset();
    document.getElementById("create-btn").textContent = "Ajouter";
    document.getElementById("create-cancel").hidden = true;
}

document.getElementById("create-cancel").addEventListener("click", stopEditService);

document.getElementById("create-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("create-btn");
    const status = document.getElementById("create-status");
    btn.disabled = true;
    status.hidden = true;

    const name = document.getElementById("name").value.trim().toLowerCase();
    const backend_port = parseInt(document.getElementById("backend_port").value, 10);

    try {
        const res = editingName
            ? await fetch(`/api/services/${encodeURIComponent(editingName)}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ backend_port }),
            })
            : await fetch("/api/services", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name, backend_port }),
            });
        const data = await res.json();
        status.hidden = false;
        if (!res.ok) {
            status.className = "status status-error";
            status.textContent = data.error || "Erreur";
        } else {
            status.className = "status status-ok";
            status.textContent = editingName ? `Service '${name}' modifie et Caddy recharge.` : `Service '${name}' ajoute et Caddy recharge.`;
            stopEditService();
            loadServices();
        }
    } catch (e) {
        status.hidden = false;
        status.className = "status status-error";
        status.textContent = "Erreur reseau";
    } finally {
        btn.disabled = false;
    }
});

loadServices();

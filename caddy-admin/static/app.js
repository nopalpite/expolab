let editingName = null;
let servicesByName = {};
let usedPorts = {};

function computeUsedPorts(services) {
    const used = {};
    services.forEach(s => {
        used[s.backend_port] = s.name;
        (s.extra_routes || []).forEach(r => {
            used[r.backend_port] = `${s.name} (${r.path})`;
        });
    });
    return used;
}

function checkPortConflict() {
    const field = document.getElementById("backend_port");
    const hint = document.getElementById("used-ports-hint");
    const port = parseInt(field.value, 10);
    const owner = usedPorts[port];
    const conflict = owner && owner !== editingName;
    field.style.borderColor = conflict ? "var(--error)" : "";
    hint.textContent = conflict ? `Port deja utilise par '${owner}'` : "";
    return !conflict;
}

document.getElementById("backend_port").addEventListener("input", checkPortConflict);

function addRouteRow(path = "", port = "") {
    const list = document.getElementById("extra-routes-list");
    const row = document.createElement("div");
    row.className = "route-row";
    row.innerHTML = `
        <input type="text" class="route-path" placeholder="/vnc-ws/*" value="${path}">
        <input type="number" class="route-port" placeholder="6080" min="1" max="65535" value="${port}">
        <button type="button" class="danger">Retirer</button>
    `;
    row.querySelector("button").addEventListener("click", () => row.remove());
    list.appendChild(row);
}

function clearRouteRows() {
    document.getElementById("extra-routes-list").innerHTML = "";
}

function collectExtraRoutes() {
    return Array.from(document.querySelectorAll("#extra-routes-list .route-row"))
        .map(row => ({
            path: row.querySelector(".route-path").value.trim(),
            backend_port: parseInt(row.querySelector(".route-port").value, 10),
        }))
        .filter(r => r.path);
}

document.getElementById("add-route-btn").addEventListener("click", () => addRouteRow());

async function loadServices() {
    const body = document.getElementById("services-body");
    try {
        const res = await fetch("/api/services");
        const data = await res.json();
        const services = data.services || [];
        servicesByName = Object.fromEntries(services.map(s => [s.name, s]));
        usedPorts = computeUsedPorts(services);
        if (!services.length) {
            body.innerHTML = '<tr><td colspan="5" class="empty">Aucun service</td></tr>';
            return;
        }
        body.innerHTML = services.map(s => {
            const routes = s.extra_routes || [];
            const routesText = routes.length
                ? routes.map(r => `<code>${r.path}</code> &rarr; ${r.backend_port}`).join("<br>")
                : "-";
            return `
            <tr>
                <td data-label="Nom">${s.name}</td>
                <td data-label="URL"><code>${s.name}.web.expolab.lan</code></td>
                <td data-label="Port backend">${s.backend_port}</td>
                <td data-label="Routes additionnelles">${routesText}</td>
                <td class="actions">
                    <button class="secondary" data-edit-name="${s.name}">Modifier</button>
                    <button class="danger" data-name="${s.name}">Retirer</button>
                </td>
            </tr>
        `;
        }).join("");
        body.querySelectorAll("button.danger").forEach(btn => {
            btn.addEventListener("click", () => deleteService(btn.dataset.name));
        });
        body.querySelectorAll("button[data-edit-name]").forEach(btn => {
            btn.addEventListener("click", () => startEditService(btn.dataset.editName));
        });
    } catch (e) {
        body.innerHTML = '<tr><td colspan="5" class="status-error">Erreur de chargement</td></tr>';
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
    loadServices().then(loadDiscoveries);
}

function startEditService(name) {
    const svc = servicesByName[name];
    if (!svc) return;
    editingName = name;
    const nameField = document.getElementById("name");
    nameField.value = name;
    nameField.disabled = true;
    document.getElementById("backend_port").value = svc.backend_port;
    clearRouteRows();
    (svc.extra_routes || []).forEach(r => addRouteRow(r.path, r.backend_port));
    document.getElementById("create-btn").textContent = "Modifier";
    document.getElementById("create-cancel").hidden = false;
    document.getElementById("create-status").hidden = true;
    checkPortConflict();
}

function stopEditService() {
    editingName = null;
    const nameField = document.getElementById("name");
    nameField.disabled = false;
    document.getElementById("create-form").reset();
    clearRouteRows();
    document.getElementById("create-btn").textContent = "Ajouter";
    document.getElementById("create-cancel").hidden = true;
    document.getElementById("backend_port").style.borderColor = "";
    document.getElementById("used-ports-hint").textContent = "";
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
    const extra_routes = collectExtraRoutes();

    try {
        const res = editingName
            ? await fetch(`/api/services/${encodeURIComponent(editingName)}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ backend_port, extra_routes }),
            })
            : await fetch("/api/services", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name, backend_port, extra_routes }),
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
            loadServices().then(loadDiscoveries);
        }
    } catch (e) {
        status.hidden = false;
        status.className = "status status-error";
        status.textContent = "Erreur reseau";
    } finally {
        btn.disabled = false;
    }
});

function suggestName(containerName) {
    let name = containerName.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/^-+/, "");
    if (!name || !/^[a-z]/.test(name)) name = "svc-" + name;
    return name.slice(0, 32).replace(/-+$/, "");
}

function proposeService(container, port) {
    stopEditService();
    document.getElementById("name").value = suggestName(container);
    document.getElementById("backend_port").value = port;
    checkPortConflict();
    document.getElementById("create-form").scrollIntoView({ behavior: "smooth", block: "center" });
}

async function loadDiscoveries() {
    const panel = document.getElementById("discover-panel");
    const body = document.getElementById("discover-body");
    try {
        const res = await fetch("/api/discover");
        const data = await res.json();
        const suggestions = data.suggestions || [];
        if (!suggestions.length) {
            panel.hidden = true;
            return;
        }
        panel.hidden = false;
        body.innerHTML = suggestions.map(s => `
            <tr>
                <td data-label="Conteneur">${s.container}</td>
                <td data-label="Port">${s.port}</td>
                <td class="actions"><button class="secondary" data-container="${s.container}" data-port="${s.port}">Proposer</button></td>
            </tr>
        `).join("");
        body.querySelectorAll("button[data-container]").forEach(btn => {
            btn.addEventListener("click", () => proposeService(btn.dataset.container, btn.dataset.port));
        });
    } catch (e) {
        panel.hidden = true;
    }
}

loadServices().then(loadDiscoveries);

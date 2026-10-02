let editingName = null;
let servicesByName = {};
let usedPorts = {};
let currentDomain = "web.expolab.lan";

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
            body.innerHTML = '<tr><td colspan="6" class="empty">Aucun service</td></tr>';
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
                <td data-label="URL"><code>${s.name}.${currentDomain}</code></td>
                <td data-label="Port backend">${s.backend_port}</td>
                <td data-label="Auth">${s.auth ? "oui" : "-"}</td>
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
        body.innerHTML = '<tr><td colspan="6" class="status-error">Erreur de chargement</td></tr>';
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
    document.getElementById("auth").checked = !!svc.auth;
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
    document.getElementById("auth").checked = true;
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
    const auth = document.getElementById("auth").checked;

    try {
        const res = editingName
            ? await fetch(`/api/services/${encodeURIComponent(editingName)}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ backend_port, extra_routes, auth }),
            })
            : await fetch("/api/services", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name, backend_port, extra_routes, auth }),
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
    // Tous nos propres conteneurs sont prefixes "expolab-" (docker ps),
    // mais leurs entrees Caddy utilisent des noms courts (dashboard,
    // dnsmasq, caddy...) - retire ce prefixe avant de suggerer, pour
    // rester coherent avec le reste du tableau plutot que de proposer
    // "expolab-machin" a chaque fois.
    let name = containerName.toLowerCase().replace(/^expolab-/, "");
    name = name.replace(/[^a-z0-9-]/g, "-").replace(/^-+/, "");
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

function updateTlsFieldsVisibility() {
    const signed = document.querySelector('input[name="tls_mode"]:checked').value === "signed";
    document.getElementById("tls-signed-fields").hidden = !signed;
}

document.querySelectorAll('input[name="tls_mode"]').forEach(radio => {
    radio.addEventListener("change", updateTlsFieldsVisibility);
});

async function loadTls() {
    try {
        const res = await fetch("/api/tls");
        const data = await res.json();
        document.querySelector(`input[name="tls_mode"][value="${data.TLS_MODE || "internal"}"]`).checked = true;
        document.getElementById("tls_signed_domain").value = data.TLS_SIGNED_DOMAIN || "";
        document.getElementById("ovh_endpoint").value = data.OVH_ENDPOINT || "ovh-eu";
        const placeholder = (set) => (set ? "•••• (deja configure, laisser vide pour garder)" : "");
        document.getElementById("ovh_application_key").placeholder = placeholder(data.ovh_application_key_set);
        document.getElementById("ovh_application_secret").placeholder = placeholder(data.ovh_application_secret_set);
        document.getElementById("ovh_consumer_key").placeholder = placeholder(data.ovh_consumer_key_set);
        updateTlsFieldsVisibility();
        currentDomain = data.TLS_MODE === "signed" && data.TLS_SIGNED_DOMAIN ? data.TLS_SIGNED_DOMAIN : "web.expolab.lan";
        document.getElementById("current-domain").textContent = currentDomain;
    } catch (e) {
        // Formulaire reste sur ses valeurs par defaut (mode internal) si
        // le chargement echoue - non bloquant pour le reste de la page.
    }
}

document.getElementById("tls-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("tls-save-btn");
    const status = document.getElementById("tls-status");
    btn.disabled = true;
    status.hidden = true;

    const body = {
        tls_mode: document.querySelector('input[name="tls_mode"]:checked').value,
        tls_signed_domain: document.getElementById("tls_signed_domain").value.trim(),
        ovh_endpoint: document.getElementById("ovh_endpoint").value,
        ovh_application_key: document.getElementById("ovh_application_key").value.trim(),
        ovh_application_secret: document.getElementById("ovh_application_secret").value.trim(),
        ovh_consumer_key: document.getElementById("ovh_consumer_key").value.trim(),
    };

    try {
        const res = await fetch("/api/tls", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        status.hidden = false;
        if (!res.ok) {
            status.className = "status status-error";
            status.textContent = data.error || "Erreur";
        } else {
            status.className = "status status-ok";
            status.textContent = "Configuration TLS enregistree, services redeployes.";
            document.getElementById("ovh_application_key").value = "";
            document.getElementById("ovh_application_secret").value = "";
            document.getElementById("ovh_consumer_key").value = "";
            loadTls().then(loadServices);
        }
    } catch (e) {
        // Attendu ici (pas forcement un echec) : cette page est
        // elle-meme servie via Caddy, que cet enregistrement redemarre -
        // la sauvegarde a deja eu lieu avant ce redemarrage, cote
        // serveur (voir api_tls_update). Rien a faire cote utilisateur a
        // part recharger la page dans quelques secondes.
        status.hidden = false;
        status.className = "status status-ok";
        status.textContent = "Enregistre - Caddy redemarre (coupure normale de quelques secondes), recharge la page pour verifier.";
    } finally {
        btn.disabled = false;
    }
});

loadTls().then(() => loadServices().then(loadDiscoveries));

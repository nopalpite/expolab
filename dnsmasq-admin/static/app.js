async function loadLeases() {
    const body = document.getElementById("leases-body");
    try {
        const res = await fetch("/api/leases");
        const data = await res.json();
        if (!data.leases.length) {
            body.innerHTML = '<tr><td colspan="4" class="empty">Aucun bail actif</td></tr>';
            return;
        }
        body.innerHTML = data.leases.map(l => `
            <tr>
                <td data-label="Hostname">${l.hostname}</td>
                <td data-label="IP"><code>${l.ip}</code></td>
                <td data-label="MAC"><code>${l.mac}</code></td>
                <td data-label="Expire dans">${l.expires_in_min} min</td>
            </tr>
        `).join("");
    } catch (e) {
        body.innerHTML = '<tr><td colspan="4" class="status-error">Erreur de chargement</td></tr>';
    }
}

async function loadReservations() {
    const body = document.getElementById("reservations-body");
    try {
        const res = await fetch("/api/reservations");
        const data = await res.json();
        if (!data.reservations.length) {
            body.innerHTML = '<tr><td colspan="4" class="empty">Aucune reservation</td></tr>';
            return;
        }
        body.innerHTML = data.reservations.map(r => `
            <tr>
                <td data-label="MAC"><code>${r.mac}</code></td>
                <td data-label="IP"><code>${r.ip}</code></td>
                <td data-label="Hostname">${r.hostname || "-"}</td>
                <td class="actions"><button class="danger" data-mac="${r.mac}">Retirer</button></td>
            </tr>
        `).join("");
        body.querySelectorAll("button.danger").forEach(btn => {
            btn.addEventListener("click", () => deleteReservation(btn.dataset.mac));
        });
    } catch (e) {
        body.innerHTML = '<tr><td colspan="4" class="status-error">Erreur de chargement</td></tr>';
    }
}

async function deleteReservation(mac) {
    if (!confirm(`Retirer la reservation pour ${mac} ?`)) return;
    const res = await fetch(`/api/reservations/${encodeURIComponent(mac)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) { alert(data.error || "Erreur"); return; }
    loadReservations();
}

document.getElementById("reservation-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("reservation-btn");
    const status = document.getElementById("reservation-status");
    btn.disabled = true;
    status.hidden = true;

    const mac = document.getElementById("r-mac").value.trim().toLowerCase();
    const ip = document.getElementById("r-ip").value.trim();
    const hostname = document.getElementById("r-hostname").value.trim().toLowerCase();

    try {
        const res = await fetch("/api/reservations", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mac, ip, hostname }),
        });
        const data = await res.json();
        status.hidden = false;
        if (!res.ok) {
            status.className = "status status-error";
            status.textContent = data.error || "Erreur";
        } else {
            status.className = "status status-ok";
            status.textContent = `Reservation ajoutee pour ${mac}.`;
            document.getElementById("reservation-form").reset();
            loadReservations();
        }
    } catch (e) {
        status.hidden = false;
        status.className = "status status-error";
        status.textContent = "Erreur reseau";
    } finally {
        btn.disabled = false;
    }
});

async function loadDnsRecords() {
    const body = document.getElementById("dns-body");
    try {
        const res = await fetch("/api/dns-records");
        const data = await res.json();
        if (!data.records.length) {
            body.innerHTML = '<tr><td colspan="3" class="empty">Aucun enregistrement</td></tr>';
            return;
        }
        body.innerHTML = data.records.map(r => `
            <tr>
                <td data-label="Hostname">${r.hostname}</td>
                <td data-label="IP"><code>${r.ip}</code></td>
                <td class="actions"><button class="danger" data-hostname="${r.hostname}">Retirer</button></td>
            </tr>
        `).join("");
        body.querySelectorAll("button.danger").forEach(btn => {
            btn.addEventListener("click", () => deleteDnsRecord(btn.dataset.hostname));
        });
    } catch (e) {
        body.innerHTML = '<tr><td colspan="3" class="status-error">Erreur de chargement</td></tr>';
    }
}

async function deleteDnsRecord(hostname) {
    if (!confirm(`Retirer l'enregistrement '${hostname}' ?`)) return;
    const res = await fetch(`/api/dns-records/${encodeURIComponent(hostname)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) { alert(data.error || "Erreur"); return; }
    loadDnsRecords();
}

document.getElementById("dns-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("dns-btn");
    const status = document.getElementById("dns-status");
    btn.disabled = true;
    status.hidden = true;

    const hostname = document.getElementById("d-hostname").value.trim().toLowerCase();
    const ip = document.getElementById("d-ip").value.trim();

    try {
        const res = await fetch("/api/dns-records", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ hostname, ip }),
        });
        const data = await res.json();
        status.hidden = false;
        if (!res.ok) {
            status.className = "status status-error";
            status.textContent = data.error || "Erreur";
        } else {
            status.className = "status status-ok";
            status.textContent = `Enregistrement '${hostname}' ajoute.`;
            document.getElementById("dns-form").reset();
            loadDnsRecords();
        }
    } catch (e) {
        status.hidden = false;
        status.className = "status status-error";
        status.textContent = "Erreur reseau";
    } finally {
        btn.disabled = false;
    }
});

loadLeases();
loadReservations();
loadDnsRecords();
setInterval(loadLeases, 15000);

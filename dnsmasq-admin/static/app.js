async function loadLeases() {
    const body = document.getElementById("leases-body");
    try {
        const res = await fetch("/api/leases");
        const data = await res.json();
        if (!data.leases.length) {
            body.innerHTML = '<tr><td colspan="5" class="empty">Aucun bail actif</td></tr>';
            return;
        }
        body.innerHTML = data.leases.map(l => `
            <tr>
                <td data-label="Hostname">${l.hostname}</td>
                <td data-label="IP"><code>${l.ip}</code></td>
                <td data-label="MAC"><code>${l.mac}</code></td>
                <td data-label="Expire dans">${l.expires_in_min} min</td>
                <td class="actions">
                    <button class="secondary" data-lease-mac="${l.mac}" data-lease-ip="${l.ip}" data-lease-hostname="${l.hostname === '(inconnu)' ? '' : l.hostname}">Reserver</button>
                </td>
            </tr>
        `).join("");
        body.querySelectorAll("button[data-lease-mac]").forEach(btn => {
            btn.addEventListener("click", () => startReservationFromLease(btn.dataset.leaseMac, btn.dataset.leaseIp, btn.dataset.leaseHostname));
        });
    } catch (e) {
        body.innerHTML = '<tr><td colspan="5" class="status-error">Erreur de chargement</td></tr>';
    }
}

let editingMac = null;

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
                <td class="actions">
                    <button class="secondary" data-edit-mac="${r.mac}" data-edit-ip="${r.ip}" data-edit-hostname="${r.hostname || ""}">Modifier</button>
                    <button class="danger" data-mac="${r.mac}">Retirer</button>
                </td>
            </tr>
        `).join("");
        body.querySelectorAll("button.danger").forEach(btn => {
            btn.addEventListener("click", () => deleteReservation(btn.dataset.mac));
        });
        body.querySelectorAll("button[data-edit-mac]").forEach(btn => {
            btn.addEventListener("click", () => startEditReservation(btn.dataset.editMac, btn.dataset.editIp, btn.dataset.editHostname));
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
    if (editingMac === mac) stopEditReservation();
    loadReservations();
}

function startEditReservation(mac, ip, hostname) {
    editingMac = mac;
    const macField = document.getElementById("r-mac");
    macField.value = mac;
    macField.disabled = true;
    document.getElementById("r-ip").value = ip;
    document.getElementById("r-hostname").value = hostname;
    document.getElementById("reservation-btn").textContent = "Modifier";
    document.getElementById("reservation-cancel").hidden = false;
    document.getElementById("reservation-status").hidden = true;
}

function startReservationFromLease(mac, ip, hostname) {
    editingMac = null;
    const macField = document.getElementById("r-mac");
    macField.value = mac;
    macField.disabled = false;
    document.getElementById("r-ip").value = ip;
    document.getElementById("r-hostname").value = hostname;
    document.getElementById("reservation-btn").textContent = "Reserver";
    document.getElementById("reservation-cancel").hidden = true;
    document.getElementById("reservation-status").hidden = true;
    document.getElementById("reservation-form").scrollIntoView({ behavior: "smooth", block: "center" });
}

function stopEditReservation() {
    editingMac = null;
    const macField = document.getElementById("r-mac");
    macField.disabled = false;
    document.getElementById("reservation-form").reset();
    document.getElementById("reservation-btn").textContent = "Reserver";
    document.getElementById("reservation-cancel").hidden = true;
}

document.getElementById("reservation-cancel").addEventListener("click", stopEditReservation);

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
        const res = editingMac
            ? await fetch(`/api/reservations/${encodeURIComponent(editingMac)}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ ip, hostname }),
            })
            : await fetch("/api/reservations", {
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
            status.textContent = editingMac ? `Reservation ${mac} modifiee.` : `Reservation ajoutee pour ${mac}.`;
            stopEditReservation();
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

loadLeases();
loadReservations();
setInterval(loadLeases, 15000);

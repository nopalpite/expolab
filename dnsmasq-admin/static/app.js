// Une reservation est identifiee par sa MAC, ou par son NOM quand elle n'a
// pas de MAC (reservation "par nom" : suit la machine qui s'annonce sous ce
// nom, voir app.py). `id` = r.mac || r.hostname, fourni par l'API.
let reservationsByMac = {};
let reservationsByName = {};
let editingId = null;

function reservationFor(lease) {
    return reservationsByMac[lease.mac] || reservationsByName[lease.hostname];
}

async function loadLeases() {
    const body = document.getElementById("leases-body");
    try {
        const res = await fetch("/api/leases");
        const data = await res.json();
        if (!data.leases.length) {
            body.innerHTML = '<tr><td colspan="5" class="empty">Aucun bail actif</td></tr>';
            return;
        }
        body.innerHTML = data.leases.map(l => {
            const reserved = reservationFor(l);
            const btn = reserved
                ? `<button class="secondary" data-edit-id="${reserved.id}">Modifier</button>`
                : `<button class="secondary" data-lease-mac="${l.mac}" data-lease-ip="${l.ip}" data-lease-hostname="${l.hostname === '(inconnu)' ? '' : l.hostname}">Reserver</button>`;
            return `
            <tr>
                <td data-label="Hostname">${l.hostname}</td>
                <td data-label="IP"><code>${l.ip}</code></td>
                <td data-label="MAC"><code>${l.mac}</code></td>
                <td data-label="Expire dans">${l.expires_in_min} min</td>
                <td class="actions">
                    ${btn}
                    <button class="danger" data-release-mac="${l.mac}" data-release-ip="${l.ip}" title="Libere ce bail (carte morte a remplacer) - l'IP redevient disponible">Liberer</button>
                </td>
            </tr>
        `;
        }).join("");
        body.querySelectorAll("button[data-lease-mac]").forEach(btn => {
            btn.addEventListener("click", () => startReservationFromLease(btn.dataset.leaseMac, btn.dataset.leaseIp, btn.dataset.leaseHostname));
        });
        body.querySelectorAll("button[data-edit-id]").forEach(btn => {
            btn.addEventListener("click", () => startEditReservation(btn.dataset.editId));
        });
        body.querySelectorAll("button[data-release-mac]").forEach(btn => {
            btn.addEventListener("click", () => releaseLease(btn.dataset.releaseMac, btn.dataset.releaseIp));
        });
    } catch (e) {
        body.innerHTML = '<tr><td colspan="5" class="status-error">Erreur de chargement</td></tr>';
    }
}

async function releaseLease(mac, ip) {
    if (!confirm(`Liberer le bail de ${mac} (${ip}) ?\n\nA faire pour une machine MORTE, afin que sa remplacante reprenne l'IP. Une machine encore en service renegociera simplement un bail.`)) return;
    const res = await fetch(`/api/leases/${encodeURIComponent(mac)}/release`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) { alert(data.error || "Erreur"); return; }
    loadLeases();
}

let reservationsById = {};

async function loadReservations() {
    const body = document.getElementById("reservations-body");
    try {
        const res = await fetch("/api/reservations");
        const data = await res.json();
        reservationsById = Object.fromEntries(data.reservations.map(r => [r.id, r]));
        reservationsByMac = Object.fromEntries(data.reservations.filter(r => r.mac).map(r => [r.mac, r]));
        reservationsByName = Object.fromEntries(data.reservations.filter(r => !r.mac).map(r => [r.hostname, r]));
        if (!data.reservations.length) {
            body.innerHTML = '<tr><td colspan="4" class="empty">Aucune reservation</td></tr>';
            return;
        }
        body.innerHTML = data.reservations.map(r => `
            <tr>
                <td data-label="MAC">${r.mac ? `<code>${r.mac}</code>` : '<span class="badge badge-pending" title="Reservation par nom : suit la machine qui s\'annonce sous ce hostname, quelle que soit sa MAC">par nom</span>'}</td>
                <td data-label="IP">
                    <code>${r.ip}</code>
                    ${r.pending_renewal ? '<span class="badge badge-pending" title="L\'appareil a encore un bail actif sur une autre IP - il prendra celle-ci a son prochain renouvellement ou redemarrage">en attente de renouvellement</span>' : ''}
                </td>
                <td data-label="Hostname">${r.hostname || "-"}</td>
                <td class="actions">
                    <button class="secondary" data-edit-id="${r.id}">Modifier</button>
                    <button class="danger" data-id="${r.id}">Retirer</button>
                </td>
            </tr>
        `).join("");
        body.querySelectorAll("button.danger").forEach(btn => {
            btn.addEventListener("click", () => deleteReservation(btn.dataset.id));
        });
        body.querySelectorAll("button[data-edit-id]").forEach(btn => {
            btn.addEventListener("click", () => startEditReservation(btn.dataset.editId));
        });
    } catch (e) {
        body.innerHTML = '<tr><td colspan="4" class="status-error">Erreur de chargement</td></tr>';
    }
}

async function deleteReservation(id) {
    if (!confirm(`Retirer la reservation pour ${id} ?`)) return;
    const res = await fetch(`/api/reservations/${encodeURIComponent(id)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) { alert(data.error || "Erreur"); return; }
    if (editingId === id) stopEditReservation();
    loadReservations().then(loadLeases);
}

function startEditReservation(id) {
    const r = reservationsById[id];
    if (!r) return;
    editingId = id;
    const macField = document.getElementById("r-mac");
    macField.value = r.mac || "";
    macField.disabled = true;
    const hostnameField = document.getElementById("r-hostname");
    hostnameField.value = r.hostname || "";
    // Une reservation par nom ne change pas de nom : c'est sa cle.
    hostnameField.disabled = !r.mac;
    document.getElementById("r-ip").value = r.ip;
    document.getElementById("reservation-btn").textContent = "Modifier";
    document.getElementById("reservation-cancel").hidden = false;
    document.getElementById("reservation-status").hidden = true;
    document.getElementById("reservation-form").scrollIntoView({ behavior: "smooth", block: "center" });
}

function startReservationFromLease(mac, ip, hostname) {
    editingId = null;
    const macField = document.getElementById("r-mac");
    macField.value = mac;
    macField.disabled = false;
    document.getElementById("r-hostname").disabled = false;
    document.getElementById("r-ip").value = ip;
    document.getElementById("r-hostname").value = hostname;
    document.getElementById("reservation-btn").textContent = "Reserver";
    document.getElementById("reservation-cancel").hidden = true;
    document.getElementById("reservation-status").hidden = true;
    document.getElementById("reservation-form").scrollIntoView({ behavior: "smooth", block: "center" });
}

function stopEditReservation() {
    editingId = null;
    document.getElementById("r-mac").disabled = false;
    document.getElementById("r-hostname").disabled = false;
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
    const label = mac || hostname;
    const wasEditing = editingId;

    try {
        const res = wasEditing
            ? await fetch(`/api/reservations/${encodeURIComponent(wasEditing)}`, {
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
            status.textContent = wasEditing ? `Reservation ${label} modifiee.` : `Reservation ajoutee pour ${label}.`;
            stopEditReservation();
            loadReservations().then(loadLeases);
        }
    } catch (e) {
        status.hidden = false;
        status.className = "status status-error";
        status.textContent = "Erreur reseau";
    } finally {
        btn.disabled = false;
    }
});

// L'ordre compte : loadLeases() lit reservationsByMac/ByName (pour savoir si
// un bail a deja une reservation et afficher "Modifier" plutot que
// "Reserver"), qui ne sont peuples que par loadReservations().
loadReservations().then(loadLeases);
setInterval(loadLeases, 15000);

function formatBytes(n) {
    if (n < 1024) return `${n} o`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} Ko`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} Mo`;
    return `${(n / 1024 / 1024 / 1024).toFixed(1)} Go`;
}

async function loadPeers() {
    const body = document.getElementById("peers-body");
    try {
        const res = await fetch("/api/peers");
        const data = await res.json();
        if (!data.peers.length) {
            body.innerHTML = '<tr><td colspan="5" class="empty">Aucun pair</td></tr>';
            return;
        }
        body.innerHTML = data.peers.map(p => `
            <tr>
                <td data-label="Nom">${p.name}</td>
                <td data-label="IP VPN"><code>${p.vpn_ip}</code></td>
                <td data-label="Etat">
                    ${p.connected
                        ? '<span class="badge badge-ok">connecte</span>'
                        : p.last_handshake_min !== null
                            ? `<span class="badge badge-muted">vu il y a ${p.last_handshake_min} min</span>`
                            : '<span class="badge badge-muted">jamais connecte</span>'}
                </td>
                <td data-label="Trafic">&darr; ${formatBytes(p.transfer_rx)} / &uarr; ${formatBytes(p.transfer_tx)}</td>
                <td class="actions">
                    <button class="secondary" data-qr="${p.name}">QR</button>
                    <a class="secondary" style="text-decoration:none; display:inline-flex; align-items:center; padding:4px 10px; border:1px solid var(--muted); border-radius:6px; font-size:0.85rem;" href="/api/peers/${encodeURIComponent(p.name)}/conf">.conf</a>
                    <button class="danger" data-remove="${p.name}">Retirer</button>
                </td>
            </tr>
        `).join("");
        body.querySelectorAll("button[data-qr]").forEach(btn => {
            btn.addEventListener("click", () => showQr(btn.dataset.qr));
        });
        body.querySelectorAll("button[data-remove]").forEach(btn => {
            btn.addEventListener("click", () => removePeer(btn.dataset.remove));
        });
    } catch (e) {
        body.innerHTML = '<tr><td colspan="5" class="status-error">Erreur de chargement</td></tr>';
    }
}

function showQr(name) {
    document.getElementById("qr-modal-title").textContent = `QR code - ${name}`;
    document.getElementById("qr-modal-img").src = `/api/peers/${encodeURIComponent(name)}/qr.png?t=${Date.now()}`;
    document.getElementById("qr-modal").hidden = false;
}

document.getElementById("qr-modal-close").addEventListener("click", () => {
    document.getElementById("qr-modal").hidden = true;
});
document.getElementById("qr-modal").addEventListener("click", (e) => {
    if (e.target.id === "qr-modal") document.getElementById("qr-modal").hidden = true;
});

async function removePeer(name) {
    if (!confirm(`Retirer le pair '${name}' ?`)) return;
    const res = await fetch(`/api/peers/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) {
        alert(data.error || "Erreur");
        return;
    }
    loadPeers();
}

document.getElementById("create-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("create-btn");
    const status = document.getElementById("create-status");
    btn.disabled = true;
    status.hidden = true;

    const name = document.getElementById("name").value.trim().toLowerCase();

    try {
        const res = await fetch("/api/peers", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
        });
        const data = await res.json();
        status.hidden = false;
        if (!res.ok) {
            status.className = "status status-error";
            status.textContent = data.error || "Erreur";
        } else {
            status.className = "status status-ok";
            status.textContent = `Pair '${name}' cree.`;
            document.getElementById("create-form").reset();
            loadPeers();
        }
    } catch (e) {
        status.hidden = false;
        status.className = "status status-error";
        status.textContent = "Erreur reseau";
    } finally {
        btn.disabled = false;
    }
});

loadPeers();
setInterval(loadPeers, 15000);

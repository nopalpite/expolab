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

loadLeases();
setInterval(loadLeases, 15000);

const fleetBody = document.getElementById("fleet-body");
const createForm = document.getElementById("create-form");
const createBtn = document.getElementById("create-btn");
const createStatus = document.getElementById("create-status");

function statusClass(status) {
    const s = (status || "").toLowerCase();
    if (s === "running") return "badge badge-ok";
    if (s === "absent") return "badge badge-muted";
    return "badge badge-pending";
}

function renderFleet(fleet) {
    if (!fleet.length) {
        fleetBody.innerHTML = '<tr><td colspan="7" class="empty">Aucun faux Pi pour l\'instant.</td></tr>';
        return;
    }
    fleetBody.innerHTML = fleet.map((pi) => {
        const live = pi.live || { status: "Absent", ips: [] };
        const ip = live.ips && live.ips.length ? live.ips[0] : "-";
        return `
            <tr>
                <td data-label="Nom">${pi.name}</td>
                <td data-label="Statut"><span class="${statusClass(live.status)}">${live.status}</span></td>
                <td data-label="IP">${ip}</td>
                <td data-label="Role">${pi.role}</td>
                <td data-label="Identifiants"><code>${pi.username} / ${pi.password}</code></td>
                <td data-label="MAC"><code>${pi.mac}</code></td>
                <td data-label=""><button class="danger" data-name="${pi.name}">Supprimer</button></td>
            </tr>
        `;
    }).join("");

    fleetBody.querySelectorAll("button.danger").forEach((btn) => {
        btn.addEventListener("click", () => deletePi(btn.dataset.name));
    });
}

async function refreshFleet() {
    try {
        const res = await fetch("/api/fleet");
        const data = await res.json();
        renderFleet(data.fleet || []);
    } catch (err) {
        fleetBody.innerHTML = `<tr><td colspan="7" class="empty">Erreur de chargement : ${err}</td></tr>`;
    }
}

async function pollJob(jobId, onDone) {
    const res = await fetch(`/api/jobs/${jobId}`);
    const job = await res.json();
    if (job.status === "running") {
        setTimeout(() => pollJob(jobId, onDone), 3000);
    } else {
        onDone(job);
    }
}

createForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    createBtn.disabled = true;
    createStatus.hidden = false;
    createStatus.className = "status";
    createStatus.textContent = "Creation en cours (1 a 4 min)...";

    const payload = Object.fromEntries(new FormData(createForm).entries());

    try {
        const res = await fetch("/api/fleet", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.error || "erreur inconnue");
        }
        refreshFleet();
        pollJob(data.job_id, (job) => {
            createBtn.disabled = false;
            if (job.status === "done") {
                createStatus.className = "status status-ok";
                createStatus.textContent = `${payload.name} pret.`;
                createForm.reset();
                document.getElementById("username").value = "pi";
                document.getElementById("password").value = "raspberry";
            } else {
                createStatus.className = "status status-error";
                createStatus.textContent = `Echec : ${job.log.slice(-400)}`;
            }
            refreshFleet();
        });
    } catch (err) {
        createBtn.disabled = false;
        createStatus.className = "status status-error";
        createStatus.textContent = `Erreur : ${err.message}`;
    }
});

async function deletePi(name) {
    if (!confirm(`Supprimer ${name} ?`)) return;
    try {
        const res = await fetch(`/api/fleet/${name}`, { method: "DELETE" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "erreur inconnue");
        refreshFleet();
        pollJob(data.job_id, () => refreshFleet());
    } catch (err) {
        alert(`Erreur : ${err.message}`);
    }
}

refreshFleet();
setInterval(refreshFleet, 5000);

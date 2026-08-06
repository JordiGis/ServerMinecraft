async function loadMods() {
    try {
        // ###TODO: CAMBIAR LA URL A "/api/mods"
        const response = await fetch("http://localhost:5000/api/mods");
        const data = await response.json();

        const ul = $("mod-list");

        if (!data.mods || !data.mods.length) {
            ul.innerHTML = '<li class="muted">Sin mods instalados</li>';
            document.getElementById('mod-toolbar').style.display = 'none';
            return;
        }

        document.getElementById('mod-toolbar').style.display = 'flex';
        document.getElementById('mod-search').value = '';

        ul.innerHTML = data.mods.map(mod => {
            const name = mod.name.replace(/'/g, "\\'");
            const disabledClass = mod.enabled ? "" : "disabled";

            return `
                <li class="mod-item ${disabledClass}" onclick="toggleRowSelection(event, this)">
                    <div class="mod-item-left">
                        <input type="checkbox" class="mod-cb" value="${name}">
                        <span>${mod.name} <span class="muted">(${mod.size_kb} KB)</span></span>
                    </div>
                    <div class="mod-item-actions">
                        <button onclick="toggleMod('${name}')">${mod.enabled ? "Desactivar" : "Activar"}</button>
                        <button class="stop" onclick="deleteMod('${name}')">Borrar</button>
                    </div>
                </li>
            `;
        }).join("");
    } catch (e) {
        alert(e);
        $("mod-list").innerHTML = '<li class="err">error al listar</li>';
    }
}



async function uploadPack() {
const inp = $("packfile");
if (!inp.files.length) return;
const box = $("pack-out");
box.textContent = "Instalando… (puede tardar si descarga mods de CurseForge)";
const fd = new FormData();
fd.append("file", inp.files[0]);
try {
    const r = await fetch("/api/modpack", { method: "POST", body: fd });
    const d = await r.json();
    const lines = (d.log || []).concat(d.error ? ["ERROR: " + d.error] : ["OK"]);
    box.innerHTML = lines.map(l => "• " + l).join("<br>");
    box.className = "sub" + (d.error ? " err" : "");
} catch (e) { box.className = "sub err"; box.textContent = "fallo: " + e; }
inp.value = "";
loadMods();
}



async function uploadMod() {
    const inp = $("modfile");

    if (!inp.files.length) {
        return;
    }

    const fd = new FormData();
    fd.append("file", inp.files[0]);
    const response = await fetch("/api/mods", { method: "POST", body: fd });
    const data = await response.json();
    log(data.error ? "[mod] " + data.error : "[mod] subido: " + data.name, data.error ? "err" : "muted");
    inp.value = "";
    loadMods();
}

async function addModUrl() {
    const url = $("modurl").value.trim();
    if (!url) {
        alert("Introduce una URL directa a un .jar (Modrinth/CurseForge/…)");
        return;
    }

    log("[mod] descargando…", "muted");

    try {
        const response = await fetch("/api/mods", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({url})
        });

        if (!response.ok) {
            alert("Error al descargar el mod introducido en la url. Revisa que la URL introducida esté bien escrita y apunte a un archivo .jar");
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();
        log("[mod] descargado: " + data.name, d.error ? "err" : "muted");
    } catch (error) {
        log("[mod] error al descargar: " + error.message, "err");
        return;
    }

    $("modurl").value = "";

    loadMods();
}



async function toggleMod(name) {
    const r = await fetch("/api/mods/" + encodeURIComponent(name) + "/toggle", {method:"POST"});
    const d = await r.json();
    log(d.error ? "[mod] " + d.error : "[mod] " + name + (d.enabled ? " activado" : " desactivado") + " (reinicia para aplicar)", d.error ? "err" : "muted");
    loadMods();
}

async function deleteMod(name) {
    if (!confirm("¿Borrar mod " + name + "?")) return;
    const r = await fetch("/api/mods/" + encodeURIComponent(name), { method: "DELETE" });
    const d = await r.json();
    log(d.error ? "[mod] " + d.error : "[mod] borrado: " + name, d.error ? "err" : "muted");
    loadMods();
}



function obtainSelectedMods() {
    const checkboxes = document.querySelectorAll('.mod-cb:checked');
    const mods = Array.from(checkboxes).map(cb => cb.value);
    return mods; // Returns an array with the names, e.g.: ["mod1.jar", "mod2.jar"]
}

function updateBulkActionsVisibility() {
    const checkedCount = document.querySelectorAll('.mod-cb:checked').length;
    const bulkContainer = document.getElementById('bulk-actions');

    // Only shows the buttons if there is 1 or more selected
    bulkContainer.style.display = checkedCount > 0 ? 'flex' : 'none';
}

function toggleRowSelection(e, row) {
    // If the user clicks on the action buttons, we interrupt here
    // to not accidentally select the mod.
    if (e.target.tagName === 'BUTTON') return;

    const checkbox = row.querySelector('.mod-cb');
    
    // If the user clicks on the container or the text (not directly on the checkbox),
    // we manually toggle the state of the checkbox.
    if (e.target.tagName !== 'INPUT') {
        checkbox.checked = !checkbox.checked;
    }

    updateBulkActionsVisibility();
}

function filterMods() {
    const term = document.getElementById('mod-search').value.toLowerCase();
    const items = document.querySelectorAll('.mod-item');
    
    items.forEach(item => {
        const name = item.querySelector('span').textContent.toLowerCase();
        
        if (name.includes(term)) {
            item.style.display = 'flex';
        } else {
            item.style.display = 'none';
        }
    });
}

async function bulkAction(action) {
    const selectedMods = obtainSelectedMods();

    if (selectedMods.length === 0) return;

    if (action === 'download') {
        alert("Esta acción aún no está disponible");
        return;
    }

    if (action === 'delete') {
        alert("Esta acción aún no está disponible");
        return;
        // ###TODO: Implementar petición API con todos los mods y que se borren. Con una única petición.
        if (!confirm(`¿Estás seguro de borrar ${selectedMods.length} mod(s)?`)) return;
    }

    alert("Esta acción aún no está disponible");
    return;

    // ###TODO: Implementar endpoints para activar y desactivar masivamente con una petición
    log(`[mod] Procesando ${selectedMods.length} mod(s) (${action})...`, "muted");

    try {
        // Process sequentially to avoid overwhelming the web server with multiple requests
        for (const name of selectedMods) {

            // Search for the HTML element of the mod to verify its current state
            const checkbox = document.querySelector(`.mod-cb[value="${name}"]`);
            const row = checkbox.closest('.mod-item');
            
            // If the row has the 'disabled' class, the mod is deactivated
            const estaDesactivado = row.classList.contains('disabled');

            // if (action === 'delete') {
            //     await fetch("/api/mods/" + encodeURIComponent(name), { method: "DELETE" });
            // } 
            if (action === 'activate' && estaDesactivado) {
                // Only invert the state if it was deactivated
                await fetch("/api/mods/" + encodeURIComponent(name) + "/toggle", { method: "POST" });

            } else if (action === 'deactivate' && !estaDesactivado) {
                // Only invert the state if it was really activated
                await fetch("/api/mods/" + encodeURIComponent(name) + "/toggle", { method: "POST" });
            }
        }

        log(`[mod] Acción masiva '${action}' finalizada (reinicia para aplicar cambios en estado).`, "muted");
    } catch (e) {
        log(`[mod] Error procesando acción masiva: ${e}`, "err");
    }

    // Remove the buttons and reload the list
    document.getElementById('bulk-actions').style.display = 'none';
    loadMods();
}
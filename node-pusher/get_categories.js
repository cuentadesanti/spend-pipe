const fs = require('fs');
const path = require('path');
const os = require('os');

console.log = console.error;
console.info = console.error;
console.warn = console.error;

const api = require('@actual-app/api');

function loadDotEnv() {
    const p = path.join(__dirname, '..', '.env');
    if (!fs.existsSync(p)) return;
    for (const line of fs.readFileSync(p, 'utf-8').split('\n')) {
        const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
        if (m && process.env[m[1]] === undefined) {
            process.env[m[1]] = m[2].replace(/^["']|["']$/g, '');
        }
    }
}

function env(name) {
    const v = process.env[name];
    if (!v) throw new Error(`Falta la variable de entorno ${name}`);
    return v;
}

async function main() {
    loadDotEnv();
    const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'spendpipe-actual-categories-'));
    const serverURL = env('SPENDPIPE_ACTUAL_SERVER_URL');
    const password = env('SPENDPIPE_ACTUAL_PASSWORD');
    const syncId = env('SPENDPIPE_ACTUAL_SYNC_ID');

    await api.init({ dataDir, serverURL, password });
    await api.downloadBudget(syncId, { password });

    // Obtener categorías y grupos
    const groups = await api.getCategoryGroups();

    // Aplanar categorías por grupo
    const categoriesList = [];
    groups.forEach(g => {
        if (g.categories && !g.is_income) {
            g.categories.forEach(c => {
                if (!c.hidden) {
                    categoriesList.push({
                        id: c.id,
                        name: c.name,
                        group_name: g.name,
                        full_name: `${g.name} / ${c.name}`
                    });
                }
            });
        }
    });

    // Ordenar alfabéticamente
    categoriesList.sort((a, b) => a.full_name.localeCompare(b.full_name));

    // Imprimir JSON en stdout
    process.stdout.write(JSON.stringify(categoriesList) + "\n");
}

main()
    .catch(err => {
        console.error(err);
        process.exit(1);
    })
    .finally(() => api.shutdown());

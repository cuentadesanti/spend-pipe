const fs = require('fs');
const path = require('path');
const os = require('os');

// Redirigir todos lo logs de consola a stderr para dejar stdout limpio para el JSON
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
    const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'spendpipe-actual-accounts-'));
    const serverURL = env('SPENDPIPE_ACTUAL_SERVER_URL');
    const password = env('SPENDPIPE_ACTUAL_PASSWORD');
    const syncId = env('SPENDPIPE_ACTUAL_SYNC_ID');

    await api.init({ dataDir, serverURL, password });
    await api.downloadBudget(syncId, { password });

    const accounts = await api.getAccounts();

    // Imprimir UNICAMENTE el JSON al stdout
    process.stdout.write(JSON.stringify(accounts) + "\n");
}

main()
    .catch(err => {
        console.error(err);
        process.exit(1);
    })
    .finally(() => api.shutdown());

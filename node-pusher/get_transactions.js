// Dump de TODAS las transacciones existentes en Actual, por cuenta, como JSON.
// Alimenta el espejo local (actual_mirror) con el que spend-pipe reconcilia data
// histórica/legacy antes de pushear. Solo lectura.
//
// Uso: node get_transactions.js            → todas las cuentas
//      node get_transactions.js "Nombre"   → solo esa cuenta

const fs = require('fs');
const path = require('path');
const os = require('os');

// Logs a stderr; stdout queda limpio para el JSON (patrón de get_accounts.js).
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
    const onlyAccount = process.argv[2] || null;

    const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'spendpipe-actual-mirror-'));
    await api.init({ dataDir, serverURL: env('SPENDPIPE_ACTUAL_SERVER_URL'), password: env('SPENDPIPE_ACTUAL_PASSWORD') });
    await api.downloadBudget(env('SPENDPIPE_ACTUAL_SYNC_ID'), { password: env('SPENDPIPE_ACTUAL_PASSWORD') });

    // Mapa categoría id → 'Grupo / Nombre' para exportar nombres legibles.
    const catById = {};
    for (const g of await api.getCategoryGroups()) {
        for (const c of (g.categories || [])) catById[c.id] = `${g.name} / ${c.name}`;
    }
    const payeeById = {};
    for (const p of await api.getPayees()) payeeById[p.id] = p.name;

    const accounts = (await api.getAccounts()).filter(a => !onlyAccount || a.name === onlyAccount);
    const out = [];
    for (const a of accounts) {
        // Rango amplio: todo el histórico disponible.
        const txns = await api.getTransactions(a.id, '2000-01-01', '2100-01-01');
        for (const t of txns) {
            out.push({
                actual_txn_id: t.id,
                account_name: a.name,
                date: t.date,
                amount_cents: t.amount,                 // Actual ya guarda centavos enteros
                payee_name: payeeById[t.payee] || null,
                category_name: catById[t.category] || null,
                imported_id: t.imported_id || null,
                notes: t.notes || null,
                is_parent: !!t.is_parent,               // padres de splits
                transfer_id: t.transfer_id || null,     // patas de transferencia vinculada
            });
        }
        console.error(`  ${a.name}: ${txns.length} transacciones`);
    }

    process.stdout.write(JSON.stringify(out) + '\n');
}

main()
    .catch(err => { console.error(err); process.exit(1); })
    .finally(() => api.shutdown().catch(() => {}));

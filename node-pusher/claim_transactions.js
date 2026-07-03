// Backfill de imported_id en transacciones legacy de Actual que spend-pipe ADOPTÓ
// (reconciliación): tras reclamarlas, la próxima pasada las atrapa por id exacto
// (nivel 1) y el matching difuso no se vuelve a pagar.
//
// Entrada (stdin): JSON [{actual_txn_id, imported_id}, ...]
// Idempotente: si la txn ya tiene ese imported_id, se salta. Si tiene OTRO
// imported_id no-spendpipe (legacy), se sobreescribe (es el claim). Si ya tiene
// uno de spendpipe distinto, se avisa y NO se toca (conflicto para revisar).

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
    const input = fs.readFileSync(0, 'utf-8');   // stdin
    const pairs = JSON.parse(input);
    if (!Array.isArray(pairs) || !pairs.length) {
        console.error('Nada que reclamar.');
        process.stdout.write(JSON.stringify({ claimed: 0, skipped: 0, conflicts: 0 }) + '\n');
        return;
    }

    const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'spendpipe-claim-'));
    await api.init({ dataDir, serverURL: env('SPENDPIPE_ACTUAL_SERVER_URL'), password: env('SPENDPIPE_ACTUAL_PASSWORD') });
    await api.downloadBudget(env('SPENDPIPE_ACTUAL_SYNC_ID'), { password: env('SPENDPIPE_ACTUAL_PASSWORD') });

    let claimed = 0, skipped = 0, conflicts = 0;
    for (const { actual_txn_id, imported_id } of pairs) {
        // Leer el estado actual de la txn para decidir (updateTransaction es ciego).
        const existing = await api.runQuery(
            api.q('transactions').filter({ id: actual_txn_id }).select(['id', 'imported_id'])
        );
        const row = (existing.data || [])[0];
        if (!row) { console.error(`  ⚠️  ${actual_txn_id}: no existe en Actual (¿borrada?); se salta.`); skipped++; continue; }
        if (row.imported_id === imported_id) { skipped++; continue; }   // ya reclamada
        if ((row.imported_id || '').startsWith('spendpipe:')) {
            console.error(`  ⚠️  ${actual_txn_id}: ya tiene otro id spendpipe (${row.imported_id}); conflicto, no se toca.`);
            conflicts++;
            continue;
        }
        await api.updateTransaction(actual_txn_id, { imported_id });
        console.error(`  ✓ ${actual_txn_id} ← ${imported_id}${row.imported_id ? `  (antes: ${row.imported_id})` : ''}`);
        claimed++;
    }
    await api.sync();
    console.error(`\nReclamadas: ${claimed} · ya reclamadas/saltadas: ${skipped} · conflictos: ${conflicts}`);
    process.stdout.write(JSON.stringify({ claimed, skipped, conflicts }) + '\n');
}

main()
    .catch(err => { console.error(err); process.exit(1); })
    .finally(() => api.shutdown().catch(() => {}));

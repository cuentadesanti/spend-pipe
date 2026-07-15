// Operaciones directas sobre Actual, una por invocación. Lo consume el MCP:
//   node actual_ops.js '{"op":"get_balances"}'
//   node actual_ops.js '{"op":"add_transaction","account":"BBVA TDC","date":"2026-07-15","amount_cents":-1000,"payee":"X","category":"Gastos variables / Compras","notes":null}'
//   node actual_ops.js '{"op":"update_transaction","id":"<uuid>","category":"...","payee":"...","notes":"..."}'
//   node actual_ops.js '{"op":"delete_transaction","id":"<uuid>"}'
// Logs a stderr; el resultado es UNA línea JSON en stdout.
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

async function resolveCategoryId(name) {
    // Acepta 'Grupo / Categoría' o solo 'Categoría' (incluye grupos de ingreso).
    const groups = await api.getCategoryGroups();
    for (const g of groups) {
        for (const c of g.categories || []) {
            if (c.name === name || `${g.name} / ${c.name}` === name) return c.id;
        }
    }
    throw new Error(`Categoría no encontrada: ${name}`);
}

async function main() {
    loadDotEnv();
    const cmd = JSON.parse(process.argv[2]);
    const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'spendpipe-actual-ops-'));

    await api.init({
        dataDir,
        serverURL: env('SPENDPIPE_ACTUAL_SERVER_URL'),
        password: env('SPENDPIPE_ACTUAL_PASSWORD'),
    });
    await api.downloadBudget(env('SPENDPIPE_ACTUAL_SYNC_ID'), {
        password: env('SPENDPIPE_ACTUAL_PASSWORD'),
    });

    const accounts = await api.getAccounts();
    const out = {};

    switch (cmd.op) {
        case 'get_balances': {
            for (const a of accounts.filter(a => !a.closed)) {
                out[a.name] = (await api.getAccountBalance(a.id)) / 100;
            }
            break;
        }
        case 'add_transaction': {
            const acc = accounts.find(a => a.name === cmd.account);
            if (!acc) throw new Error(`Cuenta no encontrada: ${cmd.account}`);
            const txn = {
                date: cmd.date,
                amount: Math.round(cmd.amount_cents),
                payee_name: cmd.payee,
                notes: cmd.notes || null,
                cleared: true,
            };
            if (cmd.category) txn.category = await resolveCategoryId(cmd.category);
            const ids = await api.addTransactions(acc.id, [txn]);
            out.added = ids;
            out.saldo_cuenta = (await api.getAccountBalance(acc.id)) / 100;
            break;
        }
        case 'update_transaction': {
            const fields = {};
            if (cmd.category) fields.category = await resolveCategoryId(cmd.category);
            if (cmd.payee) fields.payee_name = cmd.payee;
            if (cmd.notes !== undefined && cmd.notes !== null) fields.notes = cmd.notes;
            if (Object.keys(fields).length === 0) throw new Error('Nada que actualizar');
            await api.updateTransaction(cmd.id, fields);
            out.updated = cmd.id;
            break;
        }
        case 'delete_transaction': {
            // OJO: no cascadea a la contraparte de una transferencia vinculada.
            await api.deleteTransaction(cmd.id);
            out.deleted = cmd.id;
            break;
        }
        default:
            throw new Error(`Operación desconocida: ${cmd.op}`);
    }

    await api.sync();
    process.stdout.write(JSON.stringify(out) + '\n');
}

main()
    .catch(err => {
        console.error(err.message || err);
        process.exitCode = 1;
    })
    .finally(() => api.shutdown());

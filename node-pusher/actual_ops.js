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
    const disponibles = [];
    for (const g of groups) {
        for (const c of g.categories || []) {
            if (c.name === name || `${g.name} / ${c.name}` === name) return c.id;
            if (!c.hidden) disponibles.push(`${g.name} / ${c.name}`);
        }
    }
    throw new Error(
        `Categoría no encontrada: '${name}'. Disponibles: ${disponibles.join(' | ')}`
    );
}

async function resolvePayeeId(name) {
    const payees = await api.getPayees();
    const match = payees.find(p => (p.name || '').toLowerCase() === name.toLowerCase());
    if (!match) throw new Error(`Payee no encontrado: '${name}' (para renombrar usa el nombre exacto de un payee existente)`);
    return match.id;
}

async function getTxn(id) {
    const { q } = api;
    const res = await api.runQuery(
        q('transactions')
            .filter({ id })
            .select(['id', 'date', 'amount', 'notes', 'payee.name', 'category.name', 'account.name'])
    );
    return (res.data && res.data[0]) || null;
}

function txnView(t) {
    return t && {
        fecha: t.date, monto: t.amount / 100, payee: t['payee.name'],
        categoria: t['category.name'], notas: t.notes, cuenta: t['account.name'],
    };
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
            const before = await getTxn(cmd.id);
            if (!before) throw new Error(`not_found: no existe transacción con id ${cmd.id}`);
            const acc = accounts.find(a => a.name === before['account.name']);
            if (cmd.category && acc && acc.offbudget) {
                throw new Error(
                    `La cuenta '${acc.name}' es off-budget: Actual no acepta categorías ahí ` +
                    '(la categoría se ignoraría en silencio)'
                );
            }
            const fields = {};
            if (cmd.category) fields.category = await resolveCategoryId(cmd.category);
            if (cmd.payee) fields.payee = await resolvePayeeId(cmd.payee);
            if (cmd.notes !== undefined && cmd.notes !== null) fields.notes = cmd.notes;
            if (Object.keys(fields).length === 0) throw new Error('Nada que actualizar');
            await api.updateTransaction(cmd.id, fields);
            out.updated = cmd.id;
            out.before = txnView(before);
            // OJO: no re-consultamos ('el motor AQL cachea la query del before y
            // devuelve el valor viejo'); el after se construye con lo aplicado.
            out.after = {
                ...out.before,
                ...(cmd.category ? { categoria: cmd.category } : {}),
                ...(cmd.payee ? { payee: cmd.payee } : {}),
                ...(cmd.notes !== undefined && cmd.notes !== null ? { notas: cmd.notes } : {}),
            };
            break;
        }
        case 'delete_transaction': {
            // OJO: no cascadea a la contraparte de una transferencia vinculada.
            const before = await getTxn(cmd.id);
            if (!before) throw new Error(`not_found: no existe transacción con id ${cmd.id}`);
            await api.deleteTransaction(cmd.id);
            out.deleted = cmd.id;
            out.era = txnView(before);
            break;
        }
        case 'get_categories': {
            const groups = await api.getCategoryGroups();
            out.categorias = [];
            for (const g of groups) {
                for (const c of g.categories || []) {
                    if (!c.hidden) out.categorias.push(`${g.name} / ${c.name}`);
                }
            }
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
        // El error viaja como JSON en stdout (stderr trae los logs de sync del
        // SDK de Actual y no sirve como canal de error hacia el MCP).
        process.stdout.write(JSON.stringify({ error: String(err.message || err) }) + '\n');
        process.exitCode = 1;
    })
    .finally(() => api.shutdown());

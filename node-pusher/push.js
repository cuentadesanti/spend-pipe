// Worker de push: lee un artefacto batch-<id>.json de spend-pipe y lo empuja a
// Actual Budget en PikaPods. Es deliberadamente "tonto": toda la lógica de negocio
// (qué payee, qué categoría, qué cuenta, qué imported_id) ya viene resuelta desde
// Python. Acá solo: mapear nombre de cuenta → id, convertir monto a entero, importar.
//
// Uso:
//   node push.js ../artifacts/batch-<id>.json            (dry-run: solo muestra el plan)
//   node push.js ../artifacts/batch-<id>.json --commit   (escribe en Actual)

const fs = require('fs');
const path = require('path');
const os = require('os');
const api = require('@actual-app/api');

const COMMIT = process.argv.includes('--commit');
const artifactPath = process.argv.find((a) => a.endsWith('.json'));

// ── .env mínimo: carga ../.env sin dependencias, sin pisar lo que ya esté en el entorno.
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
  if (!v) throw new Error(`Falta la variable de entorno ${name} (definila en .env)`);
  return v;
}

async function main() {
  if (!artifactPath) {
    throw new Error('Pasá la ruta del artefacto: node push.js <batch.json> [--commit]');
  }
  loadDotEnv();

  const artifact = JSON.parse(fs.readFileSync(artifactPath, 'utf-8'));
  if (artifact.schema_version !== '1.0') {
    console.warn(`⚠️  schema_version '${artifact.schema_version}' (este worker espera '1.0')`);
  }

  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'spendpipe-actual-'));
  const serverURL = env('SPENDPIPE_ACTUAL_SERVER_URL');
  const password = env('SPENDPIPE_ACTUAL_PASSWORD');
  const syncId = env('SPENDPIPE_ACTUAL_SYNC_ID');

  await api.init({ dataDir, serverURL, password });
  await api.downloadBudget(syncId, { password });

  const accounts = await api.getAccounts();
  const idByName = Object.fromEntries(accounts.map((a) => [a.name, a.id]));

  console.log('='.repeat(64));
  console.log(`BATCH ${artifact.batch_id}  —  modo: ${COMMIT ? 'COMMIT (escribe en Actual)' : 'DRY-RUN'}`);
  console.log('='.repeat(64));

  const plan = [];
  for (const grp of artifact.accounts) {
    const accountId = idByName[grp.actual_account_name];
    if (!accountId) {
      console.log(`\n⚠️  Cuenta '${grp.actual_account_name}' no existe en Actual; se omite el grupo.`);
      continue;
    }
    const txns = grp.transactions.map((t) => {
      const tx = {
        date: t.date,
        amount: api.utils.amountToInteger(parseFloat(t.amount)),
        payee_name: t.payee_name || undefined,
        notes: t.notes || undefined,
        imported_id: t.imported_id,
        cleared: t.cleared !== false,
      };
      // category_name es null en MVP1 (Actual categoriza post-import con sus reglas).
      // Cuando spend-pipe empiece a mandar categoría (MVP3), acá se resuelve name → id.
      return tx;
    });
    console.log(`\n──── ${grp.actual_account_name}  (${txns.length} txns) ────`);
    for (const t of txns.slice(0, 5)) {
      console.log(`  ${t.date}  ${String(api.utils.integerToAmount(t.amount)).padStart(11)}  ${(t.payee_name || '').slice(0, 30)}`);
    }
    if (txns.length > 5) console.log(`  … (+${txns.length - 5} más)`);
    plan.push({ accountId, name: grp.actual_account_name, txns });
  }

  const total = plan.reduce((n, p) => n + p.txns.length, 0);
  console.log(`\nTOTAL: ${plan.length} cuenta(s), ${total} transacción(es).`);

  if (!COMMIT) {
    console.log('\nDRY-RUN: no se escribió nada. Volvé a correr con --commit para importar.');
    return;
  }

  console.log('\nImportando...');
  for (const { accountId, name, txns } of plan) {
    const res = await api.importTransactions(accountId, txns);
    console.log(`  ✓ ${name}: +${(res.added || []).length} nuevas, ${(res.updated || []).length} actualizadas`);
  }
  console.log('\n✓ Push completo.');
}

main()
  .catch((err) => { console.error('\nError en el push:', err.message || err); process.exitCode = 1; })
  .finally(async () => { try { await api.shutdown(); } catch (_) {} });

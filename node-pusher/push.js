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
  if (!/^1\./.test(artifact.schema_version || '')) {
    console.warn(`⚠️  schema_version '${artifact.schema_version}' (este worker espera 1.x)`);
  }

  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'spendpipe-actual-'));
  const serverURL = env('SPENDPIPE_ACTUAL_SERVER_URL');
  const password = env('SPENDPIPE_ACTUAL_PASSWORD');
  const syncId = env('SPENDPIPE_ACTUAL_SYNC_ID');

  await api.init({ dataDir, serverURL, password });
  await api.downloadBudget(syncId, { password });

  const accounts = await api.getAccounts();
  const idByName = Object.fromEntries(accounts.map((a) => [a.name, a.id]));

  // ── Resolver categorías ('Grupo / Categoría' → id), creando las que falten ──
  const groups = await api.getCategoryGroups();
  const catIdByFull = {};
  const groupByName = {};
  for (const g of groups) {
    groupByName[g.name] = g;
    for (const c of (g.categories || [])) catIdByFull[`${g.name} / ${c.name}`] = c.id;
  }
  async function resolveCategory(fullName) {
    if (!fullName) return undefined;
    if (catIdByFull[fullName]) return catIdByFull[fullName];
    const [groupName, catName] = fullName.split(' / ');
    const group = groupByName[groupName];
    if (!group || !catName) {
      console.log(`  ⚠️  categoría '${fullName}': grupo inexistente; va sin categoría.`);
      return undefined;
    }
    if (!COMMIT) return undefined; // dry-run: no crear nada
    const id = await api.createCategory({ name: catName, group_id: group.id, is_income: !!group.is_income });
    catIdByFull[fullName] = id;
    console.log(`  ✓ categoría creada: ${fullName}`);
    return id;
  }

  console.log('='.repeat(64));
  console.log(`BATCH ${artifact.batch_id}  —  modo: ${COMMIT ? 'COMMIT (escribe en Actual)' : 'DRY-RUN'}`);
  console.log('='.repeat(64));

  const plan = [];
  const transferLegs = [];   // patas negativas a vincular como transferencia (fase 2)
  for (const grp of artifact.accounts) {
    const accountId = idByName[grp.actual_account_name];
    if (!accountId) {
      console.log(`\n⚠️  Cuenta '${grp.actual_account_name}' no existe en Actual; se omite el grupo.`);
      continue;
    }
    const txns = [];
    for (const t of grp.transactions) {
      if (t.transfer_to_actual_account) {
        transferLegs.push({
          accountId, date: t.date, imported_id: t.imported_id,
          destName: t.transfer_to_actual_account,
        });
      }
      txns.push({
        date: t.date,
        amount: api.utils.amountToInteger(parseFloat(t.amount)),
        payee_name: t.payee_name || undefined,
        notes: t.notes || undefined,
        imported_id: t.imported_id,
        cleared: t.cleared !== false,
        // Categoría resuelta desde spend-pipe (reglas en-pipeline). Las transferencias
        // no llevan categoría (la fase 2 las vincula).
        category: t.transfer_to_actual_account ? undefined : await resolveCategory(t.category_name),
      });
    }
    console.log(`\n──── ${grp.actual_account_name}  (${txns.length} txns) ────`);
    for (const t of txns.slice(0, 5)) {
      console.log(`  ${t.date}  ${String(api.utils.integerToAmount(t.amount)).padStart(11)}  ${(t.payee_name || '').slice(0, 30)}`);
    }
    if (txns.length > 5) console.log(`  … (+${txns.length - 5} más)`);
    plan.push({ accountId, name: grp.actual_account_name, txns });
  }
  if (transferLegs.length) {
    console.log(`\n⇄ ${transferLegs.length} transferencia(s) a vincular tras el import:`);
    for (const l of transferLegs) console.log(`  ${l.date}  → ${l.destName}`);
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

  // ── Fase 2: vincular transferencias ──
  // A cada pata negativa se le pone el transfer-payee de la cuenta destino;
  // Actual auto-crea la contraparte (+) vinculada en esa cuenta. Idempotente:
  // si la txn ya tiene transfer_id (re-run), se salta.
  if (transferLegs.length) {
    console.log('\nVinculando transferencias...');
    const payees = await api.getPayees();
    for (const leg of transferLegs) {
      const destId = idByName[leg.destName];
      if (!destId) { console.log(`  ⚠️  destino '${leg.destName}' no existe; se deja como transacción normal.`); continue; }
      const tp = payees.find((p) => p.transfer_acct === destId);
      if (!tp) { console.log(`  ⚠️  no hay transfer-payee para '${leg.destName}'; se deja normal.`); continue; }
      const tx = (await api.getTransactions(leg.accountId, leg.date, leg.date))
        .find((t) => t.imported_id === leg.imported_id);
      if (!tx) { console.log(`  ⚠️  no se encontró ${leg.imported_id} tras el import.`); continue; }
      if (tx.transfer_id) { console.log(`  · ${leg.date} → ${leg.destName}: ya vinculada (se omite).`); continue; }
      await api.updateTransaction(tx.id, { payee: tp.id });
      console.log(`  ⇄ ${leg.date} → ${leg.destName}: vinculada (contraparte auto-creada).`);
    }
    await api.sync();
  }
  console.log('\n✓ Push completo.');
}

main()
  .catch((err) => { console.error('\nError en el push:', err.message || err); process.exitCode = 1; })
  .finally(async () => { try { await api.shutdown(); } catch (_) {} });

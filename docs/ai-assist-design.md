# Diseño: módulo AI-assisted (match / categorizer / parser)

**Principio rector (decidido 2026-07-02):** determinista primero; la IA nunca es el
corazón del pipeline — es un *sugeridor* sobre lo que el camino determinista no
resolvió, y todo lo que sugiere queda en `needs_review` salvo confianza altísima.
La IA propone, el humano (o una regla que ya existe) dispone.

## Dónde encaja (los tres sub-módulos)

```
                    determinista                IA (fallback)
matching     niveles 1-4 + payee_similarity  →  juez de las nivel-3 dudosas
categorizar  rules/categorize.yaml           →  sugeridor para payees sin regla
parsear      registry + sniff + mapeos       →  extractor para layouts desconocidos
```

### 1. AI-match (juez de dudosas) — el más valioso hoy
Caso real medido: tras similarity, quedan **22 nivel-3** tipo
`OPEN BANK, S.A. -0.28 (26-01)` ↔ `Comisión Bancaria (27-01)` — misma transacción,
payees semánticamente iguales pero textualmente distintos. Un modelo chico decide
"¿son la misma?" con contexto (monto, fechas, payees, categoría legacy).

- **Input**: los pares nivel-3 del `ReconcileReport` (batch de a N, JSON compacto).
- **Output**: `same | different | unsure` + razón de una línea.
- **Acción**: `same` con confianza alta → auto-adoptar marcando `sync_origin='adopted'`
  y `confidence<1.0`; `unsure`/`different` → queda en la tabla de dudosas como hoy.
- **Modelo**: Haiku (barato, task simple y estructurada). Prompt con few-shots de
  los patrones ya conocidos (fee bancario, wallet-prefix, truncamiento).

### 2. AI-categorizer (sugeridor sin regla)
Caso real: 153/353 filas sin regla; la cola larga son restaurantes/bares one-off.
- **Input**: payee + monto + fecha + lista cerrada de categorías reales (de
  `get_categories.js`, ya cacheadas).
- **Output**: categoría de la lista (o `none`) + confianza. **Nunca inventa
  categorías** — elige de las existentes o se abstiene.
- **Acción**: se guarda en `category` con `confidence<1.0` → la fila sigue en
  `needs_review` pero llega **pre-llenada** al triage (aceptar = un click).
- **Loop de mejora**: si el humano confirma N veces el mismo payee→categoría,
  sugerir promoverlo a regla YAML (la IA se vuelve innecesaria para ese payee).
  Ese es el objetivo: la IA alimenta el sistema determinista, no lo reemplaza.

### 3. AI-parser (extractor de layouts desconocidos)
Ya previsto desde el diseño original de PDF por niveles (el "nivel 3" de entonces).
- **Trigger**: `sniff` cae en "PDF/HTML no reconocido" — hoy termina en pegado manual.
- **Método**: el LLM recibe el texto extraído (pdftotext/html) y devuelve filas
  `{date, amount, payee, memo}` **+ los totales que ve en el documento**.
- **Candado obligatorio**: reconciliación contra esos totales igual que BBVA
  (`ParseValidationError` si no cuadra) — mismo estándar que los parsers
  deterministas: fail loud, nunca basura plausible. Todo entra como
  `format='ai-extracted'` y `needs_review` completo.
- **No** reemplaza parsers deterministas: si un formato AI-parseado se vuelve
  mensual, se escribe su parser real (la IA sirvió de puente, como el pegado manual).

## Infraestructura común
- Un solo módulo `spend_pipe/ai.py` con cliente Anthropic (API key por env var
  `SPENDPIPE_ANTHROPIC_KEY`, opcional: sin key, los tres fallbacks se desactivan y
  el pipeline sigue 100% funcional).
- Toda sugerencia de IA se persiste con `confidence < 1.0` y origen identificable
  (p.ej. `rule_label='ai:haiku'`) — auditable y filtrable.
- Presupuesto: llamadas solo on-demand (botón "Sugerir con IA" en el review) al
  principio; automatizar recién cuando la tasa de acierto medida lo justifique.

## Orden sugerido de implementación
1. AI-categorizer (máximo volumen: 153 filas/mes, riesgo mínimo — solo pre-llena).
2. AI-match (22 dudosas/mes, elimina casi todo el trabajo manual de reconciliación).
3. AI-parser (menor frecuencia; el pegado manual ya cubre el gap).

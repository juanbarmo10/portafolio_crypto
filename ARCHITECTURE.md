# Arquitectura y decisiones de diseño

Documento técnico del panel cripto-macro: cómo está construido y **por qué** se tomó cada
decisión. Sin datos personales ni de cartera — solo el sistema. Para instalar y ejecutar, ver
[README.md](README.md).

---

## 1. Objetivo y filosofía

Panel que ingiere datos **gratuitos**, calcula indicadores y responde **cuatro preguntas en orden**:

1. **¿Hay apetito por riesgo?** — macro (Fed, inflación, empleo, dólar).
2. **¿El rally tiene sustento o es mecánico?** — estructura de mercado (flujos ETF, OI, funding).
3. **¿La tesis del activo sigue viva?** — fundamentales del token (TVL, revenue, unlocks).
4. **¿Toca ejecutar según el plan?** — estado del DCA y reglas escritas.

Dos decisiones de diseño rectoras:

- **Pull, no push (anti-over-trading).** El mayor riesgo del proyecto no es técnico, es conductual:
  un panel en tiempo real empuja a mirar y mirar empuja a operar. No hay tickers en vivo,
  auto-refresh ni alertas de precio. Resolución diaria salvo justificación. Las alertas solo se
  disparan por condiciones accionables escritas de antemano.
- **Solo fuentes gratuitas.** Una cartera pequeña no puede pagar $50/mes en herramientas. Cualquier
  dato de pago se documenta como fuera de alcance y se busca alternativa gratuita.

## 2. Vista general del flujo

```mermaid
flowchart LR
    subgraph Fuentes["Fuentes gratuitas"]
        FRED[FRED REST]
        CG[CoinGecko]
        DL[DefiLlama]
        DER[Binance/Bybit ccxt]
        ETF[Farside scraper]
        DVOL[Deribit DVOL]
        FNG[Alternative.me F&G]
        BCOM[Blockchain.com]
        SPOT[Coinbase/Binance spot]
    end
    Fuentes --> ING[ingest/ · fetch parsers puros]
    ING --> LOADER[db/loader · upsert idempotente]
    LOADER --> DB[(SQLite / PostgreSQL<br/>formato largo)]
    DB --> TR[transform/ · indicadores puros]
    DB --> VAL[validation/ · backtest]
    TR --> APP[app/dashboard.py · Streamlit pull]
    VAL --> APP
    TR --> ALERTS[alerts/ · reglas + Telegram]
    DB --> ALERTS
```

Capas (cada una testeable de forma aislada):

| Capa | Responsabilidad | Nota clave |
|---|---|---|
| `ingest/` | `fetch() -> DataFrame[source, series_id, ts, ts_release, value]` | Parsers **puros** + fixtures congelados; fallan ruidosamente si cambia el HTML/JSON. |
| `db/` | Esquema (formato largo) + upsert idempotente + lectura | `INSERT ... ON CONFLICT DO UPDATE`; adapter portable SQLite↔Postgres. |
| `transform/` | Indicadores como **funciones puras** + constructores de tabla | Sin red; entrada faltante → `None`, no excepción. |
| `validation/` | Backtest de señales (retornos forward + bootstrap) | Point-in-time, sin look-ahead. |
| `alerts/` | Reglas declarativas + Telegram | Dedup por evento/día; solo condiciones accionables. |
| `app/` | Dashboard Streamlit (5 secciones + showcase) | Pull, sin auto-refresh. |

## 3. Esquema de datos — formato largo

Una sola tabla de observaciones en **formato largo** `(source, series_id, ts, ts_release, value)`.
Añadir una serie nueva **no requiere migración de esquema**. `ts` = fecha de referencia del dato;
`ts_release` = fecha de publicación (crítico, ver §5). Tablas auxiliares: `events` (calendario),
`trades`, `dca_plan`, `exit_rules`, `alerts_log`.

**Idempotencia obligatoria:** todo upsert usa `ON CONFLICT DO UPDATE`. El pipeline se re-ejecuta
muchas veces al día; nunca debe duplicar. La clave primaria `(source, series_id, ts)` lo garantiza.

## 4. Decisiones técnicas notables

- **Point-in-time / anti look-ahead.** El CPI de junio se publica a mediados de julio. Asignarlo a
  "junio" en un backtest usa información del futuro. Por eso cada dato macro guarda `ts` **y**
  `ts_release`, y todo z-score/backtest usa solo la ventana anterior a cada fecha.
- **FRED sin `fredapi`.** Se usa el REST directo con `output_type=4` (initial release only) para
  fechas point-in-time. Las series diarias superan el límite de 2000 vintage dates por petición, así
  que se **bisecta la ventana real-time** de forma recursiva hasta quedar por debajo del límite.
- **Backend de DB portable.** Un adaptador fino (`db/database.py`) expone la interfaz de `sqlite3`
  sobre `psycopg`, seleccionado por la variable `DATABASE_URL`. Traduce placeholders (`?`→`%s`) y
  el `executescript`. El código de negocio no sabe qué backend hay debajo (SQLite local, Postgres
  gestionado en la nube).
- **Scraping frágil por diseño.** Los scrapers (flujos ETF) tienen parser puro testeado contra un
  **fixture HTML congelado**: si la estructura cambia, el test falla ruidosamente en CI en vez de
  emitir datos silenciosamente incorrectos.
- **Aislamiento de privacidad.** La ingesta a la DB pública usa `--public`, que **omite** la
  sincronización de la cuenta personal; el dashboard desplegado usa `PUBLIC_MODE=1`, que oculta la
  sección de cartera real. Las claves de cuenta nunca se despliegan.
- **pandas 3.0 / Altair.** Altair 6.2 no serializa un DataFrame de pandas 3.0 (inyecta cero filas).
  Los charts se construyen pasando registros pre-convertidos vía `alt.Data`. Las dependencias tienen
  cota superior por major para evitar que un `pip install` nuevo rompa el panel en silencio.

## 5. Metodología e indicadores

Todos los indicadores son funciones puras y testeadas. Los más relevantes:

- **Funding z-score** — `(x_t − media(V)) / desv(V)` sobre ventana móvil de 90 días, solo con la
  ventana anterior (point-in-time). `|z| ≥ 2` marca posicionamiento hacinado.
- **Estado del rally** — cuadrantes precio×interés-abierto: precio↑/OI↑ = convicción (dinero nuevo);
  precio↑/OI↓ = mecánico (cierre de cortos, frágil); etc.
- **Value accrual (concepto rector)** — dos ratios:
  - `MC/TVL` = valoración vs. capital bloqueado (proxy de actividad).
  - `MC/Revenue` (P/E cripto) = valoración vs. *revenue* que realmente llega al token (DefiLlama,
    anualizado). Un protocolo con mucho TVL pero **$0 de revenue** (sin fee switch) no tiene
    `MC/Revenue` — la señal más clara de "el token no captura valor".
- **Validación de señales** — retorno *forward* a 7/30/90 días desde cada fecha de señal vs. baseline
  de **fechas sin señal** (disjunto), con p-valor por **permutación**. El baseline disjunto evita
  comparar un subconjunto contra su propio superconjunto. Los z-scores son point-in-time. Los
  resultados se documentan **incluidos los que no funcionan** — ese es el punto.
- **DCA vs. baseline** — precio medio de entrada real vs. **DCA ciego** sobre la misma ventana de
  acumulación. El coste realizado de invertir un importe fijo por periodo es la **media armónica**
  de los precios (`n / Σ 1/p`), no la aritmética — usar la aritmética sesgaría el *edge*.
- **Tablero de invalidación** — semáforo por activo a partir de señales cuantitativas (salidas ETF
  sostenidas para BTC/ETH, caída de TVL, dilución, unlock próximo). Es **honesto** sobre lo que no
  puede medir: las invalidaciones cualitativas (demanda de token, riesgo regulatorio) se marcan como
  no medibles en vez de fingir un estado verde.
- **Liquidez neta de la Fed** — `WALCL − TGA − RRP`. Los tres componentes de FRED tienen **unidades
  y cadencias distintas** (WALCL/TGA semanales en *millones*, RRP diaria en *miles de millones*): se
  normalizan a miles de millones con un `unit_scale` por serie (verificado contra la metadata de FRED)
  y se alinean *as-of* (unión de fechas, *forward-fill*) antes de restar. La resta con unidades sin
  normalizar es un error silencioso clásico; por eso el escalado vive en config y está testeado.
- **Basis y premium** — `(a/base − 1)·100`: **basis** perp−spot (apalancamiento/optimismo; para un
  perp es la prima instantánea, no un basis anualizado con vencimiento) y **premium de Coinbase**
  (Coinbase vs. Binance = demanda spot US). Ambos emparejan las dos series en la **misma fecha**
  (*inner join*) para que un desfase de un día no contamine una señal pequeña.
- **Rotación** — ETH/BTC y TOTAL2/TOTAL3 (mcap total menos BTC, y menos BTC y ETH) desde datos
  propios; complementan la dominancia BTC para "¿entorno favorable a altcoins?".
- **Semáforo de régimen** — *marcador rector* que agrega las señales de nivel 1+2 (liquidez neta,
  NFCI, spread HY, DXY, racha de flujos ETF, stablecoins, funding z) en un score. Cada señal vota
  `+1/0/−1` a través de una *dead-zone* documentada en config; el score es la **suma con pesos
  iguales fijos**, clasificada con umbrales fijos. Es la operacionalización de la regla dura de §1
  (si niveles 1-2 en rojo, no comprar) y es deliberadamente **transparente** (desglose por señal) y
  **anti-overfitting**: pocos componentes, pesos fijos, sin optimizar sobre el histórico (§4). Es un
  estado semanal que **bloquea** malas decisiones, no un gatillo de operación.
- **Drift vs. objetivo** — peso actual por tramo (sobre capital **invertido**, sin efectivo) vs. los
  pesos objetivo de §5 (en config). El drift en puntos porcentuales identifica el tramo más
  infra-ponderado → **rebalanceo por aportación** (añadir al tramo bajo, no vender: evita comisiones
  e impuestos). Los activos fuera de §5 se clasifican vía `symbol_aliases` (WBETH → ETH, núcleo)
  manteniendo su propio precio; los no clasificables caen en "Sin tramo", honesto.
- **Magnitud del unlock** — no solo la fecha: `unlock_pct` (% del circulante que se libera) entra en
  el tablero de invalidación — un unlock **grande y próximo** (≥ umbral dentro de 30 d) se marca en
  rojo, no solo por inminencia (§5: "unlock > 5% del circulante" es la regla accionable).
- **Riesgo de cartera** — sobre retornos diarios propios (backfill): **correlación**, **beta a BTC**,
  **volatilidad** anualizada (365 d, no 252), **max drawdown** de la senda de valor, **concentración**
  (HHI y N efectivo = 1/HHI) y **contribución al riesgo** `RCᵢ = wᵢ·(Σw)ᵢ/(wᵀΣw)` (suma 100 %: una
  posición del 15 % del capital puede ser el 40 % del riesgo si es la más volátil). Cuantifica la
  diversificación **real** por modo de fallo (§5): posiciones correladas ~0.9 son una sola apuesta,
  no diez. WBETH se une a ETH en los pesos (misma apuesta), conservando su propio precio.
- **Scorecard conductual** — construye dos carteras con el **mismo flujo de capital** (las
  operaciones reales): los activos operados vs. un contrafactual que enruta cada dólar a **BTC** al
  precio de la fecha de cada operación. Compara su valor actual sobre la misma base invertida — el
  test directo de "¿mi selección/timing batió a solo mantener BTC?". Es el widget que **valida la
  tesis anti-over-trading** del proyecto. Honesto: usa solo operaciones (ambos lados), y excluye de
  la comparación las anteriores a la ventana de 365 días gratis de precios (contándolas).
- **Ayudante de aporte mensual** — no calcula nada nuevo: **compone** el semáforo de régimen, el
  calendario de eventos (releases macro/FOMC ≤7 d) y el drift de asignación en **una decisión
  mensual**: ejecutar (→ tramo más infra-ponderado) o posponer N días (hasta después de un evento
  inminente, o mientras el régimen esté risk-off). Operacionaliza el nivel 4 del checklist — el plan
  escrito hecho concreto — con cadencia mensual por diseño, nunca un gatillo.

## 6. Trampas del dominio que el diseño evita

- **Look-ahead bias** — el error que invalida más backtests (ver §4/§5).
- **Overfitting** — validación fuera de muestra; desconfiar de reglas con muchos parámetros libres;
  no probar 50 variantes y quedarse con la mejor.
- **Discrepancia entre fuentes** — cada número documenta de qué proveedor sale; no se mezclan
  proveedores en una misma serie.
- **Rate limits** — endpoints batch, backoff exponencial, y un *backfill* histórico que reintenta y
  salta un activo ante un 429 (idempotente, se completa al re-ejecutar).

## 7. Calidad y despliegue

- **Tests** (`pytest`) obligatorios en parsers (con fixtures) y transformaciones; humo de render del
  dashboard con `AppTest`. **CI** (GitHub Actions) corre `ruff` + `pytest` en cada push.
- **Config externalizada** — activos, umbrales, ventanas e IDs en YAML; secretos solo en `.env`
  (ignorado por git). Nada hardcodeado.
- **Despliegue** — cron/systemd local escribe datos **públicos** en una Postgres gestionada; la app
  en Streamlit Community Cloud la lee en modo público. La cartera personal nunca sale de la máquina
  local.

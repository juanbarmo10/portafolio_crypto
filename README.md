# cryptodash — Dashboard cripto-macro

Panel personal de análisis cripto-macro con fuentes de datos **gratuitas**. Ingesta
macro (FRED), precios (CoinGecko) y TVL (DefiLlama) en SQLite, calcula indicadores
derivados y los presenta en Streamlit, con alertas por Telegram y un framework de
validación estadística de señales.

El contexto completo del proyecto (filosofía de diseño, activos, tesis, hoja de ruta
por fases y trampas del dominio) vive en [CLAUDE.md](CLAUDE.md). Las **decisiones técnicas
y ecuaciones** (formulación, efecto en cripto, uso y qué validar) están en [RESEARCH.md](RESEARCH.md).

## Ejecutar el proyecto (resumen)

```bash
# 1. Entorno (una vez): crea el venv e instala todo (runtime + dashboard + mercados + dev).
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,app,markets]"

# 2. Secretos (una vez): copia la plantilla y añade las claves que tengas.
cp config/.env.example config/.env    # FRED_API_KEY, BINANCE_API_KEY/SECRET (read-only), TELEGRAM_*

# 3. Ingesta: descarga datos de todas las fuentes a la DB (idempotente).
python run_ingest.py                  # o: python run_ingest.py --dry-run  (solo crea la DB)

# 4. Dashboard: abre el panel de 5 secciones (pull, sin auto-refresh).
./run_dashboard.sh                    # http://localhost:8501

# 5. Alertas: evalúa las reglas y envía a Telegram (dry-run si no hay bot).
python run_alerts.py                  # o --dry-run para solo loguear

# 6. Validación: informe honesto de backtest (retornos forward + bootstrap).
python run_validation.py

# 7. Todo en uno (para cron/systemd): ingesta -> alertas (+ validación los domingos).
./run_daily.sh                        # automatización diaria: ver deploy/README.md

# Tests + lint.
pytest && ruff check .
```

## Estado

**Fases 0-3 completadas.** Ver la tabla de fases en [CLAUDE.md](CLAUDE.md) §12.

Incluye hasta ahora:
- Estructura de paquetes (`core`, `ingest`, `db`, `transform`, `alerts`, `validation`, `app`).
- Configuración externalizada: `config/settings.yaml` (activos §5, umbrales, IDs verificados,
  metadatos de series FRED, categorías de tesis, exchanges de derivados, scraper ETF) y
  `config/assets_meta.yaml` (logos, descripciones y `next_unlock` por token).
- Esquema SQLite en formato largo (`db/schema.sql`) — portable a PostgreSQL/TimescaleDB.
- Loader idempotente (`db/loader.py`) con `INSERT ... ON CONFLICT DO UPDATE`.
- Ingesta multi-fuente: **FRED** (macro con `ts`/`ts_release` point-in-time), **CoinGecko**
  (snapshot + dominancia de mercado total), **DefiLlama** (TVL histórico), **derivados**
  (ccxt: funding, open interest, cierre del perp) y **flujos ETF** (scraper Farside).
- Indicadores: dominancia BTC (+ variación 30 d/1 año), variación 24h/7d/30d, distancia al ATH,
  MC/TVL, dilución, **funding z-score (90 d)**, **estado del rally** (divergencia precio/OI) y
  **racha de flujos ETF**.
- **Sincronización de cuenta Binance (solo lectura):** holdings reales (spot + funding +
  Simple Earn, valorando WBETH por su precio propio) + historial de operaciones (precio,
  comisiones) para el nivel 4; nunca opera ni retira. Requiere una API key **read-only** en
  `config/.env` (ver `.env.example`); si falta, se omite. El capital no legible con key de solo
  lectura (p. ej. grid trading bots) se registra a mano en `binance_account.manual_holdings`.
- Dashboard Streamlit con 5 secciones (**Macro / Radar / Estructura de mercado / Tesis /
  Ejecución**): tablas interactivas (ordenar/reordenar/ocultar), colores y flechas ▲▼, logos,
  y tooltips de efecto en cripto. Pull, sin auto-refresh (§2).
- **Alertas** (`run_alerts.py`): motor declarativo (`alerts.rules` en config) — racha de salidas
  ETF, funding z extremo, caída de TVL, unlock próximo, recordatorio de aporte mensual — con
  entrega por Telegram (dry-run si no hay bot) y dedup por `alerts_log`.
- **Validación de señales** (`run_validation.py`): retornos forward 7/30/90 d, baseline y test de
  significancia por bootstrap; z-scores point-in-time (sin look-ahead, §9). Ver *Validación* abajo.
- Logging estructurado con nivel configurable por env var (`LOG_LEVEL`).
- Tests (`pytest`, 113) de esquema, idempotencia, config, indicadores, rally-quality, alertas,
  validación, parsers de ingesta (incl. fixture Farside congelado), holdings y humo de render.

## Requisitos

- Python 3.11+

## Instalación

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # runtime + tooling de tests
# Extras opcionales por fase:
#   pip install -e ".[app]"      # Streamlit (dashboard, fase 1)
#   pip install -e ".[markets]"  # ccxt / scraping (fase 2)
```

## Configuración

```bash
cp config/.env.example config/.env
# Editar config/.env y añadir FRED_API_KEY (gratuita) cuando se llegue a la fase 1.
```

Los secretos van solo en `config/.env` (ignorado por git). El resto de la configuración
—activos, umbrales, ventanas, IDs— vive en `config/settings.yaml`. Nada se hardcodea en
código (ver [CLAUDE.md](CLAUDE.md) §10).

> **IDs de activos:** cada `coingecko_id` / `defillama` en `settings.yaml` está marcado
> `verified: false`. Confirmar cada ID contra la API en vivo antes de confiar en un
> número para una decisión (decisión abierta de §12).

## Uso

```bash
# Crea/verifica la DB sin ingerir nada (criterio de aceptación de la Fase 0).
python run_ingest.py --dry-run

# Ingesta real. CoinGecko y DefiLlama no requieren key; el macro de FRED
# se puebla solo si FRED_API_KEY está configurada (si no, se salta con warning).
python run_ingest.py

# Dashboard (requiere el extra ".[app]"). Lanzador de un comando (usa el venv):
./run_dashboard.sh                    # http://localhost:8501
# o directamente:
streamlit run app/dashboard.py

# Alertas (Fase 3): evalúa reglas y envía a Telegram (dry-run si no hay bot).
python run_alerts.py --dry-run        # evaluar y loguear, sin enviar
python run_alerts.py                  # enviar nuevas alertas (necesita TELEGRAM_*)

# Validación de señales (Fase 3): informe honesto de backtest.
python run_validation.py --z 1.5 --horizon 7
```

> Para la ingesta de **derivados y flujos ETF** (Fase 2) instala el extra
> `pip install -e ".[markets]"` (ccxt, lxml, websockets).

`run_ingest.py` es idempotente: ejecutarlo dos veces no duplica filas. El panel es
*pull* — refleja el último `run_ingest.py`; no hay auto-refresh (decisión de diseño, §2).

## Validación de señales (resultado preliminar)

> Documentar los resultados de validación **incluidos los que no funcionan** es parte del
> proyecto (§8). Esto es un resultado *preliminar y honesto*, no una recomendación.

**Señal probada:** funding z-score alto (≥1.0, ventana 90 d, *point-in-time*) → retorno a 7 días
del cierre del perp, contra baseline de todas las fechas, con p-valor por bootstrap (permutación).
**Hipótesis:** largos hacinados (funding alto) → corrección → *edge* negativo.

| Activo | n | señal % | base % | edge (pp) | p |
|---|---:|---:|---:|---:|---:|
| ETH | 12 | +1.91 | +5.59 | **−3.68** | 0.019 |
| LINK | 19 | +1.91 | +4.70 | **−2.79** | 0.023 |
| SUI | 26 | +0.12 | +2.86 | **−2.74** | 0.038 |
| XLM | 22 | −1.72 | +1.76 | −3.49 | 0.185 |
| BTC | 29 | +2.22 | +2.63 | −0.41 | 0.572 |
| SOL | 12 | +3.52 | +2.59 | +0.93 | 0.748 |
| UNI | 30 | +7.97 | +7.54 | +0.43 | 0.803 |

**Conclusión honesta:** el *edge* es **mayormente negativo** (coherente con la teoría: funding
alto precede a retornos más débiles), y tres activos cruzan p<0.05. **Pero no es concluyente:**
con ~30 días de historial las muestras son diminutas, los p-valores poco potentes, hay *multiple
testing* (11 activos → se esperan falsos positivos), y SOL/UNI van en contra. **No accionable aún.**
Reejecutar `python run_validation.py` al acumular meses de datos. *(Cifras del 2026-07-23; cambiarán
al crecer el historial.)*

## Tests

```bash
pytest            # suite completa (120)
ruff check .      # linting
```

## Inspeccionar la base de datos

### SQLite (local — incluye la cuenta real)

```bash
# Usa la ruta que resuelve la raíz del repo: sqlite3 CREA un fichero vacío si no la
# encuentra (estar en otra carpeta = "no such table: observations").
sqlite3 "$(git -C . rev-parse --show-toplevel)/cryptodash.db"
```

Dentro del prompt `sqlite>`:

```sql
.headers on
.mode column
.tables                                                 -- observations, trades, events, dca_plan, exit_rules, alerts_log
SELECT source, COUNT(*) FROM observations GROUP BY source;
SELECT source, series_id, ts, value FROM observations ORDER BY ts DESC LIMIT 20;
SELECT * FROM trades ORDER BY ts DESC LIMIT 10;         -- operaciones reales (solo local)
SELECT * FROM alerts_log ORDER BY fired_at DESC LIMIT 10;
.quit
```

### Neon (Postgres — datos públicos, sin cuenta)

- **Web (recomendado):** Neon Console → tu proyecto → **SQL Editor** / **Tables**. Cero instalación.
- **Terminal** (`psycopg` viene con el extra `.[postgres]`):

```bash
DATABASE_URL="postgresql://…neon…?sslmode=require" python - <<'PY'
from pathlib import Path
from db.database import open_connection
c = open_connection(Path("cryptodash.db"))      # DATABASE_URL presente -> usa Neon
print("tablas:", [r[0] for r in c.execute(
    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")])
print("observations:", c.execute("SELECT COUNT(*) FROM observations").fetchone()[0])
print("holdings (debe ser 0):", c.execute(
    "SELECT COUNT(*) FROM observations WHERE series_id LIKE '%:balance%'").fetchone()[0])
print("trades   (debe ser 0):", c.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
c.close()
PY
```

- Conserva `?sslmode=require`. `holdings` y `trades` deben dar **0**: la ingesta a Neon usa
  `run_ingest.py --public` y **nunca** sube la cuenta de Binance (§ seguridad).

## Estructura

```
cryptodash/
├── core/            # config (settings.yaml + assets_meta.yaml + .env) y logging
├── config/          # settings.yaml · assets_meta.yaml · .env.example
├── db/              # schema.sql (formato largo) · loader idempotente · queries (lectura)
├── ingest/          # base · fred · coingecko · defillama · derivatives · etf_flows · binance_account
├── transform/       # indicators.py · rally_quality.py (funciones puras + tablas)
├── alerts/          # rules.py (motor declarativo) · telegram.py
├── validation/      # metrics.py (forward return, bootstrap) · backtest.py
├── app/             # dashboard.py (Streamlit, 5 secciones interactivas)
├── tests/           # pytest + fixtures/ (fixture HTML congelado de Farside)
├── run_ingest.py    # pipeline de ingesta · run_alerts.py · run_validation.py
└── run_dashboard.sh # lanzador del dashboard (un comando)
```

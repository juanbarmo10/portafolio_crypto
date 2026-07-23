# cryptodash — Dashboard cripto-macro

Panel personal de análisis cripto-macro con fuentes de datos **gratuitas**. Ingesta
macro (FRED), precios (CoinGecko) y TVL (DefiLlama) en SQLite, calcula indicadores
derivados y los presenta en Streamlit, con alertas por Telegram y un framework de
validación estadística de señales.

El contexto completo del proyecto (filosofía de diseño, activos, tesis, hoja de ruta
por fases y trampas del dominio) vive en [CLAUDE.md](CLAUDE.md). Las **decisiones técnicas
y ecuaciones** (formulación, efecto en cripto, uso y qué validar) están en [RESEARCH.md](RESEARCH.md).

## Estado

**Fases 0, 1 y 2 completadas.** Ver la tabla de fases en [CLAUDE.md](CLAUDE.md) §12.

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
- Logging estructurado con nivel configurable por env var (`LOG_LEVEL`).
- Tests (`pytest`, 85) de esquema, idempotencia, config, indicadores, rally-quality, parsers de
  ingesta (incl. fixture Farside congelado), helpers del dashboard y humo de render (AppTest).

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
```

> Para la ingesta de **derivados y flujos ETF** (Fase 2) instala el extra
> `pip install -e ".[markets]"` (ccxt, lxml, websockets).

`run_ingest.py` es idempotente: ejecutarlo dos veces no duplica filas. El panel es
*pull* — refleja el último `run_ingest.py`; no hay auto-refresh (decisión de diseño, §2).

## Tests

```bash
pytest            # suite completa
ruff check .      # linting
```

## Estructura

```
cryptodash/
├── core/            # config (settings.yaml + assets_meta.yaml + .env) y logging
├── config/          # settings.yaml · assets_meta.yaml · .env.example
├── db/              # schema.sql (formato largo) · loader idempotente · queries (lectura)
├── ingest/          # base · fred · coingecko · defillama · derivatives · etf_flows · binance_account
├── transform/       # indicators.py · rally_quality.py (funciones puras + tablas)
├── alerts/          # reglas + Telegram (fase 3)
├── validation/      # backtest / métricas (fase 3)
├── app/             # dashboard.py (Streamlit, 5 secciones interactivas)
├── tests/           # pytest + fixtures/ (fixture HTML congelado de Farside)
├── run_ingest.py    # punto de entrada del pipeline
└── run_dashboard.sh # lanzador del dashboard (un comando)
```

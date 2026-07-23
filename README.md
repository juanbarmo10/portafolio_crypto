# cryptodash — Dashboard cripto-macro

Panel personal de análisis cripto-macro con fuentes de datos **gratuitas**. Ingesta
macro (FRED), precios (CoinGecko) y TVL (DefiLlama) en SQLite, calcula indicadores
derivados y los presenta en Streamlit, con alertas por Telegram y un framework de
validación estadística de señales.

El contexto completo del proyecto (filosofía de diseño, activos, tesis, hoja de ruta
por fases y trampas del dominio) vive en [CLAUDE.md](CLAUDE.md).

## Estado

**Fases 0 y 1 completadas.** Ver la tabla de fases en [CLAUDE.md](CLAUDE.md) §12.

Incluye hasta ahora:
- Estructura de paquetes (`core`, `ingest`, `db`, `transform`, `alerts`, `validation`, `app`).
- Configuración externalizada en `config/settings.yaml` (activos §5, umbrales, IDs verificados).
- Esquema SQLite en formato largo (`db/schema.sql`) — portable a PostgreSQL/TimescaleDB.
- Loader idempotente (`db/loader.py`) con `INSERT ... ON CONFLICT DO UPDATE`.
- Ingesta multi-fuente: **FRED** (macro con `ts`/`ts_release` point-in-time), **CoinGecko**
  (snapshot batch + dominancia de mercado total) y **DefiLlama** (TVL histórico por protocolo y cadena).
- Indicadores derivados: dominancia BTC, variación 24h/7d/30d, distancia al ATH, MC/TVL, dilución.
- Dashboard Streamlit con 4 secciones (Macro / Cartera / Tesis / Ejecución), pull, sin auto-refresh.
- Logging estructurado con nivel configurable por env var (`LOG_LEVEL`).
- Tests (`pytest`, 37) de esquema, idempotencia, config, indicadores y parsers de ingesta.

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

# Dashboard (requiere el extra ".[app]").
streamlit run app/dashboard.py
```

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
├── core/           # config (settings.yaml + .env) y logging estructurado
├── config/         # settings.yaml + .env.example
├── db/             # schema.sql (formato largo) + loader idempotente
├── ingest/         # base.py (clase abstracta + retry/backoff); fuentes en fase 1+
├── transform/      # indicadores derivados (fase 1+)
├── alerts/         # reglas + Telegram (fase 3)
├── validation/     # backtest / métricas (fase 3)
├── app/            # dashboard Streamlit (fase 1)
├── tests/          # pytest: esquema, idempotencia, config
└── run_ingest.py   # punto de entrada del pipeline
```

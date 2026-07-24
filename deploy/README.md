# Orquestación (Fase 4)

Ejecuta a diario `run_daily.sh` = **ingesta → alertas** (+ validación los domingos), con log en
`logs/daily.log`. El panel es *pull*: refleja el último run.

## Opción recomendada — systemd user timer (con catch-up)

`Persistent=true` recupera una ejecución perdida la próxima vez que el equipo se encienda, así que
**no hace falta tener el PC encendido 24/7**, solo una vez al día.

```bash
# desde la raíz del repo
chmod +x run_daily.sh
mkdir -p ~/.config/systemd/user
cp deploy/cryptodash.service deploy/cryptodash.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cryptodash.timer
loginctl enable-linger "$USER"     # ejecutar aunque no haya sesión iniciada
```

Comprobar / operar:
```bash
systemctl --user list-timers cryptodash.timer   # próxima ejecución
systemctl --user start cryptodash.service       # ejecutar ahora (prueba)
journalctl --user -u cryptodash.service -n 50    # logs del servicio
tail -f logs/daily.log                           # salida del pipeline
```

Cambiar la hora: editar `OnCalendar` en `~/.config/systemd/user/cryptodash.timer`
(`OnCalendar=*-*-* 13:00:00`), luego `systemctl --user daemon-reload && systemctl --user restart cryptodash.timer`.

Desinstalar:
```bash
systemctl --user disable --now cryptodash.timer
rm ~/.config/systemd/user/cryptodash.{service,timer}
systemctl --user daemon-reload
```

## Alternativa — cron

```bash
crontab -e
# añade (13:00 cada día); cron NO recupera ejecuciones perdidas (usa el timer si eso importa):
0 13 * * * /home/juanb/Research_Lab/projects/portafolio_crypto/run_daily.sh
```

## Despliegue del dashboard (Streamlit Community Cloud + Neon)

Arquitectura: tu **cron local** escribe datos **públicos** en una **Postgres gestionada (Neon)**;
la app en **Streamlit Community Cloud** lee esa DB en modo público (sin la cartera real). El backend
se elige solo con la variable `DATABASE_URL` (si está → Postgres; si no → SQLite local).

> **Privacidad:** la cartera de Binance **nunca** sale de tu máquina. La ingesta hacia Neon usa
> `run_ingest.py --public` (omite la cuenta), y la app usa `PUBLIC_MODE=1` (oculta el nivel 4).

### 1. Base de datos gestionada (Neon, gratis)
Crea un proyecto en [neon.tech], copia la cadena de conexión (`postgresql://...`). Esa es tu
`DATABASE_URL`.

### 2. Poblar Neon con datos públicos (desde tu máquina)
```bash
pip install -e ".[markets,postgres]"
DATABASE_URL="postgresql://...neon..." python run_ingest.py --public   # crea esquema + puebla
```
Para mantenerla al día, añade un timer/cron que ejecute lo anterior a diario (igual que
`cryptodash.timer`, pero con `DATABASE_URL` en el entorno del servicio y `--public`).

### 3. Subir el repo a GitHub
```bash
git remote add origin git@github.com:<usuario>/portafolio_crypto.git
git push -u origin main
```
`.env`, `*.db` y `logs/` están en `.gitignore` — no se suben secretos ni datos.

### 4. App en Streamlit Community Cloud
- [share.streamlit.io] → *New app* → tu repo, rama, archivo `app/dashboard.py`.
- Dependencias: usa `requirements.txt` (mínimo, solo lectura).
- **Secrets** (⚙️ → Secrets), formato TOML:
  ```toml
  DATABASE_URL = "postgresql://...neon..."
  PUBLIC_MODE = "1"
  ```
  El dashboard expone los secrets como variables de entorno, así que `core.config` los recoge.

Resultado: un enlace público que muestra Macro / Radar / Estructura de mercado / Tesis (sin la
cartera real), con los datos que tu cron sube a Neon.

## Nube total (cron también sin tu PC) — opcional

Para que la **ingesta pública** corra sin tu equipo, un workflow de **GitHub Actions** (cron gratis)
puede ejecutar `run_ingest.py --public` contra Neon con `DATABASE_URL` como *secret* del repo.
**Nunca** pongas claves de cuenta de Binance en un runner alojado (§ seguridad).

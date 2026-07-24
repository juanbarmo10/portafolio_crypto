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

## Nube (sin PC encendido) — pendiente

La única forma de correr con el PC apagado es un runner en la nube (GitHub Actions, gratis):
requiere subir el repo a GitHub y configurar secretos. **No** poner claves de cuenta de Binance en
un runner alojado (§ seguridad); allí solo ingesta pública + alertas, o una DB gestionada. Se
aborda en el paso de despliegue de la Fase 4.

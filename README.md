# Diputetacam

Cámara virtual para Linux con un **loop natural de los últimos 20 segundos**. Pensada para Meet, Teams, Zoom y cualquier aplicación que consuma V4L2, sin OBS.

La idea es que al activar el loop la cámara física se apague y la reunión siga viendo un fragmento reciente de ti. El corte no se hace siempre al frame más viejo: se elige el frame de los últimos 15–20 s que más se parece al frame actual, para que el freeze-cut y el borde del loop pasen entre imágenes casi idénticas.

## Estado

MVP funcional:

- Salida de cámara virtual con `v4l2loopback`.
- Buffer en memoria de 20 s en JPEG.
- Match cut de 15–20 s.
- Transición **freeze-cut** de entrada y salida.
- Liberación de la cámara física al entrar en loop y pre-roll al volver.
- CLI, GUI GTK4, servicio systemd de usuario.
- Modo de prueba con patrón de vídeo y `fakesink` (sin cámara ni kernel module).

Todavía no incluye captura de pantalla, audio, blur por IA ni múltiples cámaras virtuales simultáneas.

## Requisitos

- Arch Linux / Omarchy (el instalador asume `pacman`).
- Python 3.11+.
- GStreamer 1.20+ con `gst-plugins-base`, `gst-plugins-good` y `gst-plugins-bad`.
- PyGObject, GTK4 y libadwaita.
- `v4l2loopback-dkms` y `v4l2loopback-utils`.
- Kernel headers (`linux-omarchy-headers` en Omarchy).

## Instalación

```bash
git clone git@github.com:skalanux/diputetacam.git
cd diputetacam

# Instala v4l2loopback y crea /dev/video10
sudo ./contrib/setup-v4l2loopback.sh

# Enlaces en ~/.local/bin, unidad systemd y .desktop
./install-user.sh

systemctl --user enable --now diputetacamd

diputetacam doctor
diputetacam live   # arranca la cámara y el buffer (20 s)
```

El daemon arranca en **standby** por defecto: la cámara física está apagada y Meet/Teams ven la tarjeta de “cámara apagada”. `diputetacam live` enciende la cámara y empieza a llenar el buffer. Si prefieres que arranque en vivo, pon `[startup] live = true` en la configuración.

`setup-v4l2loopback.sh` escribe:

```ini
# /etc/modprobe.d/99-diputetacam.conf
options v4l2loopback devices=1 video_nr=10 card_label="Virtual Camera" exclusive_caps=1 max_buffers=4
```

y carga el módulo. Si ya estaba cargado y no se puede descargar, reinicia o cierra las aplicaciones que estén usando la cámara virtual.

### Secure Boot

Si tienes Secure Boot activo, el módulo DKMS tiene que estar firmado. En Omarchy el Secure Boot de esta máquina está desactivado, así que no hace falta. Si lo activas, firma el módulo con `sbctl` o la herramienta de tu distribución.

## Uso

```bash
diputetacam status
diputetacam devices

diputetacam loop     # entra en el loop y libera la cámara física
diputetacam live     # vuelve a la cámara real
diputetacam toggle   # alterna live/loop
diputetacam standby  # tarjeta de "cámara apagada"
```

GUI:

```bash
diputetacam-gui
```

En Meet/Teams/Zoom elige **Virtual Camera** como cámara. Si la app ya estaba abierta antes que el daemon, reiníciala o recarga la página: muchos navegadores cachean la lista de dispositivos.

### Hyprland

```ini
# ~/.config/hypr/bindings.conf (o tu archivo de binds)
bind = SUPER SHIFT, L, exec, diputetacam loop
bind = SUPER SHIFT, K, exec, diputetacam live
bind = SUPER ALT, L, exec, diputetacam toggle
```

También puedes poner el estado en Waybar o en tu barra con:

```bash
diputetacam --json status
```

## Cómo funciona

```
Cámara ─┐
Loop ───┼──► compositor 1280x720@30 ──► tee ──► v4l2sink /dev/video10
Standby ┘                                ├──► jpegenc ──► buffer JPEG 20 s
                                         └──► 32x18 GRAY8 ──► thumbnails

Entrada al loop:
  LIVE → freeze 150 ms → cut al frame de inicio emparejado → LOOP

Vuelta a live:
  LOOP → arranca cámara en segundo plano → freeze 150 ms → cut a LIVE
```

- El pipeline de salida **nunca se reinicia**; solo cambian las fuentes dentro del compositor. Así Meet/Teams no pierden la cámara.
- El buffer guarda JPEG, no vídeo crudo: 20 s a 30 fps son unos 60–90 MB con una cámara real.
- El match cut compara thumbnails de 32×18 en gris y elige el frame de hace 15–20 s más parecido al actual. Si no encuentra uno suficientemente bueno, usa los 20 s completos y hace un corte seco.
- La cámara se libera después de que el freeze sea visible; el LED se apaga. Al volver, se arranca mientras el loop sigue y se corta cuando llega el primer frame.

## Configuración

`~/.config/diputetacam/config.toml` (se crea con defaults la primera vez):

```toml
[output]
device = "/dev/video10"
width = 1280
height = 720
fps = 30
format = "YUY2"

[camera]
device = ""          # vacío = primera cámara V4L2 encontrada
width = 1280
height = 720
fps = 30

[buffer]
seconds = 20
min_loop_seconds = 15
max_loop_seconds = 20
jpeg_quality = 80
max_memory_mb = 256
match_threshold = 24.0

[transition]
freeze_ms = 150

[startup]
live = false
```

Reinicia el daemon después de editar:

```bash
systemctl --user restart diputetacamd
```

## Troubleshooting

**No aparece `/dev/video10`**

```bash
diputetacam doctor
lsmod | grep v4l2loopback
sudo modprobe v4l2loopback
```

Si el módulo se cargó antes de escribir `/etc/modprobe.d/99-diputetacam.conf`, recárgalo:

```bash
sudo modprobe -r v4l2loopback
sudo modprobe v4l2loopback
```

**La cámara virtual no aparece en el navegador**

- Arranca el daemon antes que el navegador.
- Reinicia el navegador o recarga la pestaña.
- En Flatpak, comprueba que la app tenga acceso a dispositivos (`--device=all` o el portal correspondiente).
- Revisa `chrome://media-internals` o los permisos de cámara del navegador.

**La cámara física sigue encendida**

Algunas cámaras UVC no apagan el LED hasta que se cierra el dispositivo o se suspende el USB. Comprueba que la app no esté usando la cámara física; el daemon la libera al entrar en loop. Si el LED sigue encendido, tapa la cámara o desconéctala.

**El módulo deja de compilar tras actualizar el kernel**

```bash
sudo dkms autoinstall
sudo modprobe v4l2loopback
```

**No hay sonido en el loop**

Es intencionado: el loop es solo vídeo. El micrófono de la reunión sigue siendo el real.

## Desarrollo

Modo de prueba, sin cámara ni `v4l2loopback`:

```bash
./bin/diputetacamd --test
./bin/diputetacam --test status
./bin/diputetacam --test loop
./bin/diputetacam --test live
```

Tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

El daemon en modo test usa `fakesink` y una fuente `videotestsrc`; el socket es `diputetacam-test.sock`.

## Limitaciones y uso responsable

- El loop repite un fragmento: en una reunión larga se puede notar. 20 s cubren una pausa corta; 30–45 s se notan menos, a cambio de más RAM.
- No hay audio, blur por IA, captura de pantalla ni múltiples salidas todavía.
- Simular presencia puede ir contra las normas de tu trabajo o estudio. Úsalo solo donde esté permitido y con criterio.

## Licencia

MIT.

# CamSec – Eines d'escriptori

> 🌐 **Idioma / Language:** [Català](#camsec--eines-descriptori) · [English](#desktop-tools--english)

---

Eines locals per interactuar amb el sistema CamSec sense necessitar el servidor Docker.

---

## `cam_viewer.py` – Visor en temps real

Aplicació d'escriptori (tkinter + Pillow) que es subscriu als topics MQTT de les càmeres
i mostra les imatges en directe en una graella. Funciona de manera independent: no requereix
el detector YOLOv8 ni el viewer web.

### Funcionalitats

- Graella automàtica (fins a 3 columnes) — una targeta per càmera.
- Actualització en temps real sense bloquejar la interfície (fil MQTT separat).
- Mostra resolució original, mida en KB i número de frame.
- Indicador 🌙 quan la càmera detecta poca llum (mode flash actiu).
- Indicador de connexió MQTT (verd = connectat, groc = error / reconnectant).
- Suporta múltiples càmeres simultànies.
- Reconnexió automàtica si el broker MQTT no és accessible.

### Dependències

```bash
pip install paho-mqtt Pillow
```

### Configuració

```bash
cp .env.example .env
```

Edita `.env` amb els valors del teu entorn:

```env
MQTT_BROKER=192.168.1.10   # IP o hostname del broker Mosquitto
MQTT_PORT=1883
MQTT_USER=                  # deixa buit si el broker és anònim
MQTT_PASS=
```

> El fitxer `.env` **no s'ha de pujar al repositori**. El `.env.example` és la plantilla segura.

### Execució

```bash
# Llegeix la configuració del .env automàticament
python cam_viewer.py

# O amb arguments de línia de comandes (prioritat sobre el .env)
python cam_viewer.py --broker <IP> [--port 1883] [--user <USUARI>] [--password <CLAU>]

# Ajuda
python cam_viewer.py --help
```

### Topics MQTT escoltats

| Topic | Descripció |
|---|---|
| `<prefix>/image/begin` | Inici de chunk d'imatge |
| `<prefix>/image/data` | Dades binàries del chunk |
| `<prefix>/image/end` | Fi d'imatge — reassembla i mostra el JPEG |

Els topics d'alerta (`*/alert/*`) s'ignoren — el visor mostra únicament les imatges raw de la càmera.

### Variables d'entorn

| Variable | Per defecte | Descripció |
|---|---|---|
| `MQTT_BROKER` | `localhost` | IP o hostname del broker MQTT |
| `MQTT_PORT` | `1883` | Port TCP del broker |
| `MQTT_USER` | *(buit)* | Usuari MQTT (opcional) |
| `MQTT_PASS` | *(buit)* | Contrasenya MQTT (opcional) |

---

## `mqtt_same_probe.py` – Prova de deduplicació MQTT

Script de diagnòstic per mesurar en viu:
- Frames complets (`image/begin`+`image/end`)
- Frames deduplicats `image/same` en mode `exact` i `quasi`
- Percentatge d'estalvi (ratio de `same`)
- Avisos del detector a `<prefix>/alert/same`

### Execució

```bash
python mqtt_same_probe.py --broker 192.168.1.10 --duration 120

# Només una càmera
python mqtt_same_probe.py --broker 192.168.1.10 --prefix cam/01 --duration 90
```

Si existeix `tools/.env`, l'script el carrega automàticament (pots ometre `--broker`, `--user`, etc.).

### Paràmetres

| Argument | Per defecte | Descripció |
|---|---|---|
| `--broker` | `MQTT_BROKER` o `localhost` | Host del broker MQTT |
| `--port` | `MQTT_PORT` o `1883` | Port del broker |
| `--prefix` | *(buit)* | Filtra una sola càmera (`cam/01`) |
| `--duration` | `120` | Durada de la prova en segons (`0` = infinit) |
| `--print-every` | `10` | Interval del resum per consola |

---

---

# Desktop Tools — English

Standalone local tools for interacting with the CamSec system without needing the Docker server.

---

## `cam_viewer.py` – Live camera viewer

A desktop application (tkinter + Pillow) that subscribes to the cameras' MQTT topics and
displays live images in a grid. It works independently — no YOLOv8 detector or web viewer required.

### Features

- Automatic grid (up to 3 columns) — one card per camera.
- Real-time updates without UI freezes (MQTT runs on a separate thread).
- Shows original resolution, size in KB, and frame counter.
- 🌙 indicator when the camera detects low light (flash mode active).
- MQTT connection indicator (green = connected, yellow = error / reconnecting).
- Supports multiple simultaneous cameras.
- Automatic reconnection if the MQTT broker is unreachable.

### Dependencies

```bash
pip install paho-mqtt Pillow
```

### Configuration

```bash
cp .env.example .env
```

Edit `.env` with your environment values:

```env
MQTT_BROKER=192.168.1.10   # IP or hostname of the Mosquitto broker
MQTT_PORT=1883
MQTT_USER=                  # leave empty if the broker is anonymous
MQTT_PASS=
```

> The `.env` file **must not be committed to the repository**. Use `.env.example` as the safe template.

### Running

```bash
# Reads configuration from .env automatically
python cam_viewer.py

# Or with explicit command-line arguments (override .env)
python cam_viewer.py --broker <IP> [--port 1883] [--user <USER>] [--password <PASS>]

# Help
python cam_viewer.py --help
```

### MQTT topics listened

| Topic | Description |
|---|---|
| `<prefix>/image/begin` | Start of image chunk stream |
| `<prefix>/image/data` | Binary chunk data |
| `<prefix>/image/end` | End of image — reassembles and displays the JPEG |

Alert topics (`*/alert/*`) are ignored — the viewer only shows raw camera images.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `MQTT_BROKER` | `localhost` | IP or hostname of the MQTT broker |
| `MQTT_PORT` | `1883` | TCP port of the broker |
| `MQTT_USER` | *(empty)* | MQTT username (optional) |
| `MQTT_PASS` | *(empty)* | MQTT password (optional) |

---

## `mqtt_same_probe.py` – MQTT dedup test probe

Diagnostic script to measure in real time:
- Full frames (`image/begin`+`image/end`)
- Deduplicated frames via `image/same` (`exact` and `quasi`)
- Dedup ratio (`same` over total signaled frames)
- Detector warnings from `<prefix>/alert/same`

### Run

```bash
python mqtt_same_probe.py --broker 192.168.1.10 --duration 120

# Single camera only
python mqtt_same_probe.py --broker 192.168.1.10 --prefix cam/01 --duration 90
```

If `tools/.env` exists, the script auto-loads it (so you can omit `--broker`, `--user`, etc.).

### Arguments

| Argument | Default | Description |
|---|---|---|
| `--broker` | `MQTT_BROKER` or `localhost` | MQTT broker host |
| `--port` | `MQTT_PORT` or `1883` | MQTT broker port |
| `--prefix` | *(empty)* | Restrict to one camera prefix (`cam/01`) |
| `--duration` | `120` | Probe duration in seconds (`0` = infinite) |
| `--print-every` | `10` | Console summary interval |

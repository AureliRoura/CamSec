# CamSec – Servidor de detecció

> 🌐 **Idioma / Language:** [Català](#camsec--servidor-de-detecció) · [English](#camsec-server--english)

---

Contenidor Docker que subscriu als streams MQTT de les càmeres ESP32-CAM,
reassembla les imatges JPEG, executa **YOLOv8** per detectar persones i
publica alertes MQTT amb la imatge anotada (requadres i etiquetes).

---

## Estructura

```
server/
├── .env.example            ← plantilla de configuració (copia a .env)
├── docker-compose.yml      ← definició del servei
└── detector/
    ├── Dockerfile
    ├── requirements.txt
    └── detector.py         ← lògica principal
```

---

## Requisits

| Requisit | Versió mínima |
|---|---|
| Docker Engine | 24 |
| Docker Compose | v2.20 |
| SO recomanat | Linux (per `network_mode: host`) |

> El servei **no inclou** broker MQTT. Cal un broker Mosquitto (o compatible)
> accessible per xarxa. Veure secció [Broker MQTT](#broker-mqtt).

---

## Configuració ràpida

### 1. Crea el fitxer `.env`

```bash
cp .env.example .env
```

Edita `.env` amb els valors del teu entorn:

```dotenv
# IP o hostname del broker MQTT extern
CAMSEC_MQTT_BROKER=192.168.1.10
CAMSEC_MQTT_PORT=1883

# Credencials MQTT (deixa buit si el broker és anònim)
DETECTOR_MQTT_USER=
DETECTOR_MQTT_PASS=

# Nivell de logs per servei
DETECTOR_LOG_LEVEL=INFO
VIEWER_LOG_LEVEL=INFO
```

### 2. Construeix i aixeca el contenidor

```bash
docker compose up --build -d
```

La primera construcció descarrega PyTorch + Ultralytics (~1,5 GB) i el model
`yolov8n.pt` (~6 MB). Les següents arrencades usen la caché i tarden ~10 s.

### 3. Comprova que funciona

```bash
# Segueix els logs en temps real
docker compose logs -f detector

# Comprova l'estat MQTT (requereix mosquitto-clients al host)
mosquitto_sub -h 192.168.1.10 -t "detector/status" -v
mosquitto_sub -h 192.168.1.10 -t "+/alert" -v
```

---

## Variables d'entorn

Totes es defineixen al fitxer `.env`. El `docker-compose.yml` les passa
automàticament al contenidor.

| Variable | Valor per defecte | Descripció |
|---|---|---|
| `CAMSEC_MQTT_BROKER` | `localhost` | IP o hostname del broker |
| `CAMSEC_MQTT_PORT` | `1883` | Port TCP del broker |
| `DETECTOR_MQTT_USER` | *(buit)* | Usuari MQTT del detector (opcional) |
| `DETECTOR_MQTT_PASS` | *(buit)* | Contrasenya MQTT del detector (opcional) |
| `VIEWER_MQTT_USER` | *(buit)* | Usuari MQTT del viewer (opcional) |
| `VIEWER_MQTT_PASS` | *(buit)* | Contrasenya MQTT del viewer (opcional) |
| `DETECTOR_LOG_LEVEL` | `INFO` | Nivell de log del detector: `DEBUG` · `INFO` · `WARNING` · `ERROR` |
| `VIEWER_LOG_LEVEL` | `INFO` | Nivell de log del viewer: `DEBUG` · `INFO` · `WARNING` · `ERROR` |
| `YOLO_MODEL` | `/models/yolov8n.pt` | Model YOLO: `n`=nano · `s`=small · `m`=medium |
| `YOLO_IMGSZ` | `320` | Mida d'entrada al model (px); valors més petits → menys RAM |
| `CONFIDENCE` | `0.45` | Llindar de confiança de detecció (0.0–1.0) |
| `CHUNK_SIZE` | `4096` | Mida del chunk en bytes (ha de coincidir amb la càmera) |
| `STALE_TTL_S` | `60` | Descarta buffers d'imatge incomplets passats N segons |
| `SAME_PERSON_ALERT_THRESHOLD` | `5` | Publica `<prefix>/alert/same` quan hi ha N `image/same` seguits després d'haver detectat persones |
| `SAME_PERSON_ALERT_STEP` | `5` | Repetició de l'avís cada N nous `image/same` addicionals |
| `MAX_ALERTS` | `50` | Màxim d'alertes en memòria RAM al viewer |
| `PERSIST_DIR` | `/data/alerts` | Ruta dins del contenidor on es desen les imatges |
| `MAX_DISK_ALERTS` | `500` | Número màxim d'imatges a conservar al disc |
| `MAX_DISK_DAYS` | `7` | Dies màxim de retenció d'imatges |
| `ALERTS_DIR` | `/home/pi/camsec-alerts` | Carpeta del **host** on es munten les alertes (bind mount) |

Per canviar qualsevol paràmetre edita `.env` i reinicia:

```bash
docker compose up -d
```

---

## Persistència d'alertes al disc

El viewer desa cada imatge d'alerta en una carpeta del host i recupera l'historial
complert en reiniciar el contenidor.

### Directori de dades

La variable `ALERTS_DIR` al fitxer `.env` defineix la carpeta del host:

```dotenv
ALERTS_DIR=/home/pi/camsec-alerts   # per defecte
```

Pot ser qualsevol ruta accessible, inclús un disc USB muntat:

```dotenv
ALERTS_DIR=/mnt/usb/camsec-alerts
```

Crea el directori si no existeix:

```bash
mkdir -p "$ALERTS_DIR"
```

### Estructura de fitxers

```
$ALERTS_DIR/
├── index.jsonl          # metadades de cada alerta (una línia JSON per entrada)
└── images/
    ├── 1.jpg
    ├── 2.jpg
    └── …
```

### Retenció automàtica

Cada 10 alertes el viewer neteja les imatges antigues:
- Elimina imatges de més de **`MAX_DISK_DAYS`** dies (per defecte 7).
- Conserva com a màxim **`MAX_DISK_ALERTS`** imatges (per defecte 500).

Per canviar els límits, edita el `.env`:

```dotenv
MAX_DISK_DAYS=14
MAX_DISK_ALERTS=1000
```

I reinicia el viewer:

```bash
docker compose restart viewer
```

---

## Dashboard i API HTTP

Accedeix a **http://\<ip-servidor\>:8088**

- **Vista principal** — una targeta per càmera amb la darrera foto detectada.
- **Historial** — clica qualsevol targeta per veure totes les fotos d'aquella càmera (llegeix del disc, no només de la RAM).
- **Eliminar fotos** — botó 🗑️ a cada foto de l'historial per eliminar-la individualment; botó **Elimina totes** a la capçalera del modal per buidar tota la càmera.
- **Temps real** — s'actualitza automàticament via Server-Sent Events (SSE) sense cal recarregar la pàgina.
- **Embed** — panel mínim per incloure via `<iframe src="http://servidor:8088/embed">`.

### Endpoints REST

| Mètode | Endpoint | Descripció |
|---|---|---|
| `GET` | `/api/alerts` | Llista d'alertes recents en memòria (JSON) |
| `GET` | `/api/alerts/latest` | Darrera alerta per càmera (JSON) |
| `GET` | `/api/alerts/camera/<prefix>` | Historial complet d'una càmera (des de disc) |
| `DELETE` | `/api/alerts/camera/<prefix>` | Elimina totes les fotos d'una càmera (RAM + disc) |
| `GET` | `/api/image/<id>` | Serveix el JPEG d'una alerta concreta |
| `DELETE` | `/api/image/<id>` | Elimina una foto concreta (RAM + disc) |
| `GET` | `/stream` | Server-Sent Events en temps real |
| `GET` | `/embed` | Panel mínim per `<iframe>` |
| `GET` | `/help` | Documentació dels endpoints i variables de configuració (JSON) |

---

### Consumits (per càmera)

| Topic | Format | Descripció |
|---|---|---|
| `<prefix>/image/begin` | JSON | Inici d'imatge: `{id, size, chunks, dark}` |
| `<prefix>/image/data` | Binari | Chunk: `[4B id BE][2B idx BE][dades JPEG]` |
| `<prefix>/image/end` | JSON | Fi d'imatge: `{id, chunks, ok}` |
| `<prefix>/image/same` | JSON | Sense JPEG: `{id, same_as, size, dark, mode[, score]}` |

Quan arriba `<prefix>/image/same`, el detector no executa inferència nova (no hi ha payload JPEG) i el viewer actualitza el timestamp/estat del directe reutilitzant l'últim frame disponible.

### Publicats en detecció

| Topic | Format | Descripció |
|---|---|---|
| `detector/status` | text | `online` / `offline` (retained) |
| `<prefix>/alert` | JSON | Resum de la detecció |
| `<prefix>/alert/same` | JSON | Avís: molts `image/same` seguits després d'una detecció de persones |
| `<prefix>/alert/image/begin` | JSON | Inici d'imatge anotada |
| `<prefix>/alert/image/data` | Binari | Chunk d'imatge anotada |
| `<prefix>/alert/image/end` | JSON | Fi d'imatge anotada |

**Exemple d'alerta JSON** (`cam/01/alert`):
```json
{
  "camera": "cam/01",
  "image_id": 42,
  "ts": 1746518400.123,
  "ts_iso": "2026-05-06T10:00:00Z",
  "persons": 1,
  "dark": false,
  "detections": [
    { "x1": 120, "y1": 80, "x2": 310, "y2": 450, "conf": 0.87 }
  ]
}
```

---

## Broker MQTT

Tant el detector com el viewer es connecten al broker via la xarxa física del host
(`network_mode: host`), sense cap xarxa Docker interna.

| Situació del broker | `CAMSEC_MQTT_BROKER` |
|---|---|
| Servidor remot | `192.168.1.10` (IP de la màquina) |
| Mateix host, fora de Docker | `127.0.0.1` |
| Mateix host, en Docker | `127.0.0.1` (amb `network_mode: host` al broker també) |

> **Windows / macOS**: `network_mode: host` no és compatible amb Docker
> Desktop. Al `docker-compose.yml`, comenta `network_mode: host` del viewer
> i descomenta el bloc `ports` per exposar el port manualment. Usa la IP
> real del host (p. ex. `192.168.1.10`) com a `CAMSEC_MQTT_BROKER` en lloc
> de `127.0.0.1`.

---

## Models YOLO disponibles

| Model | Fitxer | Velocitat | Precisió |
|---|---|---|---|
| Nano | `yolov8n.pt` | ★★★★★ | ★★☆☆☆ |
| Small | `yolov8s.pt` | ★★★★☆ | ★★★☆☆ |
| Medium | `yolov8m.pt` | ★★★☆☆ | ★★★★☆ |
| Large | `yolov8l.pt` | ★★☆☆☆ | ★★★★★ |

Canvia `YOLO_MODEL` al `.env` i reconstrueix:

```bash
docker compose build --no-cache
docker compose up -d
```

---

## Operacions habituals

```bash
# Aturar el servei
docker compose down

# Veure logs de les últimes 100 línies
docker compose logs --tail=100 detector

# Reiniciar sense reconstruir
docker compose restart detector

# Reconstruir i reiniciar (després de canvis al codi)
docker compose up --build -d

# Eliminar el contenidor i la caché del model YOLO
docker compose down -v
```

---

## Seguretat

- Les credencials MQTT **mai** s'han d'escriure al `docker-compose.yml`.
  Usa sempre el fitxer `.env` i no el commitis al repositori.
- Afegeix `.env` al `.gitignore`.
- Per a entorns exposats a Internet usa MQTT sobre TLS (port 8883) i
  configura `WiFiClientSecure` a l'ESP32 i `tls_*` al broker.

---

## Compatibilitat de plataformes

Les imatges Docker són multi-arquitectura (`linux/amd64` + `linux/arm64/v8`).
Docker selecciona automàticament l'arquitectura correcta en fer `docker compose pull`.

| Plataforma | Arquitectura | Notes |
|---|---|---|
| PC / servidor x86 | `linux/amd64` | Cap configuració addicional |
| Raspberry Pi 4/5 | `linux/arm64/v8` | PyTorch CPU-only (sense CUDA) |

> La imatge del detector instal·la `torch` i `torchvision` des de l'index CPU de PyTorch
> (`https://download.pytorch.org/whl/cpu`) per garantir compatibilitat amb ARM sense GPU.

---

---

# CamSec Server — English

Docker services that receive MQTT image streams from ESP32-CAM cameras, run **YOLOv8** person detection, and serve the alert history via a web dashboard.

## Quick start

```bash
cd server
cp .env.example .env          # fill in your MQTT broker IP and credentials
docker compose up -d --build
```

Dashboard: **http://localhost:8088**

## Environment variables

All variables are set in the `.env` file. `docker-compose.yml` passes them automatically to the containers.

| Variable | Default | Description |
|---|---|---|
| `CAMSEC_MQTT_BROKER` | `localhost` | IP or hostname of the broker |
| `CAMSEC_MQTT_PORT` | `1883` | TCP port of the broker |
| `DETECTOR_MQTT_USER` | *(empty)* | MQTT username for the detector (optional) |
| `DETECTOR_MQTT_PASS` | *(empty)* | MQTT password for the detector (optional) |
| `VIEWER_MQTT_USER` | *(empty)* | MQTT username for the viewer (optional) |
| `VIEWER_MQTT_PASS` | *(empty)* | MQTT password for the viewer (optional) |
| `DETECTOR_LOG_LEVEL` | `INFO` | Detector log level: `DEBUG` · `INFO` · `WARNING` · `ERROR` |
| `VIEWER_LOG_LEVEL` | `INFO` | Viewer log level: `DEBUG` · `INFO` · `WARNING` · `ERROR` |
| `YOLO_MODEL` | `/models/yolov8n.pt` | YOLO model: `n`=nano · `s`=small · `m`=medium |
| `YOLO_IMGSZ` | `320` | Model input size (px); smaller → less RAM |
| `CONFIDENCE` | `0.45` | Detection confidence threshold (0.0–1.0) |
| `CHUNK_SIZE` | `4096` | Chunk size in bytes (must match the camera firmware) |
| `STALE_TTL_S` | `60` | Discard incomplete image buffers after N seconds |
| `MAX_ALERTS` | `50` | Maximum alerts kept in RAM by the viewer |
| `PERSIST_DIR` | `/data/alerts` | Path inside the container where images are stored |
| `MAX_DISK_ALERTS` | `500` | Maximum number of images to keep on disk |
| `MAX_DISK_DAYS` | `7` | Maximum retention period in days |
| `ALERTS_DIR` | `/home/pi/camsec-alerts` | **Host** folder bind-mounted into the viewer container |

## Alert persistence

The viewer saves every alert image to the host filesystem and restores the full history on container restart.

```
$ALERTS_DIR/
├── index.jsonl          # one JSON line per alert (metadata)
└── images/
    ├── 1.jpg
    └── …
```

Every 10 alerts the viewer prunes old images: removes files older than `MAX_DISK_DAYS` days and keeps at most `MAX_DISK_ALERTS` images.

## Dashboard & REST API

Access at **http://\<server-ip\>:8088**

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/alerts` | Recent alerts in memory (JSON) |
| `GET` | `/api/alerts/latest` | Latest alert per camera (JSON) |
| `GET` | `/api/alerts/camera/<prefix>` | Full camera history (from disk) |
| `DELETE` | `/api/alerts/camera/<prefix>` | Delete all photos for a camera (RAM + disk) |
| `GET` | `/api/image/<id>` | Serve a JPEG alert image |
| `DELETE` | `/api/image/<id>` | Delete a specific photo (RAM + disk) |
| `GET` | `/stream` | Server-Sent Events real-time stream |
| `GET` | `/embed` | Minimal panel for `<iframe>` embedding |

## MQTT topics

### Consumed (per camera)

| Topic | Format | Description |
|---|---|---|
| `<prefix>/image/begin` | JSON | Image start: `{id, size, chunks, dark}` |
| `<prefix>/image/data` | Binary | Chunk: `[4B id BE][2B idx BE][JPEG data]` |
| `<prefix>/image/end` | JSON | Image end: `{id, chunks, ok}` |
| `<prefix>/image/same` | JSON | No JPEG payload: `{id, same_as, size, dark, mode[, score]}` |

When `<prefix>/image/same` arrives, the detector skips a new inference (no JPEG payload) and the viewer refreshes live timestamp/status while reusing the latest cached frame.

### Published on detection

| Topic | Format | Description |
|---|---|---|
| `detector/status` | text | `online` / `offline` (retained) |
| `<prefix>/alert` | JSON | Detection summary |
| `<prefix>/alert/same` | JSON | Warning: many consecutive `image/same` events after a person detection |
| `<prefix>/alert/image/begin` | JSON | Start of annotated image |
| `<prefix>/alert/image/data` | Binary | Annotated image chunk |
| `<prefix>/alert/image/end` | JSON | End of annotated image |

## YOLO models

| Model | File | Speed | Accuracy |
|---|---|---|---|
| Nano | `yolov8n.pt` | ★★★★★ | ★★☆☆☆ |
| Small | `yolov8s.pt` | ★★★★☆ | ★★★☆☆ |
| Medium | `yolov8m.pt` | ★★★☆☆ | ★★★★☆ |
| Large | `yolov8l.pt` | ★★☆☆☆ | ★★★★★ |

Change `YOLO_MODEL` in `.env` and rebuild: `docker compose up --build -d`

## Common operations

```bash
docker compose down                        # stop
docker compose logs --tail=100 detector    # last 100 log lines
docker compose restart detector            # restart without rebuilding
docker compose up --build -d               # rebuild and restart
```

## Security

- MQTT credentials must **never** be written in `docker-compose.yml`. Always use `.env` and add it to `.gitignore`.
- For internet-exposed deployments, use MQTT over TLS (port 8883).

## Platform compatibility

Docker images are multi-architecture (`linux/amd64` + `linux/arm64/v8`). Docker automatically selects the correct architecture when running `docker compose pull`.

| Platform | Architecture | Notes |
|---|---|---|
| PC / x86 server | `linux/amd64` | No additional configuration |
| Raspberry Pi 4/5 | `linux/arm64/v8` | CPU-only PyTorch (no CUDA) |

# CamSec – Servidor de detecció

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
MQTT_BROKER=192.168.1.10
MQTT_PORT=1883

# Credencials MQTT (deixa buit si el broker és anònim)
DETECTOR_MQTT_USER=
DETECTOR_MQTT_PASS=
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
| `MQTT_BROKER` | `localhost` | IP o hostname del broker |
| `MQTT_PORT` | `1883` | Port TCP del broker |
| `DETECTOR_MQTT_USER` | *(buit)* | Usuari MQTT (opcional) |
| `DETECTOR_MQTT_PASS` | *(buit)* | Contrasenya MQTT (opcional) |
| `YOLO_MODEL` | `/models/yolov8n.pt` | Model YOLO: `n`=nano · `s`=small · `m`=medium |
| `YOLO_IMGSZ` | `320` | Mida d'entrada al model (px); valors més petits → menys RAM |
| `CONFIDENCE` | `0.45` | Llindar de confiança de detecció (0.0–1.0) |
| `CHUNK_SIZE` | `4096` | Mida del chunk en bytes (ha de coincidir amb la càmera) |
| `STALE_TTL_S` | `60` | Descarta buffers d'imatge incomplets passats N segons |
| `LOG_LEVEL` | `INFO` | Nivell de log: `DEBUG` · `INFO` · `WARNING` · `ERROR` |

Per canviar qualsevol paràmetre edita `.env` i reinicia:

```bash
docker compose up -d
```

---

## Topics MQTT

### Consumits (per càmera)

| Topic | Format | Descripció |
|---|---|---|
| `<prefix>/image/begin` | JSON | Inici d'imatge: `{id, size, chunks, dark}` |
| `<prefix>/image/data` | Binari | Chunk: `[4B id BE][2B idx BE][dades JPEG]` |
| `<prefix>/image/end` | JSON | Fi d'imatge: `{id, chunks, ok}` |

### Publicats en detecció

| Topic | Format | Descripció |
|---|---|---|
| `detector/status` | text | `online` / `offline` (retained) |
| `<prefix>/alert` | JSON | Resum de la detecció |
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

El detector es connecta al broker via la xarxa física del host
(`network_mode: host`), sense cap xarxa Docker interna.

| Situació del broker | `MQTT_BROKER` |
|---|---|
| Servidor remot | `192.168.1.10` (IP de la màquina) |
| Mateix host, fora de Docker | `127.0.0.1` |
| Mateix host, en Docker | `127.0.0.1` (amb `network_mode: host` al broker també) |

> **Windows / macOS**: `network_mode: host` no és compatible amb Docker
> Desktop. Usa la IP real del host en lloc de `127.0.0.1`.

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

# CamSec

> 🌐 **Idioma / Language:** [Català](#camsec) · [English](#camsec--english)

---

Sistema de vigilància basat en ESP32-CAM amb detecció de persones per Intel·ligència Artificial.

Les càmeres capturen imatges periòdicament i les envien via MQTT. Un servei de detecció (YOLOv8) analitza les imatges en temps real i genera alertes quan detecta persones. Un dashboard web mostra les últimes fotos de cada càmera i l'historial d'alertes.

```
┌─────────────┐   MQTT (chunks JPEG)   ┌──────────────┐   MQTT (alertes)   ┌──────────────┐
│  ESP32-CAM  │ ─────────────────────► │   Detector   │ ─────────────────► │    Viewer    │
│  (firmware) │                        │  (YOLOv8)    │                    │  (dashboard) │
└─────────────┘                        └──────────────┘                    └──────────────┘
                                               │                                   │
                                        ┌──────┴──────┐                            │
                                        │   Mosquitto │ ◄──────────────────────────┘
                                        │  (broker)   │
                                        └─────────────┘
```

---

## Prerequisits

### Maquinari
- Placa **AI-Thinker ESP32-CAM** (OV2640)
- Adaptador USB-TTL (p. ex. FTDI FT232RL) per programar
- Cable USB i alimentació 5 V / 2 A

### Software – servidor
| Eina | Versió mínima |
|---|---|
| Docker Desktop | 24+ |
| Docker Compose | V2 (integrat a Docker Desktop) |

### Software – firmware
| Eina | Versió mínima |
|---|---|
| VS Code | 1.85+ |
| Extensió PlatformIO IDE | 3.3+ |

### Xarxa
- El broker MQTT i el servidor han d'estar a la mateixa xarxa local que les càmeres.
- Port **1883** (MQTT) obert al servidor.
- Port **8088** (dashboard web) accessible des del navegador.

---

## Estructura del projecte

```
CamSec/
├── camara/                  # Firmware ESP32-CAM (PlatformIO)
│   ├── src/
│   │   ├── main.cpp         # Lògica principal: captura, MQTT, config mode
│   │   ├── web_portal.h     # Portal web AP (configuració Wi-Fi/MQTT)
│   │   └── config.h         # Estructura de configuració (NVS)
│   └── platformio.ini       # Configuració de la plataforma
│
├── server/                  # Serveis Docker
│   ├── docker-compose.yml   # Orquestració dels serveis
│   ├── .env.example         # Plantilla de variables d'entorn
│   ├── mosquitto/           # Broker MQTT
│   │   ├── config/
│   │   │   └── mosquitto.conf
│   │   └── scripts/
│   │       └── entrypoint.sh
│   ├── detector/            # Detecció de persones (YOLOv8)
│   │   ├── detector.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── viewer/              # Dashboard web
│       ├── viewer.py
│       ├── Dockerfile
│       └── requirements.txt
│
└── tools/                   # Eines d'escriptori
    ├── cam_viewer.py        # Visor en temps real de les càmeres (tkinter)
    └── .env.example         # Plantilla de variables per al visor
```

---

## Instal·lació i configuració

### 1. Servidor (Docker)

**1.1 Clonar i configurar variables d'entorn**

```bash
cd server
cp .env.example .env
```

Edita `.env` amb la IP del servidor on corre el broker MQTT:

```env
MQTT_BROKER=192.168.1.10   # IP del teu servidor / màquina actual
MQTT_PORT=1883

# Deixa buit si el broker no requereix autenticació
DETECTOR_MQTT_USER=
DETECTOR_MQTT_PASS=
VIEWER_MQTT_USER=
VIEWER_MQTT_PASS=
```

> Si el broker Mosquitto corre al **mateix ordinador** que Docker, usa la IP local de la interfície de xarxa (no `127.0.0.1`), per exemple `192.168.1.10`.

**1.2 Arrancar els serveis**

```bash
docker compose -f server/docker-compose.yml up -d --build
```

Primera execució: descarrega el model YOLOv8 (~6 MB), pot trigar uns minuts.

**1.3 Verificar que funciona**

```bash
docker compose -f server/docker-compose.yml logs -f
```

Hauries de veure:
```
camsec-detector  | MQTT connected to 192.168.1.10:1883
camsec-viewer    | MQTT connected to 192.168.1.10:1883
camsec-viewer    |  * Running on http://0.0.0.0:8088
```

**Dashboard:** http://localhost:8088

---

### 2. Broker MQTT (Mosquitto)

El broker Mosquitto **no forma part** del `docker-compose.yml` i ha de córrer de manera independent (al servidor o a un altre equip de la xarxa).

**Opció A – Mosquitto natiu (Linux/macOS)**

```bash
sudo apt install mosquitto mosquitto-clients   # Debian/Ubuntu
sudo systemctl enable --now mosquitto
```

**Opció B – Mosquitto amb Docker** (fora del projecte)

```bash
docker run -d --name mosquitto \
  -p 1883:1883 \
  -v "$PWD/server/mosquitto/config:/mosquitto/config" \
  -v "$PWD/server/mosquitto/scripts/entrypoint.sh:/docker-entrypoint.sh" \
  --entrypoint /docker-entrypoint.sh \
  eclipse-mosquitto:2
```

**Autenticació (opcional)**

Per habilitar autenticació, edita `.env`:
```env
MQTT_AUTH=1
MQTT_USERS=cam01:secret detector:admin viewer:admin
DETECTOR_MQTT_USER=detector
DETECTOR_MQTT_PASS=admin
VIEWER_MQTT_USER=viewer
VIEWER_MQTT_PASS=admin
```

---

### 3. Firmware ESP32-CAM

**3.1 Instal·lar PlatformIO**

1. Obre VS Code → Extensions → cerca `PlatformIO IDE` → Instal·la.
2. Obre la carpeta `camara/` com a workspace de PlatformIO.

**3.2 Connectar l'ESP32-CAM en mode programació**

```
ESP32-CAM       Adaptador USB-TTL
──────────      ──────────────────
GND        ───► GND
5V         ───► VCC (5V)
U0TXD (1)  ───► RX
U0RXD (3)  ───► TX
GPIO0      ───► GND   ← Posa a GND per entrar en mode flash
```

**3.3 Compilar i pujar el firmware**

Amb PlatformIO (VS Code):
- **Build:** `Ctrl+Alt+B` o botó ✓
- **Upload:** `Ctrl+Alt+U` o botó →

O des de terminal:
```bash
cd camara
pio run -t upload
```

**3.4 Desconnectar GPIO0 de GND i reiniciar**

Desconnecta el pont GPIO0–GND i prem el botó RST (o desconnecta i reconnecta l'alimentació).

---

### 4. Configuració de la càmera (mode AP)

La primera vegada (o si canvies la xarxa), cal configurar la càmera via el portal web:

1. **Alimenta la càmera.** Durant els **primers 5 segons** el LED vermell farà blink ràpid.
2. **Prem el botó BOOT (GPIO0)** mentre el LED fa blink → la càmera entra en mode configuració.
3. El LED vermell fa blink lent (50 ms ON / 2 s OFF) mentre el portal és actiu.
4. **Connecta el teu mòbil o ordinador** a la xarxa Wi-Fi **`CamSec-Config`** (contrasenya: `camsec123`).
5. Obre el navegador a **http://192.168.4.1** (si no s'obre automàticament).
6. Omple el formulari:

   | Camp | Descripció |
   |---|---|
   | Wi-Fi SSID | Nom de la xarxa Wi-Fi |
   | Wi-Fi Password | Contrasenya Wi-Fi |
   | MQTT Broker | IP del servidor (p. ex. `192.168.1.10`) |
   | MQTT Port | `1883` per defecte |
   | MQTT User / Pass | Buit si el broker no requereix autenticació |
   | MQTT Client ID | Identificador únic per a aquesta càmera (p. ex. `cam-sala`) |
   | MQTT Prefix | Prefix dels topics (p. ex. `cam/01`) |
   | Orientació de la càmera | **Normal** (cap amunt) o **Cap per avall** (rotació 180°) |

7. Clica **Guardar i reiniciar**. La càmera es connectarà a la xarxa i començarà a capturar.

> Si si vols tornar a entrar al mode configuració, reinicia la càmera i prem BOOT dins dels 5 primers segons.

---

## Funcionament del sistema

### Flux de dades

```
ESP32-CAM                    Broker MQTT                  Servidor
─────────                    ───────────                  ────────

Captura JPEG (5.5 s)
  │
  ├─► cam/01/image/begin ──►  broker  ──► detector
  ├─► cam/01/image/data  ──►  broker  ──► detector  (reassembla el JPEG)
  └─► cam/01/image/end   ──►  broker  ──► detector
                                              │
                                         YOLOv8 detecta persones
                                              │
                                  ┌───────────┴───────────┐
                                  │                       │
                           cam/01/alert           cam/01/alert/image/*
                           (JSON resum)           (JPEG anotat, chunks)
                                  │                       │
                                  └───────────┬───────────┘
                                              │
                                           viewer
                                              │
                                    Dashboard web (SSE)
                                    http://localhost:8088
```

### Topics MQTT

| Topic | Direcció | Contingut |
|---|---|---|
| `<prefix>/image/begin` | càmera → broker | JSON: `{id, size, chunks, dark}` |
| `<prefix>/image/data`  | càmera → broker | Binari: `[4B id][2B idx][dades JPEG]` |
| `<prefix>/image/end`   | càmera → broker | JSON: `{id, chunks, ok}` |
| `<prefix>/cmd`         | broker → càmera | `"start"` \| `"stop"` |
| `<prefix>/status`      | càmera → broker | `"online"` \| `"capturing"` \| `"idle"` \| `"offline"` |
| `<prefix>/alert`       | detector → broker | JSON resum de detecció |
| `<prefix>/alert/image/*` | detector → broker | JPEG anotat amb bounding boxes |
| `detector/status`      | detector → broker | `"online"` \| `"offline"` (retained) |

### LED de la càmera

| Estat | LED vermell (GPIO33) |
|---|---|
| Finestra de configuració (5 s al boot) | Blink ràpid (100 ms ON / 500 ms OFF) |
| Mode configuració actiu (portal AP) | Blink lent (50 ms ON / 2 s OFF) |
| Capturant imatges | 2 parpellejos ràpids cada 3 s |
| Presa de fotos aturada (`stop`) | 1 parpelleig cada 3 s |
| Reconnectant MQTT | Apagat |
| Flash per captura nocturna | LED blanc (GPIO4), temporalment |

---

## Dashboard web

Accedeix a **http://\<ip-servidor\>:8088**

- **Vista principal:** una targeta per càmera amb la **última foto detectada**.
- **Historial:** clica qualsevol targeta per veure totes les fotos d'aquella càmera.
- **Eliminar fotos:** botó 🗑 a cada foto de l'historial; botó "Elimina totes" per buidar tota la càmera.
- **Temps real:** les targetes s'actualitzen automàticament via Server-Sent Events (SSE).

### API HTTP (per integració externa)

| Endpoint | Descripció |
|---|---|
| `GET /api/alerts` | Llista de totes les alertes recents (JSON) |
| `GET /api/alerts/latest` | Última alerta per càmera (JSON) |
| `GET /api/alerts/camera/<prefix>` | Historial d'una càmera concreta |
| `DELETE /api/alerts/camera/<prefix>` | Elimina totes les fotos d'una càmera |
| `GET /api/image/<id>` | Imatge JPEG d'una alerta |
| `DELETE /api/image/<id>` | Elimina una foto concreta |
| `GET /stream` | Server-Sent Events en temps real |
| `GET /embed` | Panel mínim per `<iframe>` |

---

## Operació i manteniment

### Visor d'escriptori (optional)

`tools/cam_viewer.py` és una aplicació d'escriptori (tkinter) que mostra en
temps real les imatges que envien les càmeres, sense necessitar el servidor
Docker ni el detector YOLOv8.

**Instal·lació de dependències:**
```bash
pip install paho-mqtt Pillow
```

**Configuració:**
```bash
cd tools
cp .env.example .env
# edita .env amb la IP del broker i les credencials
```

**Execució:**
```bash
python tools/cam_viewer.py
# o amb arguments explícits:
python tools/cam_viewer.py --broker <IP_BROKER> --user <USUARI> --password <CLAU>
```

---

### Logs

```bash
# Tots els serveis
docker compose -f server/docker-compose.yml logs -f

# Només detector
docker compose -f server/docker-compose.yml logs -f detector

# Només viewer
docker compose -f server/docker-compose.yml logs -f viewer
```

### Aturar / reiniciar

```bash
# Aturar
docker compose -f server/docker-compose.yml down

# Reiniciar un servei
docker compose -f server/docker-compose.yml restart viewer

# Reconstruir i reiniciar
docker compose -f server/docker-compose.yml up -d --build
```

### Ajustar el model de detecció

Edita `YOLO_MODEL` a `docker-compose.yml`:

| Model | Velocitat | Precisió |
|---|---|---|
| `yolov8n.pt` | Molt ràpid | Bàsica |
| `yolov8s.pt` | Ràpid | Bona |
| `yolov8m.pt` | Moderat | Alta |

### Ajustar la confiança de detecció

`CONFIDENCE` a `docker-compose.yml` (per defecte `0.45`):
- Augmenta → menys falsos positius, pot perdre deteccions.
- Disminueix → detecta més, però pot generar més falses alarmes.

---

## Solució de problemes

| Símptoma | Possible causa | Solució |
|---|---|---|
| La càmera no es connecta al Wi-Fi | SSID/password incorrectes | Entra al mode configuració i torna a guardar |
| La càmera no arriba al broker MQTT | IP del broker incorrecta o firewall | Comprova la IP i que el port 1883 és accessible |
| El dashboard no mostra fotos | Viewer no connectat al broker | Comprova `docker logs camsec-viewer` |
| `persons: 0` a totes les alertes | Model no carregat correctament | `docker logs camsec-detector` per veure errors YOLO |
| El portal AP no s'obre | El dispositiu no es connecta a `CamSec-Config` | Connecta manualment i obre http://192.168.4.1 |
| Imatges incompletes / trencades | Paquets MQTT perduts | Augmenta `STALE_TTL_S` al detector |
| Imatge al revés | Càmera muntada cap per avall | Activa **Cap per avall** al portal de configuració |
| Detector peta amb exit code 132 a la Pi | PyTorch CUDA en lloc de CPU | Fes `docker compose pull` per obtenir la imatge arm64 CPU-only |

---

---

# CamSec — English

AI-powered surveillance system based on ESP32-CAM cameras.

Cameras periodically capture images and send them via MQTT. A detection service (YOLOv8) analyses the images in real time and generates alerts when a person is detected. A web dashboard shows the latest photo from each camera and a full alert history.

```
┌─────────────┐   MQTT (JPEG chunks)   ┌──────────────┐   MQTT (alerts)   ┌──────────────┐
│  ESP32-CAM  │ ─────────────────────► │   Detector   │ ────────────────► │    Viewer    │
│  (firmware) │                        │  (YOLOv8)    │                   │  (dashboard) │
└─────────────┘                        └──────────────┘                   └──────────────┘
                                               │                                  │
                                        ┌──────┴──────┐                           │
                                        │   Mosquitto │ ◄─────────────────────────┘
                                        │  (broker)   │
                                        └─────────────┘
```

## Prerequisites

### Hardware
- **AI-Thinker ESP32-CAM** board (OV2640 sensor)
- USB-to-TTL adapter (e.g. FTDI FT232RL) for programming
- USB cable and 5 V / 2 A power supply

### Software — server
| Tool | Minimum version |
|---|---|
| Docker Desktop | 24+ |
| Docker Compose | V2 (bundled with Docker Desktop) |

### Software — firmware
| Tool | Minimum version |
|---|---|
| VS Code | 1.85+ |
| PlatformIO IDE extension | 3.3+ |

### Network
- The MQTT broker and the server must be on the same local network as the cameras.
- Port **1883** (MQTT) must be open on the server.
- Port **8088** (web dashboard) must be reachable from the browser.

## Quick start

### 1. Server (Docker)

```bash
cd server
cp .env.example .env          # fill in your MQTT broker IP and credentials
docker compose -f server/docker-compose.yml up -d --build
```

Dashboard: **http://localhost:8088**

### 2. MQTT broker (Mosquitto)

The broker is **not included** in `docker-compose.yml`. Run it independently:

```bash
# Native (Debian/Ubuntu)
sudo apt install mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto
```

### 3. Firmware

1. Open `camara/` in VS Code with PlatformIO installed.
2. Connect GPIO0 to GND (flash mode), then connect the UART adapter.
3. **Build & Upload** (`Ctrl+Alt+U`) or `pio run -t upload`.
4. Disconnect GPIO0 from GND and press RST.

### 4. Camera configuration (AP mode)

1. Power the camera. During the first **5 seconds** the red LED blinks fast.
2. Press **BOOT (GPIO0)** while the LED blinks → enters configuration mode.
3. Connect to Wi-Fi **`CamSec-Config`** (password: `camsec123`).
4. Open **http://192.168.4.1** and fill in the form (Wi-Fi, MQTT broker IP, credentials, prefix, orientation).
5. Click **Save & Restart**.

## Web dashboard

Access at **http://\<server-ip\>:8088**

- **Main view:** one card per camera with the latest detected photo.
- **History:** click any card to see all photos from that camera.
- **Delete photos:** 🗑 button on each photo; "Delete all" button to clear a whole camera.
- **Real time:** cards update automatically via Server-Sent Events (SSE).

### HTTP API

| Endpoint | Description |
|---|---|
| `GET /api/alerts` | List of recent alerts (JSON) |
| `GET /api/alerts/latest` | Latest alert per camera (JSON) |
| `GET /api/alerts/camera/<prefix>` | Full history for one camera |
| `DELETE /api/alerts/camera/<prefix>` | Delete all photos for one camera |
| `GET /api/image/<id>` | JPEG image of a specific alert |
| `DELETE /api/image/<id>` | Delete a specific photo |
| `GET /stream` | Server-Sent Events stream |
| `GET /embed` | Minimal panel for `<iframe>` |

## Desktop viewer (optional)

`tools/cam_viewer.py` is a standalone desktop app (tkinter) that shows live camera images without needing the Docker server or YOLOv8 detector.

```bash
pip install paho-mqtt Pillow
cd tools
cp .env.example .env          # fill in your broker IP and credentials
python cam_viewer.py
```

## MQTT topics

| Topic | Direction | Content |
|---|---|---|
| `<prefix>/image/begin` | camera → broker | JSON: `{id, size, chunks, dark}` |
| `<prefix>/image/data`  | camera → broker | Binary: `[4B id][2B idx][JPEG data]` |
| `<prefix>/image/end`   | camera → broker | JSON: `{id, chunks, ok}` |
| `<prefix>/cmd`         | broker → camera | `"start"` \| `"stop"` |
| `<prefix>/status`      | camera → broker | `"online"` \| `"capturing"` \| `"idle"` \| `"offline"` |
| `<prefix>/alert`       | detector → broker | Detection summary JSON |
| `<prefix>/alert/image/*` | detector → broker | Annotated JPEG with bounding boxes |

## Camera LED

| State | Red LED (GPIO33) |
|---|---|
| Configuration window (5 s at boot) | Fast blink (100 ms ON / 500 ms OFF) |
| Configuration mode active (AP portal) | Slow blink (50 ms ON / 2 s OFF) |
| Capturing images | 2 quick flashes every 3 s |
| Capture stopped (`stop`) | 1 flash every 3 s |
| Reconnecting MQTT | Off |
| Night capture flash | White LED (GPIO4), momentarily |

## Operation & maintenance

### Desktop viewer

```bash
cd tools
cp .env.example .env
python cam_viewer.py
```

### Logs

```bash
docker compose -f server/docker-compose.yml logs -f           # all services
docker compose -f server/docker-compose.yml logs -f detector  # detector only
docker compose -f server/docker-compose.yml logs -f viewer    # viewer only
```

### Stop / restart

```bash
docker compose -f server/docker-compose.yml down
docker compose -f server/docker-compose.yml restart viewer
docker compose -f server/docker-compose.yml up -d --build
```

## Troubleshooting

| Symptom | Possible cause | Solution |
|---|---|---|
| Camera doesn't connect to Wi-Fi | Wrong SSID/password | Enter config mode and save again |
| Camera can't reach the MQTT broker | Wrong broker IP or firewall | Check the IP and that port 1883 is open |
| Dashboard shows no photos | Viewer not connected to broker | Check `docker logs camsec-viewer` |
| `persons: 0` on all alerts | YOLO model not loaded correctly | Check `docker logs camsec-detector` |
| AP portal doesn't open | Device not connected to `CamSec-Config` | Connect manually and open http://192.168.4.1 |
| Incomplete / broken images | Lost MQTT packets | Increase `STALE_TTL_S` in the detector |
| Image upside down | Camera mounted inverted | Enable **Upside down** in the configuration portal |
| Detector crashes with exit code 132 on Pi | CUDA PyTorch instead of CPU | Run `docker compose pull` to get the arm64 CPU-only image |

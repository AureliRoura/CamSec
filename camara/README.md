# CamSec – ESP32-CAM firmware

Firmware per a una placa **AI-Thinker ESP32-CAM** (OV2640) que captura imatges JPEG periòdicament i les envia per MQTT dividides en chunks binaris. Inclou un portal web de configuració accessible via Wi-Fi en mode punt d'accés.

---

## Característiques

| Funció | Detall |
|---|---|
| Captura d'imatges | Cada **5,5 s** en format JPEG |
| Resolució | UXGA (1600×1200) amb PSRAM · VGA (640×480) sense |
| Detecció de poca llum | Gain AGC ≥ 25 → activa el LED de flaix |
| Enviament per MQTT | Imatge dividida en chunks de **4 096 bytes** |
| Comandes MQTT | `start` · `stop` |
| Orientació de la càmera | Rotació 180° configurable des del portal web |
| Mode configuració | Portal web en punt d'accés (prem BOOT 5 s a l'arrencada) |

---

## Estructura del projecte

```
camara/
├── platformio.ini
└── src/
    ├── config.h        # Estructura AppConfig
    ├── web_portal.h    # Portal web en mode AP
    └── main.cpp        # Firmware principal
```

---

## Preparació de l'entorn de desenvolupament

### 1. Instal·lar VS Code i PlatformIO

1. Descarrega i instal·la [Visual Studio Code](https://code.visualstudio.com/).
2. Obre VS Code, ves a **Extensions** (`Ctrl+Shift+X`) i cerca **PlatformIO IDE**.
3. Instal·la l'extensió i **reinicia VS Code** quan ho demani.

### 2. Obrir el projecte

1. A VS Code: **File → Open Folder…** i selecciona la carpeta `camara/`.
2. PlatformIO detectarà el `platformio.ini` automàticament.

### 3. Descarregar la plataforma i les dependències

La primera vegada cal descarregar el toolchain i les biblioteques. Hi ha dues maneres:

**Opció A – Compilar (recomanat)**

```
Ctrl+Shift+P → "PlatformIO: Build"
```

PlatformIO descarregarà automàticament:
- Toolchain `xtensa-esp32-elf-gcc`
- Plataforma `espressif32`
- Framework `arduino` per a ESP32
- Biblioteca `PubSubClient`

**Opció B – CLI**

```bash
pio run
```

### 4. Solucionar "Cannot open source file Arduino.h"

Aquest error apareix a l'editor **abans de la primera compilació** perquè VS Code no sap on es troben les capçaleres del framework. Té dos orígens possibles:

#### a) La plataforma no s'ha descarregat encara

Compila el projecte almenys una vegada (veure pas 3). PlatformIO generarà el fitxer `.vscode/c_cpp_properties.json` amb els paths correctes i l'error desapareixerà.

#### b) IntelliSense no s'ha actualitzat

Si ja has compilat i l'error persisteix:

1. `Ctrl+Shift+P` → **"PlatformIO: Rebuild IntelliSense Index"**
2. Espera uns segons fins que la barra inferior de PlatformIO deixi de girar.
3. Si segueix: tanca i torna a obrir VS Code.

> **Nota**: l'error de `Arduino.h` és únicament un problema de IntelliSense — el codi compila correctament amb `pio run` fins i tot quan l'editor el marca com a error.

### 5. Instal·lar drivers USB-UART (si cal)

| Xip de l'adaptador | Driver |
|---|---|
| **CH340 / CH341** | [ch341ser](https://www.wch-ic.com/downloads/CH341SER_EXE.html) |
| **CP210x** | [Silicon Labs CP210x](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers) |
| **FTDI FT232** | [FTDI VCP](https://ftdichip.com/drivers/vcp-drivers/) |

---

## Requisits

- **Hardware**: AI-Thinker ESP32-CAM + adaptador USB-UART (CH340, CP210x…)
- **Software**: [PlatformIO](https://platformio.org/) (extensió VS Code o CLI)
- **Dependència**: PubSubClient ≥ 2.8 (instal·lada automàticament per PlatformIO)

---

## Càrrega del firmware

1. Connecta `GPIO0` a `GND` (mode flash).
2. Connecta l'adaptador UART i alimenta la placa.
3. Executa `pio run --target upload`.
4. Desconnecta `GPIO0` de `GND` i prem **RST**.

---

## Mode configuració (primera arrencada)

1. Mantén el botó **BOOT** (GPIO0) premut durant **3 s** mentre encens la placa.
   - El LED parpellejarà ràpidament durant el compte enrere.
2. Connecta't a la xarxa Wi-Fi:
   - **SSID**: `CamSec-Config`
   - **Contrasenya**: `camsec123`
3. Obre el navegador a [http://192.168.4.1](http://192.168.4.1).
4. Omple el formulari i fes clic a **Desar i reiniciar**.

> El LED parpellejarà lentament (1 s) mentre el portal és actiu.

### Orientació de la càmera

Si la càmera està muntada **cap per avall**, activa la rotació 180° des del portal de configuració:

1. Entra al mode configuració (veure punt anterior).
2. A la secció **Càmera**, selecciona **Cap per avall (rotar 180°)**.
3. Desa i reinicia.

El firmware aplica `vflip + hmirror` al sensor OV2640 en arrencar — totes les imatges surten ja orientades correctament sense cost per captura.

---

## Topics MQTT

Substitueix `cam/01` pel prefix configurat.

| Topic | Direcció | Format | Descripció |
|---|---|---|---|
| `cam/01/cmd` | Subscripció | text | `start` o `stop` |
| `cam/01/status` | Publicació | text | `online` · `capturing` · `idle` · `offline` |
| `cam/01/image/begin` | Publicació | JSON | `{"id":1,"size":45000,"chunks":11,"dark":0}` |
| `cam/01/image/data` | Publicació | binari | `[4B id BE][2B chunk_idx BE][dades JPEG]` |
| `cam/01/image/end` | Publicació | JSON | `{"id":1,"chunks":11,"ok":1}` |

### Reconstrucció d'imatge (exemple Python)

```python
import paho.mqtt.client as mqtt
import struct, io

chunks = {}
meta = {}

def on_message(client, userdata, msg):
    t = msg.topic
    if t.endswith("/image/begin"):
        import json
        meta.update(json.loads(msg.payload))
        chunks.clear()
    elif t.endswith("/image/data"):
        img_id = struct.unpack_from(">I", msg.payload, 0)[0]
        idx    = struct.unpack_from(">H", msg.payload, 4)[0]
        data   = msg.payload[6:]
        chunks[idx] = data
    elif t.endswith("/image/end"):
        n = meta.get("chunks", 0)
        if len(chunks) == n:
            jpeg = b"".join(chunks[i] for i in range(n))
            with open(f"image_{meta['id']}.jpg", "wb") as f:
                f.write(jpeg)
            print(f"Image {meta['id']} saved ({len(jpeg)} B)")

c = mqtt.Client()
c.on_message = on_message
c.connect("localhost", 1883)
c.subscribe("cam/01/#")
c.loop_forever()
```

---

## Paràmetres ajustables (`main.cpp`)

| Constant | Valor per defecte | Descripció |
|---|---|---|
| `CAPTURE_INTERVAL_MS` | 5500 | Interval entre captures (ms) |
| `CHUNK_SIZE` | 4096 | Mida de cada chunk MQTT (bytes) |
| `LOW_LIGHT_AGC_TH` | 25 | Llindar AGC per detectar poca llum (0-30) |
| `FLASH_DELAY_MS` | 80 | Temps d'espera del flaix abans de capturar (ms) |
| `CONFIG_HOLD_MS` | 3000 | ms que cal mantenir BOOT per entrar en mode config |

---

## LED de la càmera

| Estat | LED vermell (GPIO33) |
|---|---|
| Finestra de configuració (5 s al boot) | Blink ràpid (100 ms ON / 500 ms OFF) |
| Mode configuració actiu (portal AP) | Blink lent (50 ms ON / 2 s OFF) |
| Capturant imatges | 2 parpellejos ràpids cada 3 s |
| Presa de fotos aturada (`stop`) | 1 parpelleig cada 3 s |
| Reconnectant MQTT | Apagat |
| Flash per captura nocturna | LED blanc (GPIO4), temporalment |

---

## Notes de seguretat

- Les credencials Wi-Fi i MQTT s'emmagatzemen a la **NVS** de l'ESP32 (flash xifrada amb eFuse si es configura secure boot).
- Les contrasenyes **mai** es pre-omplen en el formulari web.
- La connexió MQTT és en text pla (port 1883). Per a entorns exposats, considera usar un broker amb TLS (port 8883) i ajusta la biblioteca PubSubClient per usar `WiFiClientSecure`.
- La xarxa del portal de configuració (`CamSec-Config`) és oberta internament però protegida amb contrasenya WPA2.

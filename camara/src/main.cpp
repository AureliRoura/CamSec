// =============================================================================
// CamSec – ESP32-CAM main firmware
// =============================================================================
// Features:
//   • Captures a JPEG image every 5.5 s and transmits it via MQTT in chunks
//   • Detects low-light conditions via AGC gain; enables flash LED when dark
//   • Subscribes to MQTT commands: "start" and "stop"
//   • Config mode: hold BOOT button (GPIO0) for 3 s at power-on →
//     AP "CamSec-Config" + web portal at http://192.168.4.1
//
// MQTT topics  (prefix configurable, default "cam/01"):
//   Subscribe : <prefix>/cmd          – "start" | "stop" | "flash_on" | "flash_off" | "flash_auto"
//   Publish   : <prefix>/status       – "online" | "capturing" | "idle" | "offline"
//   Publish   : <prefix>/image/begin  – JSON: {id, size, chunks, dark}
//   Publish   : <prefix>/image/data   – Binary: [4B img_id BE][2B chunk_idx BE][data]
//   Publish   : <prefix>/image/end    – JSON: {id, chunks, ok}
// =============================================================================

#include <Arduino.h>
#include <WiFi.h>
#include <Preferences.h>
#include "esp_camera.h"
#include <PubSubClient.h>

#include "config.h"
#include "web_portal.h"

// ─── AI-Thinker ESP32-CAM pin map ────────────────────────────────────────────
#define CAM_PIN_PWDN    32
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK     0   // Also BOOT button – read BEFORE camera init
#define CAM_PIN_SIOD    26
#define CAM_PIN_SIOC    27
#define CAM_PIN_D7      35
#define CAM_PIN_D6      34
#define CAM_PIN_D5      39
#define CAM_PIN_D4      36
#define CAM_PIN_D3      21
#define CAM_PIN_D2      19
#define CAM_PIN_D1      18
#define CAM_PIN_D0       5
#define CAM_PIN_VSYNC   25
#define CAM_PIN_HREF    23
#define CAM_PIN_PCLK    22

// ─── Hardware ─────────────────────────────────────────────────────────────────
#define FLASH_LED_PIN      4    // Built-in white flash LED
#define RED_LED_PIN       33    // Built-in red status LED (active LOW)
#define CONFIG_BUTTON_PIN  0    // BOOT button (shared with XCLK after cam init)
#define CONFIG_WINDOW_MS  5000  // ms window at boot to press BOOT for config mode

// ─── Tuning ──────────────────────────────────────────────────────────────────
#define CAPTURE_INTERVAL_MS   5500  // 5.5 seconds between captures
#define CHUNK_SIZE            4096  // Bytes per MQTT chunk
#define LOW_LIGHT_SIZE_TH   200000  // Probe JPEG bytes above this → dark scene → flash
#define FLASH_DELAY_MS          80  // ms for flash to illuminate before capture
#define WIFI_MAX_TRIES          40  // Attempts before giving up (×500 ms each)
#define MQTT_RECONNECT_MS     5000  // Delay between reconnect attempts
#define MQTT_KEEPALIVE_S        60  // MQTT keep-alive interval

// ─── Application globals ─────────────────────────────────────────────────────
AppConfig    cfg;
Preferences  prefs;

WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);

volatile bool capturing       = true;
uint32_t      imageId         = 0;
unsigned long lastCapture     = 0;
bool          flashOverride   = false;  // true = manual flash control
bool          flashOverrideOn = false;  // effective value when override active

// Pre-built topic strings (assembled in buildTopics())
static char topicCmd[96];
static char topicStatus[96];
static char topicBegin[96];
static char topicData[96];
static char topicEnd[96];

// ─── Config persistence ───────────────────────────────────────────────────────
void loadConfig() {
    prefs.begin("camsec", /*readOnly=*/true);
    strlcpy(cfg.wifi_ssid,   prefs.getString("wifi_ssid",   "").c_str(),         sizeof(cfg.wifi_ssid));
    strlcpy(cfg.wifi_pass,   prefs.getString("wifi_pass",   "").c_str(),         sizeof(cfg.wifi_pass));
    strlcpy(cfg.mqtt_broker, prefs.getString("mqtt_broker", "").c_str(),         sizeof(cfg.mqtt_broker));
    cfg.mqtt_port =          prefs.getUShort("mqtt_port",   1883);
    strlcpy(cfg.mqtt_user,   prefs.getString("mqtt_user",   "").c_str(),         sizeof(cfg.mqtt_user));
    strlcpy(cfg.mqtt_pass,   prefs.getString("mqtt_pass",   "").c_str(),         sizeof(cfg.mqtt_pass));
    strlcpy(cfg.mqtt_client, prefs.getString("mqtt_client", "esp32cam").c_str(), sizeof(cfg.mqtt_client));
    strlcpy(cfg.mqtt_prefix, prefs.getString("mqtt_prefix", "cam/01").c_str(),   sizeof(cfg.mqtt_prefix));
    prefs.end();
}

void saveConfig() {
    prefs.begin("camsec", /*readOnly=*/false);
    prefs.putString("wifi_ssid",   cfg.wifi_ssid);
    prefs.putString("wifi_pass",   cfg.wifi_pass);
    prefs.putString("mqtt_broker", cfg.mqtt_broker);
    prefs.putUShort("mqtt_port",   cfg.mqtt_port);
    prefs.putString("mqtt_user",   cfg.mqtt_user);
    prefs.putString("mqtt_pass",   cfg.mqtt_pass);
    prefs.putString("mqtt_client", cfg.mqtt_client);
    prefs.putString("mqtt_prefix", cfg.mqtt_prefix);
    prefs.end();
    Serial.println("[Config] Saved to NVS");
}

// ─── Camera initialisation ────────────────────────────────────────────────────
bool initCamera() {
    bool hasPsram = psramFound();

    camera_config_t cc;
    cc.pin_pwdn      = CAM_PIN_PWDN;
    cc.pin_reset     = CAM_PIN_RESET;
    cc.pin_xclk      = CAM_PIN_XCLK;
    cc.pin_sccb_sda  = CAM_PIN_SIOD;
    cc.pin_sccb_scl  = CAM_PIN_SIOC;
    cc.pin_d7        = CAM_PIN_D7;
    cc.pin_d6        = CAM_PIN_D6;
    cc.pin_d5        = CAM_PIN_D5;
    cc.pin_d4        = CAM_PIN_D4;
    cc.pin_d3        = CAM_PIN_D3;
    cc.pin_d2        = CAM_PIN_D2;
    cc.pin_d1        = CAM_PIN_D1;
    cc.pin_d0        = CAM_PIN_D0;
    cc.pin_vsync     = CAM_PIN_VSYNC;
    cc.pin_href      = CAM_PIN_HREF;
    cc.pin_pclk      = CAM_PIN_PCLK;
    cc.xclk_freq_hz  = 20000000;
    cc.ledc_timer    = LEDC_TIMER_0;
    cc.ledc_channel  = LEDC_CHANNEL_0;
    cc.pixel_format  = PIXFORMAT_JPEG;

    if (hasPsram) {
        cc.frame_size  = FRAMESIZE_UXGA;       // 1600×1200
        cc.jpeg_quality = 10;                  // 0-63, lower = better
        cc.fb_count    = 2;
        cc.grab_mode   = CAMERA_GRAB_LATEST;   // Always grab newest frame
    } else {
        cc.frame_size  = FRAMESIZE_VGA;        // 640×480
        cc.jpeg_quality = 12;
        cc.fb_count    = 1;
        cc.grab_mode   = CAMERA_GRAB_WHEN_EMPTY;
    }

    esp_err_t err = esp_camera_init(&cc);
    if (err != ESP_OK) {
        Serial.printf("[Camera] Init failed: 0x%x\n", err);
        return false;
    }

    // Ensure auto-exposure, auto-gain and auto-white-balance are active
    // so that AGC gain reflects true scene brightness
    sensor_t* s = esp_camera_sensor_get();
    if (s) {
        s->set_whitebal(s, 1);
        s->set_gain_ctrl(s, 1);
        s->set_exposure_ctrl(s, 1);
        s->set_aec2(s, 1);         // Enable AEC DSP
        s->set_aec_value(s, 300);  // Mid-range starting exposure
    }

    Serial.printf("[Camera] OK  resolution=%s  PSRAM=%s\n",
                  hasPsram ? "UXGA" : "VGA",
                  hasPsram ? "yes"  : "no");
    return true;
}

// ─── Light detection (via probe frame JPEG size) ─────────────────────────────

// OV2640 AEC register readback via SCCB is unreliable for live auto-adjusted
// values. Instead, we use the probe JPEG file size as a brightness proxy:
//   • Bright scene → clean image → efficient JPEG → smaller file
//   • Dark scene   → high gain   → noisy image  → more entropy → larger file
// Adjust LOW_LIGHT_SIZE_TH based on the probe_size values printed in the log.
static bool isLowLight(camera_fb_t* probe) {
    uint32_t sz = probe->len;
    Serial.printf("[Light] probe_size=%u threshold=%u\n", sz, LOW_LIGHT_SIZE_TH);
    return sz > LOW_LIGHT_SIZE_TH;
}

static void setFlash(bool on) {
    digitalWrite(FLASH_LED_PIN, on ? HIGH : LOW);
}

// ─── Red status LED ───────────────────────────────────────────────────────────
// Capturing : 2 blinks every 3 s  (ON·80 ms OFF·200 ms ON·80 ms OFF·2640 ms)
// Idle      : 1 blink  every 3 s  (ON·80 ms OFF·2920 ms)
// Called each loop() iteration.
static void updateLed() {
    unsigned long t = millis() % 3000UL;
    bool on;
    if (capturing) {
        on = (t < 80) || (t >= 280 && t < 360);
    } else {
        on = (t < 80);
    }
    digitalWrite(RED_LED_PIN, on ? LOW : HIGH);  // active LOW
}

// ─── Wi-Fi ────────────────────────────────────────────────────────────────────
static void connectWifi() {
    Serial.printf("[WiFi] Connecting to \"%s\"", cfg.wifi_ssid);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(cfg.wifi_ssid, cfg.wifi_pass);

    uint8_t tries = 0;
    while (WiFi.status() != WL_CONNECTED && tries < WIFI_MAX_TRIES) {
        delay(500);
        Serial.print('.');
        ++tries;
    }
    Serial.println();

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[WiFi] Failed – rebooting");
        delay(1000);
        ESP.restart();
    }
    Serial.printf("[WiFi] Connected: %s\n", WiFi.localIP().toString().c_str());
}

// ─── MQTT helpers ─────────────────────────────────────────────────────────────
static void buildTopics() {
    snprintf(topicCmd,    sizeof(topicCmd),    "%s/cmd",         cfg.mqtt_prefix);
    snprintf(topicStatus, sizeof(topicStatus), "%s/status",      cfg.mqtt_prefix);
    snprintf(topicBegin,  sizeof(topicBegin),  "%s/image/begin", cfg.mqtt_prefix);
    snprintf(topicData,   sizeof(topicData),   "%s/image/data",  cfg.mqtt_prefix);
    snprintf(topicEnd,    sizeof(topicEnd),    "%s/image/end",   cfg.mqtt_prefix);
    Serial.printf("[MQTT] Topics prefix: %s\n", cfg.mqtt_prefix);
}

static void mqttCallback(char* topic, byte* payload, unsigned int len) {
    // Accept commands up to 15 chars
    char msg[16] = {0};
    memcpy(msg, payload, min(len, (unsigned int)(sizeof(msg) - 1)));

    Serial.printf("[MQTT] CMD received: \"%s\"\n", msg);

    if (strcmp(msg, "start") == 0) {
        if (!capturing) {
            capturing   = true;
            lastCapture = millis() - CAPTURE_INTERVAL_MS;  // Capture immediately
            mqtt.publish(topicStatus, "capturing", /*retain=*/true);
            Serial.println("[MQTT] Capture STARTED");
        }
    } else if (strcmp(msg, "stop") == 0) {
        if (capturing) {
            capturing = false;
            setFlash(false);
            mqtt.publish(topicStatus, "idle", /*retain=*/true);
            Serial.println("[MQTT] Capture STOPPED");
        }
    } else if (strcmp(msg, "flash_on") == 0) {
        flashOverride   = true;
        flashOverrideOn = true;
        Serial.println("[MQTT] Flash forced ON");
    } else if (strcmp(msg, "flash_off") == 0) {
        flashOverride   = true;
        flashOverrideOn = false;
        setFlash(false);
        Serial.println("[MQTT] Flash forced OFF");
    } else if (strcmp(msg, "flash_auto") == 0) {
        flashOverride = false;
        Serial.println("[MQTT] Flash set to AUTO");
    }
}

static void connectMqtt() {
    mqtt.setKeepAlive(MQTT_KEEPALIVE_S);

    while (!mqtt.connected()) {
        Serial.printf("[MQTT] Connecting to %s:%u … ", cfg.mqtt_broker, cfg.mqtt_port);

        const char* user = (strlen(cfg.mqtt_user) > 0) ? cfg.mqtt_user : nullptr;
        const char* pass = (strlen(cfg.mqtt_pass) > 0) ? cfg.mqtt_pass : nullptr;

        // Last-will: publish "offline" if connection drops unexpectedly
        bool ok = mqtt.connect(
            cfg.mqtt_client,
            user, pass,
            topicStatus, /*QoS*/0, /*retain*/true, "offline"
        );

        if (ok) {
            Serial.println("OK");
            mqtt.subscribe(topicCmd, /*QoS*/0);
            mqtt.publish(topicStatus, capturing ? "capturing" : "idle", /*retain=*/true);
        } else {
            Serial.printf("failed (rc=%d) – retry in %u s\n",
                          mqtt.state(), MQTT_RECONNECT_MS / 1000);
            delay(MQTT_RECONNECT_MS);
        }
    }
}

// ─── Capture and chunk-send ───────────────────────────────────────────────────
static void captureAndSend() {
    // --- Step 1: Probe frame with flash OFF – used as light-level sensor ---
    setFlash(false);
    camera_fb_t* probe = esp_camera_fb_get();
    if (!probe) {
        Serial.println("[Camera] Probe frame failed");
        return;
    }
    bool dark = flashOverride ? flashOverrideOn : isLowLight(probe);
    esp_camera_fb_return(probe);

    // --- Step 2: Enable flash if dark, capture real frame ---
    setFlash(dark);
    if (dark) delay(FLASH_DELAY_MS);

    camera_fb_t* fb = esp_camera_fb_get();
    setFlash(false);  // Turn off flash immediately after capture

    if (!fb) {
        Serial.println("[Camera] Capture failed");
        return;
    }

    uint32_t totalLen  = (uint32_t)fb->len;
    uint16_t numChunks = (uint16_t)((totalLen + CHUNK_SIZE - 1) / CHUNK_SIZE);
    uint32_t id        = ++imageId;

    Serial.printf("[Image] #%u  size=%u B  chunks=%u  dark=%d\n",
                  id, totalLen, numChunks, (int)dark);

    // --- Step 3: Publish BEGIN notification ---
    char jsonBuf[160];
    snprintf(jsonBuf, sizeof(jsonBuf),
             "{\"id\":%u,\"size\":%u,\"chunks\":%u,\"dark\":%d}",
             id, totalLen, numChunks, dark ? 1 : 0);
    mqtt.publish(topicBegin, (uint8_t*)jsonBuf, strlen(jsonBuf), /*retain=*/false);

    // --- Step 4: Publish CHUNKs ---
    // Binary layout: [image_id: 4 B big-endian][chunk_index: 2 B big-endian][JPEG data]
    uint8_t hdr[6];
    hdr[0] = (id >> 24) & 0xFF;
    hdr[1] = (id >> 16) & 0xFF;
    hdr[2] = (id >>  8) & 0xFF;
    hdr[3] =  id        & 0xFF;

    bool sendOk = true;
    for (uint16_t i = 0; i < numChunks; i++) {
        uint32_t offset   = (uint32_t)i * CHUNK_SIZE;
        uint32_t chunkLen = (totalLen - offset < CHUNK_SIZE)
                                ? (totalLen - offset)
                                : CHUNK_SIZE;
        hdr[4] = (i >> 8) & 0xFF;
        hdr[5] =  i       & 0xFF;

        // beginPublish streams header + payload directly without extra buffering
        if (!mqtt.beginPublish(topicData, 6 + chunkLen, /*retain=*/false)) {
            Serial.printf("[MQTT] beginPublish failed at chunk %u\n", i);
            sendOk = false;
            break;
        }
        mqtt.write(hdr, 6);
        mqtt.write(fb->buf + offset, chunkLen);
        mqtt.endPublish();

        mqtt.loop();  // Service MQTT heartbeat during long transmissions
        yield();
    }

    // --- Step 5: Publish END notification ---
    snprintf(jsonBuf, sizeof(jsonBuf),
             "{\"id\":%u,\"chunks\":%u,\"ok\":%d}",
             id, numChunks, sendOk ? 1 : 0);
    mqtt.publish(topicEnd, (uint8_t*)jsonBuf, strlen(jsonBuf), /*retain=*/false);

    esp_camera_fb_return(fb);
    Serial.printf("[Image] #%u %s\n", id, sendOk ? "sent OK" : "FAILED");
}

// =============================================================================
// setup()
// =============================================================================
void setup() {
    Serial.begin(115200);
    Serial.println("\n===== CamSec ESP32-CAM =====");

    // Flash LED and config button
    pinMode(FLASH_LED_PIN, OUTPUT);
    digitalWrite(FLASH_LED_PIN, LOW);
    pinMode(RED_LED_PIN, OUTPUT);
    digitalWrite(RED_LED_PIN, HIGH);  // OFF (active LOW)
    pinMode(CONFIG_BUTTON_PIN, INPUT_PULLUP);

    // ── Config-mode detection: 5-second window ──────────────────────────────
    // Must happen BEFORE camera init because GPIO0 is repurposed as XCLK
    Serial.println("[Boot] Press BOOT within 5 s to enter config mode…");
    bool configMode = false;
    unsigned long windowEnd = millis() + CONFIG_WINDOW_MS;

    while (millis() < windowEnd) {
        // Fast blink red LED during window (active LOW)
        digitalWrite(RED_LED_PIN, ((millis() / 150) & 1) ? LOW : HIGH);
        if (digitalRead(CONFIG_BUTTON_PIN) == LOW) {
            configMode = true;
            Serial.println("[Boot] BOOT pressed – entering config mode");
            break;
        }
        delay(10);
    }
    digitalWrite(RED_LED_PIN, HIGH);  // OFF

    // ── Load stored config ────────────────────────────────────────────────
    loadConfig();

    // ── Config portal (AP mode) ───────────────────────────────────────────
    if (configMode) {
        Serial.println("[Boot] Entering config portal…");
        startConfigPortal(cfg, saveConfig);
        // startConfigPortal() calls ESP.restart() on save and never returns here
        return;
    }

    // ── Normal operation ──────────────────────────────────────────────────
    if (!initCamera()) {
        Serial.println("[Boot] Camera init failed – rebooting in 3 s");
        delay(3000);
        ESP.restart();
    }

    connectWifi();
    buildTopics();

    mqtt.setServer(cfg.mqtt_broker, cfg.mqtt_port);
    mqtt.setCallback(mqttCallback);
    mqtt.setBufferSize(512);  // Enough for small control messages

    connectMqtt();

    // First capture triggers immediately (no initial delay)
    lastCapture = millis() - CAPTURE_INTERVAL_MS;

    Serial.println("[Boot] Ready – capturing every 5.5 s");
}

// =============================================================================
// loop()
// =============================================================================
void loop() {
    // Reconnect Wi-Fi if dropped
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[WiFi] Connection lost – reconnecting…");
        connectWifi();
    }

    // Reconnect MQTT if dropped
    if (!mqtt.connected()) {
        Serial.println("[MQTT] Connection lost – reconnecting…");
        connectMqtt();
    }

    mqtt.loop();
    updateLed();

    // Timed image capture
    if (capturing && (millis() - lastCapture >= CAPTURE_INTERVAL_MS)) {
        lastCapture = millis();
        captureAndSend();
    }
}

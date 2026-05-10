#pragma once
#include <stdint.h>

// Application configuration – persisted in ESP32 NVS (flash)
struct AppConfig {
    char     wifi_ssid[64];     // Wi-Fi network name
    char     wifi_pass[64];     // Wi-Fi password
    char     mqtt_broker[64];   // MQTT broker IP or hostname
    uint16_t mqtt_port;         // MQTT port (default 1883)
    char     mqtt_user[64];     // MQTT username (empty = no auth)
    char     mqtt_pass[64];     // MQTT password (empty = no auth)
    char     mqtt_client[32];   // MQTT client ID
    char     mqtt_prefix[64];   // MQTT topic prefix (e.g. "cam/01")
    uint8_t  cam_flip;          // 0=normal, 1=cap per avall (180°)
};

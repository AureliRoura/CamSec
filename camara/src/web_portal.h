#pragma once
// ─────────────────────────────────────────────────────────────────────────────
// web_portal.h  –  Access-Point config portal for CamSec ESP32-CAM
//
// Usage:
//   Call startConfigPortal(cfg, saveFn) to enter AP mode.
//   The function blocks in its own loop. On form submission it calls saveFn(),
//   serves a confirmation page, and reboots the ESP32.
//
// AP credentials: SSID="CamSec-Config"  Password="camsec123"
//   (change AP_PASSWORD below if desired)
// ─────────────────────────────────────────────────────────────────────────────

#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include "config.h"

#define AP_SSID     "CamSec-Config"
#define AP_PASSWORD "camsec123"
#define AP_CHANNEL  1
#define DNS_PORT    53

// ─── Embedded HTML pages ─────────────────────────────────────────────────────

static const char PORTAL_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="ca">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CamSec &ndash; Configuraci&oacute;</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:Arial,sans-serif;background:#eceff1;color:#333;padding:1em}
  .card{background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.15);
        max-width:480px;margin:1em auto;padding:1.5em}
  h1{color:#1565C0;font-size:1.4em;margin-bottom:.2em}
  .sub{color:#888;font-size:.8em;margin-bottom:1.2em}
  h2{font-size:.95em;text-transform:uppercase;letter-spacing:.05em;
     color:#1565C0;border-bottom:2px solid #1565C0;padding-bottom:.3em;
     margin:1.2em 0 .6em}
  label{display:block;font-size:.82em;color:#555;margin-top:.6em;margin-bottom:.15em}
  input[type=text],input[type=password],input[type=number]{
        width:100%;padding:.5em .7em;border:1px solid #bbb;border-radius:5px;
        font-size:.95em;transition:border-color .2s}
  input:focus{outline:none;border-color:#1565C0}
  .hint{font-size:.75em;color:#aaa;margin-top:.2em}
  .pw-wrap{position:relative}
  .pw-wrap input{padding-right:2.4em}
  .pw-toggle{position:absolute;right:.5em;top:50%;transform:translateY(-50%);
             background:none;border:none;cursor:pointer;padding:0;margin:0;
             font-size:1.1em;color:#888;width:auto;line-height:1}
  .pw-toggle:hover{color:#1565C0}
  button[type=submit]{margin-top:1.5em;width:100%;padding:.85em;background:#1565C0;color:#fff;
         border:none;border-radius:6px;font-size:1.05em;font-weight:bold;cursor:pointer}
  button[type=submit]:hover{background:#0D47A1}
</style>
<script>
function togglePw(btn){
  var inp=btn.previousElementSibling;
  var show=inp.type==='password';
  inp.type=show?'text':'password';
  btn.innerHTML=show?'&#128065;':'&#128274;';
}
</script>
</head>
<body>
<div class="card">
  <h1>&#128247; CamSec</h1>
  <p class="sub">Configuraci&oacute; de la c&agrave;mera ESP32-CAM</p>

  <form action="/save" method="POST">

    <h2>&#128246; Wi-Fi</h2>
    <label>SSID (nom de la xarxa)</label>
    <input type="text" name="wifi_ssid" maxlength="63" value="{SSID}" required>
    <label>Contrasenya Wi-Fi</label>
    <div class="pw-wrap">
      <input type="password" name="wifi_pass" maxlength="63" placeholder="(deixar buit per mantenir l'actual)">
      <button type="button" class="pw-toggle" onclick="togglePw(this)" title="Mostra/amaga">&#128274;</button>
    </div>
    <p class="hint">Deixa buit per no modificar la contrasenya emmagatzemada.</p>

    <h2>&#128200; MQTT</h2>
    <label>Broker (IP o hostname)</label>
    <input type="text" name="mqtt_broker" maxlength="63" value="{BROKER}" required>
    <label>Port</label>
    <input type="number" name="mqtt_port" min="1" max="65535" value="{PORT}" required>
    <label>Usuari MQTT <em style="color:#aaa">(opcional)</em></label>
    <input type="text" name="mqtt_user" maxlength="63" value="{MUSER}">
    <label>Contrasenya MQTT <em style="color:#aaa">(opcional)</em></label>
    <div class="pw-wrap">
      <input type="password" name="mqtt_pass" maxlength="63" placeholder="(deixar buit per mantenir l'actual)">
      <button type="button" class="pw-toggle" onclick="togglePw(this)" title="Mostra/amaga">&#128274;</button>
    </div>
    <p class="hint">Deixa buit per no modificar la contrasenya emmagatzemada.</p>
    <label>Client ID</label>
    <input type="text" name="mqtt_client" maxlength="31" value="{CLIENT}" required>
    <label>Prefix de topic</label>
    <input type="text" name="mqtt_prefix" maxlength="63" value="{PREFIX}" required>
    <p class="hint">Exemple: <code>cam/01</code> &rarr; topics: <code>cam/01/status</code>, <code>cam/01/image/data</code>, etc.</p>

    <h2>&#128247; C&agrave;mera</h2>
    <label>Interval entre captures (ms)</label>
    <input type="number" name="capture_interval_ms" min="1000" max="3600000" value="{INTERVAL}" required>
    <p class="hint">Temps entre captures en mil&middot;lisegons (p. ex. <code>5500</code> = 5,5 s).</p>
    <label>Orientaci&oacute; de la c&agrave;mera</label>
    <select name="cam_flip" style="width:100%;padding:.5em .7em;border:1px solid #bbb;border-radius:5px;font-size:.95em">
      <option value="0" {SEL_NORMAL}>Normal (cap amunt)</option>
      <option value="1" {SEL_FLIP}>Cap per avall (rotar 180&deg;)</option>
    </select>
    <p class="hint">Selecciona &laquo;Cap per avall&raquo; si la c&agrave;mera est&agrave; muntada al rev&eacute;s.</p>

    <button type="submit">&#128190; Desar i reiniciar</button>
  </form>
</div>
</body>
</html>
)rawliteral";

static const char SAVED_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="ca">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Desat!</title>
<style>
  body{font-family:Arial,sans-serif;background:#eceff1;display:flex;
       align-items:center;justify-content:center;min-height:100vh;margin:0}
  .card{background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.15);
        padding:2em;text-align:center;max-width:360px}
  h1{color:#2e7d32;font-size:1.5em;margin-bottom:.5em}
  p{color:#555;font-size:.95em}
  .ico{font-size:3em;margin-bottom:.5em}
</style>
</head>
<body>
<div class="card">
  <div class="ico">&#10003;</div>
  <h1>Configuraci&oacute; desada!</h1>
  <p>El dispositiu es reiniciar&agrave; en uns segons i intentar&agrave; connectar-se a la xarxa configurada.</p>
</div>
</body>
</html>
)rawliteral";

// ─── Portal internals ─────────────────────────────────────────────────────────

static WebServer _portalServer(80);
static DNSServer _dnsServer;
static AppConfig* _pCfg      = nullptr;
static void (*_pSaveFn)()    = nullptr;

// Build the config page substituting current values (passwords never pre-filled)
static String _buildPage(const AppConfig& cfg) {
    String html = FPSTR(PORTAL_HTML);
    html.replace("{SSID}",       String(cfg.wifi_ssid));
    html.replace("{BROKER}",     String(cfg.mqtt_broker));
    html.replace("{PORT}",       String(cfg.mqtt_port));
    html.replace("{MUSER}",      String(cfg.mqtt_user));
    html.replace("{CLIENT}",     String(cfg.mqtt_client));
    html.replace("{PREFIX}",     String(cfg.mqtt_prefix));
    html.replace("{SEL_NORMAL}", cfg.cam_flip == 0 ? "selected" : "");
    html.replace("{SEL_FLIP}",   cfg.cam_flip == 1 ? "selected" : "");
    html.replace("{INTERVAL}",   String(cfg.capture_interval_ms));
    return html;
}

static void _handleRoot() {
    _portalServer.send(200, "text/html; charset=utf-8", _buildPage(*_pCfg));
}

static void _handleSave() {
    AppConfig& cfg = *_pCfg;

    // Copy fields – bounded to prevent buffer overflow
    if (_portalServer.hasArg("wifi_ssid") && _portalServer.arg("wifi_ssid").length() > 0)
        strlcpy(cfg.wifi_ssid,   _portalServer.arg("wifi_ssid").c_str(),   sizeof(cfg.wifi_ssid));

    // Only overwrite password if the user typed something
    if (_portalServer.hasArg("wifi_pass") && _portalServer.arg("wifi_pass").length() > 0)
        strlcpy(cfg.wifi_pass,   _portalServer.arg("wifi_pass").c_str(),   sizeof(cfg.wifi_pass));

    if (_portalServer.hasArg("mqtt_broker") && _portalServer.arg("mqtt_broker").length() > 0)
        strlcpy(cfg.mqtt_broker, _portalServer.arg("mqtt_broker").c_str(), sizeof(cfg.mqtt_broker));

    if (_portalServer.hasArg("mqtt_port")) {
        int p = _portalServer.arg("mqtt_port").toInt();
        if (p > 0 && p <= 65535) cfg.mqtt_port = (uint16_t)p;
    }

    if (_portalServer.hasArg("mqtt_user"))
        strlcpy(cfg.mqtt_user,   _portalServer.arg("mqtt_user").c_str(),   sizeof(cfg.mqtt_user));

    if (_portalServer.hasArg("mqtt_pass") && _portalServer.arg("mqtt_pass").length() > 0)
        strlcpy(cfg.mqtt_pass,   _portalServer.arg("mqtt_pass").c_str(),   sizeof(cfg.mqtt_pass));

    if (_portalServer.hasArg("mqtt_client") && _portalServer.arg("mqtt_client").length() > 0)
        strlcpy(cfg.mqtt_client, _portalServer.arg("mqtt_client").c_str(), sizeof(cfg.mqtt_client));

    if (_portalServer.hasArg("mqtt_prefix") && _portalServer.arg("mqtt_prefix").length() > 0)
        strlcpy(cfg.mqtt_prefix, _portalServer.arg("mqtt_prefix").c_str(), sizeof(cfg.mqtt_prefix));

    if (_portalServer.hasArg("cam_flip"))
        cfg.cam_flip = (_portalServer.arg("cam_flip") == "1") ? 1 : 0;

    if (_portalServer.hasArg("capture_interval_ms")) {
        long v = _portalServer.arg("capture_interval_ms").toInt();
        if (v >= 1000 && v <= 3600000) cfg.capture_interval_ms = (uint32_t)v;
    }

    // Persist to NVS
    _pSaveFn();

    _portalServer.send(200, "text/html; charset=utf-8", FPSTR(SAVED_HTML));

    // Reboot after brief delay so the browser receives the response
    delay(3500);
    ESP.restart();
}

// Captive-portal redirect: send any unknown URL back to root
static void _handleNotFound() {
    _portalServer.sendHeader("Location", "http://192.168.4.1/");
    _portalServer.send(302, "text/plain", "");
}

// Captive-portal detection endpoints for Android, iOS and Windows.
// Returning the config page (instead of the expected response) causes
// each OS to recognise there is a captive portal and open the browser.
static void _handleCaptivePortal() {
    _portalServer.sendHeader("Location", "http://192.168.4.1/");
    _portalServer.send(302, "text/plain", "");
}

// ─── Public API ──────────────────────────────────────────────────────────────

/**
 * Start Access-Point config portal.
 * Blocks indefinitely until the user saves the configuration,
 * at which point saveFn() is called and the ESP32 reboots.
 *
 * @param cfg     Reference to the AppConfig to be filled.
 * @param saveFn  Callback that persists cfg to NVS.
 */
void startConfigPortal(AppConfig& cfg, void (*saveFn)()) {
    _pCfg    = &cfg;
    _pSaveFn = saveFn;

    // Signal entry into config mode with 3 red LED blinks (active LOW)
    pinMode(33, OUTPUT);
    for (int i = 0; i < 3; i++) {
        digitalWrite(33, LOW);  delay(200);
        digitalWrite(33, HIGH); delay(200);
    }

    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, AP_PASSWORD, AP_CHANNEL);
    delay(500);

    IPAddress apIP = WiFi.softAPIP();
    Serial.printf("[Portal] AP IP : %s\n", apIP.toString().c_str());
    Serial.printf("[Portal] SSID  : %s\n", AP_SSID);
    Serial.printf("[Portal] Pass  : %s\n", AP_PASSWORD);
    Serial.println("[Portal] Connect and browse to http://192.168.4.1/");

    // DNS: redirect every domain to this device (captive portal behaviour)
    _dnsServer.start(DNS_PORT, "*", apIP);

    _portalServer.on("/",     HTTP_GET,  _handleRoot);
    _portalServer.on("/save", HTTP_POST, _handleSave);

    // Captive-portal detection URLs (Android, iOS, Windows)
    _portalServer.on("/generate_204",              HTTP_GET, _handleCaptivePortal); // Android
    _portalServer.on("/gen_204",                   HTTP_GET, _handleCaptivePortal); // Android
    _portalServer.on("/hotspot-detect.html",        HTTP_GET, _handleCaptivePortal); // Apple
    _portalServer.on("/library/test/success.html",  HTTP_GET, _handleCaptivePortal); // Apple
    _portalServer.on("/canonical.html",             HTTP_GET, _handleCaptivePortal); // Apple
    _portalServer.on("/success.txt",                HTTP_GET, _handleCaptivePortal); // Apple
    _portalServer.on("/ncsi.txt",                   HTTP_GET, _handleCaptivePortal); // Windows
    _portalServer.on("/connecttest.txt",            HTTP_GET, _handleCaptivePortal); // Windows
    _portalServer.on("/redirect",                   HTTP_GET, _handleCaptivePortal); // Generic
    _portalServer.on("/favicon.ico",                HTTP_GET, []() { _portalServer.send(204); });

    _portalServer.onNotFound(_handleNotFound);
    _portalServer.begin();

    // GPIO33 (red LED) must stay OFF while WiFi AP is active to avoid interference.
    // Short pulse (50 ms ON / 2 s OFF) minimises WiFi disruption.
    digitalWrite(33, HIGH);  // OFF
    uint32_t lastBlink = 0;
    bool     ledOn     = false;

    for (;;) {
        _dnsServer.processNextRequest();
        _portalServer.handleClient();

        uint32_t now = millis();
        if (!ledOn && now - lastBlink >= 2000) {
            digitalWrite(33, LOW);   // ON
            ledOn     = true;
            lastBlink = now;
        } else if (ledOn && now - lastBlink >= 50) {
            digitalWrite(33, HIGH);  // OFF
            ledOn     = false;
        }

        yield();
    }
}

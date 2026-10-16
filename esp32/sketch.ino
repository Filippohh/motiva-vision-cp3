/*
  Motiva Vision - ESP32 de sinalização da via (Wokwi ou placa física)

  Assina  motiva/<RM>/comandos  (QoS 1)  -> SUSPEITA | CONFIRMAR | REJEITAR | ENCERRAR | PING
  Publica motiva/<RM>/status             -> ACK (RECEBIDO/APLICADO/DUPLICADO/IGNORADO_ENCERRADO),
                                            HEARTBEAT a cada 5 s, ONLINE (retida) e LWT offline (retida)

  Estados (LED RGB + buzzer + LCD 16x2 I2C):
    NORMAL      verde fixo                       nenhuma ocorrência ativa
    SUSPEITA    amarelo piscando 1 Hz            aguardando avaliação humana (sinal preliminar)
    CONFIRMADO  vermelho piscando 4 Hz + bipes   operador confirmou, sinalização de acidente ativa
    FALHA       roxo piscando (2 flashes)        sem broker OU sem PING da central há > 15 s

  Tudo com millis(): nenhuma chamada bloqueante longa no loop, reconexão a cada 3 s.
*/
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// ----------------------------------------------------------------- configuração
#define RM "rm565994"                    // <- mesmo valor de MOTIVA_RM na central
const char* WIFI_SSID = "Wokwi-GUEST";   // placa física: usar a rede local
const char* WIFI_PASS = "";
const char* MQTT_HOST = "broker.hivemq.com";
const uint16_t MQTT_PORT = 1883;

const char* T_CMD = "motiva/" RM "/comandos";
const char* T_STATUS = "motiva/" RM "/status";

const int PIN_R = 25, PIN_G = 26, PIN_B = 27, PIN_BUZZER = 14;
const unsigned long HEARTBEAT_MS = 5000, CENTRAL_TIMEOUT_MS = 15000, RECONNECT_MS = 3000;

// ----------------------------------------------------------------- estado
enum Estado { NORMAL, SUSPEITA, CONFIRMADO, FALHA };
const char* NOMES[] = {"NORMAL", "SUSPEITA", "CONFIRMADO", "FALHA"};

struct Ocorrencia { char id[48]; Estado st; bool usado; };
const int MAX_OC = 8, MAX_CMD = 32, MAX_FECH = 16;
Ocorrencia ocs[MAX_OC];
char cmdsProcessados[MAX_CMD][24]; int cmdIdx = 0;     // anel de command_id já aplicados
char fechados[MAX_FECH][48];       int fechIdx = 0;    // anel de event_id encerrados/rejeitados

WiFiClient net;
PubSubClient mqtt(net);
LiquidCrystal_I2C lcd(0x27, 16, 2);

unsigned long lastReconnect = 0, lastHeartbeat = 0, lastCentral = 0, lastBlink = 0, bootMs = 0;
bool blinkOn = false, centralVista = false;
Estado mostrado = FALHA;
String motivoFalha = "inicializando";
String ultimoCmd = "-";
char clientId[40];

// ----------------------------------------------------------------- utilidades
bool contem(char arr[][48], int n, const char* s) { for (int i = 0; i < n; i++) if (strcmp(arr[i], s) == 0) return true; return false; }
bool cmdJaProcessado(const char* c) { for (int i = 0; i < MAX_CMD; i++) if (strcmp(cmdsProcessados[i], c) == 0) return true; return false; }
void marcarCmd(const char* c) { strncpy(cmdsProcessados[cmdIdx], c, 23); cmdsProcessados[cmdIdx][23] = 0; cmdIdx = (cmdIdx + 1) % MAX_CMD; }
void marcarFechado(const char* e) { strncpy(fechados[fechIdx], e, 47); fechados[fechIdx][47] = 0; fechIdx = (fechIdx + 1) % MAX_FECH; }

int acharOc(const char* id) { for (int i = 0; i < MAX_OC; i++) if (ocs[i].usado && strcmp(ocs[i].id, id) == 0) return i; return -1; }
int novaOc(const char* id) {
  for (int i = 0; i < MAX_OC; i++) if (!ocs[i].usado) { ocs[i].usado = true; strncpy(ocs[i].id, id, 47); ocs[i].id[47] = 0; ocs[i].st = SUSPEITA; return i; }
  return -1;
}
int ativas() { int n = 0; for (int i = 0; i < MAX_OC; i++) if (ocs[i].usado) n++; return n; }

Estado estadoOcorrencias() {
  Estado e = NORMAL;
  for (int i = 0; i < MAX_OC; i++) if (ocs[i].usado) { if (ocs[i].st == CONFIRMADO) return CONFIRMADO; e = SUSPEITA; }
  return e;
}

Estado estadoAtual() {
  if (!mqtt.connected()) { motivoFalha = "sem broker"; return FALHA; }
  if (!centralVista || millis() - lastCentral > CENTRAL_TIMEOUT_MS) { motivoFalha = "central muda"; return FALHA; }
  return estadoOcorrencias();
}

void publicarStatus(JsonDocument& doc, bool retain = false) {
  char buf[384];
  size_t n = serializeJson(doc, buf);
  if (mqtt.connected()) mqtt.publish(T_STATUS, (const uint8_t*)buf, n, retain);
  Serial.print("[TX] "); Serial.println(buf);
}

void ack(const char* cmdId, const char* eventId, const char* fase, const char* acao) {
  StaticJsonDocument<320> d;
  d["tipo"] = "ACK"; d["command_id"] = cmdId; d["event_id"] = eventId; d["acao"] = acao;
  d["fase"] = fase; d["estado"] = NOMES[estadoOcorrencias()]; d["uptime_s"] = (millis() - bootMs) / 1000;
  publicarStatus(d);
}

// ----------------------------------------------------------------- comandos
void onMessage(char* topic, byte* payload, unsigned int len) {
  StaticJsonDocument<512> doc;
  if (deserializeJson(doc, payload, len)) { Serial.println("[RX] JSON inválido"); return; }
  const char* acao = doc["acao"] | "";
  lastCentral = millis(); centralVista = true;          // qualquer mensagem da central conta como sinal de vida
  if (strcmp(acao, "PING") == 0) return;

  const char* cmdId = doc["command_id"] | "";
  const char* evId = doc["event_id"] | "";
  Serial.printf("[RX] %s cmd=%s ev=%s\n", acao, cmdId, evId);
  if (!*cmdId || !*evId) return;

  ack(cmdId, evId, "RECEBIDO", acao);
  if (cmdJaProcessado(cmdId)) { ack(cmdId, evId, "DUPLICADO", acao); return; }   // não repete atuação
  marcarCmd(cmdId);

  bool fechado = contem(fechados, MAX_FECH, evId);
  int i = acharOc(evId);
  if (strcmp(acao, "SUSPEITA") == 0 || strcmp(acao, "CONFIRMAR") == 0) {
    if (fechado) { ack(cmdId, evId, "IGNORADO_ENCERRADO", acao); return; }   // não reabre evento encerrado
    if (i < 0) i = novaOc(evId);
    if (i < 0) { ack(cmdId, evId, "IGNORADO_SEM_ESPACO", acao); return; }
    if (strcmp(acao, "CONFIRMAR") == 0) ocs[i].st = CONFIRMADO;
    else if (ocs[i].st != CONFIRMADO) ocs[i].st = SUSPEITA;
  } else if (strcmp(acao, "REJEITAR") == 0 || strcmp(acao, "ENCERRAR") == 0) {
    if (i >= 0) ocs[i].usado = false;               // ocorrência encerrada -> volta a NORMAL se não houver outras
    if (!fechado) marcarFechado(evId);
  } else {
    ack(cmdId, evId, "ACAO_DESCONHECIDA", acao); return;
  }
  ultimoCmd = String(acao) + " " + String(evId).substring(String(evId).length() - 4);
  ack(cmdId, evId, "APLICADO", acao);
}

// ----------------------------------------------------------------- conectividade
void manterConexao() {
  if (WiFi.status() != WL_CONNECTED) {
    if (millis() - lastReconnect > RECONNECT_MS) { lastReconnect = millis(); WiFi.disconnect(); WiFi.begin(WIFI_SSID, WIFI_PASS, 6); Serial.println("[WiFi] reconectando..."); }
    return;
  }
  if (mqtt.connected()) return;
  if (millis() - lastReconnect < RECONNECT_MS) return;
  lastReconnect = millis();
  Serial.print("[MQTT] conectando a "); Serial.println(MQTT_HOST);
  const char* will = "{\"tipo\":\"LWT\",\"online\":false}";
  if (mqtt.connect(clientId, nullptr, nullptr, T_STATUS, 1, true, will)) {
    mqtt.subscribe(T_CMD, 1);
    StaticJsonDocument<128> d; d["tipo"] = "ONLINE"; d["online"] = true; d["estado"] = NOMES[estadoOcorrencias()];
    publicarStatus(d, true);
    Serial.println("[MQTT] conectado");
  } else {
    Serial.printf("[MQTT] falhou rc=%d (nova tentativa em %lus)\n", mqtt.state(), RECONNECT_MS / 1000);
  }
}

// ----------------------------------------------------------------- sinalização
void rgb(bool r, bool g, bool b) { digitalWrite(PIN_R, r); digitalWrite(PIN_G, g); digitalWrite(PIN_B, b); }

void atualizarSinalizacao(Estado e) {
  unsigned long t = millis();
  switch (e) {
    case NORMAL: rgb(0, 1, 0); noTone(PIN_BUZZER); break;
    case SUSPEITA: rgb((t / 500) % 2 == 0, (t / 500) % 2 == 0, 0); noTone(PIN_BUZZER); break;    // amarelo 1 Hz
    case CONFIRMADO: {
      bool on = (t / 125) % 2 == 0;                                                               // vermelho 4 Hz
      rgb(on, 0, 0);
      unsigned long p = t % 2000;                                                                 // 2 bipes a cada 2 s
      if (p < 150 || (p > 300 && p < 450)) tone(PIN_BUZZER, 2000); else noTone(PIN_BUZZER);
      break;
    }
    case FALHA: {
      unsigned long p = t % 1500;                                                                 // roxo, 2 flashes
      bool on = p < 120 || (p > 250 && p < 370);
      rgb(on, 0, on); noTone(PIN_BUZZER);
      break;
    }
  }
}

void atualizarLCD(Estado e) {
  static Estado ultimo = (Estado)-1; static String l2ant = "";
  String l2;
  if (e == FALHA) l2 = motivoFalha;
  else if (e == NORMAL) l2 = "monitorando";
  else l2 = String(ativas()) + " ativa " + ultimoCmd;
  if (e == ultimo && l2 == l2ant) return;   // só escreve no I2C quando muda
  lcd.clear(); lcd.setCursor(0, 0); lcd.print("Via: "); lcd.print(NOMES[e]);
  lcd.setCursor(0, 1); lcd.print(l2.substring(0, 16));
  ultimo = e; l2ant = l2;
}

// ----------------------------------------------------------------- setup/loop
void setup() {
  Serial.begin(115200);
  pinMode(PIN_R, OUTPUT); pinMode(PIN_G, OUTPUT); pinMode(PIN_B, OUTPUT); pinMode(PIN_BUZZER, OUTPUT);
  lcd.init(); lcd.backlight(); lcd.print("Motiva Vision"); lcd.setCursor(0, 1); lcd.print("iniciando...");
  bootMs = millis();
  snprintf(clientId, sizeof(clientId), "motiva-esp32-%s-%04X", RM, (unsigned)(esp_random() & 0xFFFF));
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS, 6);
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(768);
  mqtt.setKeepAlive(10);
  mqtt.setSocketTimeout(2);   // limita bloqueio do connect()
}

void loop() {
  manterConexao();
  mqtt.loop();

  Estado e = estadoAtual();
  if (e != mostrado) { Serial.printf("[ESTADO] %s -> %s\n", NOMES[mostrado], NOMES[e]); mostrado = e; }
  atualizarSinalizacao(e);
  atualizarLCD(e);

  if (mqtt.connected() && millis() - lastHeartbeat > HEARTBEAT_MS) {
    lastHeartbeat = millis();
    StaticJsonDocument<256> d;
    d["tipo"] = "HEARTBEAT"; d["estado"] = NOMES[e]; d["estado_ocorrencias"] = NOMES[estadoOcorrencias()];
    d["ativas"] = ativas(); d["uptime_s"] = (millis() - bootMs) / 1000; d["rssi"] = WiFi.RSSI();
    d["ultimo_cmd"] = ultimoCmd;
    publicarStatus(d);
  }
  delay(5);
}

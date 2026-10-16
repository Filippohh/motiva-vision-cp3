"""Gêmeo em Python do firmware do ESP32 (mesma lógica de esp32/sketch.ino).

Serve para testes automatizados de integração sem a placa/Wokwi: estados
NORMAL/SUSPEITA/CONFIRMADO/FALHA, ACK RECEBIDO/APLICADO, proteção contra
command_id duplicado, não reabertura de eventos encerrados, heartbeat, LWT e
reconexão sem travar. A "sinalização" é impressa no console.

Uso: python -m tools.esp32_sim
"""
import json
import time
import uuid
from collections import deque

from common.mqtt import make_client
from config import settings

HEARTBEAT_S, CENTRAL_TIMEOUT_S = 5, 15


class Esp32Sim:
    def __init__(self, log=print):
        self.log = log
        self.ocs = {}                       # event_id -> SUSPEITA | CONFIRMADO
        self.cmds = deque(maxlen=32)
        self.fechados = deque(maxlen=16)
        self.last_central = None
        self.connected = False
        self.mostrado = "FALHA"
        self.ultimo_cmd = "-"
        self.transicoes = []                # (t, de, para) para evidência
        self.boot = time.time()
        self.cli = make_client(f"motiva-esp32sim-{uuid.uuid4().hex[:4]}", settings.TOPIC_STATUS,
                               json.dumps({"tipo": "LWT", "online": False}))
        self.cli.on_connect = self._on_connect
        self.cli.on_disconnect = self._on_disconnect
        self.cli.on_message = self._on_message

    # ------------------------------------------------------------ estado
    def estado_ocorrencias(self):
        if "CONFIRMADO" in self.ocs.values():
            return "CONFIRMADO"
        return "SUSPEITA" if self.ocs else "NORMAL"

    def estado(self):
        if not self.connected:
            return "FALHA"
        if self.last_central is None or time.time() - self.last_central > CENTRAL_TIMEOUT_S:
            return "FALHA"
        return self.estado_ocorrencias()

    def _pub(self, d, retain=False):
        if self.connected:
            self.cli.publish(settings.TOPIC_STATUS, json.dumps(d), qos=0, retain=retain)

    def _ack(self, cid, eid, fase, acao):
        self._pub({"tipo": "ACK", "command_id": cid, "event_id": eid, "acao": acao, "fase": fase,
                   "estado": self.estado_ocorrencias(), "uptime_s": int(time.time() - self.boot)})
        self.log(f"[esp32-sim] ACK {fase:18s} {acao:9s} {cid} {eid}")

    # ------------------------------------------------------------ MQTT
    def _on_connect(self, c, u, f, rc):
        if rc != 0:
            return
        self.connected = True
        c.subscribe(settings.TOPIC_COMANDOS, qos=1)
        self._pub({"tipo": "ONLINE", "online": True, "estado": self.estado_ocorrencias()}, retain=True)
        self.log("[esp32-sim] conectado ao broker")

    def _on_disconnect(self, c, u, rc):
        self.connected = False
        self.log(f"[esp32-sim] desconectado rc={rc}")

    def _on_message(self, c, u, msg):
        try:
            d = json.loads(msg.payload)
        except Exception:
            return
        self.last_central = time.time()
        acao = d.get("acao", "")
        if acao == "PING":
            return
        cid, eid = d.get("command_id"), d.get("event_id")
        if not cid or not eid:
            return
        self._ack(cid, eid, "RECEBIDO", acao)
        if cid in self.cmds:
            self._ack(cid, eid, "DUPLICADO", acao)
            return
        self.cmds.append(cid)
        if acao in ("SUSPEITA", "CONFIRMAR"):
            if eid in self.fechados:
                self._ack(cid, eid, "IGNORADO_ENCERRADO", acao)
                return
            if acao == "CONFIRMAR":
                self.ocs[eid] = "CONFIRMADO"
            elif self.ocs.get(eid) != "CONFIRMADO":
                self.ocs[eid] = "SUSPEITA"
        elif acao in ("REJEITAR", "ENCERRAR"):
            self.ocs.pop(eid, None)
            if eid not in self.fechados:
                self.fechados.append(eid)
        else:
            self._ack(cid, eid, "ACAO_DESCONHECIDA", acao)
            return
        self.ultimo_cmd = f"{acao} {eid[-4:]}"
        self._ack(cid, eid, "APLICADO", acao)

    # ------------------------------------------------------------ loop
    def start(self):
        self.cli.connect_async(settings.BROKER_HOST, settings.BROKER_PORT, keepalive=10)
        self.cli.loop_start()

    def tick(self):
        e = self.estado()
        if e != self.mostrado:
            self.transicoes.append((time.strftime("%H:%M:%S"), self.mostrado, e))
            self.log(f"[esp32-sim] ESTADO {self.mostrado} -> {e}   (LED: {LED[e]})")
            self.mostrado = e

    def heartbeat(self):
        self._pub({"tipo": "HEARTBEAT", "estado": self.estado(), "estado_ocorrencias": self.estado_ocorrencias(),
                   "ativas": len(self.ocs), "uptime_s": int(time.time() - self.boot), "rssi": -55,
                   "ultimo_cmd": self.ultimo_cmd})

    def stop(self):
        self.cli.loop_stop()
        self.cli.disconnect()


LED = {"NORMAL": "verde fixo", "SUSPEITA": "amarelo 1 Hz", "CONFIRMADO": "vermelho 4 Hz + bipes",
       "FALHA": "roxo 2 flashes"}


def main():
    sim = Esp32Sim()
    sim.start()
    last_hb = 0
    try:
        while True:
            sim.tick()
            if time.time() - last_hb > HEARTBEAT_S:
                sim.heartbeat()
                last_hb = time.time()
            time.sleep(0.1)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()

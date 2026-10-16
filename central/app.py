"""Central operacional Motiva Vision (Flask + MQTT + SQLite).

- Recebe eventos da visão (motiva/<RM>/eventos), deduplica por event_id/versao
  e grava no SQLite (histórico persiste ao atualizar a tela ou reiniciar).
- Envia sinalização preliminar SUSPEITA ao ESP32 e, após decisão humana,
  CONFIRMAR / REJEITAR / ENCERRAR (motiva/<RM>/comandos) com command_id.
- Acompanha ACKs do ESP32 (RECEBIDO -> APLICADO) e heartbeat (motiva/<RM>/status).
- Painel web em http://localhost:5000 ; exportação CSV/JSON.

Uso: python -m central.app
"""
import csv
import io
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from common.mqtt import make_client
from config import settings

app = Flask(__name__)
DB_LOCK = threading.Lock()
RETRY_S, MAX_TENT = 6, 3

STATE = {"broker_conectado": False, "esp32": {"online": False, "ultimo_contato": None, "estado": "DESCONHECIDO",
                                              "detalhe": {}}, "visao": {}}

TRANSICOES = {  # acao do operador -> (estados de origem permitidos, novo estado, comando ao ESP32)
    "CONFIRMAR": ({"SUSPEITA"}, "CONFIRMADO", "CONFIRMAR"),
    "REJEITAR": ({"SUSPEITA"}, "REJEITADO", "REJEITAR"),
    "ENCERRAR": ({"CONFIRMADO"}, "ENCERRADO", "ENCERRAR"),
}


def now_iso():
    return datetime.now(settings.TZ).isoformat(timespec="seconds")


# ------------------------------------------------------------------------ banco --
def db():
    conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


CONN = None


def init_db():
    global CONN
    settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONN = db()
    CONN.executescript("""
    CREATE TABLE IF NOT EXISTS ocorrencias(
        event_id TEXT PRIMARY KEY, versao INTEGER, recebido_em TEXT, atualizado_em TEXT,
        camera_id TEXT, tipo TEXT, confianca REAL, estado TEXT, regra TEXT, recurso TEXT, payload TEXT);
    CREATE TABLE IF NOT EXISTS acoes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, acao TEXT, operador TEXT, ts TEXT, observacao TEXT);
    CREATE TABLE IF NOT EXISTS comandos(
        command_id TEXT PRIMARY KEY, event_id TEXT, acao TEXT, status TEXT, tentativas INTEGER,
        enviado_em TEXT, recebido_em TEXT, aplicado_em TEXT, resposta TEXT);
    CREATE TABLE IF NOT EXISTS mensagens(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, direcao TEXT, topico TEXT, payload TEXT);
    """)
    CONN.commit()


def q(sql, args=(), one=False, commit=False):
    with DB_LOCK:
        cur = CONN.execute(sql, args)
        if commit:
            CONN.commit()
            return cur
        rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def log_msg(direcao, topico, payload):
    q("INSERT INTO mensagens(ts,direcao,topico,payload) VALUES(?,?,?,?)",
      (now_iso(), direcao, topico, payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)),
      commit=True)


def registrar_acao(event_id, acao, operador, obs=""):
    q("INSERT INTO acoes(event_id,acao,operador,ts,observacao) VALUES(?,?,?,?,?)",
      (event_id, acao, operador, now_iso(), obs), commit=True)


# ------------------------------------------------------------------------- MQTT --
MQ = make_client(f"motiva-central-{uuid.uuid4().hex[:6]}")


def on_connect(client, userdata, flags, rc):
    STATE["broker_conectado"] = rc == 0
    client.subscribe([(settings.TOPIC_EVENTOS, 1), (settings.TOPIC_STATUS, 1), (settings.TOPIC_VISAO, 0)])
    print(f"[central] conectado ao broker {settings.BROKER_HOST} rc={rc}")


def on_disconnect(client, userdata, rc):
    STATE["broker_conectado"] = False
    print(f"[central] desconectado do broker rc={rc}")


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        log_msg("RX-INVALIDA", msg.topic, msg.payload.decode("utf-8", "replace"))
        return
    if msg.topic == settings.TOPIC_EVENTOS:
        log_msg("RX", msg.topic, data)
        handle_event(data)
    elif msg.topic == settings.TOPIC_STATUS:
        if data.get("tipo") != "HEARTBEAT":
            log_msg("RX", msg.topic, data)
        handle_status(data, retained=msg.retain)
    elif msg.topic == settings.TOPIC_VISAO:
        STATE["visao"][data.get("camera_id", "?")] = data


def handle_event(ev):
    eid, ver = ev.get("event_id"), int(ev.get("versao", 1))
    if not eid:
        return
    row = q("SELECT versao, estado FROM ocorrencias WHERE event_id=?", (eid,), one=True)
    rec = ev.get("recomendacao", {})
    conf = ev.get("confianca", {}).get("valor", 0)
    if row is None:
        q("INSERT INTO ocorrencias VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (eid, ver, now_iso(), now_iso(), ev.get("camera_id"), ev.get("tipo"), conf, "SUSPEITA",
           rec.get("regra"), rec.get("recurso"), json.dumps(ev, ensure_ascii=False)), commit=True)
        registrar_acao(eid, "EVENTO_RECEBIDO", "sistema", f"tipo={ev.get('tipo')} conf={conf}")
        send_command(eid, "SUSPEITA")   # sinalização preliminar (diferenciada da confirmada)
    elif ver > row["versao"]:
        q("UPDATE ocorrencias SET versao=?, atualizado_em=?, tipo=?, confianca=?, regra=?, recurso=?, payload=? WHERE event_id=?",
          (ver, now_iso(), ev.get("tipo"), conf, rec.get("regra"), rec.get("recurso"),
           json.dumps(ev, ensure_ascii=False), eid), commit=True)
        registrar_acao(eid, "EVENTO_ATUALIZADO", "sistema", f"versao={ver} tipo={ev.get('tipo')} conf={conf}")
    else:
        registrar_acao(eid, "DUPLICATA_IGNORADA", "sistema", f"versao recebida={ver} <= armazenada={row['versao']}")


def handle_status(st, retained=False):
    tipo = st.get("tipo")
    esp = STATE["esp32"]
    if tipo in ("HEARTBEAT", "ONLINE", "ACK"):
        if not retained:
            esp["online"] = True
            esp["ultimo_contato"] = time.time()
        esp["estado"] = st.get("estado", esp["estado"])
        esp["detalhe"] = st
    elif tipo == "LWT":
        esp["online"] = False
        esp["estado"] = "OFFLINE"
    if tipo == "ACK" and st.get("command_id"):
        fase = st.get("fase")
        cmd = q("SELECT * FROM comandos WHERE command_id=?", (st["command_id"],), one=True)
        if not cmd:
            return
        if fase == "RECEBIDO" and cmd["status"] == "ENVIADO":
            q("UPDATE comandos SET status='RECEBIDO', recebido_em=? WHERE command_id=?",
              (now_iso(), st["command_id"]), commit=True)
        elif fase in ("APLICADO", "DUPLICADO", "IGNORADO_ENCERRADO"):
            if cmd["status"] != "APLICADO":
                q("UPDATE comandos SET status=?, aplicado_em=COALESCE(aplicado_em, ?), resposta=? WHERE command_id=?",
                  ("APLICADO" if fase != "IGNORADO_ENCERRADO" else "IGNORADO_PELO_ESP32", now_iso(),
                   json.dumps(st, ensure_ascii=False), st["command_id"]), commit=True)
            if fase == "DUPLICADO":
                registrar_acao(cmd["event_id"], "ESP32_DUPLICATA_DESCARTADA", "esp32", st["command_id"])


def send_command(event_id, acao, command_id=None, tentativa=1):
    cid = command_id or f"CMD-{uuid.uuid4().hex[:8].upper()}"
    payload = {"command_id": cid, "event_id": event_id, "acao": acao, "timestamp": now_iso(), "tentativa": tentativa}
    if command_id is None:
        q("INSERT INTO comandos VALUES(?,?,?,?,?,?,?,?,?)",
          (cid, event_id, acao, "ENVIADO", 1, now_iso(), None, None, None), commit=True)
    MQ.publish(settings.TOPIC_COMANDOS, json.dumps(payload), qos=settings.QOS_COMANDOS, retain=False)
    log_msg("TX", settings.TOPIC_COMANDOS, payload)
    return cid


def background():
    """Heartbeat da central para o ESP32 e retransmissão de comandos sem ACK."""
    last_hb = 0
    while True:
        time.sleep(1)
        t = time.time()
        if t - last_hb >= settings.CENTRAL_HEARTBEAT_S:
            MQ.publish(settings.TOPIC_COMANDOS, json.dumps({"acao": "PING", "timestamp": now_iso()}), qos=0)
            last_hb = t
        esp = STATE["esp32"]
        if esp["ultimo_contato"] and t - esp["ultimo_contato"] > settings.ESP32_TIMEOUT_S:
            esp["online"] = False
        for c in q("SELECT * FROM comandos WHERE status='ENVIADO' OR status='RECEBIDO'"):
            sent = datetime.fromisoformat(c["enviado_em"]).timestamp()
            if t - sent > RETRY_S * c["tentativas"]:
                if c["tentativas"] >= MAX_TENT:
                    q("UPDATE comandos SET status='SEM_CONFIRMACAO' WHERE command_id=?", (c["command_id"],), commit=True)
                    registrar_acao(c["event_id"], "COMANDO_SEM_CONFIRMACAO", "sistema", c["command_id"])
                    continue
                q("UPDATE comandos SET tentativas=tentativas+1 WHERE command_id=?", (c["command_id"],), commit=True)
                send_command(c["event_id"], c["acao"], command_id=c["command_id"], tentativa=c["tentativas"] + 1)


# ------------------------------------------------------------------------- HTTP --
@app.route("/")
def index():
    return render_template("index.html", rm=settings.RM, broker=settings.BROKER_HOST, topic=settings.TOPIC_BASE)


@app.route("/evidencias/<path:name>")
def evidencias(name):
    return send_from_directory(settings.EVIDENCE_DIR, name)


@app.route("/api/state")
def api_state():
    ocs = q("SELECT * FROM ocorrencias ORDER BY recebido_em DESC")
    for o in ocs:
        o["payload"] = json.loads(o["payload"])
        o["comandos"] = q("SELECT * FROM comandos WHERE event_id=? ORDER BY enviado_em", (o["event_id"],))
        o["acoes"] = q("SELECT * FROM acoes WHERE event_id=? ORDER BY id", (o["event_id"],))
    esp = dict(STATE["esp32"])
    esp["segundos_sem_contato"] = round(time.time() - esp["ultimo_contato"], 1) if esp["ultimo_contato"] else None
    esp["ultimo_contato"] = (datetime.fromtimestamp(esp["ultimo_contato"], settings.TZ).isoformat(timespec="seconds")
                             if esp["ultimo_contato"] else None)
    msgs = q("SELECT * FROM mensagens ORDER BY id DESC LIMIT 40")
    return jsonify({"broker_conectado": STATE["broker_conectado"], "esp32": esp, "visao": STATE["visao"],
                    "ocorrencias": ocs, "mensagens": msgs, "agora": now_iso()})


@app.route("/api/ocorrencias/<event_id>/acao", methods=["POST"])
def api_acao(event_id):
    body = request.get_json(force=True)
    acao, operador = body.get("acao"), (body.get("operador") or "").strip() or "operador"
    oc = q("SELECT * FROM ocorrencias WHERE event_id=?", (event_id,), one=True)
    if not oc:
        return jsonify({"erro": "ocorrência inexistente"}), 404
    if acao == "REENVIAR":  # teste de comando duplicado: reenvia o último command_id
        last = q("SELECT * FROM comandos WHERE event_id=? ORDER BY enviado_em DESC LIMIT 1", (event_id,), one=True)
        if not last:
            return jsonify({"erro": "sem comando"}), 400
        send_command(event_id, last["acao"], command_id=last["command_id"], tentativa=99)
        registrar_acao(event_id, "REENVIO_MANUAL_TESTE_DUPLICATA", operador, last["command_id"])
        return jsonify({"ok": True})
    if acao not in TRANSICOES:
        return jsonify({"erro": "ação inválida"}), 400
    origem, novo, cmd = TRANSICOES[acao]
    if oc["estado"] not in origem:
        return jsonify({"erro": f"ação {acao} não permitida no estado {oc['estado']}"}), 409
    q("UPDATE ocorrencias SET estado=?, atualizado_em=? WHERE event_id=?", (novo, now_iso(), event_id), commit=True)
    registrar_acao(event_id, acao, operador, body.get("observacao", ""))
    cid = send_command(event_id, cmd)
    return jsonify({"ok": True, "estado": novo, "command_id": cid})


def _export_rows():
    rows = []
    for o in q("SELECT * FROM ocorrencias ORDER BY recebido_em"):
        p = json.loads(o["payload"])
        acoes = q("SELECT * FROM acoes WHERE event_id=? ORDER BY id", (o["event_id"],))
        cmds = q("SELECT * FROM comandos WHERE event_id=? ORDER BY enviado_em", (o["event_id"],))
        rows.append({"ocorrencia": {k: v for k, v in o.items() if k != "payload"}, "evento": p,
                     "acoes": acoes, "comandos": cmds})
    return rows


@app.route("/api/export.json")
def export_json():
    data = {"exportado_em": now_iso(), "ocorrencias": _export_rows(),
            "mensagens_mqtt": q("SELECT * FROM mensagens ORDER BY id")}
    return Response(json.dumps(data, indent=2, ensure_ascii=False), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=motiva_historico.json"})


@app.route("/api/export.csv")
def export_csv():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["event_id", "camera_id", "tipo", "confianca", "regra", "recurso", "estado_final", "recebido_em",
                "acao", "operador", "acao_em", "observacao"])
    for r in _export_rows():
        o = r["ocorrencia"]
        for a in r["acoes"] or [{}]:
            w.writerow([o["event_id"], o["camera_id"], o["tipo"], o["confianca"], o["regra"], o["recurso"],
                        o["estado"], o["recebido_em"], a.get("acao"), a.get("operador"), a.get("ts"), a.get("observacao")])
    w.writerow([])
    w.writerow(["command_id", "event_id", "acao", "status", "tentativas", "enviado_em", "recebido_em", "aplicado_em"])
    for c in q("SELECT * FROM comandos ORDER BY enviado_em"):
        w.writerow([c["command_id"], c["event_id"], c["acao"], c["status"], c["tentativas"], c["enviado_em"],
                    c["recebido_em"], c["aplicado_em"]])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=motiva_historico.csv"})


def main():
    init_db()
    MQ.on_connect, MQ.on_disconnect, MQ.on_message = on_connect, on_disconnect, on_message
    MQ.connect_async(settings.BROKER_HOST, settings.BROKER_PORT, keepalive=20)
    MQ.loop_start()
    threading.Thread(target=background, daemon=True).start()
    print(f"[central] painel em http://localhost:{settings.CENTRAL_HTTP_PORT}  tópicos {settings.TOPIC_BASE}/#")
    app.run(host="127.0.0.1", port=settings.CENTRAL_HTTP_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()

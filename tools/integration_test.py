"""Teste de integração ponta a ponta (vídeo -> visão -> MQTT -> central -> ESP32 -> ACK).

Sobe um broker local (amqtt), a central (Flask) e o gêmeo do ESP32, executa os
cenários obrigatórios e grava evidências em results/integracao/.

Uso: python -m tools.integration_test
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "integracao"
OUT.mkdir(parents=True, exist_ok=True)
PORT_MQTT, PORT_HTTP = 18830, 5055
ENV = dict(os.environ, MQTT_HOST="127.0.0.1", MQTT_PORT=str(PORT_MQTT), CENTRAL_PORT=str(PORT_HTTP),
           MOTIVA_DB=str(OUT / "central_integracao.db"), PYTHONIOENCODING="utf-8", MOTIVA_RM="rm-teste")
os.environ.update({k: ENV[k] for k in ("MQTT_HOST", "MQTT_PORT", "MOTIVA_RM")})

from config import settings  # noqa: E402  (lê o ambiente acima)
from tools.esp32_sim import Esp32Sim  # noqa: E402

LOG = []
RESULTS = []


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    LOG.append(line)
    print(line, flush=True)


def proc(args, name):
    f = open(OUT / f"{name}.log", "w", encoding="utf-8")
    return subprocess.Popen([sys.executable, "-m"] + args, cwd=ROOT, env=ENV, stdout=f, stderr=subprocess.STDOUT)


def http(path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT_HTTP}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = r.read()
            return json.loads(data) if path.endswith(".json") or path.startswith("/api/state") or body is not None else data
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def wait(cond, timeout=20, step=0.3):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = cond()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


def check(test, desc, ok, evid=""):
    RESULTS.append({"teste": test, "verificacao": desc, "ok": bool(ok), "evidencia": evid})
    log(f"[{'PASSOU' if ok else 'FALHOU'}] {test}: {desc} {('-> ' + evid) if evid else ''}")


def run_vision(video, camera):
    log(f"visão: analisando {video}")
    r = subprocess.run([sys.executable, "-m", "vision.run_vision", "--video", f"dataset/videos/{video}", "--camera", camera,
                        "--out", str(OUT / f"vision_{video}.json")], cwd=ROOT, env=ENV, capture_output=True, text=True,
                       encoding="utf-8")
    (OUT / f"vision_{video}.log").write_text(r.stdout + r.stderr, encoding="utf-8")
    return json.loads((OUT / f"vision_{video}.json").read_text(encoding="utf-8"))[video]


def occ(eid):
    return next((o for o in http("/api/state")["ocorrencias"] if o["event_id"] == eid), None)


def main():
    for p in OUT.glob("central_integracao.db*"):
        p.unlink()
    broker = proc(["tools.local_broker", "--port", str(PORT_MQTT)], "broker")
    time.sleep(2)
    central = proc(["central.app"], "central")
    sim = Esp32Sim(log=log)
    sim.start()
    stop = threading.Event()

    def sim_loop():
        last = 0
        while not stop.is_set():
            sim.tick()
            if time.time() - last > 5:
                sim.heartbeat()
                last = time.time()
            time.sleep(0.1)
    threading.Thread(target=sim_loop, daemon=True).start()

    try:
        ok = wait(lambda: http("/api/state")["esp32"]["online"] and sim.estado() == "NORMAL", 30)
        check("Integração", "central conectada, ESP32 online e em NORMAL", ok, f"ESP32 estado={sim.estado()}")

        # 1) cadeia completa + confirmação + encerramento
        r = run_vision("seq_06.mp4", "CAM-SP000-KM112-R")
        eid = r["eventos"][0]["event_id"]
        o = wait(lambda: occ(eid))
        check("Três categorias / integração", "evento da visão chega à central", o is not None, f"{eid} tipo={o and o['tipo']}")
        st = wait(lambda: sim.estado() == "SUSPEITA" and occ(eid)["comandos"][0]["status"] == "APLICADO")
        check("Integração completa", "comando SUSPEITA aplicado pelo ESP32 (sinal preliminar amarelo)", st,
              f"cmd={occ(eid)['comandos'][0]['command_id']} status={occ(eid)['comandos'][0]['status']}")
        res = http(f"/api/ocorrencias/{eid}/acao", {"acao": "CONFIRMAR", "operador": "op.teste"})
        st = wait(lambda: sim.estado() == "CONFIRMADO" and occ(eid)["comandos"][-1]["status"] == "APLICADO")
        c = occ(eid)["comandos"][-1]
        check("Confirmação e rejeição", "CONFIRMAR registrado e ESP32 em CONFIRMADO; enviado != aplicado registrado", st,
              f"{res.get('command_id')} enviado={c['enviado_em']} recebido={c['recebido_em']} aplicado={c['aplicado_em']}")
        dup_before = len(sim.cmds)
        http(f"/api/ocorrencias/{eid}/acao", {"acao": "REENVIAR", "operador": "op.teste"})
        time.sleep(2)
        acoes = [a["acao"] for a in occ(eid)["acoes"]]
        check("Comando duplicado", "reenvio do mesmo command_id não repete atuação (ACK DUPLICADO)",
              "ESP32_DUPLICATA_DESCARTADA" in acoes and len(sim.cmds) == dup_before and sim.estado() == "CONFIRMADO",
              f"ações={acoes[-2:]}")
        http(f"/api/ocorrencias/{eid}/acao", {"acao": "ENCERRAR", "operador": "op.teste"})
        st = wait(lambda: sim.estado() == "NORMAL" and occ(eid)["estado"] == "ENCERRADO")
        check("Encerramento", "ENCERRAR leva ESP32 de volta a NORMAL", st, f"estado central={occ(eid)['estado']}")
        # tentativa de reabrir evento encerrado com novo command_id
        sim_cli_cmd = {"command_id": "CMD-REABRIR01", "event_id": eid, "acao": "SUSPEITA", "timestamp": "teste"}
        sim.cli.publish(settings.TOPIC_COMANDOS, json.dumps(sim_cli_cmd), qos=1)
        time.sleep(1.5)
        check("Comando duplicado", "comando para evento encerrado não reabre a ocorrência", sim.estado() == "NORMAL",
              f"estado ESP32={sim.estado()}")
        dup_ev = r["eventos"][0]
        sim.cli.publish(settings.TOPIC_EVENTOS, json.dumps(dup_ev), qos=1)
        time.sleep(1.5)
        n = len([o for o in http("/api/state")["ocorrencias"] if o["event_id"] == eid])
        check("Duplicatas", "evento repetido (mesmo event_id/versão) não gera nova ocorrência", n == 1 and
              "DUPLICATA_IGNORADA" in [a["acao"] for a in occ(eid)["acoes"]], f"ocorrências com esse id={n}")

        # 2) rejeição
        r = run_vision("seq_02.mp4", "CAM-SP000-KM112-R")
        eid2 = r["eventos"][0]["event_id"]
        wait(lambda: sim.estado() == "SUSPEITA")
        http(f"/api/ocorrencias/{eid2}/acao", {"acao": "REJEITAR", "operador": "op.teste"})
        st = wait(lambda: sim.estado() == "NORMAL" and occ(eid2)["estado"] == "REJEITADO")
        check("Confirmação e rejeição", "REJEITAR registrado e ESP32 volta a NORMAL", st, f"{eid2} tipo={r['eventos'][0]['tipo']}")

        # 3) trânsito sem acidente
        before = len(http("/api/state")["ocorrencias"])
        r = run_vision("seq_13.mp4", "CAM-SP000-KM112-R")
        time.sleep(1)
        after = len(http("/api/state")["ocorrencias"])
        check("Trânsito sem acidente", "vídeo de frenagem/parada não gera ocorrência", after == before and r["saida"] == "SEM_ACIDENTE",
              f"saida={r['saida']}")

        # 4) ocorrência persistente com atualização (neblina: alerta + contato -> mesmo event_id v2)
        r = run_vision("seq_10.mp4", "CAM-SP000-KM112-R")
        ids = {e["event_id"] for e in r["eventos"]}
        eid3 = r["eventos"][0]["event_id"]
        o = wait(lambda: occ(eid3) and occ(eid3)["versao"] == len(r["eventos"]) and occ(eid3))
        check("Ocorrência persistente / visibilidade", "neblina -> INDETERMINADO; publicações com um único event_id",
              len(ids) == 1 and o and o["tipo"] == "INDETERMINADO",
              f"publicações={len(r['eventos'])} ids={len(ids)} regra={o and o['regra']}")
        http(f"/api/ocorrencias/{eid3}/acao", {"acao": "REJEITAR", "operador": "op.teste", "observacao": "revisado: sem imagem suficiente"})
        wait(lambda: sim.estado() == "NORMAL")

        # 5) broker indisponível
        log("parando o broker MQTT...")
        broker.terminate()
        broker.wait()
        st = wait(lambda: sim.estado() == "FALHA", 25)
        check("Broker indisponível", "ESP32 indica FALHA ao perder o broker", st, f"estado={sim.estado()}")
        st2 = wait(lambda: not http("/api/state")["broker_conectado"], 25)
        check("Broker indisponível", "central exibe broker desconectado", st2)
        time.sleep(3)
        log("reiniciando o broker MQTT...")
        broker = proc(["tools.local_broker", "--port", str(PORT_MQTT)], "broker2")
        t0 = time.time()
        st = wait(lambda: sim.estado() == "NORMAL", 40)
        check("Broker indisponível", "ESP32 reconecta e volta a NORMAL sem reiniciar", st, f"recuperou em {time.time() - t0:.1f} s")
        st = wait(lambda: http("/api/state")["esp32"]["online"], 20)
        check("Broker indisponível", "central volta a ver o ESP32 online", st)

        # 6) central parada -> dados desatualizados
        log("parando a central (dados desatualizados)...")
        central.terminate()
        central.wait()
        t0 = time.time()
        st = wait(lambda: sim.estado() == "FALHA", 30)
        check("Heartbeat / timeout", "sem PING da central por >15 s o ESP32 indica FALHA", st, f"após {time.time() - t0:.1f} s")
        central = proc(["central.app"], "central2")
        st = wait(lambda: sim.estado() == "NORMAL", 30)
        check("Heartbeat / timeout", "central de volta -> ESP32 retorna a NORMAL", st)
        hist = wait(lambda: len(http("/api/state")["ocorrencias"]), 10)
        check("Histórico", "histórico preservado após reiniciar a central (SQLite)", hist and hist >= 3, f"ocorrências={hist}")

        (OUT / "export_central.json").write_bytes(urllib.request.urlopen(f"http://127.0.0.1:{PORT_HTTP}/api/export.json").read())
        (OUT / "export_central.csv").write_bytes(urllib.request.urlopen(f"http://127.0.0.1:{PORT_HTTP}/api/export.csv").read())
    finally:
        stop.set()
        sim.stop()
        for p in (central, broker):
            p.terminate()
        res = {"total": len(RESULTS), "passou": sum(r["ok"] for r in RESULTS), "verificacoes": RESULTS,
               "transicoes_esp32": sim.transicoes}
        (OUT / "resultado_integracao.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        (OUT / "log_integracao.txt").write_text("\n".join(LOG), encoding="utf-8")
        log(f"RESUMO: {res['passou']}/{res['total']} verificações passaram")


if __name__ == "__main__":
    main()

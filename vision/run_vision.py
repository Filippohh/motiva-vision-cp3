"""Executa a análise de vídeo e publica os eventos no MQTT.

Exemplos:
  python -m vision.run_vision --video dataset/videos/seq_06.mp4 --camera CAM-SP000-KM112-R
  python -m vision.run_vision --all                 # todos os vídeos do manifesto
  python -m vision.run_vision --all --no-mqtt       # só análise (offline)
  python -m vision.run_vision --video X --realtime  # respeita o FPS (demonstração)
"""
import argparse
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

import cv2

from config import settings
from vision.pipeline import VideoAnalyzer

ROOT = settings.ROOT


class Publisher:
    def __init__(self, enabled):
        self.client = None
        if not enabled:
            return
        from common.mqtt import make_client
        self.client = make_client(f"motiva-visao-{uuid.uuid4().hex[:6]}")
        self.client.connect(settings.BROKER_HOST, settings.BROKER_PORT, keepalive=30)
        self.client.loop_start()
        time.sleep(1.0)

    def event(self, ev):
        if self.client:
            info = self.client.publish(settings.TOPIC_EVENTOS, json.dumps(ev, ensure_ascii=False),
                                       qos=settings.QOS_EVENTOS, retain=False)
            info.wait_for_publish(timeout=5)

    def status(self, payload):
        if self.client:
            self.client.publish(settings.TOPIC_VISAO, json.dumps(payload, ensure_ascii=False), qos=0, retain=False)

    def close(self):
        if self.client:
            time.sleep(0.5)
            self.client.loop_stop()
            self.client.disconnect()


def analyze_video(path, camera_id, pub, realtime=False, verbose=True, show=False):
    cameras = json.loads(settings.CAMERAS_FILE.read_text(encoding="utf-8"))
    cam = cameras[camera_id]
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    log = print if verbose else (lambda *a, **k: None)
    an = VideoAnalyzer(camera_id, cam, fps, Path(path).name, pub.event, log=log)
    t0 = time.time()
    n = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        an.process(frame)
        n += 1
        if n % int(fps * 2) == 0:   # monitoramento periódico (saída SEM_ACIDENTE quando não há ocorrência)
            pub.status({"origem": "visao", "camera_id": camera_id, "fonte": Path(path).name,
                        "timestamp": datetime.now(settings.TZ).isoformat(timespec="seconds"),
                        "tempo_video_s": round(n / fps, 1), "visibilidade": round(an.q_now, 2),
                        "ocorrencias_ativas": len([i for i in an.inc.active]),
                        "saida": "SEM_ACIDENTE" if not an.inc.active else "OCORRENCIA_ATIVA"})
        if show:
            cv2.imshow("Motiva Vision", frame)
            if cv2.waitKey(1) == 27:
                break
        if realtime:
            delay = n / fps - (time.time() - t0)
            if delay > 0:
                time.sleep(delay)
    cap.release()
    elapsed = time.time() - t0
    saida = "SEM_ACIDENTE" if not an.events else an.events[-1]["tipo"]
    return {"video": Path(path).name, "camera_id": camera_id, "quadros": n, "fps": fps,
            "tempo_proc_s": round(elapsed, 2), "eventos": an.events, "candidatos": an.candidates, "saida": saida}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video")
    ap.add_argument("--camera", default="CAM-SP000-KM112-R")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--split", choices=["ajuste", "avaliacao"], help="filtra o conjunto do manifesto")
    ap.add_argument("--no-mqtt", action="store_true")
    ap.add_argument("--realtime", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "results" / "deteccoes.json"))
    args = ap.parse_args()

    pub = Publisher(not args.no_mqtt)
    jobs = []
    if args.all or args.split:
        manifest = json.loads((ROOT / "dataset" / "manifest.json").read_text(encoding="utf-8"))
        for name, m in manifest.items():
            if args.split and m["conjunto"] != args.split:
                continue
            jobs.append((ROOT / "dataset" / m["arquivo"], m["camera_id"]))
    else:
        jobs.append((Path(args.video), args.camera))

    results = []
    for path, cam in jobs:
        print(f"== {path.name} ({cam})")
        r = analyze_video(path, cam, pub, realtime=args.realtime, show=args.show)
        print(f"   saída: {r['saida']}  eventos={len(r['eventos'])}  ({r['quadros']} quadros em {r['tempo_proc_s']} s)")
        results.append(r)
    pub.close()
    out = Path(args.out)
    prev = json.loads(out.read_text(encoding="utf-8")) if out.exists() and args.split else {}
    prev.update({r["video"]: r for r in results})
    out.write_text(json.dumps(prev, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"resultados em {out}")


if __name__ == "__main__":
    main()

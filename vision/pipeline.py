"""Pipeline de visão: detecção -> rastreamento -> análise temporal -> evento.

1. Detecção: subtração de fundo (MOG2) + morfologia -> blobs de veículos.
2. Rastreamento: associação quadro a quadro por posição prevista; quando dois
   veículos se fundem num único blob (contato), cada trilha passa a seguir o
   deslocamento do blob compartilhado.
3. Análise temporal do par em contato (não basta sobreposição de caixas):
     s_aprox    velocidade de fechamento imediatamente antes do contato
     s_impacto  queda brusca de velocidade logo APÓS o contato
     s_persist  veículos permanecem parados após o contato
     s_clareza  quão perto o ângulo entre as trajetórias está do protótipo
                da categoria (0° traseira, 90° lateral, 180° frontal)
4. Confiança = (0,25 s_aprox + 0,30 s_impacto + 0,20 s_persist + 0,25 s_clareza)
              x (0,4 + 0,6 q_visib)
   base < 0,35 -> descartado (SEM_ACIDENTE); confiança < 0,60 ou visibilidade
   ruim -> INDETERMINADO (revisão humana); senão categoria.
"""
import math
import uuid
from collections import deque
from datetime import datetime

import cv2
import numpy as np

from common.recommendation import LIMIAR_ALTA_ENERGIA_KMH, LIMIAR_CONFIANCA, recomendar
from config import settings

LIMIAR_DESCARTE = 0.35
Q_MIN = 0.45               # visibilidade mínima para concluir um tipo
POST = 20                  # quadros analisados após o contato antes de decidir
MIN_AREA = 400
PESOS = {"aprox": 0.25, "impacto": 0.30, "persist": 0.20, "clareza": 0.25}

CORES = {"COLISAO_TRASEIRA": (0, 140, 255), "COLISAO_FRONTAL": (0, 0, 255),
         "COLISAO_LATERAL": (255, 0, 200), "INDETERMINADO": (0, 220, 255)}


def clip01(v):
    return float(max(0.0, min(1.0, v)))


# ---------------------------------------------------------------- visibilidade --
def visibility(frame, contraste_ref=30.0):
    """Índice 0..1 de visibilidade.

    Contraste local = percentil 90 do desvio-padrão em blocos 16x16 da imagem
    reduzida e suavizada (bordas de faixa, acostamento e veículos). É comparado
    com a referência da câmera em condição limpa (`contraste_ref` em
    cameras.json, calibrado no conjunto de ajuste). Brilho médio muito baixo
    (noite) também derruba o índice.
    """
    g = cv2.cvtColor(cv2.resize(frame, (320, 180)), cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (3, 3), 0).astype(np.float32)
    blocks = g[:176, :320].reshape(11, 16, 20, 16).std(axis=(1, 3))
    p90, mean = float(np.percentile(blocks, 90)), float(g.mean())
    ratio = p90 / contraste_ref
    q_con = clip01((ratio - 0.35) / (0.80 - 0.35))
    q_bri = clip01((mean - 30) / (60 - 30))
    return min(q_con, q_bri), {"contraste_local_p90": p90, "razao_ref": ratio, "brilho": mean}


# -------------------------------------------------------------------- detecção --
class Detector:
    def __init__(self):
        self.bg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=28, detectShadows=False)
        self.k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        self.n = 0

    def __call__(self, frame):
        self.n += 1
        blur = cv2.GaussianBlur(frame, (5, 5), 0)
        lr = -1 if self.n < 12 else 0.0008     # aprende o fundo no início, depois quase congela
        fg = self.bg.apply(blur, learningRate=lr)
        _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self.k_open)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, self.k_close)
        cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        blobs = []
        for c in cnts:
            area = cv2.contourArea(c)
            if area < MIN_AREA:
                continue
            x, y, w, h = cv2.boundingRect(c)
            m = cv2.moments(c)
            cx, cy = m["m10"] / m["m00"], m["m01"] / m["m00"]
            (_, _), (rw, rh), ang = cv2.minAreaRect(c)
            axis = ang if rw >= rh else ang + 90   # orientação do eixo maior (graus)
            blobs.append({"bbox": (x, y, w, h), "c": (cx, cy), "area": area, "axis": axis % 180})
        return blobs, fg


# ---------------------------------------------------------------- rastreamento --
class Track:
    def __init__(self, tid, f, blob):
        self.id = tid
        self.hist = deque(maxlen=120)   # (f, cx, cy, bbox, shared, axis)
        self.missed = 0
        self.last_blob_c = None
        self.prev_shared = False
        self.add(f, blob["c"], blob["bbox"], False, blob["axis"])

    def add(self, f, c, bbox, shared, axis):
        self.hist.append((f, c[0], c[1], bbox, shared, axis))

    @property
    def c(self):
        return self.hist[-1][1], self.hist[-1][2]

    def vel(self, k=4, upto=None):
        h = [p for p in self.hist if upto is None or p[0] <= upto]
        if len(h) < 2:
            return 0.0, 0.0
        a, b = h[max(0, len(h) - 1 - k)], h[-1]
        df = max(1, b[0] - a[0])
        return (b[1] - a[1]) / df, (b[2] - a[2]) / df

    def at(self, f):
        for p in reversed(self.hist):
            if p[0] <= f:
                return p
        return self.hist[0]

    def speed_between(self, f0, f1):
        pts = [p for p in self.hist if f0 <= p[0] <= f1]
        if len(pts) < 2:
            return None
        sp = [math.hypot(b[1] - a[1], b[2] - a[2]) / max(1, b[0] - a[0]) for a, b in zip(pts, pts[1:])]
        return float(np.mean(sp))

    def vel_between(self, f0, f1):
        pts = [p for p in self.hist if f0 <= p[0] <= f1]
        if len(pts) < 2:
            return None
        a, b = pts[0], pts[-1]
        df = max(1, b[0] - a[0])
        return (b[1] - a[1]) / df, (b[2] - a[2]) / df

    def first_frame(self):
        return self.hist[0][0]


def _inside(pt, bbox, pad=10):
    x, y, w, h = bbox
    return x - pad <= pt[0] <= x + w + pad and y - pad <= pt[1] <= y + h + pad


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / float(aw * ah + bw * bh - inter + 1e-6)


class Tracker:
    def __init__(self):
        self.tracks = {}
        self.next_id = 1

    def update(self, f, blobs):
        claims = {}  # blob idx -> [track ids]
        unclaimed = []
        for t in self.tracks.values():
            vx, vy = t.vel()
            pred = (t.c[0] + vx * (1 + t.missed), t.c[1] + vy * (1 + t.missed))
            inside = [i for i, b in enumerate(blobs) if _inside(pred, b["bbox"])]
            if inside:
                i = min(inside, key=lambda i: math.dist(pred, blobs[i]["c"]))
                claims.setdefault(i, []).append(t.id)
                t._pred = pred
            else:
                unclaimed.append((t, pred))
        for t, pred in unclaimed:
            best, bd = None, 60.0
            for i, b in enumerate(blobs):
                if i in claims:
                    continue
                d = math.dist(pred, b["c"])
                if d < bd:
                    best, bd = i, d
            if best is None:
                t.missed += 1
                continue
            claims.setdefault(best, []).append(t.id)
            t._pred = pred

        for i, tids in claims.items():
            b = blobs[i]
            shared = len(tids) > 1
            for tid in tids:
                t = self.tracks[tid]
                if shared:
                    if t.prev_shared and t.last_blob_c is not None:
                        c = (t.c[0] + b["c"][0] - t.last_blob_c[0], t.c[1] + b["c"][1] - t.last_blob_c[1])
                    else:
                        x, y, w, h = b["bbox"]
                        c = (min(max(t._pred[0], x), x + w), min(max(t._pred[1], y), y + h))
                    t.add(f, c, b["bbox"], True, b["axis"])
                else:
                    t.add(f, b["c"], b["bbox"], False, b["axis"])
                t.last_blob_c = b["c"]
                t.prev_shared = shared
                t.missed = 0
                t.shared_with = set(tids) - {tid}
        for i, b in enumerate(blobs):
            if i not in claims:
                self.tracks[self.next_id] = Track(self.next_id, f, b)
                self.tracks[self.next_id].shared_with = set()
                self.next_id += 1
        for tid in [k for k, t in self.tracks.items() if t.missed > 12]:
            del self.tracks[tid]


# ------------------------------------------------------------ análise temporal --
def classify_angle(va, vb, axis_a, axis_b):
    """Retorna (tipo, clareza, angulo_graus, metodo)."""
    sa, sb = math.hypot(*va), math.hypot(*vb)
    MOV = 0.8
    if sa >= MOV and sb >= MOV:
        cosv = (va[0] * vb[0] + va[1] * vb[1]) / (sa * sb)
        phi = math.degrees(math.acos(max(-1.0, min(1.0, cosv))))
        centers = {"COLISAO_TRASEIRA": 0, "COLISAO_LATERAL": 90, "COLISAO_FRONTAL": 180}
        tipo = min(centers, key=lambda k: abs(phi - centers[k]))
        clar = clip01(1 - abs(phi - centers[tipo]) / 45)
        return tipo, clar, phi, "trajetorias"
    if max(sa, sb) < MOV:
        return None, 0.0, None, "ambos_parados"
    # um veículo parado: compara trajetória do móvel com o eixo do parado
    mv, axis = (va, axis_b) if sa >= MOV else (vb, axis_a)
    ang_mv = math.degrees(math.atan2(mv[1], mv[0])) % 180
    d = abs(ang_mv - axis) % 180
    d = min(d, 180 - d)  # 0..90
    if d < 45:
        return "COLISAO_TRASEIRA", 0.8 * clip01(1 - d / 45), d, "eixo_veiculo_parado"
    return "COLISAO_LATERAL", 0.8 * clip01(1 - (90 - d) / 45), d, "eixo_veiculo_parado"


def lanes_obstructed(bbox, camera):
    x, y, w, h = bbox
    res = []
    for fx in camera.get("faixas", []):
        lo, hi = (y, y + h) if fx["eixo"] == "y" else (x, x + w)
        if min(hi, fx["max"]) - max(lo, fx["min"]) > 15:
            res.append(fx["id"])
    return res


class IncidentManager:
    """Agrupa candidatos em ocorrências com event_id único (sem repetição por quadro)."""

    def __init__(self):
        self.active = []   # {"event_id", "c", "tracks", "f_last", "event"}

    def find(self, c, tids, f):
        for inc in self.active:
            if inc["tracks"] & tids or math.dist(inc["c"], c) < 140 or inc.get("global"):
                return inc
        return None

    def expire(self, f, alive_ids):
        self.active = [i for i in self.active
                       if i.get("global") or (i["tracks"] & alive_ids) or f - i["f_last"] < 90]


class VideoAnalyzer:
    def __init__(self, camera_id, camera, fps, source_name, publish, evidence_dir=None, log=print):
        self.camera_id, self.camera, self.fps = camera_id, camera, fps
        self.source = source_name
        self.publish = publish
        self.log = log
        self.evdir = evidence_dir or settings.EVIDENCE_DIR
        self.evdir.mkdir(parents=True, exist_ok=True)
        self.det, self.trk, self.inc = Detector(), Tracker(), IncidentManager()
        self.buf = deque(maxlen=60)
        self.q_hist = deque(maxlen=60)
        self.contacts = {}          # (a,b) -> frame do 1o contato
        self.evaluated = set()
        self.events = []            # eventos emitidos (para avaliação)
        self.candidates = []        # todos os candidatos analisados (inclusive descartados)
        self.low_vis_frames = 0
        self.f = -1
        self.q_now, self.q_raw = 1.0, {}

    # ------------------------------------------------------------------
    def process(self, frame):
        self.f += 1
        f = self.f
        if f % 3 == 0:
            self.q_now, self.q_raw = visibility(frame, self.camera.get("contraste_ref", 30.0))
        self.q_hist.append(self.q_now)
        blobs, _ = self.det(frame)
        if f < 12:
            self.buf.append((f, frame))
            return
        self.trk.update(f, blobs)
        self.buf.append((f, frame))
        self._contacts(f)
        self._evaluate(f)
        self._visibility_alert(f)
        self.inc.expire(f, set(self.trk.tracks))

    def _contacts(self, f):
        tr = [t for t in self.trk.tracks.values() if t.missed == 0 and len(t.hist) >= 5]
        for i in range(len(tr)):
            for j in range(i + 1, len(tr)):
                a, b = tr[i], tr[j]
                key = (min(a.id, b.id), max(a.id, b.id))
                if key in self.contacts:
                    continue
                touching = (b.id in getattr(a, "shared_with", set())) or _iou(a.hist[-1][3], b.hist[-1][3]) > 0.02
                if touching:
                    self.contacts[key] = f

    def _evaluate(self, f):
        for key, cf in list(self.contacts.items()):
            if key in self.evaluated or f < cf + POST:
                continue
            self.evaluated.add(key)
            a, b = self.trk.tracks.get(key[0]), self.trk.tracks.get(key[1])
            if a is None or b is None:
                continue
            if cf - a.first_frame() < 8 or cf - b.first_frame() < 8:
                continue   # sem histórico suficiente para análise temporal
            self._analyze_pair(a, b, cf, f)

    def _analyze_pair(self, a, b, cf, f):
        fps, mpp = self.fps, self.camera.get("metros_por_pixel", 0.05)
        va, vb = a.vel_between(cf - 8, cf - 2) or (0, 0), b.vel_between(cf - 8, cf - 2) or (0, 0)
        pre_a, pre_b = a.speed_between(cf - 6, cf - 1) or 0, b.speed_between(cf - 6, cf - 1) or 0
        post_a, post_b = a.speed_between(cf + 10, cf + POST) or 0, b.speed_between(cf + 10, cf + POST) or 0
        pa0, pb0, pa1, pb1 = a.at(cf - 6), b.at(cf - 6), a.at(cf - 1), b.at(cf - 1)
        d0 = math.hypot(pa0[1] - pb0[1], pa0[2] - pb0[2])
        d1 = math.hypot(pa1[1] - pb1[1], pa1[2] - pb1[2])
        closing = max(0.0, (d0 - d1) / max(1, pa1[0] - pa0[0]))
        rel_v = math.hypot(va[0] - vb[0], va[1] - vb[1])

        stopped = 0
        for ff in range(cf + 10, cf + POST + 1):
            sa = a.speed_between(ff - 2, ff)
            sb = b.speed_between(ff - 2, ff)
            if sa is not None and sb is not None and sa < 1.0 and sb < 1.0:
                stopped += 1
        s = {
            "aprox": clip01(closing / 4.0),
            "impacto": clip01((max(pre_a, pre_b) - max(post_a, post_b)) / 4.0),
            "persist": clip01(stopped / (POST - 10 + 1)),
        }
        tipo, clar, phi, metodo = classify_angle(va, vb, a.at(cf - 2)[5], b.at(cf - 2)[5])
        s["clareza"] = clar
        base = sum(PESOS[k] * s[k] for k in PESOS)
        q = float(np.mean([qq for qq in list(self.q_hist)[-40:]]))
        conf = base * (0.4 + 0.6 * q)
        cand = {"frame_contato": cf, "trilhas": [a.id, b.id], "componentes": {k: round(v, 3) for k, v in s.items()},
                "base": round(base, 3), "visibilidade": round(q, 3), "confianca": round(conf, 3),
                "tipo_angulo": tipo, "angulo_graus": None if phi is None else round(phi, 1), "metodo": metodo}
        self.candidates.append(cand)
        self.log(f"  candidato f={cf} trilhas={a.id},{b.id} {s} base={base:.2f} q={q:.2f} conf={conf:.2f} -> {tipo} ang={phi}")
        if base < LIMIAR_DESCARTE:
            return
        if tipo is None or q < Q_MIN or conf < LIMIAR_CONFIANCA:
            motivo = "visibilidade_insuficiente" if q < Q_MIN else "evidencia_insuficiente"
            tipo_final = "INDETERMINADO"
        else:
            motivo = None
            tipo_final = tipo

        bx = self._union_bbox(a.hist[-1][3], b.hist[-1][3])
        kmh = rel_v * fps * mpp * 3.6
        indic = {
            "veiculos_envolvidos": 2,
            "faixa_obstruida": lanes_obstructed(bx, self.camera) or "desconhecido",
            "velocidade_relativa_kmh": round(kmh, 1),
            "impacto_alta_energia": kmh > LIMIAR_ALTA_ENERGIA_KMH if q >= Q_MIN else "desconhecido",
            "veiculos_parados_apos_contato": s["persist"] > 0.5,
            "angulo_trajetorias_graus": None if phi is None else round(phi, 1),
            "pedestres": "desconhecido",
            "visibilidade": round(q, 2),
        }
        if motivo:
            indic["motivo_indeterminado"] = motivo
        c = ((bx[0] + bx[2] / 2), (bx[1] + bx[3] / 2))
        inc = self.inc.find(c, {a.id, b.id}, f)
        if inc is not None:
            inc["tracks"] |= {a.id, b.id}
            inc["f_last"] = f
            prev = inc["event"]
            stronger = (prev["tipo"] == "INDETERMINADO" and tipo_final != "INDETERMINADO") or                        (tipo_final != "INDETERMINADO" and conf > prev["confianca"]["valor"] + 0.10)
            if inc.get("global") or stronger:
                # mesma ocorrência, evidência mais forte: republica com o MESMO event_id (versao+1)
                inc["global"] = False
                inc["c"] = c
                self._emit(tipo_final, conf, s, indic, cf, (a, b), bx, update_of=inc)
            else:
                self.log(f"  -> candidato agregado à ocorrência {inc['event_id']} (sem novo evento)")
            return
        self._emit(tipo_final, conf, s, indic, cf, (a, b), bx, center=c)

    def _visibility_alert(self, f):
        q = self.q_now
        self.low_vis_frames = self.low_vis_frames + 1 if q < Q_MIN else 0
        if self.low_vis_frames == 60 and not any(i.get("global") for i in self.inc.active):
            indic = {"veiculos_envolvidos": "desconhecido", "faixa_obstruida": "desconhecido",
                     "pedestres": "desconhecido", "visibilidade": round(q, 2),
                     "motivo_indeterminado": "visibilidade_insuficiente", "medidas_imagem": {k: round(v, 2) for k, v in self.q_raw.items()}}
            s = {"aprox": 0, "impacto": 0, "persist": 0, "clareza": 0}
            self._emit("INDETERMINADO", round(0.3 * q, 3), s, indic, f, (), None, center=(0, 0), global_=True)

    @staticmethod
    def _union_bbox(b1, b2):
        x0, y0 = min(b1[0], b2[0]), min(b1[1], b2[1])
        x1, y1 = max(b1[0] + b1[2], b2[0] + b2[2]), max(b1[1] + b1[3], b2[1] + b2[3])
        return (x0, y0, x1 - x0, y1 - y0)

    # ------------------------------------------------------------------ evento --
    def _emit(self, tipo, conf, comp, indic, cf, tracks, bbox, center=None, global_=False, update_of=None):
        now = datetime.now(settings.TZ)
        if update_of:
            event_id, versao = update_of["event_id"], update_of["versao"] + 1
        else:
            event_id = f"EVT-{now:%Y%m%d-%H%M%S}-{self.camera_id.split('-')[-2]}-{uuid.uuid4().hex[:4].upper()}"
            versao = 1
        nivel = "ALTA" if conf >= 0.8 else "MEDIA" if conf >= LIMIAR_CONFIANCA else "BAIXA"
        img_path, clip_path = self._save_evidence(event_id, cf, tracks, bbox, tipo, conf)
        loc = dict(self.camera.get("local", {}))
        ev = {
            "schema": "motiva.evento.v1",
            "event_id": event_id,
            "versao": versao,
            "timestamp": now.isoformat(timespec="seconds"),
            "camera_id": self.camera_id,
            "local": loc,
            "tipo": tipo,
            "confianca": {
                "valor": round(conf, 3), "nivel": nivel, "limiar": LIMIAR_CONFIANCA,
                "componentes": {k: round(v, 3) for k, v in comp.items()},
                "definicao": "0..1 = soma ponderada (aprox 0,25; impacto 0,30; persistência 0,20; clareza do ângulo 0,25) "
                             "x (0,4 + 0,6 x visibilidade). ALTA >= 0,80; MEDIA >= 0,60; BAIXA < 0,60 -> INDETERMINADO.",
            },
            "indicadores": indic,
            "recomendacao": recomendar(tipo, conf, indic),
            "estado": "SUSPEITA",
            "evidencia": {
                "fonte": self.source,
                "frame": cf,
                "tempo_video_s": round(cf / self.fps, 2),
                "frame_decisao": self.f,
                "imagem": img_path,
                "clip": clip_path,
            },
        }
        if update_of:
            update_of["event"] = ev
            update_of["versao"] = versao
        else:
            self.inc.active.append({"event_id": event_id, "versao": 1, "c": center,
                                    "tracks": {t.id for t in tracks}, "f_last": self.f, "event": ev, "global": global_})
        self.events.append(ev)
        self.log(f"  >>> EVENTO {event_id} v{versao} tipo={tipo} conf={conf:.2f} t={cf / self.fps:.2f}s regra={ev['recomendacao']['regra']}")
        self.publish(ev)

    def _save_evidence(self, event_id, cf, tracks, bbox, tipo, conf):
        frames = {f: fr for f, fr in self.buf}
        key = cf if cf in frames else max(frames)
        img = frames[key].copy()
        color = CORES.get(tipo, (255, 255, 255))
        for t in tracks:
            pts = [(int(p[1]), int(p[2])) for p in t.hist if cf - 30 <= p[0] <= cf]
            for p0, p1 in zip(pts, pts[1:]):
                cv2.line(img, p0, p1, (0, 255, 255), 2)
            if pts:
                cv2.circle(img, pts[-1], 4, (0, 255, 255), -1)
        if bbox:
            x, y, w, h = bbox
            cv2.rectangle(img, (x - 6, y - 6), (x + w + 6, y + h + 6), color, 2)
        label = f"{tipo} conf={conf:.2f} t={cf / self.fps:.2f}s f={cf}"
        cv2.rectangle(img, (0, 0), (img.shape[1], 30), (0, 0, 0), -1)
        cv2.putText(img, f"{event_id}  {label}", (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
        img_name = f"{event_id}.jpg"
        cv2.imwrite(str(self.evdir / img_name), img)
        clip_name = f"{event_id}.mp4"
        h, w = img.shape[:2]
        vw = cv2.VideoWriter(str(self.evdir / clip_name), cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
        for f, fr in self.buf:
            if f >= cf - 25:
                vw.write(fr)
        vw.release()
        return f"evidencias/{img_name}", f"evidencias/{clip_name}"

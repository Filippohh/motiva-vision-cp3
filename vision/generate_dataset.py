"""Gera as sequências de teste (simulações visuais coerentes, vista superior).

Cada vídeo é produzido por uma pequena simulação física 2D: os veículos têm
posição, rumo e velocidade; a colisão NÃO é escrita à mão como rótulo, ela
acontece quando os retângulos dos veículos se tocam e é resolvida como choque
inelástico seguido de atrito. O detector recebe apenas os pixels do .mp4.

Saídas:
  dataset/videos/seq_XX.mp4     vídeos com nomes neutros (não revelam o rótulo)
  dataset/manifest.json         vídeo -> camera_id e conjunto (ajuste/avaliacao)
  dataset/ground_truth.json     rótulos esperados (lidos SOMENTE por evaluate.py)

Uso:  python -m vision.generate_dataset
"""
import json
import math
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "dataset"
W, H, FPS = 960, 540, 30

CAM_HWY = "CAM-SP000-KM112-R"
CAM_X = "CAM-SP000-KM118-X"

# centro das faixas (pixels)
HWY_LANES = {"F1": (157, 180), "F2": (232, 180), "F3": (307, 0), "F4": (382, 0)}
X_LANES = {"H-OESTE": (230, 180), "H-LESTE": (310, 0), "V-SUL": (440, 90), "V-NORTE": (520, 270)}

PALETTE = [(40, 40, 200), (200, 60, 40), (30, 160, 230), (230, 230, 230), (60, 60, 60),
           (40, 170, 60), (150, 50, 160), (20, 120, 200), (180, 180, 40), (90, 90, 160)]


# ----------------------------------------------------------------- cenário --
def render_background(kind, rng):
    img = np.zeros((H, W, 3), np.uint8)
    grass = rng.normal(0, 6, (H, W, 1)) + np.array([70, 115, 70])
    img[:] = np.clip(grass, 0, 255).astype(np.uint8)
    asphalt = (88, 88, 92)
    if kind == "hwy":
        cv2.rectangle(img, (0, 120), (W, 420), asphalt, -1)
        cv2.line(img, (0, 122), (W, 122), (230, 230, 230), 3)
        cv2.line(img, (0, 418), (W, 418), (230, 230, 230), 3)
        cv2.line(img, (0, 268), (W, 268), (40, 200, 230), 2)
        cv2.line(img, (0, 273), (W, 273), (40, 200, 230), 2)
        for y in (195, 345):
            for x in range(0, W, 60):
                cv2.line(img, (x, y), (x + 30, y), (220, 220, 220), 2)
    else:
        cv2.rectangle(img, (0, 190), (W, 350), asphalt, -1)
        cv2.rectangle(img, (400, 0), (560, H), asphalt, -1)
        for x in list(range(0, 390, 50)) + list(range(570, W, 50)):
            cv2.line(img, (x, 270), (x + 25, 270), (40, 200, 230), 2)
        for y in list(range(0, 180, 50)) + list(range(360, H, 50)):
            cv2.line(img, (480, y), (480, y + 25), (40, 200, 230), 2)
        for x0 in (395, 565):  # faixas de pedestre / linhas de retenção
            cv2.line(img, (x0, 190), (x0, 350), (230, 230, 230), 3)
    noise = rng.normal(0, 3, img.shape)
    return np.clip(img + noise, 0, 255).astype(np.uint8)


class Car:
    def __init__(self, x, y, heading, speed, spawn, color, length=86, width=40, schedule=()):
        self.x, self.y = float(x), float(y)
        self.h = float(heading)
        self.speed = float(speed)
        self.vx = self.vy = 0.0
        self.spawn = spawn
        self.color = color
        self.L, self.Wd = length, width
        self.schedule = list(schedule)  # (frame_ini, frame_fim, aceleracao px/quadro^2)
        self.crashed = False
        self.ang_vel = 0.0
        self.friction = 0.0
        self._set_v()

    def _set_v(self):
        r = math.radians(self.h)
        self.vx, self.vy = self.speed * math.cos(r), self.speed * math.sin(r)

    def active(self, f):
        return f >= self.spawn

    def rect(self):
        return ((self.x, self.y), (self.L, self.Wd), self.h)

    def step(self, f):
        if not self.crashed:
            acc = sum(a for (a0, a1, a) in self.schedule if a0 <= f < a1)
            self.speed = max(0.0, self.speed + acc)
            self._set_v()
        else:
            sp = math.hypot(self.vx, self.vy)
            if sp > 0:
                k = max(0.0, sp - self.friction) / sp
                self.vx *= k
                self.vy *= k
            self.h += self.ang_vel
            self.ang_vel *= 0.86
        self.x += self.vx
        self.y += self.vy

    def draw(self, img):
        box = cv2.boxPoints(((self.x + 4, self.y + 5), (self.L, self.Wd), self.h)).astype(np.int32)
        cv2.fillPoly(img, [box], (45, 45, 48))  # sombra
        box = cv2.boxPoints(self.rect()).astype(np.int32)
        cv2.fillPoly(img, [box], self.color)
        r = math.radians(self.h)
        fx, fy = math.cos(r), math.sin(r)
        for off, ln, shade in ((self.L * 0.18, self.L * 0.18, 0.35), (-self.L * 0.3, self.L * 0.12, 0.5)):
            cx, cy = self.x + fx * off, self.y + fy * off
            wbox = cv2.boxPoints(((cx, cy), (ln, self.Wd * 0.8), self.h)).astype(np.int32)
            cv2.fillPoly(img, [wbox], tuple(int(c * shade) for c in self.color))
        cv2.polylines(img, [box], True, (25, 25, 25), 1)


def resolve_contacts(cars, f, log):
    act = [c for c in cars if c.active(f)]
    for i in range(len(act)):
        for j in range(i + 1, len(act)):
            a, b = act[i], act[j]
            if a.crashed and b.crashed:
                continue
            kind, _ = cv2.rotatedRectangleIntersection(a.rect(), b.rect())
            if kind == cv2.INTERSECT_NONE:
                continue
            # choque perfeitamente inelástico entre massas iguais + atrito forte
            vx, vy = (a.vx + b.vx) / 2, (a.vy + b.vy) / 2
            rel = math.hypot(a.vx - b.vx, a.vy - b.vy)
            cosang = abs(math.cos(math.radians(a.h - b.h)))
            for c in (a, b):
                c.vx, c.vy = vx, vy
                c.crashed = True
                c.friction = 0.35 + 0.02 * rel
            if cosang < 0.6:  # impacto transversal gera giro
                b.ang_vel = 3.0 if (a.vx * b.vy - a.vy * b.vx) > 0 else -3.0
                a.ang_vel = -b.ang_vel * 0.4
            log.append({"frame": f, "x": round((a.x + b.x) / 2), "y": round((a.y + b.y) / 2),
                        "vel_relativa_px_quadro": round(rel, 2)})


def apply_effects(frame, effect, rng, f):
    img = frame.astype(np.float32)
    if effect == "fog":
        img = img * 0.28 + np.array([205, 205, 205]) * 0.72
        img = cv2.GaussianBlur(img, (9, 9), 0)
        img += rng.normal(0, 9, img.shape)
    elif effect == "night":
        img = img * 0.2
        img += rng.normal(0, 8, img.shape)
    elif effect == "flicker":
        img = img * (0.92 + 0.08 * math.sin(f / 9.0))
        img += rng.normal(0, 4, img.shape)
    else:
        img += rng.normal(0, 3.5, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def render(name, kind, cars, frames, effect, seed):
    rng = np.random.default_rng(seed)
    bg = render_background(kind, rng)
    out = OUT_DIR / "videos" / f"{name}.mp4"
    vw = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    log = []
    for f in range(frames):
        for c in cars:
            if c.active(f):
                c.step(f)
        resolve_contacts(cars, f, log)
        img = bg.copy()
        for c in cars:
            if c.active(f):
                c.draw(img)
        vw.write(apply_effects(img, effect, rng, f))
    vw.release()
    return log


# --------------------------------------------------------------- roteiros --
def hwy_car(lane, x, speed, spawn, color, **kw):
    y, h = HWY_LANES[lane]
    return Car(x, y, h, speed, spawn, color, **kw)


def x_car(lane, pos, speed, spawn, color, **kw):
    c, h = X_LANES[lane]
    if lane.startswith("H"):
        return Car(pos, c, h, speed, spawn, color, **kw)
    return Car(c, pos, h, speed, spawn, color, **kw)


def colors(rng, n):
    idx = rng.permutation(len(PALETTE))[:n]
    return [PALETTE[i] for i in idx]


def sc_rear_end(rng, lane="F3", effect="clear"):
    c = colors(rng, 5)
    east = HWY_LANES[lane][1] == 0
    start = -60 if east else W + 60
    vl = 4.0 * rng.uniform(0.9, 1.1)
    vs = 9.0 * rng.uniform(0.95, 1.1)
    cars = [hwy_car(lane, start, vl, 20, c[0]),
            hwy_car(lane, start, vs, 90, c[1])]
    other = "F1" if lane in ("F3", "F4") else "F4"
    o_start = W + 60 if HWY_LANES[other][1] == 180 else -60
    cars += [hwy_car(other, o_start, 6.5, 10, c[2]), hwy_car(other, o_start, 7.0, 120, c[3])]
    return "hwy", cars, 270, effect


def sc_frontal(rng, effect="clear"):
    c = colors(rng, 4)
    lane = rng.choice(["F2", "F3"])
    v1, v2 = 6.5 * rng.uniform(0.9, 1.1), 6.5 * rng.uniform(0.9, 1.1)
    y = HWY_LANES[lane][0]
    cars = [Car(-60, y, 0, v1, 30, c[0]),            # sentido correto ou contramão
            Car(W + 60, y, 180, v2, 30, c[1])]
    cars += [hwy_car("F4", -60, 7.5, 5, c[2]), hwy_car("F1", W + 60, 6.0, 60, c[3])]
    return "hwy", cars, 240, effect


def sc_lateral(rng, variant=0, effect="clear"):
    c = colors(rng, 4)
    if variant == 0:  # veículo vindo do norte (V-SUL) atinge o lateral de quem segue para leste
        vs, ve = 7.0, 8.0
        t0 = 60
        striker = x_car("V-SUL", -60, vs, t0, c[0])
        t_hit = t0 + (248 + 60) / vs
        target = x_car("H-LESTE", -60, ve, int(t_hit - (440 + 60) / ve), c[1])
        extra = [x_car("H-OESTE", W + 60, 6.0, 10, c[2])]
    else:  # veículo vindo do sul (V-NORTE) atinge quem segue para oeste
        vs, ve = 7.5, 7.0
        t0 = 50
        striker = x_car("V-NORTE", H + 60, vs, t0, c[0])
        t_hit = t0 + (H + 60 - 272) / vs
        target = x_car("H-OESTE", W + 60, ve, int(t_hit - (W + 60 - 520) / ve), c[1])
        extra = [x_car("H-LESTE", -60, 8.0, 0, c[2])]
    return "x", [striker, target] + extra, 260, effect


def sc_normal_pass(rng):
    c = colors(rng, 7)
    cars = [hwy_car("F3", -60, 5.0, 10, c[0]), hwy_car("F4", -60, 8.5, 40, c[1]),
            hwy_car("F4", -60, 8.0, 130, c[2]), hwy_car("F1", W + 60, 7.0, 20, c[3]),
            hwy_car("F2", W + 60, 4.5, 30, c[4]), hwy_car("F1", W + 60, 8.0, 80, c[5]),
            hwy_car("F3", -60, 6.0, 150, c[6])]
    return "hwy", cars, 260, "clear"


def sc_normal_brake(rng):
    c = colors(rng, 5)
    # líder freia forte até parar, segue parado e depois arranca; seguidor para com folga
    lead = hwy_car("F2", W + 60, 7.0, 15, c[0], schedule=[(85, 105, -0.35), (170, 200, 0.25)])
    foll = hwy_car("F2", W + 60, 7.0, 40, c[1], schedule=[(95, 117, -0.33), (180, 210, 0.25)])
    cars = [lead, foll, hwy_car("F3", -60, 6.0, 20, c[2]), hwy_car("F4", -60, 8.0, 70, c[3]),
            hwy_car("F1", W + 60, 6.0, 110, c[4])]
    return "hwy", cars, 270, "clear"


def sc_normal_approach(rng):
    c = colors(rng, 5)
    # veículo para na faixa (pane); outro se aproxima e para atrás sem tocar
    stopped = hwy_car("F4", -60, 6.0, 5, c[0], schedule=[(70, 90, -0.3)])
    appro = hwy_car("F4", -60, 8.0, 95, c[1], schedule=[(125, 150, -0.32)])
    cars = [stopped, appro, hwy_car("F3", -60, 7.0, 40, c[2]), hwy_car("F2", W + 60, 6.5, 30, c[3]),
            hwy_car("F3", -60, 7.5, 150, c[4])]
    return "hwy", cars, 270, "clear"


def sc_normal_intersection(rng):
    c = colors(rng, 5)
    # leste passa antes; veículo do norte para na retenção, espera e cruza depois
    cars = [x_car("H-LESTE", -60, 8.0, 10, c[0]),
            x_car("V-SUL", -60, 6.0, 20, c[1], schedule=[(40, 60, -0.29), (130, 160, 0.2)]),
            x_car("H-OESTE", W + 60, 7.0, 40, c[2]),
            x_car("V-NORTE", H + 60, 6.0, 150, c[3]),
            x_car("H-LESTE", -60, 7.0, 170, c[4])]
    return "x", cars, 270, "clear"


def sc_normal_dense(rng):
    c = colors(rng, 9)
    cars = []
    plan = [("F3", 6.0, 10), ("F3", 6.2, 38), ("F3", 6.0, 66), ("F4", 8.0, 25), ("F4", 8.2, 80),
            ("F1", 7.0, 15), ("F2", 5.0, 30), ("F2", 5.2, 58), ("F1", 7.2, 75)]
    for i, (lane, sp, spawn) in enumerate(plan):
        start = -60 if HWY_LANES[lane][1] == 0 else W + 60
        sched = [(110, 125, -0.2), (150, 170, 0.15)] if lane == "F3" else []
        cars.append(hwy_car(lane, start, sp, spawn, c[i], schedule=sched))
    return "hwy", cars, 260, "flicker"


# conjunto: (nome neutro, função, kwargs, seed, rótulo esperado, conjunto)
SPEC = [
    ("traseira", sc_rear_end, {"lane": "F3"}, 11, "COLISAO_TRASEIRA", "ajuste", "Colisão traseira em pista livre"),
    ("frontal", sc_frontal, {}, 12, "COLISAO_FRONTAL", "ajuste", "Colisão frontal com veículo na contramão"),
    ("lateral", sc_lateral, {"variant": 0}, 13, "COLISAO_LATERAL", "ajuste", "Colisão lateral (T-bone) na interseção"),
    ("normal_pass", sc_normal_pass, {}, 14, "SEM_ACIDENTE", "ajuste", "Passagem normal com ultrapassagens"),
    ("normal_brake", sc_normal_brake, {}, 15, "SEM_ACIDENTE", "ajuste", "Frenagem forte e parada sem contato"),
    ("amb_fog", sc_rear_end, {"lane": "F4", "effect": "fog"}, 16, "INDETERMINADO", "ajuste", "Neblina densa (há impacto traseiro)"),
    ("traseira", sc_rear_end, {"lane": "F2"}, 21, "COLISAO_TRASEIRA", "avaliacao", "Colisão traseira sentido oeste"),
    ("traseira", sc_rear_end, {"lane": "F1"}, 22, "COLISAO_TRASEIRA", "avaliacao", "Colisão traseira faixa da esquerda"),
    ("frontal", sc_frontal, {}, 23, "COLISAO_FRONTAL", "avaliacao", "Colisão frontal (outra faixa/velocidades)"),
    ("lateral", sc_lateral, {"variant": 1}, 24, "COLISAO_LATERAL", "avaliacao", "Colisão lateral vindo do sul"),
    ("normal_approach", sc_normal_approach, {}, 25, "SEM_ACIDENTE", "avaliacao", "Aproximação de veículo parado sem impacto"),
    ("normal_intersection", sc_normal_intersection, {}, 26, "SEM_ACIDENTE", "avaliacao", "Cruzamento com parada na retenção"),
    ("normal_dense", sc_normal_dense, {}, 27, "SEM_ACIDENTE", "avaliacao", "Tráfego denso com onda de frenagem e variação de luz"),
    ("amb_night", sc_lateral, {"variant": 0, "effect": "night"}, 28, "INDETERMINADO", "avaliacao", "Noite / baixa luminosidade (há impacto lateral)"),
]


def main():
    (OUT_DIR / "videos").mkdir(parents=True, exist_ok=True)
    manifest, truth = {}, {}
    order = np.random.default_rng(2026).permutation(len(SPEC))  # embaralha nomes neutros
    for pos, k in enumerate(order, start=1):
        kind_name, fn, kw, seed, label, split, desc = SPEC[k]
        name = f"seq_{pos:02d}"
        rng = np.random.default_rng(seed)
        kind, cars, frames, effect = fn(rng, **kw)
        log = render(name, kind, cars, frames, effect, seed)
        cam = CAM_HWY if kind == "hwy" else CAM_X
        manifest[name] = {"arquivo": f"videos/{name}.mp4", "camera_id": cam, "conjunto": split, "fps": FPS}
        truth[name] = {"esperado": label, "cenario": kind_name, "descricao": desc, "efeito": effect,
                       "contatos_simulados": log}
        print(f"{name}  {split:9s} {label:17s} {kind_name:20s} contatos={[(c['frame'], c['x'], c['y']) for c in log]}")
    (OUT_DIR / "manifest.json").write_text(json.dumps(dict(sorted(manifest.items())), indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "ground_truth.json").write_text(json.dumps(dict(sorted(truth.items())), indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()

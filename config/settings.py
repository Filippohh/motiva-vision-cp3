"""Configuração central do projeto Motiva Vision.

Valores sensíveis (RM, usuário/senha do broker) são lidos de variáveis de
ambiente ou de um arquivo `.env` local (fora do repositório, ver .gitignore).
"""
import os
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.split(" #", 1)[0].strip())


_load_dotenv()

RM = os.environ.get("MOTIVA_RM", "rm565994")
BROKER_HOST = os.environ.get("MQTT_HOST", "broker.hivemq.com")
BROKER_PORT = int(os.environ.get("MQTT_PORT", "1883"))
BROKER_USER = os.environ.get("MQTT_USER") or None
BROKER_PASS = os.environ.get("MQTT_PASS") or None

TOPIC_BASE = f"motiva/{RM}"
TOPIC_EVENTOS = f"{TOPIC_BASE}/eventos"      # visão -> central
TOPIC_COMANDOS = f"{TOPIC_BASE}/comandos"    # central -> ESP32
TOPIC_STATUS = f"{TOPIC_BASE}/status"        # ESP32 -> central (ACK, heartbeat, LWT)
TOPIC_VISAO = f"{TOPIC_BASE}/visao"          # visão -> central (monitoramento / SEM_ACIDENTE)

QOS_EVENTOS = 1
QOS_COMANDOS = 1

# Fuso horário informado em todos os timestamps (Brasília, sem horário de verão)
TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")

CENTRAL_HTTP_PORT = int(os.environ.get("CENTRAL_PORT", "5000"))
CENTRAL_HEARTBEAT_S = 5          # central -> ESP32 (PING)
ESP32_TIMEOUT_S = 15             # central considera ESP32 sem contato

EVIDENCE_DIR = ROOT / "results" / "evidencias"
DB_PATH = Path(os.environ.get("MOTIVA_DB", ROOT / "results" / "central.db"))
CAMERAS_FILE = ROOT / "config" / "cameras.json"

"""Broker MQTT local (amqtt) para testes offline, inclusive o teste de broker indisponível.

Uso: python -m tools.local_broker [--port 1883]
"""
import argparse
import asyncio

from amqtt.broker import Broker


async def run(port):
    cfg = {"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
           "sys_interval": 0, "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]},
           "topic-check": {"enabled": False}}
    broker = Broker(cfg)
    await broker.start()
    print(f"[broker] ouvindo em 127.0.0.1:{port}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=1883)
    asyncio.run(run(ap.parse_args().port))

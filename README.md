# Motiva Vision: detecção e resposta simulada a acidentes rodoviários

Protótipo acadêmico (CP3, Prof. Marcelo Morgantini). A cadeia é:

**vídeo → visão computacional (Python/OpenCV) → MQTT → central operacional (Flask + SQLite) → decisão humana → ESP32 (Wokwi) → ACK de execução → central**

> Simulação acadêmica. As recomendações são regras fictícias: não fazem diagnóstico e não acionam serviços reais. A decisão é sempre do operador.

## Estrutura

| Pasta | Conteúdo |
|---|---|
| `vision/` | `pipeline.py` (detecção MOG2, rastreamento, análise temporal, confiança, event_id), `run_vision.py` (CLI + publicação MQTT), `evaluate.py` (métricas), `generate_dataset.py` (gera os vídeos de teste) |
| `central/` | `app.py` (Flask + MQTT + SQLite, histórico, comandos, exportação CSV/JSON) e `templates/index.html` (painel) |
| `esp32/` | `sketch.ino`, `diagram.json`, `libraries.txt` (projeto Wokwi) |
| `common/` | `recommendation.py` (matriz de recomendação), `mqtt.py` |
| `config/` | `settings.py` (tópicos, QoS, fuso), `cameras.json` (câmeras fictícias, faixas, escala) |
| `tools/` | `esp32_sim.py` (gêmeo em Python do firmware), `local_broker.py` (broker local amqtt), `integration_test.py` (teste ponta a ponta) |
| `dataset/` | 14 vídeos `seq_XX.mp4`, `manifest.json` (vídeo → câmera/conjunto), `ground_truth.json` (rótulos, lidos **apenas** por `evaluate.py`) |
| `results/` | `avaliacao*.json/.md`, `evidencias*/` (JPG + MP4 de cada evento), `integracao/` (logs, export da central) |
| `docs/` | `relatorio.pdf` e imagens |

## Instalação

Requer Python 3.10+ (testado com o 3.13 no Windows 11).

```bash
pip install -r requirements.txt
```

Copie `.env.example` para `.env` e defina `MOTIVA_RM` (o mesmo valor do `#define RM` em `esp32/sketch.ino`). O `.env` fica fora do Git.

## Execução (demonstração completa no broker público)

1. **Central**: `python -m central.app`, depois abra http://localhost:5000
2. **ESP32 no Wokwi**: crie um projeto ESP32 em https://wokwi.com, cole `esp32/sketch.ino` e `esp32/diagram.json` e adicione as bibliotecas de `esp32/libraries.txt` (PubSubClient, ArduinoJson 6, LiquidCrystal I2C). Ajuste `RM` e clique em ▶. O ESP32 usa `Wokwi-GUEST` e o `broker.hivemq.com:1883`.
   *Sem Wokwi:* `python -m tools.esp32_sim` roda a mesma lógica em Python.
3. **Visão**: `python -m vision.run_vision --video dataset/videos/seq_07.mp4 --camera CAM-SP000-KM118-X --realtime`
   ou `python -m vision.run_vision --all` para todos os vídeos.
4. No painel: veja o evento (tipo, confiança, evidência, recomendação). Use **CONFIRMAR**, **REJEITAR** ou **ENCERRAR** e acompanhe o comando ser enviado, recebido e aplicado pelo ESP32.

## Testes

```bash
python -m vision.generate_dataset      # (re)gera os 14 vídeos de teste
python -m vision.evaluate              # avaliação por vídeo + métricas -> results/avaliacao.*
python -m tools.integration_test       # ponta a ponta com broker local: 18 verificações
```

`integration_test` sobe um broker local, a central e o gêmeo do ESP32. Ele testa: cadeia completa, confirmação, rejeição, encerramento, comando duplicado, evento duplicado, ocorrência persistente, broker indisponível (derruba e religa o broker), central muda (timeout de heartbeat) e persistência do histórico.

## Protocolo MQTT (resumo)

| Tópico | Sentido | QoS | Retida | Conteúdo |
|---|---|---|---|---|
| `motiva/<RM>/eventos` | visão → central | 1 | não | evento JSON `motiva.evento.v1` |
| `motiva/<RM>/visao` | visão → central | 0 | não | monitoramento periódico (`SEM_ACIDENTE` / `OCORRENCIA_ATIVA`) |
| `motiva/<RM>/comandos` | central → ESP32 | 1 | não | `{command_id, event_id, acao}`; `PING` a cada 5 s |
| `motiva/<RM>/status` | ESP32 → central | 0* | ONLINE/LWT sim | ACK (RECEBIDO/APLICADO/DUPLICADO/IGNORADO_ENCERRADO), HEARTBEAT 5 s |

\*O PubSubClient só publica com QoS 0. A garantia vem do ACK de aplicação: a central retransmite o mesmo `command_id` a cada 6 s, até 3 vezes. Detalhes no relatório.

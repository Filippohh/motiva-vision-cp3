"""Avalia o detector contra os rótulos esperados (dataset/ground_truth.json).

Executa a análise offline (sem MQTT) e gera:
  results/avaliacao.json   resultados completos por vídeo
  results/avaliacao.md     tabela-resumo para o relatório

Uso: python -m vision.evaluate [--split avaliacao|ajuste]
"""
import argparse
import json
import shutil
from collections import Counter

from config import settings
from vision.run_vision import analyze_video

ROOT = settings.ROOT
ACID = {"COLISAO_TRASEIRA", "COLISAO_FRONTAL", "COLISAO_LATERAL"}


class _NoPub:
    def event(self, ev):
        pass

    def status(self, p):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["ajuste", "avaliacao"])
    args = ap.parse_args()
    manifest = json.loads((ROOT / "dataset" / "manifest.json").read_text(encoding="utf-8"))
    truth = json.loads((ROOT / "dataset" / "ground_truth.json").read_text(encoding="utf-8"))
    ev_dir = settings.ROOT / "results" / "evidencias_avaliacao"
    settings.EVIDENCE_DIR = ev_dir
    shutil.rmtree(ev_dir, ignore_errors=True)

    rows = []
    for name, m in manifest.items():
        if args.split and m["conjunto"] != args.split:
            continue
        gt = truth[name]
        r = analyze_video(ROOT / "dataset" / m["arquivo"], m["camera_id"], _NoPub(), verbose=False)
        pred = r["saida"]
        ids = {e["event_id"] for e in r["eventos"]}
        contact = gt["contatos_simulados"][0]["frame"] if gt["contatos_simulados"] else None
        lat = None
        last = r["eventos"][-1] if r["eventos"] else None
        if contact is not None and last and last["evidencia"]["frame"] >= contact - 5:
            lat = round((last["evidencia"]["frame_decisao"] - contact) / r["fps"], 2)
        esperado = gt["esperado"]
        if esperado in ACID:
            res = "ACERTO" if pred == esperado else ("FALSO_NEGATIVO" if pred == "SEM_ACIDENTE" else
                                                      "REVISAO_HUMANA" if pred == "INDETERMINADO" else "CLASSE_ERRADA")
        elif esperado == "SEM_ACIDENTE":
            res = "ACERTO" if pred == "SEM_ACIDENTE" else ("FALSO_POSITIVO" if pred in ACID else "ALERTA_REVISAO")
        else:  # INDETERMINADO esperado (visibilidade prejudicada com impacto real)
            res = "ACERTO" if pred == "INDETERMINADO" else ("FALSO_NEGATIVO" if pred == "SEM_ACIDENTE" else "CERTEZA_INDEVIDA")
        rows.append({
            "video": name, "conjunto": m["conjunto"], "cenario": gt["descricao"], "esperado": esperado,
            "obtido": pred, "resultado": res, "event_ids_unicos": len(ids), "publicacoes": len(r["eventos"]),
            "confianca": last["confianca"]["valor"] if last else None,
            "regra": last["recomendacao"]["regra"] if last else None,
            "evidencia": (f"{last['evidencia']['imagem']} (t={last['evidencia']['tempo_video_s']} s, quadro {last['evidencia']['frame']})" if last else "-"),
            "latencia_s": lat, "candidatos": r["candidatos"], "tempo_proc_s": r["tempo_proc_s"], "quadros": r["quadros"],
        })
        print(f"{name} {m['conjunto']:9s} esperado={esperado:17s} obtido={pred:17s} {res:15s} ids={len(ids)} lat={lat}")

    # métricas binárias: "há impacto" (inclui ambíguos, que contêm impacto) x "sistema alertou"
    tp = sum(1 for r in rows if r["esperado"] != "SEM_ACIDENTE" and r["obtido"] != "SEM_ACIDENTE")
    fn = sum(1 for r in rows if r["esperado"] != "SEM_ACIDENTE" and r["obtido"] == "SEM_ACIDENTE")
    fp = sum(1 for r in rows if r["esperado"] == "SEM_ACIDENTE" and r["obtido"] != "SEM_ACIDENTE")
    tn = sum(1 for r in rows if r["esperado"] == "SEM_ACIDENTE" and r["obtido"] == "SEM_ACIDENTE")
    acc_cls = [r for r in rows if r["esperado"] in ACID]
    resumo = {
        "videos": len(rows), "resultados": dict(Counter(r["resultado"] for r in rows)),
        "deteccao": {"VP": tp, "FN": fn, "FP": fp, "VN": tn,
                     "precisao": round(tp / (tp + fp), 3) if tp + fp else None,
                     "recall": round(tp / (tp + fn), 3) if tp + fn else None},
        "acuracia_classificacao_acidentes": round(sum(r["resultado"] == "ACERTO" for r in acc_cls) / len(acc_cls), 3) if acc_cls else None,
        "latencia_media_s": round(sum(r["latencia_s"] for r in rows if r["latencia_s"] is not None) /
                                  max(1, sum(r["latencia_s"] is not None for r in rows)), 2),
        "duplicatas": sum(1 for r in rows if r["event_ids_unicos"] > 1),
    }
    print(json.dumps(resumo, indent=2, ensure_ascii=False))
    suffix = f"_{args.split}" if args.split else ""
    out = ROOT / "results" / f"avaliacao{suffix}.json"
    out.write_text(json.dumps({"resumo": resumo, "videos": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    md = ["| Vídeo | Conjunto | Cenário | Esperado | Obtido | Conf. | Regra | IDs | Resultado |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['video']} | {r['conjunto']} | {r['cenario']} | {r['esperado']} | {r['obtido']} | "
                  f"{r['confianca'] if r['confianca'] is not None else '-'} | {r['regra'] or '-'} | {r['event_ids_unicos']} | {r['resultado']} |")
    (ROOT / "results" / f"avaliacao{suffix}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"gravado {out}")


if __name__ == "__main__":
    main()

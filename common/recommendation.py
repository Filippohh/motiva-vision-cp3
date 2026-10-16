"""Matriz de recomendação operacional SIMULADA.

Regras fictícias para fins acadêmicos. Não constituem protocolo médico, não
emitem diagnóstico e não acionam serviços reais. A decisão é sempre do operador.
As regras são avaliadas em ordem; a primeira que casar é aplicada.
"""

ACIDENTES = ("COLISAO_FRONTAL", "COLISAO_LATERAL", "COLISAO_TRASEIRA")

MATRIZ = [
    {
        "regra": "R1",
        "condicao": "Tipo INDETERMINADO ou confiança abaixo do limiar (0,60)",
        "recurso": "REVISAO_OPERADOR",
        "texto": "Solicitar revisão do operador sem concluir o tipo ou a gravidade.",
    },
    {
        "regra": "R2",
        "condicao": "Colisão frontal OU colisão com 3+ veículos envolvidos",
        "recurso": "AVALIACAO_PRIORITARIA_RESGATE",
        "texto": "Sugerir avaliação prioritária e recurso adicional de resgate (simulação).",
    },
    {
        "regra": "R3",
        "condicao": "Colisão lateral/traseira com indicador de impacto de alta energia (vel. relativa > 40 km/h)",
        "recurso": "AMBULANCIA_SIMULADA",
        "texto": "Sugerir atendimento por ambulância na simulação.",
    },
    {
        "regra": "R4",
        "condicao": "Colisão lateral/traseira sem alta energia, com faixa obstruída",
        "recurso": "EQUIPE_OPERACIONAL_E_SINALIZACAO",
        "texto": "Solicitar equipe operacional e sinalização/guincho para liberar a faixa.",
    },
    {
        "regra": "R5",
        "condicao": "Evento confirmado sem sinais adicionais",
        "recurso": "EQUIPE_OPERACIONAL",
        "texto": "Solicitar avaliação da equipe operacional.",
    },
]

LIMIAR_CONFIANCA = 0.60
LIMIAR_ALTA_ENERGIA_KMH = 40.0


def recomendar(tipo, confianca, indicadores):
    """Retorna dict com regra, recurso e justificativa da regra aplicada."""
    rule = _escolher(tipo, confianca, indicadores)
    just = rule["texto"] + " Regra " + rule["regra"] + ": " + rule["condicao"] + "."
    detalhes = []
    vel = indicadores.get("velocidade_relativa_kmh")
    if isinstance(vel, (int, float)):
        detalhes.append(f"vel. relativa estimada {vel:.0f} km/h")
    faixa = indicadores.get("faixa_obstruida")
    if faixa:
        detalhes.append(f"faixa obstruída: {faixa if isinstance(faixa, str) else ', '.join(faixa)}")
    detalhes.append(f"veículos envolvidos: {indicadores.get('veiculos_envolvidos', 'desconhecido')}")
    return {"regra": rule["regra"], "recurso": rule["recurso"],
            "justificativa": just + " Indicadores: " + "; ".join(detalhes) + "."}


def _escolher(tipo, confianca, ind):
    by = {r["regra"]: r for r in MATRIZ}
    if tipo not in ACIDENTES or confianca < LIMIAR_CONFIANCA:
        return by["R1"]
    nveic = ind.get("veiculos_envolvidos")
    if tipo == "COLISAO_FRONTAL" or (isinstance(nveic, int) and nveic >= 3):
        return by["R2"]
    if ind.get("impacto_alta_energia") is True:
        return by["R3"]
    faixa = ind.get("faixa_obstruida")
    if faixa and faixa != "desconhecido":
        return by["R4"]
    return by["R5"]

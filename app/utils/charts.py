"""
Utilitários de gráfico reutilizáveis para todos os módulos do AEGIS.
"""
import datetime as dt


def build_time_series(
    data: list,
    days: int,
    value_field: str,
    date_field: str = "day",
    secondary_field: str = None,
    fill_value=0,
) -> dict:
    """
    Constrói série temporal com janela de N dias, preenchendo dias sem dados.

    Args:
        data:             Lista de dicts com date_field e value_field.
        days:             Número de dias a cobrir (janela termina hoje).
        value_field:      Campo do valor principal (e.g. 'net_amount').
        date_field:       Campo da data nos dicts (default 'day').
        secondary_field:  Campo de valor secundário opcional (e.g. 'gross_amount').
        fill_value:       Valor para dias sem dados (default 0).

    Returns:
        {
          'labels':    ['2026-04-01', ...],   # YYYY-MM-DD, len == days
          'values':    [1234.5, 0, ...],      # value_field por dia
          'secondary': [1500.0, 0, ...],      # secondary_field por dia, ou []
        }
    """
    today      = dt.date.today()
    date_range = [
        (today - dt.timedelta(days=days - 1 - i)).isoformat()
        for i in range(days)
    ]

    value_map     = {r[date_field]: r[value_field]       for r in data if date_field in r}
    secondary_map = {}
    if secondary_field:
        secondary_map = {r[date_field]: r[secondary_field] for r in data
                         if date_field in r and secondary_field in r}

    return {
        "labels":    date_range,
        "values":    [value_map.get(d, fill_value)     for d in date_range],
        "secondary": [secondary_map.get(d, fill_value) for d in date_range]
                     if secondary_field else [],
    }


def display_labels(labels: list, every_n: int = 5) -> list:
    """
    Retorna labels compactas para exibição no eixo X do Chart.js.
    Mostra apenas a cada N dias para não sobrecarregar.
    """
    result = []
    for i, label in enumerate(labels):
        day = int(label[8:10])
        result.append(label[5:] if (i % every_n == 0 or day == 1) else "")
    return result

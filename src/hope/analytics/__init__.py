"""Аналитика результатов: раунд-трипы, сводные метрики, кривая equity.

Чистые функции над pandas.DataFrame, загруженными из БД результатов (см. store/schema.sql).
Ничего не пишут и не читают сами — данные передаются снаружи (монитор, ноутбуки, тесты).
"""

from .metrics import (
    ROUNDTRIP_COLUMNS,
    downsample,
    equity_curve,
    roundtrips,
    spread_captured_bps,
    summary,
    to_jsonable,
)

__all__ = [
    "ROUNDTRIP_COLUMNS",
    "downsample",
    "equity_curve",
    "roundtrips",
    "spread_captured_bps",
    "summary",
    "to_jsonable",
]

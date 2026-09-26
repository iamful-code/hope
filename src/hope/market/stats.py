"""Скользящая статистика по символу: медиана спреда, волатильность mid, поток сделок, дисбаланс."""

from __future__ import annotations

import math
from collections import deque

from ..types import Bbo, Side, Trade


class RollingStats:
    def __init__(self, window_secs: int = 120) -> None:
        self.window_ms = window_secs * 1000
        self._spread: deque[tuple[int, float]] = deque()
        self._mid: deque[tuple[int, float]] = deque()
        self._trades: deque[tuple[int, float, float]] = deque()  # ts, signed_qty, notional
        self._last_sample_sec = -1
        self.last_bbo: Bbo | None = None

    def _trim(self, now: int) -> None:
        lim = now - self.window_ms
        for dq in (self._spread, self._mid, self._trades):
            while dq and dq[0][0] < lim:
                dq.popleft()

    def on_bbo(self, bbo: Bbo) -> None:
        self.last_bbo = bbo
        sec = bbo.ts // 1000
        if sec != self._last_sample_sec and bbo.valid:
            self._last_sample_sec = sec
            self._spread.append((bbo.ts, bbo.spread_bps))
            self._mid.append((bbo.ts, bbo.mid))
            self._trim(bbo.ts)

    def on_trade(self, t: Trade) -> None:
        self._trades.append((t.ts, t.qty * t.taker_side.sign, t.qty * t.price))
        self._trim(t.ts)

    @property
    def spread_med_bps(self) -> float:
        if not self._spread:
            return 0.0
        vals = sorted(v for _, v in self._spread)
        return vals[len(vals) // 2]

    @property
    def vol_bps(self) -> float:
        """Std лог-доходностей посекундного mid, приведённая к минуте, bps."""
        if len(self._mid) < 10:
            return 0.0
        prev = None
        rets = []
        for _, m in self._mid:
            if prev and prev > 0 and m > 0:
                rets.append(math.log(m / prev))
            prev = m
        if len(rets) < 5:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var) * math.sqrt(60) * 1e4

    @property
    def trades_per_min(self) -> float:
        if not self._trades:
            return 0.0
        span = max(self._trades[-1][0] - self._trades[0][0], 1000)
        return len(self._trades) / (span / 60_000)

    @property
    def turnover_per_min(self) -> float:
        if not self._trades:
            return 0.0
        span = max(self._trades[-1][0] - self._trades[0][0], 1000)
        return sum(n for _, _, n in self._trades) / (span / 60_000)

    def flow_imbalance(self, window_secs: float | None = None, now: int | None = None) -> float:
        """(buy - sell) / (buy + sell) по объёму за окно, в [-1, 1]."""
        if not self._trades:
            return 0.0
        lim = (now or self._trades[-1][0]) - int((window_secs or self.window_ms / 1000) * 1000)
        b = s = 0.0
        for ts, sq, _ in reversed(self._trades):
            if ts < lim:
                break
            if sq > 0:
                b += sq
            else:
                s -= sq
        return (b - s) / (b + s) if b + s > 0 else 0.0

    @property
    def n_samples(self) -> int:
        return len(self._spread)

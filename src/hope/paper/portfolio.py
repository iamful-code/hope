"""Портфель: позиции по средней цене, реализованный/нереализованный PnL, комиссии, equity, просадка."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..types import Fill, Side


@dataclass(slots=True)
class Position:
    symbol: str
    qty: float = 0.0  # >0 long, <0 short
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    n_fills: int = 0
    opened_ts: int = 0
    last_ts: int = 0
    mark: float = 0.0

    @property
    def notional(self) -> float:
        return abs(self.qty) * (self.mark or self.avg_price)

    @property
    def unrealized_pnl(self) -> float:
        if self.qty == 0 or self.mark <= 0:
            return 0.0
        return (self.mark - self.avg_price) * self.qty

    @property
    def side(self) -> str:
        return "long" if self.qty > 0 else "short" if self.qty < 0 else "flat"


@dataclass
class Portfolio:
    initial_equity: float = 10_000.0
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    n_fills: int = 0
    peak_equity: float = 0.0
    max_drawdown: float = 0.0
    day_start_equity: float = 0.0
    day_key: int = -1

    def __post_init__(self) -> None:
        self.peak_equity = self.initial_equity
        self.day_start_equity = self.initial_equity

    def position(self, symbol: str) -> Position:
        p = self.positions.get(symbol)
        if p is None:
            p = Position(symbol)
            self.positions[symbol] = p
        return p

    def qty(self, symbol: str) -> float:
        p = self.positions.get(symbol)
        return p.qty if p else 0.0

    # ---------------------------------------------------------------- события
    def on_fill(self, f: Fill) -> float:
        """Обновить позицию по исполнению. Возвращает реализованный PnL этого исполнения (без комиссии)."""
        p = self.position(f.symbol)
        signed = f.qty if f.side is Side.BUY else -f.qty
        realized = 0.0
        if p.qty == 0 or (p.qty > 0) == (signed > 0):
            # открытие или наращивание: средняя цена
            new_qty = p.qty + signed
            p.avg_price = (p.avg_price * abs(p.qty) + f.price * abs(signed)) / abs(new_qty)
            if p.qty == 0:
                p.opened_ts = f.ts
            p.qty = new_qty
        else:
            # сокращение / переворот
            closing = min(abs(signed), abs(p.qty))
            direction = 1.0 if p.qty > 0 else -1.0
            realized = (f.price - p.avg_price) * closing * direction
            remaining = abs(signed) - closing
            new_qty = p.qty + signed
            if abs(new_qty) < 1e-12:
                p.qty = 0.0
                p.avg_price = 0.0
                p.opened_ts = 0
            elif remaining > 1e-12:
                # переворот: остаток открывает новую позицию по цене исполнения
                p.qty = new_qty
                p.avg_price = f.price
                p.opened_ts = f.ts
            else:
                p.qty = new_qty
        p.realized_pnl += realized
        p.fees += f.fee
        p.n_fills += 1
        p.last_ts = f.ts
        p.mark = f.price if p.mark <= 0 else p.mark
        self.realized_pnl += realized
        self.fees += f.fee
        self.n_fills += 1
        f.realized_pnl = realized
        f.position_after = p.qty
        return realized

    def on_mark(self, symbol: str, price: float) -> None:
        p = self.positions.get(symbol)
        if p is not None and price > 0:
            p.mark = price

    def on_funding(self, symbol: str, rate: float, mark: float) -> float:
        """Фандинг: long платит при положительной ставке. Возвращает начисление (USDT, знак = влияние на equity)."""
        p = self.positions.get(symbol)
        if p is None or p.qty == 0:
            return 0.0
        amount = -p.qty * mark * rate
        p.funding += amount
        self.funding += amount
        return amount

    # ---------------------------------------------------------------- показатели
    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    @property
    def net_realized(self) -> float:
        return self.realized_pnl - self.fees + self.funding

    @property
    def equity(self) -> float:
        return self.initial_equity + self.net_realized + self.unrealized_pnl

    @property
    def gross_notional(self) -> float:
        return sum(p.notional for p in self.positions.values() if p.qty != 0)

    @property
    def n_positions(self) -> int:
        return sum(1 for p in self.positions.values() if p.qty != 0)

    def update_drawdown(self, now_ms: int) -> None:
        eq = self.equity
        day = now_ms // 86_400_000
        if day != self.day_key:
            self.day_key = day
            self.day_start_equity = eq
        if eq > self.peak_equity:
            self.peak_equity = eq
        dd = self.peak_equity - eq
        if dd > self.max_drawdown:
            self.max_drawdown = dd

    @property
    def daily_pnl(self) -> float:
        return self.equity - self.day_start_equity

    def snapshot(self) -> dict:
        return {
            "equity": self.equity,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "fees": self.fees,
            "funding": self.funding,
            "gross_notional": self.gross_notional,
            "n_positions": self.n_positions,
            "n_fills": self.n_fills,
            "max_drawdown": self.max_drawdown,
            "daily_pnl": self.daily_pnl,
        }

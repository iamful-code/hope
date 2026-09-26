"""Риск-слой: лимиты портфеля и kill-switch. Блокирует только ВХОДЫ; выходы всегда разрешены."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import RiskCfg
from ..types import Side
from .portfolio import Portfolio


@dataclass(slots=True)
class RiskVerdict:
    ok: bool
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskCfg) -> None:
        self.cfg = cfg
        self.killed = False
        self.kill_reason = ""

    def check_portfolio(self, pf: Portfolio) -> None:
        if not self.killed and pf.daily_pnl <= -abs(self.cfg.max_daily_loss_usd):
            self.killed = True
            self.kill_reason = f"дневной убыток {pf.daily_pnl:.2f} <= -{self.cfg.max_daily_loss_usd:.2f}"

    def reset_day(self) -> None:
        self.killed = False
        self.kill_reason = ""

    def allow_entry(
        self, pf: Portfolio, symbol: str, side: Side, qty: float, price: float, n_open_orders: int
    ) -> RiskVerdict:
        """Разрешить ли ордер, увеличивающий позицию по symbol."""
        if self.killed:
            return RiskVerdict(False, f"kill-switch: {self.kill_reason}")
        pos = pf.qty(symbol)
        signed = qty if side is Side.BUY else -qty
        new_qty = pos + signed
        # уменьшение позиции — не вход
        if pos != 0 and abs(new_qty) <= abs(pos) + 1e-12:
            return RiskVerdict(True)
        notional_after = abs(new_qty) * price
        if notional_after > self.cfg.max_position_notional_usd + 1e-9:
            return RiskVerdict(False, f"позиция {notional_after:.0f} > лимита {self.cfg.max_position_notional_usd:.0f}")
        gross_after = pf.gross_notional - abs(pos) * price + notional_after
        if gross_after > self.cfg.max_gross_notional_usd + 1e-9:
            return RiskVerdict(False, f"экспозиция {gross_after:.0f} > лимита {self.cfg.max_gross_notional_usd:.0f}")
        if n_open_orders >= self.cfg.max_open_orders_per_symbol:
            return RiskVerdict(False, f"слишком много открытых ордеров по {symbol}")
        return RiskVerdict(True)

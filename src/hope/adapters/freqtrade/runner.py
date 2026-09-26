"""Запуск стратегий Freqtrade (IStrategy) внутри движка hope.

Зачем: у Freqtrade самая большая библиотека публичных стратегий, но его dry-run требует REST Bybit
(гео-блокируется из ряда стран), а бэктест — загрузки истории через REST. Этот адаптер берёт файл
стратегии Freqtrade без изменений и исполняет его в hope: свечи строятся из публичного потока
(live) или из архива сделок (бэктест), сигналы считает сам класс стратегии, выходы (ROI, стоп-лосс,
trailing, custom_stoploss, custom_exit, exit-сигнал) — штатный `IStrategy.should_exit` Freqtrade
поверх `LocalTrade`. Ордера рыночные (как в большинстве community-стратегий), исполняет paper-брокер hope.

Что НЕ поддерживается (документировано в README ветки): лимитные входы/выходы по entry_pricing,
position_adjustment (DCA), informative-пары через self.dp.get_pair_dataframe, стоп-ордера на бирже,
protections кроме CooldownPeriod, unfilledtimeout (рыночные ордера исполняются сразу).
Для «настоящего» Freqtrade используйте adapters/freqtrade/docker-compose.yml + `hope ft-export`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ...engine.strategy import Context, Strategy
from ...types import Bbo, Candle, Fill, Order, OrderDone, Purpose, Side

log = logging.getLogger("hope.adapters.freqtrade")


def hope_to_ft_pair(symbol: str, quote: str = "USDT", category: str = "linear") -> str:
    base = symbol[: -len(quote)] if symbol.endswith(quote) else symbol
    return f"{base}/{quote}:{quote}" if category != "spot" else f"{base}/{quote}"


def ft_to_hope_symbol(pair: str) -> str:
    base, _, rest = pair.partition("/")
    quote = rest.split(":")[0]
    return f"{base}{quote}"


@dataclass(slots=True)
class PairState:
    pair: str
    trade: Any = None            # freqtrade LocalTrade
    pending_entry: int | None = None
    pending_exit: int | None = None
    pending_side: Side | None = None
    pending_tag: str = ""
    enter_long: bool = False
    enter_short: bool = False
    exit_long: bool = False
    exit_short: bool = False
    enter_tag: str = ""
    exit_tag: str = ""
    last_check: int = 0
    cooldown_until: int = 0
    n_candles: int = 0
    last_candle_ts: int = 0


class FreqtradeStrategy(Strategy):
    """Обёртка: params.strategy_path (файл .py) + params.class_name -> стратегия Freqtrade в hope."""

    name = "freqtrade"
    needs_trades = True
    needs_tickers = False

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {
            "strategy_path": "",          # путь к .py файлу стратегии Freqtrade
            "class_name": "",             # имя класса (по умолчанию — имя файла)
            "stake_amount_usd": 100.0,    # размер входа (stake_amount Freqtrade)
            "max_open_trades": 5,
            "fee": 0.00055,               # комиссия для расчёта профита LocalTrade (тейкер Bybit)
            "check_secs": 5,              # как часто оценивать выходы между свечами (process_throttle_secs)
            "leverage": 1.0,
            "cooldown_default_candles": 0,
            "ft_config": {},              # дополнительные ключи конфига Freqtrade (перекрытия)
        }

    # ------------------------------------------------------------------ загрузка
    def on_start(self, ctx: Context) -> None:
        from freqtrade.data.dataprovider import DataProvider
        from freqtrade.enums import CandleType, RunMode
        from freqtrade.resolvers import StrategyResolver

        path = Path(str(self.p("strategy_path")))
        if not path.exists():
            raise FileNotFoundError(f"файл стратегии Freqtrade не найден: {path}")
        class_name = str(self.p("class_name") or path.stem)
        self.category = ctx.params.get("_category", "linear")
        cfg_ex = getattr(ctx, "cfg", None)
        quote = "USDT"
        category = "linear"
        if cfg_ex is not None:
            quote = cfg_ex.exchange.quote_coin
            category = cfg_ex.exchange.category
        self.quote, self.category = quote, category
        self.pairs = {s: hope_to_ft_pair(s, quote, category) for s in ctx.symbols}
        self.by_pair = {p: s for s, p in self.pairs.items()}
        user_data = Path(cfg_ex.run.data_dir if cfg_ex else "data") / "freqtrade_user_data"
        (user_data / "strategies").mkdir(parents=True, exist_ok=True)
        self.candle_type = CandleType.FUTURES if category != "spot" else CandleType.SPOT
        ft_config: dict[str, Any] = {
            "strategy": class_name,
            "strategy_path": str(path.parent.resolve()),
            "user_data_dir": user_data,
            "stake_currency": quote,
            "stake_amount": float(self.p("stake_amount_usd")),
            "max_open_trades": int(self.p("max_open_trades")),
            "dry_run": True,
            "runmode": RunMode.BACKTEST if ctx.mode != "live" else RunMode.DRY_RUN,
            "trading_mode": "futures" if category != "spot" else "spot",
            "margin_mode": "isolated" if category != "spot" else "",
            "candle_type_def": self.candle_type,
            "exchange": {"name": "bybit", "pair_whitelist": list(self.pairs.values()), "pair_blacklist": []},
            "pairlists": [{"method": "StaticPairList"}],
            "internals": {"process_throttle_secs": int(self.p("check_secs"))},
        }
        ft_config.update(dict(self.p("ft_config") or {}))
        self.ft = StrategyResolver.load_strategy(ft_config)
        self.ft_config = ft_config
        self.dp = DataProvider(ft_config, None)
        self.ft.dp = self.dp
        self.ft.ft_bot_start()
        self.timeframe = self.ft.timeframe
        engine_tf = cfg_ex.strategy.timeframe if cfg_ex else None
        if engine_tf and engine_tf != self.timeframe:
            raise ValueError(
                f"strategy.timeframe в конфиге hope = {engine_tf}, а стратегия Freqtrade {class_name} требует {self.timeframe}"
            )
        self.tf_min = int(pd.Timedelta(self.timeframe).total_seconds() // 60)
        self.startup = int(getattr(self.ft, "startup_candle_count", 0) or 0)
        self.name = f"ft_{class_name}"
        self.st: dict[str, PairState] = {s: PairState(p) for s, p in self.pairs.items()}
        self._next_trade_id = 1
        self.cooldown_candles = self._cooldown_from_protections()
        ctx.event(
            f"Freqtrade {class_name}: tf={self.timeframe}, roi={self.ft.minimal_roi}, stoploss={self.ft.stoploss}, "
            f"trailing={self.ft.trailing_stop}, can_short={self.ft.can_short}, startup={self.startup}, cooldown={self.cooldown_candles} свечей"
        )

    def _cooldown_from_protections(self) -> int:
        prot = getattr(self.ft, "protections", None) or []
        n = int(self.p("cooldown_default_candles") or 0)
        for p in prot:
            if p.get("method") == "CooldownPeriod":
                if "stop_duration_candles" in p:
                    n = max(n, int(p["stop_duration_candles"]))
                elif "stop_duration" in p:
                    n = max(n, int(p["stop_duration"]) // max(self.tf_min, 1))
            else:
                log.warning("protection %s не поддерживается адаптером, игнорируется", p.get("method"))
        return n

    def on_stop(self, ctx: Context) -> None:
        ctx.cancel_all()

    # ------------------------------------------------------------------ свечи -> сигналы
    def _frame(self, ctx: Context, symbol: str) -> pd.DataFrame | None:
        series = ctx.candles(symbol)
        if len(series) == 0:
            return None
        df = series.to_frame()
        df = df.rename(columns={"ts_open": "ts"})
        out = pd.DataFrame(
            {
                "date": pd.to_datetime(df["ts"], unit="ms", utc=True),
                "open": df["open"].astype(float),
                "high": df["high"].astype(float),
                "low": df["low"].astype(float),
                "close": df["close"].astype(float),
                "volume": df["volume"].astype(float),
            }
        )
        return out

    def on_candle(self, ctx: Context, symbol: str, candle: Candle) -> None:
        ps = self.st[symbol]
        ps.n_candles += 1
        ps.last_candle_ts = candle.ts_close
        df = self._frame(ctx, symbol)
        if df is None or len(df) < max(self.startup, 30):
            return
        meta = {"pair": ps.pair}
        try:
            out = self.ft.analyze_ticker(df, meta)
        except Exception as e:  # noqa: BLE001
            ctx.event(f"ошибка индикаторов {ps.pair}: {type(e).__name__}: {e}", "error", symbol)
            return
        self.dp._set_cached_df(ps.pair, self.timeframe, out, self.candle_type)
        last = out.iloc[-1]
        ps.enter_long = bool(last.get("enter_long", 0) == 1)
        ps.enter_short = bool(self.ft.can_short and last.get("enter_short", 0) == 1)
        ps.exit_long = bool(last.get("exit_long", 0) == 1)
        ps.exit_short = bool(last.get("exit_short", 0) == 1)
        ps.enter_tag = str(last.get("enter_tag", "") or "")
        ps.exit_tag = str(last.get("exit_tag", "") or "")
        self._evaluate(ctx, symbol, candle.close, candle.ts_close, from_candle=True)

    def on_bbo(self, ctx: Context, symbol: str, bbo: Bbo) -> None:
        ps = self.st[symbol]
        if ps.trade is None or not bbo.valid:
            return
        if ctx.now - ps.last_check < int(self.p("check_secs")) * 1000:
            return
        self._evaluate(ctx, symbol, bbo.mid, ctx.now, from_candle=False)

    # ------------------------------------------------------------------ решения
    def _n_open(self) -> int:
        return sum(1 for s in self.st.values() if s.trade is not None or s.pending_entry is not None)

    def _evaluate(self, ctx: Context, symbol: str, rate: float, now_ms: int, from_candle: bool) -> None:
        ps = self.st[symbol]
        ps.last_check = now_ms
        now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        if ps.trade is not None:
            if ps.pending_exit is not None:
                return
            t = ps.trade
            enter = ps.enter_short if t.is_short else ps.enter_long
            exit_ = ps.exit_short if t.is_short else ps.exit_long
            try:
                exits = self.ft.should_exit(t, rate, now, enter=enter, exit_=exit_)
            except Exception as e:  # noqa: BLE001
                ctx.event(f"ошибка should_exit {ps.pair}: {type(e).__name__}: {e}", "error", symbol)
                return
            for ex in exits:
                if ex.exit_flag:
                    reason = ex.exit_reason or ex.exit_type.value
                    side = Side.BUY if t.is_short else Side.SELL
                    qty = abs(ctx.position(symbol).qty)
                    if qty <= 0:
                        ps.trade = None
                        return
                    o = ctx.place_market(symbol, side, qty, purpose=Purpose.EXIT, tag=reason[:40], reduce_only=True)
                    if o is not None:
                        ps.pending_exit = o.id
                        ps.pending_tag = reason
                    return
            return
        # нет позиции: вход только по закрытой свече
        if not from_candle or ps.pending_entry is not None:
            return
        if now_ms < ps.cooldown_until:
            return
        if self._n_open() >= int(self.p("max_open_trades")):
            return
        side: Side | None = None
        if ps.enter_long and not ps.exit_long:
            side = Side.BUY
        elif ps.enter_short and not ps.exit_short:
            side = Side.SELL
        if side is None:
            return
        stake = float(self.p("stake_amount_usd")) * float(self.p("leverage"))
        qty = ctx.qty_for_notional(symbol, stake, rate)
        if qty <= 0:
            return
        o = ctx.place_market(symbol, side, qty, purpose=Purpose.ENTRY, tag=(ps.enter_tag or "entry")[:40])
        if o is not None:
            ps.pending_entry = o.id
            ps.pending_side = side
            ps.pending_tag = ps.enter_tag

    # ------------------------------------------------------------------ исполнения
    def on_fill(self, ctx: Context, fill: Fill) -> None:
        ps = self.st.get(fill.symbol)
        if ps is None:
            return
        if fill.order_id == ps.pending_entry:
            self._open_trade(ctx, ps, fill)
        elif fill.order_id == ps.pending_exit:
            pos = ctx.position(fill.symbol).qty
            if abs(pos) < 1e-12 and ps.trade is not None:
                t = ps.trade
                try:
                    t.close(fill.price)
                except Exception:  # noqa: BLE001
                    pass
                ctx.metric("ft_trade_profit_ratio", float(t.calc_profit_ratio(fill.price)), fill.symbol)
                ctx.event(f"выход {ps.pair} {ps.pending_tag} по {fill.price:.6g}, профит {t.calc_profit_ratio(fill.price)*100:+.2f}%", "info", fill.symbol)
                ps.trade = None
                ps.pending_exit = None
                if self.cooldown_candles > 0:
                    ps.cooldown_until = fill.ts + self.cooldown_candles * self.tf_min * 60_000

    def _open_trade(self, ctx: Context, ps: PairState, fill: Fill) -> None:
        from freqtrade.enums import TradingMode
        from freqtrade.persistence import LocalTrade

        pos = ctx.position(fill.symbol)
        if ps.trade is not None:
            return
        qty = abs(pos.qty)
        if qty <= 0:
            return
        is_short = pos.qty < 0
        fee = float(self.p("fee"))
        t = LocalTrade(
            id=self._next_trade_id,
            pair=ps.pair,
            base_currency=ps.pair.split("/")[0],
            stake_currency=self.quote,
            open_rate=pos.avg_price,
            open_rate_requested=pos.avg_price,
            open_date=datetime.fromtimestamp(fill.ts / 1000, tz=timezone.utc),
            stake_amount=qty * pos.avg_price / float(self.p("leverage")),
            amount=qty,
            amount_requested=qty,
            fee_open=fee,
            fee_close=fee,
            is_open=True,
            enter_tag=ps.pending_tag or None,
            timeframe=self.tf_min,
            exchange="bybit",
            is_short=is_short,
            trading_mode=TradingMode.FUTURES if self.category != "spot" else TradingMode.SPOT,
            leverage=float(self.p("leverage")),
            orders=[],
        )
        self._next_trade_id += 1
        t.adjust_stop_loss(t.open_rate, self.ft.stoploss, initial=True)
        ps.trade = t
        ps.pending_entry = None
        ctx.event(f"вход {ps.pair} {'short' if is_short else 'long'} {qty:g} @ {pos.avg_price:.6g} tag={ps.pending_tag}", "info", fill.symbol)

    def on_order_done(self, ctx: Context, done: OrderDone) -> None:
        ps = self.st.get(done.symbol)
        if ps is None:
            return
        if done.order_id == ps.pending_entry and done.status != "filled":
            ps.pending_entry = None
        if done.order_id == ps.pending_exit and done.status != "filled":
            ps.pending_exit = None

"""Multi-subaccount orchestrator (live).

Per Setari BOT regula 2: la RESTART, istoric trade și equity = de la 0.
NU folosim state persistence (JSON load) — fiecare pornire e cycle nou cu
balance = pool_total, equity = equity_start.

Per regula 14: stdout line_buffering pentru Docker (logs vizibile imediat).
"""

from __future__ import annotations

import asyncio
import io as _io
import os
import sys as _sys
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

# Regula 14: line-buffering stdout/stderr pentru Docker logs live.
# `PYTHONUNBUFFERED=1` din Dockerfile e override-uit de TextIOWrapper default.
_sys.stdout = _io.TextIOWrapper(
    _sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
)
_sys.stderr = _io.TextIOWrapper(
    _sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
)

from vse_bot import telegram_bot as tg
from vse_bot.bot_state import BotState, TradeRecord
from vse_bot.config import AppConfig, SubaccountConfig, load_config
from vse_bot.cycle_manager import (
    SubaccountState,
    on_trade_closed,
    restart_cycle_after_success,
)
from vse_bot.event_log import log_event
from vse_bot.exchange.bybit_client import BybitClient, MarketInfo
from vse_bot.exchange.bybit_private_ws import BybitPrivateWS
from vse_bot.exchange.bybit_ws import BybitKlineWS
from vse_bot.trade_lifecycle import (
    LivePosition,
    close_position_market,
    open_trade_live,
    update_trailing_stop,
)
from vse_bot.vse_signal_live import VSESignalLive


class SubaccountRunner:
    """O instanță per subaccount. Coordonează semnale → ordine → cycle.

    Două state separate (per design):
      - ``state`` (SubaccountState): cycle-pure (equity, balance, pool_used).
        Folosit pentru sizing & cycle SUCCESS detection. Calculat local.
      - ``bot`` (BotState): account real Bybit + trades + equity curve UI.
        Folosit pentru chart & telegram. ``bot.account += real_pnl`` (regula 5).
    """

    def __init__(
        self,
        sub_cfg: SubaccountConfig,
        cfg: AppConfig,
        client: BybitClient,
    ) -> None:
        self.sub_cfg = sub_cfg
        self.cfg = cfg
        self.client = client
        # Cycle-logic state (cycle SUCCESS, sizing)
        self.state: SubaccountState = SubaccountState.fresh(cfg.strategy)
        # UI/account real state (regula 5: account += real PnL Bybit)
        self.bot: BotState = BotState(
            initial_account=cfg.strategy.pool_total,
            account=cfg.strategy.pool_total,
        )

        self.signals: dict[tuple[str, str], VSESignalLive] = {}
        self.positions: dict[str, LivePosition | None] = {}
        self.market_info: dict[str, MarketInfo] = {}
        self.last_signal_dir: dict[tuple[str, str], int] = {}

        # Chart UI state (per regula 4+8+10+13)
        self.clients: set = set()                        # WebSocket clients
        self.candles_live: list[list] = []               # [[ts_s, o, h, l, c], ...]
                                                          # DOAR bare LIVE (după pornire)
                                                          # — afișate pe primary pair

        # Operational flag — manual pause via /api/pause endpoint
        self.paused: bool = False

    def primary_pair_key(self) -> tuple[str, str] | None:
        """Returnează (symbol, timeframe) al primei perechi (pt chart)."""
        if not self.sub_cfg.pairs:
            return None
        p = self.sub_cfg.pairs[0]
        return (p.symbol, p.timeframe)

    def active_position_payload(self) -> dict | None:
        """Pentru chart /api/init — poziția activă pe primary pair (dacă există)."""
        primary = self.primary_pair_key()
        if not primary:
            return None
        pos = self.positions.get(primary[0])
        if pos is None:
            return None
        return {
            "symbol": pos.symbol,
            "direction": "LONG" if pos.side == "long" else "SHORT",
            "entry": pos.entry_price,
            "sl": pos.sl_price,
            "tp": 0,    # VSE n-are TP, e trailing-only
            "qty": pos.qty,
            "risk_usd": pos.risk_usd,
            "entry_ms": int(pos.opened_ts.timestamp() * 1000),
        }

    # ── Setup ────────────────────────────────────────────────────────────
    async def setup(self) -> None:
        """State FRESH (regula 2: restart = de la 0). Configurează leverage,
        warmup signal generators. Apoi RECONCILE cu Bybit — dacă găsim
        poziții sau ordine reziduale → PAUSED + alert.
        """
        self.state = SubaccountState.fresh(self.cfg.strategy)
        log_event(
            self.cfg.operational.log_dir, self.sub_cfg.name, "BOOT",
            balance=self.state.balance_broker, equity=self.state.equity,
            cycle=self.state.cycle_num,
            note="FRESH state (no persistence per regula 2)",
        )

        for pair in self.sub_cfg.pairs:
            mi = await self.client.fetch_market_info(pair.symbol)
            self.market_info[pair.symbol] = mi
            await self.client.set_isolated_margin(pair.symbol, self.cfg.strategy.leverage)
            await self.client.set_leverage(pair.symbol, self.cfg.strategy.leverage)

            sig = VSESignalLive(
                strategy_cfg=self.cfg.strategy,
                indicator_cfg=self.cfg.indicator,
                symbol=pair.symbol,
                timeframe=pair.timeframe,
            )
            # Warmup cu 400 bare istorice. fetch_ohlcv include bara CURENTĂ ÎN
            # FORMARE (Bybit V5 default). filter_closed_bars o elimină → buffer
            # primește DOAR bare confirmed → fără duplicate când WS livrează
            # ulterior aceeași bară confirmed.
            from vse_bot.no_lookahead import filter_closed_bars
            ohlcv = await self.client.fetch_ohlcv(pair.symbol, pair.timeframe, limit=400)
            ohlcv = filter_closed_bars(ohlcv, pair.timeframe)
            df = _ohlcv_list_to_df(ohlcv)
            if not df.empty:
                sig.warm_up(df)
            self.signals[(pair.symbol, pair.timeframe)] = sig
            self.positions[pair.symbol] = None
            self.last_signal_dir[(pair.symbol, pair.timeframe)] = 0

        # Telegram READY notice (după warmup, înainte de reconcile)
        pairs_str = ", ".join(f"{p.symbol} {p.timeframe}" for p in self.sub_cfg.pairs)
        await tg.send(
            "✅ STRATEGY READY",
            f"Subaccount: <code>{self.sub_cfg.name}</code>\n"
            f"Pairs: <code>{pairs_str}</code>\n"
            f"Indicators warmed (400 bare/pereche)\n"
            f"SL bounds: {self.cfg.strategy.sl_min_pct * 100:.2f}% — "
            f"{self.cfg.strategy.sl_max_pct * 100:.2f}%\n"
            f"Equity init: ${self.cfg.strategy.equity_start:,.2f}"
        )

        # ── RECONCILE — verifică Bybit pentru rezidue ────────────────────
        await self._reconcile_with_bybit()

    async def _reconcile_with_bybit(self) -> None:
        """Detectează poziții / ordine reziduale pe Bybit.

        Reguli:
          - Position fără SL → CRITIC + PAUSED.
          - Position + SL (state mismatch local) → PAUSED (manual review).
          - NO position + ordine deschise (orphan SL/limit) → AUTO-CLEANUP
            (cancel orders + continue), NU pause. Cazul tipic: SL trigger sau
            OPP close — poziția e închisă, dar SL-ul/order-ul rămâne uneori
            "open" pe Bybit. Safe să cancel.
        """
        residue: list[str] = []
        for pair in self.sub_cfg.pairs:
            sym = pair.symbol
            try:
                pos = await self.client.fetch_position(sym)
                orders = await self.client.fetch_open_orders(sym)
            except Exception as e:
                residue.append(f"{sym}: fetch failed ({e!r})")
                continue

            has_position = pos is not None and float(pos.get("contracts") or 0) > 0
            sl_orders = [o for o in orders if o.get("type") in
                         ("stop_market", "stop", "stop_loss")
                         or (o.get("info", {}).get("stopOrderType") in
                             ("Stop", "TakeProfit", "StopLoss"))]
            other_orders = [o for o in orders if o not in sl_orders]

            if has_position and not sl_orders:
                residue.append(
                    f"{sym}: ⚠️  POSITION FĂRĂ SL — "
                    f"qty={pos.get('contracts')} avg={pos.get('entryPrice')}"
                )
            elif has_position:
                residue.append(
                    f"{sym}: position={pos.get('contracts')} @ {pos.get('entryPrice')}, "
                    f"sl_orders={len(sl_orders)}, other={len(other_orders)}"
                )
            elif sl_orders or other_orders:
                # AUTO-CLEANUP: orphan orders fără poziție corespondentă —
                # cancel toate (safe: nu mai există nimic de protejat).
                cancelled = 0
                failed = 0
                for o in sl_orders + other_orders:
                    try:
                        await self.client.cancel_order(sym, o["id"])
                        cancelled += 1
                    except Exception as e:
                        failed += 1
                        print(f"  [RECONCILE] cancel orphan {sym} {o.get('id')[:8]} fail: {e}")
                print(
                    f"  [RECONCILE] {sym}: orphan orders auto-cleanup — "
                    f"cancelled={cancelled} failed={failed} (NO position, safe)"
                )
                log_event(
                    self.cfg.operational.log_dir, self.sub_cfg.name,
                    "RECONCILE_ORPHAN_CLEANED",
                    symbol=sym, cancelled=cancelled, failed=failed,
                )
                # NB: nu adăugăm la residue → NU pause

        if residue:
            self.paused = True
            details = "\n".join(f"  - {r}" for r in residue)
            print(
                f"  [RECONCILE] ⚠️  REZIDUE Bybit detectat — bot PAUSED\n{details}\n"
                f"  Trigger /api/resume DOAR după manual review."
            )
            log_event(
                self.cfg.operational.log_dir, self.sub_cfg.name, "RECONCILE_PAUSED",
                residue=residue,
            )
            await tg.send_critical(
                "RECONCILE PAUSED",
                f"Subaccount: <code>{self.sub_cfg.name}</code>\n"
                f"Reziduri Bybit detectate la pornire:\n<pre>{details}</pre>\n"
                f"Bot PAUZAT. Verifică manual și trimite /api/resume."
            )
        else:
            log_event(
                self.cfg.operational.log_dir, self.sub_cfg.name, "RECONCILE_OK",
                note="No positions or open orders on Bybit",
            )

    # ── Bar handling ─────────────────────────────────────────────────────
    async def on_bar(self, bar: dict[str, Any]) -> None:
        """Apelat pe fiecare tick WS Bybit (bare ÎN FORMARE + bare CONFIRMED).

        Real-time chart: bara curentă (confirmed=False) e broadcast la chart
        pentru update vizual tick-by-tick. Procesarea de strategy (signal,
        cycle, trade) se face DOAR pe bare CONFIRMED.
        """
        key = (bar["symbol"], bar["timeframe"])
        if key not in self.signals:
            return

        ts_s = int(bar["ts_ms"]) // 1000
        confirmed = bool(bar.get("confirmed"))

        # Chart broadcast — DOAR pe primary pair (regula 8+10).
        # candles_live persistă AMBELE: bare confirmed + bara curentă în formare.
        # La refresh /api/init, chart vede întreg istoricul live (NU pierde
        # bara curentă). Replace tail dacă timestamp identic (update tick),
        # altfel append nou.
        if key == self.primary_pair_key():
            from vse_bot.chart_server import broadcast as _bc
            # Auto-precision: ~5 cifre semnificative în funcție de magnitudine.
            # Ex: 2300 → 2 zec, 95 → 3 zec, 0.07 → 6 zec. Aliniat cu fmtPx() chart.
            import math as _math
            ref = bar["close"] if bar.get("close") else 0
            if ref > 0:
                mag = _math.floor(_math.log10(ref))
                _prec = max(2, min(8, 4 - mag))
            else:
                _prec = 6
            candle_arr = [
                ts_s,
                round(bar["open"], _prec),
                round(bar["high"], _prec),
                round(bar["low"], _prec),
                round(bar["close"], _prec),
            ]
            if self.candles_live and self.candles_live[-1][0] == ts_s:
                # Update bara curentă (acelaș timestamp = tick update)
                self.candles_live[-1] = candle_arr
            else:
                # Bară nouă (prima sau timestamp diferit)
                self.candles_live.append(candle_arr)
            if confirmed:
                self.bot.mark_first_candle(ts_s)
            if len(self.candles_live) > 20000:
                self.candles_live.pop(0)
            await _bc(self, {
                "type": "candle",
                "confirmed": confirmed,
                "data": {
                    "time": ts_s,
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                },
            })

        # Strategy processing DOAR pe bare CONFIRMED (signal, cycle, trade).
        if not confirmed:
            return
        if self.paused:
            return

        sig_engine = self.signals[key]
        ts = pd.Timestamp(bar["ts_ms"], unit="ms", tz="UTC")
        candle = {
            "ts": ts,
            "open": bar["open"], "high": bar["high"],
            "low": bar["low"], "close": bar["close"],
            "volume": bar.get("volume", 0.0),
        }

        # 1. Pe pozițiile deschise pentru acest pair:
        #    a) execute OPP exit planificat (din bara anterioară) la NEXT bar open
        #    b) update trailing stop SuperTrend
        #    c) detectează OPP signal pe bara curentă → planifică exit
        pos = self.positions.get(bar["symbol"])
        if pos is not None:
            # 1a. Execute planned OPP exit (signaled la close anterioară)
            if pos.opp_exit_planned:
                result = await close_position_market(
                    client=self.client, pos=pos, reason="opp"
                )
                # Reconciliere PnL real se face în on_trade_filled_close prin
                # WS execution events; aici doar marchem intent.
                log_event(
                    self.cfg.operational.log_dir, self.sub_cfg.name,
                    "OPP_EXIT_EXECUTED",
                    symbol=pos.symbol, side=pos.side,
                    bar_open=bar["open"],
                )
                # Pos rămâne în self.positions până la confirm WS — but mark
                self.positions[pos.symbol] = None
                # Cooldown clock
                for (sym, _tf), eng in self.signals.items():
                    if sym == pos.symbol:
                        eng.mark_position_closed(ts)
                # Continuă spre Phase 2 (poate fi nou signal pe aceeași bară —
                # mode "pure" cooldown 3 bars va respinge oricum).
                pos = None

        if pos is not None:
            # 1b. Update buffer & trailing
            sig_engine.update(candle)   # ignorăm signal-ul (poziție deja open)
            st_pair = sig_engine.latest_supertrend()
            if st_pair is not None:
                long_stop, short_stop = st_pair
                target_stop = long_stop if pos.side == "long" else short_stop
                changed = await update_trailing_stop(
                    client=self.client, pos=pos, new_stop=target_stop
                )
                if changed:
                    log_event(
                        self.cfg.operational.log_dir, self.sub_cfg.name,
                        "TRAILING_UPDATE",
                        symbol=pos.symbol, new_stop=pos.sl_price,
                    )

            # 1c. Detectează OPP signal — planifică exit la NEXT bar open
            raw_signals = sig_engine.latest_raw_signals()
            if raw_signals is not None:
                raw_long, raw_short = raw_signals
                opp_detected = (
                    (pos.side == "long" and raw_short)
                    or (pos.side == "short" and raw_long)
                )
                if opp_detected and not pos.opp_exit_planned:
                    pos.opp_exit_planned = True
                    log_event(
                        self.cfg.operational.log_dir, self.sub_cfg.name,
                        "OPP_EXIT_PLANNED",
                        symbol=pos.symbol, side=pos.side,
                        ts=str(ts),
                    )
            return

        # 2. Fără poziție: încercăm signal pe bara curentă
        log_event(
            self.cfg.operational.log_dir, self.sub_cfg.name, "BAR_RECEIVED",
            symbol=bar["symbol"], tf=bar["timeframe"],
            ts=str(ts), close=bar["close"],
        )
        signal = sig_engine.update(candle)
        if signal is None:
            self.last_signal_dir[key] = 0
            # FILTER_REJECTED: log motivul (sl out of bounds, cooldown, etc.)
            raw = sig_engine.latest_raw_signals()
            if raw and (raw[0] or raw[1]):
                log_event(
                    self.cfg.operational.log_dir, self.sub_cfg.name,
                    "FILTER_REJECTED",
                    symbol=bar["symbol"],
                    raw_long=raw[0], raw_short=raw[1],
                    reason="sl_bounds_or_cooldown_or_st_nan",
                )
            return

        log_event(
            self.cfg.operational.log_dir, self.sub_cfg.name, "SIGNAL_EVALUATED",
            symbol=bar["symbol"], side=signal.side,
            entry_price=signal.entry_price,
            sl_price=signal.sl_price, sl_pct=signal.sl_pct,
        )

        log_event(
            self.cfg.operational.log_dir, self.sub_cfg.name, "SIGNAL_DETECTED",
            symbol=bar["symbol"], side=signal.side, sl_pct=signal.sl_pct,
            entry_price=signal.entry_price, sl_price=signal.sl_price,
            ts=str(signal.ts),
        )
        self.last_signal_dir[key] = 1 if signal.side == "long" else -1

        mi = self.market_info[bar["symbol"]]
        used_margin = sum(
            (p.pos_usd / self.cfg.strategy.leverage)
            for p in self.positions.values() if p is not None
        )
        new_pos = await open_trade_live(
            client=self.client,
            signal=signal,
            symbol=bar["symbol"],
            state=self.state,
            cfg=self.cfg.strategy,
            qty_step=mi.qty_step,
            qty_min=mi.qty_min,
            used_margin_other=used_margin,
            notify=tg.send,
            notify_critical=tg.send_critical,
        )
        if new_pos is not None:
            self.positions[bar["symbol"]] = new_pos
            log_event(
                self.cfg.operational.log_dir, self.sub_cfg.name, "TRADE_OPENED",
                symbol=new_pos.symbol, side=new_pos.side, qty=new_pos.qty,
                pos_usd=new_pos.pos_usd, sl=new_pos.sl_price,
            )
            # Telegram — pattern aliniat cu boilerplate example_strategy.py:197
            direction = new_pos.side.upper()
            dir_icon = "🚀" if new_pos.side == "long" else "📉"
            await tg.send(
                f"{dir_icon} ENTRY {direction}",
                f"<b>Strategy:</b> <code>VSE_Nou1</code>\n"
                f"<b>Symbol:</b>   <code>{new_pos.symbol}</code>\n"
                f"Entry:    {new_pos.entry_price:.6f}\n"
                f"SL:       {new_pos.sl_price:.6f}  ({signal.sl_pct * 100:.3f}%)\n"
                f"Qty:      {new_pos.qty}\n"
                f"Notional: ${new_pos.pos_usd:,.2f}\n"
                f"Risk:     ${new_pos.risk_usd:.2f}\n"
                f"Account:  ${self.bot.account:,.2f}"
            )
            # Chart broadcast — pattern aliniat cu boilerplate set_active_position
            # (main.py:162). Doar primary pair desenează liniile (chart afișează
            # candele primary, SL line cu prețul secondary ar fi misleading).
            if key == self.primary_pair_key():
                from vse_bot.chart_server import broadcast as _bc
                await _bc(self, {
                    "type": "position_open",
                    "symbol": new_pos.symbol,
                    "direction": "LONG" if new_pos.side == "long" else "SHORT",
                    "entry": new_pos.entry_price,
                    "sl": new_pos.sl_price,
                    "tp": 0,
                    "qty": new_pos.qty,
                    "risk_usd": new_pos.risk_usd,
                    # Chart cere entry_ms ca să deseneze liniile bounded entry→current.
                    "entry_ms": int(new_pos.opened_ts.timestamp() * 1000),
                })

    # ── Position event handler (Bybit private WS) ─────────────────────────
    async def on_bybit_position_event(self, event: dict[str, Any]) -> None:
        """Bybit position update — detectează close (size=0) și trage PnL real.

        Spec regula 3 + 5: PnL nu se calculează local; se trage de pe Bybit
        prin ``fetch_pnl_for_trade`` după ce poziția se confirmă închisă.
        """
        symbol = event.get("symbol", "")
        size = float(event.get("size") or 0)
        # Map Bybit symbol → our internal (Bybit returnează "KAIAUSDT" raw)
        pos = self.positions.get(symbol)
        if pos is None:
            return
        if size > 0:
            # poziția încă deschisă — ignore
            return

        # Position size = 0 → trade closed pe Bybit. Trage PnL real.
        entry_ts_ms = int(pos.opened_ts.timestamp() * 1000)
        exit_ts_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
        pnl_data = await self.client.fetch_pnl_for_trade(
            symbol, entry_ts_ms, exit_ts_ms
        )
        pnl_real = pnl_data["pnl"]
        fees_real = pnl_data["fees"]
        avg_exit = pnl_data["avg_exit"] or pos.sl_price

        # Reason heuristic: opp dacă era planificat, altfel TS / MANUAL
        if pos.opp_exit_planned:
            reason = "OPP"
        elif pos.sl_price and abs(avg_exit - pos.sl_price) / max(pos.sl_price, 1e-9) < 0.005:
            reason = "TS"
        else:
            reason = "MANUAL"

        await self._on_trade_closed_finalize(
            symbol=symbol, pos=pos,
            exit_price=avg_exit, pnl_net=pnl_real, fees=fees_real,
            reason=reason,
        )

    async def _on_trade_closed_finalize(
        self,
        *,
        symbol: str,
        pos: LivePosition,
        exit_price: float,
        pnl_net: float,
        fees: float,
        reason: str,
    ) -> None:
        """Finalizare close: log, trade record, update state + bot.account."""
        ts_now = pd.Timestamp.utcnow().tz_localize("UTC")
        entry_ts_ms = int(pos.opened_ts.timestamp() * 1000)
        exit_ts_ms = int(ts_now.timestamp() * 1000)

        # Adaugă în BotState (regula 5: account += pnl real)
        trade_rec = TradeRecord(
            id=0,   # set de add_closed_trade
            date=ts_now.strftime("%Y-%m-%d"),
            direction="LONG" if pos.side == "long" else "SHORT",
            symbol=symbol,
            entry_ts_ms=entry_ts_ms,
            entry_price=pos.entry_price,
            sl_price=pos.sl_price,
            tp_price=None,
            qty=pos.qty,
            exit_ts_ms=exit_ts_ms,
            exit_price=exit_price,
            exit_reason=reason,
            pnl=pnl_net,
            fees=fees,
            extra={"sl_initial": pos.sl_initial, "pos_usd": pos.pos_usd},
        )
        self.bot.add_closed_trade(trade_rec)

        log_event(
            self.cfg.operational.log_dir, self.sub_cfg.name, "TRADE_CLOSED",
            symbol=symbol, side=pos.side, qty=pos.qty,
            entry=pos.entry_price, exit=exit_price,
            pnl_net=pnl_net, fees=fees, reason=reason,
            account_after=self.bot.account,
        )

        # Telegram alert — pattern aliniat cu boilerplate main.py:427
        sign = "📈" if pnl_net >= 0 else "📉"
        prec = int(os.getenv("PRICE_PRECISION", "6"))
        await tg.send(
            f"{sign} TRADE INCHIS — {pos.side.upper()} {symbol}",
            f"<b>Strategy:</b> <code>VSE_Nou1</code>\n"
            f"Exit: {exit_price:,.{prec}f}  ({reason})\n"
            f"PnL: <b>${pnl_net:+,.2f}</b>  (Bybit real, fees incluse)\n"
            f"Account: ${self.bot.account:,.2f}  |  "
            f"Return: {(self.bot.account/self.bot.initial_account - 1)*100:+.2f}%"
        )

        # Chart broadcast: trade closed + position cleared
        from vse_bot.chart_server import broadcast as _bc
        await _bc(self, {
            "type": "trade_closed",
            "trade": trade_rec.to_dict(),
            "equity": self.bot.account,
            "summary": self.bot.summary(),
        })
        await _bc(self, {"type": "position_close"})

        # Cleanup positions + cooldown
        self.positions[symbol] = None
        for (sym, _tf), eng in self.signals.items():
            if sym == symbol:
                eng.mark_position_closed(ts_now)

        # Cycle logic — folosește pnl_real (NU calc local)
        ev = on_trade_closed(self.state, pnl_net, self.cfg.strategy)

        if ev == "SUCCESS":
            await self._handle_cycle_success()
        elif ev == "RESET":
            log_event(
                self.cfg.operational.log_dir, self.sub_cfg.name, "RESET",
                cycle=self.state.cycle_num, reset_count=self.state.reset_count,
                pool_used=self.state.pool_used, equity=self.state.equity,
            )
        elif ev == "POOL_LOW":
            log_event(
                self.cfg.operational.log_dir, self.sub_cfg.name, "POOL_LOW",
                balance=self.state.balance_broker,
            )

    async def _handle_cycle_success(self) -> None:
        # Close ALL open positions (force market exit)
        for sym, pos in list(self.positions.items()):
            if pos is not None:
                await close_position_market(
                    client=self.client, pos=pos, reason="cycle_success"
                )
                self.positions[sym] = None

        withdraw = restart_cycle_after_success(self.state, self.cfg.strategy)
        log_event(
            self.cfg.operational.log_dir, self.sub_cfg.name, "CYCLE_SUCCESS",
            cycle_num_completed=self.state.cycle_num - 1,
            withdraw_amount=withdraw,
            balance=self.state.balance_broker,
        )
        # NB: state-ul intern e IZOLAT de balance-ul real Bybit (cap-ul
        # pe pos folosește fetch_balance_usdt la fiecare entry). Bot continuă
        # tradingul cu cycle nou (equity=$50), iar transferul către master
        # rămâne decizia operațională user (manual UI Bybit).
        await tg.send(
            "🎉 CYCLE SUCCESS",
            f"Subaccount: <code>{self.sub_cfg.name}</code>\n"
            f"Withdraw disponibil: <b>${withdraw:,.2f}</b>\n"
            f"Cycle #{self.state.cycle_num - 1} închis. Cycle #{self.state.cycle_num} începe.\n\n"
            f"<i>Recomandare: transferă ${withdraw:,.2f} din subaccount → master "
            f"(UI Bybit) când e convenabil. Bot continuă tradingul oricum.</i>"
        )


# ── Helpers ──────────────────────────────────────────────────────────────
def _ohlcv_list_to_df(ohlcv: list[list[float]]) -> pd.DataFrame:
    if not ohlcv:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(ohlcv, columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.set_index("ts").drop(columns=["ts_ms"])
    return df


# ── Top-level ────────────────────────────────────────────────────────────
async def run_live(config_path: str = "config/config.yaml") -> None:
    """Pornește bot LIVE pentru UN SINGUR subaccount.

    Env vars cheie:
      SUBACCOUNT_NAME  — nume din config (ex ``subacc_1_kaia_aave``).
                         Default: primul enabled din config.yaml.
      TRADING_MODE     — testnet | live.
      <PREFIX>_API_KEY / _API_SECRET — credentials (PREFIX e configurat per subaccount).
      BOT_NAME         — pentru Telegram + chart header.
      TELEGRAM_TOKEN, TELEGRAM_CHAT_ID — opțional.

    Per regula utilizator: 2 procese separate cu Docker compose, fiecare
    rulează acest script cu SUBACCOUNT_NAME diferit + chart pe port unic.
    """
    load_dotenv()
    cfg = load_config(config_path)
    cfg.operational.log_dir.mkdir(parents=True, exist_ok=True)

    testnet = os.getenv("TRADING_MODE", "testnet").lower() != "live"

    # Selectează subaccount-ul pentru acest proces
    sub_name = os.getenv("SUBACCOUNT_NAME", "")
    target_sub = None
    for sub in cfg.subaccounts:
        if not sub.enabled:
            continue
        if sub_name == sub.name or (not sub_name and target_sub is None):
            target_sub = sub
            if sub_name:
                break
    if target_sub is None:
        raise SystemExit(f"Subaccount '{sub_name}' nu există în config")

    # Credentials generice (least privilege): fiecare container primește
    # DOAR cheile subaccount-ului lui. Compose files mapează SUB1/SUB2 → BYBIT_*.
    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")
    if not api_key or not api_secret:
        raise SystemExit(
            f"Lipsește BYBIT_API_KEY / BYBIT_API_SECRET pentru {target_sub.name}. "
            f"Verifică env vars din container."
        )

    print(f"\n{'=' * 70}")
    print(f"  VSE BOT — {target_sub.name}")
    if testnet:
        print(f"  mode: testnet (paper trading)")
    else:
        print(f"  mode: 🔴 MAINNET — REAL MONEY on Bybit")
    print(f"  pairs: {[(p.symbol, p.timeframe) for p in target_sub.pairs]}")
    print(f"  withdraw_target: ${cfg.strategy.withdraw_target:,.0f}  "
          f"opp_exit_mode: {cfg.strategy.opp_exit_mode}")
    print(f"{'=' * 70}\n")

    client = await BybitClient.create(api_key, api_secret, testnet=testnet)
    try:
        runner = SubaccountRunner(sub_cfg=target_sub, cfg=cfg, client=client)
        await runner.setup()
    except Exception as e:
        # Asigură close pe client dacă setup eșuează (evită "Unclosed client session")
        print(f"  [SETUP FAILED] {e!r} — closing client and exit")
        await client.close()
        raise

    # Public WS — kline (multi-symbol pentru perechile subaccount-ului)
    subscriptions = [(p.symbol, p.timeframe) for p in target_sub.pairs]

    async def on_bar(bar: dict) -> None:
        await runner.on_bar(bar)

    async def on_stale(symbol: str, tf: str, age_sec: float) -> None:
        log_event(
            cfg.operational.log_dir, target_sub.name, "WS_STALE",
            symbol=symbol, tf=tf, age_sec=round(age_sec, 0),
        )
        await tg.send(
            "⚠️ WS STALE — auto-reconnect",
            f"Subaccount: <code>{target_sub.name}</code>\n"
            f"Symbol: <code>{symbol} {tf}</code>\n"
            f"Age: {age_sec:.0f}s fără bară confirmed.\n"
            f"Bot face auto-reconnect — verifică următoarele bare."
        )

    public_ws = BybitKlineWS(
        subscriptions, on_bar, testnet=testnet, on_stale=on_stale
    )

    # Private WS — order/execution/position events
    private_ws = BybitPrivateWS(
        api_key=api_key,
        api_secret=api_secret,
        testnet=testnet,
        on_position=runner.on_bybit_position_event,
        log_prefix=f"ws-priv {target_sub.name}",
    )

    # Chart FastAPI server (regula 4 + 8)
    from vse_bot.chart_server import create_app, serve_chart
    chart_port = int(os.getenv("CHART_PORT", "8101"))
    app = create_app(runner)
    print(f"  [chart] http://0.0.0.0:{chart_port}/  (TZ: Europe/Bucharest)")

    # Telegram boot notice
    mode_label = "testnet (paper)" if testnet else "🔴 <b>MAINNET — REAL MONEY</b>"
    await tg.send(
        "BOT STARTED ✅",
        f"Strategy: <code>VSE_Nou1</code>\n"
        f"Subaccount: <code>{target_sub.name}</code>\n"
        f"Mode: {mode_label}\n"
        f"Account init: ${cfg.strategy.pool_total:,.2f}\n"
        f"Pairs: {', '.join(p.symbol for p in target_sub.pairs)}\n"
        f"Chart: port {chart_port}"
    )

    try:
        await asyncio.gather(
            public_ws.run(),
            private_ws.run(),
            serve_chart(app, chart_port),
        )
    finally:
        try:
            n_trades = len(runner.bot.trades)
            ret_pct = (runner.bot.account / runner.bot.initial_account - 1) * 100
            await tg.send(
                "🛑 BOT STOPPED",
                f"Subaccount: <code>{target_sub.name}</code>\n"
                f"Account: ${runner.bot.account:,.2f}  |  Return: {ret_pct:+.2f}%\n"
                f"Trades: {n_trades}"
            )
        except Exception as e:
            print(f"  [SHUTDOWN] tg.send failed: {e}")
        await client.close()


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.yaml")
    args = ap.parse_args()
    asyncio.run(run_live(args.config))


if __name__ == "__main__":
    main()

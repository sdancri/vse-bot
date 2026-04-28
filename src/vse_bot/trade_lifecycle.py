"""Trade lifecycle live (open / trail / close) — peste un client Bybit-ccxt.

Spec source: STRATEGY_LOGIC.md secțiunea 5 (trailing) + 7 (lifecycle).

Workflow:
  1. ``open_trade_live(client, signal, state, cfg, symbol_meta)``:
     - calculează size, verifică margin, deschide market entry,
     - plasează SL stop-market reduce-only.
     - returnează ``LivePosition`` cu ID-urile ordinelor.

  2. ``update_trailing_stop(client, pos, new_stop)``:
     - dacă noul stop e MAI BUN (sus pe long, jos pe short), modifică ordinul SL.
     - altfel no-op.

  3. ``close_position_market(client, pos, reason)``:
     - market close (dacă SL n-a tras încă).
     - return-ul include exit_price aproximativ; PnL real se trage din
       ``client.fetch_realized_pnl`` după ce Bybit înregistrează evenimentul.

Notă: nu testăm aici WS execution events (deja sunt prin ``BybitClient`` în
main.py). Funcțiile de aici sunt sincron-async coordonator-side, nu fac polling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from vse_bot.config import StrategyConfig
from vse_bot.cycle_manager import SubaccountState
from vse_bot.sizing import compute_qty
from vse_bot.vse_signal_live import LiveSignal

if TYPE_CHECKING:
    from vse_bot.exchange.bybit_client import BybitClient


@dataclass
class LivePosition:
    symbol: str
    side: str                  # "long" | "short"
    qty: float
    entry_price: float
    sl_price: float
    sl_initial: float
    pos_usd: float
    risk_usd: float
    opened_ts: datetime
    order_entry_id: str
    order_sl_id: str
    extra: dict[str, Any]
    # OPP exit planning — set la TRUE când raw opposite signal apare pe bara
    # curentă; exit-ul se execută la NEXT bar open (per spec STRATEGY_LOGIC sec 6).
    opp_exit_planned: bool = False


async def open_trade_live(
    *,
    client: "BybitClient",
    signal: LiveSignal,
    symbol: str,
    state: SubaccountState,
    cfg: StrategyConfig,
    qty_step: float,
    qty_min: float,
    used_margin_other: float = 0.0,
    notify: "Callable[[str, str], Awaitable[None]] | None" = None,
) -> LivePosition | None:
    """Plasează entry market + SL stop-market reduce-only.

    Sizing logic (per spec utilizator):
      1. ``risk_usd = 0.20 × state.equity`` (state.equity = $50 + compound REAL Bybit).
      2. ``pos_internal = risk_usd / sl_pct`` (formula bot internă).
      3. **Query BALANȚĂ Bybit REALĂ** înainte de trade.
      4. ``max_bybit = balance_real × leverage`` (max permis de Bybit, 100%).
         ``cap_value = 0.95 × max_bybit`` (5% sub max pt siguranță).
      5. **Dacă** ``pos_internal > max_bybit`` (peste ce permite Bybit) →
         ``pos_final = cap_value`` (intrăm cât permite Bybit -5%).
         **Altfel** → ``pos_final = pos_internal``.

    Returnează ``None`` doar dacă:
      - balance_real ≤ 0,
      - qty < min step Bybit,
      - Bybit reject la create_order.
    """
    # 1+2. Sizing intern (bot equity-based)
    risk_usd = cfg.risk_pct_equity * state.equity
    if signal.sl_pct <= 0:
        return None
    pos_internal = risk_usd / signal.sl_pct

    # 3. Query balance REALĂ Bybit
    balance_real = await client.fetch_balance_usdt()
    if balance_real <= 0:
        print(f"  [SIZING] balance_real=0 — skip {signal.side} {symbol}")
        return None

    # 4+5. Cap dacă pos depășește max-ul Bybit
    max_bybit = balance_real * cfg.leverage
    cap_value = cfg.cap_pct_of_max * max_bybit
    if pos_internal > max_bybit:
        pos_final = cap_value
        print(
            f"  [SIZING] CAP {signal.side} {symbol}: pos_internal=${pos_internal:.2f} "
            f"> max_bybit=${max_bybit:.2f} → pos=${pos_final:.2f} "
            f"(balance_real=${balance_real:.2f}, lev={cfg.leverage})"
        )
    else:
        pos_final = pos_internal

    qty = compute_qty(pos_final, signal.entry_price, qty_step)
    if qty < qty_min:
        print(
            f"  [SIZING] SKIP {signal.side} {symbol}: qty={qty} < min={qty_min}"
        )
        return None

    bybit_side = "buy" if signal.side == "long" else "sell"

    # 1. Market entry
    entry_order = await client.create_market_order(
        symbol=symbol,
        side=bybit_side,
        qty=qty,
        reduce_only=False,
    )

    # 1b. Așteaptă fill REAL (poll fetch_order, timeout 10s).
    # Cazuri tratate:
    #   - Filled total: avg_price + qty real
    #   - PartiallyFilled timeout: folosește qty filled real (mai mic)
    #   - Rejected: NO position (return None)
    #   - Timeout fără fill: cancel + NO position
    import asyncio
    actual_qty = 0.0
    actual_entry = signal.entry_price
    deadline = asyncio.get_event_loop().time() + 10.0
    last_status = ""
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)
        order_status = await client.fetch_order(symbol, entry_order["id"])
        if order_status is None:
            continue
        last_status = order_status.get("status") or order_status.get("info", {}).get("orderStatus", "")
        filled = float(order_status.get("filled") or 0)
        avg = order_status.get("average") or order_status.get("info", {}).get("avgPrice")
        if filled > 0:
            actual_qty = filled
            if avg:
                actual_entry = float(avg)
        if last_status in ("closed", "filled", "Filled"):
            break
        if last_status in ("rejected", "canceled", "Cancelled", "Rejected"):
            print(
                f"  [ORDER] {symbol} entry REJECTED status={last_status} — abort"
            )
            return None

    if actual_qty <= 0:
        # Timeout fără fill — cancel safety
        try:
            await client.cancel_order(symbol, entry_order["id"])
        except Exception:
            pass
        print(f"  [ORDER] {symbol} entry timeout 10s, status={last_status} — abort")
        return None

    if actual_qty < qty:
        print(
            f"  [ORDER] {symbol} PARTIAL FILL {actual_qty}/{qty} — "
            f"folosesc qty real, recalc SL pe entry={actual_entry}"
        )

    # 1c. Verifică SL bounds după real fill (slippage check)
    sl_dist = abs(actual_entry - signal.sl_price)
    actual_sl_pct = sl_dist / actual_entry if actual_entry > 0 else 0
    if actual_sl_pct < cfg.sl_min_pct or actual_sl_pct > cfg.sl_max_pct:
        # Slippage scoate SL din bounds → close imediat (round-trip pe Bybit)
        close_side = "sell" if signal.side == "long" else "buy"
        await client.create_market_order(
            symbol=symbol, side=close_side, qty=actual_qty, reduce_only=True
        )
        print(
            f"  [ORDER] {symbol} slippage out of bounds (sl_pct={actual_sl_pct:.4f}), "
            f"closed immediately"
        )
        if notify is not None:
            try:
                await notify(
                    f"⚠️ ENTRY ABORTED — slippage {symbol}",
                    f"Side: <code>{signal.side.upper()}</code>\n"
                    f"Entry fill: {actual_entry:.6f} (signal {signal.entry_price:.6f})\n"
                    f"SL pct after fill: {actual_sl_pct * 100:.3f}% "
                    f"(bounds {cfg.sl_min_pct * 100:.2f}–{cfg.sl_max_pct * 100:.2f}%)\n"
                    f"Bybit round-trip executat (open + close imediat)."
                )
            except Exception as _e:
                print(f"  [TG] notify failed: {_e}")
        return None

    # 2. Stop-market SL reduce-only (opposite side) cu qty REAL
    sl_side = "sell" if signal.side == "long" else "buy"
    try:
        sl_order = await client.create_stop_market(
            symbol=symbol,
            side=sl_side,
            qty=actual_qty,
            stop_price=signal.sl_price,
            reduce_only=True,
        )
    except Exception as e:
        # SL order eșuează → poziția deschisă pe Bybit FĂRĂ SL = critical.
        # Close imediat (market reduce-only) și alert critic.
        print(f"  [ORDER] {symbol} SL create FAILED: {e!r} — closing position")
        try:
            close_side = "sell" if signal.side == "long" else "buy"
            await client.create_market_order(
                symbol=symbol, side=close_side, qty=actual_qty, reduce_only=True
            )
        except Exception as e2:
            print(f"  [ORDER] {symbol} CRITICAL — close after SL fail also failed: {e2!r}")
        if notify is not None:
            try:
                await notify(
                    f"🚨 CRITICAL — SL ORDER FAILED {symbol}",
                    f"Side: <code>{signal.side.upper()}</code>\n"
                    f"Entry: {actual_entry:.6f}  Qty: {actual_qty}\n"
                    f"Eroare SL: <code>{type(e).__name__}: {str(e)[:200]}</code>\n"
                    f"Poziția a fost ÎNCHISĂ market reduce-only. Verifică manual pe Bybit."
                )
            except Exception as _e:
                print(f"  [TG] notify failed: {_e}")
        return None

    return LivePosition(
        symbol=symbol,
        side=signal.side,
        qty=actual_qty,            # qty REAL filled (poate fi < qty cerut)
        entry_price=actual_entry,  # avg fill price REAL
        sl_price=signal.sl_price,
        sl_initial=signal.sl_price,
        pos_usd=actual_qty * actual_entry,
        risk_usd=risk_usd,
        opened_ts=datetime.now(timezone.utc),
        order_entry_id=entry_order["id"],
        order_sl_id=sl_order["id"],
        extra={
            "sl_pct_at_signal": signal.sl_pct,
            "sl_pct_actual": actual_sl_pct,
            "balance_real_at_entry": balance_real,
            "pos_internal": pos_internal,
            "qty_requested": qty,
            "partial_fill": actual_qty < qty,
            "was_capped": pos_internal > max_bybit,
        },
    )


async def update_trailing_stop(
    *,
    client: "BybitClient",
    pos: LivePosition,
    new_stop: float,
) -> bool:
    """Modifică SL-ul DOAR dacă e îmbunătățire (sus pe long, jos pe short).

    Folosește ``modify_stop_price`` care are fallback intern la cancel+create.
    Dacă fallback-ul produce un nou order_id, ``pos.order_sl_id`` se update-ează.

    Returnează True dacă a updated, False dacă no-op.
    """
    if pos.side == "long":
        if new_stop <= pos.sl_price:
            return False
    else:
        if new_stop >= pos.sl_price:
            return False

    sl_side = "sell" if pos.side == "long" else "buy"
    result = await client.modify_stop_price(
        symbol=pos.symbol,
        order_id=pos.order_sl_id,
        new_stop_price=new_stop,
        side=sl_side,
        qty=pos.qty,
    )
    new_id = result.get("id") if isinstance(result, dict) else None
    if new_id and new_id != pos.order_sl_id:
        pos.order_sl_id = str(new_id)
    pos.sl_price = float(new_stop)
    return True


async def close_position_market(
    *,
    client: "BybitClient",
    pos: LivePosition,
    reason: str,
) -> dict[str, Any]:
    """Force-close pe market. Folosit la signal_reverse, cycle_success, panic."""
    bybit_side = "sell" if pos.side == "long" else "buy"
    await client.cancel_order(symbol=pos.symbol, order_id=pos.order_sl_id)
    result = await client.create_market_order(
        symbol=pos.symbol,
        side=bybit_side,
        qty=pos.qty,
        reduce_only=True,
    )
    result["reason"] = reason
    return result

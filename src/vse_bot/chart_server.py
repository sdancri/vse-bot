"""FastAPI chart server pentru un SubaccountRunner.

Servește chart_live.html + API + WebSocket broadcast pentru clienți.

Reguli implementate:
  - 4: port unic (CHART_PORT env), Bucharest TZ (config în chart HTML).
  - 8: chart_live.html template din boilerplate.
  - 10: prima bară LIVE = first_candle_ts (indicatori sunt calculați din
    warmup, dar candele afișate doar de la prima pornire).
  - 12: timestamp_ms din warmup → secunde live (conversion la broadcast).
  - 13: chart afișează DOAR SL/TP/PnL live, NU indicatori (regula nouă).

Multi-pair note: subaccount-ul are 2 perechi (ex KAIA+AAVE). Chart afișează
candele pe perechea PRINCIPALĂ (prima din config). Trade list și equity
curve arată ambele perechi.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from vse_bot.main import SubaccountRunner


def create_app(runner: "SubaccountRunner") -> FastAPI:
    """Construiește FastAPI app pentru un SubaccountRunner."""
    base = Path(__file__).resolve().parents[2]
    static_dir = base / "static"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Background tasks pornite de main.py — aici doar yield.
        yield

    app = FastAPI(
        title=f"VSE chart — {runner.sub_cfg.name}",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(str(static_dir / "chart_live.html"))

    @app.get("/api/init")
    async def api_init() -> JSONResponse:
        """Init payload — schema match cu chart_live.html boilerplate."""
        primary = runner.primary_pair_key()
        symbol = primary[0] if primary else ""
        bot_name = os.getenv("BOT_NAME", runner.sub_cfg.name)
        bp = runner.bot.init_payload()
        return JSONResponse({
            # Schema match chart_live.html
            "symbol": symbol,
            "timezone": "Europe/Bucharest",
            "bot_name": bot_name,
            "strategy": "VSE_Nou1",
            "candles": runner.candles_live,
            "trades": bp["trades"],
            "equity": bp["equity_curve"],
            "active_position": runner.active_position_payload(),
            "first_ts": bp["first_candle_ts"],
            "summary": {
                **bp["summary"],
                "initial_account": bp["initial_account"],
            },
            "indicators": [],
            "indicator_meta": [],
            # Extra fields pentru debug / API consumers
            "subaccount": runner.sub_cfg.name,
            "primary_pair": symbol,
        })

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        """Healthcheck endpoint cu reguli smart:
          - 200 OK: bot funcțional (running, candele recent, NU paused)
          - 503 Service Unavailable: degraded (paused, bar stale, no candles yet
            după start_period grace)

        Folosit de Docker healthcheck (interval 30s, retries 3).
        """
        import time as _t
        from vse_bot.exchange.bybit_ws import _tf_to_seconds   # type: ignore

        last = runner.candles_live[-1] if runner.candles_live else None
        primary = runner.primary_pair_key()
        primary_tf = primary[1] if primary else "1h"

        # Health rules
        warnings = []
        if runner.paused:
            warnings.append("paused")
        if last is None:
            # Niciun candle încă — OK în primele minute după start
            # (start_period 45s în compose acoperă asta)
            warnings.append("no_candles_yet")
        else:
            age_s = _t.time() - last[0]
            stale_threshold = 2 * _tf_to_seconds(primary_tf)
            if age_s > stale_threshold:
                warnings.append(f"bar_stale_{int(age_s)}s")

        body = {
            "bot_name": os.getenv("BOT_NAME", runner.sub_cfg.name),
            "subaccount": runner.sub_cfg.name,
            "healthy": len(warnings) == 0,
            "warnings": warnings,
            "candles_total": len(runner.candles_live),
            "last_candle_ts": last[0] if last else None,
            "connected_clients": len(runner.clients),
            "paused": runner.paused,
            "summary": runner.bot.summary(),
            "state": {
                "equity": runner.state.equity,
                "balance_broker": runner.state.balance_broker,
                "pool_used": runner.state.pool_used,
                "cycle_num": runner.state.cycle_num,
                "reset_count": runner.state.reset_count,
            },
        }
        # 503 dacă bot e degraded (Docker healthcheck va marca unhealthy)
        # — dar NU pentru "no_candles_yet" în primele minute (acoperit de start_period)
        critical = [w for w in warnings if w != "no_candles_yet" and w != "paused"]
        # paused != unhealthy (e operational pause); doar bar_stale e critical
        status_code = 503 if critical else 200
        return JSONResponse(body, status_code=status_code)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        runner.clients.add(ws)
        try:
            while True:
                # keep-alive (clienții nu trimit comenzi)
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            runner.clients.discard(ws)

    # ── Operational endpoints ───────────────────────────────────────────
    @app.post("/api/pause")
    async def api_pause() -> dict[str, Any]:
        runner.paused = True
        from vse_bot.event_log import log_event
        log_event(
            runner.cfg.operational.log_dir, runner.sub_cfg.name,
            "MANUAL_PAUSE", source="/api/pause",
        )
        return {"paused": True, "subaccount": runner.sub_cfg.name}

    @app.post("/api/resume")
    async def api_resume() -> dict[str, Any]:
        was_paused = runner.paused
        runner.paused = False
        from vse_bot.event_log import log_event
        log_event(
            runner.cfg.operational.log_dir, runner.sub_cfg.name,
            "MANUAL_RESUME", source="/api/resume", was_paused=was_paused,
        )
        return {"paused": False, "subaccount": runner.sub_cfg.name}

    @app.get("/api/state")
    async def api_state() -> dict[str, Any]:
        """Diagnostic snapshot pentru debug."""
        return {
            "subaccount": runner.sub_cfg.name,
            "paused": runner.paused,
            "state": {
                "equity": runner.state.equity,
                "balance_broker": runner.state.balance_broker,
                "pool_used": runner.state.pool_used,
                "cycle_num": runner.state.cycle_num,
                "reset_count": runner.state.reset_count,
            },
            "positions": {
                sym: (
                    {
                        "side": p.side, "qty": p.qty,
                        "entry": p.entry_price, "sl": p.sl_price,
                    } if p else None
                )
                for sym, p in runner.positions.items()
            },
            "summary": runner.bot.summary(),
        }

    return app


async def broadcast(runner: "SubaccountRunner", payload: dict) -> None:
    """Trimite payload JSON la toți clienții WebSocket. Idempotent la dead clients."""
    if not runner.clients:
        return
    msg = json.dumps(payload, default=str)
    dead: set = set()
    for ws in list(runner.clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    runner.clients.difference_update(dead)


async def serve_chart(app: FastAPI, port: int) -> None:
    """Pornește uvicorn (server) ca task async — folosit de main.py."""
    import uvicorn
    config = uvicorn.Config(
        app, host="0.0.0.0", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    await server.serve()

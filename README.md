# VSE 2Perechi — Bot trading multi-subaccount Bybit perpetuals

**Strategy:** VSE Nou1 (NO KILL SWITCH) per [strategy.md](strategy.md) / [CONFIG.md](CONFIG.md) / [STRATEGY_LOGIC.md](STRATEGY_LOGIC.md).
**Deploy:** Docker compose (2 containere, 1 per subaccount), Portainer-ready pe VPS.

## Status final

✅ Replay engine live-fidel ($28k+ portfolio 2.3y)
✅ Live wrapper: ccxt async + Bybit public/private WS + chart FastAPI
✅ Sizing: equity-compound, query Bybit balance real, cap 95% × balance × 20
✅ PnL real Bybit la close (cu fees) prin `fetch_pnl_for_trade`
✅ Chart per-subaccount cu equity curve + trade list + LIVE SL/TP/PnL
✅ Telegram alerts cu BOT_NAME prefix
✅ Dockerfile + docker-compose (port 8101 sub1, 8102 sub2)
✅ 10/10 tests pass

## Arhitectură deploy

```
Host VPS
├── VSE_1 (container)
│   ├── 1 process, SUBACCOUNT_NAME=subacc_1_kaia_aave
│   ├── chart: http://VPS:8101/
│   ├── Bybit subacc 1 (KAIA + AAVE 1H)
│   └── logs: ./logs/sub1/subacc_1_kaia_aave.jsonl
└── VSE_2 (container)
    ├── 1 process, SUBACCOUNT_NAME=subacc_2_ont_eth
    ├── chart: http://VPS:8102/
    ├── Bybit subacc 2 (ONT 1H + ETH 2H)
    └── logs: ./logs/sub2/subacc_2_ont_eth.jsonl
```

## Setup local

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# editează cu API keys Bybit (SUB1_*, SUB2_*) + Telegram (opțional)

# Replay validation (pe parquet istoric)
.venv/bin/python scripts/run_replay.py
# Așteptat: ~$28-31k portfolio wealth

# Tests
.venv/bin/python -m pytest tests/

# Pre-flight înainte de live
.venv/bin/python scripts/preflight_check.py
```

## Deploy

### Opțiunea 1: Portainer (recomandat) — 2 stack-uri separate

Creează 2 stack-uri INDEPENDENTE (redeploy unul nu-l afectează pe celălalt):

**Stack VSE_1**:
- Repository: `https://github.com/sdancri/vse-bot.git`
- Compose path: `compose.vse1.yml`
- Env vars: setează `TRADING_MODE`, `SUB1_*`, `SUB2_*`, `TELEGRAM_*`

**Stack VSE_2**:
- Repository: `https://github.com/sdancri/vse-bot.git`
- Compose path: `compose.vse2.yml`
- Env vars: aceleași (ambele stack-uri au nevoie de SUB1+SUB2 pentru Bybit auth)

În Portainer: Stacks → Add stack → Repository → completează settings de mai sus.

### Opțiunea 2: docker-compose direct (ambele într-un singur stack)

```bash
git clone https://github.com/sdancri/vse-bot.git
cd vse-bot
cp .env.example .env   # editează cu credentials
docker compose up -d   # pornește VSE_1 + VSE_2

# Verifică
docker compose ps
docker compose logs -f VSE_1
docker compose logs -f VSE_2
```

⚠️ **Atenție** la Opțiunea 2: `docker compose down` sau Portainer "Redeploy stack" reset-ează AMBII boți simultan (state fresh, charts goale, reconnect WS Bybit ~5s downtime).

Pentru restart selectiv pe Opțiunea 2:
```bash
docker compose up -d VSE_1   # restart DOAR VSE_1, VSE_2 continuă
```

### Charts

- `http://VPS_IP:8101` — VSE_1 (KAIA + AAVE)
- `http://VPS_IP:8102` — VSE_2 (ONT + ETH)

## Push DockerHub + GitHub (sdancri)

```bash
# DockerHub
docker login -u sdancri
docker build -t sdancri/vse_1:latest -t sdancri/vse_2:latest .
docker push sdancri/vse_1:latest
docker push sdancri/vse_2:latest

# GitHub
git init
git config user.email "lgadresa2000@gmail.com"
git config user.name "sdancri"
git add .
git commit -m "Initial commit: VSE 2Perechi bot v1.0"
git remote add origin https://github.com/sdancri/vse-bot.git
git push -u origin main
```

## Reguli implementate (Setari BOT.txt)

| # | Regulă | Implementare |
|---|---|---|
| 1 | Limite SLmin/SLmax | `config.yaml::sl_min_pct=0.005, sl_max_pct=0.035` |
| 2 | **Restart=de la 0** | `BotState.fresh()` la fiecare pornire, NU persistence |
| 3 | **PnL real Bybit** | `BybitClient.fetch_pnl_for_trade()` la close |
| 4 | Port chart Bucharest TZ | `Dockerfile::TZ=Europe/Bucharest`, `CHART_PORT=8101/8102` |
| 5 | `account += real PnL` | `BotState.add_closed_trade()` cu pnl=Bybit real |
| 6 | DockerHub: sdancri | `sdancri/vse_1:latest` + `sdancri/vse_2:latest` (separate) |
| 7 | Telegram cu BOT_NAME | `telegram_bot.py::_header()` |
| 8 | chart_template | `static/chart_live.html` |
| 9 | **Cap pe balanță Bybit reală** | `trade_lifecycle.py`: `if pos > balance×20: pos = 0.95×balance×20` |
| 10 | Indicators warmup | `VSESignalLive.warm_up()`, prima bară LIVE = `bot.first_candle_ts` |
| 11 | Logs detaliate | `event_log.py`: BAR_RECEIVED, SIGNAL_EVALUATED, FILTER_REJECTED, etc. |
| 12 | Timestamps ms→s | `on_bar`: `ts_s = ts_ms // 1000` la broadcast chart |
| 13 | Chart fără indicatori | chart_live.html — doar candele + SL/TP/PnL live |
| 14 | stdout buffering | `main.py`: `_sys.stdout = TextIOWrapper(line_buffering=True)` |

## Config final ([config/config.yaml](config/config.yaml))

```yaml
strategy:
  pool_total: 100.0
  equity_start: 50.0
  risk_pct_equity: 0.20         # 20% × equity_compound
  reset_trigger: 15.0
  reset_target: 50.0
  max_resets: null              # NO KILL
  withdraw_target: 10000.0      # $10k → wealth ~$31k vs ~$16k la $5k
  sl_min_pct: 0.005
  sl_max_pct: 0.035
  cooldown_bars: 3
  leverage: 20
  cap_pct_of_max: 0.95          # cap = 95% × balance_real × 20
  taker_fee: 0.00055
  style: Balanced
  withdraw_check_mode: on_close
  opp_exit_mode: pure
```

## Workflow în ordine

1. **Bybit setup**: 2 subaccounts, API keys MAINNET (Trade ON / Withdraw OFF), $100 USDT × 2, leverage 20× isolated.
2. **`.env`**: completează cu SUB1_*, SUB2_*, TELEGRAM_*, **`TRADING_MODE=live`**.
3. **Pre-flight**: `python scripts/preflight_check.py` → toate ✓.
4. **Live deploy**: 2 stack-uri Portainer (`compose.vse1.yml` + `compose.vse2.yml`).
5. Monitor primele zile prin chart + Telegram. Cycle 1 SUCCESS așteptat în lunile 14-22.

⚠️ **Default acum e LIVE (banii reali).** Dacă vrei testnet (paper trading) pentru validare: setează explicit `TRADING_MODE=testnet` în `.env`.

## Wealth target replay (2024-01 → 2026-04, full window)

| | Subacc 1 | Subacc 2 | Total |
|---|---|---|---|
| Wealth | ~$15-18k | ~$13k | **~$28-31k** |
| Cycle SUCCESS | 1 (~14 luni) | 1 (~17 luni) | 2 |

**Notă live**: pornind acum (apr 2026), startup-ul nu beneficiază de compounding 2024-2025. Așteptarea pentru primele 12 luni: portfolio ~$1-2k. Cycle 1 SUCCESS probabil în lunile 14-22.

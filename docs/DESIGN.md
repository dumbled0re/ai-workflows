# JP Stock Analyzer - Design Document

## Overview

Claude AIを活用した日本株分析システム。GitHub Actionsで毎日2回自動実行し、保有銘柄の上昇/下落予測と有望銘柄の発掘結果をSlackに通知する。

## Requirements

- 日本株のみ対象（保有銘柄は`stocks.yml`で設定）
- Claude API（Anthropic）でテクニカル指標ベースの分析
- 毎日2回実行（朝8:00 JST / 夕16:00 JST）
- 結果をSlack Webhookで通知
- 有望銘柄は日経225構成銘柄からスクリーニング

## Architecture

### System Flow

```
[stocks.yml] ──> [config_loader] ──> Config object
                                          │
                    ┌─────────────────────┤
                    ▼                     ▼
            Holdings tickers      Nikkei 225 tickers
                    │                     │
                    ▼                     ▼
            [data_fetcher]         [data_fetcher]
            yf.download(batch)     yf.download(5 batches × 50)
                    │                     │
                    ▼                     ▼
            [technical_indicators] [stock_screener]
            Full TA for each       Fast screen → top 50 → Full TA
                    │                     │
                    └──────┬──────────────┘
                           ▼
                    [ai_analyzer]
                    Claude API call #1: Holdings analysis
                    Claude API call #2: Discovery analysis
                           │
                           ▼
                    [slack_notifier]
                    Format Block Kit message
                    POST to Webhook URL
                           │
                           ▼
                    Slack channel notification
```

### File Structure

```
jp-stock-analyzer/
├── .github/workflows/
│   └── stock-analysis.yml       # GitHub Actions workflow
├── src/
│   ├── main.py                  # Entry point / orchestrator
│   ├── config_loader.py         # stocks.yml parser & validator
│   ├── data_fetcher.py          # yfinance data retrieval
│   ├── technical_indicators.py  # Technical indicator computation
│   ├── stock_screener.py        # Nikkei 225 screening
│   ├── nikkei225_components.py  # Nikkei 225 ticker list
│   ├── ai_analyzer.py           # Claude API integration
│   └── slack_notifier.py        # Slack webhook notification
├── stocks.yml                   # User holdings configuration
├── requirements.txt             # Python dependencies
├── docs/
│   └── DESIGN.md                # This file
└── README.md                    # Setup guide
```

## Component Details

### 1. `stocks.yml` - User Configuration

```yaml
holdings:
  - ticker: "7203.T"
    name: "トヨタ自動車"
    shares: 100
    avg_cost: 2500  # optional
  - ticker: "6758.T"
    name: "ソニーグループ"
    shares: 50

settings:
  history_days: 90
  discovery_top_n: 5
  screener_pool_size: 50
  claude_model: "claude-sonnet-4-5-20250929"
```

### 2. `src/config_loader.py` - Configuration Loader

**Responsibilities:**
- Parse `stocks.yml` with PyYAML
- Validate fields (ticker `.T` suffix, required fields)
- Return typed dataclass objects

**Data Models:**
```python
@dataclass
class Holding:
    ticker: str       # e.g. "7203.T"
    name: str         # e.g. "トヨタ自動車"
    shares: int
    avg_cost: float | None = None

@dataclass
class Settings:
    history_days: int = 90
    discovery_top_n: int = 5
    screener_pool_size: int = 50
    claude_model: str = "claude-sonnet-4-5-20250929"

@dataclass
class Config:
    holdings: list[Holding]
    settings: Settings
```

### 3. `src/nikkei225_components.py` - Nikkei 225 Ticker List

**Approach:** Hardcoded list (not `pytickersymbols` dependency)
- 225 tickers with sector info
- Updated infrequently (Nikkei reviews in April/October)
- Avoids external dependency and ensures reliability in CI

```python
NIKKEI_225_TICKERS: list[dict] = [
    {"ticker": "7203.T", "name": "トヨタ自動車", "sector": "自動車"},
    {"ticker": "6758.T", "name": "ソニーグループ", "sector": "電気機器"},
    # ... all 225
]
```

### 4. `src/data_fetcher.py` - Market Data Retrieval

**Key Design:**
- `yf.download()` for batch fetching (225 calls → 5 calls)
- Batches of 50 tickers with 3-second sleep between batches
- Exponential backoff retry (2s, 4s, 8s) for 429 errors
- Returns `dict[str, pd.DataFrame]`

```python
def fetch_batch(tickers: list[str], period: str = "3mo") -> dict[str, pd.DataFrame]:
    """Fetch historical data for multiple tickers in one API call."""

def fetch_single(ticker: str, period: str = "3mo") -> pd.DataFrame:
    """Fallback for individual ticker fetch with retry logic."""
```

### 5. `src/technical_indicators.py` - Technical Analysis

**Library:** `ta` (bukosabino/ta) - pure Python, no C compilation needed

**Computed Indicators:**

| Indicator | Parameters | Purpose |
|-----------|-----------|---------|
| SMA | 5, 25, 75日 | トレンド方向、ゴールデン/デッドクロス |
| RSI | 14日 | 買われすぎ/売られすぎ判定 |
| MACD | 12, 26, 9 | モメンタム、トレンド転換シグナル |
| Bollinger Bands | 20, 2σ | ボラティリティ、バンド内ポジション |
| Volume Ratio | 現在/20日平均 | 出来高異常検知 |
| Price Changes | 1日, 5日, 1ヶ月, 3ヶ月 | 短期〜中期パフォーマンス |
| 52W High/Low Distance | % | 年間レンジ内ポジション |

**Output per ticker:**
```python
{
    "ticker": "7203.T",
    "name": "トヨタ自動車",
    "current_price": 2850,
    "price_change_1d": -1.2,
    "price_change_5d": 3.5,
    "price_change_1m": -2.1,
    "price_change_3m": 8.7,
    "sma_5": 2830, "sma_25": 2790, "sma_75": 2650,
    "rsi_14": 62.3,
    "macd_value": 15.2, "macd_signal": 10.1, "macd_histogram": 5.1,
    "bb_upper": 2920, "bb_middle": 2800, "bb_lower": 2680,
    "bb_position_pct": 0.73,
    "volume_ratio": 1.35,
    "distance_from_52w_high": -5.2,
    "distance_from_52w_low": 28.4,
    "trend_signal": "SMA5 > SMA25 > SMA75 (bullish alignment)"
}
```

### 6. `src/stock_screener.py` - Two-Phase Screening

**Phase 1: Python Fast Screening (no Claude API)**
- Batch download all 225 tickers (5 batches of ~50)
- Compute lightweight indicators: RSI, volume ratio, SMA crossovers
- Score each stock (0-100) based on bullish criteria:
  - RSI 30-50 (oversold recovery): +20pts
  - RSI 50-65 (healthy momentum): +15pts
  - Volume ratio > 1.5x: +20pts
  - Price broke above SMA25 in last 3 days: +20pts
  - MACD histogram turning positive: +15pts
  - Price near BB lower band (within 5%): +10pts
- Estimated time: 30-60 seconds

**Phase 2: Claude Deep Analysis (1 API call)**
- Top 50 scored candidates → full indicator computation
- Send all 50 to Claude → Claude picks top 5
- Estimated time: 5-10 seconds

### 7. `src/ai_analyzer.py` - Claude API Integration

**Model:** `claude-sonnet-4-5-20250929` (cost-effective, sufficient for TA interpretation)

**API Calls per Run:** Exactly 2
1. Holdings analysis
2. Discovery analysis

**System Prompt:**
```
You are an expert Japanese stock market analyst. You analyze technical indicators,
market data, and price patterns for stocks listed on the Tokyo Stock Exchange.

Your analysis should consider:
- Technical indicator signals (RSI, MACD, Bollinger Bands, moving averages)
- Volume patterns and institutional interest signals
- Price momentum and trend direction
- Japanese market-specific factors (BOJ policy, yen strength, sector rotation)

Always provide:
1. A clear UP or DOWN prediction
2. Confidence level (HIGH / MEDIUM / LOW)
3. 2-3 bullet-point reasons
4. A risk factor to watch

Respond in valid JSON format as specified.
```

**Holdings Analysis Response Format:**
```json
{
  "holdings_analysis": [
    {
      "ticker": "7203.T",
      "name": "トヨタ自動車",
      "prediction": "UP",
      "confidence": "HIGH",
      "reasons": ["reason1", "reason2", "reason3"],
      "risk_factor": "...",
      "short_summary": "..."
    }
  ],
  "market_overview": "..."
}
```

**Discovery Response Format:**
```json
{
  "recommended_stocks": [
    {
      "rank": 1,
      "ticker": "XXXX.T",
      "name": "...",
      "prediction": "UP",
      "confidence": "HIGH",
      "expected_move": "+X% to +Y% in Z weeks",
      "reasons": ["reason1", "reason2", "reason3"],
      "risk_factor": "...",
      "entry_strategy": "..."
    }
  ]
}
```

### 8. `src/slack_notifier.py` - Slack Notification

**Format:** Slack Block Kit (rich formatting)

**Message Structure:**
```
[Header] 日本株AI分析レポート - 2026-03-01 朝
[Divider]
[Section] マーケット概況
[Divider]
[Header] 保有銘柄分析
[Section per holding]
  *トヨタ自動車 (7203.T)* ↑ UP | 信頼度: HIGH
  現在値: 2,850円 | 1日: -1.2% | 5日: +3.5%
  ・RSI 62で健全なモメンタム
  ・MACD正転し拡大中
  ・出来高1.35倍で機関投資家の買い示唆
  ⚠ リスク: 円高が輸出利益を圧迫する可能性
[Divider]
[Header] おすすめ銘柄
[Section per recommendation]
  *#1 - ファーストリテイリング (9983.T)* | 信頼度: HIGH
  予想: 2週間で+5-8%
  ・SMA75ブレイクアウト+出来高急増
  ・RSI売られすぎ圏から回復中
  エントリー: 38,500円以下で検討
[Context] Generated by Claude AI | Not financial advice
```

### 9. `src/main.py` - Orchestrator

```python
def main():
    # 1. Determine timing (morning/evening based on JST)
    # 2. Load config from stocks.yml
    # 3. Fetch holdings data (batch)
    # 4. Compute full indicators for holdings
    # 5. Screen Nikkei 225 candidates
    # 6. Claude analysis (2 API calls)
    # 7. Send Slack notification
    # 8. Top-level error handling → Slack error notification
```

### 10. GitHub Actions Workflow

```yaml
name: Japanese Stock Analysis

on:
  schedule:
    - cron: '0 23 * * 0-4'   # Mon-Fri 8:00 JST = Sun-Thu 23:00 UTC
    - cron: '0 7 * * 1-5'    # Mon-Fri 16:00 JST = Mon-Fri 07:00 UTC
  workflow_dispatch: {}        # Manual trigger

env:
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}

jobs:
  analyze:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'
      - run: pip install -r requirements.txt
      - run: python src/main.py
```

## Error Handling

| Component | Error | Handling |
|-----------|-------|----------|
| data_fetcher | 429 Rate Limit | Exponential backoff (2s, 4s, 8s), skip ticker |
| data_fetcher | Invalid ticker | Log warning, exclude, continue |
| data_fetcher | Network timeout | Retry 3 times with 5s timeout |
| technical_indicators | Insufficient data (<20 rows) | Skip, use available metrics only |
| ai_analyzer | Claude API 5xx | Retry once after 5s |
| ai_analyzer | Invalid JSON response | Retry once with "fix JSON" follow-up |
| ai_analyzer | Invalid API key | Fail immediately with clear error |
| slack_notifier | Webhook failure | Retry once, fallback to stdout |
| main | Unhandled exception | Catch, send error to Slack, exit(1) |

## Dependencies

```
yfinance>=0.2.40
anthropic>=0.40.0
requests>=2.31.0
pandas>=2.1.0
ta>=0.11.0
PyYAML>=6.0
```

**Why `ta` over `TA-Lib`:** Pure Python, no C library compilation required in GitHub Actions.

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| `ta` library (not TA-Lib) | Pure Python, easy CI setup |
| Hardcoded Nikkei 225 list | No dependency, list changes infrequently |
| `yf.download()` batch | 225 API calls → 5 calls |
| Claude Sonnet (not Opus) | Cost-effective (~$0.05/run) |
| JSON response format | Reliable parsing, structured Slack formatting |
| Two-phase screening | Python pre-filter avoids sending 225 stocks to Claude |

## Cost Estimate

- **Claude API:** ~2 calls/run × $0.02 = ~$0.04/run → ~$0.08/day → ~$2.40/month
- **GitHub Actions:** ~3 min/run × 2/day × 22 days = ~132 min/month (well within free tier)

## GitHub Secrets Required

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL |

## Disclaimer

This tool provides AI-generated analysis for informational purposes only. It is not financial advice. Always conduct your own research before making investment decisions.

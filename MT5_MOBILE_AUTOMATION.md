# Mobile MT5 Automated Trade Execution: Architecture & Roadmap

This document outlines the architectural options and step-by-step roadmap for allowing the Telegram Bot (`bot.py`, hosted on Render) to automatically execute trades extracted from trading chart screenshots so that positions appear instantly on your **MetaTrader 5 (MT5) Mobile App (iOS / Android)**.

---

## 1. Executive Summary & Core Constraints

### The Mobile MT5 Reality
iOS and Android mobile apps run in strict sandboxes. Python scripts running in the cloud (e.g. Render) **cannot directly tap into the MT5 Mobile App** installed on your phone to press "Buy" or "Sell".

### How Mobile MT5 Automation Works
Even though the phone app itself cannot be scripted, your Mobile MT5 app connects to your **Broker's MT5 Trading Server**. 
- Whenever an order is placed **server-side** on your broker account, it **instantly reflects on your Mobile MT5 app**.
- You can monitor floating PnL, modify Stop Loss (SL) / Take Profit (TP), or close trades directly from your phone.

### User Security & Operational Rules
1. **No Personal Computer Dependency:** The solution must run 24/7 in the cloud without requiring a personal desktop or laptop to stay powered on.
2. **No Third-Party Credential Sharing:** Broker login numbers and passwords **must never** be shared with third-party cloud bridge services (such as MetaApi).

---

## 2. Architectural Options

```mermaid
graph TD
    A[User sends Chart Screenshot] -->|Telegram Photo| B[bot.py on Render]
    B -->|Gemini Vision AI| C[Extract Asset, Entry, SL, TP]
    
    subgraph Option 1: Self-Hosted MQL5 EA
    C -->|Signal JSON via Telegram API| D[MQL5 Expert Advisor on Free AWS VPS]
    D -->|OrderSend| E[Broker MT5 Server]
    end
    
    subgraph Option 2: Direct Broker REST API
    C -->|HTTP POST Request| F[Broker REST API e.g. Exness / OANDA / Bybit]
    F -->|Execute Order| E
    end
    
    subgraph Option 3: Assisted One-Tap Mobile
    C -->|Interactive Telegram Inline Button| G[Copy Formatted Trade Spec]
    G -->|User Paste/Tap| E
    end
    
    E -->|Real-Time Position Sync| H[Mobile MT5 App on Phone]
```

---

### Option 1 (Recommended): Self-Hosted MQL5 Telegram Copier EA (Free VPS)
**Best for:** Traders using any standard MT5 broker who want 100% security and zero third-party data sharing.

#### How It Works
1. `bot.py` analyzes the chart screenshot on Render and broadcasts a structured signal message (or JSON) to a designated Telegram chat or channel.
2. A lightweight **MQL5 Expert Advisor (EA)** script running inside an official MT5 terminal on a free cloud VPS (such as **AWS EC2 Micro Free Tier**, $0/month) polls the Telegram API (`WebRequest`).
3. Upon receiving the signal from `bot.py`, the EA executes the trade via MT5's native `OrderSend()` function.
4. The trade immediately pops up on your **Mobile MT5 app**.

#### Security & Privacy Profile
- **Zero Third-Party Access:** Your MT5 account login and password remain encrypted inside the official MetaTrader 5 terminal on your own private server instance.
- **No API Keys Needed:** Operates via standard MT5 terminal connectivity.

#### Implementation Steps
- [ ] Create `MQL5/TelegramSignalCopier.mq5` script in this repository.
- [ ] Configure `WebRequest` URLs in MT5 (`https://api.telegram.org`).
- [ ] Implement lot-size calculation based on account balance and risk % or fixed lots.
- [ ] Document free-tier AWS EC2 / Linux VPS setup for 24/7 MT5 terminal hosting.

---

### Option 2: Direct Broker REST API (Zero VPS / Terminal Needed)
**Best for:** Traders whose broker provides an official HTTP REST API (e.g. Exness, OANDA, Bybit, IC Markets cTrader).

#### How It Works
1. You generate an official API Key from your broker client dashboard with **Trade permissions only** (withdrawal permissions explicitly disabled).
2. `bot.py` on Render sends an HTTP POST request directly to your broker's official API endpoint when a chart is analyzed.
3. The broker server opens the position, and it syncs instantly to your **Mobile App**.

#### Security & Privacy Profile
- **Zero VPS / Desktop Terminal Required:** No MT5 terminal needs to be hosted anywhere.
- **Restricted Token Scope:** API tokens cannot withdraw funds.

#### Implementation Steps
- [ ] Identify broker API documentation and authentication mechanism.
- [ ] Create `broker_api.py` helper in this repository to wrap order execution calls.
- [ ] Add environment variables for API Key/Secret to `.env.example` and `render.yaml`.
- [ ] Integrate automatic execution into `bot.py`'s `handle_chart` pipeline.

---

### Option 3: Assisted Mobile "One-Tap" Inline Telegram Buttons
**Best for:** Zero server setup, zero broker account connectivity, semi-automated workflow.

#### How It Works
1. When `bot.py` replies with the analyzed signal card, it attaches interactive Telegram **Inline Keyboard Buttons** (e.g., `[ 📋 Copy Trade Spec ]` / `[ 📱 Open MT5 ]`).
2. Tapping the button on your mobile device formats the exact symbol, lot size, SL, and TP into your phone clipboard for immediate pasting into Mobile MT5.

#### Security & Privacy Profile
- **100% Offline from Account:** The bot never connects to or knows your trading account.

#### Implementation Steps
- [ ] Build Telegram inline keyboards in `bot.py`.
- [ ] Add callback query handlers for mobile formatting shortcuts.

---

## 3. Decision & Implementation Tracker

| Option | Status | Selected | Next Action |
| --- | --- | --- | --- |
| **Option 1: Self-Hosted MQL5 EA** | Proposed | ⏳ Pending User Choice | Write MQL5 Expert Advisor template and VPS setup guide. |
| **Option 2: Direct Broker REST API** | Proposed | ⏳ Pending User Choice | Confirm broker name & API availability. |
| **Option 3: Assisted Mobile Buttons** | Proposed | ⏳ Pending User Choice | Add Telegram Inline Keyboard to signal cards. |

---

## 4. Documentation Log

- **2026-07-31:** Created initial architecture document comparing Mobile MT5 execution strategies without third-party credential bridges or local PC dependency.

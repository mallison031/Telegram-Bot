# Roadmap & Instructions: Automated Trading Chart Vision Bot

This document outlines the step-by-step roadmap for building an automated Telegram bot that receives a chart image, extracts trading parameters using Vision AI, calculates profit/loss percentages, and returns a strictly formatted message.

**Target Output Format:**
```text
[ASSET] [ACTION] [ORDER_TYPE]
ENTRY: [VALUE]
SL: [VALUE]
TP: [VALUE]
Profit: +[X]% / Loss: -[Y]%
```

---

## Phase 1: Telegram Bot Provisioning
**Goal:** Create the interface where users will send images and receive the signals.

1.  **Create the Bot:** Open Telegram and search for `@BotFather`. Use the `/newbot` command to create a new bot.
2.  **Secure Credentials:** Obtain the HTTP API Token provided by BotFather. This will be used by your backend server to communicate with Telegram.
3.  **Configure Bot Settings:** Set the bot's description and profile picture. Use BotFather to configure the commands menu (e.g., adding a `/start` command).

## Phase 2: Vision AI Setup & Prompt Engineering
**Goal:** Configure a multimodal Large Language Model (like Google Gemini 1.5 Pro) to accurately "read" trading charts. 

*Reference Case Study:* When the bot processes an image like `BTCUSD_2026-07-09_10-34-20.png`, it needs to identify the asset name in the top left ("Bitcoin / U.S. Dollar"), recognize the TradingView Short Position tool, and read the price labels on the right axis.
*   **Grey Box (Middle):** 63552.40 (Entry)
*   **Red Box (Top):** 63717.50 (Stop Loss)
*   **Blue Box (Bottom):** 60361.53 (Take Profit)

**Instructions for AI Prompting:**
1.  **Enforce Structured Output:** Do not let the AI write the final message directly. Instruct the AI to output *only* a strictly formatted JSON object containing the raw numbers and asset string.
2.  **Define Visual Cues:** Tell the AI in the prompt how to read the chart. For example: "The red shaded box represents the Stop Loss zone. The blue shaded box represents the Take Profit zone. The line separating them is the Entry price."
3.  **Determine Order Type:** Instruct the AI to deduce the order type. If the current price (e.g., `62913.22` in `BTCUSD_2026-07-09_10-34-20.png`) is below a short position entry, it is a "SELL LIMIT". If it's a long position above the current price, it's a "BUY LIMIT".

## Phase 3: Backend Logic & Math (Avoiding AI Hallucinations)
**Goal:** Process the AI's data and accurately calculate the percentages. *Never trust the AI to do the math.*

1.  **Parse the AI Response:** Extract the JSON data containing Asset, Entry, SL, TP, and Action.
2.  **Calculate Percentages (Instructions):**
    *   **For a SELL signal:**
        *   `Profit Percentage = ((Entry - TP) / Entry) * 100`
        *   `Loss Percentage = ((SL - Entry) / Entry) * 100`
    *   **For a BUY signal:**
        *   `Profit Percentage = ((TP - Entry) / Entry) * 100`
        *   `Loss Percentage = ((Entry - SL) / Entry) * 100`
3.  **Format the Data:** Round the calculated percentages to two decimal places.

## Phase 4: Message Formatting & Delivery
**Goal:** Assemble the final text and send it back to the user on Telegram.

1.  **Construct the String:** Map the parsed JSON values and calculated percentages to your exact requested template. 
    *   *Example construction based on `BTCUSD_2026-07-09_10-34-20.png`:*
        BTCUSD SELL LIMIT
        ENTRY: 63552.40
        SL: 63717.50
        TP: 60361.53
2.  **API Delivery:** Use your Telegram Bot Token to trigger the `sendMessage` endpoint, targeting the `chat_id` of the user who sent the picture.

## Phase 5: Hosting and Deployment
**Goal:** Ensure the bot is responsive 24/7 without running on a personal computer.

1.  **Choose a Cloud Provider:** Select a backend hosting provider (e.g., AWS, Render, Heroku, or DigitalOcean).
2.  **Set Environment Variables:** Securely store your Telegram Token and Vision AI API keys in the cloud environment variables.
3.  **Deploy as a Background Worker:** Deploy your application so it constantly listens for incoming webhooks or polls the Telegram API for new messages.

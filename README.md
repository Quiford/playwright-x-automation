# Playwright X Automation

An asynchronous browser automation framework built with Python and Playwright for real-time X (Twitter) monitoring, queue-based task processing, and automated workflow execution.

This project demonstrates:
- browser automation
- asynchronous processing
- SQLite persistence
- queue management
- configuration-driven workflows
- Telegram notifications
- duplicate prevention systems
- and automation architecture design

The framework operates entirely through a real Chromium browser using Playwright and does not rely on external APIs.

---

# Features

- Real-time X timeline monitoring
- Playwright-powered browser automation
- Queue-based reply scheduling
- SQLite persistence layer
- Duplicate reply prevention
- Per-account cooldown protection
- Configurable reply probability and delays
- Telegram notifications
- Human-like typing simulation
- Proxy support
- Persistent browser session handling
- Async task orchestration with asyncio

---

# Project Structure
playwright-x-automation/
│
├── config/
│   └── settings.py          # Pydantic configuration loader
│
├── watcher/
│   └── monitor.py           # Timeline monitoring & scraping
│
├── scheduler/
│   └── queue.py             # Queue management & scheduling logic
│
├── responder/
│   └── engine.py            # Browser-based action execution
│
├── storage/
│   └── database.py          # SQLite persistence layer
│
├── notifier/
│   └── telegram.py          # Telegram event notifications
│
├── utils/
│   ├── logger.py            # Logging utilities
│   ├── mutation.py          # Text mutation utilities
│   └── proxy.py             # Proxy configuration helpers
│
├── data/
│   ├── accounts.json        # Target account list
│   └── replies.json         # Reply template pool
│
├── .env.example
├── requirements.txt
├── README.md
└── main.py

---

# Technology Stack

- Python
- Playwright
- SQLite
- asyncio
- Pydantic Settings
- Telegram Bot API

---

# Core Workflow

1. Launches a persistent Chromium browser session using Playwright
2. Monitors configured X timelines in real time
3. Detects new posts from configured accounts
4. Queues automated actions using scheduling logic
5. Processes queued actions asynchronously
6. Stores all state locally using SQLite
7. Prevents duplicate processing through multi-layer safeguards
8. Sends Telegram notifications for system events

---

# Duplicate Prevention & Safety Logic

The framework includes multiple safeguards to prevent duplicate actions:

- Processed tweet tracking
- Queue-level duplicate protection
- Per-account cooldown management
- Daily action limits
- Async locking to prevent concurrent execution
- Self-account filtering
- Persistent state tracking across restarts

---

# Installation

## 1. Clone the Repository
git clone https://github.com/Quiford/playwright-x-automation.git
cd playwright-x-automation

---

## 2. Install Dependencies
pip install -r requirements.txt
playwright install chromium

---

## 3. Configure Environment Variables

Copy the example configuration file:
copy .env.example .env

Edit .env with your preferred settings.

---

## 4. Configure Target Accounts

Edit:
data/accounts.json

Example:
[
  { "handle": "example_account" }
]

---

## 5. Configure Reply Templates

Edit:
data/replies.json

Add custom reply templates.

---

## 6. First Launch

Run:
py main.py

On first launch:
- Chromium opens
- Log into X manually
- Session state is stored locally for reuse

---

# Telegram Notifications

Optional Telegram integration supports:
- queued event notifications
- successful action alerts
- failure/error alerts
- startup/shutdown notifications

Configure in:
.env

---

# Persistence Layer

SQLite is used for:
- processed item tracking
- queue management
- cooldown storage
- execution state persistence

This allows the framework to recover cleanly after restarts without reprocessing old items.

---

# Key Engineering Concepts Demonstrated

- Async Python architecture
- Browser automation
- Queue processing systems
- Persistent state management
- Automation workflow orchestration
- Real-time event monitoring
- Fault-tolerant task execution
- Modular Python project structure

---

# Disclaimer

This project is intended for educational purposes, browser automation experimentation, and workflow automation research only.

Users are responsible for complying with platform policies and applicable laws when operating automated systems.

---

# Author

Yusuf Afolabi

GitHub:
https://github.com/Quiford
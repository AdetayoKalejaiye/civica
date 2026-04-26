# 🗳️ Civica — Neutral Civic Intelligence, Everywhere

> **AI-powered civic education delivered as a Progressive Web App and SMS bot.**  
> Stay informed on politics, elections, and government — without the spin.

---

## ✨ What is Civica?

Civica is a dual-platform civic intelligence tool that cuts through media noise and keeps citizens informed. It pulls news exclusively from trusted, neutral outlets and uses a large language model to synthesize it into clear, unbiased civic digests — delivered either through a beautiful installable web app **or** straight to your phone via text message.

No partisan commentary. No algorithm rabbit holes. Just facts.

---

## 🚀 Features

### 📱 Progressive Web App (`app.py`)
- **Installable PWA** — works like a native app on iOS and Android
- **Push Notifications** — get civic digests delivered to your device via Web Push (VAPID)
- **Location-aware feed** — national news + your state and city's political updates
- **AI Q&A panel** — ask any civic question and get a factual, sourced answer
- **Offline support** — service worker caches the app for offline reading
- **Dark mode** — easy on the eyes, day or night

### 📲 SMS Bot (`civica.py`)
- **Text-in civic digests** — subscribe by texting your city/state or zip code
- **48-hour digest schedule** — automated, relevant news summaries sent on a cadence
- **Civic Q&A by SMS** — ask up to 5 civic questions per day, right from your messages
- **Rate limiting** — fair use per user, resets every 24 hours
- **STOP/START support** — full Twilio opt-out compliance

### 🤖 AI & News Intelligence (shared)
- **LLM-powered summaries** — Groq-hosted Llama 3 (70B) synthesizes news into structured civic digests
- **AI classifier** — automatically screens questions for civic relevance before answering
- **Google Custom Search grounding** — Q&A answers are grounded with real-time web results
- **Structured digests** — each digest includes a headline, summary, story breakdowns, and a civic education fact
- **Trusted sources only** — Reuters, AP News, NPR, PBS, BBC, Politico, The Hill

---

## 🏗️ Architecture

```
civica/
├── app.py            # PWA backend — Flask + Web Push
├── civica.py         # SMS backend — Flask + Twilio
├── requirements.txt  # Python dependencies
├── runtime.txt       # Python 3.11.9
├── templates/
│   └── index.html    # PWA shell (single-page app)
└── static/
    ├── manifest.json # PWA manifest
    ├── sw.js         # Service Worker (cache + push)
    ├── css/
    │   └── app.css
    └── js/
        └── app.js
```

### Data Flow

```
NewsAPI ──► fetch_news()
               │
               ▼
         Groq / Llama 3  ──► generate_digest()
               │
        ┌──────┴──────┐
        ▼             ▼
    PostgreSQL    Push / SMS
    (digests,    (pywebpush /
    subs, users)   Twilio)
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Web Framework | Flask 3 |
| Database | PostgreSQL (asyncpg) |
| Async HTTP | httpx |
| LLM | Groq API — Llama 3.3 70B |
| News | NewsAPI |
| Web Search | Google Custom Search API |
| SMS | Twilio |
| Push Notifications | pywebpush (VAPID) |
| Scheduler | APScheduler |
| Frontend | Vanilla JS PWA + Service Worker |

---

## ⚙️ Environment Variables

### PWA (`app.py`)

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `GROQ_API_KEY` | ✅ | Groq API key for Llama 3 |
| `NEWS_API_KEY` | ✅ | NewsAPI.org key |
| `VAPID_PRIVATE_KEY` | ✅ | VAPID private key (Web Push) |
| `VAPID_PUBLIC_KEY` | ✅ | VAPID public key (Web Push) |
| `VAPID_EMAIL` | ✅ | Contact email in VAPID claims |
| `ADMIN_KEY` | ⚠️ | Secret key for admin endpoints (default: `change-me`) |
| `PORT` | — | Server port (default: `8000`) |

### SMS Bot (`civica.py`)

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `GROQ_API_KEY` | ✅ | Groq API key for Llama 3 |
| `NEWS_API_KEY` | ✅ | NewsAPI.org key |
| `TWILIO_ACCOUNT_SID` | ✅ | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | ✅ | Twilio auth token |
| `TWILIO_PHONE_NUMBER` | ✅ | Your Twilio phone number |
| `GOOGLE_API_KEY` | — | Google Custom Search API key (Q&A grounding) |
| `GOOGLE_CX` | — | Google Custom Search engine ID |
| `ADMIN_KEY` | ⚠️ | Secret key for admin endpoints (default: `change-me`) |
| `QUESTIONS_PER_DAY` | — | SMS Q&A rate limit per user (default: `5`) |

---

## 🚦 Getting Started

### Prerequisites

- Python 3.11+
- PostgreSQL database
- A [Groq API key](https://console.groq.com) (free tier available)
- A [NewsAPI key](https://newsapi.org) (free tier available)

### 1. Clone & install

```bash
git clone https://github.com/AdetayoKalejaiye/civica.git
cd civica
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env   # or create .env manually
# Fill in your API keys and database URL
```

### 3. Initialize the database

The database tables are created automatically on first startup. Just make sure your `DATABASE_URL` points to a live PostgreSQL instance.

### 4a. Run the PWA

```bash
python app.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

### 4b. Run the SMS Bot

```bash
python civica.py
```

Point your Twilio webhook to `https://<your-domain>/webhook/sms`.

---

## 📡 API Reference

### PWA Endpoints (`app.py`)

| Method | Route | Description |
|---|---|---|
| `GET` | `/` | Serve the PWA shell |
| `GET` | `/api/updates?location=<loc>` | Fetch recent civic digests for a location |
| `POST` | `/api/generate-digest` | Generate a fresh digest on demand |
| `POST` | `/api/subscribe` | Subscribe to Web Push notifications |
| `POST` | `/api/unsubscribe` | Unsubscribe from Web Push notifications |
| `POST` | `/api/ask` | Ask a civic question (AI-answered) |
| `GET` | `/health` | Health check |
| `POST` | `/admin/trigger-digest` | Manually trigger digest job *(requires `X-Admin-Key`)* |

#### `POST /api/ask`
```json
// Request
{ "question": "How does the electoral college work?", "location": "Denver, CO" }

// Response
{ "answer": "...", "not_civic": false }
```

#### `POST /api/subscribe`
```json
{
  "subscription": {
    "endpoint": "https://...",
    "keys": { "p256dh": "...", "auth": "..." }
  },
  "location": "Austin, TX",
  "state": "TX"
}
```

### SMS Bot Endpoints (`civica.py`)

| Method | Route | Description |
|---|---|---|
| `POST` | `/webhook/sms` | Twilio inbound SMS webhook |
| `GET` | `/health` | Health check |
| `GET` | `/admin/users` | List subscribed users *(requires `X-Admin-Key`)* |
| `POST` | `/admin/send-digest-now` | Manually trigger digest send *(requires `X-Admin-Key`)* |

#### SMS Commands (user-facing)

| Message | Action |
|---|---|
| `Austin, TX` | Register or update location |
| `73301` | Register or update by zip code |
| `STOP` / `UNSUBSCRIBE` | Opt out of digests |
| Any civic question | Get an AI answer (up to 5/day) |

---

## 🔐 Security Notes

- **Change `ADMIN_KEY`** — the default `"change-me"` value must be replaced in production.
- **VAPID keys** — generate with `pywebpush` or `openssl`; never commit them.
- **Rate limiting** — the SMS bot enforces per-user question limits to prevent abuse.
- **Topic filtering** — a Llama 3 classifier blocks non-civic questions before they consume tokens.
- **Source allowlisting** — news is only fetched from a curated list of neutral outlets.

---

## 🗓️ Scheduled Jobs

Both apps run an `APScheduler` background job every **hour** that:

1. Identifies users/subscribers who haven't received a digest in 48+ hours
2. Fetches national + local news for each unique location
3. Generates a structured AI digest via Groq/Llama 3
4. Sends push notifications (PWA) or SMS messages (bot)
5. Updates `last_notified_at` / `last_digest_at` to prevent double-sends

---

## 🤝 Contributing

Contributions are welcome! Here's how:

1. Fork the repo and create a feature branch
2. Make your changes with clear commit messages
3. Test locally against a PostgreSQL instance
4. Open a pull request describing what you changed and why

Please keep the project's core principle in mind: **strict neutrality**. Changes to news sources, LLM prompts, or answer generation should not introduce political bias.

---

## 📄 License

This project is open source. See [LICENSE](LICENSE) for details.

---

<p align="center">Built to make civic participation easier for everyone 🇺🇸</p>

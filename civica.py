import os
import re
import json
import logging
import httpx
import asyncpg
from datetime import datetime, timezone
from threading import Thread

from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler
import asyncio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

DATABASE_URL        = os.getenv("DATABASE_URL", "postgresql://localhost/civicbot")
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY")
BRAVE_API_KEY       = os.getenv("BRAVE_API_KEY")
NEWS_API_KEY        = os.getenv("NEWS_API_KEY")
ADMIN_KEY           = os.getenv("ADMIN_KEY", "change-me")
QUESTIONS_PER_DAY   = int(os.getenv("QUESTIONS_PER_DAY", "5"))

GROQ_URL  = "https://api.groq.com/openai/v1/chat/completions"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

TRUSTED_SOURCES    = "reuters.com,apnews.com,npr.org,pbs.org,bbc.com,politico.com,thehill.com"
TRUSTED_SOURCE_IDS = "reuters,associated-press,npr,bbc-news,politico"

# ─── DATABASE ─────────────────────────────────────────────────────────────────

async def get_conn():
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    conn = await get_conn()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                phone TEXT UNIQUE NOT NULL,
                location TEXT NOT NULL,
                state TEXT,
                subscribed BOOL DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                last_digest_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rate_limits (
                phone TEXT PRIMARY KEY,
                question_count INT DEFAULT 0,
                window_start TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        logger.info("DB initialized")
    finally:
        await conn.close()

async def get_user(phone):
    conn = await get_conn()
    try:
        return await conn.fetchrow("SELECT * FROM users WHERE phone=$1", phone)
    finally:
        await conn.close()

async def upsert_user(phone, location, state=None):
    conn = await get_conn()
    try:
        return await conn.fetchrow(
            """INSERT INTO users (phone, location, state)
               VALUES ($1, $2, $3)
               ON CONFLICT (phone) DO UPDATE SET location=$2, state=$3
               RETURNING *""",
            phone, location, state
        )
    finally:
        await conn.close()

async def get_subscribed_users():
    conn = await get_conn()
    try:
        return await conn.fetch("SELECT * FROM users WHERE subscribed=TRUE")
    finally:
        await conn.close()

async def update_last_digest(phone):
    conn = await get_conn()
    try:
        await conn.execute("UPDATE users SET last_digest_at=NOW() WHERE phone=$1", phone)
    finally:
        await conn.close()

async def unsubscribe(phone):
    conn = await get_conn()
    try:
        await conn.execute("UPDATE users SET subscribed=FALSE WHERE phone=$1", phone)
    finally:
        await conn.close()

async def check_rate_limit(phone):
    """Returns (allowed, remaining)."""
    conn = await get_conn()
    try:
        row = await conn.fetchrow("SELECT * FROM rate_limits WHERE phone=$1", phone)
        now = datetime.now(timezone.utc)
        if row is None:
            await conn.execute(
                "INSERT INTO rate_limits (phone, question_count, window_start) VALUES ($1, 1, $2)",
                phone, now
            )
            return True, QUESTIONS_PER_DAY - 1
        hours = (now - row["window_start"]).total_seconds() / 3600
        if hours >= 24:
            await conn.execute(
                "UPDATE rate_limits SET question_count=1, window_start=$2 WHERE phone=$1", phone, now
            )
            return True, QUESTIONS_PER_DAY - 1
        if row["question_count"] >= QUESTIONS_PER_DAY:
            return False, 0
        await conn.execute(
            "UPDATE rate_limits SET question_count=question_count+1 WHERE phone=$1", phone
        )
        return True, QUESTIONS_PER_DAY - row["question_count"] - 1
    finally:
        await conn.close()

# ─── NEWS ─────────────────────────────────────────────────────────────────────

async def fetch_news(location, state=None):
    if not NEWS_API_KEY:
        return [], []

    async with httpx.AsyncClient(timeout=10) as client:
        national = []
        try:
            r = await client.get("https://newsapi.org/v2/top-headlines", params={
                "sources": TRUSTED_SOURCE_IDS,
                "pageSize": 5,
                "apiKey": NEWS_API_KEY,
            })
            national = [
                {"title": a["title"], "description": a.get("description", ""), "source": a["source"]["name"]}
                for a in r.json().get("articles", []) if a.get("title")
            ]
        except Exception as e:
            logger.error(f"National news error: {e}")

        local = []
        try:
            query = f"{state} state legislature politics {location}" if state else f"{location} politics government"
            r = await client.get("https://newsapi.org/v2/everything", params={
                "q": query,
                "domains": TRUSTED_SOURCES,
                "sortBy": "publishedAt",
                "pageSize": 3,
                "language": "en",
                "apiKey": NEWS_API_KEY,
            })
            local = [
                {"title": a["title"], "description": a.get("description", ""), "source": a["source"]["name"]}
                for a in r.json().get("articles", []) if a.get("title")
            ]
        except Exception as e:
            logger.error(f"Local news error: {e}")

    return national, local

# ─── LLM ──────────────────────────────────────────────────────────────────────

async def classify_message(message):
    """Groq/Llama: is this a civic/political question? Returns (is_civic, confidence)."""
    if not GROQ_API_KEY:
        return True, 0.5
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama3-8b-8192",
                    "messages": [
                        {"role": "system", "content": (
                            "You are a classifier. Is the user's message a civic or political question? "
                            "Civic topics: elections, voting, laws, government, policy, legislation, "
                            "civic rights, national security, public officials, political processes, etc. "
                            "Respond ONLY with valid JSON: {\"is_civic\": true/false, \"confidence\": 0.0-1.0}"
                        )},
                        {"role": "user", "content": f"Classify: {message}"}
                    ],
                    "max_tokens": 60,
                    "temperature": 0.1,
                }
            )
            raw = r.json()["choices"][0]["message"]["content"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            return result["is_civic"], result["confidence"]
    except Exception as e:
        logger.error(f"Classification error: {e}")
        return True, 0.5  # Fail open


async def brave_search(query, count=5):
    """Fetch top web results from Brave Search for Q&A context."""
    if not BRAVE_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(BRAVE_URL,
                headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
                params={"q": query, "count": count, "search_lang": "en",
                        "result_filter": "web", "freshness": "pw"},
            )
            results = r.json().get("web", {}).get("results", [])
            return [{"title": x["title"], "snippet": x.get("description", ""), "url": x["url"]}
                    for x in results]
    except Exception as e:
        logger.error(f"Brave search error: {e}")
        return []


async def groq_chat(system, user_msg, max_tokens=400, model="llama3-70b-8192"):
    """Generic Groq chat helper."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "system", "content": system},
                              {"role": "user", "content": user_msg}],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            }
        )
        return r.json()["choices"][0]["message"]["content"].strip()


async def generate_digest(national, local, location):
    """Groq/Llama: neutral SMS civic digest."""
    news_text = f"Location: {location}\n\nNATIONAL:\n"
    for a in national:
        news_text += f"- {a['title']} ({a['source']}): {a['description']}\n"
    if local:
        news_text += "\nLOCAL/STATE:\n"
        for a in local:
            news_text += f"- {a['title']} ({a['source']}): {a['description']}\n"

    if not GROQ_API_KEY:
        lines = [f"📰 Civic Update for {location}:"]
        for a in national[:3]:
            lines.append(f"• {a['title']} ({a['source']})")
        if local:
            lines.append("Local:")
            for a in local[:2]:
                lines.append(f"• {a['title']}")
        lines.append(f"\nReply with a civic question! ({QUESTIONS_PER_DAY}/day limit)")
        return "\n".join(lines)

    try:
        return await groq_chat(
            system=(
                "You are a neutral civic education assistant. Summarize news into a factual SMS digest. "
                "Be strictly neutral — no bias, no opinion, facts only. Under 900 chars total. "
                "Use • bullet points. Do not editorialize."
            ),
            user_msg=f"Create a civic digest:\n\n{news_text}",
            max_tokens=400,
        )
    except Exception as e:
        logger.error(f"Digest generation error: {e}")
        return f"📰 Civic Update for {location} — check Reuters, AP News, or NPR for today's top stories."


async def answer_question(question, location):
    """Groq/Llama + Brave Search: neutral civic Q&A."""
    if not GROQ_API_KEY:
        return "Sorry, I'm unable to answer questions right now. Try again later."

    # Search for current context using Brave
    search_query = f"{question} {location} site:reuters.com OR site:apnews.com OR site:npr.org OR site:bbc.com"
    results = await brave_search(search_query)

    context = ""
    if results:
        context = "\n\nCurrent search results for context:\n"
        for r in results[:4]:
            context += f"- {r['title']}: {r['snippet']} ({r['url']})\n"

    try:
        return await groq_chat(
            system=(
                "You are a neutral civic education assistant. Answer political/civic questions factually. "
                "Be strictly neutral — no bias, cite sources (Reuters, AP, NPR, PBS, BBC), "
                "present multiple perspectives on contested issues, keep under 800 chars for SMS. "
                "Never tell people how to vote or what to think politically. "
                f"User is in: {location}. Prioritize local/state info when relevant."
            ),
            user_msg=f"{question}{context}",
            max_tokens=500,
            model="llama3-70b-8192",
        )
    except Exception as e:
        logger.error(f"Q&A error: {e}")
        return "Sorry, I had trouble with that. Please try again shortly."

# ─── DIGEST SENDER ────────────────────────────────────────────────────────────

async def send_digest(phone, location, state=None):
    national, local = await fetch_news(location, state)
    if not national and not local:
        logger.warning(f"No news for {location}")
        return False
    digest = await generate_digest(national, local, location)
    if len(digest) > 1580:
        digest = digest[:1577] + "..."
    try:
        twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        twilio.messages.create(body=digest, from_=TWILIO_PHONE_NUMBER, to=phone)
        await update_last_digest(phone)
        logger.info(f"Digest sent to {phone}")
        return True
    except Exception as e:
        logger.error(f"Twilio send error: {e}")
        return False

async def run_digest_job():
    """Send digests to all users overdue by 48hrs."""
    users = await get_subscribed_users()
    now = datetime.now(timezone.utc)
    sent = 0
    for user in users:
        last = user["last_digest_at"]
        if last and (now - last).total_seconds() / 3600 < 48:
            continue
        await send_digest(user["phone"], user["location"], user["state"])
        sent += 1
    logger.info(f"Digest job: {sent} sent")

def digest_job_sync():
    asyncio.run(run_digest_job())

# ─── SCHEDULER ────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(digest_job_sync, "interval", hours=1, id="digest_job", max_instances=1)

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def twiml(message):
    resp = MessagingResponse()
    resp.message(message)
    return Response(str(resp), mimetype="application/xml")

def parse_location(text):
    """Returns (location_str, state) or (None, None)."""
    text = text.strip()
    if re.match(r"^\d{5}$", text):
        return text, None
    m = re.match(r"^([a-zA-Z\s]+),\s*([A-Z]{2})$", text)
    if m:
        return f"{m.group(1).strip()}, {m.group(2).strip()}", m.group(2).strip()
    return None, None

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/webhook/sms", methods=["POST"])
def sms_webhook():
    phone = request.form.get("From", "").strip()
    body  = request.form.get("Body", "").strip()

    if not phone or not body:
        return twiml("Sorry, I didn't receive your message.")

    logger.info(f"SMS from {phone}: {body[:60]}")

    async def handle():
        if body.upper() in ["STOP", "UNSUBSCRIBE", "QUIT", "CANCEL"]:
            await unsubscribe(phone)
            return "You've been unsubscribed from CivicBot. Reply START to resubscribe anytime."

        user = await get_user(phone)

        # New user
        if not user:
            location, state = parse_location(body)
            if not location:
                return (
                    f"👋 Welcome to CivicBot!\n\n"
                    f"You'll get neutral civic news every 48hrs + can ask up to {QUESTIONS_PER_DAY} civic questions/day.\n\n"
                    "Reply with your city & state (e.g. Austin, TX) or zip code to get started."
                )
            await upsert_user(phone, location, state)
            Thread(target=lambda: asyncio.run(send_digest(phone, location, state)), daemon=True).start()
            return f"✅ Registered! Sending your first civic digest for {location} now..."

        # Location update
        location, state = parse_location(body)
        if location and len(body) < 30:
            await upsert_user(phone, location, state)
            return f"✅ Location updated to {location}!"

        # Rate limit
        allowed, remaining = await check_rate_limit(phone)
        if not allowed:
            return f"⏳ You've hit your daily limit of {QUESTIONS_PER_DAY} questions. Resets in 24hrs."

        # Classify
        is_civic, confidence = await classify_message(body)
        if not is_civic and confidence > 0.75:
            return (
                "❌ I only answer civic & political questions.\n\n"
                "Try: 'How does Congress pass a bill?' or 'Who represents me in the Senate?'\n\n"
                f"({remaining} questions left today)"
            )

        # Answer
        answer = await answer_question(body, user["location"])
        if len(answer) > 1550:
            answer = answer[:1547] + "..."
        footer = f"\n\n({remaining} questions left today)"
        if len(answer) + len(footer) <= 1600:
            answer += footer
        return answer

    return twiml(asyncio.run(handle()))


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/admin/users")
def admin_users():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return {"error": "Unauthorized"}, 403
    users = asyncio.run(get_subscribed_users())
    return {"count": len(users), "users": [dict(u) for u in users]}


@app.route("/admin/send-digest-now", methods=["POST"])
def admin_trigger_digest():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return {"error": "Unauthorized"}, 403
    Thread(target=digest_job_sync, daemon=True).start()
    return {"status": "triggered"}

# ─── STARTUP ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(init_db())
    scheduler.start()
    logger.info("CivicBot starting...")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
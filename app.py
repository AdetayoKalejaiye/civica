import os
import json
import logging
import asyncio
import httpx
import asyncpg
from datetime import datetime, timezone
from threading import Thread

from flask import Flask, request, jsonify, render_template, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
from pywebpush import webpush, WebPushException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

DATABASE_URL      = os.getenv("DATABASE_URL", "postgresql://localhost/civicpwa")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")
NEWS_API_KEY      = os.getenv("NEWS_API_KEY")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY")
VAPID_EMAIL       = os.getenv("VAPID_EMAIL", "mailto:admin@civicbot.app")
ADMIN_KEY         = os.getenv("ADMIN_KEY", "change-me")

GROQ_URL         = "https://api.groq.com/openai/v1/chat/completions"
TRUSTED_SOURCES  = "reuters.com,apnews.com,npr.org,pbs.org,bbc.com,politico.com,thehill.com"
TRUSTED_IDS      = "reuters,associated-press,npr,bbc-news,politico"

# ─── DATABASE ─────────────────────────────────────────────────────────────────

async def get_conn():
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    conn = await get_conn()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS digests (
                id SERIAL PRIMARY KEY,
                location TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id SERIAL PRIMARY KEY,
                endpoint TEXT UNIQUE NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                location TEXT DEFAULT 'National',
                state TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                last_notified_at TIMESTAMPTZ
            )
        """)
        logger.info("DB initialized")
    finally:
        await conn.close()

async def save_digest(location, content):
    conn = await get_conn()
    try:
        await conn.execute("INSERT INTO digests (location, content) VALUES ($1, $2)", location, content)
    finally:
        await conn.close()

async def get_recent_digests(location="National", limit=20):
    conn = await get_conn()
    try:
        return await conn.fetch(
            "SELECT * FROM digests WHERE location=$1 ORDER BY created_at DESC LIMIT $2",
            location, limit
        )
    finally:
        await conn.close()

async def save_subscription(endpoint, p256dh, auth, location="National", state=None):
    conn = await get_conn()
    try:
        await conn.execute("""
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, location, state)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (endpoint) DO UPDATE SET location=$4, state=$5
        """, endpoint, p256dh, auth, location, state)
    finally:
        await conn.close()

async def get_all_subscriptions():
    conn = await get_conn()
    try:
        return await conn.fetch("SELECT * FROM push_subscriptions")
    finally:
        await conn.close()

async def delete_subscription(endpoint):
    conn = await get_conn()
    try:
        await conn.execute("DELETE FROM push_subscriptions WHERE endpoint=$1", endpoint)
    finally:
        await conn.close()

async def update_last_notified(endpoint):
    conn = await get_conn()
    try:
        await conn.execute("UPDATE push_subscriptions SET last_notified_at=NOW() WHERE endpoint=$1", endpoint)
    finally:
        await conn.close()

# ─── NEWS ─────────────────────────────────────────────────────────────────────

async def fetch_news(location="National", state=None):
    if not NEWS_API_KEY:
        return [], []
    async with httpx.AsyncClient(timeout=10) as client:
        national = []
        try:
            r = await client.get("https://newsapi.org/v2/top-headlines", params={
                "sources": TRUSTED_IDS, "pageSize": 6, "apiKey": NEWS_API_KEY,
            })
            national = [{"title": a["title"], "description": a.get("description",""), "source": a["source"]["name"]}
                        for a in r.json().get("articles",[]) if a.get("title")]
        except Exception as e:
            logger.error(f"National news error: {e}")

        local = []
        if state and location != "National":
            try:
                r = await client.get("https://newsapi.org/v2/everything", params={
                    "q": f"{state} state legislature politics {location}",
                    "domains": TRUSTED_SOURCES, "sortBy": "publishedAt",
                    "pageSize": 4, "language": "en", "apiKey": NEWS_API_KEY,
                })
                local = [{"title": a["title"], "description": a.get("description",""), "source": a["source"]["name"]}
                         for a in r.json().get("articles",[]) if a.get("title")]
            except Exception as e:
                logger.error(f"Local news error: {e}")
    return national, local

# ─── LLM ──────────────────────────────────────────────────────────────────────

async def groq_chat(system, user_msg, max_tokens=800):
    if not GROQ_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "system", "content": system},
                              {"role": "user", "content": user_msg}],
                "max_tokens": max_tokens, "temperature": 0.3,
            }
        )
        return r.json()["choices"][0]["message"]["content"].strip()

async def generate_digest(national, local, location):
    news_text = f"Location: {location}\n\nNATIONAL:\n"
    for a in national:
        news_text += f"- {a['title']} ({a['source']}): {a['description']}\n"
    if local:
        news_text += "\nLOCAL/STATE:\n"
        for a in local:
            news_text += f"- {a['title']} ({a['source']}): {a['description']}\n"

    result = await groq_chat(
        system=(
            "You are a neutral civic education assistant. Return ONLY valid JSON, no markdown, no explanation. "
            'Structure: {"headline": "8-word summary", "summary": "2-3 sentence neutral overview", '
            '"stories": [{"title": "...", "body": "2-3 sentence summary", "source": "...", "category": "National|Local|Policy|Election"}], '
            '"civic_fact": "one interesting civic education fact related to todays news"}'
        ),
        user_msg=f"Create a civic digest:\n\n{news_text}",
    )

    if result:
        try:
            return json.loads(result.replace("```json","").replace("```","").strip())
        except Exception:
            pass

    # Fallback
    stories = [{"title": a["title"], "body": a["description"], "source": a["source"], "category": "National"} for a in national[:4]]
    if local:
        stories += [{"title": a["title"], "body": a["description"], "source": a["source"], "category": "Local"} for a in local[:2]]
    return {
        "headline": f"Civic Update — {location}",
        "summary": "Today's top civic and political news from neutral sources.",
        "stories": stories,
        "civic_fact": "The US Constitution has been amended 27 times since its ratification in 1788."
    }

# ─── PUSH ─────────────────────────────────────────────────────────────────────

def send_push(subscription, payload):
    try:
        webpush(
            subscription_info={"endpoint": subscription["endpoint"],
                                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]}},
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_EMAIL},
        )
        return True
    except WebPushException as e:
        if "410" in str(e) or "404" in str(e):
            asyncio.run(delete_subscription(subscription["endpoint"]))
        logger.error(f"Push error: {e}")
        return False

async def run_digest_and_notify():
    subscriptions = await get_all_subscriptions()
    now = datetime.now(timezone.utc)
    locations = {}
    for sub in subscriptions:
        last = sub["last_notified_at"]
        if last and (now - last).total_seconds() / 3600 < 48:
            continue
        loc = sub["location"] or "National"
        if loc not in locations:
            locations[loc] = {"state": sub["state"], "subs": []}
        locations[loc]["subs"].append(sub)

    for location, data in locations.items():
        national, local = await fetch_news(location, data["state"])
        digest = await generate_digest(national, local, location)
        await save_digest(location, json.dumps(digest))
        payload = {"title": "📰 " + digest["headline"], "body": digest["summary"],
                   "tag": "civic-digest", "renotify": True}
        for sub in data["subs"]:
            Thread(target=send_push, args=(dict(sub), payload), daemon=True).start()
            await update_last_notified(sub["endpoint"])

    logger.info(f"Digest job: {len(locations)} locations")

def digest_job_sync():
    asyncio.run(run_digest_and_notify())

scheduler = BackgroundScheduler()
scheduler.add_job(digest_job_sync, "interval", hours=1, id="digest_job", max_instances=1)

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", vapid_public_key=VAPID_PUBLIC_KEY or "")

@app.route("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")

@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")

@app.route("/api/updates")
def get_updates():
    location = request.args.get("location", "National")
    digests = asyncio.run(get_recent_digests(location))
    result = []
    for d in digests:
        try:
            result.append({"id": d["id"], "location": d["location"],
                           "created_at": d["created_at"].isoformat(),
                           "content": json.loads(d["content"])})
        except Exception:
            pass
    return jsonify(result)

@app.route("/api/subscribe", methods=["POST"])
def subscribe():
    data = request.json
    sub = data.get("subscription", {})
    endpoint = sub.get("endpoint")
    p256dh = sub.get("keys", {}).get("p256dh")
    auth = sub.get("keys", {}).get("auth")
    if not all([endpoint, p256dh, auth]):
        return jsonify({"error": "Invalid subscription"}), 400
    asyncio.run(save_subscription(endpoint, p256dh, auth, data.get("location","National"), data.get("state")))
    return jsonify({"status": "subscribed"})

@app.route("/api/unsubscribe", methods=["POST"])
def unsubscribe_route():
    data = request.json
    if data and data.get("endpoint"):
        asyncio.run(delete_subscription(data["endpoint"]))
    return jsonify({"status": "unsubscribed"})

@app.route("/api/generate-digest", methods=["POST"])
def generate_digest_now():
    """Generate a fresh digest on demand (for first load)."""
    data = request.json or {}
    location = data.get("location", "National")
    state = data.get("state")

    async def run():
        national, local = await fetch_news(location, state)
        if not national and not local:
            return None
        digest = await generate_digest(national, local, location)
        await save_digest(location, json.dumps(digest))
        return digest

    digest = asyncio.run(run())
    if not digest:
        return jsonify({"error": "Could not fetch news"}), 503
    return jsonify({"id": -1, "location": location,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "content": digest})


@app.route("/api/ask", methods=["POST"])
def ask_question():
    """Answer a civic question using Groq/Llama."""
    data = request.json or {}
    question = data.get("question", "").strip()
    location = data.get("location", "National")

    if not question:
        return jsonify({"error": "No question provided"}), 400

    async def run():
        # Classify first
        classify_result = await groq_chat(
            system=(
                "You are a classifier. Is this a civic or political question? "
                "Civic topics: elections, voting, laws, government, policy, legislation, "
                "civic rights, national security, public officials, political processes. "
                "Respond ONLY with valid JSON: {\"is_civic\": true/false, \"confidence\": 0.0-1.0}"
            ),
            user_msg=f"Classify: {question}",
            max_tokens=60,
        )
        try:
            result = json.loads(classify_result.replace("```json","").replace("```","").strip())
            if not result["is_civic"] and result["confidence"] > 0.75:
                return {"answer": None, "not_civic": True}
        except Exception:
            pass

        # Answer
        answer = await groq_chat(
            system=(
                "You are a neutral civic education assistant. Answer political and civic questions factually. "
                "Be strictly neutral — no bias, no opinion. Cite sources where possible (Reuters, AP, NPR, PBS, BBC). "
                "Present multiple perspectives on contested issues. Never tell people how to vote or what to think. "
                f"The user is located in: {location}. Prioritize relevant local or state info when applicable."
            ),
            user_msg=question,
            max_tokens=500,
        )
        return {"answer": answer, "not_civic": False}

    try:
        result = asyncio.run(run())
        return jsonify(result)
    except Exception as e:
        logger.error(f"/api/ask error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/admin/trigger-digest", methods=["POST"])
def trigger_digest():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 403
    Thread(target=digest_job_sync, daemon=True).start()
    return jsonify({"status": "triggered"})

# ─── STARTUP ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(init_db())
    scheduler.start()
    logger.info("CivicBot PWA starting...")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=False)

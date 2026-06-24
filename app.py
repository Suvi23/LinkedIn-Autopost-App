"""
LinkedIn Content Generator - Web Application
Researches topics and generates engaging LinkedIn content using Groq AI
Multi-user support · Free AI image generation · DuckDuckGo search
"""

import os, time, re, textwrap, json, base64, io
from urllib.parse import urlencode

from flask import Flask, render_template, request, jsonify, redirect, session
from flask_session import Session
from ddgs import DDGS
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from groq import Groq
from PIL import Image

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "super-secret-key-change-me")
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
Session(app)

# ─── API Keys ────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
GROQ_MODEL = "llama-3.3-70b-versatile"

LINKEDIN_CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID", "")
LINKEDIN_CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET", "")
LINKEDIN_REDIRECT_URI = os.getenv("LINKEDIN_REDIRECT_URI", "http://localhost:5000/auth/linkedin/callback")

# ─── Config ──────────────────────────────────────────────────────────────────
CONFIG = {
    "max_search_results": 8,
    "max_content_chars": 3000,
    "request_timeout": 15,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

SESSION_HTTP = requests.Session()
SESSION_HTTP.headers.update({"User-Agent": CONFIG["user_agent"]})


# ═══════════════════════════════════════════════════════════════════════════
#  FREE AI IMAGE GENERATION  (Pollinations.ai – no API key required)
# ═══════════════════════════════════════════════════════════════════════════

def generate_image(topic: str) -> str:
    """Generate an image using Pollinations.ai free API. Returns base64 data URI."""
    prompt = f"Professional, clean, modern illustration about {topic}, suitable for LinkedIn post, flat design style, high quality, 4k"
    try:
        resp = requests.get(
            f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}?width=1024&height=1024&nologo=true",
            timeout=20
        )
        if resp.status_code == 200:
            img_data = base64.b64encode(resp.content).decode("utf-8")
            return f"data:image/jpeg;base64,{img_data}"
    except:
        pass
    return ""


# ═══════════════════════════════════════════════════════════════════════════
#  GROQ AI HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def groq_chat(system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
    if not groq_client:
        return "GROQ_API_KEY not set."
    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL, temperature=temperature, max_tokens=4096,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Groq API error: {e}"


def clean_text(text: str) -> str:
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text)
    text = text.replace('*', '')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def generate_with_groq(topic: str, insights: dict, style: str, content_type: str) -> str:
    stats = insights.get("stats", [])
    key_points = insights.get("key_points", [])
    sources = insights.get("sources", [])

    research_context = f"Topic: {topic}\n\n"
    if stats:
        research_context += "Key Statistics:\n" + "\n".join(f"- {s}" for s in stats[:8]) + "\n\n"
    if key_points:
        research_context += "Key Insights:\n" + "\n".join(f"- {p}" for p in key_points[:8]) + "\n\n"
    if sources:
        research_context += "Sources:\n" + "\n".join(f"- {s['title']}" for s in sources[:5]) + "\n\n"

    tone_map = {
        "storytelling": "Weave a personal story, anecdote, or narrative.",
        "professional": "Keep it polished, data-driven, and authoritative.",
        "casual": "Conversational and relatable. Use emojis, short paragraphs.",
        "educational": "Break it down step-by-step. Teach something valuable.",
        "inspiring": "Uplifting, future-focused, and motivational.",
    }
    format_map = {
        "post": "Write a single LinkedIn post (800-1500 chars). Start with a hook. End with CTA and hashtags.",
        "carousel": "Write a 5-7 slide carousel. Label each slide clearly.",
        "article": "Write a LinkedIn article (500-700 words) with headline, subheadings, conclusion.",
    }

    system_prompt = """You are a top LinkedIn content strategist.
CRITICAL RULES:
1. NEVER use asterisks (*) for formatting. Plain text only.
2. Hook the reader in the first 2 lines.
3. Use short paragraphs with line breaks.
4. Add 2-5 relevant hashtags at the end (for posts/carousels).
5. End with a question or call-to-action.
6. Sound human - conversational, not robotic."""

    user_prompt = f"""TOPIC: {topic}
TONE: {tone_map.get(style.lower(), tone_map["professional"])}
FORMAT: {format_map.get(content_type.lower(), format_map["post"])}

RESEARCH DATA:
{research_context}

NO asterisks (*). Plain text only."""

    return clean_text(groq_chat(system_prompt, user_prompt, temperature=0.8))


def generate_trending_suggestions(topic: str) -> list:
    if not groq_client:
        return ["GROQ_API_KEY not set"]
    system_prompt = "You are a content trend analyst. Suggest 5 trending/related article ideas. Format: 'Title: description'. No asterisks. Keep descriptions under 15 words."
    try:
        raw = clean_text(groq_chat(system_prompt, f"Topic: {topic}\n\nSuggest 5 trending article ideas.", temperature=0.6))
        suggestions = []
        for line in raw.split('\n'):
            line = re.sub(r'^[\d\.\-\s]+', '', line).strip()
            if line and len(line) > 10:
                suggestions.append(line)
        return suggestions[:6]
    except:
        return ["Could not generate suggestions."]


# ═══════════════════════════════════════════════════════════════════════════
#  RESEARCH ENGINE  (DuckDuckGo – works without API key)
# ═══════════════════════════════════════════════════════════════════════════

def research_topic(topic: str) -> dict:
    """Search DuckDuckGo and scrape content from results."""
    results = []
    try:
        with DDGS() as ddgs:
            ddgs_results = list(ddgs.text(topic, max_results=CONFIG["max_search_results"]))
    except Exception as e:
        return {"error": f"Search failed: {str(e)}", "results": []}

    for item in ddgs_results:
        url = item.get("href", "")
        title = item.get("title", url)
        body_snippet = item.get("body", "")
        if not url:
            continue
        # Try to fetch full page content
        try:
            resp = SESSION_HTTP.get(url, timeout=CONFIG["request_timeout"])
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            text = re.sub(r'\s+', ' ', text)[:CONFIG["max_content_chars"]]
            results.append({"url": url, "title": title, "content": text})
        except:
            # If scraping fails, use the snippet from search results
            if body_snippet:
                results.append({"url": url, "title": title, "content": body_snippet[:CONFIG["max_content_chars"]]})

    return {"topic": topic, "results": results, "total_sources": len(results)}


def extract_key_insights(research_data: dict) -> dict:
    all_content = " ".join([r["content"] for r in research_data.get("results", [])])

    stats = re.findall(r'\b\d{2,}%|\b\d+[,\d]*\s*(?:million|billion|trillion|users|people|dollars|%|x)\b', all_content)
    stats = list(set(stats))[:10]

    sentences = re.split(r'[.!?]+', all_content)
    important = []
    for s in sentences:
        s = s.strip()
        if 50 < len(s) < 300 and any(kw in s.lower() for kw in [
            "key", "important", "significant", "according", "study", "research",
            "found", "revealed", "trend", "breakthrough", "innovation",
            "top", "leading", "expert"
        ]):
            important.append(s)
    important = important[:15]

    sources = [{"title": r["title"], "url": r["url"]} for r in research_data.get("results", [])[:5]]

    return {"stats": stats, "key_points": important, "sources": sources, "raw_content": all_content[:5000]}


# ═══════════════════════════════════════════════════════════════════════════
#  LINKEDIN OAUTH – Multi-user via Flask session
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/auth/linkedin/login")
def linkedin_login():
    if not LINKEDIN_CLIENT_ID:
        return jsonify({"error": "LinkedIn Client ID not configured"}), 400
    state = os.urandom(16).hex()
    session["linkedin_state"] = state
    params = {"response_type": "code", "client_id": LINKEDIN_CLIENT_ID,
              "redirect_uri": LINKEDIN_REDIRECT_URI,
              "scope": "openid profile email w_member_social", "state": state}
    return redirect(f"https://www.linkedin.com/oauth/v2/authorization?{urlencode(params)}")


@app.route("/auth/linkedin/callback")
def linkedin_callback():
    code = request.args.get("code")
    error = request.args.get("error")
    state = request.args.get("state")
    if error:
        return jsonify({"error": f"LinkedIn OAuth error: {error}"}), 400
    if not code:
        return jsonify({"error": "No authorization code"}), 400
    # Verify state
    saved_state = session.pop("linkedin_state", None)
    if saved_state and state != saved_state:
        return jsonify({"error": "State mismatch. Possible CSRF."}), 400

    try:
        resp = requests.post("https://www.linkedin.com/oauth/v2/accessToken", data={
            "grant_type": "authorization_code", "code": code,
            "client_id": LINKEDIN_CLIENT_ID, "client_secret": LINKEDIN_CLIENT_SECRET,
            "redirect_uri": LINKEDIN_REDIRECT_URI,
        }, timeout=10)
        resp.raise_for_status()
        token_data = resp.json()
        access_token = token_data.get("access_token")
        headers = {"Authorization": f"Bearer {access_token}"}
        user_resp = requests.get("https://api.linkedin.com/v2/userinfo", headers=headers, timeout=10)
        user_resp.raise_for_status()
        user_info = user_resp.json()

        # Store in session (multi-user safe – each session has its own token)
        session["linkedin_token"] = access_token
        session["linkedin_user_id"] = user_info.get("sub", "unknown")
        session["linkedin_name"] = user_info.get("name", "User")

        return f"""
        <html><body style="font-family:Inter,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;background:#f8fafc;">
        <div style="text-align:center;background:white;padding:48px;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,0.06);max-width:400px;">
            <h1 style="color:#0a66c2;">&#10003; LinkedIn Connected!</h1>
            <p>You can now auto-post to your LinkedIn account.</p>
            <p style="color:#5e6872;">Connected as: {user_info.get('name', 'User')}</p>
            <br><a href="/" style="display:inline-block;padding:12px 24px;background:#0a66c2;color:white;text-decoration:none;border-radius:8px;font-weight:600;">Back to App</a>
        </div></body></html>"""
    except Exception as e:
        return jsonify({"error": f"Token exchange failed: {str(e)}"}), 500


@app.route("/api/linkedin/status")
def linkedin_status():
    token = session.get("linkedin_token")
    name = session.get("linkedin_name")
    return jsonify({"connected": bool(token), "name": name})


@app.route("/api/post-to-linkedin", methods=["POST"])
def post_to_linkedin():
    data = request.get_json()
    if not data or "content" not in data:
        return jsonify({"error": "No content provided"}), 400
    content = data["content"].strip()
    access_token = session.get("linkedin_token")
    user_id = session.get("linkedin_user_id")
    if not access_token or not user_id:
        return jsonify({"error": "Not connected. Click 'Connect LinkedIn' first.", "needs_auth": True}), 401

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json",
               "X-Restli-Protocol-Version": "2.0.0", "LinkedIn-Version": "202401"}
    post_data = {"author": f"urn:li:person:{user_id}", "commentary": content,
                 "visibility": "PUBLIC", "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
                 "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    try:
        resp = requests.post("https://api.linkedin.com/v2/posts", headers=headers, json=post_data, timeout=15)
        post_id = "unknown"
        try:
            resp_json = resp.json()
            if resp_json and "id" in resp_json:
                post_id = resp_json["id"]
            elif resp.status_code in (200, 201):
                loc = resp.headers.get("Location", "")
                if loc: post_id = loc.split("/")[-1]
        except: pass
        if resp.status_code in (200, 201):
            return jsonify({"success": True, "message": "Posted to LinkedIn successfully!", "post_id": post_id})
        elif resp.status_code == 422 and "DUPLICATE_POST" in resp.text:
            return jsonify({"success": True, "message": "Already posted! This content is already on your LinkedIn feed.", "duplicate": True})
        else:
            if resp.status_code == 401:
                session.pop("linkedin_token", None)
                session.pop("linkedin_user_id", None)
                return jsonify({"error": "LinkedIn session expired. Reconnect.", "needs_auth": True}), 401
            return jsonify({"error": f"LinkedIn API error ({resp.status_code})"}), 500
    except Exception as e:
        return jsonify({"error": f"Failed to post: {str(e)}"}), 500


@app.route("/api/logout-linkedin", methods=["POST"])
def logout_linkedin():
    session.pop("linkedin_token", None)
    session.pop("linkedin_user_id", None)
    session.pop("linkedin_name", None)
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN APP ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html",
                           linkedin_configured=bool(LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET))


@app.route("/api/research", methods=["POST"])
def api_research():
    data = request.get_json()
    if not data or "topic" not in data:
        return jsonify({"error": "Provide a topic"}), 400
    topic = data["topic"].strip()
    style = data.get("style", "professional").strip().lower()
    content_type = data.get("content_type", "post").strip().lower()
    if not topic:
        return jsonify({"error": "Topic empty"}), 400
    include_image = data.get("include_image", False)

    try:
        research_data = research_topic(topic)
        if "error" in research_data:
            return jsonify({"error": research_data["error"]}), 500

        insights = extract_key_insights(research_data)
        groq_content = generate_with_groq(topic, insights, style, content_type)

        # Generate image if requested
        image_data_url = ""
        if include_image:
            image_data_url = generate_image(topic)

        return jsonify({
            "success": True, "topic": topic, "style": style, "content_type": content_type,
            "research": {
                "total_sources": research_data["total_sources"],
                "sources": insights["sources"][:5],
                "key_insights": insights["key_points"][:5],
                "statistics": insights["stats"][:8]
            },
            "content": {"type": content_type, "raw": groq_content},
            "image": image_data_url,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trending", methods=["POST"])
def api_trending():
    data = request.get_json()
    if not data or "topic" not in data:
        return jsonify({"error": "Provide a topic"}), 400
    topic = data["topic"].strip()
    if not topic:
        return jsonify({"error": "Topic empty"}), 400
    suggestions = generate_trending_suggestions(topic)
    return jsonify({"topic": topic, "suggestions": suggestions})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    print("=" * 40)
    print("  LinkedIn Content Generator")
    print("=" * 40)
    print(f"  Groq: {'Connected' if groq_client else 'API key missing'}")
    print(f"  LinkedIn: {'Configured' if LINKEDIN_CLIENT_ID else 'Not configured'}")
    print(f"  Open: http://localhost:5000")
    print("=" * 40)
    app.run(debug=True, host="0.0.0.0", port=5000)
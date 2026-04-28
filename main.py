import requests, os, json, hashlib
from datetime import datetime, timedelta

import google.generativeai as genai

genai.configure(api_key=os.environ["GEMINI_KEY"])
model = genai.GenerativeModel("gemini-2.5-flash")

def fetch_news():
    url = "https://gnews.io/api/v4/top-headlines"
    params = {
        "token": os.environ["GNEWS_KEY"],
        "lang":  "en",
        "max":   "10",
        "topic": "world",  # or: breaking-news, technology, sports
    }
    articles = requests.get(url, params=params).json()["articles"]

    # Score each article: recency + has image + trusted source
    def score(a):
        age = (datetime.utcnow() - datetime.fromisoformat(
                 a["publishedAt"].replace("Z",""))).seconds / 3600
        s = max(0, 10 - age)                      # newer = higher score
        s += 4 if a.get("image") else 0          # image available
        s += 3 if len(a["content"]) > 200 else 0  # enough content
        return s

    best = sorted(articles, key=score, reverse=True)[0]
    return best



def generate_content(article):
    prompt = f"""
You are a news social media editor. Given this article, return ONLY
valid JSON — no markdown, no backticks, no explanation.

Article title: {article['title']}
Article summary: {article['description']}

Return JSON with exactly these fields:
- "headline": 6-word punchy graphic headline (all caps)
- "caption": 2 sentence engaging summary, conversational, max 1 emoji
- "cta": one sentence call-to-action (e.g. "Save this for later 📌")
- "hashtags": list of 8 relevant hashtags as strings
- "graphic_color": one of: "#1e293b", "#7f1d1d", "#1e3a5f", "#14532d"
"""

    import re

    response = model.generate_content(prompt)

    raw = response.text.strip()

    # Remove ```json ... ``` or ``` ... ``` if Gemini adds markdown
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    # Extract first JSON object from the response
    match = re.search(r"\{.*\}", raw, re.DOTALL)

    if not match:
        raise ValueError(f"Gemini did not return JSON. Raw response: {raw}")

    return json.loads(match.group(0))


from PIL import Image, ImageDraw, ImageFont, ImageFilter
import io, textwrap

def fetch_bg_image(keyword):
    # Pexels API — free, no attribution required
    headers = {"Authorization": os.environ["PEXELS_KEY"]}
    r = requests.get(f"https://api.pexels.com/v1/search?query={keyword}&per_page=1",
                     headers=headers)
    photo_url = r.json()["photos"][0]["src"]["large2x"]
    img_data = requests.get(photo_url).content
    return Image.open(io.BytesIO(img_data)).convert("RGB")

def create_image(content, keyword):
    SIZE = (1080, 1080)
    bg = fetch_bg_image(keyword).resize(SIZE)

    # Darken with a semi-transparent overlay
    overlay = Image.new("RGBA", SIZE, (0, 0, 0, 170))
    bg = bg.convert("RGBA")
    bg.paste(overlay, mask=overlay)

    draw = ImageDraw.Draw(bg)
    # Use a bold font (download Roboto-Bold.ttf to repo)
    font_big = ImageFont.truetype("Roboto-Bold.ttf", 72)
    font_src = ImageFont.truetype("Roboto-Regular.ttf", 32)

    # Wrap and draw headline
    headline = content["headline"]
    lines = textwrap.wrap(headline, width=16)
    y = 380
    for line in lines:
        draw.text((80, y), line, font=font_big, fill="white")
        y += 90

    # Source label at bottom
    draw.text((80, 960), "YOUR NEWS PAGE  •  @handle",
              font=font_src, fill="rgba(255,255,255,0.7)")

    path = "/tmp/post.jpg"
    bg.convert("RGB").save(path, quality=95)
    return path


import time

def upload_image_free(filepath):
    # ImgBB — free image hosting with API (no login needed)
    with open(filepath, "rb") as f:
        r = requests.post("https://api.imgbb.com/1/upload",
            data={"key": os.environ["IMGBB_KEY"]},
            files={"image": f})
    return r.json()["data"]["url"]

def publish_to_instagram(image_path, content):
    BASE = f"https://graph.facebook.com/v21.0/{os.environ['IG_USER_ID']}"
    TOKEN = os.environ["IG_TOKEN"]
    caption = (content["caption"] + "\n\n" + content["cta"] +
               "\n\n" + " ".join(content["hashtags"]))

    # Step 1: Upload image to free host
    image_url = upload_image_free(image_path)

    # Step 2: Create media container
    r1 = requests.post(f"{BASE}/media", data={
        "image_url": image_url,
        "caption": caption,
        "access_token": TOKEN
    }).json()
    container_id = r1["id"]

    # Step 3: Wait for container to process (poll)
    for _ in range(10):
        status = requests.get(f"https://graph.facebook.com/v21.0/{container_id}",
            params={"fields":"status_code","access_token":TOKEN}).json()
        if status.get("status_code") == "FINISHED": break
        time.sleep(3)

    # Step 4: Publish
    r2 = requests.post(f"{BASE}/media_publish", data={
        "creation_id": container_id,
        "access_token": TOKEN
    }).json()
    return r2.get("id")


import gspread
from google.oauth2.service_account import Credentials

def log_to_sheets(article, content, post_id, status):
    creds_json = json.loads(os.environ["GSHEET_CREDS"])

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open("Instagram Bot Log").sheet1

    sheet.append_row([
        datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        article["title"][:80],
        content["headline"],
        post_id or "FAILED",
        status,
        article.get("url", "")
    ])
# ─── MAIN ENTRY POINT ───────────────────────────────────────────
if __name__ == "__main__":
    article = fetch_news()
    content = generate_content(article)
    img_path = create_image(content, article["title"].split()[0])
    post_id  = publish_to_instagram(img_path, content)
    log_to_sheets(article, content, post_id, "success" if post_id else "failed")
    print(f"✓ Posted: {content['headline']}")
import base64
import io
import json
import os
import random
import re
import textwrap
import time
from datetime import datetime, timezone

import gspread
import requests
from google import genai
from google.oauth2.service_account import Credentials
from PIL import Image, ImageDraw, ImageFont


GNEWS_URL = "https://gnews.io/api/v4/top-headlines"
GEMINI_MODEL = "gemini-2.5-flash"
INSTAGRAM_API_VERSION = "v25.0"
POST_IMAGE_PATH = "/tmp/post.jpeg"

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def fetch_news():
    params = {
        "token": os.environ["GNEWS_KEY"],
        "lang": "en",
        "max": "10",
        "topic": "world",
    }

    response = requests.get(GNEWS_URL, params=params, timeout=30)
    response.raise_for_status()

    data = response.json()
    articles = data.get("articles", [])

    if not articles:
        raise RuntimeError("No articles found from GNews.")

    def score(article):
        published_at = article.get("publishedAt")

        try:
            published_dt = datetime.fromisoformat(
                published_at.replace("Z", "+00:00")
            )
            age_hours = (
                datetime.now(timezone.utc) - published_dt
            ).total_seconds() / 3600
        except Exception:
            age_hours = 24

        article_content = article.get("content") or ""

        score_value = max(0, 10 - age_hours)
        score_value += 4 if article.get("image") else 0
        score_value += 3 if len(article_content) > 200 else 0

        return score_value

    best_article = sorted(articles, key=score, reverse=True)[0]
    return best_article


def generate_content(article):
    prompt = f"""
You are a news social media editor. Given this article, return ONLY
valid JSON, no markdown, no backticks, no explanation.

Article title: {article.get("title", "")}
Article summary: {article.get("description", "")}

Return JSON with exactly these fields:
- "headline": 6-word punchy graphic headline, all caps
- "caption": 2 sentence engaging summary, conversational, max 1 emoji
- "cta": one sentence call-to-action, example: "Save this for later 📌"
- "hashtags": list of 8 relevant hashtags as strings
- "graphic_color": one of: "#1e293b", "#7f1d1d", "#1e3a5f", "#14532d"
"""

    max_retries = 5

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )

            if not response.text:
                raise ValueError("Gemini returned an empty response.")

            raw = response.text.strip()

            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"^```\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

            match = re.search(r"\{.*\}", raw, re.DOTALL)

            if not match:
                raise ValueError(f"Gemini did not return JSON. Raw response: {raw}")

            content = json.loads(match.group(0))
            validate_generated_content(content)

            return content

        except Exception as error:
            error_text = str(error).lower()

            is_quota_error = (
                "429" in error_text
                or "resource_exhausted" in error_text
                or "quota" in error_text
                or "rate limit" in error_text
            )

            if is_quota_error and attempt < max_retries - 1:
                wait_time = 65 + random.randint(1, 15)
                print(
                    f"Gemini quota reached. Waiting {wait_time} seconds before retry..."
                )
                time.sleep(wait_time)
                continue

            raise

    raise RuntimeError("Gemini request failed after multiple retries.")


def validate_generated_content(content):
    required_fields = ["headline", "caption", "cta", "hashtags", "graphic_color"]

    for field in required_fields:
        if field not in content:
            raise ValueError(f"Missing required Gemini field: {field}")

    if not isinstance(content["hashtags"], list):
        raise ValueError("Gemini field 'hashtags' must be a list.")

    if len(content["hashtags"]) == 0:
        raise ValueError("Gemini returned no hashtags.")


def fetch_bg_image(keyword):
    headers = {"Authorization": os.environ["PEXELS_KEY"]}
    params = {
        "query": keyword,
        "per_page": 1,
        "orientation": "square",
    }

    response = requests.get(
        "https://api.pexels.com/v1/search",
        headers=headers,
        params=params,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    photos = data.get("photos", [])

    if not photos:
        raise RuntimeError(f"No Pexels image found for keyword: {keyword}")

    photo_url = photos[0]["src"]["large2x"]

    image_response = requests.get(photo_url, timeout=30)
    image_response.raise_for_status()

    return Image.open(io.BytesIO(image_response.content)).convert("RGB")


def create_image(content, keyword):
    size = (1080, 1080)
    bg = fetch_bg_image(keyword).resize(size)

    overlay = Image.new("RGBA", size, (0, 0, 0, 170))
    bg = bg.convert("RGBA")
    bg.paste(overlay, mask=overlay)

    draw = ImageDraw.Draw(bg)

    font_big = ImageFont.truetype("Roboto-Bold.ttf", 72)
    font_src = ImageFont.truetype("Roboto-Regular.ttf", 32)

    headline = content["headline"]
    lines = textwrap.wrap(headline, width=16)

    y = 380

    for line in lines:
        draw.text((80, y), line, font=font_big, fill="white")
        y += 90

    draw.text(
        (80, 960),
        "THE WORLD JOURNAL  •  @the.worldjournal",
        font=font_src,
        fill=(255, 255, 255, 180),
    )

    bg.convert("RGB").save(POST_IMAGE_PATH, quality=95)
    return POST_IMAGE_PATH


def upload_image_free(filepath):
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]

    filename = f"post-{int(time.time())}.jpg"
    path = f"public-posts/{filename}"

    with open(filepath, "rb") as image_file:
        encoded_content = base64.b64encode(image_file.read()).decode("utf-8")

    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    response = requests.put(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "message": f"Upload Instagram image {filename}",
            "content": encoded_content,
        },
        timeout=30,
    )

    data = response.json()
    print("GitHub upload response:", json.dumps(data, indent=2))

    if response.status_code not in [200, 201]:
        raise RuntimeError(f"GitHub image upload failed: {data}")

    return f"https://raw.githubusercontent.com/{repo}/main/{path}"


def publish_to_instagram(image_path, content):
    base_url = (
        f"https://graph.facebook.com/{INSTAGRAM_API_VERSION}/"
        f"{os.environ['IG_USER_ID']}"
    )
    token = os.environ["IG_TOKEN"]

    caption = (
        content["caption"]
        + "\n\n"
        + content["cta"]
        + "\n\n"
        + " ".join(content["hashtags"])
    )

    image_url = upload_image_free(image_path)

    media_response = requests.post(
        f"{base_url}/media",
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": token,
        },
        timeout=30,
    ).json()

    print("Instagram media response:", json.dumps(media_response, indent=2))

    if "id" not in media_response:
        raise RuntimeError(f"Instagram media creation failed: {media_response}")

    container_id = media_response["id"]

    for _ in range(10):
        status_response = requests.get(
            f"https://graph.facebook.com/{INSTAGRAM_API_VERSION}/{container_id}",
            params={
                "fields": "status_code",
                "access_token": token,
            },
            timeout=30,
        ).json()

        print("Instagram container status:", json.dumps(status_response, indent=2))

        if status_response.get("status_code") == "FINISHED":
            break

        if status_response.get("status_code") == "ERROR":
            raise RuntimeError(f"Instagram container processing failed: {status_response}")

        time.sleep(3)

    publish_response = requests.post(
        f"{base_url}/media_publish",
        data={
            "creation_id": container_id,
            "access_token": token,
        },
        timeout=30,
    ).json()

    print("Instagram publish response:", json.dumps(publish_response, indent=2))

    if "id" not in publish_response:
        raise RuntimeError(f"Instagram publish failed: {publish_response}")

    return publish_response["id"]


def log_to_sheets(article, content, post_id, status):
    creds_json = json.loads(os.environ["GSHEET_CREDS"])

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    sheets_client = gspread.authorize(creds)
    sheet = sheets_client.open("Instagram Bot Log").sheet1

    sheet.append_row(
        [
            datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            article.get("title", "")[:80],
            content.get("headline", ""),
            post_id or "FAILED",
            status,
            article.get("url", ""),
        ]
    )


def main():
    article = fetch_news()
    print(f"Selected article: {article.get('title', '')}")

    content = generate_content(article)
    print("Generated content:", json.dumps(content, indent=2))

    keyword = article.get("title", "world news").split()[0]
    image_path = create_image(content, keyword)

    post_id = publish_to_instagram(image_path, content)

    status = "success" if post_id else "failed"
    log_to_sheets(article, content, post_id, status)

    print(f"Posted: {content['headline']}")


if __name__ == "__main__":
    main()
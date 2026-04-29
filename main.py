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
from PIL import Image, ImageDraw, ImageFont, ImageOps


GNEWS_URL = "https://gnews.io/api/v4/top-headlines"
GEMINI_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.5-flash",
]
INSTAGRAM_API_VERSION = "v25.0"
POST_IMAGE_PATH = "/tmp/post.jpeg"

client = genai.Client(api_key=os.environ["GEMINI_KEY"])


def fetch_news():
    params = {
        "token": os.environ["GNEWS_KEY"],
        "lang": "en",
        "max": "10",
        "topic": "world",
    }

    response = requests.get(GNEWS_URL, params=params, timeout=30)
    response.raise_for_status()

    articles = response.json().get("articles", [])

    if not articles:
        raise RuntimeError("No articles found from GNews.")

    def score(article):
        published_at = article.get("publishedAt")

        try:
            published_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
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

    return sorted(articles, key=score, reverse=True)[0]


def build_local_fallback_content(article):
    title = article.get("title", "Breaking News Update")
    description = article.get("description", "")

    clean_title = re.sub(r"[^\w\s]", "", title).strip()
    words = clean_title.split()

    headline = (
        " ".join(words[:8]).upper() if len(words) >= 4 else "BREAKING STORY DEVELOPING"
    )

    caption_source = (description or title).strip()

    if len(caption_source) > 180:
        caption_source = caption_source[:177].rsplit(" ", 1)[0] + "..."

    return {
        "headline": headline,
        "caption": f"{caption_source} This story is developing.",
        "cta": "What do you think about this?",
        "hashtags": [
            "#News",
            "#BreakingNews",
            "#WorldNews",
            "#CurrentEvents",
            "#TodayNews",
            "#GlobalNews",
            "#NewsUpdate",
            "#TheWorldJournal",
        ],
        "graphic_color": "#1e3a5f",
    }


def generate_content(article):
    prompt = f"""
You are a senior news social media editor creating content for modern, high-impact Instagram news graphics.

Return ONLY valid JSON.
No markdown.
No backticks.
No explanation.

Article title: {article.get("title", "")}
Article summary: {article.get("description", "")}

Return JSON with exactly these fields:
- "headline": a strong hook-style graphic headline in ALL CAPS, 4 to 8 words, punchy, curiosity-driven, fact-based, and made for a bold news card
- "caption": exactly 2 short sentences, engaging and conversational, max 1 emoji
- "cta": one short engagement-focused sentence
- "hashtags": a list of 8 relevant hashtags as strings
- "graphic_color": one of: "#1e293b", "#7f1d1d", "#1e3a5f", "#14532d"

Headline rules:
- Make the headline the strongest hook.
- Use urgency, tension, surprise, or impact.
- Avoid weak summaries.
- Avoid false clickbait.
- Keep it short enough for a square Instagram graphic.
"""

    max_retries_per_model = 2

    for model_name in GEMINI_MODELS:
        for attempt in range(max_retries_per_model):
            try:
                print(
                    f"Trying Gemini model: {model_name}, "
                    f"attempt {attempt + 1}/{max_retries_per_model}"
                )

                response = client.models.generate_content(
                    model=model_name,
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

                print(f"Gemini content generated using model: {model_name}")
                return content

            except Exception as error:
                error_text = str(error).lower()

                is_retryable_error = (
                    "429" in error_text
                    or "quota" in error_text
                    or "rate limit" in error_text
                    or "resource_exhausted" in error_text
                    or "503" in error_text
                    or "unavailable" in error_text
                    or "high demand" in error_text
                    or "servererror" in error_text
                    or "temporarily" in error_text
                    or "500" in error_text
                    or "502" in error_text
                    or "504" in error_text
                )

                if is_retryable_error:
                    wait_time = 20 + random.randint(5, 15)
                    print(
                        f"Gemini temporary error on {model_name}. "
                        f"Waiting {wait_time} seconds before retry..."
                    )
                    time.sleep(wait_time)
                    continue

                print(f"Gemini failed with non-retryable error: {error}")
                break

    print("All Gemini models failed. Using local fallback content.")
    return build_local_fallback_content(article)


def validate_generated_content(content):
    required_fields = ["headline", "caption", "cta", "hashtags", "graphic_color"]

    for field in required_fields:
        if field not in content:
            raise ValueError(f"Missing required Gemini field: {field}")

    if not isinstance(content["hashtags"], list):
        raise ValueError("Gemini field 'hashtags' must be a list.")

    if not content["hashtags"]:
        raise ValueError("Gemini returned no hashtags.")


def fetch_bg_image(keyword):
    headers = {"Authorization": os.environ["PEXELS_KEY"]}

    search_query = keyword
    if not search_query or len(search_query) < 3:
        search_query = "world news"

    params = {
        "query": search_query,
        "per_page": 5,
        "orientation": "portrait",
    }

    response = requests.get(
        "https://api.pexels.com/v1/search",
        headers=headers,
        params=params,
        timeout=30,
    )
    response.raise_for_status()

    photos = response.json().get("photos", [])

    if not photos:
        raise RuntimeError(f"No Pexels image found for keyword: {search_query}")

    photo_url = photos[0]["src"].get("portrait") or photos[0]["src"].get("large2x")

    image_response = requests.get(photo_url, timeout=30)
    image_response.raise_for_status()

    return Image.open(io.BytesIO(image_response.content)).convert("RGB")


def download_image(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return Image.open(io.BytesIO(response.content)).convert("RGB")


def get_background_image(article, keyword):
    article_image = article.get("image")

    if article_image:
        try:
            img = download_image(article_image)

            if img.width > 100 and img.height > 100:
                return img

        except Exception as error:
            print(f"Article image failed. Using Pexels fallback. Error: {error}")

    title = article.get("title", "") or keyword or "world news"
    cleaned_title = re.sub(r"[^a-zA-Z0-9\s]", " ", title)
    words = cleaned_title.split()

    search_keyword = " ".join(words[:4]) if words else "world news"

    try:
        return fetch_bg_image(search_keyword)
    except Exception as error:
        print(f"Pexels title search failed. Using generic fallback. Error: {error}")
        return fetch_bg_image("breaking news")


def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def wrap_text_by_width(draw, text, font, max_width):
    words = str(text).split()
    lines = []
    current_line = ""

    for word in words:
        test_line = f"{current_line} {word}".strip()
        bbox = draw.textbbox((0, 0), test_line, font=font)
        test_width = bbox[2] - bbox[0]

        if test_width <= max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = word

    if current_line:
        lines.append(current_line)

    return lines


def paste_rounded_image(base, image, box, radius=38):
    x, y, width, height = box

    fitted = ImageOps.fit(
        image,
        (width, height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )

    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, width, height), radius=radius, fill=255)

    base.paste(fitted.convert("RGBA"), (x, y), mask)


def create_image(content, keyword, article):
    size = (1080, 1350)
    bg_color = content.get("graphic_color", "#1e3a5f")
    accent_color = "#eaff00"

    canvas = Image.new("RGBA", size, bg_color)
    draw = ImageDraw.Draw(canvas)

    font_logo = load_font("Roboto-Bold.ttf", 32)
    font_tag = load_font("Roboto-Bold.ttf", 28)
    font_headline = load_font("Roboto-Bold.ttf", 78)
    font_caption = load_font("Roboto-Regular.ttf", 34)
    font_button = load_font("Roboto-Bold.ttf", 26)
    font_footer = load_font("Roboto-Regular.ttf", 26)

    # Background shapes
    draw.ellipse((760, -190, 1320, 380), fill=(255, 255, 255, 28))
    draw.ellipse((-260, 920, 390, 1570), fill=(255, 255, 255, 20))

    # Border frame
    draw.rounded_rectangle(
        (44, 44, 1036, 1306),
        radius=52,
        outline=(255, 255, 255, 70),
        width=2,
    )

    # Logo
    draw.rounded_rectangle((70, 70, 265, 135), radius=12, fill=(0, 0, 0, 235))
    draw.text((90, 86), "THE WORLD", font=font_logo, fill="white")
    draw.rectangle((90, 121, 230, 128), fill=accent_color)

    # Main image
    bg_image = get_background_image(article, keyword)
    image_box = (70, 175, 940, 565)
    paste_rounded_image(canvas, bg_image, image_box, radius=42)

    # Image overlay
    overlay = Image.new("RGBA", (940, 565), (0, 0, 0, 45))
    mask = Image.new("L", (940, 565), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, 940, 565), radius=42, fill=255)
    canvas.paste(overlay, (70, 175), mask)

    # Badge
    draw.rounded_rectangle((70, 780, 380, 842), radius=14, fill=accent_color)
    draw.text((100, 797), "BREAKING NEWS", font=font_tag, fill="black")

    # White content card
    card_x = 70
    card_y = 875
    card_w = 940
    card_h = 300

    draw.rounded_rectangle(
        (card_x, card_y, card_x + card_w, card_y + card_h),
        radius=38,
        fill="white",
    )

    # Headline
    headline = content.get("headline", "BREAKING STORY DEVELOPING").upper()
    headline_lines = wrap_text_by_width(draw, headline, font_headline, 840)

    y = card_y + 45
    for line in headline_lines[:2]:
        draw.text((card_x + 45, y), line, font=font_headline, fill="black")
        y += 86

    # Caption
    caption = content.get("caption", "")
    caption_lines = wrap_text_by_width(draw, caption, font_caption, 840)

    y += 4
    for line in caption_lines[:2]:
        draw.text((card_x + 48, y), line, font=font_caption, fill=(45, 45, 45))
        y += 42

    # Bottom button
    footer_y = 1230

    draw.rounded_rectangle(
        (70, footer_y, 285, footer_y + 58), radius=14, fill=accent_color
    )
    draw.text((100, footer_y + 16), "READ MORE", font=font_button, fill="black")

    # Source
    source_name = "Newsmedia"
    if isinstance(article.get("source"), dict):
        source_name = article["source"].get("name", "Newsmedia")

    source_text = f"Source: {source_name}"
    source_bbox = draw.textbbox((0, 0), source_text, font=font_footer)
    source_width = source_bbox[2] - source_bbox[0]

    draw.text(
        (1010 - source_width, footer_y + 17),
        source_text,
        font=font_footer,
        fill=(255, 255, 255, 235),
    )

    canvas.convert("RGB").save(POST_IMAGE_PATH, quality=95)
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
        f"https://graph.facebook.com/{INSTAGRAM_API_VERSION}/{os.environ['IG_USER_ID']}"
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
            raise RuntimeError(
                f"Instagram container processing failed: {status_response}"
            )

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
    image_path = create_image(content, keyword, article)

    post_id = publish_to_instagram(image_path, content)

    status = "success" if post_id else "failed"
    log_to_sheets(article, content, post_id, status)

    print(f"Posted: {content['headline']}")


if __name__ == "__main__":
    main()

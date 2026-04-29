import base64
import io
import json
import os
import random
import re
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

    if len(words) >= 4:
        headline = " ".join(words[:8]).upper()
    else:
        headline = "BREAKING STORY DEVELOPING"

    caption_source = (description or title).strip()

    if len(caption_source) > 160:
        caption_source = caption_source[:157].rsplit(" ", 1)[0] + "..."

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
You are a senior news social media editor creating content for premium Instagram news graphics.

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
- Keep it short enough for a 1080x1350 Instagram portrait graphic.
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


def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def hex_to_rgb(hex_color, fallback=(30, 58, 95)):
    try:
        value = hex_color.strip().lstrip("#")
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def clamp_text(text, max_chars):
    text = str(text or "").strip()

    if len(text) <= max_chars:
        return text

    return text[: max_chars - 3].rsplit(" ", 1)[0] + "..."


def wrap_text_by_width(draw, text, font, max_width, max_lines=None):
    words = str(text or "").split()
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

            if max_lines and len(lines) >= max_lines:
                break

    if current_line and (not max_lines or len(lines) < max_lines):
        lines.append(current_line)

    return lines


def image_stats(img):
    sample = img.convert("L").resize((80, 80))
    pixels = list(sample.getdata())

    avg = sum(pixels) / len(pixels)
    variance = sum((p - avg) ** 2 for p in pixels) / len(pixels)
    dark_ratio = sum(1 for p in pixels if p < 18) / len(pixels)
    bright_ratio = sum(1 for p in pixels if p > 245) / len(pixels)

    return avg, variance, dark_ratio, bright_ratio


def is_image_usable(img):
    try:
        if img is None:
            return False

        if img.width < 400 or img.height < 300:
            return False

        avg, variance, dark_ratio, bright_ratio = image_stats(img)

        print(
            "Image check:",
            f"size={img.width}x{img.height}",
            f"avg={avg:.2f}",
            f"variance={variance:.2f}",
            f"dark_ratio={dark_ratio:.2f}",
            f"bright_ratio={bright_ratio:.2f}",
        )

        if avg < 35:
            return False

        if avg > 240:
            return False

        if variance < 150:
            return False

        if dark_ratio > 0.72:
            return False

        if bright_ratio > 0.86:
            return False

        return True

    except Exception as error:
        print(f"Image usability check failed: {error}")
        return False


def download_image(url):
    response = requests.get(
        url,
        timeout=35,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    response.raise_for_status()

    content_type = response.headers.get("content-type", "").lower()

    if "image" not in content_type:
        raise RuntimeError(f"URL did not return an image. Content-Type: {content_type}")

    img = Image.open(io.BytesIO(response.content)).convert("RGB")

    if not is_image_usable(img):
        raise RuntimeError("Downloaded image failed usability validation.")

    return img


def derive_pexels_queries(article, keyword):
    title = article.get("title", "") or ""
    description = article.get("description", "") or ""
    combined = f"{title} {description}".lower()

    queries = []

    topic_map = [
        (
            ["airline", "flight", "plane", "airport", "passenger"],
            "airplane airport news",
        ),
        (["trump", "iran", "nuclear", "war", "peace"], "world leaders politics"),
        (["market", "stock", "economy", "inflation"], "stock market trading"),
        (["climate", "weather", "storm", "flood"], "climate weather disaster"),
        (["cyber", "hack", "data", "security"], "cyber security hacker"),
        (["health", "hospital", "doctor", "medical"], "hospital healthcare news"),
        (["football", "world cup", "soccer"], "football stadium"),
        (["ship", "sea", "migrant", "boat"], "rescue boat sea"),
    ]

    for keywords, query in topic_map:
        if any(word in combined for word in keywords):
            queries.append(query)

    cleaned_title = re.sub(r"[^a-zA-Z0-9\s]", " ", title)
    title_words = [w for w in cleaned_title.split() if len(w) > 3]

    if title_words:
        queries.append(" ".join(title_words[:4]))

    if keyword:
        queries.append(keyword)

    queries.extend(
        [
            "breaking news",
            "journalist newsroom",
            "press conference",
            "global news",
        ]
    )

    clean_queries = []
    for query in queries:
        query = re.sub(r"\s+", " ", query).strip()

        if query and query not in clean_queries:
            clean_queries.append(query)

    return clean_queries


def fetch_bg_image(article, keyword):
    headers = {"Authorization": os.environ["PEXELS_KEY"]}

    for search_query in derive_pexels_queries(article, keyword):
        print(f"Trying Pexels search: {search_query}")

        params = {
            "query": search_query,
            "per_page": 10,
            "orientation": "landscape",
        }

        try:
            response = requests.get(
                "https://api.pexels.com/v1/search",
                headers=headers,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
        except Exception as error:
            print(f"Pexels search failed for {search_query}: {error}")
            continue

        photos = response.json().get("photos", [])

        for photo in photos:
            src = photo.get("src", {})
            urls = [
                src.get("large2x"),
                src.get("large"),
                src.get("landscape"),
                src.get("original"),
            ]

            for photo_url in urls:
                if not photo_url:
                    continue

                try:
                    img = download_image(photo_url)
                    print(f"Using Pexels image: {photo_url}")
                    return img
                except Exception as error:
                    print(f"Skipped bad Pexels image: {error}")

    raise RuntimeError("No usable Pexels image found.")


def create_placeholder_news_image(content, article):
    w, h = 940, 565
    base = Image.new("RGB", (w, h), (18, 28, 48))
    draw = ImageDraw.Draw(base)

    accent = "#eaff00"

    font_topic = load_font("Roboto-Bold.ttf", 82)
    font_label = load_font("Roboto-Bold.ttf", 38)
    font_small = load_font("Roboto-Regular.ttf", 28)

    for y in range(h):
        ratio = y / h
        r = int(18 + ratio * 12)
        g = int(28 + ratio * 22)
        b = int(48 + ratio * 36)
        draw.line((0, y, w, y), fill=(r, g, b))

    draw.ellipse((-180, -160, 360, 380), fill=(255, 255, 255, 28))
    draw.ellipse((620, 220, 1120, 760), fill=(255, 255, 255, 22))
    draw.ellipse((700, -120, 1060, 240), fill=(234, 255, 0, 45))

    draw.rounded_rectangle(
        (65, 70, 875, 495), radius=34, outline=(255, 255, 255, 90), width=2
    )
    draw.rectangle((95, 105, 125, 460), fill=accent)

    headline = content.get("headline", "BREAKING NEWS").upper()
    words = [
        word for word in re.sub(r"[^A-Z0-9\s]", " ", headline).split() if len(word) > 2
    ]

    topic = words[0] if words else "NEWS"
    secondary = " ".join(words[1:4]) if len(words) > 1 else "DEVELOPING STORY"

    draw.text((160, 155), topic[:11], font=font_topic, fill="white")
    draw.text((164, 250), secondary[:24], font=font_label, fill=accent)

    source_name = "Global News"
    if isinstance(article.get("source"), dict):
        source_name = article["source"].get("name", "Global News")

    draw.text(
        (164, 330), f"Source: {source_name}", font=font_small, fill=(235, 235, 235)
    )

    for x in [720, 770, 820]:
        draw.rounded_rectangle(
            (x, 105, x + 36, 420), radius=12, fill=(255, 255, 255, 32)
        )

    return base


def get_background_image(article, keyword, content):
    article_image = article.get("image")

    if article_image:
        try:
            img = download_image(article_image)
            print(f"Using article image: {article_image}")
            return img
        except Exception as error:
            print(f"Article image rejected: {error}")

    try:
        return fetch_bg_image(article, keyword)
    except Exception as error:
        print(f"Pexels failed. Using designed fallback visual. Error: {error}")
        return create_placeholder_news_image(content, article)


def paste_rounded_image(base, image, box, radius=38):
    x, y, width, height = box

    fitted = ImageOps.fit(
        image.convert("RGB"),
        (width, height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )

    if not is_image_usable(fitted):
        raise RuntimeError("Fitted image became unusable before paste.")

    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, width, height), radius=radius, fill=255)

    base.paste(fitted.convert("RGBA"), (x, y), mask)


def draw_cover_visual(canvas, visual, box, radius, content, article):
    x, y, width, height = box

    try:
        paste_rounded_image(canvas, visual, box, radius=radius)
    except Exception as error:
        print(f"Primary visual paste failed. Using fallback visual. Error: {error}")
        fallback = create_placeholder_news_image(content, article)
        paste_rounded_image(canvas, fallback, box, radius=radius)

    crop = canvas.crop((x, y, x + width, y + height)).convert("RGB")

    if not is_image_usable(crop):
        print("Final image slot is unusable after paste. Repainting fallback visual.")
        fallback = create_placeholder_news_image(content, article)
        paste_rounded_image(canvas, fallback, box, radius=radius)


def draw_soft_overlay(canvas, box, radius=42):
    x, y, width, height = box

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 34))
    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, width, height), radius=radius, fill=255)

    canvas.paste(overlay, (x, y), mask)


def create_image(content, keyword, article):
    size = (1080, 1350)
    bg_color_hex = content.get("graphic_color", "#1e3a5f")
    bg_rgb = hex_to_rgb(bg_color_hex)
    accent_color = "#eaff00"

    canvas = Image.new("RGBA", size, bg_rgb)
    draw = ImageDraw.Draw(canvas)

    font_logo = load_font("Roboto-Bold.ttf", 34)
    font_tag = load_font("Roboto-Bold.ttf", 28)
    font_headline = load_font("Roboto-Bold.ttf", 72)
    font_caption = load_font("Roboto-Regular.ttf", 32)
    font_button = load_font("Roboto-Bold.ttf", 26)
    font_footer = load_font("Roboto-Regular.ttf", 25)

    draw.rectangle((0, 0, 1080, 1350), fill=bg_rgb)
    draw.ellipse((760, -190, 1320, 380), fill=(255, 255, 255, 28))
    draw.ellipse((-280, 920, 390, 1580), fill=(255, 255, 255, 20))
    draw.ellipse((830, 780, 1280, 1220), fill=(0, 0, 0, 24))

    draw.rounded_rectangle(
        (44, 44, 1036, 1306),
        radius=52,
        outline=(255, 255, 255, 78),
        width=2,
    )

    draw.rounded_rectangle((70, 70, 275, 140), radius=13, fill=(0, 0, 0, 235))
    draw.text((90, 88), "THE WORLD", font=font_logo, fill="white")
    draw.rectangle((90, 126, 238, 133), fill=accent_color)

    image_box = (70, 175, 940, 565)
    visual = get_background_image(article, keyword, content)
    draw_cover_visual(canvas, visual, image_box, 42, content, article)
    draw_soft_overlay(canvas, image_box, radius=42)

    draw.rounded_rectangle((70, 780, 382, 842), radius=14, fill=accent_color)
    draw.text((100, 797), "BREAKING NEWS", font=font_tag, fill="black")

    card_x, card_y, card_w, card_h = 70, 875, 940, 310
    draw.rounded_rectangle(
        (card_x, card_y, card_x + card_w, card_y + card_h),
        radius=38,
        fill="white",
    )

    headline = clamp_text(
        content.get("headline", "BREAKING STORY DEVELOPING").upper(), 70
    )
    headline_lines = wrap_text_by_width(
        draw,
        headline,
        font_headline,
        835,
        max_lines=2,
    )

    y_pos = card_y + 44

    for line in headline_lines[:2]:
        draw.text((card_x + 45, y_pos), line, font=font_headline, fill="black")
        y_pos += 80

    caption = clamp_text(content.get("caption", ""), 145)
    caption_lines = wrap_text_by_width(
        draw,
        caption,
        font_caption,
        835,
        max_lines=2,
    )

    y_pos += 8

    for line in caption_lines[:2]:
        draw.text((card_x + 48, y_pos), line, font=font_caption, fill=(45, 45, 45))
        y_pos += 40

    footer_y = 1230

    draw.rounded_rectangle(
        (70, footer_y, 285, footer_y + 58),
        radius=14,
        fill=accent_color,
    )
    draw.text((100, footer_y + 16), "READ MORE", font=font_button, fill="black")

    source_name = "Newsmedia"

    if isinstance(article.get("source"), dict):
        source_name = article["source"].get("name", "Newsmedia")

    source_text = clamp_text(f"Source: {source_name}", 32)
    source_bbox = draw.textbbox((0, 0), source_text, font=font_footer)
    source_width = source_bbox[2] - source_bbox[0]

    draw.text(
        (1010 - source_width, footer_y + 17),
        source_text,
        font=font_footer,
        fill=(255, 255, 255, 235),
    )

    canvas.convert("RGB").save(POST_IMAGE_PATH, quality=95, optimize=True)
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

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
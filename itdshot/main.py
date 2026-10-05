import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from subprocess import run
from urllib.parse import quote, urlparse

from itd import Post
from itd.enums import AttachType
from itd.span import Span
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from playwright.sync_api import sync_playwright

from itdshot.video import VideoBox, make_gif

base_path = Path(__file__).parent
templates_path = base_path / "templates"
out_path = base_path.parent / "out.html"

SITE_URL = "https://xn--d1ah4a.com/"
# страница открывается как будто с сайта: шрифты и иконки там подключаются
# относительными путями, а шрифты с другого origin (file://) браузер не грузит
RENDER_URL = SITE_URL + "__itdshot__"
# шрифты и иконки сайта кешируются, чтобы не качать их при каждом запуске
CACHE_PATH = Path.home() / ".cache" / "itdshot"

# размеры с сайта: ширина ленты и ограничения одиночного вложения
DEFAULT_WIDTH = 650
SINGLE_MAX_WIDTH = 650
SINGLE_MAX_HEIGHT = 500
PIN_SIZES = {"xs": 12, "sm": 14, "md": 16, "lg": 22}

MONTHS = [
    "янв.", "февр.", "мар.", "апр.", "мая", "июн.",
    "июл.", "авг.", "сент.", "окт.", "нояб.", "дек.",
]


@dataclass
class Media:
    src: str
    width: int | None
    height: int | None
    video_url: str | None = None
    video_index: int | None = None


@dataclass
class Extras:
    """Данные постов, которых нет в itd-sdk: тетрадь, корректор, красная ручка."""

    notebook: str | None = None  # grid / ruled
    marks: list[dict] = field(default_factory=list)  # замазанный текст
    corrections: list[dict] = field(default_factory=list)  # правки красной ручкой
    corrector_actors: list[dict] = field(default_factory=list)
    red_pen_actors: list[dict] = field(default_factory=list)
    has_tools: bool = False  # пост рендерится через слой инструментов (как на сайте)


def is_url(value: str | None) -> bool:
    return bool(value) and value.startswith(("http://", "https://", "/"))


def count(value: int) -> str:
    for divider, suffix in ((1_000_000, "M"), (1_000, "K")):
        if value >= divider:
            number = value / divider
            return f"{number:g}{suffix}" if number.is_integer() else f"{number:.1f}{suffix}"
    return str(value)


def short_date(date: datetime) -> str:
    date = date.astimezone()
    return f"{date.day} {MONTHS[date.month - 1]}"


def relative_time(date: datetime) -> str:
    seconds = int((datetime.now(timezone.utc) - date).total_seconds())
    if seconds < 60:
        return "сейчас"
    if seconds < 3600:
        return f"{seconds // 60} мин."
    if seconds < 86400:
        return f"{seconds // 3600} ч."
    if seconds < 604800:
        return f"{seconds // 86400} дн."
    if seconds < 2419200:
        return f"{seconds // 604800} нед."
    return short_date(date)


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def single_width(width: int, height: int) -> int:
    if width / height > SINGLE_MAX_WIDTH / SINGLE_MAX_HEIGHT:
        return round(min(width, SINGLE_MAX_WIDTH))
    return round(min(height, SINGLE_MAX_HEIGHT) * width / height)


def media_of(post: Post, videos: list[Media]) -> list[Media]:
    media = []
    for attach in post.attachments:
        if attach.type == AttachType.VIDEO:
            item = Media(
                attach.thumbnail_url or attach.url,
                attach.width,
                attach.height,
                video_url=attach.url,
                video_index=len(videos),
            )
            videos.append(item)
            media.append(item)
        elif attach.type in (AttachType.IMAGE, AttachType.MEDIA):
            media.append(Media(attach.url, attach.width, attach.height))
    return media


def _alive(items: list[dict], now: datetime) -> list[dict]:
    """Корректор и ручка временные: на сайте показываются только до endsAt."""
    result = []
    for item in items:
        ends_at = item.get("endsAt")
        if ends_at is None or datetime.fromisoformat(ends_at.replace("Z", "+00:00")) > now:
            result.append(item)
    return result


def _created_at(item: dict) -> int:
    if item.get("createdAtMicros") is not None:
        return int(item["createdAtMicros"])
    if item.get("createdAt"):
        return int(datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00")).timestamp() * 1e6)
    return 0


def _unique_actors(items: list[dict]) -> list[dict]:
    actors = {}
    for item in items:
        actor = item.get("actor") or {}
        actors.setdefault(actor.get("id"), actor)
    return list(actors.values())


def extras_of(raw: dict | None) -> Extras:
    if not raw:
        return Extras()

    now = datetime.now(timezone.utc)
    notebook = (raw.get("notebook") or {}).get("style")
    corrector = raw.get("corrector")
    red_pen = raw.get("redPen")

    marks = _alive(corrector.get("marks") or [], now) if corrector else []

    claims = []
    corrections = []
    if red_pen:
        claims = red_pen.get("claims") or ([red_pen["claim"]] if red_pen.get("claim") else [])
        claims = _alive(claims, now)
        if claims:
            corrections = red_pen.get("corrections") or []

    # если на одном фрагменте и замазка, и правка, остаётся более поздняя
    latest: dict[tuple, tuple] = {}
    for kind, item in [("paint", m) for m in marks] + [("pen", c) for c in corrections]:
        key = (item["start"], item["end"])
        rank = (_created_at(item), str(item.get("id")))
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, item.get("id"))

    def keep(item: dict) -> bool:
        return latest[(item["start"], item["end"])][1] == item.get("id")

    return Extras(
        notebook=notebook if notebook in ("grid", "ruled") else None,
        marks=[m for m in marks if keep(m)],
        corrections=[c for c in corrections if keep(c)],
        corrector_actors=_unique_actors(marks),
        red_pen_actors=_unique_actors(claims) if corrections else [],
        has_tools=bool(corrector or red_pen),
    )


def corrector_textures(paths: list[list[str]]) -> list[str]:
    textures = []
    for outline, lines in paths:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="28" viewBox="0 0 120 28" preserveAspectRatio="none">'
            f'<path d="{outline}" fill="#fffef7" stroke="#cecbbc" stroke-width=".6" vector-effect="non-scaling-stroke"/>'
            f'<path d="{lines}" fill="none" stroke="#e6e2d4" stroke-width=".55"/></svg>'
        )
        textures.append("data:image/svg+xml," + quote(svg, safe="-_.!~*'()"))
    return textures


def _fnv1a(value: str) -> int:
    result = 2166136261
    for unit in value.encode("utf-16-le")[::2]:
        result = ((result ^ unit) * 16777619) & 0xFFFFFFFF
    return result


def texture_variants(marks: list[dict], count: int) -> dict[str, int]:
    """Как на сайте: соседние замазки получают разные формы пятна."""
    variants: dict[str, int] = {}
    used: set[int] = set()
    previous = -1
    for mark in marks:
        mark_id = str(mark.get("id"))
        if mark_id in variants:
            continue
        if len(used) == count:
            used.clear()
        variant = _fnv1a(mark_id) % count
        while variant in used or (not used and variant == previous):
            variant = (variant + 1) % count
        variants[mark_id] = variant
        used.add(variant)
        previous = variant
    return variants


def render_text(
    text: str,
    spans: list[Span],
    classes: dict[str, str],
    marks: list[dict] = [],
    textures: list[str] = [],
) -> Markup:
    """Аналог компонента текста поста на сайте. Смещения в UTF-16."""
    encoded = text.encode("utf-16-le")
    length = len(encoded) // 2
    variants = texture_variants(marks, len(textures)) if marks else {}

    def substring(start: int, end: int) -> str:
        return encoded[start * 2 : end * 2].decode("utf-16-le", errors="replace")

    def with_marks(start: int, end: int) -> Markup:
        hits = [m for m in marks if m["start"] < end and m["end"] > start]
        if not hits:
            return escape(substring(start, end))
        cuts = {start, end}
        for mark in hits:
            cuts.add(max(start, mark["start"]))
            cuts.add(min(end, mark["end"]))
        cuts = sorted(cuts)
        result = Markup("")
        for left, right in zip(cuts, cuts[1:]):
            piece = substring(left, right)
            mark = next((m for m in hits if m["start"] <= left < m["end"]), None)
            if mark is None:
                result += escape(piece)
                continue
            variant = variants[str(mark.get("id"))]
            result += Markup(
                '<span class="%s" data-corrector-mark style="background-image: url(&quot;%s&quot;)">'
                '<span aria-hidden="true">%s</span></span>'
            ) % (classes["corrector"], Markup(textures[variant]), piece)
        return result

    if not spans:
        return Markup('<span class="">%s</span>') % with_marks(0, length)

    events = []
    for index, span in enumerate(spans):
        events.append((span.offset, 0, index))
        events.append((span.offset + span.length, -1, index))
    events.sort()

    parts = []
    active: dict[int, Span] = {}
    position = 0

    def push(end: int):
        if end <= position:
            return
        chunk = with_marks(position, end)
        styles = {span.type.value for span in active.values()}

        if "bold" in styles:
            chunk = Markup("<strong>%s</strong>") % chunk
        if "italic" in styles:
            chunk = Markup("<em>%s</em>") % chunk
        if "underline" in styles:
            chunk = Markup('<span class="%s">%s</span>') % (classes["underline"], chunk)
        if "strike" in styles:
            chunk = Markup("<s>%s</s>") % chunk
        if "monospace" in styles:
            chunk = Markup('<code class="%s">%s</code>') % (classes["monospace"], chunk)
        if "quote" in styles:
            chunk = Markup('<span class="%s">%s</span>') % (classes["quote"], chunk)
        if "spoiler" in styles:
            chunk = Markup('<span class="%s">%s</span>') % (classes["spoiler"], chunk)
        for kind in ("link", "mention", "hashtag"):
            if kind in styles:
                chunk = Markup('<a class="%s">%s</a>') % (classes[kind], chunk)
        parts.append(Markup("<span>%s</span>") % chunk)

    for offset, kind, index in events:
        push(offset)
        position = max(position, offset)
        if kind == 0:
            active[index] = spans[index]
        else:
            active.pop(index, None)
    push(length)

    return Markup('<span class="">%s</span>') % Markup("").join(parts)


def edit_html(
    post: Post,
    dark: bool = True,
    width: int = DEFAULT_WIDTH,
    raw: dict | None = None,
    animated: bool = False,
) -> list[Media]:
    """Собрать out.html. raw — сырой ответ API поста (для данных, которых нет в itd-sdk).

    Возвращает видео из поста (по порядку их индексов в разметке).
    """
    classes = json.loads((templates_path / "classes.json").read_text())
    textures = corrector_textures(classes["corrector_textures"])

    videos: list[Media] = []
    post_media = media_of(post, videos)
    orig = post.original_post
    orig_media = media_of(orig, videos) if orig is not None else []

    env = Environment(
        loader=FileSystemLoader(templates_path), autoescape=select_autoescape()
    )
    env.globals.update(
        classes=classes,
        pin_sizes=PIN_SIZES,
        is_url=is_url,
        count=count,
        short_date=short_date,
        relative_time=relative_time,
        plural=plural,
        single_width=single_width,
        render_text=lambda text, spans, marks=[]: render_text(
            text, spans, classes["text"], marks, textures
        ),
    )

    html = env.get_template("post.html").render(
        post=post,
        post_media=post_media,
        post_extra=extras_of(raw),
        orig_media=orig_media,
        orig_extra=extras_of((raw or {}).get("originalPost")),
        dark=dark,
        width=width,
        animated=animated,
        site_css=Markup((templates_path / "site.css").read_text()),
    )
    out_path.write_text(html)
    return videos


VIDEO_BOXES_JS = """
() => {
    const post = document.querySelector('#post').getBoundingClientRect();
    return [...document.querySelectorAll('[data-itdshot-video]')].map(el => {
        const r = el.getBoundingClientRect();
        let clip = {l: r.left, t: r.top, r: r.right, b: r.bottom};
        for (let a = el.parentElement; a; a = a.parentElement) {
            const s = getComputedStyle(a);
            if (s.overflowX === 'visible' && s.overflowY === 'visible') continue;
            const q = a.getBoundingClientRect();
            clip = {
                l: Math.max(clip.l, q.left), t: Math.max(clip.t, q.top),
                r: Math.min(clip.r, q.right), b: Math.min(clip.b, q.bottom),
            };
        }
        const radius = parseFloat(getComputedStyle(el).borderTopLeftRadius)
            || parseFloat(getComputedStyle(el.parentElement).borderTopLeftRadius) || 0;
        return {
            index: Number(el.dataset.itdshotVideo),
            x: r.left - post.left, y: r.top - post.top, width: r.width, height: r.height,
            clip_x: clip.l - post.left, clip_y: clip.t - post.top,
            clip_width: clip.r - clip.l, clip_height: clip.b - clip.t,
            radius,
        };
    });
}
"""


def _cached(route):
    url = urlparse(route.request.url)
    file = CACHE_PATH / url.path.strip("/").replace("/", "_")
    if not file.exists():
        response = route.fetch(timeout=60_000)
        if not response.ok:
            route.fulfill(response=response)
            return
        CACHE_PATH.mkdir(parents=True, exist_ok=True)
        file.write_bytes(response.body())
    route.fulfill(path=file)


def screenshot(
    path: Path,
    clipboard: bool = False,
    scale: float = 1,
    videos: list[Media] = [],
    fps: int = 20,
    max_duration: float = 10,
):
    """Снять пост. Если path — .gif и в посте есть видео, получится анимация."""
    with sync_playwright() as p:
        browser = p.firefox.launch()
        # ширина больше брейкпоинта сайта (1174px), чтобы применились десктопные стили
        page = browser.new_page(
            viewport={"width": 1280, "height": 720}, device_scale_factor=scale
        )
        html = out_path.read_text()
        page.route(
            RENDER_URL,
            lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"),
        )
        page.route(SITE_URL + "fonts/**", _cached)
        page.route(SITE_URL + "assets/**", _cached)
        page.goto(RENDER_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_function("window.itdshotReady === true")

        post = page.locator("#post")
        if path.suffix.lower() == ".gif" and videos:
            boxes = [VideoBox(**box) for box in page.evaluate(VIDEO_BOXES_JS)]
            frame = path.with_suffix(".frame.png")
            post.screenshot(path=frame, omit_background=True)
            browser.close()
            try:
                make_gif(
                    frame,
                    path,
                    [(videos[box.index].video_url, box) for box in boxes],
                    scale=scale,
                    fps=fps,
                    max_duration=max_duration,
                )
            finally:
                frame.unlink(missing_ok=True)
        else:
            post.screenshot(path=path, omit_background=True)
            browser.close()

    if clipboard:
        run(
            ["xclip", "-i", "-selection", "clipboard", "-t", "text/uri-list"],
            input=path.absolute().as_uri().encode("utf-8"),
            check=True
        )
        print("copied")
    else:
        print(f"saved to {path}")

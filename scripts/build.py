#!/usr/bin/env python3
"""Build the static SnatchTv site from the public WordPress.com REST API.

Reads cached JSON if present under /tmp/snatch, otherwise fetches it.
Writes HTML, a search index, and downloaded images into the repo root.
"""

from __future__ import annotations

import html
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
API = "https://public-api.wordpress.com/wp/v2/sites/barrymgotay.wordpress.com"
ORIGIN = "https://barrymgotay.wordpress.com"
HOSTS = {"barrymgotay.wordpress.com"}
X_PROFILE = "https://x.com/SnatchLiberty"
X_LABEL = "Snatch Liberty (@SnatchLiberty)"
CACHE = Path("/tmp/snatch")
UA = "SnatchTv-static-mirror/1.0 (public archive; +https://bmgotay.github.io)"
FEED_PAGE_SIZE = 8
GRID_PAGE_SIZE = 12
LIVE = "https://bmgotay.github.io"

ssl_ctx = ssl.create_default_context()


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=60, context=ssl_ctx) as resp:
        return resp.read()


def load_collection(name: str, endpoint: str) -> list:
    path = CACHE / f"{name}.json"
    if path.exists() and path.stat().st_size > 1000:
        return json.loads(path.read_text())
    print("fetch", endpoint)
    data = json.loads(fetch(endpoint).decode())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def plain(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def esc(value: str) -> str:
    return html.escape(plain(value), quote=True)


def parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value)


def post_path(post: dict) -> str:
    dt = parse_date(post["date"])
    return f"/{dt:%Y/%m/%d}/{post['slug']}/"


def page_path(page: dict) -> str:
    if page["slug"] in {"about", "home"} or page.get("link", "").rstrip("/") == ORIGIN:
        return "/"
    return f"/{page['slug']}/"


def media_rel(url: str) -> str | None:
    if not url:
        return None
    path = unquote(urlparse(url).path)
    marker = "/wp-content/uploads/"
    idx = path.find(marker)
    if idx < 0:
        return None
    rel = path[idx + len(marker) :]
    if not rel or ".." in rel.split("/"):
        return None
    if not re.search(r"\.(jpe?g|png|gif|webp|avif)$", rel, re.I):
        return None
    return "assets/media/" + rel


def public_media(url: str) -> str | None:
    rel = media_rel(url)
    return "/" + rel if rel else None


UPLOAD_RE = re.compile(
    r"https://barrymgotay\.wordpress\.com/wp-content/uploads/[A-Za-z0-9_./%\-]+",
    re.I,
)


def find_uploads(text: str) -> set[str]:
    found = set()
    for match in UPLOAD_RE.findall(text or ""):
        url = match.split("?")[0]
        url = re.sub(r"[^A-Za-z0-9._/\-]+$", "", url)
        if media_rel(url):
            found.add(url)
    return found


def featured_url(item: dict) -> str | None:
    media = (item.get("_embedded") or {}).get("wp:featuredmedia") or []
    if media and isinstance(media[0], dict):
        return (media[0].get("source_url") or "").split("?")[0] or None
    return None


def terms_of(post: dict, taxonomy: str) -> list[dict]:
    groups = (post.get("_embedded") or {}).get("wp:term") or []
    out = []
    for group in groups:
        if not isinstance(group, list):
            continue
        for term in group:
            if isinstance(term, dict) and term.get("taxonomy") == taxonomy:
                out.append(term)
    return out


def author_of(post: dict) -> tuple[str, str]:
    authors = (post.get("_embedded") or {}).get("author") or []
    if authors and isinstance(authors[0], dict):
        name = authors[0].get("name") or "SnatchTvG"
        slug = authors[0].get("slug") or "bmgotay"
        return name, slug
    return "SnatchTvG", "bmgotay"


def is_naani_profile(href: str) -> bool:
    raw = html.unescape(href or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(raw)
    host = parsed.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host not in {"twitter.com", "mobile.twitter.com", "x.com"}:
        return False
    parts = [part for part in unquote(parsed.path).split("/") if part]
    return bool(parts) and parts[0].lower() == "naanimouse"


def rewrite_href(href: str) -> str:
    if not href:
        return href
    raw = html.unescape(href).strip()
    if raw.startswith("#") or raw.startswith("mailto:") or raw.startswith("tel:"):
        return raw
    if raw.startswith("//"):
        raw = "https:" + raw
    if is_naani_profile(raw):
        return X_PROFILE
    parsed = urlparse(raw)
    host = parsed.netloc.lower().split(":")[0]
    if host not in HOSTS and host != "":
        return raw
    if host == "" and not raw.startswith("/"):
        return raw
    path = unquote(parsed.path or "/")
    local_media = public_media(ORIGIN + path)
    if local_media:
        return local_media
    trimmed = path.rstrip("/")
    routes = {
        "": "/",
        "/about": "/",
        "/the-feed": "/the-feed/",
        "/about-snatchnet": "/about-snatchnet/",
        "/link-in-bio-with-flowery-wallpaper": "/link-in-bio-with-flowery-wallpaper/",
        "/snatchtv-news-stories-images": "/snatchtv-news-stories-images/",
    }
    if trimmed in routes:
        return routes[trimmed]
    if re.fullmatch(r"/\d{4}/\d{2}/\d{2}/[A-Za-z0-9\-]+", trimmed):
        return trimmed + "/"
    for prefix in ("/category/", "/tag/", "/author/"):
        if trimmed.startswith(prefix) and trimmed.count("/") >= 2:
            parts = trimmed.strip("/").split("/")
            if len(parts) >= 2 and parts[0] in {"category", "tag", "author"}:
                return f"/{parts[0]}/{parts[1]}/"
    if host in HOSTS:
        return raw
    return href


def youtube_id(url: str) -> str | None:
    parsed = urlparse(html.unescape(url))
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        ident = parsed.path.strip("/").split("/")[0]
        return ident or None
    if "youtube.com" not in host:
        return None
    if parsed.path.startswith("/watch"):
        from urllib.parse import parse_qs
        return (parse_qs(parsed.query).get("v") or [None])[0]
    for part in ("/embed/", "/shorts/", "/live/"):
        if part in parsed.path:
            return parsed.path.split(part, 1)[1].split("/")[0].split("?")[0]
    return None


def transform_html(source: str, featured_by_path: dict[str, str]) -> str:
    soup = BeautifulSoup(source or "", "html.parser")
    for script in soup.find_all("script"):
        script.decompose()
    for style in soup.find_all("style"):
        style.decompose()

    for fig in list(soup.select("figure.wp-block-embed-instagram, .wp-block-embed-instagram")):
        link = None
        quote = fig.find("blockquote")
        if quote and quote.get("data-instgrm-permalink"):
            link = quote["data-instgrm-permalink"].split("?")[0]
        if not link:
            anchor = fig.find("a", href=True)
            link = anchor["href"] if anchor else "https://www.instagram.com/"
        card = soup.new_tag("p", attrs={"class": "embed-card"})
        anchor = soup.new_tag("a", href=link, target="_blank", rel="noopener noreferrer")
        anchor.string = "Watch on Instagram"
        card.append(anchor)
        fig.replace_with(card)

    for div in list(soup.select(".wp-block-jetpack-story")):
        raw = div.get("data-settings") or ""
        try:
            data = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue
        wrap = soup.new_tag("div", attrs={"class": "story-slides"})
        for slide in data.get("slides") or []:
            url = slide.get("url") or ""
            alt = plain(slide.get("alt") or "")
            figure = soup.new_tag("figure")
            image = soup.new_tag("img", src=public_media(url) or url, alt=alt)
            figure.append(image)
            if alt:
                caption = soup.new_tag("figcaption")
                caption.string = alt
                figure.append(caption)
            wrap.append(figure)
        div.replace_with(wrap)

    for node in list(soup.select(".jetpack-blogging-prompt__answers")):
        node.decompose()
    for anchor in list(soup.find_all("a", href=True)):
        label = anchor.get_text(" ", strip=True).lower()
        if "wordpress.com/tag/dailyprompt" in anchor["href"] or label == "view all responses":
            anchor.decompose()

    for video in list(soup.find_all("video")):
        src = video.get("src") or ""
        ident = youtube_id(src)
        if not ident:
            continue
        shorts = "/shorts/" in src
        figure = soup.new_tag("div", attrs={"class": "video-embed shorts" if shorts else "video-embed"})
        frame = soup.new_tag(
            "iframe",
            src=f"https://www.youtube.com/embed/{ident}",
            title="YouTube video",
            loading="lazy",
            allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share",
            allowfullscreen="",
        )
        figure.append(frame)
        video.replace_with(figure)

    for frame in soup.find_all("iframe"):
        src = html.unescape(frame.get("src") or "")
        ident = youtube_id(src)
        if ident:
            src = f"https://www.youtube.com/embed/{ident}"
        frame["src"] = src
        frame["loading"] = "lazy"
        if frame.has_attr("sandbox"):
            del frame["sandbox"]
        frame["allowfullscreen"] = ""
        if "allow" not in frame.attrs:
            frame["allow"] = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"

    for query in list(soup.select(".wp-block-query")):
        query.decompose()

    for image in list(soup.find_all("img")):
        src = image.get("data-orig-file") or image.get("src") or ""
        if "gravatar.com" in src or "avatar" in " ".join(image.get("class") or []):
            image.decompose()
            continue
        local = public_media(src) or public_media(image.get("data-large-file") or "")
        if local:
            image["src"] = local
        elif image.get("src", "").startswith("data:"):
            pass
        for attr in list(image.attrs):
            if attr.startswith("data-") or attr in {"srcset", "sizes"}:
                del image[attr]
        if not image.get("src"):
            image.decompose()

    for article in soup.select("article"):
        anchor = article.select_one("a[href]")
        if not anchor:
            continue
        path = urlparse(rewrite_href(anchor["href"])).path
        if not path.endswith("/"):
            path += "/"
        thumb = article.select_one(".post-thumbnail")
        existing = article.select_one(".post-thumbnail img, figure img")
        if existing and existing.get("src"):
            continue
        feat = featured_by_path.get(path)
        if not feat or thumb is None:
            continue
        image = soup.new_tag("img", src=feat, alt="")
        placeholder = thumb.select_one(".wp-block-newspack-blocks-carousel__placeholder")
        if placeholder:
            placeholder.replace_with(image)
        else:
            thumb.append(image)

    for anchor in soup.find_all("a", href=True):
        was_naani = is_naani_profile(anchor["href"])
        anchor["href"] = rewrite_href(anchor["href"])
        if was_naani:
            label = anchor.find("span", class_="wp-block-social-link-label")
            if label is not None:
                classes = [name for name in (label.get("class") or []) if name != "screen-reader-text"]
                label["class"] = classes or ["wp-block-social-link-label"]
                label.string = X_LABEL
            elif anchor.find("svg") is None:
                anchor.string = X_LABEL
        if anchor["href"].startswith("http"):
            anchor["rel"] = "noopener noreferrer"
            if not anchor.get("target"):
                anchor["target"] = "_blank"

    return soup.decode()


SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <meta name="description" content="__DESCRIPTION__">
  <link rel="canonical" href="__CANONICAL__">
  <meta property="og:site_name" content="SnatchTv">
  <meta property="og:title" content="__TITLE__">
  <meta property="og:description" content="__DESCRIPTION__">
  <meta property="og:url" content="__CANONICAL__">
  <meta property="og:image" content="__IMAGE__">
  <meta name="twitter:card" content="summary_large_image">
  <link rel="icon" href="__ICON__" sizes="32x32">
  <link rel="apple-touch-icon" href="__ICON__">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:ital,wght@0,400;0,700;1,400;1,700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/assets/css/site.css">
</head>
<body class="__BODY_CLASS__">
  <a class="skip-link" href="#content">Skip to content</a>
  <header class="site-header">
    <div class="header-bar">
      <p class="site-title"><a href="/" rel="home">SnatchTv</a></p>
      <button class="menu-toggle" type="button" aria-expanded="false" aria-controls="site-menu" aria-label="Open menu">
        <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7.5h16v1.5H4z"></path><path d="M4 15h16v1.5H4z"></path></svg>
      </button>
    </div>
    <nav id="site-menu" class="nav-overlay" hidden>
      <button class="menu-close" type="button" aria-label="Close menu">
        <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true"><path d="m13.06 12 6.47-6.47-1.06-1.06L12 10.94 5.53 4.47 4.47 5.53 10.94 12l-6.47 6.47 1.06 1.06L12 13.06l6.47 6.47 1.06-1.06L13.06 12Z"></path></svg>
      </button>
      <ul class="nav-list">
        <li><a href="/the-feed/"__FEED_CURRENT__>Complete Feed</a></li>
        <li class="sub"><a href="/"__HOME_CURRENT__>SnatchTv Info</a></li>
        <li class="sub"><a href="/about-snatchnet/"__ABOUT_CURRENT__>About Barry</a></li>
        <li><a href="/search/"__SEARCH_CURRENT__>Search</a></li>
      </ul>
    </nav>
    <div class="header-spacer" aria-hidden="true"></div>
  </header>
  __MAIN__
  <footer class="site-footer">
    <div class="subscribe">
      <p>Email updates from the old WordPress.com form are not carried over. Write Barry directly.</p>
      <a class="button" href="mailto:Bmgotay@gmail.com">Email Bmgotay@gmail.com</a>
    </div>
    <div class="footer-bar">
      <p class="footer-credit">Creator of SnatchTVG · <a href="https://twitter.com/SnatchLiberty" rel="me noopener noreferrer">@SnatchLiberty</a></p>
      <p class="site-title"><a href="/">SnatchTv</a></p>
    </div>
  </footer>
  <script src="/assets/js/site.js"></script>
</body>
</html>
"""


def current(flag: bool) -> str:
    return ' aria-current="page"' if flag else ""


def write_page(rel: str, title: str, description: str, main: str, body_class: str, section: str, icon: str, social_image: str) -> None:
    path = "/" if rel in {"", "/"} else rel if rel.startswith("/") else "/" + rel
    if path != "/" and not path.endswith("/"):
        path += "/"
    canonical = LIVE + (path if path != "/" else "/")
    doc_title = "SnatchTv" if section == "home" else f"{plain(title)} – SnatchTv"
    html_doc = (
        SHELL.replace("__TITLE__", html.escape(doc_title))
        .replace("__DESCRIPTION__", html.escape(plain(description)[:300] or "SnatchTv — news, politics, and culture."))
        .replace("__CANONICAL__", html.escape(canonical))
        .replace("__IMAGE__", html.escape(urljoin(LIVE, social_image)))
        .replace("__ICON__", html.escape(icon))
        .replace("__BODY_CLASS__", html.escape(body_class, quote=True))
        .replace("__MAIN__", main)
        .replace("__FEED_CURRENT__", current(section == "feed"))
        .replace("__HOME_CURRENT__", current(section == "home"))
        .replace("__ABOUT_CURRENT__", current(section == "about"))
        .replace("__SEARCH_CURRENT__", current(section == "search"))
    )
    dest = ROOT / "index.html" if path == "/" else ROOT / path.strip("/") / "index.html"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html_doc)


def feed_date(dt: datetime) -> str:
    return f"{dt:%b} {dt.day}"


def full_date(dt: datetime) -> str:
    return f"{dt:%B} {dt.day}, {dt.year}"


def pager(current_page: int, total: int, href_for) -> str:
    if total <= 1:
        return ""
    parts = ['<nav class="pager" aria-label="Pagination">']
    if current_page > 1:
        parts.append(f'<a href="{href_for(current_page - 1)}">← Previous</a>')
    for number in range(1, total + 1):
        if number == current_page:
            parts.append(f'<span class="current" aria-current="page">{number}</span>')
        else:
            parts.append(f'<a href="{href_for(number)}">{number}</a>')
    if current_page < total:
        parts.append(f'<a href="{href_for(current_page + 1)}">Next Page →</a>')
    parts.append("</nav>")
    return "".join(parts)


def feed_item(post: dict, image: str | None) -> str:
    dt = parse_date(post["date"])
    title = esc(post["title"]["rendered"])
    href = post_path(post)
    thumb = f'<a href="{href}" tabindex="-1" aria-hidden="true"><img class="feed-thumb" src="{html.escape(image)}" alt=""></a>' if image else ""
    klass = "feed-row" if image else "feed-row no-thumb"
    return (
        f'<li class="{klass}">'
        f'<div class="feed-date"><time datetime="{dt.date().isoformat()}">{feed_date(dt)}</time></div>'
        f'<h2 class="feed-title"><a href="{href}">{title}</a></h2>'
        f"{thumb}</li>"
    )


def excerpt_text(post: dict, limit: int = 180) -> str:
    text = plain(post.get("excerpt", {}).get("rendered") or "")
    if len(text) < 12:
        text = plain(post.get("content", {}).get("rendered") or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(".,;:—-")
    return f"{cut}…"


def home_feed_item(post: dict, image: str | None) -> str:
    dt = parse_date(post["date"])
    title = esc(post["title"]["rendered"])
    href = post_path(post)
    excerpt = excerpt_text(post)
    excerpt_html = f'<p class="feed-excerpt">{esc(excerpt)}</p>' if excerpt else ""
    thumb = f'<a href="{href}" tabindex="-1" aria-hidden="true"><img class="feed-thumb" src="{html.escape(image)}" alt=""></a>' if image else ""
    klass = "feed-row" if image else "feed-row no-thumb"
    return (
        f'<li class="{klass}">'
        f'<div class="feed-date"><time datetime="{dt.date().isoformat()}">{dt:%b} {dt.day}, {dt.year}</time></div>'
        f'<div class="feed-copy"><h2 class="feed-title"><a href="{href}">{title}</a></h2>{excerpt_html}</div>'
        f"{thumb}</li>"
    )


def grid_item(post: dict, image: str | None) -> str:
    href = post_path(post)
    title = esc(post["title"]["rendered"])
    media = (
        f'<a class="card-media" href="{href}" tabindex="-1" aria-hidden="true"><img src="{html.escape(image)}" alt=""></a>'
        if image
        else f'<a class="card-media" href="{href}" tabindex="-1" aria-hidden="true"></a>'
    )
    return f'<li>{media}<h2 class="card-title"><a href="{href}">{title}</a></h2></li>'


def download_one(url: str, dest: Path) -> tuple[str, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return url, "cached"
    candidates = [url]
    if "?" not in url:
        candidates.append(url + "?w=2000")
    last_error = "failed"
    for candidate in candidates:
        try:
            data = fetch(candidate)
        except Exception as exc:  # noqa: BLE001 - keep going through the mirror
            last_error = str(exc)
            continue
        if len(data) < 80:
            last_error = "too small"
            continue
        dest.write_bytes(data)
        return url, f"ok {len(data)}"
    return url, last_error


def main(only: str | None = None) -> None:
    posts = load_collection("posts", f"{API}/posts?per_page=100&_embed=1")
    pages = load_collection("pages", f"{API}/pages?per_page=100&_embed=1")
    posts = [p for p in posts if p.get("status") == "publish"]
    pages = [p for p in pages if p.get("status") == "publish"]
    posts.sort(key=lambda p: p["date"], reverse=True)

    by_slug_page = {p["slug"]: p for p in pages}
    home = by_slug_page["about"]

    uploads: set[str] = set()
    for item in posts + pages:
        uploads |= find_uploads(item.get("content", {}).get("rendered", ""))
        feat = featured_url(item)
        if feat:
            uploads.add(feat)
    uploads.add(ORIGIN + "/wp-content/uploads/2023/03/wp-1679338921279.jpg")
    uploads.add(ORIGIN + "/wp-content/uploads/2023/03/screenshot-2023-03-11-221012.png")

    print(f"downloading {len(uploads)} images")
    jobs = []
    for url in sorted(uploads):
        rel = media_rel(url)
        if rel:
            jobs.append((url, ROOT / rel))
    failures = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(download_one, url, dest): url for url, dest in jobs}
        done = 0
        for future in as_completed(futures):
            done += 1
            url, status = future.result()
            if not status.startswith("ok") and status != "cached":
                failures.append((url, status))
                print("MISS", status, url)
            elif done % 20 == 0:
                print(f"  {done}/{len(jobs)}")
    print(f"images done, missing {len(failures)}")

    def local_featured(item: dict) -> str | None:
        url = featured_url(item)
        if not url:
            return None
        rel = media_rel(url)
        if rel and (ROOT / rel).exists():
            return "/" + rel
        return None

    featured_by_path = {post_path(p): local_featured(p) for p in posts}
    featured_by_path = {k: v for k, v in featured_by_path.items() if v}

    icon = public_media(ORIGIN + "/wp-content/uploads/2023/03/wp-1679338921279.jpg") or "/assets/media/2023/03/wp-1679338921279.jpg"
    social = public_media(ORIGIN + "/wp-content/uploads/2023/03/screenshot-2023-03-11-221012.png") or icon

    def content_of(item: dict) -> str:
        return transform_html(item.get("content", {}).get("rendered", ""), featured_by_path)

    # Home: landing copy, then every post newest-first.
    home_desc = plain(home["content"]["rendered"])
    chrono = sorted(posts, key=lambda item: item["date"], reverse=True)
    home_rows = "".join(home_feed_item(post, local_featured(post)) for post in chrono)
    write_page(
        "/",
        "SnatchTv Info",
        home_desc,
        f'<main id="content"><div class="narrow"><h1 class="post-title">{esc(home["title"]["rendered"])}</h1>'
        f'<div class="entry-content">{content_of(home)}</div></div>'
        f'<section class="home-feed wide" aria-label="All posts">'
        f'<h2 class="home-feed-heading">The Feed</h2>'
        f'<ul class="feed">{home_rows}</ul></section></main>',
        "home",
        "home",
        icon,
        social,
    )
    if only == "home":
        print("home feed posts", len(chrono))
        return

    # About Barry
    about = by_slug_page["about-snatchnet"]
    about_html = content_of(about)
    write_page(
        page_path(about),
        about["title"]["rendered"],
        plain(about_html) or "About Barry Gotay Jr.",
        f'<main id="content"><div class="narrow"><h1 class="post-title">{esc(about["title"]["rendered"])}</h1>'
        f'<div class="entry-content">{about_html}</div></div></main>',
        "page",
        "about",
        icon,
        local_featured(about) or social,
    )

    # Link in bio, with its featured image and static carousel.
    bio = by_slug_page["link-in-bio-with-flowery-wallpaper"]
    bio_feat = local_featured(bio)
    bio_fig = f'<figure class="wide featured"><img src="{html.escape(bio_feat)}" alt=""></figure>' if bio_feat else ""
    write_page(
        page_path(bio),
        bio["title"]["rendered"],
        "YouTube, recent posts, and affiliate stores from SnatchTv.",
        f'<main id="content"><div class="narrow"><h1 class="post-title">{esc(bio["title"]["rendered"])}</h1></div>'
        f"{bio_fig}<div class=\"entry-content narrow\">{content_of(bio)}</div></main>",
        "page",
        "page",
        icon,
        bio_feat or social,
    )

    sticky = [p for p in posts if p.get("sticky")]
    rest = [p for p in posts if not p.get("sticky")]
    # WordPress shows sticky posts first on page 1, then the normal stream without repeating them.
    ordered = sticky + rest

    def feed_href(number: int) -> str:
        return "/the-feed/" if number == 1 else f"/the-feed/page/{number}/"

    feed_pages = max(1, (len(ordered) + FEED_PAGE_SIZE - 1) // FEED_PAGE_SIZE)
    # Page 1 capacity is the same size; sticky is already at the front of `ordered`.
    for number in range(1, feed_pages + 1):
        chunk = ordered[(number - 1) * FEED_PAGE_SIZE : number * FEED_PAGE_SIZE]
        items = "".join(feed_item(post, local_featured(post)) for post in chunk)
        main = (
            f'<main id="content" class="wide"><ul class="feed">{items}</ul>'
            f'{pager(number, feed_pages, feed_href)}</main>'
        )
        write_page(
            feed_href(number),
            "The Feed",
            "Complete feed of SnatchTv posts.",
            main,
            "archive feed",
            "feed",
            icon,
            social,
        )

    news = by_slug_page["snatchtv-news-stories-images"]
    news_intro = content_of(news)
    news_feat = local_featured(news)
    news_fig = f'<figure class="wide featured"><img src="{html.escape(news_feat)}" alt=""></figure>' if news_feat else ""

    def news_href(number: int) -> str:
        return page_path(news) if number == 1 else f"{page_path(news)}page/{number}/"

    news_pages = max(1, (len(ordered) + GRID_PAGE_SIZE - 1) // GRID_PAGE_SIZE)
    for number in range(1, news_pages + 1):
        chunk = ordered[(number - 1) * GRID_PAGE_SIZE : number * GRID_PAGE_SIZE]
        cards = "".join(grid_item(post, local_featured(post)) for post in chunk)
        intro = f'<div class="entry-content narrow intro">{news_intro}</div>' if number == 1 else ""
        main = (
            f'<main id="content"><div class="narrow"><h1 class="post-title">{esc(news["title"]["rendered"])}</h1></div>'
            f"{news_fig if number == 1 else ''}"
            f"{intro}<div class=\"wide\"><ul class=\"grid\">{cards}</ul>"
            f"{pager(number, news_pages, news_href)}</div></main>"
        )
        write_page(
            news_href(number),
            news["title"]["rendered"],
            plain(news["content"]["rendered"])[:280],
            main,
            "archive",
            "page",
            icon,
            news_feat or social,
        )

    # Posts
    for index, post in enumerate(ordered):
        dt = parse_date(post["date"])
        name, slug = author_of(post)
        cats = terms_of(post, "category")
        tags = terms_of(post, "post_tag")
        feat = local_featured(post)
        fig = f'<figure class="featured"><img src="{html.escape(feat)}" alt=""></figure>' if feat else ""
        cat_html = "".join(
            f'<a href="/category/{html.escape(term["slug"])}/">{esc(term["name"])}</a>' for term in cats
        )
        tag_html = "".join(
            f'<a href="/tag/{html.escape(term["slug"])}/">{esc(term["name"])}</a>' for term in tags
        )
        terms = ""
        if cat_html or tag_html:
            terms = f'<div class="terms">{cat_html}{tag_html}</div>'
        share_url = quote(LIVE + post_path(post), safe="")
        share_title = quote(plain(post["title"]["rendered"]), safe="")
        comments = ""
        if post.get("comment_status") == "open":
            comments = (
                '<p class="comments-note">Comments were open on WordPress.com and are not hosted on this archive. '
                '<a href="mailto:Bmgotay@gmail.com">Email Barry</a>.</p>'
            )
        main = (
            f'<main id="content"><article class="narrow">'
            f'<h1 class="post-title">{esc(post["title"]["rendered"])}</h1>'
            f'<div class="meta"><a href="/author/{html.escape(slug)}/">{esc(name)}</a>'
            f'<time datetime="{dt.date().isoformat()}">{full_date(dt)}</time></div>'
            f"{fig}<div class=\"entry-content\">{content_of(post)}</div>"
            f'<div class="share"><h2>Share this</h2><p class="share-links">'
            f'<a href="https://twitter.com/intent/tweet?url={share_url}&text={share_title}">X</a>'
            f'<a href="https://www.facebook.com/sharer/sharer.php?u={share_url}">Facebook</a>'
            f"</p></div>{terms}{comments}</article></main>"
        )
        write_page(
            post_path(post),
            post["title"]["rendered"],
            plain(post.get("excerpt", {}).get("rendered") or post["content"]["rendered"]),
            main,
            "single",
            "post",
            icon,
            feat or social,
        )

    def archive(items: list[dict], base: str, title: str, description: str) -> None:
        total = max(1, (len(items) + FEED_PAGE_SIZE - 1) // FEED_PAGE_SIZE)

        def href_for(number: int) -> str:
            return base if number == 1 else f"{base}page/{number}/"

        for number in range(1, total + 1):
            chunk = items[(number - 1) * FEED_PAGE_SIZE : number * FEED_PAGE_SIZE]
            rows = "".join(feed_item(post, local_featured(post)) for post in chunk) or '<li class="empty">No posts.</li>'
            main = (
                f'<main id="content" class="wide"><h1 class="post-title">{esc(title)}</h1>'
                f'<ul class="feed">{rows}</ul>{pager(number, total, href_for)}</main>'
            )
            write_page(href_for(number), title, description, main, "archive", "archive", icon, social)

    categories: dict[str, dict] = {}
    tags: dict[str, dict] = {}
    authors: dict[str, dict] = {}
    for post in ordered:
        for term in terms_of(post, "category"):
            categories.setdefault(term["slug"], {"name": term["name"], "posts": []})["posts"].append(post)
        for term in terms_of(post, "post_tag"):
            tags.setdefault(term["slug"], {"name": term["name"], "posts": []})["posts"].append(post)
        name, slug = author_of(post)
        authors.setdefault(slug, {"name": name, "posts": []})["posts"].append(post)

    for slug, data in categories.items():
        archive(data["posts"], f"/category/{slug}/", data["name"], f"Posts filed under {plain(data['name'])}.")
    for slug, data in tags.items():
        archive(data["posts"], f"/tag/{slug}/", data["name"], f"Posts tagged {plain(data['name'])}.")
    for slug, data in authors.items():
        archive(data["posts"], f"/author/{slug}/", data["name"], f"Posts by {plain(data['name'])}.")

    write_page(
        "/search/",
        "Search",
        "Search SnatchTv posts and pages.",
        '<main id="content" class="wide"><h1 class="post-title">Search</h1>'
        '<form class="search-form" action="/search/" method="get" role="search">'
        '<label class="screen-reader-text" for="q">Search</label>'
        '<input id="q" type="search" name="q" data-search placeholder="Search posts and pages" autocomplete="off">'
        '<button class="button" type="submit">Search</button></form>'
        '<p class="search-status" data-search-status>Type to search posts and pages.</p>'
        '<div data-results></div></main>',
        "search",
        "search",
        icon,
        social,
    )

    not_found = (
        '<main id="content" class="narrow"><h1 class="post-title">Page not found</h1>'
        '<p>That page is not in this archive. Try the <a href="/the-feed/">complete feed</a> '
        'or <a href="/search/">search</a>.</p></main>'
    )
    # 404.html is served from the repo root by GitHub Pages.
    path = ROOT / "404.html"
    # Reuse the shell by writing through write_page then moving.
    write_page("/404-temp/", "Page not found", "Page not found.", not_found, "page", "page", icon, social)
    (ROOT / "404.html").write_text((ROOT / "404-temp" / "index.html").read_text())
    (ROOT / "404-temp" / "index.html").unlink()
    (ROOT / "404-temp").rmdir()

    index = []
    for post in ordered:
        dt = parse_date(post["date"])
        cats = ", ".join(plain(t["name"]) for t in terms_of(post, "category"))
        tags_text = ", ".join(plain(t["name"]) for t in terms_of(post, "post_tag"))
        index.append({
            "title": plain(post["title"]["rendered"]),
            "url": post_path(post),
            "dateLabel": feed_date(dt),
            "text": plain(post.get("excerpt", {}).get("rendered") or "") + " " + plain(post["content"]["rendered"])[:600],
            "terms": cats + " " + tags_text,
            "image": local_featured(post) or "",
        })
    for page in pages:
        if page["slug"] == "the-feed":
            continue
        path = page_path(page)
        page_text = plain(content_of(page) if page["slug"] == "about-snatchnet" else page["content"]["rendered"])
        index.append({
            "title": plain(page["title"]["rendered"]),
            "url": path,
            "dateLabel": "",
            "text": page_text[:800],
            "terms": "page",
            "image": local_featured(page) or "",
        })
    (ROOT / "assets" / "search-index.json").write_text(json.dumps(index, ensure_ascii=False))
    (ROOT / ".nojekyll").write_text("")
    print("wrote", len(list(ROOT.rglob('index.html'))), "html files")
    print("search", len(index))


if __name__ == "__main__":
    import sys
    main(only="home" if "--home-only" in sys.argv else None)

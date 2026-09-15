"""Bounded fallback HTML text extraction; not a full article-extraction engine."""

from html.parser import HTMLParser
from email.message import Message
from urllib.parse import urljoin


def utf8_content_type(value):
    if not isinstance(value, str):
        raise ValueError("Missing content type")
    header = Message()
    header["content-type"] = value
    params = header.get_params() or []
    charsets = [item for key, item in params[1:] if key.lower() == "charset"]
    if len(charsets) > 1 or any(charset.lower() not in {"utf-8", "utf8"} for charset in charsets):
        raise ValueError("Only UTF-8 content is supported")
    return value.split(";", 1)[0].strip().lower()


class DocumentParser(HTMLParser):
    def __init__(self, url, max_text=20000):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.max_text = max_text
        self.parts = []
        self.length = 0
        self.truncated = False
        self.hidden = []
        self.in_title = False
        self.title = ""
        self.links = []
        self.seen = set()

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden.append(tag)
        if tag == "title":
            self.in_title = True
        if self.hidden:
            return
        if tag == "a" and len(self.links) < 20:
            href = dict(attrs).get("href")
            if href and len(href) <= 2048:
                target = urljoin(self.url, href)
                if target.startswith("https://") and target not in self.seen:
                    self.seen.add(target)
                    self.links.append(target)

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.hidden:
            return
        cleaned = " ".join(data.split())
        if self.in_title:
            self.title = (self.title + cleaned)[:2000]
            return
        if not cleaned:
            return
        remaining = self.max_text - self.length
        if remaining <= 0:
            self.truncated = True
            return
        piece = ("\n" if self.parts else "") + cleaned
        self.parts.append(piece[:remaining])
        self.length += min(len(piece), remaining)
        self.truncated = self.truncated or len(piece) > remaining

    def document(self, requested_url):
        return {"title": self.title or None, "requested_url": requested_url, "final_url": self.url,
                "text": "".join(self.parts), "truncated": self.truncated, "links": self.links,
                "source_capability": "bounded-https", "extraction": "html-text-no-javascript"}

"""쇼핑몰 '이용안내' 페이지(회사가 작성한 정책 안내)만 수집한다.

- robots.txt가 허용하지 않으면 건너뛴다.
- 수집 목적을 밝힌 User-Agent를 쓰고, 요청 사이에 고정 간격을 둔다.
- 고객 Q&A 게시판은 개인정보가 섞여 있으므로 대상에 넣지 않는다.

실행: py -3.14 scripts/collect_guides.py
결과: data/raw/<사이트>.txt (출처 URL과 수집 시각 포함)
"""
import re
import sys
import time
import urllib.request
import urllib.robotparser
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

USER_AGENT = "KangnamUniv-StudentProject-QnaDesk/0.1 (course research; shop policy pages only)"
DELAY_SECONDS = 5

TARGETS = {
    "66girls": "https://66girls.co.kr/shopinfo/guide.html",
    "hotping": "https://www.hotping.co.kr/shopinfo/guide.html",
    "slowand": "https://www.slowand.com/shopinfo/guide.html",
    "miamasvin": "https://www.miamasvin.co.kr/shopinfo/guide.html",
    "blackup": "https://blackup.kr/shopinfo/guide.html",
}

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


class TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "dt", "dd", "table", "section"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)

    def text(self):
        lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def allowed(url):
    parts = urlparse(url)
    robots = urllib.robotparser.RobotFileParser(f"{parts.scheme}://{parts.netloc}/robots.txt")
    robots.read()
    return robots.can_fetch(USER_AGENT, url)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in TARGETS.items():
        try:
            if not allowed(url):
                print(f"[건너뜀] {name}: robots.txt가 허용하지 않음")
                continue
            time.sleep(DELAY_SECONDS)
            parser = TextExtractor()
            parser.feed(fetch(url))
            text = parser.text()
        except Exception as error:
            print(f"[실패] {name}: {error}")
            continue
        header = f"source_url: {url}\ncollected_at: {datetime.now():%Y-%m-%d %H:%M}\n\n"
        (OUT_DIR / f"{name}.txt").write_text(header + text, encoding="utf-8")
        print(f"[저장] {name}: {len(text)}자")
        time.sleep(DELAY_SECONDS)


if __name__ == "__main__":
    sys.exit(main())

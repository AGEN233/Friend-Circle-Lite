"""Feed discovery, parsing, and incremental tracking services."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

from friend_circle_lite import HEADERS_XML, timeout
from friend_circle_lite.models import Article, FeedEndpoint
from friend_circle_lite.utils.time import format_published_time
from friend_circle_lite.utils.url import replace_non_domain


class FeedDiscoveryService:
    """Discover an RSS or Atom endpoint for a website."""

    POSSIBLE_FEEDS = [
        ("rss1", "/feed"),
        ("rss2", "/feed/"),
        ("rss3", "/rss.xml"),
        ("rss4", "/atom.xml"),
        ("rss5", "/feed.xml"),
        ("rss6", "/index.xml"),
        ("rss7", "/feed.atom"),
        ("rss8", "/rss2.xml"),
        ("rss9", "/rss/feed.xml"),
        ("rss10", "/rss.php"),
        ("rss11", "/feed.php"),
    ]

    def __init__(self, session: requests.Session):
        self.session = session

    def discover(self, website_url: str) -> FeedEndpoint | None:
        """Try common feed endpoints and return the first valid match."""
        for feed_type, path in self.POSSIBLE_FEEDS:
            feed_url = website_url.rstrip("/") + path

            try:
                response = self.session.get(feed_url, headers=HEADERS_XML, timeout=timeout)
            except requests.RequestException as exc:
                logging.warning(f"探测 RSS 失败：{feed_url}，网络错误：{exc}")
                continue

            ok, reason = FeedParserService.check_feed_response(response, feed_url)
            if not ok:
                logging.info(f"跳过 RSS 探测地址：{feed_url}，原因：{reason}")
                continue

            return FeedEndpoint(url=feed_url, feed_type=feed_type, source="auto")

        logging.warning(f"未找到 {website_url} 的 RSS 订阅源")
        return None


class FeedParserService:
    """Parse a discovered feed into normalized article objects."""

    def __init__(self, session: requests.Session):
        self.session = session

    @staticmethod
    def check_feed_response(response: requests.Response, feed_url: str) -> tuple[bool, str]:
        """Check whether HTTP response is likely a real RSS/Atom feed."""
        status = response.status_code
        content_type = response.headers.get("content-type", "").lower()
        text_head = response.text[:3000].lower()

        if status == 403:
            if "just a moment" in text_head or "challenges.cloudflare.com" in text_head:
                return False, "被 Cloudflare 挑战页拦截：GitHub Actions / 云服务器 IP 需要浏览器 JS 验证"
            if "region is forbidden" in text_head:
                return False, "被地区/IP策略拦截：服务端返回 region is forbidden"
            return False, f"HTTP 403 Forbidden：目标站拒绝当前运行环境访问"

        if status >= 400:
            return False, f"HTTP {status}：目标站返回错误状态"

        if "just a moment" in text_head or "challenges.cloudflare.com" in text_head:
            return False, "被 Cloudflare 验证页拦截：返回的是 HTML 验证页，不是 RSS"

        if "region is forbidden" in text_head:
            return False, "被地区/IP策略拦截：服务端返回 region is forbidden"

        has_feed_tag = (
            "<rss" in text_head
            or "<feed" in text_head
            or "<rdf:rdf" in text_head
        )

        if has_feed_tag:
            return True, "OK"

        if "html" in content_type or "<!doctype html" in text_head or "<html" in text_head:
            return False, "返回的是 HTML 页面，不是 RSS/XML；可能是验证页、错误页或站点首页"

        if "xml" in content_type or "rss" in content_type or "atom" in content_type:
            return True, "OK"

        return False, f"返回内容不像 RSS/Atom，Content-Type={content_type or 'unknown'}"

    def parse(self, feed_url: str, count: int = 5, blog_url: str = "") -> list[Article]:
        """Parse a feed URL and return the newest `count` articles."""
        try:
            response = self.session.get(feed_url, headers=HEADERS_XML, timeout=timeout)
            response.encoding = "utf-8"

            ok, reason = self.check_feed_response(response, feed_url)
            if not ok:
                logging.warning(f"RSS 请求被跳过：{feed_url}，原因：{reason}")
                logging.warning(
                    "响应摘要：status=%s, content-type=%s, final-url=%s, head=%s",
                    response.status_code,
                    response.headers.get("content-type", ""),
                    response.url,
                    response.text[:300].replace("\n", " "),
                )
                return []

            feed = feedparser.parse(response.text)

        except Exception as exc:
            logging.error(f"解析 RSS 失败：{feed_url}，错误: {exc}")
            return []

        if getattr(feed, "bozo", False):
            logging.warning(f"RSS 可能存在格式问题：{feed_url}，错误：{getattr(feed, 'bozo_exception', '')}")

        if not feed.entries:
            logging.warning(f"RSS 未解析出文章：{feed_url}，但响应看起来是 RSS/XML")
            return []

        default_author = feed.feed.author if "author" in feed.feed else ""
        articles: list[Article] = []

        for entry in feed.entries:
            published = self._extract_published_time(entry)
            article_link = replace_non_domain(entry.link, blog_url) if "link" in entry else ""

            article = Article(
                title=entry.title if "title" in entry else "",
                author=default_author,
                link=article_link,
                published=published,
                summary=entry.summary if "summary" in entry else "",
                content=entry.content[0].value
                if "content" in entry and entry.content
                else entry.description
                if "description" in entry
                else "",
            )
            articles.append(article)

        valid_articles = [article for article in articles if article.published]

        valid_articles_with_dates = []
        for article in valid_articles:
            try:
                parsed_date = datetime.strptime(article.published, "%Y-%m-%d %H:%M")
                valid_articles_with_dates.append((article, parsed_date))
            except ValueError:
                logging.warning(f"文章 {article.title} 的发布时间格式异常: {article.published}，已跳过")

        valid_articles_with_dates.sort(key=lambda item: item[1], reverse=True)
        sorted_articles = [item[0] for item in valid_articles_with_dates]

        return sorted_articles[:count] if count < len(sorted_articles) else sorted_articles

    @staticmethod
    def _extract_published_time(entry) -> str:
        """Extract a normalized publish time from a feed entry."""
        import time

        def convert_time_to_string(time_value):
            if isinstance(time_value, str):
                return time_value

            if isinstance(time_value, time.struct_time):
                if time_value.tm_year < 1900:
                    logging.warning(
                        f"文章 {entry.get('title', 'Unknown')} 的时间年份异常: {time_value.tm_year}，已跳过"
                    )
                    return ""
                return time.strftime("%Y-%m-%dT%H:%M:%SZ", time_value)

            logging.warning(
                f"文章 {entry.get('title', 'Unknown')} 的时间格式未知: {type(time_value)}，已跳过"
            )
            return ""

        if "published" in entry:
            time_str = convert_time_to_string(entry.published)
            if not time_str:
                return ""
            return format_published_time(time_str)

        if "updated" in entry:
            time_str = convert_time_to_string(entry.updated)
            if not time_str:
                return ""
            published = format_published_time(time_str)
            logging.warning(f"文章 {entry.title} 未包含发布时间，已使用更新时间 {published}")
            return published

        logging.warning(f"文章 {entry.title} 未包含任何时间信息, 请检查原文, 跳过该文章")
        return ""


class LatestArticleTracker:
    """Track whether a website published new posts since the last crawl."""

    def __init__(self, storage_path: str | Path, max_tracked_articles: int = 10):
        from friend_circle_lite.cache_store import ArticleTrackingStore

        self.store = ArticleTrackingStore(storage_path, max_tracked_articles)

    def diff_and_persist(self, latest_articles: list[Article]) -> list[dict] | None:
        previous_articles = self.store.load_articles()

        if not previous_articles:
            logging.info("首次运行：跳过推送以防止发送旧文章")
            self.store.save_articles(latest_articles)
            return None

        previous_latest_date = self._get_latest_date(previous_articles)

        new_articles = []
        for article in latest_articles:
            if self._is_truly_new_article(article, previous_articles):
                new_articles.append(article)

        if not new_articles:
            self.store.save_articles(latest_articles)
            return None

        truly_new_articles = []
        for article in new_articles:
            if not article.published:
                continue

            try:
                article_date = datetime.strptime(article.published, "%Y-%m-%d %H:%M")
                if previous_latest_date is None or article_date > previous_latest_date:
                    truly_new_articles.append(article)
            except Exception as exc:
                logging.warning(f"解析文章日期失败: {article.title}, 日期: {article.published}, 错误: {exc}")
                continue

        self.store.save_articles(latest_articles)

        if truly_new_articles:
            logging.info(f"发现 {len(truly_new_articles)} 篇新文章（日期比之前更新）")
            return [article.to_tracking_dict() for article in truly_new_articles]

        logging.info(f"发现 {len(new_articles)} 篇新文章，但日期不够新，跳过推送")
        return None

    @staticmethod
    def _is_truly_new_article(article: Article, previous_articles: list[Article]) -> bool:
        for prev in previous_articles:
            if article.link and article.link == prev.link:
                return False
            if article.title and article.title == prev.title:
                return False
            if article.published and article.published == prev.published:
                return False

        return True

    @staticmethod
    def _get_latest_date(articles: list[Article]) -> datetime | None:
        latest_date = None

        for article in articles:
            if not article.published:
                continue

            try:
                article_date = datetime.strptime(article.published, "%Y-%m-%d %H:%M")
                if latest_date is None or article_date > latest_date:
                    latest_date = article_date
            except Exception:
                continue

        return latest_date


def extract_blog_origin(url: str) -> str:
    """Return a normalized origin for display or author profile links."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}"
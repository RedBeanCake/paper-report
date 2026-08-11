"""Paper Report: arXiv screening, full-text deep dive, archive pages and Feishu delivery.

The manual deep-dive path uses this reading order when an arXiv HTML page is
available:

1. Abstract
2. Experiments / results / evaluation / ablation
3. Method / approach / architecture / algorithm
4. Introduction / conclusion / discussion
5. Other top-level sections
6. Related work / background
7. Appendix / acknowledgements / ethics

The selected sections are restored to their original order before being sent
to the model. The full-text budget is 90,000 characters per paper.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from openai import OpenAI


LOGGER = logging.getLogger(__name__)
FULLTEXT_BUDGET = 90000
REQUEST_ATTEMPTS = 3
REQUEST_BACKOFF_SECONDS = 2
MODEL_NAME = os.getenv("PAPER_REPORT_MODEL", "qwen3.7-plus")
CATEGORIES = [item.strip() for item in os.getenv("ARXIV_CATEGORIES", "cs.RO").split(",") if item.strip()]
HEADERS = {
    "User-Agent": "paper-report/2.0 (+https://github.com/RedBeanCake/paper-report)",
}

RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
ARXIV_ID_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf|html)/|arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)",
    re.IGNORECASE,
)

# Lower number means higher priority during the 90k-character selection pass.
SECTION_PRIORITY: Sequence[Tuple[re.Pattern[str], int]] = (
    (
        re.compile(r"experiment|result|evaluation|ablation|benchmark|analysis", re.IGNORECASE),
        0,
    ),
    (
        re.compile(r"method|approach|model|architecture|framework|algorithm|implementation", re.IGNORECASE),
        1,
    ),
    (
        re.compile(r"introduction|conclusion|discussion|motivation", re.IGNORECASE),
        2,
    ),
    (re.compile(r"related work|background|preliminar", re.IGNORECASE), 4),
    (re.compile(r"appendix|acknowledg|impact statement|ethic", re.IGNORECASE), 5),
)


class ReportError(RuntimeError):
    """Raised when a report stage cannot produce a trustworthy result."""


@dataclass
class PaperMetadata:
    paper_id: str
    title: str = ""
    abstract: str = ""
    authors: str = ""
    published: str = ""


def _env_client() -> OpenAI:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise ReportError("DASHSCOPE_API_KEY is not configured")
    return OpenAI(api_key=api_key, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")


client_llm = _env_client()
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
TARGET_CHAT_ID = os.getenv("CHAT_ID", "")
REPO_FULL_NAME = os.getenv("GITHUB_REPOSITORY", "owner/repo")
REPO_OWNER = os.getenv("GITHUB_REPOSITORY_OWNER", REPO_FULL_NAME.split("/", 1)[0])
REPO_NAME = REPO_FULL_NAME.split("/")[-1]
GITHUB_PAGES_URL = os.getenv(
    "GITHUB_PAGES_URL", f"https://{REPO_OWNER}.github.io/{REPO_NAME}/"
)

FILTER_WARNINGS: List[str] = []
FEISHU_TENANT_TOKEN: Dict[str, Any] = {"token": None, "expire": 0.0}


def _request(
    method: str,
    url: str,
    *,
    timeout: float = 20,
    headers: Optional[Dict[str, str]] = None,
    **kwargs: Any,
) -> requests.Response:
    last_error: Optional[BaseException] = None
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            response = requests.request(
                method,
                url,
                headers=headers or HEADERS,
                timeout=timeout,
                **kwargs,
            )
            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = ReportError(f"HTTP {response.status_code}: {url}")
                if attempt < REQUEST_ATTEMPTS - 1:
                    time.sleep(REQUEST_BACKOFF_SECONDS**attempt)
                    continue
            else:
                response.raise_for_status()
                return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < REQUEST_ATTEMPTS - 1:
                time.sleep(REQUEST_BACKOFF_SECONDS**attempt)
                continue
    raise ReportError(f"request failed after {REQUEST_ATTEMPTS} attempts: {url}") from last_error


def _llm(prompt: str, *, response_format: Optional[Dict[str, str]] = None) -> str:
    last_error: Optional[BaseException] = None
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            kwargs: Dict[str, Any] = {
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": prompt}],
            }
            if response_format:
                kwargs["response_format"] = response_format
            completion = client_llm.chat.completions.create(**kwargs)
            content = completion.choices[0].message.content
            if content:
                return content
            raise ReportError("LLM returned empty content")
        except Exception as exc:
            last_error = exc
            LOGGER.warning("LLM attempt %s/%s failed: %s", attempt + 1, REQUEST_ATTEMPTS, exc)
            if attempt < REQUEST_ATTEMPTS - 1:
                time.sleep(REQUEST_BACKOFF_SECONDS**attempt)
    raise ReportError("LLM call failed after retries") from last_error


def extract_arxiv_id(text: str) -> Optional[str]:
    match = ARXIV_ID_RE.search(text)
    return match.group(1).split("v", 1)[0] if match else None


def parse_input_items(raw: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for token in re.split(r"[\n,，;；\s]+", raw.strip()):
        if not token:
            continue
        if token.startswith(("http://", "https://")):
            paper_id = extract_arxiv_id(token) if "arxiv.org" in token.lower() else None
            if paper_id:
                items.append({"id": paper_id})
            else:
                slug = hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]
                items.append({"url": token, "slug": slug})
        else:
            paper_id = extract_arxiv_id(token)
            if paper_id:
                items.append({"id": paper_id})
    return items


def _section_priority(heading: str) -> int:
    for pattern, priority in SECTION_PRIORITY:
        if pattern.search(heading):
            return priority
    return 3


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _strip_arxiv_version(paper_id: str) -> str:
    return paper_id.split("v", 1)[0]


def get_arxiv_metadata(paper_id: str) -> PaperMetadata:
    """Fetch authoritative title/abstract metadata from arXiv Atom API."""

    normalized_id = _strip_arxiv_version(paper_id)
    response = _request(
        "GET",
        "https://export.arxiv.org/api/query",
        timeout=15,
        params={"id_list": normalized_id},
    )
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise ReportError(f"invalid arXiv metadata XML for {normalized_id}") from exc

    entry = next((node for node in root.iter() if node.tag.endswith("entry")), None)
    if entry is None:
        return PaperMetadata(normalized_id)

    title = ""
    abstract = ""
    authors: List[str] = []
    published = ""
    for child in entry:
        if child.tag.endswith("title") and child.text:
            title = _clean_text(child.text)
        elif child.tag.endswith("summary") and child.text:
            abstract = _clean_text(child.text)
        elif child.tag.endswith("published") and child.text:
            published = _clean_text(child.text)
        elif child.tag.endswith("author"):
            name = next((n.text for n in child if n.tag.endswith("name") and n.text), "")
            if name:
                authors.append(_clean_text(name))
    return PaperMetadata(normalized_id, title, abstract, ", ".join(authors), published)


def scrape_arxiv(category: str) -> Tuple[Optional[Dict[str, str]], int, List[Dict[str, str]]]:
    """Fetch recent arXiv papers for the screening workflow."""

    url = f"https://arxiv.org/list/{category}/recent?show=500"
    try:
        soup = BeautifulSoup(_request("GET", url, timeout=20).text, "html.parser")
        article_list = soup.find("dl", id="articles")
        if not article_list:
            raise ReportError("arXiv page has no article list")

        headings = soup.find_all("h3")
        raw_date = _clean_text(headings[0].get_text(" ", strip=True)) if headings else ""
        match = re.search(r"^(.*)\(showing \d+ of (\d+) entries", raw_date)
        date_prefix = match.group(1).strip() if match else raw_date
        total_entries = match.group(2) if match else "0"

        papers: List[Dict[str, str]] = []
        for dt_tag, dd_tag in zip(article_list.find_all("dt"), article_list.find_all("dd")):
            link = dt_tag.find("a", title="Abstract")
            title_tag = dd_tag.find("div", class_="list-title")
            abstract_tag = dd_tag.find("p", class_="mathjax")
            if not link or not title_tag:
                continue
            paper_id = link.get_text(strip=True).replace("arXiv:", "")
            papers.append(
                {
                    "id": paper_id,
                    "title": _clean_text(title_tag.get_text(" ", strip=True).replace("Title:", "")),
                    "abstract": _clean_text(abstract_tag.get_text(" ", strip=True))[:4000]
                    if abstract_tag
                    else "",
                }
            )
        return {"prefix": date_prefix, "total": total_entries}, len(papers), papers
    except Exception as exc:
        warning = f"⚠️ 抓取/解析 arXiv {category} 失败：{type(exc).__name__}: {exc}"
        FILTER_WARNINGS.append(warning)
        LOGGER.exception(warning)
        return None, 0, []


def get_arxiv_full_text(paper_id: str) -> Optional[str]:
    """Fetch arXiv HTML and select up to 90k chars by section priority.

    Sections are ranked for selection, but the final concatenation follows the
    paper's original document order. This keeps the model's reading order
    natural while protecting the method and experiment sections from a hard
    tail truncation.
    """

    normalized_id = _strip_arxiv_version(paper_id)
    try:
        soup = BeautifulSoup(
            _request("GET", f"https://arxiv.org/html/{normalized_id}", timeout=30).text,
            "html.parser",
        )
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        references = soup.find_all(
            ["section", "div"], class_=re.compile(r"bibliography|references", re.IGNORECASE)
        )
        references += soup.find_all(
            ["section", "div"], id=re.compile(r"bib|references", re.IGNORECASE)
        )
        for tag in references:
            tag.decompose()

        parts: List[Tuple[str, int, str]] = []
        abstract = soup.find("div", class_=re.compile(r"ltx_abstract", re.IGNORECASE))
        if abstract:
            parts.append(("ABSTRACT", 0, _clean_text(abstract.get_text(" ", strip=True))))

        sections = [section for section in soup.find_all("section") if section.find_parent("section") is None]
        for section in sections:
            heading_tag = section.find(["h1", "h2", "h3", "h4"])
            heading = _clean_text(heading_tag.get_text(" ", strip=True)) if heading_tag else "(untitled)"
            text = _clean_text(section.get_text(" ", strip=True))
            if text:
                parts.append((heading, _section_priority(heading), text))

        if not parts:
            body = _clean_text(soup.get_text(" ", strip=True))
            return body[:FULLTEXT_BUDGET] or None

        ranked_indices = sorted(range(len(parts)), key=lambda index: (parts[index][1], index))
        kept: set[int] = set()
        used = 0
        dropped: List[str] = []
        for index in ranked_indices:
            heading, _, text = parts[index]
            if used + len(text) <= FULLTEXT_BUDGET:
                kept.add(index)
                used += len(text)
            else:
                dropped.append(heading)

        if dropped:
            LOGGER.info("[%s] dropped low-priority sections: %s", normalized_id, ", ".join(dropped[:8]))
        chunks = [f"## {parts[index][0]}\n{parts[index][2]}" for index in range(len(parts)) if index in kept]
        return "\n\n".join(chunks) or None
    except Exception as exc:
        LOGGER.warning("full-text fetch failed for %s: %s", normalized_id, exc)
        return None


def get_webpage_text(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract readable title and body from a non-arXiv webpage."""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, None
    try:
        soup = BeautifulSoup(_request("GET", url, timeout=30).text, "html.parser")
        title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else url
        body = soup.find("article") or soup.find("main") or soup.body or soup
        for tag in body(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        text = re.sub(r"\n{3,}", "\n\n", body.get_text("\n", strip=True))
        text = re.sub(r"[ \t]{2,}", " ", text)
        return title or url, text[:FULLTEXT_BUDGET] or None
    except Exception as exc:
        LOGGER.warning("webpage fetch failed for %s: %s", url, exc)
        return None, None


def _title_hint(metadata: PaperMetadata) -> str:
    if metadata.title:
        return f"""
【可信论文元数据】
英文原文标题：{metadata.title}
论文摘要：{metadata.abstract or "（摘要未提供）"}

标题规则：
- “英文标题”字段必须逐字复制上面的英文原文标题。
- 不要把关键词、方法名或主题概括当成论文标题。
- 不要输出“原文未提供完整标题”，因为标题已经由 arXiv 元数据提供。
"""
    return """
【标题规则】
可信标题元数据不可用。英文标题必须写“标题元数据不可用”，不要根据关键词猜测标题。
"""


def build_expert_prompt(
    paper_id: str,
    full_text: Optional[str],
    *,
    metadata: Optional[PaperMetadata] = None,
    source_label: str = "",
    text_kind: str = "按章节优先级裁剪的正文",
) -> str:
    metadata = metadata or PaperMetadata(paper_id)
    source_hint = f"\n解析对象：{source_label}" if source_label else ""
    content = full_text or "（全文与摘要均不可用；所有无法确认的字段写‘原文未提及’，不要猜测。）"
    return f"""
Role: 你是一位资深的 AI/计算机科学研究员，能快速读懂论文或技术文章。
Task: 用平实、地道的中文做高信息密度总结，直接讲清楚工作做了什么、改了哪里、效果如何。
{source_hint}

{_title_hint(metadata)}

输入说明：下方内容是{ text_kind }。低优先级章节可能已被删除。
只允许依据实际输入作答。找不到依据的字段写“原文未提及”，严禁猜测，尤其是数据集、baseline、超参数和具体实验数值。

请严格按以下结构输出 Markdown，字段名和层级不要改变：

**0. 论文标题**
- **英文标题**: [逐字复制可信元数据标题]
- **中文标题**: [精准中文翻译]
- **研究机构**: [作者所属的主要单位；无法确认则写原文未提及]
- **一句话总结**: [核心贡献]

**1. 整体逻辑**
- **研究任务**: ...
- **研究动机**: ...
- **本质改动**: ...
- **技术溯源**: ...

**2. 技术拆解**
- **核心方法**: ...
- **关键细节**: ...
- **训练/优化目标**: ...

**3. 实验结果**
- **实验设置**: ...
- **评价指标**: ...
- **主要结果**: ...

待处理内容（{text_kind}）：
{content}
"""


def _extract_field(report: str, label: str) -> str:
    pattern = rf"(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*[:：]\s*(.+)"
    match = re.search(pattern, report, re.IGNORECASE)
    return _clean_text(match.group(1)) if match else ""


def _safe_title(value: str, fallback: str) -> str:
    cleaned = _clean_text(value)
    if not cleaned or cleaned.startswith(("原文未", "标题元数据不可用")):
        return fallback
    return cleaned


def deep_dive_paper(item: Dict[str, str], index: int) -> Optional[Dict[str, str]]:
    """Deep-read one arXiv paper or arbitrary URL and return archive data."""

    if item.get("url"):
        url = item["url"]
        title, body = get_webpage_text(url)
        if not body:
            raise ReportError(f"could not fetch readable body: {url}")
        report = _llm(
            build_expert_prompt(
                "",
                body,
                source_label=url,
                text_kind="网页正文",
            )
        )
        english_title = _safe_title(_extract_field(report, "英文标题"), title or url)
        chinese_title = _extract_field(report, "中文标题")
        markdown = (
            f"### {index}. [{english_title}]({url})\n"
            f"- **来源链接**: `{url}`\n\n{report}\n\n---\n"
        )
        return {
            "slug": item.get("slug", hashlib.sha256(url.encode()).hexdigest()[:10]),
            "title": english_title,
            "title_zh": chinese_title,
            "report": report,
            "markdown": markdown,
            "source_url": url,
            "kind": "url",
        }

    paper_id = _strip_arxiv_version(item["id"])
    metadata = get_arxiv_metadata(paper_id)
    full_text = get_arxiv_full_text(paper_id)
    text_kind = "按章节优先级裁剪的全文" if full_text else "arXiv 摘要"
    if not full_text:
        full_text = metadata.abstract
    report = _llm(
        build_expert_prompt(
            paper_id,
            full_text,
            metadata=metadata,
            text_kind=text_kind,
        )
    )

    # The outer title is always metadata-first. Model title output is only a fallback.
    model_title = _extract_field(report, "英文标题")
    english_title = _safe_title(model_title, metadata.title or f"arXiv: {paper_id}")
    if metadata.title:
        english_title = metadata.title
    chinese_title = _extract_field(report, "中文标题")
    affiliation = _extract_field(report, "研究机构") or "原文未提及"
    markdown = (
        f"### {index}. 🔥 [{english_title}](https://arxiv.org/abs/{paper_id})"
        f" ({chinese_title})\n"
        f"- **研究机构**: `{affiliation}`\n"
        f"- **ArXiv ID**: `{paper_id}` | [点击跳转](https://arxiv.org/abs/{paper_id})\n\n"
        f"{report}\n\n---\n"
    )
    return {
        "slug": paper_id,
        "title": english_title,
        "title_zh": chinese_title,
        "report": report,
        "markdown": markdown,
        "source_url": f"https://arxiv.org/abs/{paper_id}",
        "paper_id": paper_id,
        "kind": "arxiv",
    }


def _safe_script_json(value: str) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _render_page(title: str, body_markdown: str, *, back_link: bool) -> str:
    safe_title = html.escape(title, quote=True)
    safe_body = _safe_script_json(body_markdown)
    back = '<a href="../index.html">&#8592; 返回主索引</a>' if back_link else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{safe_title}</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.2.0/github-markdown.min.css">
  <script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js" defer></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js" defer></script>
  <style>.markdown-body {{ box-sizing: border-box; max-width: 980px; margin: 0 auto; padding: 30px; }} @media (max-width: 767px) {{ .markdown-body {{ padding: 15px; }} }}</style>
</head>
<body class="markdown-body">
  {back}
  <h1>{safe_title}</h1>
  <div id="content"></div>
  <script type="application/json" id="raw-markdown">{safe_body}</script>
  <script>
    window.addEventListener("DOMContentLoaded", function () {{
      const markdown = JSON.parse(document.getElementById("raw-markdown").textContent);
      const target = document.getElementById("content");
      if (!window.marked || !window.DOMPurify) {{ target.textContent = markdown; return; }}
      target.innerHTML = DOMPurify.sanitize(marked.parse(markdown));
    }});
  </script>
</body>
</html>
"""


def generate_single_page(entry: Dict[str, str], output_dir: str = ".") -> str:
    archive_dir = os.path.join(output_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    safe_slug = re.sub(r"[^\w.-]", "_", entry["slug"])
    path = os.path.join(archive_dir, f"{safe_slug}.html")
    title = entry["title"]
    if entry.get("title_zh"):
        title = f"{title} ({entry['title_zh']})"
    if entry.get("paper_id"):
        title = f"Paper Report - {entry['paper_id']} - {title}"
    with open(path, "w", encoding="utf-8") as file:
        file.write(_render_page(title, entry["markdown"], back_link=True))

    metadata = {
        "slug": safe_slug,
        "title": title,
        "kind": entry.get("kind", "arxiv"),
        "ts": dt.datetime.now().strftime("%Y%m%d%H%M%S"),
    }
    if entry.get("paper_id"):
        metadata["paper_id"] = entry["paper_id"]
    with open(os.path.join(archive_dir, f".meta.{safe_slug}.json"), "w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False)
    return path


def rebuild_index(output_dir: str = ".") -> None:
    archive_dir = os.path.join(output_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    rows: List[Tuple[float, str, str]] = []
    for filename in os.listdir(archive_dir):
        if not filename.endswith(".html") or filename.startswith("."):
            continue
        path = os.path.join(archive_dir, filename)
        meta_path = os.path.join(archive_dir, f".meta.{filename[:-5]}.json")
        title = filename
        timestamp = os.path.getmtime(path)
        try:
            if os.path.exists(meta_path):
                with open(meta_path, "r", encoding="utf-8") as file:
                    metadata = json.load(file)
                title = str(metadata.get("title", title))
                timestamp = float(metadata.get("ts", timestamp))
            else:
                with open(path, "r", encoding="utf-8") as file:
                    soup = BeautifulSoup(file.read(), "html.parser")
                title = soup.title.get_text(strip=True) if soup.title else filename
        except Exception as exc:
            LOGGER.warning("could not index %s: %s", filename, exc)
        rows.append((timestamp, title, filename))

    rows.sort(key=lambda row: row[0], reverse=True)
    markdown = "### 📅 解析存档列表\n\n" + "\n".join(
        f"- [{title}]({html.escape('archive/' + filename, quote=True)})" for _, title, filename in rows
    )
    index_path = os.path.join(output_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as file:
        file.write(_render_page("Paper Report - 解析存档", markdown, back_link=False))


def get_feishu_tenant_token() -> Optional[str]:
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
        return None
    if FEISHU_TENANT_TOKEN["token"] and time.time() < FEISHU_TENANT_TOKEN["expire"] - 60:
        return str(FEISHU_TENANT_TOKEN["token"])
    try:
        response = _request(
            "POST",
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            timeout=10,
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        )
        data = response.json()
        token = data.get("tenant_access_token")
        if token:
            FEISHU_TENANT_TOKEN["token"] = token
            FEISHU_TENANT_TOKEN["expire"] = time.time() + int(data.get("expire", 7200))
        return token
    except Exception as exc:
        LOGGER.warning("Feishu token request failed: %s", exc)
        return None


def _md_to_feishu_elements(content: str) -> List[Dict[str, Any]]:
    elements: List[Dict[str, Any]] = []
    buffer: List[str] = []

    def flush() -> None:
        if buffer:
            text = "\n".join(buffer).strip()
            if text:
                elements.append({"tag": "div", "text": {"tag": "lark_md", "content": text}})
            buffer.clear()

    for line in content.splitlines():
        stripped = line.strip()
        if stripped in {"---", "***", "___"}:
            flush()
            elements.append({"tag": "hr"})
        elif re.match(r"^#{1,4}\s", stripped):
            flush()
            heading = re.sub(r"^#{1,4}\s*", "", stripped)
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{heading}**"}})
        elif stripped:
            buffer.append(stripped)
    flush()
    return elements


def send_feishu_markdown_to_chat(content: str, *, title: str, paper_url: str) -> bool:
    token = get_feishu_tenant_token()
    if not (TARGET_CHAT_ID and token):
        return False
    card = {
        "header": {"title": {"tag": "plain_text", "content": title[:100]}, "template": "blue"},
        "elements": ([{"tag": "div", "text": {"tag": "lark_md", "content": f"📎 [查看原文]({paper_url})"}}]
                     + _md_to_feishu_elements(content)),
    }
    try:
        response = _request(
            "POST",
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            timeout=15,
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": TARGET_CHAT_ID, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        )
        return response.status_code == 200
    except Exception as exc:
        LOGGER.warning("Feishu app message failed: %s", exc)
        return False


def send_feishu_webhook(text: str) -> None:
    if not FEISHU_WEBHOOK:
        return
    try:
        _request("POST", FEISHU_WEBHOOK, timeout=10, json={"msg_type": "text", "content": {"text": text}})
    except Exception as exc:
        LOGGER.warning("Feishu webhook failed: %s", exc)


def send_feishu_card(title: str, message: str) -> None:
    if not FEISHU_WEBHOOK:
        return
    try:
        _request(
            "POST",
            FEISHU_WEBHOOK,
            timeout=10,
            json={
                "msg_type": "interactive",
                "card": {
                    "header": {"title": {"tag": "plain_text", "content": title[:100]}, "template": "blue"},
                    "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": message}},
                        {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "查看网页"}, "type": "primary", "url": GITHUB_PAGES_URL}]},
                    ],
                },
            },
        )
    except Exception as exc:
        LOGGER.warning("Feishu card failed: %s", exc)


def _extract_json_array(text: str) -> Optional[List[Dict[str, Any]]]:
    try:
        value = json.loads(text)
        if isinstance(value, dict) and isinstance(value.get("papers"), list):
            return value["papers"]
        if isinstance(value, list):
            return value
    except json.JSONDecodeError:
        pass
    return None


def only_filter_and_report(papers: List[Dict[str, str]]) -> str:
    if not papers:
        return "今日无新论文。"
    selected: List[Dict[str, Any]] = []
    failed: List[int] = []
    for offset in range(0, len(papers), 40):
        chunk = papers[offset : offset + 40]
        prompt = f"""你是一个专注于大模型具身智能的研究员。请从以下论文中筛选相关论文并打分。
只输出 JSON 对象 {{"papers": [{{"id": "论文ID", "title_en": "原题", "title_zh": "中文标题", "score": 9}}]}}。
只保留 7 分及以上；id 必须逐字复制输入；不确定时不要猜测。
待处理数据：{json.dumps(chunk, ensure_ascii=False)}"""
        try:
            parsed = _extract_json_array(_llm(prompt, response_format={"type": "json_object"}))
            if parsed is None:
                raise ReportError("screening output is not a JSON array")
            selected.extend(parsed)
        except Exception as exc:
            failed.append(offset // 40 + 1)
            LOGGER.warning("screening batch %s failed: %s", offset // 40 + 1, exc)

    recommendations = [paper for paper in selected if paper.get("score", 0) >= 8]
    if not recommendations:
        return "今日无高分精选论文。" + (f"\n失败批次：{failed}" if failed else "")
    report = "📊 **今日具身智能论文初筛建议**\n\n"
    report += f"👉 [点击去手动触发解析](https://github.com/{REPO_OWNER}/{REPO_NAME}/actions)\n\n"
    for paper in recommendations:
        report += f"- `{paper.get('id', '')}` | 分数: {paper.get('score', '?')} | {paper.get('title_zh', '无标题')}\n"
    if failed:
        report += f"\n⚠️ 初筛失败批次：{failed}\n"
    return report


def generate_archive_and_index(date_info: Dict[str, str], content: str, *, output_dir: str = ".") -> None:
    archive_dir = os.path.join(output_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    display_title = f"{date_info['prefix']} (VLA: {content.count('###')} of {date_info['total']} entries)"
    safe_name = re.sub(r"[^\w\s-]", "", date_info["prefix"]).replace(" ", "_") or "report"
    path = os.path.join(archive_dir, f"{safe_name}.html")
    with open(path, "w", encoding="utf-8") as file:
        file.write(_render_page(f"具身大模型简报 - {display_title}", content, back_link=True))
    rebuild_index(output_dir)
    send_feishu_card(f"具身精选 | {display_title}", f"今日共包含 **{content.count('###')}** 篇筛选论文。")


def deep_dive_only(items: List[Dict[str, str]], *, output_dir: str = ".") -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    for index, item in enumerate(items, 1):
        try:
            entry = deep_dive_paper(item, index)
            if entry:
                generate_single_page(entry, output_dir)
                results.append(entry)
                paper_url = entry["source_url"]
                if not send_feishu_markdown_to_chat(entry["report"], title=entry["title"], paper_url=paper_url):
                    send_feishu_card(entry["title"], f"📎 [查看原文]({paper_url})\n\n{entry['report'][:18000]}")
        except Exception as exc:
            LOGGER.exception("deep dive failed for %s: %s", item, exc)
    rebuild_index(output_dir)
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    raw_papers = os.getenv("PAPERS", "") or os.getenv("TARGET_IDS", "")
    if raw_papers:
        items = parse_input_items(raw_papers)
        if not items:
            send_feishu_webhook("⚠️ 没有识别到有效的论文 ID 或 URL。")
            return
        results = deep_dive_only(items)
        LOGGER.info("done. generated %s paper pages", len(results))
        return

    all_papers: Dict[str, Dict[str, str]] = {}
    date_info: Optional[Dict[str, str]] = None
    for category in CATEGORIES:
        info, _, papers = scrape_arxiv(category)
        if info:
            date_info = info
        for paper in papers:
            all_papers[paper["id"]] = paper
    report = only_filter_and_report(list(all_papers.values()))
    if FILTER_WARNINGS:
        report += "\n\n" + "\n".join(FILTER_WARNINGS)
    send_feishu_webhook(report)
    if date_info and os.getenv("GENERATE_SCREENING_ARCHIVE", "0") == "1":
        generate_archive_and_index(date_info, report)


if __name__ == "__main__":
    main()

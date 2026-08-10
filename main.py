import requests
from bs4 import BeautifulSoup
import datetime
from openai import OpenAI
import os
import re
import json
import hashlib
import html

# --- 1. 核心配置 ---
client_llm = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
# 解析完成后，报告正文要推送到的飞书群 chat_id（不填则退回 webhook 卡片）
TARGET_CHAT_ID = os.getenv("CHAT_ID", "")

repo_full_name = os.getenv('GITHUB_REPOSITORY', 'owner/repo')
repo_owner = os.getenv('GITHUB_REPOSITORY_OWNER', 'owner')
repo_name = repo_full_name.split('/')[-1]
# 这里的 URL 会根据你的新仓库名自动变化
GITHUB_PAGES_URL = f"https://{repo_owner}.github.io/{repo_name}/"

CATEGORIES = ['cs.RO']

HEADERS = {'User-Agent': 'Mozilla/5.0'}

# 飞书 API
FEISHU_TENANT_TOKEN = {"token": None, "expire": 0}


def get_feishu_tenant_token():
    """获取飞书自建应用的 tenant_access_token（带缓存）"""
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
        return None
    if FEISHU_TENANT_TOKEN["token"] and datetime.datetime.now().timestamp() < FEISHU_TENANT_TOKEN["expire"] - 60:
        return FEISHU_TENANT_TOKEN["token"]
    try:
        res = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10)
        data = res.json()
        token = data.get("tenant_access_token")
        if token:
            FEISHU_TENANT_TOKEN["token"] = token
            FEISHU_TENANT_TOKEN["expire"] = datetime.datetime.now().timestamp() + int(data.get("expire", 7200))
        return token
    except Exception as e:
        print(f"获取飞书 token 失败: {e}")
        return None


def send_feishu_markdown_to_chat(content_md, title=None):
    """把 Markdown 报告作为消息发送到指定飞书群（走应用机器人 API）"""
    if not (TARGET_CHAT_ID and get_feishu_tenant_token()):
        return False
    card = {
        "header": {"title": {"tag": "plain_text", "content": title or "📄 论文深度解析"}, "template": "blue"},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content_md[:20000]}}]
    }
    try:
        res = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {get_feishu_tenant_token()}"},
            json={"receive_id": TARGET_CHAT_ID, "msg_type": "interactive", "content": json.dumps(card)},
            timeout=15)
        print(f"飞书应用消息发送结果: {res.status_code} {res.text[:200]}")
        return res.status_code == 200
    except Exception as e:
        print(f"飞书应用消息发送失败: {e}")
        return False


def send_feishu_webhook(text):
    """发送纯文本或简单 Markdown 到飞书（webhook 方式，兼容旧配置）"""
    if not FEISHU_WEBHOOK:
        return
    try:
        requests.post(FEISHU_WEBHOOK, json={"msg_type": "text", "content": {"text": text}}, timeout=10)
    except Exception as e:
        print(f"飞书 webhook 发送失败: {e}")


# --- 2. arXiv 抓取 ---
def scrape_arxiv(category):
    """抓取 Arxiv 数据，并提取日期前缀和总论文数（仅定时初筛模式用）"""
    url = f"https://arxiv.org/list/{category}/recent?show=500"
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(res.text, 'html.parser')
        dls = soup.find_all('dl', id='articles')
        if not dls: return None, 0, []

        raw_date_str = soup.find_all('h3')[0].text.strip()
        match = re.search(r'^(.*)\(showing \d+ of (\d+) entries', raw_date_str)
        if match:
            date_prefix = match.group(1).strip()
            total_entries = match.group(2)
        else:
            date_prefix = raw_date_str
            total_entries = "0"

        papers = []
        dt_tags = dls[0].find_all('dt')
        dd_tags = dls[0].find_all('dd')

        for dt, dd in zip(dt_tags, dd_tags):
            link_tag = dt.find('a', title='Abstract')
            if not link_tag: continue
            id_str = link_tag.text.replace('arXiv:', '').strip()
            title = dd.find('div', class_='list-title').text.replace('Title:', '').strip()
            abstract = dd.find('p', class_='mathjax').text.strip() if dd.find('p', class_='mathjax') else ""
            papers.append({"id": id_str, "title": title, "abstract": abstract[:1000]})

        return {"prefix": date_prefix, "total": total_entries}, len(papers), papers
    except Exception:
        return None, 0, []


ARXIV_ID_RE = re.compile(r'(?:arxiv\.org/(?:abs|pdf|html)/|arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)', re.I)


def extract_arxiv_id(text):
    """从一段文本里提取第一个 arxiv ID"""
    m = ARXIV_ID_RE.search(text)
    return m.group(1).split('v')[0] if m else None


def get_arxiv_full_text(paper_id):
    """抓取 Arxiv HTML 正文，剔除参考文献以节省 token"""
    url = f"https://arxiv.org/html/{paper_id}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
        if res.status_code != 200: return None
        soup = BeautifulSoup(res.text, 'html.parser')

        for script in soup(["script", "style"]):
            script.decompose()

        ref_tags = soup.find_all(['section', 'div'], class_=re.compile(r'bibliography|references', re.I))
        ref_tags += soup.find_all(['section', 'div'], id=re.compile(r'bib|references', re.I))
        for tag in ref_tags:
            tag.decompose()
            print(f"[{paper_id}] 已剔除参考文献部分")

        return soup.get_text()[:30000]
    except Exception as e:
        print(f"抓取全文出错 {paper_id}: {e}")
        return None


def get_arxiv_abstract(paper_id):
    """抓取 Arxiv 摘要页（HTML 全文不存在时兜底）"""
    try:
        res_abs = requests.get(f"https://arxiv.org/abs/{paper_id}", headers=HEADERS, timeout=15)
        if res_abs.status_code == 200:
            abs_soup = BeautifulSoup(res_abs.text, 'html.parser')
            abstract = abs_soup.find('blockquote', class_='abstract')
            if abstract:
                return abstract.text
        return None
    except Exception:
        return None


def get_webpage_text(url):
    """抓取任意网页正文（blog / technical report 等），返回 (标题, 正文)"""
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
        if res.status_code != 200:
            print(f"网页抓取失败 {url}: HTTP {res.status_code}")
            return None, None
        soup = BeautifulSoup(res.text, 'html.parser')
        title = soup.title.string.strip() if soup.title and soup.title.string else url

        # 优先取 <article> / <main>，否则取 body
        body = soup.find('article') or soup.find('main') or soup.body or soup
        for tag in body(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        text = re.sub(r'\n{3,}', '\n\n', body.get_text('\n'))
        text = re.sub(r'[ \t]{2,}', ' ', text)
        return title, text[:30000]
    except Exception as e:
        print(f"网页抓取失败 {url}: {e}")
        return None, None


# --- 3. 深度解析 ---
def build_expert_prompt(paper_id, full_text, source_label=""):
    """构造深度解析提示词"""
    source_hint = f"\n\n**解析对象**: {source_label}" if source_label else ""
    return f"""
    Role: 你是一位具身智能领域研究员。请用平实、地道的中文对论文进行高信息密度的总结。
    Task: 像在组会上给同事分享一样，直接讲清楚论文做了什么、改了哪里、效果如何。严禁过度修饰，严禁使用炫技式的词汇。
    {source_hint}

    请严格按以下结构输出（使用 Markdown）：

    **0. 论文标题**
    - **英文标题**: [在此填入论文原文标题]
    - **中文标题**: [在此填入精准的中文翻译]
    - **研究机构**: [在此填入作者所属的主要单位，如：DeepMind, Stanford University等]

    **1. 整体逻辑**
    - **研究任务**: [论文研究的任务是什么，如：根据文本生成图像]
    - **研究动机**: [例如发现了什么问题需要改进，比如VLA生成动作的速度太慢]
    - **本质改动**: [本质改动，如：用视频生成代替扩散策略做轨迹预测]
    - **技术溯源**: [基于 CLIP/OpenVLA/Llama3 等哪些开源基座？]

    **2. 技术拆解**
    - **重点改进**: [本质改动对应的模型或算法的改动]
    - **架构细节**: [输入输出、具体的模型结构、模型规模等]
    - **核心 Loss**: [主 Loss 构成，是否有辅助任务（如视频重建）？]

    **3. 实验结果**
    - **数据集和baseline**: [实验用的数据集和对比的方法]
    - **评价指标**: [实验用的评价指标，如何评价]
    - **实验结果**: [比baseline好多少]

    待处理全文内容：
    {full_text if full_text else "（全文抓取失败，请基于摘要分析核心逻辑）"}
    """


def deep_dive_paper(item, idx):
    """
    对单个条目做深度解析。
    item: {"id": arxiv_id 或 "url": 任意链接}
    返回 dict（失败返回 None）
    """
    if item.get("url"):
        # --- 任意 URL（blog / technical report）---
        url = item["url"]
        slug = item.get("slug", "")
        print(f"正在进行 URL 深度解析: {url}...")
        title, full_text = get_webpage_text(url)
        if not full_text:
            full_text = None
        report = None
        try:
            completion = client_llm.chat.completions.create(
                model="qwen3.7-max-2026-05-17",
                messages=[{"role": "user", "content": build_expert_prompt("", full_text, source_label=url)}]
            )
            report = completion.choices[0].message.content
        except Exception as e:
            print(f"深度解析出错 {url}: {e}")
            return None

        t_en = title or url
        t_zh = ""
        # 渲染 Markdown
        md = f"### {idx}. 🔥 [{t_en}]({url})\n"
        md += f"- **来源链接**: `{url}`\n\n"
        md += f"{report}\n\n"
        md += "---\n"
        return {
            "slug": slug, "md": md, "title": title or url,
            "report": report, "source_url": url, "kind": "url",
        }

    # --- arXiv 论文 ---
    paper_id = item["id"]
    print(f"正在进行全文深度解析: {paper_id}...")
    full_text = get_arxiv_full_text(paper_id)
    if not full_text:
        print(f"HTML 全文暂未生成，尝试抓取摘要页...")
        full_text = get_arxiv_abstract(paper_id)

    try:
        completion = client_llm.chat.completions.create(
            model="qwen3.7-max-2026-05-17",
            messages=[{"role": "user", "content": build_expert_prompt(paper_id, full_text)}]
        )
        report = completion.choices[0].message.content
    except Exception as e:
        print(f"深度解析出错 {paper_id}: {e}")
        return None

    # --- 提取逻辑 ---
    title_en = re.search(r"英文标题\*\*: (.*)", report)
    title_zh = re.search(r"中文标题\*\*: (.*)", report)
    affiliation = re.search(r"研究机构\*\*: (.*)", report)
    t_en = title_en.group(1).strip() if title_en else f"Arxiv: {paper_id}"
    t_zh = title_zh.group(1).strip() if title_zh else ""
    aff = affiliation.group(1).strip() if affiliation else "未知机构"

    # --- 渲染 Markdown ---
    md = f"### {idx}. 🔥 [{t_en}](https://arxiv.org/abs/{paper_id}) ({t_zh})\n"
    md += f"- **研究机构**: `{aff}`\n"
    md += f"- **Arxiv ID**: `{paper_id}` | [点击跳转](https://arxiv.org/abs/{paper_id})\n\n"
    md += f"{report}\n\n"
    md += "---\n"

    return {
        "slug": paper_id, "md": md, "title": t_en, "title_zh": t_zh,
        "affiliation": aff, "report": report, "paper_id": paper_id, "kind": "arxiv",
    }


def generate_single_page(entry):
    """为单个解析结果生成独立归档页 archive/{slug}.html"""
    os.makedirs('archive', exist_ok=True)
    slug = entry["slug"]
    safe_slug = re.sub(r'[^\w.-]', '_', slug)
    file_path = f"archive/{safe_slug}.html"
    title = entry["title"]
    if entry.get("title_zh"):
        title = f"{title} ({entry['title_zh']})"
    if entry.get("paper_id"):
        title = f"🤖 具身大模型简报 - {entry['paper_id']} - {title}"

    # 全文 markdown（页面标题 = 解析标题；md 内容 = 报告正文）
    body_content = entry["md"]
    back_link = "<a href='../index.html' style='margin-bottom:20px; display:block;'>← 返回主索引</a>"
    safe_body = body_content.replace('</script>', '<\\/script>')
    safe_title = html.escape(title)

    page_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{safe_title}</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.2.0/github-markdown.min.css">
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script>
        window.MathJax = {{
            tex: {{ inlineMath: [['$', '$'], ['\\(', '\\)']], displayMath: [['$$', '$$'], ['\\[', '\\]']] }},
            options: {{ skipHtmlTags: ['script', 'style', 'textarea'] }}
        }};
    </script>
    <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
    <style>
        .markdown-body {{ box-sizing: border-box; min-width: 200px; max-width: 980px; margin: 0 auto; padding: 45px; }}
        @media (max-width: 767px) {{ .markdown-body {{ padding: 15px; }} }}
    </style>
</head>
<body class="markdown-body">
    {back_link}
    <h1>{safe_title}</h1>
    <div id="content"></div>
    <script type="text/markdown" id="raw-markdown">{safe_body}</script>
    <script>
        const rawMdElement = document.getElementById('raw-markdown');
        if (rawMdElement) {{
            document.getElementById('content').innerHTML = marked.parse(rawMdElement.textContent);
            if (window.MathJax && window.MathJax.typesetPromise) {{
                window.MathJax.typesetPromise();
            }}
        }}
    </script>
</body>
</html>
"""
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(page_html)

    # 附加元数据（用于索引排序：统一按解析时间排序，同一次解析的 arxiv 按 ID 大者优先）
    meta = {
        "slug": safe_slug, "title": title, "kind": entry.get("kind", "arxiv"),
        "ts": datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
    }
    if entry.get("paper_id"):
        meta["paper_id"] = entry["paper_id"]
    with open(f"archive/.meta.{safe_slug}.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    return file_path


def rebuild_index():
    """扫描 archive/ 下的页面和元数据，重建 index.html"""
    if not os.path.isdir('archive'):
        os.makedirs('archive', exist_ok=True)
        return
    files = [f for f in os.listdir('archive') if f.endswith('.html') and not f.startswith('.')]
    entries = []
    for f_name in files:
        try:
            meta = None
            meta_name = f"archive/.meta.{f_name[:-5]}.json"
            if os.path.exists(meta_name):
                with open(meta_name, "r", encoding="utf-8") as mf:
                    meta = json.load(mf)
            with open(f"archive/{f_name}", "r", encoding="utf-8") as hf:
                page_soup = BeautifulSoup(hf.read(), 'html.parser')
                page_title = page_soup.title.string if page_soup.title else f_name

            if meta and meta.get("paper_id"):
                # arxiv 论文：按解析批次时间 + ID 数字排序（ID 大的排前）
                m = re.search(r'(\d{4})\.(\d{4,5})', meta.get("paper_id", ""))
                num = (int(m.group(1)) * 100000 + int(m.group(2))) if m else 0
                key = (0, int(meta.get("ts", "0")), num)
            elif meta and meta.get("ts"):
                key = (0, int(meta["ts"]), 0)
            else:
                # 兼容旧文件：按修改时间
                key = (2, int(os.path.getmtime(f"archive/{f_name}")), 0)
            entries.append((key, page_title, f_name))
        except Exception as e:
            print(f"解析历史文件 {f_name} 出错: {e}")
            continue

    entries.sort(key=lambda x: x[0], reverse=True)
    index_md = "### 📅 解析存档列表\n\n"
    for _, page_title, f_name in entries:
        index_md += f"- [{page_title}](archive/{f_name})\n"

    index_html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📚 具身大模型科研日报 - 解析存档</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.2.0/github-markdown.min.css">
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        .markdown-body { box-sizing: border-box; min-width: 200px; max-width: 980px; margin: 0 auto; padding: 45px; }
        @media (max-width: 767px) { .markdown-body { padding: 15px; } }
    </style>
</head>
<body class="markdown-body">
    <h1>📚 具身大模型科研日报 - 解析存档</h1>
    <div id="content"></div>
    <script type="text/markdown" id="raw-markdown">%s</script>
    <script>
        const rawMdElement = document.getElementById('raw-markdown');
        if (rawMdElement) { document.getElementById('content').innerHTML = marked.parse(rawMdElement.textContent); }
    </script>
</body>
</html>
""" % index_md.replace('</script>', '<\\/script>')

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(index_html)


def generate_archive_and_index(date_info, arxiv_content):
    """（保留旧签名，兼容定时模式调用）生成每日聚合页并推送卡片"""
    vla_count = (arxiv_content or "").count("###")
    display_title = f"{date_info['prefix']} (VLA: {vla_count} of {date_info['total']} entries)"
    safe_date_filename = re.sub(r'[^\w\s-]', '', date_info['prefix']).replace(' ', '_')
    os.makedirs('archive', exist_ok=True)
    daily_file_path = f"archive/{safe_date_filename}.html"

    paper_ids = re.findall(r'abs/(\d+\.\d+)', arxiv_content)
    sources_text = "\n".join([f"https://arxiv.org/html/{pid}" for pid in paper_ids])

    def get_html_template(title, body_content, is_index_page=False, sources_block=""):
        back_link = "<a href='../index.html' style='margin-bottom:20px; display:block;'>← 返回主索引</a>" if not is_index_page else ""
        safe_body = body_content.replace('</script>', '<\\/script>')
        return f"""
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{title}</title>
            <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.2.0/github-markdown.min.css">
            <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
            <script>
                window.MathJax = {{
                    tex: {{ inlineMath: [['$', '$'], ['\\(', '\\)']], displayMath: [['$$', '$$'], ['\\[', '\\]']] }},
                    options: {{ skipHtmlTags: ['script', 'style', 'textarea'] }}
                }};
            </script>
            <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
            <style>
                .markdown-body {{ box-sizing: border-box; min-width: 200px; max-width: 980px; margin: 0 auto; padding: 45px; }}
                @media (max-width: 767px) {{ .markdown-body {{ padding: 15px; }} }}
            </style>
        </head>
        <body class="markdown-body">
            {back_link}
            <h1>{title}</h1>
            <div id="content"></div>
            {sources_block}
            <script type="text/markdown" id="raw-markdown">{safe_body}</script>
            <script>
                const rawMdElement = document.getElementById('raw-markdown');
                if (rawMdElement) {{
                    document.getElementById('content').innerHTML = marked.parse(rawMdElement.textContent);
                    if (window.MathJax && window.MathJax.typesetPromise) {{
                        window.MathJax.typesetPromise();
                    }}
                }}
                function copySources() {{ }}
            </script>
        </body>
        </html>
        """

    with open(daily_file_path, "w", encoding="utf-8") as f:
        f.write(get_html_template(f"🤖 具身大模型简报 - {display_title}", arxiv_content or "", False, ""))

    rebuild_index()

    # 飞书推送卡片
    if FEISHU_WEBHOOK:
        requests.post(FEISHU_WEBHOOK, json={
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"🌟 具身精选 | {display_title}"}, "template": "blue"},
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"今日共包含 **{vla_count}** 篇 VLA 筛选论文。"}},
                    {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "🌐 查看网页"}, "type": "primary", "url": GITHUB_PAGES_URL}]}
                ]
            }
        })


def send_feishu_notification(text):
    """发送纯文本或简单 Markdown 到飞书"""
    send_feishu_webhook(text)


def parse_input_items(raw):
    """把用户输入的字符串（逗号/换行分隔的 ID 或 URL）解析成条目列表"""
    items = []
    for token in re.split(r'[\n,，;；\s]+', raw.strip()):
        token = token.strip()
        if not token:
            continue
        if token.startswith(('http://', 'https://')):
            if 'arxiv.org' in token:
                pid = extract_arxiv_id(token)
                if pid:
                    items.append({"id": pid})
                    continue
            # 非 arxiv 链接：生成稳定 slug
            slug = hashlib.sha256(token.encode('utf-8')).hexdigest()[:10]
            items.append({"url": token, "slug": slug})
        else:
            pid = extract_arxiv_id(token)
            if pid:
                items.append({"id": pid})
    return items


def main():
    target_str = os.getenv("PAPERS", "") or os.getenv("TARGET_IDS", "")
    print(f"收到输入: {target_str!r}")

    if target_str:
        # --- 模式 A：手动深度解析 ---
        items = parse_input_items(target_str)
        if not items:
            send_feishu_webhook("⚠️ 没有识别到有效的论文 ID 或 URL，请检查输入格式。")
            print("没有识别到有效输入")
            return

        entries = []
        for idx, item in enumerate(items, 1):
            entry = deep_dive_paper(item, idx)
            if entry:
                file_path = generate_single_page(entry)
                print(f"已生成页面: {file_path}")

                # 把报告正文推送到飞书群（应用机器人）
                if TARGET_CHAT_ID and get_feishu_tenant_token():
                    send_feishu_markdown_to_chat(entry["report"], title=entry["title"])
                elif FEISHU_WEBHOOK:
                    # 退回 webhook：文本消息
                    send_feishu_webhook(f"📄 {entry['title']}\n{entry['report']}")

        rebuild_index()
        print("done. 共生成", len(entries), "篇解析")
    else:
        # --- 模式 B：定时任务执行初筛汇报 ---
        all_p = {}
        date_info = None
        for cat in CATEGORIES:
            info, _, ps = scrape_arxiv(cat)
            if info: date_info = info
            for p in ps: all_p[p['id']] = p

        report_list = only_filter_and_report(list(all_p.values()))
        send_feishu_notification(report_list)


if __name__ == "__main__":
    main()

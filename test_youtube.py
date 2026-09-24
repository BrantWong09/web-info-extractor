import re
import json
import asyncio
import sqlite3
import shutil
import os
import tempfile
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from html import unescape
from urllib.parse import urlparse, unquote
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from playwright.async_api import async_playwright


def load_config():
    """读取脚本同目录下的 config.yaml（本地隐私配置，不入库），模板见 config.example.yaml"""
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


import yaml

CONFIG = load_config()
proxies = {"http": CONFIG["proxy_url"], "https": CONFIG["proxy_url"]}
DATE_PATTERN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "zh-CN,zh;q=0.9"}
COOKIES = {"CONSENT": "YES+cb.20220419-16-p0.en+FX+{}", "SOCS": "CAI"}
TIMEOUT = 30
RETRY_TIMES = 3


def http_get(url, **kwargs):
    """带重试的 GET。

    走本地代理抓 YouTube 经常传输中途断连（ChunkedEncodingError /
    ConnectionResetError），单次失败就让整轮退出太脆，这里退避重试。"""
    last_error = None
    for attempt in range(RETRY_TIMES):
        try:
            resp = requests.get(url, headers=HEADERS, cookies=COOKIES,
                                proxies=proxies, timeout=TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_error = e
            if attempt < RETRY_TIMES - 1:
                wait = 2 * (attempt + 1)
                print(f"  请求失败({type(e).__name__}: {e})，{wait}s 后重试: {url}")
                time.sleep(wait)
    raise last_error


def extract_yt_data(html):
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        if script.string and "ytInitialData" in script.string:
            m = re.search(r"var ytInitialData\s*=\s*(\{.+?\});\s*</script>", script.string, re.DOTALL)
            if not m:
                m = re.search(r"var ytInitialData\s*=\s*(\{.+?});", script.string, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    m2 = re.search(r"var ytInitialData\s*=\s*(\{.+);\s*</script>", script.string, re.DOTALL)
                    if m2:
                        return json.loads(m2.group(1))
    return None


def get_channel_urls():
    """读取要抓取的频道列表。优先用 channel_urls（列表），兼容旧的单值 channel_url。"""
    value = CONFIG.get("channel_urls") or CONFIG.get("channel_url")
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    urls = []
    for u in value:
        u = (u or "").strip().rstrip("/")
        if u and u not in urls:
            urls.append(u)
    return urls


def get_video_list(channel_url):
    """抓频道视频列表，返回 [{video_id, title}]。频道页解析不了时退回 RSS。"""
    url = channel_url + "/videos"
    html = ""
    try:
        resp = http_get(url)
        html = resp.text
        data = extract_yt_data(html)
        if not data:
            # YouTube 偶尔会返回同意页 / 机器人校验页（几十 KB，没有 ytInitialData）
            raise ValueError(f"频道页没有 ytInitialData（{len(html)} 字节，可能被返回了同意页或校验页）")
        videos = parse_channel_videos(data)
        if not videos:
            raise ValueError("频道页里没有解析到视频")
        return videos
    except Exception as e:
        videos = parse_rss_videos(channel_url, html)
        if videos:
            print(f"  频道页解析失败（{e}），改用 RSS 取最近 {len(videos)} 个视频")
            return videos
        raise


def parse_channel_videos(data):
    """从频道页 ytInitialData 里取视频列表（按展示顺序）。

    不写死 contents→twoColumnBrowseResultsRenderer→tabs→richGridRenderer 这条路径，
    直接递归找 lockupViewModel（新版）或 gridVideoRenderer（老版），
    这样 YouTube 调整层级时不会整段失效。"""
    for key in ("lockupViewModel", "gridVideoRenderer"):
        videos = []
        seen = set()
        for item in _find_all(data, key):
            if key == "lockupViewModel":
                vid = item.get("contentId", "")
                title_obj = item.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title", {})
                title = title_obj.get("content", "") if isinstance(title_obj, dict) else str(title_obj)
            else:
                vid = item.get("videoId", "")
                title_obj = item.get("title", {})
                if isinstance(title_obj, dict):
                    title = title_obj.get("simpleText") or "".join(
                        r.get("text", "") for r in title_obj.get("runs", []) or [])
                else:
                    title = str(title_obj)
            if vid and title and vid not in seen:
                seen.add(vid)
                videos.append({"video_id": vid, "title": title})
        if videos:
            return videos
    return []


def parse_rss_videos(channel_url, html=""):
    """RSS 兜底：频道页抓不到时用上传视频 feed（只有最近 15 条）。

    标题里的日期格式和网页一致，所以不影响 find_latest_stable_node。"""
    channel_id = re.search(r'"externalId":"(UC[\w-]{22})"', html or "")
    if not channel_id:
        try:
            page = http_get(channel_url)
            channel_id = re.search(r'"externalId":"(UC[\w-]{22})"', page.text)
        except Exception:
            channel_id = None
    if not channel_id:
        return []
    try:
        resp = http_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id.group(1)}")
    except Exception as e:
        print(f"  RSS 兜底也失败: {e}")
        return []
    videos = []
    for entry in re.findall(r"<entry>(.*?)</entry>", resp.text, re.S):
        vid = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", entry)
        title = re.search(r"<title>(.*?)</title>", entry, re.S)
        if vid and title:
            videos.append({"video_id": vid.group(1).strip(), "title": unescape(title.group(1)).strip()})
    return videos


def find_latest_stable_node(videos):
    candidates = []
    for v in videos:
        dm = DATE_PATTERN.search(v["title"])
        if dm and "稳定节点" in v["title"]:
            d = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            candidates.append({**v, "date": d})
    return max(candidates, key=lambda x: x["date"]) if candidates else None


def get_video_page(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    return http_get(url).text


def get_comments_token(data):
    """取 ytInitialData 里评论区首页的 continuation token"""
    for panel in data.get("engagementPanels", []):
        renderer = panel.get("engagementPanelSectionListRenderer", {})
        if renderer.get("panelIdentifier") != "engagement-panel-comments-section":
            continue
        try:
            items = renderer["content"]["sectionListRenderer"]["contents"][0]["itemSectionRenderer"]["contents"]
            return items[0]["continuationItemRenderer"]["continuationEndpoint"]["continuationCommand"]["token"]
        except (KeyError, IndexError, TypeError):
            return None
    return None


def unwrap_redirect_url(url):
    """YouTube 的跳转链接（.../redirect?...&q=<真实地址>）取回真实地址。

    用正则而不是 parse_qs：q= 里的地址可能带 #（如 paste.to 的密钥），
    整体是 %23 编码的，按字符串截取再 unquote 更稳。"""
    if "youtube.com/redirect" in url:
        m = re.search(r"[?&]q=([^&]+)", url)
        if m:
            return unquote(m.group(1))
    return url


def _find_all(node, key):
    """递归收集 JSON 里所有名为 key 的节点（按文档顺序）"""
    found = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur and isinstance(cur[key], dict):
                found.append(cur[key])
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))
    return found


def parse_comments(payload):
    """把 youtubei/v1/next 的评论响应解析成评论列表，每项：
    {author, is_owner, text, urls, runs}，runs 是 {文本起始位置: 完整地址}。
    is_owner 即频道拥有者（作者本人）的评论。"""
    entities = {p.get("key"): p for p in _find_all(payload, "commentEntityPayload")}

    comments = []
    # 按评论区的展示顺序取，作者置顶的评论通常在最前面
    for thread in _find_all(payload, "commentThreadRenderer"):
        key = thread.get("commentViewModel", {}).get("commentViewModel", {}).get("commentKey")
        entity = entities.pop(key, None)
        if entity:
            comments.append(_comment_from_entity(entity))
    comments.extend(_comment_from_entity(p) for p in entities.values())

    if not comments:
        # 老版结构兜底
        comments = [_comment_from_legacy_renderer(r) for r in _find_all(payload, "commentRenderer")]
    return [c for c in comments if c["text"] or c["urls"]]


def _comment_from_entity(payload):
    author = payload.get("author", {}) or {}
    content = (payload.get("properties", {}) or {}).get("content", {}) or {}
    return _build_comment(author.get("displayName", ""), bool(author.get("isCreator")), content)


def _comment_from_legacy_renderer(renderer):
    author = (renderer.get("authorText", {}) or {}).get("simpleText", "") or ""
    return _build_comment(author, bool(renderer.get("authorIsChannelOwner")), renderer.get("contentText", {}) or {})


def _build_comment(author, is_owner, content):
    text = content.get("content") or content.get("simpleText") or \
        "".join(r.get("text", "") for r in content.get("runs", []) or [])
    runs = {}
    for cmd in content.get("commandRuns", []) or []:
        nav = cmd.get("onTap", {}).get("innertubeCommand", {}) or {}
        raw = (nav.get("urlEndpoint", {}) or {}).get("url") or \
            (nav.get("commandMetadata", {}) or {}).get("webCommandMetadata", {}).get("url", "")
        if raw:
            runs[cmd.get("startIndex", 0)] = unwrap_redirect_url(raw.replace("\\u0026", "&"))
    urls = list(runs.values())
    for u in re.findall(r"https?://[^\s\u3000\"'<>]+", text):
        u = u.rstrip("，。、）)】]")
        if u not in urls:
            urls.append(u)
    return {"author": author, "is_owner": is_owner, "text": text, "urls": urls, "runs": runs}


def get_comments(html, data):
    """抓取视频评论首页，返回评论列表。

    作者会把节点下载地址放在自己的评论区留言里（简介只写“节点在评论区”），
    所以评论是除简介外的第二个地址来源。"""
    token = get_comments_token(data)
    api_key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    if not (token and api_key):
        return []

    client_version = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    last_error = None
    for attempt in range(RETRY_TIMES):
        try:
            resp = requests.post(
                f"https://www.youtube.com/youtubei/v1/next?key={api_key.group(1)}",
                json={
                    "context": {"client": {
                        "clientName": "WEB",
                        "clientVersion": client_version.group(1) if client_version else "2.20250101.00.00",
                        "hl": "zh-CN", "gl": "US",
                    }},
                    "continuation": token,
                },
                headers={"Content-Type": "application/json", **HEADERS},
                cookies=COOKIES, proxies=proxies, timeout=TIMEOUT,
            )
            resp.raise_for_status()
            return parse_comments(resp.json())
        except Exception as e:
            last_error = e
            if attempt < RETRY_TIMES - 1:
                time.sleep(2 * (attempt + 1))
    print(f"  评论区抓取失败: {last_error}")
    return []


def extract_labeled_url(text, url_map):
    """按“下载地址：”标签取后面的链接，优先用 url_map 里该位置的完整地址"""
    m = re.search(r"下载地址[：:]\s*(\S+)", text or "")
    if not m:
        return None
    pos = m.start(1)
    return url_map.get(pos) or m.group(1).rstrip("，。、）)】]")


def find_download_url_in_owner_comments(comments, desc_url=None):
    """从频道拥有者的评论里取下载地址，返回 (地址, 该评论原文)。

    作者评论里还挂着机场广告、Telegram 群等一堆链接，所以取法必须保守：
    先认“下载地址：”标签；没标签时退回和简介同域名的链接；再退回带 paste id
    （16 位十六进制，如 paste.to）的链接。"""
    owners = [c for c in comments if c["is_owner"]]
    for c in owners:
        url = extract_labeled_url(c["text"], c["runs"])
        if url:
            return url, c["text"]
    if desc_url:
        host = urlparse(desc_url).netloc
        for c in owners:
            for u in c["urls"]:
                if host and urlparse(u).netloc == host:
                    return u, c["text"]
    for c in owners:
        for u in c["urls"]:
            if re.search(r"[0-9a-f]{16}", u):
                return u, c["text"]
    return None, None


def resolve_truncated_url(html, data, truncated_url, comments=None):
    """补全被 YouTube 截断的简介链接。

    简介里的长链接只显示前一段加省略号（如 “…/?<paste id>#<密钥前 7 位>...”），
    paste.to 拿到不完整的密钥会直接报 “mangled URL” 退出，连密码框都不弹。
    作者会在评论区另贴一份完整地址，这里按 paste id 从评论区取回完整 URL。"""
    id_match = re.search(r"[0-9a-f]{16}", truncated_url)
    if not id_match:
        return None
    paste_id = id_match.group(0)
    host = urlparse(truncated_url).netloc
    if comments is None:
        comments = get_comments(html, data)
    candidates = []
    for c in comments:
        for u in c["urls"]:
            if paste_id in u and "..." not in u:
                candidates.append(u)
    # 优先取与简介地址同域名的，避免命中评论里的广告链接
    for url in candidates:
        if urlparse(url).netloc == host:
            return url
    return candidates[0] if candidates else None


def get_description_and_download_url(video_id):
    """返回 (简介原文, 下载地址, 作者评论原文)。

    地址优先取简介里的“下载地址：”标签；简介里没有（现在作者常常只写
    “节点在我的评论区”）时，从频道拥有者的评论里取。"""
    html = get_video_page(video_id)
    data = extract_yt_data(html)
    if not data:
        return "", None, ""
    description = ""
    url_map = {}
    for panel in data.get("engagementPanels", []):
        items = panel.get("engagementPanelSectionListRenderer", {}).get("content", {}).get("structuredDescriptionContentRenderer", {}).get("items", [])
        for item in items:
            renderer = item.get("expandableVideoDescriptionBodyRenderer", {})
            if renderer:
                attr = renderer.get("attributedDescriptionBodyText", {})
                description = attr.get("content", "")
                for cmd in attr.get("commandRuns", []):
                    nav = cmd.get("onTap", {}).get("innertubeCommand", {})
                    url_ep = nav.get("commandMetadata", {}).get("webCommandMetadata", {}).get("url", "")
                    if url_ep and url_ep.startswith("http"):
                        url_map[cmd.get("startIndex", 0)] = unwrap_redirect_url(url_ep.replace("\\u0026", "&"))

    download_url = extract_labeled_url(description, url_map)
    owner_text = ""
    # 简介里没写地址（或地址被截断补不全）时，看作者评论
    if not download_url or download_url.endswith("..."):
        comments = get_comments(html, data)
        owner_text = "\n".join(c["text"] for c in comments if c["is_owner"])
        if download_url and download_url.endswith("..."):
            full_url = resolve_truncated_url(html, data, download_url, comments)
            if full_url:
                print(f"  简介地址被截断，已从作者评论补全")
                download_url = full_url
        if not download_url or download_url.endswith("..."):
            comment_url, comment_text = find_download_url_in_owner_comments(comments, download_url)
            if comment_url:
                print(f"  地址取自作者评论")
                download_url = comment_url
                owner_text = comment_text
    return description, download_url, owner_text


async def get_transcript_playwright(video_id):
    """Get transcript via Playwright by opening the video page and clicking the transcript button."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="msedge",
            headless=True,
            proxy={"server": CONFIG["proxy_url"]},
        )
        page = await browser.new_page(ignore_https_errors=True)
        try:
            await page.goto(
                f"https://www.youtube.com/watch?v={video_id}",
                timeout=60000, wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(5000)

            # Click the transcript section button (real click to trigger Polymer handler)
            clicked = False
            for sel in [
                "ytd-video-description-transcript-section-renderer button",
                "ytd-text-inline-expander yt-button-shape",
                '#description-inline-expander yt-button-shape',
                'button[aria-label="转写文稿"]',
            ]:
                try:
                    btn = page.locator(sel).first
                    await btn.scroll_into_view_if_needed(timeout=5000)
                    await btn.click(timeout=5000, force=True)
                    clicked = True
                    break
                except:
                    pass

            if not clicked:
                # Fallback: JS click on any transcript-related tab
                await page.evaluate("""
                    () => {
                        const els = document.querySelectorAll('[role="tab"], button');
                        for (let el of els) {
                            const t = (el.getAttribute('aria-label')||'') + ' ' + (el.textContent||'');
                            if (t.includes('转写文稿') || t.includes('内容转文字') || t.includes('Transcript')) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)

            await page.wait_for_timeout(5000)

            # Read text from the engagement panel
            text = await page.evaluate("""
                () => {
                    const panel = document.querySelector('ytd-engagement-panel-section-list-renderer');
                    if (!panel) return '';
                    return panel.textContent || '';
                }
            """)
            if text and len(text) > 100:
                # Remove timestamps and UI noise, keep meaningful text
                clean = " ".join(text.split())
                return clean
        except Exception:
            pass
        finally:
            await browser.close()
        return ""


def get_transcript(video_id):
    """Get transcript using player API, then yt-dlp, then Playwright."""
    # Method 1: Player API (ANDROID context)
    try:
        html = get_video_page(video_id)
        m = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        if m:
            api_key = m.group(1)
            player_resp = requests.post(
                f"https://www.youtube.com/youtubei/v1/player?key={api_key}",
                json={"context": {"client": {"clientName": "ANDROID", "clientVersion": "20.10.38"}}, "videoId": video_id},
                headers={"Content-Type": "application/json", **HEADERS},
                cookies=COOKIES,
                proxies=proxies, timeout=TIMEOUT,
            )
            if player_resp.status_code == 200:
                tracks = player_resp.json().get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
                if tracks:
                    tr_resp = http_get(tracks[0].get("baseUrl", ""))
                    texts = re.findall(r"<p[^>]*>(.*?)</p>", tr_resp.text)
                    result = " ".join(t.strip() for t in texts if t.strip())
                    if result:
                        return result
    except Exception:
        pass

    # Method 2: Playwright browser (run in separate thread to avoid loop conflict)
    import threading
    result_holder = {}

    def _run_playwright():
        result_holder["result"] = asyncio.run(get_transcript_playwright(video_id))

    t = threading.Thread(target=_run_playwright, daemon=True)
    t.start()
    t.join(timeout=90)
    result = result_holder.get("result", "")
    if result:
        print("  [字幕] 使用 Playwright 获取")
        return result

    return ""


def extract_sub_urls_by_position(paste_content):
    """按位置提取订阅链接：定位含“订阅链接”的行，取其后面最近的一个 URL，
    再根据该行及前两行上下文里出现的客户端名称判断归属（v2ray 还是 Clash）。
    地址本身的文件名经常变（v-/c- 前缀、---16v 后缀、%40 编码都出现过），
    不能按 URL 特征匹配，只认位置。"""
    lines = paste_content.splitlines()
    v2ray_url = None
    clash_url = None
    for i, line in enumerate(lines):
        if "订阅链接" not in line:
            continue
        # 标签可能和客户端列表同行，也可能换行分开，把前两行一并算作上下文
        context = "\n".join(lines[max(0, i - 2): i + 1]).lower()
        url = None
        # URL 可能和标签同行（“v2ray订阅链接： https://...”），先看“订阅链接”之后的部分
        m = re.search(r"https?://\S+", line[line.rindex("订阅链接") + len("订阅链接"):])
        if m:
            url = m.group(0)
        else:
            for j in range(i + 1, min(i + 4, len(lines))):
                m = re.search(r"https?://\S+", lines[j])
                if m:
                    url = m.group(0)
                    break
        if not url:
            continue
        if clash_url is None and "clash" in context:
            clash_url = url
        elif v2ray_url is None and re.search(
            r"v2ray|小火箭|shadowrocket|quantumult|surge|hiddify|nekobox|karing|surfboard|stash",
            context,
        ):
            v2ray_url = url
    return v2ray_url, clash_url


def extract_password(transcript):
    # strip timestamps that get concatenated with subtitle text
    clean = re.sub(r"\d{1,3}:\d{2}", "", transcript)
    clean = re.sub(r"\d+分钟\d+秒钟", "", clean)
    m = re.search(r"密码[是为:：]?\s*(\d{4,6})", clean)
    return m.group(1) if m else None


async def decrypt_paste(download_url, password):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="msedge",
            headless=True,
            proxy={"server": CONFIG["proxy_url"]},
        )
        page = await browser.new_page(ignore_https_errors=True)
        try:
            await page.goto(download_url, timeout=120000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            pw_input = page.locator('input[type="password"]').first
            await pw_input.wait_for(state="attached", timeout=15000)

            await page.evaluate("""
                () => {
                    var el = document.querySelector('input[type="password"]');
                    if(el) { el.style.display='block'; el.style.visibility='visible'; el.focus(); }
                }
            """)
            await page.wait_for_timeout(500)
            await page.fill('input[type="password"]', password)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3000)

            content = await page.text_content("#prettyprint")
            if content:
                await browser.close()
                return content.strip()
            await browser.close()
            return None
        except Exception as e:
            await browser.close()
            print(f"Playwright error: {e}")
            return None


V2RAYN_DB = CONFIG["v2rayn_db"]
MIHOMO_PARTY_DIR = CONFIG["mihomo_party_dir"]
CLASH_PARTY_PROFILE = os.path.join(MIHOMO_PARTY_DIR, "profile.yaml")
CLASH_PARTY_PROFILES_DIR = os.path.join(MIHOMO_PARTY_DIR, "profiles")
PROFILE_ID = "clash_config"

UNSUPPORTED_ENCRYPTIONS = ["mlkem768", "kyber"]


async def update_clash_party_sub(new_url):
    print(f"\n正在更新 Clash Party 订阅...")
    import yaml

    filtered_config = download_and_filter(new_url)

    profile_content_path = os.path.join(CLASH_PARTY_PROFILES_DIR, f"{PROFILE_ID}.yaml")
    with open(profile_content_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(filtered_config, f, allow_unicode=True, sort_keys=False)
    print(f"  已写入 {profile_content_path}，共 {len(filtered_config.get('proxies', []))} 个代理节点")

    import time
    now_ms = int(time.time() * 1000)
    profile_config = {
        "current": PROFILE_ID,
        "items": [
            {
                "id": PROFILE_ID,
                "type": "local",
                "name": "clash_config",
                "interval": 86400,
                "updated": now_ms,
                "override": [],
                "useProxy": False,
                "allowFixedInterval": False,
            }
        ],
    }
    with open(CLASH_PARTY_PROFILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(profile_config, f, allow_unicode=True, sort_keys=False)
    print(f"  已更新 profile.yaml (IProfileConfig)")
    print(f"  完成（配置在下一次启动/手动切换配置时生效）")


def download_and_filter(url):
    import yaml
    # raw.githubusercontent.com 必须走本地代理才能访问
    resp = http_get(url, timeout=60, verify=False)
    cfg = yaml.safe_load(resp.content.decode("utf-8"))

    before = len(cfg.get("proxies", []))
    removed_names = set()
    filtered = []
    for p in cfg.get("proxies", []):
        if any(enc in p.get("encryption", "") for enc in UNSUPPORTED_ENCRYPTIONS):
            removed_names.add(p["name"])
        else:
            filtered.append(p)
    cfg["proxies"] = filtered
    after = len(cfg["proxies"])
    removed_count = before - after
    if removed_count:
        print(f"  过滤 {removed_count} 个不支持节点 ({before} -> {after})")
        for group in cfg.get("proxy-groups", []):
            group["proxies"] = [p for p in group.get("proxies", []) if p not in removed_names]
    else:
        print(f"  无需过滤，共 {after} 个节点")
    return cfg


async def update_v2rayn_sub(new_url):
    print(f"\n正在更新 v2rayN youtube 订阅...")
    tmp = os.path.join(tempfile.gettempdir(), "guiNDB_update.db")
    try:
        shutil.copy2(V2RAYN_DB, tmp)
    except PermissionError:
        print("  v2rayN 正在运行，请先关闭 v2rayN 后重试")
        return
    conn = sqlite3.connect(tmp)
    rows = conn.execute("SELECT id, url FROM SubItem WHERE remarks='youtube'").fetchall()
    if rows:
        old_id, old_url = rows[0]
        if old_url == new_url:
            print(f"  订阅地址已是最新，无需更新")
        else:
            conn.execute("UPDATE SubItem SET url=? WHERE id=?", (new_url, old_id))
            conn.commit()
            print(f"  已更新 youtube 订阅地址")
    else:
        print("  未找到 youtube 订阅项")
    conn.close()
    shutil.copy2(tmp, V2RAYN_DB)
    os.unlink(tmp)
    print(f"  完成")


async def main_async():
    channels = get_channel_urls()
    if not channels:
        print("config.yaml 里没有配置频道（channel_urls / channel_url）")
        return

    # 多个频道都可能是最新来源（旧频道偶尔仍在更新），逐个抓取后按标题里的日期取最大值
    candidates = []
    for channel in channels:
        try:
            videos = get_video_list(channel)
        except Exception as e:
            print(f"[{channel}] 获取视频列表失败: {e}")
            continue
        print(f"[{channel}] 共获取 {len(videos)} 个视频")
        latest = find_latest_stable_node(videos)
        if latest:
            latest["channel"] = channel
            candidates.append(latest)
            print(f"  最新稳定节点: {latest['date'].strftime('%Y-%m-%d')} {latest['title']}")
        else:
            print(f"  未找到符合条件的视频")

    if not candidates:
        print("所有频道都没有符合条件的视频")
        return

    result = max(candidates, key=lambda x: x["date"])
    video_id = result["video_id"]
    print(f"\n最新视频: {result['date'].strftime('%Y-%m-%d')}")
    print(f"  来源频道: {result['channel']}")
    print(f"  标题: {result['title']}")
    print(f"  链接: https://www.youtube.com/watch?v={video_id}")

    desc, dl_url, owner_text = get_description_and_download_url(video_id)
    print(f"\n下载地址: {dl_url or '未找到'}")

    transcript = None
    for attempt in range(2):
        try:
            transcript = get_transcript(video_id)
        except Exception as e:
            print(f"  [字幕] 出错: {e}")
        if transcript:
            break
        if attempt < 1:
            print("  [字幕] 重试...")
            import time
            time.sleep(3)

    if transcript:
        print(f"\n视频文字 ({len(transcript)}字)")
    else:
        print("\n[字幕] 无法获取")

    # 密码来源按可信度排序：简介 → 作者评论 → 字幕 → 配置兜底。
    # 作者会在视频里故意念错密码，把正确密码只写在简介里，所以简介优先，
    # 解不开 paste 就换下一个候选，避免字幕里的假密码直接让流程失败。
    candidates = []
    for value in (
        extract_password(desc or ""),
        extract_password(owner_text or ""),
        extract_password(transcript) if transcript else None,
        CONFIG.get("fallback_password"),  # config.yaml 兜底密码
    ):
        if value and value not in candidates:
            candidates.append(value)
    print(f"密码候选: {', '.join(candidates) or '无'}")

    if candidates and dl_url:
            paste_content = None
            for pwd in candidates:
                print(f"\n正在用浏览器解密 paste.to（密码 {pwd}）...")
                paste_content = await decrypt_paste(dl_url, pwd)
                if paste_content:
                    break
                print(f"  密码 {pwd} 解不开，换下一个")
            if paste_content:
                print(f"\n=== Paste 内容 ===")
                print(paste_content)

                import base64 as _b64
                # 主策略：按位置判断（“订阅链接”行后的第一个 URL，按上下文客户端名分类）
                v2ray_url, clash_url = extract_sub_urls_by_position(paste_content)
                urls = re.findall(r'https?://[^\s\n]+', paste_content)
                # 兜底：按 URL 特征匹配（config.yaml 里的 pattern）
                if not clash_url or not v2ray_url:
                    for u in urls:
                        if not v2ray_url and re.search(CONFIG["v2ray_link_pattern"], u, re.I):
                            v2ray_url = u
                        elif not clash_url and re.search(CONFIG["clash_link_pattern"], u, re.I):
                            clash_url = u
                # 兼容更旧的 dlink.host/1drv base64 形态
                if not clash_url or not v2ray_url:
                    for u in urls:
                        if "dlink.host" in u and "jpg" in u:
                            raw = u.split("1drv/")[-1].replace(".jpg", "")
                            try:
                                decoded = _b64.b64decode(raw).decode()
                            except Exception:
                                continue
                            if "/u/c/" in decoded and not clash_url:
                                clash_url = u
                            elif "/t/c/" in decoded and not v2ray_url:
                                v2ray_url = u

                if v2ray_url:
                    print(f"\n=== v2rayN 订阅地址 ===")
                    print(f"  {v2ray_url}")
                    await update_v2rayn_sub(v2ray_url)
                else:
                    print("未找到 v2ray 订阅链接")

                if clash_url:
                    print(f"\n=== Clash Party 订阅地址 ===")
                    print(f"  {clash_url}")
                    await update_clash_party_sub(clash_url)
                else:
                    print("未找到 Clash 订阅链接")
            else:
                print("解密 paste 失败")
    else:
        print("没拿到可用密码或下载地址，跳过解密")


if __name__ == "__main__":
    asyncio.run(main_async())

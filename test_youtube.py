import re
import json
import asyncio
import sqlite3
import shutil
import os
import tempfile
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import parse_qs, urlparse, unquote
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


def get_video_list(channel_url):
    resp = requests.get(channel_url + "/videos", headers=HEADERS, cookies=COOKIES, proxies=proxies, timeout=TIMEOUT)
    resp.raise_for_status()
    data = extract_yt_data(resp.text)
    tabs = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
    videos = []
    for tab in tabs:
        contents = tab.get("tabRenderer", {}).get("content", {}).get("richGridRenderer", {}).get("contents", [])
        for item in contents:
            lockup = item.get("richItemRenderer", {}).get("content", {}).get("lockupViewModel", {})
            if not lockup:
                continue
            vid = lockup.get("contentId", "")
            title_obj = lockup.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title", {})
            title = title_obj.get("content", "") if isinstance(title_obj, dict) else str(title_obj)
            if vid and title:
                videos.append({"video_id": vid, "title": title})
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
    resp = requests.get(url, headers=HEADERS, cookies=COOKIES, proxies=proxies, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


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


def resolve_truncated_url(html, data, truncated_url):
    """补全被 YouTube 截断的简介链接。

    简介里的长链接只显示前一段加省略号（如 “…/?<paste id>#<密钥前 7 位>...”），
    paste.to 拿到不完整的密钥会直接报 “mangled URL” 退出，连密码框都不弹。
    作者会在评论区另贴一份完整地址，这里按 paste id 从评论区取回完整 URL。"""
    id_match = re.search(r"[0-9a-f]{16}", truncated_url)
    api_key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    token = get_comments_token(data)
    if not (id_match and api_key and token):
        return None

    client_version = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
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

    paste_id = id_match.group(0)
    host = urlparse(truncated_url).netloc
    candidates = []
    for raw in re.findall(r"https?://[^\"'\\\s]+", resp.text):
        url = unquote(raw)
        if "youtube.com/redirect" in url:
            # 评论里的链接是跳转形式，真正的地址在 q= 参数里
            url = parse_qs(urlparse(url).query).get("q", [""])[0]
        if paste_id in url and "..." not in url:
            candidates.append(url)
    # 优先取与简介地址同域名的，避免命中评论里的广告链接
    for url in candidates:
        if urlparse(url).netloc == host:
            return url
    return candidates[0] if candidates else None


def get_description_and_download_url(video_id):
    html = get_video_page(video_id)
    data = extract_yt_data(html)
    if not data:
        return "", None
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
                    start_idx = cmd.get("startIndex", 0)
                    length = cmd.get("length", 0)
                    nav = cmd.get("onTap", {}).get("innertubeCommand", {})
                    url_ep = nav.get("commandMetadata", {}).get("webCommandMetadata", {}).get("url", "")
                    if url_ep and url_ep.startswith("http"):
                        url_map[start_idx] = {
                            "display": description[start_idx:start_idx + length],
                            "full_url": url_ep.replace("\\u0026", "&"),
                        }
    download_url = None
    match = re.search(r"下载地址[：:]\s*(\S+)", description)
    if match:
        short_url = match.group(1)
        pos = match.start(1)
        if pos in url_map:
            raw = url_map[pos]["full_url"]
            if "youtube.com/redirect" in raw:
                q = parse_qs(urlparse(raw).query).get("q", [raw])[0]
                download_url = q
            else:
                download_url = raw
        else:
            download_url = short_url
    if download_url and download_url.endswith("..."):
        full_url = resolve_truncated_url(html, data, download_url)
        if full_url:
            print(f"  简介地址被截断，已从评论区补全")
            download_url = full_url
    return description, download_url


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
                    tr_resp = requests.get(tracks[0].get("baseUrl", ""), headers=HEADERS, cookies=COOKIES, proxies=proxies, timeout=TIMEOUT)
                    if tr_resp.status_code == 200:
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
    resp = requests.get(url, timeout=30, verify=False, proxies=proxies)
    resp.raise_for_status()
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
    channel = CONFIG["channel_url"]
    videos = get_video_list(channel)
    print(f"共获取 {len(videos)} 个视频")

    result = find_latest_stable_node(videos)
    if not result:
        print("未找到符合条件的视频")
        return

    video_id = result["video_id"]
    print(f"\n最新视频: {result['date'].strftime('%Y-%m-%d')}")
    print(f"  标题: {result['title']}")
    print(f"  链接: https://www.youtube.com/watch?v={video_id}")

    desc, dl_url = get_description_and_download_url(video_id)
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
        pwd = extract_password(transcript)
        print(f"\n视频文字 ({len(transcript)}字)")
        print(f"密码: {pwd or '未提取到'}")
    else:
        print("\n[字幕] 无法获取，使用缓存密码")
        pwd = CONFIG.get("fallback_password")  # config.yaml 兜底密码

    if pwd and dl_url:
            print(f"\n正在用浏览器解密 paste.to...")
            paste_content = await decrypt_paste(dl_url, pwd)
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
        print("未获取到视频字幕")


if __name__ == "__main__":
    asyncio.run(main_async())

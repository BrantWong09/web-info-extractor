# web-info-extractor

从 YouTube 频道自动获取免费节点订阅并同步到本地代理客户端（v2rayN + Clash Party / mihomo-party）的脚本。

## 工作流程

1. 抓取 `config.yaml` 里配置的所有 YouTube 频道的视频列表，按标题里的日期取最大的"稳定节点"视频（多频道时哪个最新用哪个）。网络请求都带退避重试（走代理抓 YouTube 常中途断连）；频道页被 YouTube 返回同意页/校验页（没有 `ytInitialData`）时自动退回该频道的 RSS 取最近 15 条
2. 提取 paste 下载地址：先取简介里的"下载地址："，简介里没有（作者现在常只写"节点在我的评论区"）或地址被 YouTube 截断时，从**作者本人的评论区留言**里取（评论里还挂着机场广告、Telegram 群链接，所以只认"下载地址："标签，其次同域名/带 paste id 的链接）
3. 从视频简介、作者评论、视频字幕依次提取访问密码，逐个尝试直到解开 paste
4. 用 Playwright 打开 paste 页面解密内容
5. 从解密内容中按正则识别 v2ray 订阅直链和 Clash 订阅直链
6. v2ray 直链写入 v2rayN 的 SQLite 订阅数据库（`SubItem` 表）
7. Clash 直链下载 YAML 配置、过滤本地客户端不支持的加密节点后，写入 Clash Party 的 profiles 目录（不重启客户端，下次启动或手动切换配置时生效）

## 使用方法

1. 安装依赖（字幕获取使用本机 Edge 浏览器，需已安装）：

   ```bash
   pip install -r requirements.txt
   ```

2. 复制 `config.example.yaml` 为 `config.yaml`，填入你自己的频道地址、本地代理、客户端路径等（`config.yaml` 不会提交到仓库）
3. 运行：

   ```bash
   python test_youtube.py
   ```

## 目录说明

- `test_youtube.py` — 主脚本
- `config.example.yaml` — 配置模板（占位符）
- `config.yaml` — 本地隐私配置（已被 .gitignore 排除）
- `scripts/` — 一次性调试脚本（不入库）
- `docs/` — 设计文档

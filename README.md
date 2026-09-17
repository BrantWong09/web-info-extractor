# web-info-extractor

从 YouTube 频道自动获取免费节点订阅并同步到本地代理客户端（v2rayN + Clash Party / mihomo-party）的脚本。

## 工作流程

1. 抓取指定 YouTube 频道的视频列表，找到最新的"稳定节点"视频
2. 从视频简介提取 paste 下载地址（简介里的长链接会被 YouTube 截断，截断时自动从作者评论补全完整地址），从视频字幕提取访问密码
3. 用 Playwright 打开 paste 页面解密内容
4. 从解密内容中按正则识别 v2ray 订阅直链和 Clash 订阅直链
5. v2ray 直链写入 v2rayN 的 SQLite 订阅数据库（`SubItem` 表）
6. Clash 直链下载 YAML 配置、过滤本地客户端不支持的加密节点后，写入 Clash Party 的 profiles 目录（不重启客户端，下次启动或手动切换配置时生效）

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

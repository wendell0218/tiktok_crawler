# tiktok_crawler

面向抖音网页版的关键词视频元数据采集与媒体下载工具。项目使用独立浏览器配置完成登录，由抖音页面生成请求参数，程序只捕获页面已经收到的搜索与详情响应，不维护私有签名算法。

## 能力

- 按关键词搜索视频
- 使用 `aweme_id` 去重
- 保存标题、作者、统计、封面和分享地址
- 保存全部已发现的分辨率、编码、码率和备用媒体地址
- 将元数据采集、媒体地址刷新和视频下载拆成独立阶段
- 按最佳、最低或指定分辨率选择视频
- 按 H.264、H.265 或任意编码选择视频
- 串行下载并统计每条视频的速度
- 小流量探测媒体地址
- 完整下载后立即删除测试文件
- 遇到登录失效、验证码、HTTP 403 或 HTTP 429 时停止或记录明确事件

## 工作方式

```text
持久化 Chrome 登录
        ↓
关键词搜索页自行发出请求
        ↓
捕获 JSON 响应并按 aweme_id 去重
        ↓
SQLite ──→ JSONL
   │
   ├──→ 用 aweme_id 刷新临时媒体地址
   │
   └──→ 无 Cookie 串行下载
```

浏览器部分基于 Playwright 的[持久化浏览器上下文](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)和[网络响应事件](https://playwright.dev/python/docs/network)。登录目录仅供本项目使用，不应换成日常 Chrome 的用户目录。

## 安装

仓库默认使用本机的 `douyin-crawler` Conda 环境：

```bash
source /Users/wendell/Desktop/tiktok_crawler/install.sh
```

默认调用已安装的 Google Chrome。若改用 `--channel chromium`，先在当前环境安装浏览器运行时：

```bash
python -m playwright install chromium
```

## 首次登录

```bash
source /Users/wendell/Desktop/tiktok_crawler/login.sh
```

浏览器会保持打开，完成登录或验证后自动保存状态并退出。登录配置保存在 `data/browser_profile`，该目录包含账号状态并已被 Git 忽略。

## 关键词采集

先修改 `search.sh` 第一项参数中的关键词，再运行：

```bash
source /Users/wendell/Desktop/tiktok_crawler/search.sh
```

默认最多保存 100 条视频，最多处理 20 个实际搜索响应。程序不假设一页固定为 10 条或 15 条，也不自行计算下一页位置；它把页面响应的 `cursor` 作为观测值，以实际返回内容和 `aweme_id` 去重结果计数。

数据默认写入：

```text
data/douyin.db
data/metadata.jsonl
```

## 刷新媒体地址

媒体 CDN 地址可能过期或被拒绝。数据库中的稳定定位字段是 `aweme_id`，分享入口是 `https://www.douyin.com/video/{aweme_id}`。下载前可重新访问详情页并更新媒体地址：

```bash
source /Users/wendell/Desktop/tiktok_crawler/refresh.sh
```

只刷新指定视频：

```bash
python -m dycrawler refresh --aweme-id 7420000000000000001
```

## 下载

```bash
source /Users/wendell/Desktop/tiktok_crawler/download.sh
```

默认串行下载 20 条，每条之间等待 2 秒。实际媒体请求不发送 Cookie，不读取 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY` 环境变量。

常用选择方式：

```bash
python -m dycrawler download --quality best --codec any --limit 20
python -m dycrawler download --quality 1080p --codec h264 --fallback lower --limit 20
python -m dycrawler download --keyword 人工智能 --quality 720p --limit 20
python -m dycrawler download --aweme-id 7420000000000000001 --limit 1
```

已存在的目标文件不会被覆盖。

## 轻量探测和下载后删除

只读取每条媒体地址前 64 KiB，不落盘：

```bash
source /Users/wendell/Desktop/tiktok_crawler/probe.sh
```

完整下载一条后立即删除本次临时文件：

```bash
source /Users/wendell/Desktop/tiktok_crawler/download_delete.sh
```

删除模式不会覆盖或删除同名的既有文件。

## 命令

```text
dycrawler login       建立和更新登录状态
dycrawler search      关键词采集并保存元数据
dycrawler refresh     按 aweme_id 刷新媒体地址
dycrawler list        读取数据库记录
dycrawler export      导出 JSONL
dycrawler download    串行下载视频
dycrawler probe       小流量检查媒体地址
dycrawler status      查看视频、清晰度、关键词和事件数量
dycrawler events      查看最近的风险与失败事件
```

使用 `python -m dycrawler <命令> --help` 查看完整参数。

## 元数据结构

每条记录包含：

```text
aweme_id
title
create_time
author
statistics
duration_ms
width
height
cover_url
share_url
play_url
variants[]
source_keyword
browser_user_agent
collected_at
```

每个 `variants` 项保存 `quality`、`codec`、`width`、`height`、`bitrate`、`fps`、`size_bytes`、`media_uri` 和多个备用 `urls`。`play_url` 只是当前最佳变体的便捷字段，不应被视为永久地址。

## 风控边界

- 默认使用有界数量、串行下载和固定间隔。
- 验证码和登录面板需要人工处理，程序不会尝试绕过。
- HTTP 403 表示服务器拒绝该媒体请求，常见原因包括地址已过期、请求上下文不匹配或 CDN 策略；先运行 `refresh` 再判断。
- HTTP 429 表示请求过多，程序立即停止后续媒体请求。
- 浏览器使用 `--no-proxy-server`，下载器忽略代理环境变量。操作系统级全局 VPN 或全隧道路由无法由本程序可靠绕过，需要在系统侧关闭并自行核验出口地址。
- 浏览器可能因账号状态、网络出口、行为频率或平台规则触发验证，任何固定速率都不能保证永不触发风控。

请仅采集你有权访问和保存的内容，并遵守适用法律、平台规则和内容权利。

## 测试

```bash
source /Users/wendell/Desktop/tiktok_crawler/test.sh
```

测试使用手工构造的响应与本地模拟 HTTP 传输，不访问抖音，也不消耗账号请求额度。

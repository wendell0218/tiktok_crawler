# 抖音视频下载

支持按关键词搜索下载、按用户下载公开视频，同时保存元数据。

## 安装

先安装 Conda 和 Google Chrome，在仓库目录运行：

```bash
conda create -n douyin-crawler python=3.11 -y
source ./scripts/install.sh
```

## 按关键词下载

修改 `crawl_keyword.sh`：

```bash
KEYWORDS="街舞,芭蕾舞,女团舞"
WORKERS=1
COUNT=200
OUTPUT_DIR="downloads"
QUALITY="best"
```

`COUNT` 是每个关键词最多检查的视频条数，重复视频也计数，实际下载会去重。

```bash
source ./crawl_keyword.sh
```

## 按用户下载

修改 `crawl_user.sh`：

```bash
TARGET_USER="影视飓风"
WORKERS=1
COUNT=0
OUTPUT_DIR="downloads/users"
QUALITY="best"
```

支持昵称、抖音号或主页链接；重名时改填目标主页链接。`COUNT` 是唯一视频数，`0` 表示全部可访问的公开视频。

```bash
source ./crawl_user.sh
```

两种模式中，`WORKERS` 是下载并发数；`QUALITY` 支持 `best`、`worst`、`720p`、`1080p` 等；`OUTPUT_DIR` 的相对路径以仓库目录为基准。

首次登录或遇到验证码时，在弹出的浏览器中手动完成后继续。已有视频文件会跳过。

## 风控参考

2026-09-07 本机关键词测试（每词 `COUNT=200`、翻页间隔 5 秒、换词间隔 60 秒）中，首次出现安全验证前的累计成功下载量：

| 下载 worker 数 | 首次验证前成功下载 |
| --- | ---: |
| 1 | 2,231 条 |
| 4 | 1,664 条（同一 Profile 两段运行累计） |

样本少且非严格对照，这不是固定触发阈值，也不保证低于该数量就不验证。验证发生在搜索翻页阶段，`WORKERS` 只控制下载并发。历史测试按页下载，当前关键词入口先搜索再下载，不能将上述数量直接作为运行上限。

## 目录

```text
tiktok_crawler/
├── crawl_keyword.py / crawl_keyword.sh
├── crawl_user.py / crawl_user.sh
├── scripts/       安装及辅助脚本
├── src/dycrawler/ 核心代码
├── tests/         测试
├── data/          登录状态、数据库和关键词元数据
└── downloads/     默认视频目录；用户模式同时保存每轮元数据
```

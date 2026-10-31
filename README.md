# ArchSpec Agent

面向建筑设计人员的规范检索与原页阅读工具，在自己的电脑上运行。

输入问题、关键词或条号，查找相关规范，阅读正文并核对出处、原表和原页。已有资料优先在本地检索，需要补充资料时可搜索官方来源；DeepSeek 和 Tavily 可按需配置。

## 主要功能

- **查找规范**：支持关键词、条号、选定规范及自然问题查询。
- **阅读原文**：查看完整条文、相关续文与表格，打开对应 PDF 原页。
- **核对出处**：结果关联来源、固定资料与页码，原件可下载保存。
- **本机 PDF**：导入自己的 PDF，查找已提取文字、翻页放大并下载原件。
- **本地保存**：资料与阅读状态保存在自己的电脑，刷新后继续查阅。
- **可选 AI 辅助**：使用自己的模型配置辅助理解问题、按出处整理内容。

## 快速开始

先按 [uv 官方说明](https://docs.astral.sh/uv/getting-started/installation/)安装 uv，并重新打开终端。项目默认使用 Python 3.13；uv 会准备需要的解释器和锁定依赖。

下载或克隆本仓库后，在项目目录选择启动入口：

| 系统 | 启动方式 |
| --- | --- |
| macOS | 双击 `启动个人版.command` |
| Linux | 执行 `sh 启动本机版.sh` |
| Windows | 双击 `启动本机版.bat` |

也可在终端执行：

```text
uv sync --frozen --no-dev
uv run --frozen --no-dev --offline --no-env-file python scripts/run_local.py --personal --port 8010
```

打开终端显示的本机网址。首次账号和密码保存在 `data/local-access/personal-access.private.env`，用文本编辑器查看。运行期间保留启动终端，按 Ctrl+C 退出。

## 开始查阅

可以先查《民用建筑设计统一标准》，取得正文后输入“楼梯净宽”，打开对应原页。也可查询“宜昌市建筑日照间距的计算公式”，阅读地方文件及附件。

首次使用从官方来源取得规范资料，仓库不附带规范原件。规范版本和具体项目适用条件可结合出处与原页核对。

## 模型与搜索配置

基础查找无需 Key。需要模型或 Tavily 搜索时，运行：

```text
uv run --frozen --no-dev --no-env-file python scripts/run_local.py --prepare-config
```

在生成的本机私有配置中填写自己的 Key。模型请求按所用服务计费；配置文件、规范资料和本机数据库不提交到 GitHub。

完整步骤见[本机使用说明](docs/本机使用.md)。

## 技术栈

Python · FastAPI · SQLite · PyMuPDF · RapidOCR · 原生 HTML/CSS/JavaScript

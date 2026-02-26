# Pixiv 关注作品索引 / 批量下载（Multi）

一个 **WebUI 优先** 的 Pixiv 工具：用于抓取你关注的画师与其作品，**索引并导出图片 URL**，也支持按需 **下载图片文件**。面向“长期运行、稳定增量更新、多账号 + 代理池”的使用场景。

## 功能特性

- WebUI 配置与 Runner 控制（启动/停止/热更新）
- 多账号调度（基于 `refresh_token`，自动处理 token 轮换）
- 代理池支持（`easy_proxies` / 静态列表 / 禁用）
- 持续索引关注画师作品，落库 SQLite（可导出 URL）
- 可选：下载图片文件（多线程，按账号绑定代理）

## 使用教程（从 0 到跑起来）

### 1) 环境准备

- Python `3.10+`
- 能访问 Pixiv App API 及图片 CDN

安装依赖：

```bash
python -m pip install -r requirements.txt
```

### 2) 准备配置文件

复制示例配置：

Windows：

```bash
copy multi_config.example.json multi_config.json
```

Linux/macOS：

```bash
cp multi_config.example.json multi_config.json
```

然后至少填写（或在 WebUI 里填写也可）：
- `accounts[].id`
- `accounts[].refresh_token`

> 关于 `refresh_token`：本项目需要它来调用 Pixiv App API 获取关注/作品/URL。获取方式因人而异（你可以使用你已有的方式/脚本/工具），本仓库不内置登录抓取流程。

### 3) 启动 WebUI

```bash
python web_ui.py
```

打开：

`http://127.0.0.1:5000/`

### 4) 在页面里配置并启动

1. **Accounts**：添加/更新账号（`refresh_token`、是否启用 worker、是否用于拉关注列表）
2. **Proxy Pool**：选择代理来源（推荐 `easy_proxies`，也可静态或禁用）
3. **Worker Mode**：选择运行模式与下载参数（见下文）
4. 点击 **Start Runner**
5. 在 **Runtime Status** 查看运行情况与 DB 计数

### 5) 导出 URL（可选）

- 导出 regular：
  - `/api/multi/export?kind=regular`
- 导出 original：
  - `/api/multi/export?kind=original`

## Worker 模式说明

在 WebUI 的 **Worker Mode** 里可切换：

- `index_urls`：只索引并导出 URL（不下载文件）
- `download_images`：索引 URL 后，按配置下载图片文件  
  - `download_kind=original`：下载原图（默认）
  - `download_kind=regular`：下载 regular 图

下载目录：
- 默认保存到 `runtime_dir/downloads/<original|regular>/<member_id>/...`
- 可用 `worker.download_root` 自定义输出目录

下载与鉴权/代理说明：
- `refresh_token` 用于 App API 拉取关注/作品与得到图片 URL（access_token 会自动刷新）
- 图片文件下载本身走图片 URL，并带 `Referer`；下载请求同样使用该账号绑定的代理（来自代理池分配）

## 配置字段速查

### `accounts`
- `id`：账号标识（用于 worker 命名/绑定代理）
- `refresh_token`：Pixiv refresh token
- `downloadDelay`：该账号请求延迟（秒，影响索引与下载节奏）
- `enabled`：是否启用该账号的 worker
- `follow_source`：该账号是否用于抓取关注列表

### `proxy_pool`
- `source`：`easy_proxies` / `static` / `none`
- `refresh_interval_sec`：代理池刷新间隔
- `max_tokens_per_proxy`：单个代理最多绑定多少账号
- `bindings_strict`：容量不足时是否严格限制
- `easy_proxies.*`：Easy Proxies 服务配置
- `test.*`：代理探测配置

### `worker`
- `mode`：`index_urls` / `download_images`
- `download_kind`：`original` / `regular`
- `download_concurrency`：每个 worker 的下载线程数（1~32）
- `download_root`：下载输出目录（空 => `runtime_dir/downloads`）

## 运行时数据

- 默认运行目录：`.multi_runtime/`
- 默认数据库：`.multi_runtime/db.multi.sqlite`
- Runner 状态：`.multi_runtime/status.json`

## 安全与隐私（开源仓库最佳实践）

- 配置文件可能包含敏感信息（token、代理账号密码），请妥善保管
- 本仓库默认在 `.gitignore` 中忽略 `multi_config.json` 与运行时目录 `.multi_runtime/`
- 如需在 CI/服务器部署，建议使用私密的配置分发方式（环境变量/私有文件/密钥管理等）

## 免责声明

仅供学习与研究使用，请遵守 Pixiv 的相关条款与当地法律法规。作者不对滥用或由此产生的风险负责。

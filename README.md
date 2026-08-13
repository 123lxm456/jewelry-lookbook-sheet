# 商品视觉图生成系统

## 项目简介

项目将一张商品图片生成 5 张差异化商品展示图和 1 张商品信息长图，并提供微信 OpenID 登录、账户充值与按次计费、任务进度与断点恢复、历史记录、ZIP 下载及管理员后台。当前支持珠宝、箱包、行李箱、鞋、帽、玩具和其他非服装商品；服装类会在生成前拒绝。

系统由 FastAPI Web/API、Python 图片工作流、静态前端和 PHP 微信支付回调组成。Qwen 负责商品分析和文案，图片模型负责场景图生成，Pillow 负责版式处理和长图合成。

## 当前主要功能

- 微信网页授权登录，按 OpenID 隔离用户、会话、余额和生成数据；
- 个人中心、充值套餐、余额/生成次数、充值与消费记录；
- JPEG、PNG、WebP 商品图片上传，单文件默认最大 20 MB；
- 按类别选择展示策略，并行生成 5 张差异化商品图；
- 生成商品信息长图，展示和下载共 6 张最终图片；
- 持久化任务进度、超时维护、失败恢复和服务重启后的断点续跑；
- 用户私有历史记录、移动端下载交接和 ZIP 打包；
- 独立管理员登录，以及用户、任务、充值订单的后台查询；
- MySQL 生产存储，SQLite 仅用于本地开发和自动化测试；
- 微信支付 v3 JSAPI/Native 下单、通知验签和幂等入账。

## 项目目录结构

```text
.
├── app.py                         # FastAPI 页面、API、队列和任务恢复
├── auth.py                        # 用户、会话、账户、订单和数据库逻辑
├── product_workflow/              # 通用商品模型、类别注册和提示词构建
├── jewelry_workflow/              # Qwen 视觉客户端及旧珠宝规格兼容模型
├── scripts/                       # 分析、生成、质量检查和长图合成脚本
├── configs/
│   ├── categories/                # 商品类别定义
│   └── strategies/                # 各类别五图展示策略
├── prompts/                       # 通用提示词与可选后处理模板
├── web/                           # 用户端、个人中心、支付页和管理后台前端
├── config/payment_packages.json   # 充值套餐唯一配置
├── payment_bootstrap.php          # PHP 支付公共配置、数据库和 SDK 初始化
├── pay.php                        # 微信支付下单入口
├── check_pay.php                  # PHP 支付状态检查入口
├── notify/wxpay.php               # 微信支付通知入口
├── deploy/                        # Nginx/PHP-FPM 配置示例
├── tests/                         # 离线工作流与 Web/API 测试
├── run_web.sh                     # Web 服务启动入口
├── run_workflow.sh                # 单商品 CLI 工作流入口
├── requirements.txt               # Python 依赖
├── composer.json                  # PHP 支付依赖
├── product.example.json           # CLI dry-run 与测试规格样例
├── jewelry-test.jpg               # 测试商品图
└── image1.jpg                     # 默认五图风格参考
```

运行时目录不属于源码：`outputs/` 保存上传、任务状态、过程文件和最终结果，`var/` 保存本地 SQLite 数据，`.venv/` 与 `vendor/` 分别保存 Python 和 Composer 依赖。这些路径均已加入 `.gitignore`。不要删除正在使用的 `outputs/` 或数据库文件，否则会破坏历史记录和断点恢复。

## 环境要求

- Linux/macOS 或可运行 Bash 的环境；
- Python 3.10+；
- PHP 8.0+、PHP-FPM、PDO MySQL、OpenSSL 和 Composer（启用微信支付时）；
- MySQL（生产环境）；
- Pillow 可用的中文字体。代码会依次查找常见 Noto/文鼎 CJK 字体路径；部署前应通过 dry-run 和测试确认服务器字体；
- 可访问兼容 OpenAI SDK 的 Qwen 视觉服务和图片生成服务。

## 安装依赖

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
composer install --no-dev --optimize-autoloader
```

不启用微信支付或仅运行 Python 测试时，可以不安装 Composer 依赖。

## 配置说明

复制示例文件后填写真实配置，`.env` 不应提交：

```bash
cp .env.example .env
```

主要环境变量如下。密钥、密码和 Token 均只填写在 `.env` 或部署平台的密钥管理中。

| 分组 | 环境变量 |
| --- | --- |
| 数据库 | `DB_DRIVER`、`DB_HOST`、`DB_PORT`、`DB_USER`、`DB_PASSWORD`、`DB_NAME`、`DB_CHARSET`；SQLite 开发环境使用 `APP_DB_PATH` |
| 微信登录 | `WECHAT_APP_ID`、`WECHAT_APP_SECRET`、`WECHAT_REDIRECT_URI`、`COOKIE_SECURE` |
| 管理后台 | `ADMIN_USERNAME`、`ADMIN_PASSWORD` |
| 商品分析 | `QWEN_API_KEY`、`QWEN_BASE_URL`、`QWEN_MODEL`、`QWEN_RESPONSE_FORMAT`、`QWEN_VALIDATION_ATTEMPTS` |
| 图片生成 | `IMAGE2_API_KEY`、`IMAGE2_BASE_URL`、`IMAGE2_PARALLELISM`、`IMAGE2_GLOBAL_PARALLELISM`、`IMAGE2_MAX_ATTEMPTS`、`IMAGE2_RETRY_DELAY`、`IMAGE2_BYPASS_PROXY` |
| Web/任务 | `WEB_HOST`、`WEB_PORT`、`APP_OUTPUT_ROOT`、`WEB_MAX_ACTIVE_JOBS`、`WEB_MAX_QUEUED_JOBS`、`WEB_JOB_TIMEOUT_SECONDS`、`WEB_QUEUE_TIMEOUT_SECONDS`、`WEB_ORPHAN_RESERVATION_TIMEOUT_SECONDS`、`WEB_MAINTENANCE_INTERVAL_SECONDS` |
| 支付桥接 | `PAYMENT_REQUIRED`、`PAY_CREATE_URL`、`PAY_BRIDGE_SECRET`、`DOWNLOAD_TRANSFER_SECRET`、`DOWNLOAD_TRANSFER_TTL_SECONDS`、`APP_PUBLIC_URL` |
| 微信支付 | `WX_APPID`、`WX_MCH_ID`、`WX_API_V3_KEY`、`WX_PRIVATE_KEY_PATH`、`WX_MCH_CERT_PATH`、`WX_PLATFORM_PUBLIC_KEY_PATH`、`WX_PLATFORM_PUBLIC_KEY_ID`、`WX_NOTIFY_URL` |

生产环境必须设置独立的管理员凭据和签名密钥，启用 `COOKIE_SECURE=true`，并保持 `WECHAT_DEV_LOGIN=false`。支付证书应置于公开 Web 目录之外；若暂存于项目 `cert/`，必须使用 Nginx 拒绝访问并限制文件权限。

## 启动方式

启动 Web/API：

```bash
./run_web.sh
```

默认监听 `0.0.0.0:8000`，可通过 `WEB_HOST`、`WEB_PORT` 或 `WEB_PYTHON` 覆盖。`run_api.sh` 是供旧进程管理配置使用的兼容入口，新部署应直接调用 `run_web.sh`。

执行单商品 CLI：

```bash
./run_workflow.sh --output-dir outputs/imagegen/my-item product.jpg
```

只验证参数、配置、提示词和排版准备而不请求模型：

```bash
./run_workflow.sh --dry-run --output-dir /tmp/product-dry-run jewelry-test.jpg
```

CLI 还支持 `--detail`、`--style`、`--force`、`--force-analysis`、`--mirror-only` 和 `--force-mirror`。每个商品应使用独立输出目录。

## 主要业务流程

```text
微信 OpenID 登录
  → 充值到账并增加余额/生成次数
  → 上传商品图并原子预留 1 次额度
  → Qwen 分析类别、商品事实、五图方案和文案
  → 按类别策略并行生成 5 张展示图
  → 可选质量检查与后处理
  → 合成商品信息长图
  → 成功扣减额度，持久化 6 图结果与 ZIP
  → 个人中心查看历史、恢复失败任务或下载结果
```

任务默认保存在 `outputs/wechat-{用户ID}-{OpenID摘要}/job-{时间}-{任务ID}/`。`job-state.json`、生成清单和已完成面板用于进度展示及断点恢复；所有用户和管理员下载接口都会再次校验身份与任务归属。

## 部署说明

1. 使用 MySQL 配置启动 FastAPI，并由进程管理器调用 `run_web.sh`。
2. 用 Nginx 反向代理 FastAPI；微信授权域名和 `WECHAT_REDIRECT_URI` 必须一致且使用 HTTPS。
3. 安装 Composer 生产依赖，将 `deploy/nginx-payment.conf.example` 中的 PHP-FPM Socket、站点前缀和实际项目路径改为部署值。
4. 确保 `pay.php`、`check_pay.php` 和 `notify/wxpay.php` 由 PHP-FPM 执行，禁止浏览器读取 `.env`、`cert/`、`vendor/`、`composer.json` 和 `payment_bootstrap.php`。
5. 确认微信支付 JSAPI/Native 权限、回调 URL、商户证书和平台公钥配置后，再开放充值入口。

数据库表由 `auth.py` 在启动时以兼容方式创建和补齐；当前项目没有独立迁移目录。生产升级前仍应备份数据库和 `outputs/`。

## 注意事项

- `config/payment_packages.json` 是套餐金额和次数的唯一服务端来源；调整套餐时同步检查前端展示和相关测试。
- `product-long.png` 是正式长图；`jewelry-long.png` 及对应布局 JSON 是旧接口兼容别名，暂不能删除。
- `jewelry_workflow/product_spec.py` 用于读取历史 1.0 规格，虽然属于旧模型但仍在运行时兼容链中。
- `outputs/`、`var/`、`.env`、`cert/*.pem`、`.venv/` 和 `vendor/` 不应纳入版本控制。
- 运行测试：`.venv/bin/python -m unittest discover -s tests -v`。
- Python/Shell/前端静态检查可分别使用 `compileall`、`bash -n` 和 `node --check`；PHP 文件可使用 `php -l` 检查。

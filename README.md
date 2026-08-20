# 商品视觉图生成系统

## 项目简介

上传一张商品图，系统生成 5 张差异化展示图和 1 张商品信息长图。项目包含 FastAPI Web/API、商品分析与图片工作流、静态前端，以及 PHP 微信支付入口与回调。

## 当前主要功能

- 微信 OpenID 登录、用户数据隔离、个人中心与管理员后台；
- 余额/生成次数、充值套餐和微信支付 v3；
- JPEG、PNG、WebP 商品图上传（默认上限 20 MB）；
- 珠宝、箱包、行李箱、鞋、帽、玩具和其他非服装商品的五图策略；服装会在生成前拒绝；
- Qwen 商品分析、文案生成、五图一致性/重复度检查和按需重试；
- 任务进度、失败断点恢复、历史查看、6 图展示及 ZIP 下载；
- MySQL 生产存储；SQLite 仅用于本地开发和测试。

## 项目目录结构

```text
.
├── app.py                    # FastAPI 路由、队列、任务恢复和下载
├── auth.py                   # 用户、会话、账户、订单与数据库
├── product_workflow/         # 通用商品模型、分类和提示词构建
├── jewelry_workflow/         # Qwen 客户端及历史规格兼容
├── scripts/                  # 分析、生成、质量检查、打包与长图合成
├── configs/                  # 商品分类和五图策略
├── prompts/                  # 基础与后处理提示词模板
├── web/                      # 用户端、支付页和管理后台静态资源
├── config/payment_packages.json
├── deploy/                   # Nginx/PHP-FPM 配置示例
├── tests/                    # 离线工作流和 Web/API 测试
├── run_web.sh                # Web 服务入口
├── run_workflow.sh           # 单商品 CLI 工作流入口
├── requirements.txt
└── composer.json
```

`outputs/`、`var/`、`.venv/`、`vendor/`、`.env` 和证书私钥均是运行时内容，不提交 Git。

## 环境要求

- Python 3.10+；
- PHP 8.0+、Composer、PDO MySQL 和 OpenSSL（启用微信支付时）；
- MySQL（生产环境）；
- 可访问兼容 OpenAI SDK 的 Qwen 视觉服务和图片生成服务；
- Pillow 可用的中文字体（部署前应运行 dry-run 验证）。

## 安装依赖

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
composer install --no-dev --optimize-autoloader
cp .env.example .env
```

仅运行 Python 测试或未启用支付时，不需要 Composer 依赖。

## 配置说明

在 `.env` 或部署平台密钥管理中设置配置；不得提交真实密钥、密码或证书。完整变量清单见 `.env.example`。

主要分组：

- 数据库：`DB_*`；本地 SQLite 使用 `DB_DRIVER=sqlite` 与 `APP_DB_PATH`；
- 登录与安全：`WECHAT_*`、`COOKIE_SECURE`、`ADMIN_*`、`RATE_LIMIT_*`；
- 模型：`IMAGE2_*`、`QWEN_*`、`SERIES_*`、`POSTPROCESS_QUALITY_GATE`；
- Web 和任务：`APP_OUTPUT_ROOT`、`WEB_*`、`AUTH_RESET_ON_START`；
- 支付与下载：`PAYMENT_*`、`DOWNLOAD_TRANSFER_*`、`WX_*`、`APP_PUBLIC_URL`。

生产环境必须使用 `APP_ENV=production`、HTTPS、`COOKIE_SECURE=true`、禁用 `WECHAT_DEV_LOGIN`，并设置非默认管理员凭据及签名密钥。

## 启动方式

启动 Web/API：

```bash
./run_web.sh
```

单商品 CLI：

```bash
./run_workflow.sh --output-dir outputs/imagegen/my-item product.jpg
```

不请求模型的配置/排版检查：

```bash
./run_workflow.sh --dry-run --output-dir /tmp/product-dry-run jewelry-test.jpg
```

CLI 支持 `--detail`、`--style`、`--force`、`--force-analysis`、`--mirror-only` 和 `--force-mirror`。每个商品应使用独立输出目录。

## 主要业务流程

```text
上传输入图
→ Qwen 分析主商品与生成策略
→ 构建提示词、并行生成五图
→ 一致性/重复度检查，仅重试失败面板
→ 合成长图
→ 打包 ZIP、更新任务状态
→ 历史展示与下载
```

已有的同输入分析会复用任务内 `product-spec.json` 及用户级 `.analysis-cache`；恢复任务会校验已生成图片并只补齐缺失步骤，不会因刷新历史页再次调用模型。

## 任务产物说明

Web 任务位于：`outputs/wechat-{用户ID}-{OpenID摘要}/job-{时间}-{任务ID}/`。

成功任务仅长期保留：

```text
job/
├── input.jpg                 # 已净化的用户原始输入
├── product-spec.json         # Qwen 分析、商品信息和恢复依据
├── display-plan.json         # 最终五图展示方案
├── generation-manifest.json  # 输入/策略指纹，供安全重复运行比对
├── prompts/                  # 此任务实际发送给图片模型的五份提示词
├── job-state.json            # 进度、状态、用户归属与恢复检查点
├── panel-01.png … panel-05.png
├── product-long.png
└── product-images.zip        # 六张最终图的下载包
```

执行中的 `work/`（参考裁剪、掩码、质量缓存、布局缓存）、`logs/` 和 `page.json` 只用于运行与失败恢复；任务成功后会自动删除。`prompts/` 会保留，因为它记录该任务的精确模型输入。失败任务同样保留这些运行材料，以便诊断和断点恢复。ZIP 与前端展示引用同一批 6 张最终图片，不另存图片副本。

## 部署说明

1. 以 MySQL 配置启动 `run_web.sh`，并用进程管理器守护。
2. 使用 Nginx 反向代理 FastAPI；微信授权回调必须使用 HTTPS 且与配置一致。
3. 安装 Composer 生产依赖，并基于 `deploy/nginx-payment.conf.example` 配置 PHP-FPM、路径和站点前缀。
4. 禁止公开读取 `.env`、`cert/`、`vendor/`、`composer.json` 与 `payment_bootstrap.php`。
5. 升级前备份数据库和 `outputs/`。

## 注意事项

- `config/payment_packages.json` 是套餐金额和次数的唯一服务端来源。
- `jewelry_workflow/product_spec.py` 仍用于读取历史规格，不能删除。
- 旧完成任务若只有 `jewelry-long.png`，读取接口仍兼容；新任务只写入 `product-long.png`。
- 运行测试：`.venv/bin/python -m unittest discover -s tests -v`。
- 基础检查：`python -m compileall`、`bash -n run_workflow.sh`、`node --check web/*.js`、`php -l`。

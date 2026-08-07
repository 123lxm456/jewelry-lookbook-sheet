# Jewelry Lookbook Sheet

将一张珠宝商品图片转换为五场景商品信息长图，支持 Web 和 CLI。工作流使用 Qwen 视觉模型提取结构化商品信息，通过 `gpt-image-2` 生成五张场景图，再由 Pillow 完成动态避让文字排版和长图拼接。

## 工作流程

```text
商品图
  -> Qwen 商品分析与五段文案
  -> 渲染五个图片提示词
  -> gpt-image-2 并发生成五张场景图（第五张先生成完整镜面场景，再执行镜面几何一致性校正）
  -> 内容避让与五种不重复文字布局
  -> jewelry-long.png
```

五张场景分别为佩戴、微距、静物、礼赠和镜面展示。商品类别、数量、佩戴位置和结构来自输入图片分析，不在后续流程中硬编码。

## 安装

要求：

- Linux/macOS 或支持 Bash 的环境；
- Python 3.10+；
- `requirements.txt` 中的 Python 依赖；
- 系统 CJK 字体：
  - `/usr/share/fonts/opentype/arphic/uming.ttc`
  - `/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc`
  - `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`
- 项目内置的图片生成 API 适配器（默认），或通过 `IMAGE_GEN_CLI` 指定兼容脚本。

建议使用项目虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`run_web.sh` 会优先使用 `.venv`，并让后续工作流使用同一 Python 环境。也可以设置 `WEB_PYTHON=/path/to/python`。

## 配置

在项目根目录创建 `.env`：

```dotenv
QWEN_API_KEY=...
QWEN_BASE_URL=http://your-qwen-service/v1
IMAGE2_API_KEY=...
IMAGE2_BASE_URL=https://your-image-service/v1
```

常用可选变量：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `QWEN_MODEL` | 自动发现 | 指定 Qwen 模型 |
| `QWEN_RESPONSE_FORMAT` | `auto` | JSON 响应模式 |
| `QWEN_VALIDATION_ATTEMPTS` | `3` | 分析结果修复次数 |
| `IMAGE2_PARALLELISM` | `5` | 图片生成并发数，范围 1–5 |
| `IMAGE2_MAX_ATTEMPTS` | `4` | 可重试图片请求次数 |
| `IMAGE2_RETRY_DELAY` | `60` | 重试等待秒数 |
| `IMAGE2_BYPASS_PROXY` | `false` | 图片 API 直连，代理导致连接失败时启用 |
| `IMAGE_GEN_CLI` | `scripts/image_gen_api.py` | 自定义兼容图片生成脚本 |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8000` | Web 监听地址和端口 |
| `WEB_MAX_ACTIVE_JOBS` | `2` | Web 最大活跃任务数 |

`.env` 已被 Git 忽略，不要提交真实 API 密钥。

## 启动 Web

```bash
./run_web.sh
```

默认访问 `http://127.0.0.1:8000`。端口被占用时可指定其他端口：

```bash
WEB_HOST=127.0.0.1 WEB_PORT=8001 ./run_web.sh
```

Web 支持 JPEG、PNG 和 WebP，默认最大 20 MB。根路径 `/` 始终显示登录/注册页；登录后进入受保护的 `/app` 工作台。每次进入工作台都会清除 LocalStorage、SessionStorage 和 IndexedDB，不恢复旧任务、上传文件、进度或结果；访问根路径会撤销当前会话 Cookie，必须重新登录。任务状态保存在服务进程内存中，生成文件默认按用户名隔离保存在 `outputs/{用户名}/job-{任务时间}-{任务ID前8位}/`；不同用户无法读取彼此的任务或结果。需要覆盖输出根目录时使用 `APP_OUTPUT_ROOT`。

## 使用 CLI

基本用法：

```bash
./run_workflow.sh --output-dir outputs/imagegen/my-item product.jpg
```

指定细节图和风格图：

```bash
./run_workflow.sh \
  --detail product-detail.png \
  --style image1.jpg \
  --output-dir outputs/imagegen/my-item \
  product.jpg
```

常用参数：

- `--dry-run`：不调用模型，验证参数和提示词渲染；
- `--force`：重新生成全部面板；
- `--force-analysis`：强制重新分析商品；
- `--mirror-only`：保留前四张，仅重新执行镜面分支；该模式会跳过全量面板指纹检查，但仍要求前四张图已经存在；
- `--force-mirror`：重新生成第 5 张镜面场景图。

建议每个商品使用独立输出目录。输入或生成版本与旧面板不一致时，工作流会拒绝混用；需要覆盖时显式使用 `--force`。

## 提示词与风格图

根目录 `prompts/` 包含五张图片的 `string.Template` 模板，是运行所必需的，不能删除。模板中的 `$category`、`$subject_description_en` 等变量会由当前商品信息替换。渲染后的提示词位于输出目录的 `prompts/`，任务完成后可以删除，但保留它们有助于审计和排错。

默认风格长图为 `image1.jpg`。`prepare_style_reference.py` 会识别标题、五张广告图之间的白色分隔带和底部留白，生成包含完整五张广告图的 `work/style-reference.jpg`。没有五图结构的自定义风格图使用兼容比例裁切。

风格图只提供色调、构图和版式参考，不是最终长图的直接素材。五张商品图仍由各自提示词独立生成。

## 文字排版

最终合成会优先保留五张 `2:3` 源图中的珠宝主体，并按场景调整裁切焦点。文字从左侧栏、右侧栏、竖排侧栏、顶部标题、底部描述、环绕、极简留白和杂志排版中动态选择；布局评分会避让人物皮肤/面部、手部、珠宝主体、高光和细节区域，并优先使用生成提示词指定的留白安全区。

每次合成同时输出 `jewelry-long-layout.json`，记录五张图的模板、坐标和内容风险评分。

## 主要输出

```text
product-spec.json              # 商品结构与文案
page.json                      # 五段排版文案
generation-manifest.json       # 当前生成指纹
completed-manifest.json        # 已完成版本指纹
prompts/panel-01.txt ... 05    # 渲染后的图片提示词
panel-01.png ... panel-04.png  # 前四张场景图
panel-05.png                   # 镜面场景经几何一致性二次校正后的结果
jewelry-long.png               # 最终商品信息长图
jewelry-long-layout.json       # 动态布局记录
logs/                          # 图片模型请求日志
work/style-reference.jpg       # 裁剪后的风格参考
work/panel-05-mirror-refine.txt # 镜面几何校正提示词（可审计）
```

## 测试

运行全部离线测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

验证 Shell 流程但不请求模型：

```bash
./run_workflow.sh --dry-run --output-dir /tmp/jewelry-dry-run jewelry-test.jpg
```

离线测试覆盖数据校验、图片压缩、提示词渲染、风格图裁切、并发生成、动态布局以及 Web 上传、进度、预览和下载。真实模型服务的输出质量和计费不属于离线测试范围。

## 安全说明

- Web 已增加用户注册、登录和退出；密码使用带随机盐的 scrypt 哈希保存，不保存明文密码；
- 首页、上传、任务状态、进度、结果和下载接口均要求登录；任务接口会校验当前用户归属；
- 会话存储在 SQLite 中，生产环境应设置 `COOKIE_SECURE=true` 并通过 HTTPS 访问；
- Web 任务和用户数据库应放在网站公开静态目录之外，并使用独立 Linux 用户运行服务；
- 对外部署前应增加访问控制、限流和任务文件清理；
- 上传文件会经过格式、尺寸和像素数校验；
- 低置信度材质不会被文案声明为钻石、黄金、铂金或纯银等已验证材质。

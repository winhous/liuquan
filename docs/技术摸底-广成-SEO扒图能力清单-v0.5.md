# 技术摸底报告：广成引擎 SEO + 扒图能力清单（供刘全 v0.5 详设）

- 摸底对象：`/home/winhous/project/studio`（广成引擎，只读，未改动任何文件）
- 摸底日期：2026-09（会话实读，非凭记忆）
- 用途：为「刘全 v0.5（SEO + 扒图）详设」提供可平移能力清单、依赖现状与适配建议
- 范围：eHunt API 通道、eHunt CDP 通道、keyword-research / seo-optimize / image-download 三个工序、xhs/xianyu 两个连接器、ehunt-chrome.sh 启动脚本

---

## 0. 机制差异：广成「外联 connector」 vs 刘全「引擎 connector 层 + HTTP 读接口」

| 维度 | 广成 | 刘全 |
|---|---|---|
| 调用入口 | 工序内 `engine.invoke_connector(ref, task, input_data)`，进程内直接实例化 connector 类 | 引擎 connector 层 + HTTP 读接口（connector 作为内部服务/网关暴露，调用方经 HTTP 读数据） |
| connector 契约 | 统一类接口 `run(task="", prev_deliverable=None) -> dict`（返回 `{ok, ...}` 或 `{ok: False, note}`） | 需自行定义；建议把广成 connector 的 `run()` 逻辑原样保留为纯函数/类，外面套一层 HTTP 端点即可 |
| 注册/声明 | `registry/connectors.yaml`：`id/provider/platform/env_keys/status(active/experimental)` | 刘全 config 或数据库注册表（字段级可对齐 env_keys/status） |
| 降级语义 | connector 层返回 `ok=False + note`，工序决定失败/跳过；不抛异常 | 可复用同一语义：HTTP 层透传 `ok/note` |
| 可用性探测 | connector 各自实现 `available` 属性（如探测 CDP 端口 / 检查 env key / vendor 存在） | 建议保留 `available` 语义，作为 HTTP 接口的 health/就绪检查 |

**结论**：广成的 connector 类本身就是"自包含、无引擎依赖"的（只 import stdlib + playwright + 自家 vendor），平移时**逻辑可直接搬**，只差一层 HTTP 包装 + 配置声明。本报告逐条标注可复用程度。

---

## 1. 【eHunt API 通道】`runtime/ehunt_connector.py`（129 行）

### 请求结构
- 端点：`POST https://api.ehunt.ai/api/v1/items`（商品查询接口）
- 鉴权：Header `X-VIP-TOKEN: <EHUNT_API_KEY>`（`api.ehunt.ai/workspace-api` 自助建 Key，读环境变量 `EHUNT_API_KEY`）
- 请求体（连接器实际只发最小集）：
  ```json
  {"search_key": "name necklace", "sort_by": 2, "desc": 1, "page_num": 1, "page_size": 10}
  ```
  - `sort_by`：1=周销量(默认) 2=总销量 3=评论 4=收藏 11=周评论 12=周收藏 13=月销量；连接器用 2（总销量降序，头部竞品最有用）
  - `page_size`：最大 100（官方建议 ≤50 省积分）；连接器 clamp 到 `max(1, min(x, 100))`，工序默认 10
  - 官方还支持大量可选筛选：status/category/price/sales/sales_weekly/favorites/favorites_weekly/reviews/reviews_weekly/product_type/is_raving/is_pick/is_bestsell/listed_time/country/currency_code——连接器未用，留作扩展
- 传输：`urllib.request`（标准库零依赖）；**UA 伪装必要**：eHunt WAF 直接 403 拦 `python-urllib/*`，连接器用 `Mozilla/5.0 (X11; Linux x86_64) guangcheng-ehunt-connector/1.0`

### 响应结构（官方文档 + 代码 + 单测三方确认）
```json
{
  "code": 200, "message": "success",
  "data": {"product_num": 134689, "list": [{"title", "price", "sales_total", "reviews", "favorites", "tags", "store_name", "product_url", ...}]},
  "quota": {"used_today": 2, "remaining_today": 198}
}
```

### 配额字段（记账点）
- 服务端记账：每条返回商品消耗 1 积分；**今日已用/剩余由响应体 `quota.used_today / remaining_today` 回传**——这是唯一的配额记账点，连接器只透传，不做本地账本
- 当日积分不足返回 HTTP 429
- Free/VIP 200 条/天（每条 page_size 内的 listing 都算积分，所以 page_size 与词数乘积=日消耗）
- 客户端两道闸：连接器内 `keywords[:8]` 硬顶 8 词 + `page_size` 上限

### 错误处理
- 网络异常 → `{ok: False, note: "eHunt 请求失败: <exc>"}`（不抛断流程）
- `body.code != 200` → `{ok: False, note: "eHunt 返回 code=...: <message>"}`
- 无 key → `{ok: False, note: "EHUNT_API_KEY 未配置"}`
- `transport` 可注入（callable），离线单测用

### 聚合（连接器内部）
- 每词产出画像：`product_num`（匹配商品总数=竞争度代理）+ `avg_price_top`（top 均价）+ `top_competitors`（前 5：title/price/sales_total/reviews/favorites/tags[]/store_name）

### 可复用程度：**直接照搬**
- 零第三方依赖（纯 urllib），逻辑自包含；只依赖 `EHUNT_API_KEY` 环境变量
- 平移改动：刘全 connector 层照抄类（或抽成纯函数），key 进刘全 `.env`；`transport` 注入可保留给单测

---

## 2. 【eHunt CDP 通道】`runtime/ehunt_keyword_connector.py`（277 行）

### 背景
- eHunt **无**关键词搜索量/竞争度 API 端点（`docs.api.ehunt.ai/llms.txt` 已确认），`ehunt.ai/cn/keyword-tool` 页面是唯一来源 → 只能 CDP 读页

### 连 9222 调试 Chrome
- 前置：`google-chrome --remote-debugging-port=9222 --user-data-dir=~/.local/share/ehunt-chrome-profile`（登录态不丢），`scripts/ehunt-chrome.sh` 一键启动（见 §6）
- 可用性探测：`urllib GET {cdp}/json/version`（3s 超时），端口不通即 `available=False`（也可注入 transport 恒可用）
- 会话：`playwright.sync_api.sync_playwright().start()` → `chromium.connect_over_cdp("http://127.0.0.1:9222")` → 复用已开的 `keyword-tool` 页（URL 含 "keyword-tool"），没有则 `new_page + goto`
- `browser.close()` 在 connect_over_cdp 下只断连接，**不动常驻登录态 Chrome**

### 读哪个页面 / 抓哪些指标
- 页面：`https://ehunt.ai/cn/keyword-tool`（中文站）
- 逐词流程：找搜索框（选择器依次试 `input[placeholder*="关键词"] / input[placeholder*="keyword"] / input[type=text] / input:not([type])`）→ `fill + Enter` → 等 `article:has-text('竞争度')`（指标卡渲染完成标志）→ 再等 1500ms（相关词卡片补齐）→ 读全部 `article` 的 innerText
- 指标卡文本（标签即分隔符，2026-08-16 实采）：
  `name necklace频率13竞争度225.1K浏览量总76.4M月26.7M收藏量总2.2M月3.9K销量总1.9M月5.1K分数3.39Google PD100Google CPC$2.04 历史趋势`
- 指标集：**频率 / 竞争度 / 浏览量总·月 / 收藏量总·月 / 销量总·月 / 分数 / Google PD / Google CPC** + 相关词卡片（`max_related=5` 条）
- `parse_number`：`225.1K→225100`、`76.4M→76400000`、`NR→None`；CPC 去 `$` 与逗号

### 表格解析（兜底）
- 页面双形态：≥1280px 走表格、窄窗走指标卡；指标卡优先，卡片不可见时读 `table > tbody tr` 兜底
- 列序 2026-08-16 按表头/数据格 x 坐标逐像素对齐钉死：`0 勾选 1 收藏 2 关键词 3 频率 4 竞争度 5/6 浏览量总/月 7/8 收藏量总/月 9/10 销量总/月 11 分数 12 Google PD 13 Google CPC`
- 注：表格兜底**依赖列序硬编码**，页面改版即失效（代码注释已警示）

### 失败降级
- CDP 连不上 / 读页异常 → `{ok: False, keywords: {}, note}`，**不抛异常**
- 调用方（keyword-research）语义：CDP 不可用 → 整段跳过逐词指标、记 `metrics_note`，主流程不失败
- 精确词卡缺失 → 该词 `note: "页面未返回该词的精确指标卡"` + 相关词照给

### 可复用程度：**适配改造（核心逻辑直接搬，运行前置需在刘全落地）**
- 直接搬：`parse_card_text` 正则解析（与布局解耦）、`_CdpSession`、指标卡/表格双路、8 词硬顶、transport 注入
- 需改造/前置：
  1. **运行前置**：刘全环境必须有调试 Chrome（9222）+ playwright + 持久登录 profile（首次人工登录一次）——这是进程式外部依赖，**刘全的"HTTP 读接口"机制无法替代它**（无 API 端点），只能作为引擎侧连接器保留
  2. 页面结构耦合：指标卡正则较稳（标签即分隔符），但表格列序是像素钉死的，建议留回归测试；eHunt 改版需人工复核
  3. Chrome 生命周期管理：广成靠常驻手动启动的 Chrome，刘全若上服务器建议 systemd 守护或按需拉起/探活

---

## 3. 【keyword-research 工序】`skills/keyword-research/run.py`（120 行）

### 输入契约
- `task` 文本为 JSON：`{"keywords": [...], "page_size": 10}`（`target_keywords` 同义兼容）；非 JSON 时整段视为**单个关键词**兜底
- 用户数据优先取 seed artifact（`engine.fetch(seed_id)` 的 `text`），退回 `task` 文本

### 处理流程（编排+聚合，无 LLM、无直连 HTTP、不碰浏览器）
1. `engine.invoke_connector("ehunt", task="search items", input_data={keywords, page_size})` → 竞品画像（失败 → 工序 failed）
2. `engine.invoke_connector("ehunt-keyword", task="read keyword metrics", input_data={keywords})` → CDP 逐词指标（**失败不阻塞**，记 `metrics_note`，source 降级为 `ehunt-api`）
3. 聚合：把 CDP 指标并进 `keywords[词]` 的 `metrics`（剥掉 related）+ `related_keywords`；合并成功 source=`ehunt-api+ehunt-keyword-cdp`

### 输出契约
- 交付物 `keyword-data`：`{keyword_data: {source, keywords: {词: {product_num, avg_price_top, top_competitors[], metrics?, related_keywords?}}, quota, metrics_note?}}`
- 两步链场景回显 `payload`（product_text/target_keywords/image_path）供下游 seo-optimize 取回输入（seed 只绑第一步）

### 8 词硬顶 & 配额记账点
- **硬顶在连接器层**：`ehunt` 与 `ehunt-keyword` 两个连接器内部都 `keywords[:8]`（工序本身传全量）
- 记账点 = **eHunt API 响应体 `quota.used_today/remaining_today`**（服务端计数回传），工序透传进交付物；无本地账本。CDP 关键词工具另有每日搜索次数限制（Free 有限 / Elite 无限），无计数器暴露

### 可复用程度：**直接照搬**
- 纯编排无业务耦合；只依赖两个 connector 的 `{ok, keywords, quota}` 契约
- 平移改动：`engine.invoke_connector` → 刘全 connector 层（HTTP 读接口）；黑板 artifact → 刘全契约（Event/Context 或任务 payload 传 `prev`）；8 词硬顶与 `page_size` 上限保留

---

## 4. 【seo-optimize 工序】`skills/seo-optimize/run.py`（440 行）+ `playbooks.py`（308 行）

### 三段流水线（一上下文不拆）
1. **商品理解**：拼 brief（`CURRENT TITLE/TAGS/DESCRIPTION` + `SELLER TARGET KEYWORDS`）；图可选——有 `image_path` 且 `is_model_available("vision")` 时调 Vision Analyst 补全（key_elements/niche/视觉描述），vision 不可用则纯文字并注明
2. **关键词策略**：SEO Strategist 出 6-10 个高意图关键词（1-2 头词 + 4-6 长尾 + 1-2 问题/结果词）；**有上游真实关键词数据（keyword_data）时作为 `REAL KEYWORD DATA` 附录注入，要求"以真实数据为准"**；数据缺失则靠模型推断，不阻塞
3. **文案生成**：Copywriter 出 3-5 个标题候选（含 angle 标签）+ 恰好 13 标签（每个 ≤20 字符）+ 描述重写 + materials + alt_text + suggested_category（从 playbook 的 `etsy_category_hints` 里选，禁止编造类目路径）

### prompt 结构
- 3 个内联 system prompt（Vision Analyst / SEO Strategist / Copywriter），全部带 **OUTPUT CONTRACT**："Output ONLY a single valid JSON object. No markdown, no code fences"
- Copywriter 的 user prompt = `SEED_KEYWORDS 块 + PRODUCT BRIEF + playbook.render_guidance()`
- SEO 段 user prompt = `PRODUCT BRIEF + REAL KEYWORD DATA 附录（json.dumps 全量）`

### playbooks 资产（`playbooks.py`）
- 复用自 Contentsy（MIT），改造条：剥 taxonomy API，category hints 本地缓存
- 7 本：`wall_art / digital / jewelry / clothing / home_candle / personalized / general`，dataclass 字段：`key, label, title_formula, example_title, description_sections[], tag_mix, key_attributes[], rules[], etsy_category_hints[]`
- `match_playbook(niche, aesthetic_style, key_elements)`：按 `_MATCH_ORDER` 关键词命中（digital→wall_art→jewelry→clothing→personalized→home_candle→general，具体在前）；`get_playbook(key)` 按 playbook_key 直取
- `render_guidance()` 生成注入 Copywriter 的紧凑指令块
- `ETSY_TOP_LEVELS`：15 个真实 Etsy 顶级类目常量

### JSON 输出自修复重试
- `_invoke_json(engine, capability, user, system, image_path)`：
  - 首次 `invoke_model(json_mode=True)`（text）或 vision 通道返回文本时手动 `json.loads`（剥 ```` ```json ```` 围栏）
  - 非 dict → 追加修复提示 `"IMPORTANT CORRECTION: Your previous reply was not valid. Return ONLY a single JSON object..."` 重试**一次**
  - 仍失败 → `None`，工序 failed（各段有独立 checks：`seo-stage-invalid` / `copy-stage-invalid` 等）

### 归一化规则（纯 Python，代码硬约束，不靠模型自觉）
- 标签 `_normalize_tags`：去 `#`、空白折叠、截断 20 字符、大小写不敏感去重、**上限 13**
- 标题 `_normalize_titles`：`_clamp` 截到 **140 字符**（按词截断避免切字）、**上限 5 个**
- materials `[:13]`；alt_text `_clamp(250)`；seo_keywords `[:10]`

### LLM 调用方式（invoke_model 接口）
```python
engine.invoke_model(capability, user_prompt, system_prompt=..., json_mode=bool, image_path=Optional[str])
```
- `text` 能力 → `chat_json`（json_mode=True → `response_format={"type": "json_object"}`；json_mode 失败自动降级普通文本重试一次，HTTP 错误除外）
- `vision` 能力 + image_path → `chat_vision`（多模态）；模型来自 `models.yaml`（`model_for_capability(ref)` 查能力→实例）
- `is_model_available("text")` 门禁：无 API Key → 工序 `skipped` 不报错
- 广成模型现状：`text-deepseek`（deepseek-chat，active），**vision 是占位 inactive**（DeepSeek 无视觉模型）→ seo-optimize 实际运行是纯文字路径

### 数据流
- 关键词数据来源优先级：payload 内联 `keyword_data` > 上游 artifact `inputs["prev"]`（`engine.fetch`）；两步链无 seed 时从上游交付物回读回显 `payload` 兜底
- 输出 `seo-optimization-report`：`{original{title,tags,description}, titles[{title,angle}]×3-5, tags≤13, listing_description, materials, alt_text, suggested_category, seo_keywords[6-10], search_intent, keyword_data_appendix, playbook, note, business_layer: True}`；note 明示"不保证排名、不写入 ETSY 手动粘贴回"

### 可复用程度：**直接照搬（prompts/playbooks/归一化），适配点集中在 LLM 网关**
- prompts、playbooks、归一化、retry 语义都是纯 stdlib 自包含 → 直接搬
- 适配改造：
  1. `engine.invoke_model` → 刘全 LLM 网关；刘全的 PydanticAI `output_type` 契约 + 校验失败 re-ask 可**替代** `json_mode + 自修复重试`（更符合刘全"AI 输出必过 Pydantic 校验"铁律），也可先原样搬 `_invoke_json` 再演进
  2. vision 段：刘全需配视觉模型（DeepSeek 无视觉），或 v0.5 先砍 vision 只做文字
  3. `business_layer: True` 语义保留（刘全业务层只输出、不写 Etsy）

---

## 5. 【image-download 工序 + xhs/xianyu 连接器】

### 5.1 工序 `skills/image-download/run.py`（106 行）——纯原子路由
- 输入：`task` 含商品分享链接；正则识别平台：
  - 小红书：`xiaohongshu.com/(discovery/item|explore|item)/<hex>?query`（**query 原样保留，xsec_token 不能丢**）
  - 闲鱼：`(www|h5|m).goofish.com/item(\.htm)??query`
- 路由：xhs → `xhs-download` 连接器；xianyu → `xianyu-download` 连接器；都不中 → failed（提示支持范围）
- 输出 `etsy-image-pack` 交付物：`{type, business_layer: True, paths[], count, desc, tags[], author_id, day_dir, source(xhs|xianyu), url}`
- 下载/落盘/读 db 全在连接器内部；工序不 subprocess、不 import SDK、不碰 IO（符合广成开发规范 §4/§7）

### 5.2 xhs 连接器 `runtime/xhs_connector.py`（217 行）
- **vendor 位置**：`studio/vendor/XHS-Downloader`（本地开源库，2026-08-25 从平级搬到 studio 内部）；入口 `source.XHS`
- **调用方式**：`cd vendor && uv run python -c '<脚本>'` 子进程（库依赖与广成 .venv 隔离，防 curl-cffi/httpx 污染平台环境）；`PATH` 前插 `~/.local/bin`（uv 所在）；**unset 全部代理**（实测小红书直连可达，代理反而干扰）；超时 180s
- 库参数：`work_path=out_dir, folder_name='', folder_mode=True, image_download=True, video_download=False, live_download=False, record_data=True, download_record=False, image_format='JPEG'`
  - `download_record=False` 关键：禁用库全局 `ExploreID.db` 去重（否则已下载过的作品会跳过且不落盘当前 work_path → 误判失败）；业务去重改由上层 manifest 做
- **落盘目录**：env `XHS_DOWNLOAD_DIR`，默认 `/home/winhous/picture/Etsy-Picture/XHS`；实际图在 `<root>/Download/<作品名>/`（库按作品名分文件夹）
- **新增计数**：扒前/扒后对 `<root>/Download/**` 做图片后缀（jpeg/jpg/png/webp）差集，只数本次新增，防历史图混入
- **元数据读取**：`sqlite3` 读 `<root>/Download/ExploreData.db`，表 `explore_data`，按 URL 作品 ID 匹配 `作品ID` 列，取 `作品描述/作品标签/作者ID`；标签是空格分隔串拆成 list
- **失败处理**：vendor 缺失/下载失败/未落盘 → `ok=False` 阻塞（提示 xsec_token 可能过期）；db 缺失/无匹配行/读错 → 元数据降级空值 + `note`（`db-missing`/`no-matching-row` 等），图已落盘不让整体失败
- 图文帖无需登录，xsec_token 鉴权，非收费 API

### 5.3 闲鱼连接器 `runtime/xianyu_connector.py`（489 行）
- **playwright 用法**：`sync_playwright()` → `chromium.launch(headless=True)` → UA 伪装（Chrome/120）+ viewport 1280x1800 → `goto(networkidle, 30s)`（超时降级 domcontentloaded）→ 等 3s（React 渲染）→ 滚轮触发懒加载（4×1500px + 回顶）
- **抓图策略**：主图轮播选择器（10 套 class 变体）→ 详情图选择器（6 套）→ 兜底全页 JS 收集（naturalWidth/Height ≥100）；URL 过滤（avatar/icon/logo/sprite/placeholder/qrcode 等）；`data:` 跳过
- **下载**：`page.request.get(img_url, 20s)`，文件字节 ≥15KB 才落盘（实测推荐位缩略图 5-6KB）；命名 `<base>/<item_id>/01.jpg 02.jpg...`；单图失败跳过；整体 90s 硬截止
- **落盘目录**：env `XIANYU_DOWNLOAD_DIR`，默认 `/home/winhous/picture/Etsy-Picture/闲鱼/<item_id>/`
- **元数据（尽力而为）**：标题 = `page.title()` 去"闲鱼"后缀 → desc 容器首行 → h1；卖家 ID = `window.__INITIAL_STATE__/__PRELOADED_STATE__` 里找 sellerId/userId → DOM `a[href*=userId]` 等；tags 恒空（闲鱼无标签概念）
- **失效页检测**：扫 body.innerText 命中「宝贝被删掉/已下架/已删除/宝贝不存在/宝贝已不存在」→ **删除已下载垃圾图** + `dead-page` 标记
- **失败处理**：playwright 未装（顶部 try import 守卫）→ 结构化失败提示安装命令；页面打不开/超时/未抓到图 → `ok=False`；元数据读不到 → 空值 + note，不阻塞
- 不登录扒公开页，反爬靠无头 + 限速（一次一商品），非收费 API；status=experimental（页面改版选择器会失效）

### 可复用程度：**直接照搬（工序骨架 + 两连接器），适配点是目录/依赖落地**
- 工序路由逻辑、xhs 的 ExploreData.db 读取（纯 sqlite3 stdlib）、闲鱼的 playwright 扒页逻辑都自包含 → 直接搬
- 适配改造：
  1. XHS-Downloader vendor 整目录搬进刘全（或 git submodule / uv 项目依赖）；`uv run` 模式保留（库依赖隔离）
  2. 落盘目录改刘全配置（env），默认路径不要写死广成的 `/home/winhous/picture/Etsy-Picture/...`
  3. 闲鱼选择器与页面结构强耦合 → 留回归测试与人工核对预案
  4. 刘全的「HTTP 读接口」：image-download 更适合做成**引擎侧连接器（进程内/网关）**，HTTP 读接口可暴露为"查询已落盘结果"而不是把下载也 HTTP 化（下载涉及长耗时 + 本地 IO + vendor 进程，HTTP 化收益低）

---

## 6. 【ehunt-chrome.sh】`scripts/ehunt-chrome.sh`（37 行）

- 启动：`google-chrome --remote-debugging-port=9222 --user-data-dir=~/.local/share/ehunt-chrome-profile --no-first-run --no-default-browser-check`，后台运行，日志 `/tmp/ehunt-chrome.log`
- 端口：**9222**（CDP 调试端口）；profile 持久登录态（首次需人工登录 ehunt.ai 一次）
- 幂等：先 `curl http://127.0.0.1:9222/json/version` 探活，已运行直接退出；启动后轮询 ≤10s 确认
- 配额注记：eHunt Free 关键词搜索每日有限次，升级 Elite 无限（脚本注释）
- 备注：脚本提示"账号见 memory/ehunt-data-source.md"，该文件在 studio 中已不存在（memory/ 下只有 .gitkeep）——登录账号信息需要刘全自己维护

---

## 7. 【依赖清单】与【本机现状核验】（只读核验，未安装任何东西）

| 资产 | 第三方依赖 / vendor / 外部服务 | 本机现状（2026-09 实查） |
|---|---|---|
| ehunt API 连接器 | 外部服务 api.ehunt.ai；`EHUNT_API_KEY`；urllib（零 pip 依赖） | ✅ key 已在 studio/.env（`vip_95…`）；API 可达（llms.txt/items.md 实测可拉） |
| ehunt CDP 连接器 | playwright（pip）+ chromium 浏览器 + 调试 Chrome + 登录 profile | ✅ playwright 1.62.0 在 studio/.venv；✅ ms-playwright 浏览器缓存 chromium-1234 / chromium_headless_shell-1234 / ffmpeg-1011；✅ /usr/bin/google-chrome(-stable)；✅ profile 目录已存在；⚠️ **9222 当前未监听**（需跑 ehunt-chrome.sh 或人工起 Chrome） |
| XHS-Downloader | vendor/XHS-Downloader（uv 管理：aiofiles/aiosqlite/click/emoji/fastapi/fastmcp/httpx[http2,socks]/lxml/pyperclip/pyyaml/textual/uvicorn/websockets 等，pyproject+uv.lock）；`uv`；XHS_DOWNLOAD_DIR | ✅ vendor 完整（source/ 存在）；✅ uv 在 ~/.local/bin/uv；✅ 默认落盘目录 /home/winhous/picture/Etsy-Picture/XHS 已存在 |
| xianyu 连接器 | playwright + chromium | ✅ 同上（studio/.venv + 浏览器缓存）；⚠️ 系统 python3 无 playwright（依赖在 studio venv 内）；✅ 落盘目录 /home/winhous/picture/Etsy-Picture/闲鱼 已存在；vendor/goofish-mcp-server 存在但**当前连接器未用**（遗留） |
| keyword-research / seo-optimize | 纯 stdlib（re/json）；LLM 走广成 llm_client（DeepSeek：JOJOCODE_API_KEY，base api.deepseek.com/v1，model deepseek-chat；vision 占位 inactive） | ✅ DeepSeek key 在 studio/.env；刘全侧已有 DeepSeek API（本机）+ PG16 |
| ehunt-chrome.sh | google-chrome | ✅ 已装 |

**刘全侧需要补齐的运行时依赖**（写进 v0.5 详设的部署节）：
1. playwright + chromium（刘全环境，可复用广成同款 1.62.0 版本）
2. 调试 Chrome 常驻方案（本机/服务器 + 9222 + 登录 profile；三机红线注意：eHunt 是第三方数据源非 Etsy 卖家 API，CDP Chrome 去的是 ehunt.ai，不构成"本机直连 Etsy"）
3. XHS-Downloader vendor 目录（git 子模块或 vendor 目录）
4. `EHUNT_API_KEY` / `XHS_DOWNLOAD_DIR` / `XIANYU_DOWNLOAD_DIR` 进刘全 .env

---

## 8. 【平移建议】对刘全 v0.5 逐条

| # | 资产 | 建议 | 说明/怎么改 |
|---|---|---|---|
| 1 | ehunt_connector.py（API 通道） | **直接照搬** | 逻辑零依赖自包含；刘全 connector 层注册为外部连接器（或纯函数 + HTTP 端点包装）；key 进 .env；保留 8 词/page_size 上限与 quota 透传 |
| 2 | ehunt_keyword_connector.py（CDP 通道） | **适配改造** | 核心（正则解析/CDP 会话/表格兜底/8 词）直接搬；必须落地运行前置：9222 调试 Chrome + playwright + 登录 profile（可 systemd 守护或按需拉起）；**HTTP 读接口机制无法替代**（无 API 端点），只能做引擎侧连接器；表格列序/指标卡字段留回归测试，eHunt 改版需人工复核 |
| 3 | keyword-research/run.py | **直接照搬** | 无 LLM 纯编排；invoke_connector → 刘全 connector 层；黑板 artifact → 刘全契约；8 词硬顶与 quota 记账（服务端回传 used/remaining）保留 |
| 4 | seo-optimize/run.py + playbooks.py | **直接照搬**（prompts/playbooks/归一化/重试语义），LLM 网关适配 | invoke_model → 刘全 LLM 网关（建议用 PydanticAI output_type + re-ask 替代 json_mode+自修复重试，符合铁律 6）；vision 段需视觉模型或 v0.5 先砍；`business_layer: True` 语义保留；归一化（20 字符/140 字符/13 上限）是代码硬约束，原样搬 |
| 5 | image-download/run.py + xhs/xianyu 连接器 | **直接照搬**（工序骨架 + 两连接器） | vendor 整目录搬入刘全；落盘目录改 env 配置；闲鱼选择器留回归预案；下载保持引擎侧连接器（HTTP 读接口只做结果查询）；ExploreData.db 读取（sqlite3 stdlib）原样搬 |
| 6 | ehunt-chrome.sh | **适配改造** | 启动参数/端口/profile 原样；刘全落地为 systemd 服务或脚本 + 探活；登录账号信息刘全自维护（广成 memory 已无该文档） |
| 7 | 机制对接（invoke_connector vs HTTP 读接口） | 逻辑平移 + 一层包装 | 广成 connector 的 `run(task, prev)->dict{ok,...}` 契约保留为刘全 connector 层内部接口；对外暴露 HTTP 读接口时透传 ok/note/quota；connectors.yaml 的 env_keys/status 映射到刘全注册表；available 语义 → HTTP health 检查 |

**风险提示**（供详设评估）：
1. eHunt 页面（keyword-tool 指标卡/表格、goofish 商品页）均与前端结构耦合，改版即失效 → 需要回归测试 + 人工核对兜底
2. eHunt API 配额 200 条/天是硬上限 → v0.5 需要配额记账 UI（读取回传 quota）与用量预警
3. 扒图链路（XHS xsec_token 时效、闲鱼反爬）是"尽力而为"性质 → 失败必须可人工兜底（任务单回流），符合刘全"人做完结果回流"循环
4. 三机架构红线：eHunt 是第三方数据源，CDP Chrome 访问的是 ehunt.ai 而非 Etsy 平台，安全上可接受；但 XHS/闲鱼扒图在本机执行，属外部公开页抓取，需按刘全安全规范评估

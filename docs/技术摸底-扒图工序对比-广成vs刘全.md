# 技术摸底：扒图工序对比——广成原版 vs 刘全 v0.5

> 调研性质：只读对比（2026 摸底，v0.5 扒图工序重做前）
> 对比对象：广成原版（`/home/winhous/project/studio`，只读）vs 刘全 v0.5（`/home/winhous/project/liuquan`）
> 结论一句话：**刘全 v0.5 扒图是「广成能力的骨架化照搬 + 下游升级」，但 xhs 子进程调用与元数据读取是坏的（必然失败/必然空），闲鱼选择器大幅简化，且「下载→落库→体检→选品」链在代码层面存在多处断点（无落库调用方、biz_client 未注入、链 input 契约不匹配、A47/A48 验收未实现）。重做时应把广成的 xhs 调用方式、闲鱼兜底细节原样搬回，再补链的落库/上下文装配。**

---

## 1. 【能力覆盖】广成 run.py vs 刘全实现

### 1.1 广成原版（studio/skills/image-download/）

**SKILL.md（30 行）能力声明：**
- 输入：`user_task` 含商品分享链接（小红书 `xiaohongshu.com` 带 `xsec_token`；闲鱼 `goofish.com` 的 www/h5/m 子域，`item?id=xxx` 或 `item.htm?id=xxx`）
- 输出：`etsy-image-pack` = `{ paths:[图片绝对路径], count, desc, tags:[标签], author_id, day_dir, source, url }`，`source` ∈ {xhs, xianyu}
- 平台路由：小红书 → `xhs-download` 外联；闲鱼 → `xianyu-download` 外联；未识别 → 报错阻塞「当前支持小红书/闲鱼」

**run.py（106 行）实际行为：**
- URL 提取正则：
  - xhs：`xiaohongshu.com/(discovery/item|explore|item)/[A-Za-z0-9]+(?:\?...)?`（保留 query 含 xsec_token）
  - 闲鱼：`(www.|h5.|m.)?goofish.com/item(?:.htm)?(?:\?...)?`
- 路由 → `engine.invoke_connector("xhs-download"|"xianyu-download", task, {"url": url})`
- 外联失败 → `failed` + checks `[平台:download-failed]`；成功 → `completed` + deliverable `etsy-image-pack` + checks `[平台:downloaded:N]`（有 note 时加 `[平台:meta:...]`）
- **不支持视频**：xhs_connector 显式 `video_download=False`；闲鱼只扒图。XHS-Downloader 库本身具备视频能力（`video_download=True` 参数存在），但广成主动关闭（图文帖场景）
- **元数据字段**：desc / tags / author_id / day_dir / source / url；**无价格字段**（闲鱼页面未扒价格，SKILL 未声明）

### 1.2 刘全 v0.5（liuquan）

| 维度 | 广成 | 刘全 v0.5 | 差距 |
|---|---|---|---|
| 平台 | 小红书 + 闲鱼 | 小红书 + 闲鱼 + **通用 http 图片**（http_image connector，新增） | 多一个（CRM 对话图用） |
| 路由 | 域名正则 → connector_id | `_detect_source`（xiaohongshu/xhslink→xhs、goofish/2.taobao→xianyu、其他→crm） | 基本对齐，多 xhslink/2.taobao 别名 |
| 视频 | 不支持（显式 video_download=False） | 不支持（子进程调用本身是坏的，更无视频参数） | 对齐（都不支持） |
| 输出 | etsy-image-pack（dict） | ImagePack Pydantic 模型（同字段 + note） | 契约升级（Pydantic 校验） |
| 元数据字段 | desc/tags/author_id/day_dir/source/url | 同 + note；落库后 image_file 再补 width/height/watermark/status | 下游多体检字段 |
| 路径命名 | xhs：`XHS_DOWNLOAD_DIR/Download/<作品名>/`；闲鱼：`XIANYU_DOWNLOAD_DIR/<商品id>/01.jpg...` | xhs：`{storage_dir}/xhs/Download/<作品名>/`；闲鱼：`{storage_dir}/xianyu/<商品id>/01.jpg...` | 命名一致，根目录换 SCRAPE_STORAGE_DIR（默认 `/opt/liuquan/scrape/`） |
| day_dir | xhs=str(out_dir)，闲鱼=根目录 | xhs 恒 `""`（_read_metadata 恒返回空），闲鱼 `""` | **缩水**：刘全丢 day_dir 语义 |
| 多链接批量 | 单任务单链接（task 文本提取一个） | 页面支持每行一个、多行多链接（urls 逗号分隔） | 刘全页面更强，但链 input 契约未跟上（见 §4.2） |
| 定时触发 | 无（工序本身） | 页面只有「立即」（startScrapeTask）；定时未接（通用 v0.4 定时链机制存在但 scrape_suggest_chain 无种子配置） | 详设/页面宣称「立即/定时」，定时实际未落地 |

**小结（能力覆盖）**：刘全在「平台数、批量贴链接、Pydantic 契约、体检+选品下游」上比广成多；在「day_dir、元数据兜底标注」上比广成少；视频两边都不支持。

---

## 2. 【连接器实现质量】

### 2.1 xhs：XHS-Downloader 子进程调用

| 细节 | 广成（runtime/xhs_connector.py，217 行） | 刘全（engine/connectors/xhs.py，203 行） | 判定 |
|---|---|---|---|
| 调用方式 | `uv run python -c "from source import XHS; async with XHS(work_path=..., folder_name='', folder_mode=True, image_download=True, video_download=False, live_download=False, record_data=True, download_record=False, image_format='JPEG') as xhs: await xhs.extract(url, download=True)"`，cwd=vendor | `uv run python -c "sys.path.insert(0, vendor); from main import download; download(urls=['url'], folder_name='', folder_mode=True, download_record=False, save_path=...)"`，cwd=vendor | **坏**：vendor `main.py` **没有 `download` 函数**（只有 app/api_server/mcp_server/cli），`from main import download` 必然 ImportError → 子进程 rc≠0 → 下载必失败。且参数 `save_path` 不是 XHS 的构造参数（广成用 `work_path`） |
| folder_name/folder_mode | `folder_name=''` + `folder_mode=True`（每帖独立子文件夹） | 同 | 对齐 |
| download_record | `download_record=False`（禁全局 ExploreID.db 去重，业务去重交上层 manifest） | 同 | 对齐 |
| 差集计数 | **有**：扒前 rglob 全量 → 扒后 rglob 全量 → `after - before` 只数本次新增（避免历史图误计） | **无**：`_find_downloaded_files` 全目录 rglob 按 mtime 倒序取全部 → 二次下载会把历史文件算进 paths/count | **缺**：需补差集 |
| ExploreData.db 读取 | `SELECT "作品描述","作品标签","作者ID" FROM explore_data WHERE "作品ID" = ?`（表 `explore_data`、中文列名，与库落库结构一致——recorder.py DATA_TABLE 实锤） | `SELECT note_id, desc, tags, author_id FROM note_data WHERE note_id = ?` | **坏**：库实际落 `explore_data` 表、列名是中文（作品ID/作品描述/作品标签/作者ID），刘全表名/列名全错 → 元数据必然读不到（静默降级空） |
| 未落盘检查 | 有：paths 空 → fail「未落盘任何图片（链接可能失效或 xsec_token 过期…）」 | **无**：`_find_downloaded_files` 空仍返回 ok=True、count=0 | **缺**：需补未落盘失败 |
| xsec_token 过期提示 | 未落盘 note 明示「xsec_token 过期，请重新取最新分享链接」 | 无（页面 FAQ 有「token 约 30 天」提示，但工序/连接器层无） | 缺：连接器层补 |
| 代理处理 | 显式 pop http_proxy/https_proxy/HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/all_proxy | 遍历 env 删所有含 "proxy" 的 key | 对齐（刘全更宽） |
| 超时 | 180s | 120s | 数值差异，可接受 |
| vendor 缺失 | 检查 `vendor/source/__init__.py` 存在 | `available` 只查 `vendor_dir.is_dir()`（不查 source/__init__.py 完整性） | 略弱 |
| 测试 | 实跑验证（注释明示） | 本批 fake connector 注入（零网络），**未要求真实 XHS-Downloader 可用** | 真实路径未验证 |

### 2.2 xianyu：playwright 选择器

| 细节 | 广成（runtime/xianyu_connector.py，489 行） | 刘全（engine/connectors/xianyu.py，232 行） | 判定 |
|---|---|---|---|
| 主图选择器 | 11 套 class 模糊匹配（swiper-slide/carousel/gallery/mainImg/main-image/imageMain/picMain/imageBox/slider/slide） | 3 套 class 精确（`img.main-image` / `.item-img img` / `.item-main img`） | **大幅简化**：刘全选择器是自创的，与广成完全不同的列表，真实页面命中率存疑 |
| 详情图选择器 | 6 套（itemDesc/desc/detail/content/ImageText/imageText） | 3 套（.item-detail/.detail-image/.desc-image）+ 全页 img[src*=img.alicdn.com]/img[src*=goofish] | 简化 |
| 全页兜底 | `_harvest_all_imgs`：JS 一次性收集 naturalWidth/Height≥100 的大图 | 无 naturalWidth 过滤（仅靠 URL 前缀 + ≥15KB 文件过滤） | **缺尺寸过滤** |
| URL 过滤关键词 | avatar/icon/logo/sprite/placeholder/loading/blank/default/qrcode 等 9 类 + data: 排除 | 无（只查 src 以 http 开头） | **缺**：会抓进图标/占位图 |
| 主图命中即停 | 有（命中主图选择器即 break，避免重复） | 无（三路顺序全部跑，去重靠 dict） | 略 |
| ≥15KB 过滤 | 有（_MIN_IMG_BYTES=15_000） | 有（15_000） | 对齐 |
| 90s 截止 | **全局 deadline**：`time.monotonic()+90` 下载循环内逐张检查截止 | `page.set_default_timeout(90_000)`：是单操作超时，**不是整体截止** | 语义不同，刘全弱 |
| networkidle | 先 networkidle（React 渲染完）失败降级 domcontentloaded | 直接用 domcontentloaded | 刘全可能抓早（懒加载图未渲染） |
| 懒加载滚动 | 4×wheel(1500) + 回顶 | 5×wheel(800) | 对齐（数值略异） |
| 失效页检测 | `_DEAD_PAGE_KEYWORDS`（宝贝被删掉/已下架/已删除/宝贝不存在/宝贝已不存在）扫全文 → **删已下载垃圾图** + dead-page 标记 | 无关键词检测，仅 paths 空时 `_cleanup_empty_dir`（删空目录） | **缺**：不会主动删已落盘的垃圾图 |
| 标题提取 | page.title() 去「_闲鱼/-闲鱼」后缀 → desc 容器 → h1 三路（实测 [class*=title] 是「为你推荐」不可用） | h1/.item-title/.title 一路 | 简化且踩广成踩过的坑（title 类选择器不可靠） |
| 卖家 ID | window.__INITIAL_STATE__/__PRELOADED_STATE__ JS 全局状态 + DOM seller 链接两路 | .seller-name/.user-name 一路 | 简化 |
| UA 伪装 | Chrome/120 UA + viewport 1280x1800 | Chrome/131 UA，无 viewport | 对齐（viewport 略） |
| 单图下载超时 | 20s/张 | 走默认 | 略弱 |

**小结（连接器质量）**：xhs 照搬是「坏的照搬」——调用入口和 SQL 表/列名两处硬伤，真实跑必然下载失败 + 元数据恒空；闲鱼是「大幅简化」——三路兜底只剩骨架，过滤/尺寸/失效页/全局截止/标题卖家多路兜底全部缩水。**这两块是重做时必须从广成原样搬回的核心。**

---

## 3. 【错误与降级】

| 场景 | 广成 | 刘全 v0.5 | 判定 |
|---|---|---|---|
| 无链接 | run.py failed + checks `no-supported-url` | worker 返回 ImagePack(note=connector 未注入/不可用)；页面 422 于链 input 校验 | 广成更清晰（刘全见 §4.2 契约问题） |
| vendor 缺失 | ok=False note「XHS-Downloader vendor 不存在或不完整」 | available=False note「vendor 未配置（目录不存在）」 | 对齐（完整度检查略弱） |
| 下载失败 | ok=False note（rc/stderr 前 500 字） | ok=False note（rc/stderr 前 200 字） | 对齐 |
| 未落盘 | ok=False「未落盘任何图片…xsec_token 过期」 | **不检查**：ok=True count=0 | **缺** |
| playwright 未装 | ok=False「先 pip install playwright && python -m playwright install chromium」 | available=False「playwright 未配置（chromium 未安装）」 | 对齐 |
| xsec_token 过期 | 未落盘 note 提示重取最新分享链接 | 无连接器层提示（仅页面 FAQ） | **缺** |
| 元数据降级 | 图已落盘则降级：note 记 db-missing/no-matching-row/no-title/no-seller-id → 上层 checks 标注，不让整体失败 | xhs：`_read_metadata` 全 except pass 返回空，**note 无降级标注**（固定「小红书下载完成」）；闲鱼：note 固定「闲鱼下载完成」 | **缺降级标注**：上层无法区分「真读到元数据」vs「降级空」 |
| 页面渲染失败 | n/a（无页面） | failed 图片有红标/处理中有黄标（模板） | 刘全多 |
| 单图下载失败 | 跳过继续 | 跳过继续 | 对齐 |

---

## 4. 【与业务链的衔接】

### 4.1 广成：etsy-image-pack 的下游

- grep `skills/`：**唯一消费者是 `feishu-write`**（skills/feishu-write/run.py + SKILL.md）
- 消费方式：链式 `inputs["prev"]` 取上游 etsy-image-pack → `_build_fields` 组装飞书多维表格一行（链接/描述/标签(多选)/作者ID/数量(数字)/状态(单选)/处理时间(日期)）→ `invoke_connector("lark-cli", append_record)` 写回
- 设计特点：**扒图产物 → 人工运营表格记录**，下游无 AI 加工；元数据兜底是「字段缺失则不写该字段」；缺上游时只写链接+状态（人工补录场景）

### 4.2 刘全 v0.5：ImagePack → scrape.image_file → 体检 → 选品 → 提案

设计链（详设-v0.5 §6.2）：`scrape_suggest_chain`：image_download → image_inspect → product_suggestion → 链完成消费者 `scrape.suggest`（engine/actions/scrape_proposal.py → TaskProposal → POST /api/biz/tm/proposals → tm.task_proposal pending → 审核页）。

**代码层面断点（重做重点，按严重度）：**

1. **链 input 契约不匹配（页面必 422）**：chain.yaml 声明 `input.model: SuggestionInput{image_ids[]}`，但 web `/scrape/run`（web/app.py:470）传 `{"urls": urls, "source": "mixed"}` → engine `create_task` 用 `SuggestionInput.model_validate`（engine/core/runner.py:271 / server.py:832）→ 缺 image_ids → ValidationError → 422。**页面「开始扒图」当前提交即失败**（除非另有适配，未发现）。
2. **image_download 步骤 input 缺 url/batch_id**：worker 输入是 `ImageDownloadInput{url, batch_id}`，但链步骤无 input 表达式（chain.yaml steps 无 input 字段）→ `_resolve_step_input` 直传 task_input `{urls, source}` → INIT 相位 ImageDownloadInput 校验失败（缺 url/batch_id）。**单链接 vs 多链接（urls 逗号分隔）也无人拆分**。
3. **image_file 无落库调用方**：详设 §6.1 写「产物 → 写接口 POST /api/biz/scrape/images 落 scrape.image_file」，但全仓 grep：**没有任何代码调用该写接口**（api_biz.py 只定义端点；image_download worker 只返回 ImagePack 不落库；web /scrape/run 只 create_task 不落库）。A47 验收要求「scrape.image_file 落库」但 test_acceptance_v05.py **没有 test_a47**。
4. **image_inspect 的 biz_client 不存在**：`EngineContext`（engine/core/context.py，frozen dataclass）字段只有 worker_id/domain/inputs/config/context_data/engine/llm_output/task_id/step_id/chain_id/connectors——**没有 biz_client**。image_inspect/run.py:73 `ctx.biz_client if hasattr(...)` 恒为 None → 全部图片「无法获取图片路径」。**体检工序实际不可用**。
5. **product_suggestion 无 LLM 调用**：worker.yaml `reason: llm`，但 run.py **纯模板拼装**（从 ctx.context_data["scrape_image_context"] 拿元数据拼 title/detail），无任何 LLM 调用、无 vision 识图（详设明确「model: default 文本模型，vision 识图留给批 5 crm image_caption」）。选品建议质量 = 元数据复述。
6. **页面 JS 端点曾不一致**：index.html 早期版本 JS 调 `/api/tasks/create`（不存在），当前版本已改 `/scrape/run` + `/scrape/suggest`（与 app.py 一致）——说明页面刚修过、处于开发中态（git 有未提交改动）。

**对比结论（下游设计）**：广成下游是「人工表格记录」（产物止于 etsy-image-pack → 飞书）；刘全下游是「AI 体检 + 选品提案 → 任务中心审核」（更完整的产品闭环，方向正确），但**当前代码里这条链没有真正跑通**（上述 6 个断点，A47/A48 验收未实现/未通过）。

---

## 5. 【重点问题回答】

1. **xhs 小红书是否支持视频？** 两边都不支持。广成显式 `video_download=False`（XHS-Downloader 库具备视频能力但被关，场景定位图文帖）；刘全子进程调用本身是坏的（`main.download` 不存在），更无视频参数。重做时维持图文即可；若未来要视频，XHS 库参数现成。
2. **xianyu 是否支持多图/详情图？** 广成支持（主图轮播 11 套 + 详情 6 套 + 全页兜底，≥15KB + naturalWidth 过滤 + 失效页删图）；刘全名义有三路但选择器自创简化版，无 URL 关键词过滤、无尺寸过滤、无失效页删图——**多图/详情图能力实际未照搬完整**。
3. **元数据完整度（卖家/价格/标题）？** 广成：xhs 有 desc/tags/author_id；闲鱼有 title(seller_id)，**无价格**（页面未扒价格，SKILL 未声明）；刘全：xhs 元数据 SQL 表/列名错误必然空；闲鱼 title/seller_id 选择器简化可能抓不到；**价格两边都没有**。若重做要「卖家/价格」需新增闲鱼价格选择器（广成没有可抄）。

---

## 6. 【结论建议】逐条

### A. 完全保留（刘全比广成好的部分）

1. **下游设计（体检+选品→提案审核）**：方向正确、有产品价值，保留并修通。
2. **ImagePack/SuggestionInput 等 Pydantic 契约 + worker.yaml 声明式注册**：比广成 dict 交付物更可控（AI 100% 可控原则），保留。
3. **http_image 通用图片连接器 + 域名别名（xhslink/2.taobao）**：广成没有，是刘全增量，保留。
4. **页面多链接批量贴 + 来源徽章 + 缩略图 + 批量选品建议 + 体检/水印展示**：交互完整度高于广成（广成无页面），保留。
5. **reason:none 纯代码工序（零 token）**：image_download/image_inspect 设计正确，保留。

### B. 需补齐（照广成搬回）

1. **xhs 子进程调用方式**：改为广成 `from source import XHS` + `XHS(work_path=..., folder_name='', folder_mode=True, image_download=True, video_download=False, live_download=False, record_data=True, download_record=False, image_format='JPEG')` + `await xhs.extract(url, download=True)`。当前 `from main import download` 必 ImportError。
2. **xhs 元数据 SQL**：改回 `explore_data` 表 + 中文列（`SELECT "作品描述","作品标签","作者ID" FROM explore_data WHERE "作品ID"=?`）。当前 `note_data` 表/英文列名不存在。
3. **xhs 差集计数 + 未落盘检查 + xsec_token 过期提示**：`after - before` 只数本次新增；paths 空 → ok=False；note 提示重取分享链接。
4. **闲鱼选择器/过滤/兜底全套**：11+6 套主图/详情选择器、`_harvest_all_imgs` naturalWidth≥100、URL 过滤关键词 9 类、`_DEAD_PAGE_KEYWORDS` 失效页删垃圾图、全局 90s deadline（非 set_default_timeout）、networkidle 优先、page.title 去后缀 + desc + h1 标题三路、window.__INITIAL_STATE__ 卖家两路。
5. **元数据降级标注**：ok=True 时 note 记降级原因（db-missing/no-title/...）供上层 checks，别静默返回空。

### C. 需改造（刘全自己的断点）

1. **链 input 契约统一**：`scrape_suggest_chain` 的 input model 与 web 提交对齐——要么 chain input 用 `ScrapeChainInput{urls/batch_id}` 并在链内拆分多链接为逐个 image_download 步骤（页面语义是「多行多链接」），要么页面改为逐个提交。当前 `SuggestionInput{image_ids}` 与 `{urls}` 互相矛盾，页面提交必 422。
2. **image_file 落库**：image_download 完成（或链步骤间 provider/action）调 `POST /api/biz/scrape/images` 落库（batch_id+url 幂等 409 已有）；否则下游 image_inspect/product_suggestion 无数据可读。补 A47 验收测试。
3. **EngineContext 注入 biz_client**：image_inspect 依赖 `ctx.biz_client` 读 image_file 的 local_path，但 context 无此字段 → 要么在 EngineContext 加可注入字段（走详设「写接口化/决策 26」路线），要么 image_inspect 改经 provider（如新增 `scrape.image_by_ids`）拿路径。当前恒 None = 体检不可用。
4. **product_suggestion 诚实化**：要么真接 LLM（reason:llm + prompt.md 已有模板 + 元数据入 prompt），要么改 `reason: none` 纯代码拼装并改 worker.yaml，别挂着 llm 名头做模板拼接（浪费 REASON 相位 token 且误导）。
5. **定时触发**：页面/详设宣称「立即/定时」，当前只有立即；如要定时，补 scrape_suggest_chain 的定时链配置（v0.4 机制现成）。

### D. 总体判断

**刘全扒图工序应保留广成版能力并修通，而不是推倒重来。** 广成版的价值在连接器实现细节（xhs 子进程参数、ExploreData.db 读取、闲鱼多路兜底）——这些是实跑验证过的；刘全 v0.5 的价值在下游链（落库/体检/选品/提案审核）。当前问题恰好是「上游细节丢了 + 下游没接通」：xhs 两处硬伤（必失败、必空）、闲鱼兜底缩水、链 6 个断点。重做顺序建议：① 先按 B 搬回广成连接器细节（含真实 vendor 验证测试）；② 再按 C 修通链契约/落库/context 装配；③ 最后补 A47/A48 验收与页面端到端回归。视频与价格暂不做（广成无先例，业务未排期）。

---

## 附：调研文件清单

**广成（只读）**
- `studio/skills/image-download/SKILL.md`（30 行）
- `studio/skills/image-download/run.py`（106 行）
- `studio/runtime/xhs_connector.py`（217 行）
- `studio/runtime/xianyu_connector.py`（489 行）
- `studio/vendor/XHS-Downloader/main.py`、`source/module/recorder.py`（DATA_TABLE 实锤落库结构）、`source/__init__.py`
- `studio/skills/feishu-write/run.py` + `SKILL.md`（etsy-image-pack 唯一消费者）

**刘全 v0.5**
- `engine/connectors/xhs.py`（203 行）、`xianyu.py`（232 行）、`http_image.py`（144 行）
- `engine/registry/workers/scrape/image_download/{run.py,worker.yaml,schema.py,prompt.md}`
- `engine/registry/workers/scrape/image_inspect/{run.py,worker.yaml,schema.py}`
- `engine/registry/workers/scrape/product_suggestion/{run.py,worker.yaml,schema.py,prompt.md,config/spec.yaml}`
- `engine/registry/chains/scrape/scrape_suggest_chain/chain.yaml`
- `engine/actions/scrape_proposal.py`、`engine/core/context.py`、`engine/core/runner.py`、`engine/registry/loader.py`
- `models/workers/__init__.py`（ImageDownloadInput/ImagePack/ImageInspectInput/InspectionItem/InspectionResult/SuggestionInput/SuggestionResult）
- `web/app.py`（/scrape、/scrape/run、/scrape/suggest、/scrape/thumbnail、CHAIN_INPUTS）、`web/api_biz.py`（/api/biz/scrape/images 写接口）、`web/scrape_store.py`
- `web/templates/scrape/index.html`（页面入口）
- `migrations/business/versions/0009_scrape_image_file.py`（image_file 表结构）
- `docs/详设-v0.5-SEO与扒图.md`（§5.3/5.4/5.5/6.1/6.2/6.3/6.4）

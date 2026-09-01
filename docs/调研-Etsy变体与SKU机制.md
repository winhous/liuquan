# Etsy 变体与 SKU 机制调研（问题 1/2 解决方案依据）

> 状态：**调研完成（2026-09-03，子代理 + 主代理双路查证）** ｜ 结论已并入 `数据底层逻辑-实物物料卖法三层模型.md` §3.6/§7
> 依据：Etsy 官方 Help Center + Open API v3 + 官方 Bundle 功能 + 跨境 ERP 实践

## 一、已确认事实（官方来源）

### F1. Etsy variations（变体）机制

- 每个 Listing 最多 **3 个变体属性**（property values；2026-08 起 API 支持第三个变体，此前 2 个）
- 每个**变体组合 = API 一个 product**，各带**独立 SKU / 价格 / 数量**（offerings）
- 组合数上限：3 变体 ≤ 2500 组合；1-2 变体 ≤ 4900 组合；但**逐组合独立 SKU/价/量最多 400 个**
- 来源：Etsy Open API v3 Third Variation Tutorial（developer.etsy.com）+ Help Center variations (115015664047)

### F2. Etsy SKU 框规则

- SKU 框**可选填**；每个变体组合一个值
- 建议 4-8 字符；**官方禁止逗号 / 斜杠等符号**（只允许字母数字、连字符、下划线等）
- 建议**全店唯一**（便于订单对账）
- 来源：Etsy Help Center SKU (115015691707) + SpySeller SKU 指南

### F3. 订单回传

- 订单 CSV 与 API（getShopReceipt / getShopReceiptTransaction 的 product_data.sku）**都带回 SKU**
- 另带 listing_id + 变体值可兜底（SKU 缺失时仍能对上）
- 来源：Etsy Sold Transactions CSV (360000343328) + Open API v3

### F4. Etsy 捆绑现状

- Etsy **无原生 BOM 型捆绑**（没有"一个 Listing 由多个物料组成"的官方结构）
- 2024-10 推出 **"Buy Together / Mix and Match"**：卖家可链接 **≤3 个 Listing** 打折同购（捆绑必须带折扣，最低 5%）
- 捆绑是**促销工具**：每个 Listing 仍是独立交易、各带原 SKU；一个 Listing 只能进一个 bundle
- **官方明确**：捆绑时每个商品保持原始 SKU，不需要为捆绑创建新 SKU
- 来源：Artery "Offer Listings Together For Less" 文章 + Etsy 官方促销帮助

## 二、问题 1 结论：属性变体 → 每变体组合一个 SKU

**推荐：物料层按「颜色 × 尺寸」组合粒度建 SKU**（SKU-打火机-银色 / SKU-打火机-黑色），**不做"一个 SKU 带属性"**。

理由：
1. **平台 SKU 粒度 = 变体组合**：一个 SKU 带属性则订单回来无法区分颜色 → 采购/扣库存失效
2. 与五层模型完全自洽：实物按「SKU × 仓库」、图集 = 「SKU × 店铺」、Etsy 变体可链图
3. **Etsy 变体 SKU 框直接填本地 SKU，零映射**

可选增强：同款不同色归一个「款式/商品组」概念（管理视图用，不改变 SKU 粒度）

## 三、问题 2 结论：套餐 Listing 填组合虚拟 SKU

**推荐：套餐作为独立 Listing 时，SKU 框填组合虚拟 SKU（方案 a）**，如 `COMBO-LIGHTER-CIG`。

排除项：
- 方案 b（填两个 SKU）**不可行**：一个框只能一个值，且官方禁逗号/斜杠 → 需脆弱解析
- 方案 c（不填 SKU）**不可取**：铁律③（订单对库存的钥匙）失效

虚拟 SKU 规则：
- 登记在**卖法层**，**全局唯一**（前缀区分实物 SKU，如 COMBO-）
- 订单回来精确命中虚拟 SKU → **BOM 展开** → 物料层各扣 1
- **不污染物料层**（物料层只有实物 SKU）

## 四、模型调整建议（已并入底层逻辑文档）

1. **§3.6 补充**：物料层 SKU 粒度 = 变体组合粒度
2. **§3.6 补充**：卖法层新增「卖法平台 SKU（虚拟 SKU）」字段，映射 BOM
3. **订单对库存两段式**：命中实物 SKU 直接扣；命中虚拟 SKU 按 BOM 展开扣
4. **§7 开放项 5/6 关闭**（属性变体、Etsy 变体框填写已有结论）

## 参考来源

- https://developer.etsy.com/documentation/tutorials/third-variation/
- https://help.etsy.com/hc/en-gb/articles/115015664047-How-to-Add-Variations-for-Your-Listings
- https://help.etsy.com/hc/en-us/articles/115015691707-How-to-Use-SKU-for-Your-Inventory
- https://help.etsy.com/hc/en-us/articles/360000343328（Sold Transactions CSV）
- https://www.artery.team/blog/what-is-etsys-new-tool-offer-listings-together-for-less
- https://spyseller.com/blog/how-to-set-up-sku-numbers-for-etsy-listings-beginner-friendly-guide

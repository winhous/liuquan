# crm_image_save 工序

保存CRM对话图片识图结果。

## 输入
- captions: 识图结果列表 [{message_image_id, text, note}]

## 输出
- updated: 更新结果列表 [{message_image_id, ok, note}]
- note: 降级说明

## 逻辑
1. 从 captions 中提取每个图片的 message_image_id 和 text
2. 经写接口 PATCH /api/biz/crm/message-images/{id} 更新 ocr_text + status=downloaded
3. 输出更新结果
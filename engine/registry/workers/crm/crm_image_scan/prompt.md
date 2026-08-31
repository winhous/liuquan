# crm_image_scan 工序

提取对话消息中的图片链接。

## 输入
- message_id: 对话消息 id

## 输出
- urls: 图片链接列表
- note: 降级说明

## 逻辑
1. 经 provider crm.message_images 拿消息的图片记录
2. 过滤出 status=pending 的记录
3. 返回 url 列表
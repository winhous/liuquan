# crm_image_download 工序

下载CRM对话图片（pending状态）。

## 输入
- message_image_ids: crm.message_image.id 列表（pending 状态）

## 输出
- downloaded: 下载结果列表 [{message_image_id, local_path, ok, note}]
- note: 降级说明

## 逻辑
1. 从 context_data 拿 crm.message_images provider 返回的图片记录
2. 过滤出 status=pending 的记录
3. 对每条记录调用 http_image connector 下载到 storage_dir/crm/<message_id>/
4. 输出下载结果（ok/note/local_path）
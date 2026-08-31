# crm_image_caption 工序

识别CRM对话图片内容（vision未配置降级）。

## 输入
- local_paths: 本地图片路径列表
- message_image_ids: 对应的 message_image.id 列表

## 输出
- captions: 识图结果列表 [{message_image_id, text, note}]
- note: 降级说明

## 逻辑
1. 检查 llm_output（REASON 相位产出）是否为 None（vision 未配置降级）
2. 如果 llm_output=None，输出全部 note 识图模型未配置 + text 空
3. 如果 llm_output 可用，解析识图结果，输出 captions
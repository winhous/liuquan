本工序为纯代码工序（worker.yaml 的 reason: none），REASON 相位由 runner 跳过，
无 LLM 调用、无 prompt 渲染。本文件仅占位（loader L8：声明了 prompt 就要存在）。

工序逻辑见 run.py：把链输入（echo 文本 + 业务对象 ref_id/kind）组织成
TaskProposal 契约输出；业务规格值（默认角色/文案模板等）住 config/。

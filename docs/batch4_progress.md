# 批4 SKU核心链路 — 任务清单

## 阶段1：读详设 §5.3-§5.6 + 读现有 catalog/inventory_service
- [ ] 读详设 §5.3（SKU 建档流程）→ sku_service.create_sku()
- [ ] 读详设 §5.4（SKU 列表/详情/更新）
- [ ] 读详设 §5.5（变体管理）
- [ ] 读详设 §5.6（SKU-图片关联）
- [ ] 读现有 catalog_service.py 了解已实现的校验函数
- [ ] 读现有 inventory_service.py 了解库存联动

## 阶段2：实现 sku_service.py
- [ ] create_sku() — 品名唯一+组合唯一+变体校验
- [ ] list_skus() / get_sku()
- [ ] update_sku() / delete_sku()
- [ ] create_variant() / update_variant() / delete_variant()
- [ ] attach_image() / detach_image()
- [ ] 档案快照生成

## 阶段3：验收 A84-A96
- [ ] 写 13 条验收函数
- [ ] 全部 passed

## 阶段4：更新清单 + check.sh + 报告
- [ ] acceptance_manifest.yaml 追加 A84-A96
- [ ] check_acceptance.py EXPECTED_IDS 追加
- [ ] check.sh coverage 范围追加
- [ ] 五绿验证
- [ ] 最终报告

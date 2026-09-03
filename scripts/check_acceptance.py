"""B1 断言覆盖机器检查器（check.sh 第 5 门）。

设计依据（三态设计）：
- A47/A48/A51 在 v0.5 阶段为已知缺口（扒图重做尚未落地），用 planned 标记
  记黄不红。若一刀切红，B1 自身提交会被四绿 hook 拦死锁。
- implemented = 该断言已有真实自动化测试，缺测试 = 红（防假绿核心机制）。
- manual = 手工验证无自动化测试（A11 真模型冒烟）。

用法：
  uv run python scripts/check_acceptance.py [manifest_path] [tests_dir]
  缺省 = tests/acceptance_manifest.yaml + tests/

输出：逐行打印检查结果（红/黄/绿前缀），末尾汇总；红 > 0 退出码 1，否则 0。

约束：纯标准库 + pyyaml，禁止 import engine 任何模块（R24：scripts/ 只放运维脚本）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("红: pyyaml 未安装（pip install pyyaml）")
    sys.exit(1)

# ---- 硬编码 A1-A75 全集（含版本归属，缺任一 id 即红）----
EXPECTED_IDS: dict[str, str] = {
    # v0.1 引擎地基
    "A1": "v0.1", "A2": "v0.1", "A3": "v0.1", "A4": "v0.1",
    "A5": "v0.1", "A6": "v0.1", "A7": "v0.1", "A8": "v0.1",
    "A9": "v0.1", "A10": "v0.1", "A11": "v0.1",
    # v0.2 TM
    "A12": "v0.2", "A13": "v0.2", "A14": "v0.2", "A15": "v0.2",
    "A16": "v0.2", "A17": "v0.2", "A18": "v0.2", "A19": "v0.2",
    "A20": "v0.2", "A21": "v0.2", "A22": "v0.2", "A23": "v0.2",
    "A24": "v0.2", "A25": "v0.2", "A26": "v0.2",
    # v0.3 CRM
    "A27": "v0.3", "A28": "v0.3", "A29": "v0.3", "A30": "v0.3",
    "A31": "v0.3", "A32": "v0.3", "A33": "v0.3", "A34": "v0.3",
    "A35": "v0.3", "A36": "v0.3", "A37": "v0.3",
    # v0.4 定时闭环与设置
    "A38": "v0.4", "A39": "v0.4", "A40": "v0.4", "A41": "v0.4",
    "A42": "v0.4", "A43": "v0.4", "A44": "v0.4", "A45": "v0.4",
    # v0.5 SEO + 扒图
    "A46": "v0.5", "A47": "v0.5", "A48": "v0.5", "A49": "v0.5",
    "A50": "v0.5", "A51": "v0.5", "A52": "v0.5", "A53": "v0.5",
    "A54": "v0.5", "A55": "v0.5", "A56": "v0.5",
    # v0.6 扒图重做（素材库）
    "A57": "v0.6", "A58": "v0.6", "A59": "v0.6", "A60": "v0.6",
    "A61": "v0.6", "A62": "v0.6", "A63": "v0.6", "A64": "v0.6",
    "A65": "v0.6", "A66": "v0.6", "A67": "v0.6", "A68": "v0.6",
    "A69": "v0.6",  # 批 6（详设 §15.4）：一链接一文件夹 + meta.txt
    "A70": "v0.6",  # 批 7（详设 §15.4）：夸克上传网盘 flow
    "A71": "v0.6",  # 批 7（详设 §15.4）：未授权/上传失败提示
    "A72": "v0.6",  # 批 7（详设 §15.4）：历史补传入口
    "A73": "v0.6",  # 批 8（详设 §15.4）：定时队列在扒图页（设置页去队列块）
    "A74": "v0.6",  # 批 8（详设 §15.4）：netdisk.upload_default 设置键生效
    "A75": "v0.6",  # 批 9（详设 §15.7）：独立任务详情页 GET /tasks/{id}
    # v0.7 SKU 建档
    "A76": "v0.7",  # 批 1 数据地基：catalog 7 表结构 + 约束 + 两仓种子 + sys.shop.platform
    "A77": "v0.7",  # 批 2：手工建档 physical（商品名+code+cost+active）
    "A78": "v0.7",  # 批 2：编号校验（非法/重复/必填）
    "A79": "v0.7",  # 批 2：同商品同名档冲突 409
    "A80": "v0.7",  # 批 2：combo 建档 + 配方 + 无库存
    "A81": "v0.7",  # 批 2：custom 档案 + 无库存 + 无 BOM
    "A82": "v0.7",  # 批 3：库存记账+流水（事务原子/超扣回滚/sale 501/combo+custom 409）
    "A83": "v0.7",  # 批 3：库存不影响建档（接单采购模式）
    "A89": "v0.7",  # 批 2：存量接缝（ERP 编号+无图建档）
}


def _func_exists(repo_root: Path, file_path: str, func_name: str) -> bool:
    """检查 repo_root/file_path 中是否存在 def func_name( 函数定义。"""
    full = repo_root / file_path
    if not full.is_file():
        return False
    pattern = re.compile(rf"def\s+{re.escape(func_name)}\s*\(")
    for line in full.read_text(encoding="utf-8").splitlines():
        if pattern.search(line):
            return True
    return False


def check_manifest(
    manifest_path: Path, tests_dir: Path, repo_root: Path | None = None
) -> tuple[list[str], list[str]]:
    """校验 manifest，返回 (red_msgs, yellow_msgs)。

    Red 条件：
    1. manifest 中缺少 EXPECTED_IDS 的任一 id
    2. implemented 的 test 字段指向不存在的函数
    3. planned 缺 plan 字段或 plan 为空

    Yellow（仅提示）：
    - 每个 planned 项输出缺口提示
    """
    red: list[str] = []
    yellow: list[str] = []
    if repo_root is None:
        repo_root = manifest_path.resolve().parent.parent

    with open(manifest_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "assertions" not in data:
        red.append("manifest 缺少 assertions 列表")
        return red, yellow

    manifest_ids: dict[str, dict] = {}
    for entry in data["assertions"]:
        aid = entry.get("id", "")
        manifest_ids[aid] = entry

    # 检查 1：缺 id
    missing = set(EXPECTED_IDS) - set(manifest_ids)
    if missing:
        sorted_missing = sorted(missing, key=lambda x: int(x[1:]))
        red.append(
            f"manifest 缺少断言编号: {', '.join(sorted_missing)} "
            f"（共 {len(missing)} 个，需覆盖 A1-A75 全集）"
        )

    # 逐条检查
    for aid in sorted(manifest_ids, key=lambda x: int(x[1:])):
        entry = manifest_ids[aid]
        status = entry.get("status", "")
        expected_ver = EXPECTED_IDS.get(aid, "unknown")

        # 版本归属检查（warn only）
        entry_ver = entry.get("version", "")
        if entry_ver and entry_ver != expected_ver:
            yellow.append(
                f"缺口: {aid} version 字段为 {entry_ver}，期望 {expected_ver}"
            )

        if status == "implemented":
            test_field = entry.get("test", "")
            if not test_field:
                red.append(f"红: {aid} status=implemented 但 test 字段为空")
                continue
            # 解析 file_path::func_name
            if "::" not in test_field:
                red.append(
                    f"红: {aid} test 格式错误（期望 file_path::func_name）: {test_field}"
                )
                continue
            file_path, func_name = test_field.split("::", 1)
            if not _func_exists(repo_root, file_path, func_name):
                red.append(
                    f"红: {aid} test 指向不存在的函数: {file_path}::{func_name}"
                )

        elif status == "planned":
            plan = entry.get("plan", "")
            if not plan or not str(plan).strip():
                red.append(f"红: {aid} status=planned 但 plan 字段为空或缺失")
            else:
                yellow.append(f"缺口: {aid} ({plan})")

        elif status == "manual":
            pass  # 手工验证，不检查

        else:
            red.append(
                f"红: {aid} status 值非法: '{status}'（期望 implemented/planned/manual）"
            )

    return red, yellow


def main() -> int:
    """CLI 入口，返回退出码（0=无红，1=有红）。"""
    args = sys.argv[1:]
    repo_root = Path(__file__).resolve().parent.parent
    manifest_path = Path(args[0]) if len(args) > 0 else repo_root / "tests" / "acceptance_manifest.yaml"
    tests_dir = Path(args[1]) if len(args) > 1 else repo_root / "tests"

    if not manifest_path.is_file():
        print(f"红: manifest 文件不存在: {manifest_path}")
        return 1

    print(f"---- 5 断言覆盖：检查 {manifest_path} ----")
    red, yellow = check_manifest(manifest_path, tests_dir)

    # 逐行输出
    for y in yellow:
        print(f"黄: {y}")
    for r in red:
        print(r)

    # 汇总
    print()
    print("==== 断言覆盖汇总（绿/黄/红）====")
    if yellow:
        print(f"黄: {len(yellow)} 项已知缺口（planned，不阻塞）")
    if red:
        print(f"红: {len(red)} 项不通过")
        print(f"结果：断言覆盖检查未通过（B1），{len(red)} 项真红")
        return 1

    print("绿: 所有 implemented 断言均有真实测试函数")
    print("结果：断言覆盖检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

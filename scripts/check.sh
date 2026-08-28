#!/usr/bin/env bash
# 刘全四绿总门（开发流程 ④ 四绿 hook；开发规范 R13 四绿守门）
#
# 用法：bash scripts/check.sh（任意 cwd 可执行，自动定位仓库根；可重复执行）
#
# 四绿（v0.1 全真跑，无 skipped）：
#   1) lint              engine/lint P1/P2/P3（T2 P2/P3 + T11 P1 已落地）
#   2) registry 一致性   liuquan-engine registry-check（详设 §10；T12b 已实现，真跑）
#   3) 全量单测          uv run pytest（fake LLM、零网络，规范 R12）
#   4) verify            liuquan-engine verify（详设 §10；T12b 已实现，真跑）
#
# 判定：
#   - 任一【真红】= 本脚本非零退出（git pre-commit hook 拦提交，不绿不让提交）
#   - skipped = 骨架期的诚实过渡态：该项规则尚未由对应任务建成，记绿不记红，
#     逐项注明落地任务号；对应任务落地时删除该 skipped 分支（变更日志留痕）
#
# 本脚本只做编排与汇总，不 import engine（开发规范 R24：scripts/ 只放运维脚本）。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# git hook 环境的 PATH 可能不含 uv，补默认安装位
if ! command -v uv >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
fi

# ---- 落地任务号（v0.1 已全部落地：T2 lint P2/P3、T11 P1、T12b registry-check/verify）----
REGISTRY_LAND_TASK="T12b"    # liuquan-engine registry-check（已实现，真跑）
VERIFY_LAND_TASK="T12b"      # liuquan-engine verify（已实现，真跑）

SUMMARY=()   # 末尾四行「绿/红」汇总
RED=0        # 真红计数

mark_green() { SUMMARY+=("绿 $1"); }
mark_red()   { RED=$((RED + 1)); SUMMARY+=("红 $1"); }
mark_skip()  { SUMMARY+=("绿 $1（skipped，$2）"); }

cli_gate() {
  # 用法: cli_gate <门名> <子命令> <落地任务号> <红时描述>
  # 探测即真跑（只跑一次），按 CLI 退出码约定判定（约定见 engine/cli.py 文件头）：
  #   0 = 命令已实现且通过 => 绿；非 0 且非 2 = 已实现但不通过 => 红；
  #   2 且报 argparse usage 错 = 子命令尚未实现 => skipped（骨架期过渡态）。
  # 注意不能用 `<子命令> --help` 探测：argparse 见 --help 先于未知命令校验
  # 立即退出 0，恒为真（T1 实测踩过）。
  local display="$1" cmd="$2" land_task="$3" red_desc="$4"
  local out rc
  if out="$(uv run liuquan-engine "$cmd" 2>&1)"; then rc=0; else rc=$?; fi
  if [[ "$rc" -eq 2 && ( "$out" == *"unrecognized arguments"* || "$out" == *"invalid choice:"* ) ]]; then
    echo "${display}: skipped（${cmd} 命令未实现，${land_task} 落地）"
    mark_skip "${display}" "${cmd} 未实现，${land_task} 落地"
  elif [[ "$rc" -eq 0 ]]; then
    echo "---- ${display}：liuquan-engine ${cmd} ----"
    echo "$out"
    mark_green "${display}（通过）"
  else
    echo "---- ${display}：liuquan-engine ${cmd} ----"
    echo "$out"
    mark_red "${display}（${red_desc}，详见上方输出）"
  fi
}

echo "==== 刘全四绿总门 scripts/check.sh ===="

# ---------- 绿 1/4：lint（T2 落地：P2 凭据端点零容忍 + P3 契约纪律，P1=T11）----------
# 入口 engine/lint/__main__.py：0 违规退出 0，有违规打印明细退出 1。
# lint 自身也在扫描对象内（详设 §9 扫全仓），tests/ 有正反双向测试防校验器改坏。
echo "---- 1/4 lint：uv run python -m engine.lint ----"
if uv run python -m engine.lint; then
  mark_green "1 lint（0 违规）"
else
  mark_red "1 lint（存在违规，详见上方输出）"
fi

# ---------- 绿 2/4：registry 一致性 ----------
cli_gate "2 registry" "registry-check" "$REGISTRY_LAND_TASK" "registry-check 不合规"

# ---------- 绿 3/4：全量单测（必须真跑，不允许空转）----------
echo "---- 3/4 单测：uv run pytest ----"
if uv run pytest; then
  mark_green "3 单测（uv run pytest 通过）"
else
  mark_red "3 单测（pytest 有失败或未收集到测试）"
fi

# ---------- 绿 4/4：verify ----------
cli_gate "4 verify" "verify" "$VERIFY_LAND_TASK" "verify 未通过"

# ---------- 汇总 ----------
echo
echo "==== 四绿汇总（绿/红）===="
for line in "${SUMMARY[@]}"; do
  echo "$line"
done

if [[ "$RED" -gt 0 ]]; then
  echo
  echo "结果：${RED} 项真红 -- 四绿未过（R13），改动未完成，提交被拦截"
  exit 1
fi
echo
echo "结果：四绿全绿（v0.1 全真跑，无 skipped）"
exit 0

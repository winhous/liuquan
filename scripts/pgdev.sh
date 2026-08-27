#!/usr/bin/env bash
# 刘全本地 PG16 开发/测试集群管理（Wave 0 T3 交付）
#
# 背景：本机无系统 PG16（无 docker、sudo 需密码），本脚本管理的集群由
# 「apt download + dpkg -x 解压到 ~/.local/pg16」部署（无 root，见变更日志 T3 条目）。
# 用法：
#   bash scripts/pgdev.sh start            # 启动集群（端口默认 5432，可用 LIUQUAN_PG_PORT 覆盖）
#   bash scripts/pgdev.sh stop             # 停止集群
#   bash scripts/pgdev.sh status           # 查看状态
#   bash scripts/pgdev.sh psql [db]        # 进 psql（默认连 liuquan_engine 库）
#   bash scripts/pgdev.sh createdb [db]    # 建库（默认 liuquan_engine）
#   bash scripts/pgdev.sh initdb           # 重新 initdb（仅首次/数据损坏时用）
#
# 运维脚本，不 import engine（开发规范 R24：scripts/ 只放运维脚本）。
# 测试用嵌入式簇（tests/conftest.py）另起随机端口临时实例，与本集群互不干扰。

set -euo pipefail

PGROOT="${PGROOT:-$HOME/.local/pg16}"
PGBIN="$PGROOT/usr/lib/postgresql/16/bin"
PGDATA="${PGDATA:-$PGROOT/data}"
PORT="${LIUQUAN_PG_PORT:-5432}"
# 解压的 libpq 不在系统 ld 路径，必须显式指（initdb/pg_ctl/psql 都依赖）
export LD_LIBRARY_PATH="$PGROOT/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

case "${1:-}" in
  start)    "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGROOT/server.log" -o "-p $PORT -k /tmp" start ;;
  stop)     "$PGBIN/pg_ctl" -D "$PGDATA" stop ;;
  status)   "$PGBIN/pg_ctl" -D "$PGDATA" status ;;
  initdb)   "$PGBIN/initdb" -D "$PGDATA" -U winhous --auth-local=trust --auth-host=trust -E UTF8 --locale=C ;;
  psql)     shift; "$PGBIN/psql" -h 127.0.0.1 -p "$PORT" -U winhous -d "${1:-liuquan_engine}" "${@:2}" ;;
  createdb) shift; "$PGBIN/psql" -h 127.0.0.1 -p "$PORT" -U winhous -d postgres -c "CREATE DATABASE ${1:-liuquan_engine}" ;;
  *) echo "用法: $0 {start|stop|status|initdb|psql [db]|createdb [db]}"; exit 2 ;;
esac

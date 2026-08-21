#!/bin/bash
# 研究室冷蔵庫管理 - 停止 (macOS)
# start.command のウィンドウを閉じても停止できますが、
# バックグラウンドで残ってしまった場合はこれをダブルクリックしてください。
cd "$(dirname "$0")" || exit 1

if [ -f data/server.pid ]; then
  if kill "$(cat data/server.pid)" 2>/dev/null; then
    echo "停止しました。"
  else
    echo "プロセスが見つかりません（すでに停止しています）。"
  fi
  rm -f data/server.pid
else
  echo "起動していないようです（pidファイルがありません）。"
fi
read -r -p "Enterで閉じる..." || true

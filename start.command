#!/bin/bash
# 研究室冷蔵庫管理 - 起動 (macOS)
# ダブルクリックで起動します。初回は自動でセットアップされます。
# このウィンドウを閉じる（または Ctrl+C）とアプリが停止します。
cd "$(dirname "$0")" || exit 1

# venv はクラウド同期フォルダを避けて、この機体のローカルに置く
VENV="$HOME/.venvs/fridge-manager"

if [ ! -x "$VENV/bin/python" ]; then
  echo "初回セットアップを実行しています（1〜2分かかります）..."
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[エラー] python3 が見つかりません。https://www.python.org/downloads/ からインストールしてください。"
    read -r -p "Enterで閉じる..." || true
    exit 1
  fi
  python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --quiet --upgrade pip \
    && "$VENV/bin/pip" install --quiet -r requirements.txt
  if [ $? -ne 0 ]; then
    echo "[エラー] セットアップに失敗しました。ネットワーク接続を確認して再実行してください。"
    rm -rf "$VENV"
    read -r -p "Enterで閉じる..." || true
    exit 1
  fi
  echo "セットアップ完了。起動します。"
fi

exec "$VENV/bin/python" app.py

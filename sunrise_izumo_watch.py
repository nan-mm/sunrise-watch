"""
サンライズ出雲(東京→出雲市)の「シングル」個室(禁煙・喫煙)の空席を、
複数の乗車日についてe5489の空席照会ページから直接取得し、
空きが出たらntfy.shへプッシュ通知するスクリプト。

■ 大きな特長
- ログイン不要・ブラウザ自動操作(Playwright)も不要
- e5489の「空席照会」ページ(ログイン不要の経路)に直接requestsでアクセスし、
  HTML内の <img alt="..."> 属性(「空席あり」「空席残りわずか」「残席なし」)
  を読み取るだけで判定する

■ 使い方
1. 下の CONFIG セクションを自分の環境に合わせて書き換える
2. 単発実行(デフォルト日付リストを使用): python sunrise_izumo_watch.py
   日付を指定する場合(カンマ区切りで複数指定可):
     python sunrise_izumo_watch.py --dates 2026-10-20,2026-10-21,2026-10-30
3. 定期実行: cron や GitHub Actions で 5分おきに起動
   (crontabの例)
   */5 * * * * cd /path/to/script && \
     NTFY_TOPIC=zzz \
     python3 sunrise_izumo_watch.py --dates 2026-10-20,2026-10-21,2026-10-30 >> watch.log 2>&1

■ 通知
- ntfy.sh経由でiPhoneの「ntfy」アプリに通知(トピックを購読しておく)
- 日付ごとに個別に状態を管理するので、どれか1つに空きが出れば
  その日付単体で通知される

■ 注意
- e5489の利用規約を確認し、過度に高頻度なアクセスは避けること
  (5分間隔を推奨。それより短い間隔は避ける)
- このURLの仕組みはe5489側の仕様変更で突然動かなくなる可能性があります。
  その場合は再度ブラウザの開発者ツールで構造を確認してください
"""

import os
import json
import argparse
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ============ CONFIG ============
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "sunrise-izumo-CHANGE-ME")

TARGET_TRAIN_NAME = "サンライズ出雲"
DEPARTURE_STATION = "東京"
ARRIVAL_STATION = "出雲市"
TARGET_SEAT_TYPE = "シングル"

# 「空きあり」とみなすステータス文字列(e5489の凡例に準拠)
AVAILABLE_STATUSES = ["空席あり", "残りわずか"]

# 監視したい乗車日(デフォルト)。--dates オプションで実行時に上書き可能
DEFAULT_TARGET_DATES = ["2026-10-20", "2026-10-21", "2026-10-30"]

# e5489の空席照会URL。{DATE} の部分だけ YYYYMMDD 形式で差し替える。
# それ以外のパラメータ(駅名・列車種別等のShift-JISエンコード済み値)は
# ブラウザで実際に取得したものをそのまま固定で使う(東京→出雲市・サンライズ出雲用)。
URL_TEMPLATE = (
    "https://e5489.jr-odekake.net/e5489/cssp/CBDayTimeArriveSelRsvMyDiaSP"
    "?inputDepartStName=%93%8C%8B%9E"
    "&inputArriveStName=%8Fo%89_%8Es"
    "&inputType=0"
    "&inputDate={DATE}"
    "&inputHour=21"
    "&inputMinute=00"
    "&inputUniqueDepartSt=1"
    "&inputUniqueArriveSt=1"
    "&inputSearchType=2"
    "&inputTransferDepartStName1=%93%8C%8B%9E"
    "&inputTransferArriveStName1=%8Fo%89_%8Es"
    "&inputTransferDepartStUnique1=1"
    "&inputTransferArriveStUnique1=1"
    "&inputTransferTrainType1=0001"
    "&inputSpecificTrainType1=2"
    "&inputSpecificBriefTrainKana1=%BB%B2%BD%D3%BC000"
    "&SequenceType=0"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )
}

STATE_FILE = Path(__file__).parent / "last_state.json"
# =================================


def notify(title: str, message: str, priority: str = "default"):
    """ntfy.sh経由でスマホにプッシュ通知"""
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[WARN] 通知の送信に失敗しました: {e}")


def load_last_state() -> dict:
    """日付ごとの前回状態を読み込む。例: {"2026-10-30": ["残席なし", "残席なし"]}"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def has_availability(statuses: list[str]) -> bool:
    """ステータスのリストのうち、どれか1つでも「空きあり」系ならTrue"""
    return any(status in AVAILABLE_STATUSES for status in statuses)


def fetch_seat_statuses(target_date: str) -> list[str] | None:
    """
    指定した乗車日のサンライズ出雲「シングル」(禁煙・喫煙)の空席状況を取得する。
    戻り値: ["残席なし", "残席なし"] のような、各個室(禁煙/喫煙)の状態リスト
            (取得できなければ None)
    """
    date_str = target_date.replace("-", "")  # "2026-10-30" -> "20261030"
    url = URL_TEMPLATE.format(DATE=date_str)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] {target_date} のリクエストに失敗しました: {e}")
        return None

    # e5489はShift-JIS(cp932)で配信されていることが多いので明示的に指定
    resp.encoding = "cp932"
    soup = BeautifulSoup(resp.text, "html.parser")

    statuses = [
        img["alt"].strip()
        for img in soup.select("table.seat-status-table img[alt]")
    ]

    return statuses or None


def check_one_date(target_date: str, last_state: dict) -> list[str] | None:
    """1つの日付をチェックし、空きが出ていれば通知する。最新の状態リストを返す"""
    statuses = fetch_seat_statuses(target_date)

    if statuses is None:
        print(f"{target_date}: シングルの空席情報を取得できませんでした(URL/構造要確認)")
        return None

    prev_statuses = last_state.get(target_date, [])
    was_available = has_availability(prev_statuses)
    now_available = has_availability(statuses)

    if now_available and not was_available:
        message = (
            f"{target_date} {TARGET_TRAIN_NAME}({DEPARTURE_STATION}→{ARRIVAL_STATION})\n"
            f"シングル: {' / '.join(statuses)}"
        )
        print(f"[{target_date}] 空きを検知:", message)
        notify(
            f"サンライズ出雲 シングル 空席あり！({target_date})",
            message,
            priority="urgent",
        )
    else:
        print(f"[{target_date}] シングル: {' / '.join(statuses)}(状態変化なし)")

    return statuses


def main():
    parser = argparse.ArgumentParser(description="サンライズ出雲 シングル空席監視(複数日程対応)")
    parser.add_argument(
        "--dates",
        default=",".join(DEFAULT_TARGET_DATES),
        help="監視する乗車日をカンマ区切りで指定(例: 2026-10-20,2026-10-21,2026-10-30)",
    )
    args = parser.parse_args()
    target_dates = [d.strip() for d in args.dates.split(",") if d.strip()]

    print(f"[{datetime.now()}] 空席チェック開始(対象日: {', '.join(target_dates)})")

    last_state = load_last_state()
    new_state = dict(last_state)

    for target_date in target_dates:
        updated_statuses = check_one_date(target_date, last_state)
        if updated_statuses is not None:
            new_state[target_date] = updated_statuses

    save_state(new_state)


if __name__ == "__main__":
    main()

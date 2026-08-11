#!/usr/bin/env python
"""rally_stop_hook.py — Stop フックで ADR・README の追記を促す(v2: ファイルシステム判定)。

v1 は transcript 解析で rally 使用を判定していたが、リモートコントロールセッションは
transcript をローカルに残さないため常に沈黙になる欠陥があった(rally試験003 で実測)。
さらにリモートは内部 session_id が可変で、セッション単位のナッジ上限も成立しない。

v2 の設計(誤動作防止を最優先):
- rally 使用判定 = **cwd に docs/interviews/ が存在するか**。このディレクトリは rally の
  spec-interview だけが作る規約なので、rally を使っていないプロジェクトでは発火しない。
  transcript 不要のため、ローカル・リモートで同一に動く。
- 発火条件 = docs/interviews/ と tests/e2e/ の最終更新時刻が docs/adr.md(または docs/adr/
  配下)より新しい(=ADR に未反映の決定・振る舞い変更がある)。対応済みなら沈黙。
- ループ・連発防止 = stop_hook_active ガード(公式)+ **プロジェクト単位の30分クールダウン**
  (session_id は当てにならないため cwd 基準)。
- 判定不能(cwd 無し・stdin 不正)は常に「何もしない」に倒す。

フック出力: 促す場合のみ {"decision": "block", "reason": "..."} を stdout に出す。
それ以外は何も出力せず exit 0(セッションに一切干渉しない)。
"""
import json
import pathlib
import re
import sys
import time

# Windows では標準入出力が cp932 になり、日本語入りの JSON 出力(decision/reason)が
# UTF-8 前提のフックリーダーで読めなくなる。入出力とも UTF-8 に固定する。
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

STATE_DIR = pathlib.Path.home() / ".claude" / "rally" / "hook-state"
STATE_TTL_SECONDS = 7 * 24 * 3600  # 古い state ファイルの掃除しきい値
COOLDOWN_SECONDS = 30 * 60         # 同一プロジェクトへの再ナッジ間隔

NUDGE_REASON = (
    "rally: このセッションで確定・仮決めした設計判断があれば docs/adr.md に一言ずつ追記して"
    "ください(無ければ作成。形式: `- YYYY-MM-DD <決定>。理由: <なぜ>。捨てた案: <あれば>`)。"
    "実装や仕様変更を行った場合は README.md の「申し送り」節(無ければ節を追加)も次セッション"
    "向けに更新してください。すでに両方最新なら何もせずそのまま終了して構いません。"
)


def _cleanup_state_dir():
    """TTL を過ぎた state ファイルを消す(失敗しても無視)。"""
    try:
        now = time.time()
        for p in STATE_DIR.iterdir():
            try:
                if now - p.stat().st_mtime > STATE_TTL_SECONDS:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _state_file_for(cwd):
    """プロジェクト(cwd)ごとの state ファイルパスを返す。"""
    key = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return STATE_DIR / f"{key}.json"


def _latest_mtime(paths):
    """ファイル/ディレクトリ群の配下ファイルの最終更新時刻を返す(無ければ None)。"""
    latest = None
    for base in paths:
        base = pathlib.Path(base)
        if base.is_file():
            candidates = [base]
        elif base.is_dir():
            candidates = [p for p in base.rglob("*") if p.is_file()]
        else:
            continue
        for p in candidates:
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if latest is None or m > latest:
                latest = m
    return latest


def scan_project(cwd):
    """プロジェクトのファイル状態から rally 使用と ADR 鮮度を判定する(純関数)。

    返り値: dict
      rally_used: docs/interviews/ が存在するか(rally の spec-interview だけが作る)
      last_decision_mtime: docs/interviews/ と tests/e2e/ 配下の最終更新
      adr_mtime: docs/adr.md および docs/adr/ 配下の最終更新(無ければ None)
    """
    result = {"rally_used": False, "last_decision_mtime": None, "adr_mtime": None}
    if not cwd:
        return result
    root = pathlib.Path(cwd)
    interviews = root / "docs" / "interviews"
    if not interviews.is_dir():
        return result
    result["rally_used"] = True
    result["last_decision_mtime"] = _latest_mtime([interviews, root / "tests" / "e2e"])
    result["adr_mtime"] = _latest_mtime([root / "docs" / "adr.md", root / "docs" / "adr"])
    return result


def should_nudge(scan, last_nudge_at, now):
    """ナッジすべきか判定する(純関数)。

    - rally 未使用(docs/interviews/ 無し) → しない(他プロジェクトに一切干渉しない)
    - 決定の痕跡(記録/E2E)が無い → しない(まだ決定が出ていない)
    - ADR が決定より新しい → しない(対応済み)
    - クールダウン中 → しない(リモートの session_id 可変対策)
    """
    if not scan["rally_used"]:
        return False
    if scan["last_decision_mtime"] is None:
        return False
    if scan["adr_mtime"] is not None and scan["adr_mtime"] >= scan["last_decision_mtime"]:
        return False
    if last_nudge_at is not None and (now - last_nudge_at) < COOLDOWN_SECONDS:
        return False
    return True


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0  # 入力が読めなければ何もしない

    # 自分の block によって再開したターンでは絶対に再発火しない(公式ループガード)
    if payload.get("stop_hook_active"):
        return 0

    cwd = payload.get("cwd")
    if not cwd:
        return 0

    scan = scan_project(cwd)

    state_file = _state_file_for(cwd)
    last_nudge_at = None
    if state_file.exists():
        try:
            last_nudge_at = json.loads(state_file.read_text(encoding="utf-8")).get("last_nudge_at")
        except Exception:
            last_nudge_at = None

    now = time.time()
    if not should_nudge(scan, last_nudge_at, now):
        return 0

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"last_nudge_at": now}), encoding="utf-8")
        _cleanup_state_dir()
    except OSError:
        # state が書けない場合はナッジしない(連発防止を最優先)
        return 0

    print(json.dumps({"decision": "block", "reason": NUDGE_REASON}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

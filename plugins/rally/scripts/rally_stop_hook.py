#!/usr/bin/env python
"""rally_stop_hook.py — Stop フックで ADR の追記を促す(v2: ファイルシステム判定)。

v1 は transcript 解析で rally 使用を判定していたが、リモートコントロールセッションは
transcript をローカルに残さないため常に沈黙になる欠陥があった(rally試験003 で実測)。
さらにリモートは内部 session_id が可変で、セッション単位のナッジ上限も成立しない。

v2 の設計(誤動作防止を最優先):
- rally 使用判定 = **cwd に docs/interviews/ が存在するか**。このディレクトリは rally の
  spec-interview だけが作る規約なので、rally を使っていないプロジェクトでは発火しない。
  transcript 不要のため、ローカル・リモートで同一に動く。
- 発火条件 = docs/interviews/ と tests/e2e/ の最終更新時刻が docs/adr.md(または docs/adr/
  配下)より新しい(=ADR に未反映の決定・振る舞い変更がある)。対応済みなら沈黙。
  git 管理下では、変更の無い追跡ファイルの時刻を最後のコミット日時で取る(v0.6.4。
  clone 直後は mtime が書き込み順になり、クラウドセッションの初回 Stop で誤発火した)。
- ループ・連発防止 = stop_hook_active ガード(公式)+ **プロジェクト単位の30分クールダウン**
  (session_id は当てにならないため cwd 基準)。
- 判定不能(cwd 無し・stdin 不正)は常に「何もしない」に倒す。

- 締めの発言の注意 = CLAUDE_CODE_ENTRYPOINT で出し分ける(v0.6.5)。ヘッドレス(sdk-cli)
  だけ要点の再掲を許し、それ以外は再掲させない。一律に「ヘッドレスなら再掲」と頼むと、
  経路の見えないモデルが対話セッションでも本文を繰り返していた。

フック出力: 促す場合のみ {"decision": "block", "reason": "..."} を stdout に出す。
それ以外は何も出力せず exit 0(セッションに一切干渉しない)。
"""
import json
import os
import pathlib
import re
import subprocess
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

NUDGE_ADR = (
    "rally: このセッションで確定・仮決めした設計判断があれば docs/adr.md に一言ずつ追記して"
    "ください(無ければ作成。形式: `- YYYY-MM-DD <決定>。理由: <なぜ>。捨てた案: <あれば>。対立した要件: <X vs Y、あれば>`)。"
    "すでに最新なら何もせずそのまま終了して構いません。"
)
# 締めの発言の扱いは経路で分ける。モデルには経路が見えないので hook が見分ける。
CLOSING_HEADLESS = (
    "追記したあとの締めの発言は、直前にユーザーへ返した本文(質問・手順・要約)を置き換えない"
    "こと。ヘッドレス(claude -p)の経路では最後の発言だけが相手に届くため、要点を再掲するか"
    "1〜2 行の追記に留めてください。"
)
CLOSING_ATTENDED = (
    "追記したあとの締めの発言は、ADR を更新したかどうかを 1 行で伝えるだけにしてください。"
    "直前にユーザーへ返した本文(質問・手順・要約)は相手の画面に残っているので、繰り返さないこと。"
)
HEADLESS_ENTRYPOINT = "sdk-cli"  # claude -p で起動したとき(2026-10-24 実測。対話は cli)


def nudge_reason(entrypoint):
    """CLAUDE_CODE_ENTRYPOINT に応じた促し文を返す(純関数)。

    見分けられないとき(変数が無い・未知の値)は、重複を生まない対話側の文に倒す。
    """
    closing = CLOSING_HEADLESS if entrypoint == HEADLESS_ENTRYPOINT else CLOSING_ATTENDED
    return NUDGE_ADR + closing


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


def _git_lines(top, *args):
    """git を top で実行し、-z 区切りの出力を list で返す(失敗は None)。"""
    try:
        p = subprocess.run(["git", "-C", str(top), *args], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return [s for s in p.stdout.decode("utf-8", "replace").split("\0") if s]


def _latest_time_git(paths, top):
    """git 管理下での最終更新を返す(無ければ None)。git で判定できなければ例外 LookupError。

    clone 直後の mtime は書き込み順で決まり、同じコミットの ADR が記録より数 ms 古く
    見える(クラウドセッションで実測)。変更の無い追跡ファイルは最後のコミット日時で比べ、
    mtime は変更中・未追跡のファイルにだけ使う。
    """
    rels = [str(pathlib.Path(p).relative_to(top).as_posix()) for p in paths]
    tracked = _git_lines(top, "ls-files", "-z", "--", *rels)
    changed = _git_lines(top, "diff", "--name-only", "-z", "HEAD", "--", *rels)
    if tracked is None or changed is None:
        raise LookupError
    clean = set(tracked) - set(changed)
    latest = None
    for base in paths:
        base = pathlib.Path(base)
        files = [base] if base.is_file() else ([p for p in base.rglob("*") if p.is_file()]
                                                if base.is_dir() else [])
        for p in files:
            if p.relative_to(top).as_posix() in clean:
                continue
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if latest is None or m > latest:
                latest = m
    if clean:
        out = _git_lines(top, "log", "-1", "--format=%ct", "HEAD", "--", *sorted(clean))
        if not out:
            raise LookupError
        committed = float(out[0].strip())
        if latest is None or committed > latest:
            latest = committed
    return latest


def _latest_time(paths, top):
    """git 管理下ならコミット日時を使い、そうでなければ(判定不能も含め)mtime に戻す。"""
    if top is not None:
        try:
            return _latest_time_git(paths, top)
        except (LookupError, ValueError):
            pass
    return _latest_mtime(paths)


def _git_toplevel(root):
    out = _git_lines(root, "rev-parse", "--show-toplevel")
    if not out:
        return None
    try:
        return pathlib.Path(out[0].strip()).resolve()
    except OSError:
        return None


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
    root = root.resolve()
    top = _git_toplevel(root)
    result["last_decision_mtime"] = _latest_time(
        [root / "docs" / "interviews", root / "tests" / "e2e"], top)
    result["adr_mtime"] = _latest_time([root / "docs" / "adr.md", root / "docs" / "adr"], top)
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

    reason = nudge_reason(os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

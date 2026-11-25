#!/usr/bin/env python
"""spec_build.py — 仕様原稿から docs/spec.html を組み立てる。

モデルが書くのは中身だけ(行形式の原稿)で、HTML は 1 バイトもこのスクリプトが持つ。
章立て・列構成・記号・配色・目次・検索が原稿によってぶれないことを、構造として保証する。

    python spec_build.py --src docs/.spec-src.md --out docs/spec.html

原稿の書式は references/format.md を参照。検証に失敗した場合は理由を stderr に出して
終了コード 1 を返す(既存の spec.html は書き換えない)。
"""
import argparse
import datetime
import html
import pathlib
import re
import unicodedata
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ============================================================
# 固定の章立て。順序・名称・「この章が定義するもの」はここだけが持つ
# ============================================================
CHAPTERS = [
    (1, "文書情報", "本書の位置づけ、仕様の正、記号と ID の体系", True),
    (2, "用語定義", "本書で用いる語の定義", True),
    (3, "システム概要", "システムの目的と提供機能、ユースケース", True),
    (4, "システム構成", "構成要素、要素間の関係、動作環境", True),
    (5, "インターフェース仕様", "外部エンティティとのインターフェースの一覧と詳細", True),
    (6, "データ仕様", "データストアの所在・形式・項目定義と、データフロー", True),
    (7, "機能一覧", "機能の全件と確定状況", False),
    (8, "機能仕様", "機能ごとの実行契機・前提条件・処理・出力・エラー", True),
    (9, "エラー仕様", "エラーの判定条件と、発生時の動作", True),
    (10, "非機能要件", "機能に依らず常に成立する条件", True),
    (11, "適用範囲外", "意図的に対象としない事項", True),
    (12, "設計判断", "決定とその理由", True),
    (13, "仕様の確定状況", "各記述がどの根拠で固定されているか", False),
    (14, "未確定事項", "未確定の動作と、確定に必要な確認事項", True),
]
AUTHORED = [c for c in CHAPTERS if c[3]]

REQUIRED_SUBS = {
    3: ["概要", "ユースケース"],
    4: ["構成要素", "モジュール結合図", "動作環境"],
    # 境界図(@io)と経路ごとの仕様(@paths)は同じ章に置く。図の矢印と経路の記述が
    # 離れていると、どちらか片方だけ直されて食い違うため
    5: ["外部インターフェース一覧", "インターフェース詳細"],
}

# インターフェースの詳細(5.2)の項目。1 つのインターフェースは、1 対の要求と応答。
# 要求元と要求先は @peer と @if の行から入るため、原稿には書かせない
IF_AUTHORED_ROWS = ["要求", "応答", "手段", "形式", "契機", "用途", "エラー", "検証"]
IF_KINDS = ("受付", "発行")
# 要求のパラメータ(@param)と、応答の項目(@return)の列
PARAM_COLUMNS = ["名前", "型", "必須", "既定", "取りうる値", "説明"]
RETURN_COLUMNS = ["名前", "型", "説明"]
VERB_ENDS = "うくぐすつぬぶむる"

# @rows の列は章が決める。モデルは行だけを書く
ROW_COLUMNS = {
    9: (["ID", "判定条件", "メッセージ内容", "処理フロー"], True),
    10: (["ID", "分類", "条件"], True),
    11: (["ID", "対象外の事項", "理由"], False),
    12: (["ID", "決定", "理由"], False),
    14: (["ID", "項目", "現在の動作", "確定に必要な確認事項"], False),
}
ROW_PREFIX = {9: "E", 10: "N", 11: "X", 12: "D", 14: "U"}

SPEC_FIELDS = ["概要", "実行契機", "前提条件", "入力", "処理", "出力", "エラー", "検証",
               "実装", "読む", "書く"]
# 保守する人のための任意の項目。書かなければ行ごと出ない(既存の原稿はそのまま通る)
SPEC_OPTIONAL = ["実装", "読む", "書く"]
# --code を付けたときだけ足す章。原稿には書かない
CODE_CHAPTER = (15, "保守のための情報", "ソースコードの解析結果と、仕様書の記載との対応", False)
SPEC_ROWS = ["実行契機", "前提条件", "入力", "処理", "出力", "エラー", "検証"]
STATES = {"確定": "ok", "暫定": "tmp", "未固定": "non"}

ALLOWED = {
    1: {"def"},
    2: {"p", "def"},
    3: {"p", "def", "table", "purpose", "flow", "caption"},
    4: {"p", "def", "table", "graph", "code", "caption"},
    5: {"p", "def", "table", "code", "io", "paths", "caption"},
    6: {"p", "def", "table", "store", "schema"},
    8: {"p", "spec"},
    9: {"p", "rows", "impl"},
    10: {"p", "rows"},
    11: {"p", "rows"},
    12: {"p", "rows"},
    14: {"p", "rows"},
}

ID_RE = re.compile(r"(?<![0-9A-Za-z])([FENXDU]-\d{2})(?![0-9A-Za-z])")


class SpecError(Exception):
    """原稿が書式に従っていない。メッセージは行番号と直し方を含める。"""


def fail(line_no, message):
    raise SpecError("{0} 行目: {1}".format(line_no, message))


# ============================================================
# 解析
# ============================================================
class Block(object):
    def __init__(self, kind, line_no, head=""):
        self.kind = kind
        self.line_no = line_no
        self.rows = []
        self.head = head


class Section(object):
    def __init__(self, no, name, line_no):
        self.no = no
        self.name = name
        self.line_no = line_no
        self.blocks = []
        self.subs = []


def parse(text):
    meta = {}
    chapters = []
    cur_ch = None
    cur_sub = None
    block = None
    code_open = False

    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()

        if code_open:
            if line.strip() == "@end":
                code_open = False
                block = None
            elif line.strip().startswith("##"):
                # 閉じ忘れを、無関係な「章が足りない」ではなく原因の行で知らせる
                fail(block.line_no, "@code が @end で閉じられていません({0} 行目に見出しが来ました)。".format(line_no))
            else:
                block.rows.append(raw)
            continue

        stripped = line.strip()
        if stripped == "" or stripped.startswith("#!"):
            continue

        if cur_ch is None and not stripped.startswith("##"):
            if ":" not in stripped:
                fail(line_no, "章より前に書けるのは「キー: 値」の形のメタだけです。")
            key, value = stripped.split(":", 1)
            meta[key.strip()] = value.strip()
            continue

        if stripped.startswith("## "):
            m = re.match(r"^##\s+(\d+)\s+(.+)$", stripped)
            if not m:
                fail(line_no, "章見出しは「## <番号> <章名>」の形にしてください。")
            cur_ch = Section(int(m.group(1)), m.group(2).strip(), line_no)
            cur_sub = None
            block = None
            chapters.append(cur_ch)
            continue

        if stripped.startswith("### "):
            if cur_ch is None:
                fail(line_no, "節が章より先に現れています。")
            m = re.match(r"^###\s+([\d.]+)\s+(.+)$", stripped)
            if not m:
                fail(line_no, "節見出しは「### <番号> <節名>」の形にしてください。")
            cur_sub = Section(m.group(1), m.group(2).strip(), line_no)
            block = None
            cur_ch.subs.append(cur_sub)
            continue

        target = cur_sub or cur_ch

        if stripped.startswith("@"):
            name = stripped[1:].split(None, 1)[0]
            rest = stripped[1:][len(name):].strip()
            if cur_ch is None:
                fail(line_no, "@{0} が章の外にあります。".format(name))
            if name in ("p", "caption"):
                b = Block(name, line_no)
                if not rest:
                    fail(line_no, "@{0} には本文が必要です。".format(name))
                b.head = rest
                target.blocks.append(b)
                block = None
            elif name == "code":
                block = Block("code", line_no)
                target.blocks.append(block)
                code_open = True
            elif name == "end":
                fail(line_no, "@end に対応する @code がありません。")
            elif name == "store":
                block = None
                target.blocks.append(Block("store", line_no, head=rest))
            elif name == "purpose":
                # 直下に「誰が: …」などの行を持つ
                block = Block("purpose", line_no, head=rest)
                target.blocks.append(block)
            elif name in ("def", "table", "io", "graph", "flow", "rows", "spec",
                          "schema", "impl"):
                block = Block(name, line_no)
                block.head = rest
                target.blocks.append(block)
            elif name == "paths":
                # 経路ごとの仕様(5.2)。中身は @io が持つので、ここは差し込み位置だけを示す
                block = None
                target.blocks.append(Block("paths", line_no, head=rest))
            elif name in ("path", "params"):
                fail(line_no, "@{0} は使えません。インターフェースは「@peer 外部エンティティ」の下に"
                              "「@if 名称 | 受付か発行 | 書式」で 1 対の要求と応答ごとに書きます"
                              "(references/format.md の @io)。".format(name))
            elif name in ("peer", "if", "param", "return"):
                if block is None or block.kind != "io":
                    fail(line_no, "@{0} は @io の中だけで使えます。".format(name))
                block.rows.append(("@" + name, rest, line_no))
            elif name in ("hub", "left"):
                if block is None or block.kind != "graph":
                    fail(line_no, "@{0} は @graph の中だけで使えます。".format(name))
                block.rows.append(("@" + name, rest, line_no))
            elif name == "self":
                if block is None or block.kind != "io":
                    fail(line_no, "@self は @io の中だけで使えます。")
                block.rows.append(("@self", rest, line_no))
            else:
                fail(line_no, "知らない指示 @{0} です。".format(name))
            continue

        if block is None:
            fail(line_no, "どの指示にも属さない行です。@p などの指示の下に書いてください: {0}".format(stripped[:40]))
        block.rows.append((None, line, line_no))

    if code_open:
        raise SpecError("@code が @end で閉じられていません。")

    return meta, chapters


# ============================================================
# 検証
# ============================================================
def validate(meta, chapters):
    for key in ("題名", "対象", "生成元"):
        if not meta.get(key):
            raise SpecError("先頭のメタに「{0}:」がありません。".format(key))

    got = [(c.no, c.name) for c in chapters]
    want = [(no, name) for no, name, _, _ in AUTHORED]
    if [n for n, _ in got] != [n for n, _ in want]:
        raise SpecError(
            "章の番号・順序が固定の章立てと違います。\n"
            "  期待: {0}\n  実際: {1}\n"
            "第 7 章(機能一覧)と第 13 章(仕様の確定状況)は原稿に書きません。"
            "第 8 章と各章の内容から自動で作られます。".format(
                " ".join(str(n) for n, _ in want), " ".join(str(n) for n, _ in got) or "(なし)"
            )
        )
    for (no, name), (_, actual) in zip(want, got):
        if actual != name:
            raise SpecError("第 {0} 章の名称は「{1}」で固定です。実際は「{2}」でした。".format(no, name, actual))

    for ch in chapters:
        subs = REQUIRED_SUBS.get(ch.no)
        if subs is not None:
            got_subs = [s.name for s in ch.subs]
            want_no = ["{0}.{1}".format(ch.no, i + 1) for i in range(len(subs))]
            if [s.no for s in ch.subs] != want_no or got_subs != subs:
                raise SpecError(
                    "第 {0} 章の節は固定です。\n  期待: {1}\n  実際: {2}".format(
                        ch.no,
                        " / ".join("{0} {1}".format(a, b) for a, b in zip(want_no, subs)),
                        " / ".join("{0} {1}".format(s.no, s.name) for s in ch.subs) or "(なし)",
                    )
                )
        elif ch.subs:
            raise SpecError("第 {0} 章に節は置けません。".format(ch.no))

        for block in all_blocks(ch):
            if block.kind not in ALLOWED[ch.no]:
                fail(block.line_no, "第 {0} 章で @{1} は使えません。使えるのは {2} です。".format(
                    ch.no, block.kind, "・".join("@" + k for k in sorted(ALLOWED[ch.no]))))


def all_blocks(ch):
    for b in ch.blocks:
        yield b
    for sub in ch.subs:
        for b in sub.blocks:
            yield b


def chapter(chapters, no):
    for c in chapters:
        if c.no == no:
            return c
    return None


# ============================================================
# 中身の取り出し
# ============================================================
def split_kv(line, line_no):
    if ":" not in line:
        fail(line_no, "「キー: 値」の形で書いてください: {0}".format(line[:40]))
    k, v = line.split(":", 1)
    return k.strip(), v.strip()


def split_cells(line):
    return [c.strip() for c in line.split("|")]


def check_feature_name(name, line_no):
    """機能名は名詞で書く。文になっていると、一覧や図で読みにくく、名前として引けない。"""
    why = ""
    if name[-1] in VERB_ENDS:
        why = "「{0}」で終わっていて、文になっています".format(name[-1])
    elif "を" in name:
        why = "「を」が入っていて、文になっています"
    elif name.count("の") > 1:
        why = "「の」が {0} 回あります(修飾は 1 つまで)".format(name.count("の"))
    elif re.search(r"[、。(（「]", name):
        why = "読点・句点・かっこが入っています(補足は「概要:」に書きます)"
    if why:
        fail(line_no, "機能名「{0}」は名詞にしてください。{1}。"
                      "例: 「記録の読み込み」「一覧表示」「返却の取り消し」。".format(name, why))


def read_specs(chapters):
    """第 8 章の機能仕様を読み、ID 順の検証まで行う。"""
    specs = []
    ch = chapter(chapters, 8)
    for block in ch.blocks:
        if block.kind != "spec":
            continue
        head = split_cells(block.head)
        if len(head) != 2:
            fail(block.line_no, "@spec は「@spec F-01 機能名 | 確定」の形で書いてください。")
        m = re.match(r"^(F-\d{2})\s+(.+)$", head[0])
        if not m:
            fail(block.line_no, "@spec の先頭は「F-01 機能名」の形にしてください。")
        check_feature_name(m.group(2).strip(), block.line_no)
        state = head[1]
        if state not in STATES:
            fail(block.line_no, "確定状況は {0} のいずれかです。実際は「{1}」。".format(
                " / ".join(STATES), state))
        spec = {"id": m.group(1), "name": m.group(2).strip(), "state": state,
                "fields": {}, "line_no": block.line_no}
        for _, line, line_no in block.rows:
            key, value = split_kv(line, line_no)
            if key not in SPEC_FIELDS:
                fail(line_no, "機能仕様に書ける項目は {0} です。「{1}」は使えません。".format(
                    "・".join(SPEC_FIELDS), key))
            spec["fields"].setdefault(key, []).append((value, line_no))
        for key in ("概要", "実行契機", "入力", "出力", "検証"):
            if key not in spec["fields"]:
                fail(block.line_no, "{0} に「{1}:」がありません。該当が無い場合も「なし」と書いて項目を残します。".format(
                    spec["id"], key))
        for key in ("前提条件", "処理", "エラー"):
            spec["fields"].setdefault(key, [("なし", block.line_no)])
        specs.append(spec)

    if not specs:
        raise SpecError("第 8 章に @spec がありません。機能を 1 つ以上書いてください。")
    for index, spec in enumerate(specs, 1):
        want = "F-{0:02d}".format(index)
        if spec["id"] != want:
            fail(spec["line_no"], "機能の ID は工程順に F-01 から連番です。{0} 番目は {1} であるべきですが {2} でした。".format(
                index, want, spec["id"]))
    return specs


def read_rows(chapters):
    """ID 付き条項表を章ごとに読む。"""
    table = {}
    for no, (columns, has_verify) in ROW_COLUMNS.items():
        ch = chapter(chapters, no)
        rows = []
        for block in ch.blocks:
            if block.kind != "rows":
                continue
            for _, line, line_no in block.rows:
                cells = split_cells(line)
                want = len(columns) + (1 if has_verify else 0)
                if len(cells) != want:
                    fail(line_no, "第 {0} 章の行は {1} 列です({2})。実際は {3} 列でした。".format(
                        no, want, " | ".join(columns + (["検証"] if has_verify else [])), len(cells)))
                row = {"id": cells[0], "cells": cells[1:len(columns)], "line_no": line_no}
                if has_verify:
                    row["verify"] = cells[-1]
                rows.append(row)
        prefix = ROW_PREFIX[no]
        for index, row in enumerate(rows, 1):
            want = "{0}-{1:02d}".format(prefix, index)
            if row["id"] != want:
                fail(row["line_no"], "第 {0} 章の ID は {1}-01 から連番です。{2} 番目は {3} であるべきですが {4} でした。".format(
                    no, prefix, index, want, row["id"]))
        table[no] = rows
    return table


def parse_verify(value, line_no):
    """検証欄を (状態, テスト名の一覧) に分解する。"""
    value = value.strip()
    if value in ("なし", "-", ""):
        return "未固定", []
    state = "確定"
    if value.startswith("?"):
        state = "暫定"
        value = value[1:].strip()
    names = value.split()
    for name in names:
        if not name.isidentifier():
            fail(line_no, "検証欄にはテスト関数名だけを空白区切りで書きます。「{0}」は使えません。".format(name))
    if not names:
        fail(line_no, "検証欄が空です。テスト名を書くか「なし」と書いてください。")
    return state, names


# ============================================================
# 描画
# ============================================================
def esc(text):
    return html.escape(text, quote=False)


def anchor_of(code):
    return code.lower().replace("-", "")


def inline(text):
    out = esc(text)
    out = re.sub(r"`([^`]+)`", lambda m: "<code>{0}</code>".format(m.group(1)), out)
    out = re.sub(r"\*\*([^*]+)\*\*", lambda m: "<strong>{0}</strong>".format(m.group(1)), out)
    out = ID_RE.sub(lambda m: '<a href="#{0}">{1}</a>'.format(anchor_of(m.group(1)), m.group(1)), out)
    return out


def mark(state):
    return '<span class="dc-mk {0}">{1}</span>'.format(STATES[state], state)


def tag_table(head, body, cls=""):
    out = ['<div class="dc-tw">', '<table class="dc-t{0}">'.format(cls)]
    if head:
        out.append("<thead><tr>" + "".join(head) + "</tr></thead>")
    out.append("<tbody>" + "".join(body) + "</tbody>")
    out += ["</table>", "</div>"]
    return "\n".join(out)


def render_def(block):
    body = []
    for _, line, line_no in block.rows:
        key, value = split_kv(line, line_no)
        body.append('<tr><th>{0}</th><td class="dsc">{1}</td></tr>'.format(esc(key), inline(value)))
    return tag_table([], body)


def render_table(block, parts_table=False, extra=None):
    """自由列の表。parts_table は 4.1 構成要素の表(役割の書き方を確かめ、列を足せる)。"""
    if not block.head:
        fail(block.line_no, "@table には列名が必要です(例: @table 用途 | 可否 | 根拠)。")
    columns = split_cells(block.head)
    head = ["<th>{0}</th>".format(esc(c)) for c in columns]
    for name, _ in extra or ():
        head.append("<th>{0}</th>".format(esc(name)))
    role_at = columns.index("役割") if parts_table and "役割" in columns else -1
    body = []
    for _, line, line_no in block.rows:
        cells = split_cells(line)
        if len(cells) != len(columns):
            fail(line_no, "列数が見出しと合いません。見出しは {0} 列、この行は {1} 列です。".format(
                len(columns), len(cells)))
        if role_at >= 0:
            role = cells[role_at].rstrip("。")
            if not role or not (role[-1] in VERB_ENDS or role.endswith("ない")):
                fail(line_no, "構成要素の役割は「〜する」「〜を行う」のように動詞で終えてください: {0}".format(
                    cells[role_at][-24:]))
        tds = ['<td class="dsc">{0}</td>'.format(inline(c)) for c in cells]
        for _, cell in extra or ():
            tds.append('<td class="ids">{0}</td>'.format(cell(cells[0])))
        body.append("<tr>" + "".join(tds) + "</tr>")
    return tag_table(head, body)


PURPOSE_RE = re.compile(r"^(P-\d{2})\s+(.+)$")
PID_RE = re.compile(r"^P-\d{2}$")


def parse_purpose(block):
    """@purpose P-01 <目的名> を (ID, 名前) にする。"""
    m = PURPOSE_RE.match(block.head.strip())
    if not m:
        fail(block.line_no,
             "目的は「@purpose P-01 <目的名>」の形で書いてください: {0}".format(block.head[:40]))
    return m.group(1), m.group(2).strip()


PURPOSE_FIELDS = ["誰が", "対象", "概要"]


def purpose_fields(block):
    """ユースケースの説明。「誰が、対象に対して、概要を行う」と読める 3 つを必ず書く。"""
    pid, _ = parse_purpose(block)
    got = {}
    for _, line, line_no in block.rows:
        key, value = split_kv(line, line_no)
        if key not in PURPOSE_FIELDS:
            fail(line_no, "@purpose の下に書ける項目は {0} です。「{1}」は使えません。".format(
                "・".join(PURPOSE_FIELDS), key))
        if key in got:
            fail(line_no, "{0} の「{1}:」が 2 回あります。".format(pid, key))
        got[key] = value
    missing = [k for k in PURPOSE_FIELDS if k not in got]
    if missing:
        fail(block.line_no,
             "{0} に「{1}:」がありません。ユースケースは「誰が、対象に対して、概要を行う」と読めるように"
             "書きます。概要には、何のために何をするのかを書きます(処理の順番の要約は書きません)。".format(
                 pid, ":」「".join(missing)))
    return got


def render_purpose(block):
    """目的ごとの見出しは出さない。説明は 3.2 の先頭の一覧表にまとめる(build_pages が組む)。"""
    purpose_fields(block)
    return ""


def usecase_table(flows, flow_name):
    """3.2 のユースケースの一覧。対象ごとにまとめ、「誰が、対象に対して、概要を行う」と横に読める。"""
    head = ["<th>対象</th>", "<th>ID</th>", "<th>ユースケース</th>", "<th>アクター</th>", "<th>概要</th>",
            '<th class="st">処理フロー</th>']
    body = []
    for target, members in usecase_groups(flows):
        for index, flow in enumerate(members):
            got = flow["fields"]
            first = ('<td class="fn" rowspan="{0}">{1}</td>'.format(len(members), inline(target))
                     if index == 0 else "")
            body.append('<tr>{0}<td class="id" id="{1}">{2}</td><td class="fn">{3}</td><td class="dsc">{4}</td>'
                        '<td class="dsc">{5}</td><td class="st"><a href="{6}#fl-{1}">別紙</a></td></tr>'.format(
                            first, anchor_of(flow["pid"]), esc(flow["pid"]), inline(flow["name"]),
                            inline(got["誰が"]), inline(got["概要"]), esc(flow_name)))
    return tag_table(head, body)


def render_store(block):
    """@store <データの名前> | <保管場所> の見出し。"""
    parts = split_cells(block.head)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        fail(block.line_no,
             "データは「@store <名前> | <保管場所>」の形で書いてください: {0}".format(
                 block.head[:40]))
    return ('<h4 class="dc-st"><span class="nm">{0}</span>'
            '<span class="at">{1}</span></h4>').format(inline(parts[0]), inline(parts[1]))


SCHEMA_COLUMNS = ["データ名", "和名", "型", "下限値", "上限値", "単位",
                  "主キー", "外部キー", "参照先", "説明"]
SCHEMA_NUM = ("ln", "ln", "un")          # 下限値・上限値・単位は右寄せの細い列
SCHEMA_KEY = ("ky", "ky", "rf")          # 主キー・外部キー・参照先


def check_key_cells(pk, fk, ref, line_no):
    """主キー・外部キー・参照先の 3 列を検証し、参照先の一覧を返す。"""
    for name, cell in (("主キー", pk), ("外部キー", fk)):
        if cell not in ("○", "-"):
            fail(line_no, "{0}の列は ○ か - で書きます。「{1}」は使えません。".format(
                name, cell[:20]))
    if fk == "○" and ref == "-":
        fail(line_no, "外部キーには参照先を書きます(複数なら「箱, カタログ」)。")
    if fk == "-" and ref != "-":
        fail(line_no, "外部キーが - の行に参照先は書けません: {0}".format(ref[:20]))
    if ref == "-":
        return []
    out = []
    for one in ref.split(","):
        name = one.strip()
        if not name:
            continue
        # 末尾の + は「親 1 つにつき子が 1 件以上」を表す。既定は 0 件以上
        least_one = name.endswith("+")
        out.append((name.rstrip("+").strip(), least_one))
    return out


def render_schema(block, store="", links=None):
    """@schema。列は固定で、モデルは行だけを書く。"""
    # 見出しのクラスは本体の列と揃える(幅と揃えが列ごとに決まる)
    classes = ["", "", "ty", "ln", "ln", "un", "ky", "ky", "rf", ""]
    head = ['<th{0}>{1}</th>'.format(
        ' class="{0}"'.format(cls) if cls else "", name)
        for cls, name in zip(classes, SCHEMA_COLUMNS)]
    body = []
    for _, line, line_no in block.rows:
        cells = split_cells(line)
        if len(cells) != len(SCHEMA_COLUMNS):
            fail(line_no, "@schema の行は「{0}」の {1} 列です。この行は {2} 列でした。".format(
                " | ".join(SCHEMA_COLUMNS), len(SCHEMA_COLUMNS), len(cells)))
        refs = check_key_cells(cells[6], cells[7], cells[8], line_no)
        if links is not None:
            for ref, least_one in refs:
                links.append((store, ref, cells[0].strip(), line_no, least_one))
        body.append(
            '<tr><td class="nm">{0}</td><td class="dsc">{1}</td><td class="ty">{2}</td>'
            '<td class="ln">{3}</td><td class="ln">{4}</td><td class="un">{5}</td>'
            '<td class="ky">{6}</td><td class="ky">{7}</td><td class="rf">{8}</td>'
            '<td class="dsc">{9}</td></tr>'.format(*[inline(c) for c in cells]))
    if not body:
        fail(block.line_no, "@schema に行がありません。")
    return tag_table(head, body)


def render_code(block):
    return '<pre class="dc-pre">{0}</pre>'.format(esc("\n".join(block.rows).strip("\n")))


# ============================================================
# SVG 図の共通基盤
# 閲覧側のフォントは選べないので、幅は文字種から見積もる。
# 日本語は全角 1.0em でどのフォントでも安定し、英数字は等幅でも 0.55〜0.60em と
# 振れるため広い側(0.60em)で見積もる。見積もりが外れても textLength が
# はみ出しを防ぐ(字間がわずかに詰む/開くだけで箱は壊れない)。
# ============================================================
DG_FONT = 12.0          # 図のラベルの文字サイズ(px)
DG_LINE = 17.0          # 行の高さ(px)
DG_PADX = 11.0          # ノードの左右の余白
DG_PADY = 8.0           # ノードの上下の余白
DG_ONLINE = 8           # この字数までは線の上に載せる。超えたら線から離して置く
DG_MAX_ROWS = 6         # 片側 1 列あたりの行数。超えたら列を足して横に広げる
DG_COL_GAP = 26.0       # 列と列の間隔
DG_BEND = 26.0          # 迂回するとき、避ける箱から離す距離


def text_width(text, size=DG_FONT):
    """文字列の描画幅(px)を見積もる。"""
    total = 0.0
    for ch in text:
        total += 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.6
    return total * size


# 行を折ってよい位置。左にあるものほど優先して折る
BREAKS = (" / ", "、", "・", " ")


def split_chunks(text):
    """折ってよい位置で区切った「意味のまとまり」に分ける。

    走査は 1 回だけ行う。段階的に分割すると、いちど区切った塊が後段の
    区切り(空白など)で再び割れて「/」が行頭に残る。
    """
    chunks, cur, i = [], "", 0
    while i < len(text):
        hit = next((m for m in BREAKS if text.startswith(m, i)), None)
        if hit:
            chunks.append(cur + hit)
            cur = ""
            i += len(hit)
        else:
            cur += text[i]
            i += 1
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def wrap_piece(piece, max_width, size):
    """1 つのまとまりを、中黒・読点・空白で折る。それでも収まらなければ文字単位。"""
    lines, cur = [], ""
    for chunk in split_chunks(piece):
        if cur and text_width(cur + chunk, size) > max_width:
            lines.append(cur.strip())
            cur = ""
        if text_width(chunk, size) > max_width:
            for ch in chunk:
                if cur and text_width(cur + ch, size) > max_width:
                    lines.append(cur.strip())
                    cur = ""
                cur += ch
        else:
            cur += chunk
    if cur.strip():
        lines.append(cur.strip())
    return lines


def wrap_text(text, max_width, size=DG_FONT):
    """max_width(px)に収まるよう行に分ける。

    「/」で区切られた短文は行の境界として扱い、途中で切らない。
    短文自体が幅を超えるときだけ、その中を中黒・読点・文字の順で折る。
    """
    parts = text.split(" / ")
    lines = []
    for index, part in enumerate(parts):
        piece = part.strip() + (" /" if index < len(parts) - 1 else "")
        if not piece:
            continue
        if text_width(piece, size) <= max_width:
            lines.append(piece)
        else:
            lines.extend(wrap_piece(piece, max_width, size))
    return lines or [""]


def widest_chunk(text, size=DG_FONT):
    """主要な区切り(「/」)で切ったときの、最も広いまとまりの幅。

    辺の長さはこの幅で決める。中黒や読点は折り返しには使うが、
    ここでは数えない(細かく切れる位置まで見ると辺が不必要に短くなる)。
    """
    parts = text.split(" / ")
    mark = text_width(" / ", size)      # 区切り記号は前のまとまりの行末に付く
    return max([text_width(p.strip(), size) + (mark if i < len(parts) - 1 else 0.0)
                for i, p in enumerate(parts)] or [0.0])


def clip_segment(x1, y1, x2, y2, rect):
    """線分から矩形の内側を取り除き、残った区間を返す(Liang-Barsky)。"""
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - rect[0]), (dx, rect[2] - x1),
                 (-dy, y1 - rect[1]), (dy, rect[3] - y1)):
        if abs(p) < 1e-9:
            if q < 0:
                return [(x1, y1, x2, y2)]
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return [(x1, y1, x2, y2)]
            t0 = max(t0, r)
        else:
            if r < t0:
                return [(x1, y1, x2, y2)]
            t1 = min(t1, r)
    if t0 >= t1:
        return [(x1, y1, x2, y2)]
    out = []
    if t0 > 0.001:
        out.append((x1, y1, x1 + dx * t0, y1 + dy * t0))
    if t1 < 0.999:
        out.append((x1 + dx * t1, y1 + dy * t1, x2, y2))
    return out


def svg_text(x, y, lines, size=DG_FONT, cls="dg-t", anchor="middle"):
    """複数行のテキスト。textLength で見積もり幅に収める。"""
    out = []
    for index, line in enumerate(lines):
        width = text_width(line, size)
        out.append('<text class="{0}" x="{1:.1f}" y="{2:.1f}" text-anchor="{3}" '
                   'textLength="{4:.1f}" lengthAdjust="spacingAndGlyphs">{5}</text>'.format(
                       cls, x, y + index * DG_LINE, anchor, width, esc(line)))
    return "".join(out)


def place_side(items, heights, inner_edge, outward, col_w):
    """片側の要素を縦に並べ、行数が多ければ列を足す。

    列は内側から外側へ。行は列をまたいで通し番号で進めるので、外側の列の
    辺は内側の列の箱の「あいだ」を水平に通る(箱を貫かない)。
    戻り値は (座標を決めた高さ, 使った列数)。
    """
    if not items:
        return 0.0, 0
    cols = max(1, -(-len(items) // DG_MAX_ROWS))
    # 同じ列の中で重ならない間隔。列をまたぐ隣どうしは半分ずつずれる(千鳥)
    step = max(max(n.h, h) for n, h in zip(items, heights)) + 22.0
    tallest = 0.0
    for index, node in enumerate(items):
        col = index % cols
        offset = col * (col_w + DG_COL_GAP)
        node.x = (inner_edge - node.w - offset) if outward < 0 else (inner_edge + offset)
        node.y = index * step / cols
        tallest = max(tallest, node.y + max(node.h, heights[index]))
    return tallest, cols


def svg_frame(markup, title):
    """描いた要素をすべて含む viewBox を求めて svg 要素に包む。

    レイアウトの都合で要素が上や左へはみ出すことがある。想定値から枠を
    決めると、はみ出した分がそのまま切れる。描いたものから枠を決める。
    """
    xs, ys = [], []
    for m in re.finditer(r'<rect[^>]*x="([-\d.]+)" y="([-\d.]+)" '
                         r'width="([\d.]+)" height="([\d.]+)"', markup):
        x, y, w, h = (float(m.group(i)) for i in range(1, 5))
        xs += [x, x + w]
        ys += [y, y + h]
    for m in re.finditer(r'<text[^>]*x="([-\d.]+)" y="([-\d.]+)"[^>]*'
                         r'textLength="([\d.]+)"', markup):
        cx, base, tw = (float(m.group(i)) for i in range(1, 4))
        if 'text-anchor="start"' in m.group(0):
            xs += [cx, cx + tw]
        elif 'text-anchor="end"' in m.group(0):
            xs += [cx - tw, cx]
        else:
            xs += [cx - tw / 2, cx + tw / 2]
        ys += [base - 10.0, base + 3.0]
    for m in re.finditer(r'<line[^>]*x1="([-\d.]+)" y1="([-\d.]+)" '
                         r'x2="([-\d.]+)" y2="([-\d.]+)"', markup):
        x1, y1, x2, y2 = (float(m.group(i)) for i in range(1, 5))
        xs += [x1, x2]
        ys += [y1, y2]
    for m in re.finditer(r'<polygon[^>]*points="([^"]+)"', markup):
        for pair in m.group(1).split():
            px, py = pair.split(",")
            xs.append(float(px))
            ys.append(float(py))
    pad = 4.0
    x0, y0 = (min(xs) if xs else 0.0) - pad, (min(ys) if ys else 0.0) - pad
    w = ((max(xs) if xs else 0.0) + pad) - x0
    h = ((max(ys) if ys else 0.0) + pad) - y0
    return ('<svg class="dg" viewBox="{0:.1f} {1:.1f} {2:.1f} {3:.1f}" width="{2:.0f}" '
            'height="{3:.0f}" role="img" aria-label="{4}">{5}</svg>').format(
                x0, y0, w, h, esc(title), markup)


class DgNode(object):
    """図の箱。中身から寸法を決める。"""

    def __init__(self, label, note="", accent=False, max_width=150.0, role="peer"):
        self.role = role
        self.lines = wrap_text(label, max_width)
        self.note_lines = wrap_text(note, max_width) if note else []
        self.accent = accent
        widest = max([text_width(t) for t in self.lines]
                     + [text_width(t, 10.0) for t in self.note_lines] or [0])
        self.w = widest + DG_PADX * 2
        self.h = (len(self.lines) * DG_LINE
                  + len(self.note_lines) * 14.0 + DG_PADY * 2)
        self.x = 0.0
        self.y = 0.0

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2

    def stretch(self, height):
        """中心の要素を図の高さに合わせる(辺を水平に引けるようにする)。"""
        self.h = max(self.h, height)

    def render(self):
        cls = "dg-bx k" if self.accent else "dg-bx"
        out = ['<rect class="{0}" data-role="{5}" x="{1:.1f}" y="{2:.1f}" '
               'width="{3:.1f}" height="{4:.1f}"/>'.format(
                   cls, self.x, self.y, self.w, self.h, self.role)]
        body = len(self.lines) * DG_LINE + len(self.note_lines) * 14.0
        top = self.y + (self.h - body) / 2 + DG_FONT
        out.append(svg_text(self.cx, top, self.lines,
                            cls="dg-t k" if self.accent else "dg-t"))
        if self.note_lines:
            out.append(svg_text(self.cx, top + len(self.lines) * DG_LINE + 2,
                                self.note_lines, size=10.0, cls="dg-n"))
        return "".join(out)


# 向きの記号 → (種別, 表示する矢印)。相手の数も向きもシステムごとに変わる
def id_ranges(ids):
    """連続する ID を「I-01〜I-03」の形にまとめる。"""
    out, run = [], []
    for i in ids:
        n = int(i[2:])
        if run and n == run[-1] + 1:
            run.append(n)
        else:
            if run:
                out.append(run)
            run = [n]
    if run:
        out.append(run)
    return "・".join("I-{0:02d}".format(r[0]) if len(r) == 1
                    else "I-{0:02d}〜I-{1:02d}".format(r[0], r[-1]) for r in out)


def has_response(path):
    return path["fields"]["応答"][0][0].strip() not in ("なし", "-")


def parse_io(block):
    """@io を「自分・外部エンティティ・インターフェース」に分解する(純関数)。

    インターフェース 1 つは、1 対の要求と応答。ID(I-01…)は書かせず、並び順から機械が振る。
    @peer の直下に書いた項目は、その外部エンティティのインターフェースに共通の値になる
    (同じ手段や形式を、インターフェースごとに繰り返し書かせない)。手段・形式・契機・用途は、
    インターフェースに書けばそちらが優先される。エラーと検証は、共通の分がインターフェースの
    記載に足される(全インターフェースに共通するテストや、エラー時の共通の動作を 1 か所に書く)。
    """
    self_name, self_note, peers, paths = "", "", [], []
    cur = None
    shared = {}       # 外部エンティティ → 共通の項目

    for kind, line, line_no in block.rows:
        if kind == "@self":
            parts = split_cells(line)
            self_name, self_note = parts[0], (parts[1] if len(parts) > 1 else "")
            cur = None
            continue
        if kind == "@peer":
            name = line.strip()
            if not name:
                fail(line_no, "@peer には外部エンティティの名前を書いてください。")
            if name in peers:
                fail(line_no, "@peer「{0}」が 2 回書かれています。同じ外部エンティティの"
                              "インターフェースは 1 か所にまとめてください。".format(name))
            peers.append(name)
            cur = None
            continue
        if kind == "@if":
            if not peers:
                fail(line_no, "@if は「@peer 外部エンティティ」の下に書いてください。")
            # 書式には「|」を含む記述(ビット和など)がありうるので、3 列目は残り全部とする
            cells = [c.strip() for c in line.split("|", 2)]
            if len(cells) not in (2, 3) or not cells[0]:
                fail(line_no, "@if は「@if 名称 | 受付か発行 | 書式」の形で書きます(書式は省略可)。")
            if cells[1] not in IF_KINDS:
                fail(line_no, "@if の 2 列目は「受付」(外部エンティティからの要求を受け付ける)か"
                              "「発行」(外部エンティティへ要求を発行する)です。実際は「{0}」。".format(cells[1][:20]))
            cur = {"id": "I-{0:02d}".format(len(paths) + 1), "peer": peers[-1], "label": cells[0],
                   "kind": cells[1], "usage": cells[2] if len(cells) > 2 else "",
                   "fields": {}, "params": [], "returns": [], "line_no": line_no}
            paths.append(cur)
            continue
        if kind in ("@param", "@return"):
            if cur is None:
                fail(line_no, "{0} は @if の下に書いてください。".format(kind))
            columns = PARAM_COLUMNS if kind == "@param" else RETURN_COLUMNS
            cells = split_cells(line)
            if len(cells) != len(columns):
                fail(line_no, "{0} は {1} 列です({2})。実際は {3} 列でした。".format(
                    kind, len(columns), " | ".join(columns), len(cells)))
            if kind == "@param" and cells[2] not in ("必須", "任意"):
                fail(line_no, "@param の「必須」の列は「必須」か「任意」です。実際は「{0}」。".format(cells[2]))
            cur["params" if kind == "@param" else "returns"].append(cells)
            continue
        if not peers or ":" not in line:
            fail(line_no, "@io には「@self」「@peer 外部エンティティ」「@if 名称 | 受付か発行 | 書式」と、"
                          "その下の「項目: 値」を書きます: {0}".format(line[:40]))
        key, value = split_kv(line, line_no)
        if key not in IF_AUTHORED_ROWS:
            fail(line_no, "インターフェースに書ける項目は {0} です。「{1}」は使えません"
                          "(要求元と要求先は @peer と @if の行から入るので書きません)。".format(
                              "・".join(IF_AUTHORED_ROWS), key))
        if cur is None and key in ("要求", "応答"):
            fail(line_no, "「{0}:」は @if の下に、インターフェースごとに書いてください。".format(key))
        target = cur["fields"] if cur is not None else shared.setdefault(peers[-1], {})
        target.setdefault(key, []).append((value, line_no))

    if not self_name:
        fail(block.line_no, "@io には @self でこのシステム自身を書いてください。")
    if not peers:
        fail(block.line_no, "@io には「@peer」で外部エンティティを 1 つ以上書いてください。")
    for peer in peers:
        if not any(path["peer"] == peer for path in paths):
            fail(block.line_no, "@peer「{0}」にインターフェースがありません。"
                                "「@if 名称 | 受付か発行 | 書式」を書いてください。".format(peer))
    for path in paths:
        for key, values in shared.get(path["peer"], {}).items():
            own = path["fields"].get(key)
            if own is None:
                path["fields"][key] = values
            elif key == "エラー" and own[0][0].strip() not in ("なし", "-"):
                # エラーが無いインターフェースには、エラー時の共通の動作も足さない
                path["fields"][key] = own + values
            elif key == "検証":
                state, names = parse_verify(own[0][0], own[0][1])
                common = parse_verify(" ".join(v for v, _ in values).replace("?", " "), values[0][1])[1]
                if not names:
                    state = parse_verify(values[0][0], values[0][1])[0]
                merged = list(dict.fromkeys(names + common))
                path["fields"][key] = [(("? " if state == "暫定" else "") + " ".join(merged), own[0][1])]
        for key in IF_AUTHORED_ROWS:
            if key not in path["fields"]:
                fail(path["line_no"],
                     "{0}({1})に「{2}:」がありません。該当が無い場合も「なし」と書いて"
                     "項目を残します。".format(path["id"], path["label"], key))
        path["from"], path["to"] = ((path["peer"], self_name) if path["kind"] == "受付"
                                    else (self_name, path["peer"]))
    # 図の矢印は、外部エンティティごとに「システムへ入る向き」と「出る向き」の 2 本にまとめ、ID を付ける
    labelled = []
    for peer in peers:
        mine = [p for p in paths if p["peer"] == peer]
        accept = [p for p in mine if p["kind"] == "受付"]
        issue = [p for p in mine if p["kind"] == "発行"]
        recv = [(accept, "要求"), ([p for p in issue if has_response(p)], "応答")]
        send = [([p for p in accept if has_response(p)], "応答"), (issue, "要求")]
        texts = ["、".join("{0} {1}".format(id_ranges([p["id"] for p in group]), word)
                           for group, word in side if group) for side in (recv, send)]
        if texts[0] and texts[1]:
            labelled.append((peer, " ／ ".join(texts), "both"))
        else:
            labelled.append((peer, texts[0] or texts[1], "in" if texts[0] else "out"))
    return {"self_name": self_name, "self_note": self_note,
            "peers": labelled, "paths": paths}


def render_io(block, number, data=None):
    """5.1 の境界図。自分を中央に置き、外部エンティティを左右に振り分けて辺で結ぶ。

    相手の数・向きは原稿が決める。特定のシステム形状(一方向の変換など)を
    前提にした固定の段組みにしない。
    """
    data = data or parse_io(block)
    self_name, self_note = data["self_name"], data["self_note"]
    peers = data["peers"]

    # 左右に振り分ける。数だけで決め、置き場所に意味は持たせない
    half = (len(peers) + 1) // 2
    sides = [(p, "left") for p in peers[:half]] + [(p, "right") for p in peers[half:]]
    nodes = [(DgNode(p[0], max_width=130.0), p[1], p[2], side) for p, side in sides]
    hub = DgNode(self_name, self_note, accent=True, max_width=150.0, role="self")

    col_w = max([n.w for n, _, _, _ in nodes] or [90.0])
    # 辺の長さは、ラベルの最も広い「まとまり」が 1 行に収まるように決める。
    # 図が横に伸びすぎないよう上限を設け、超えるまとまりだけ文字単位で折る
    widest = max([widest_chunk(p[1], 10.0) for p in peers] or [0.0])
    gap = min(max(190.0, widest + 46.0), 330.0)
    left = [t for t in nodes if t[3] == "left"]
    right = [t for t in nodes if t[3] == "right"]

    def halves(label):
        """双方向のやり取りを、受信と送信の 2 本に分ける。"""
        return [t.strip() for t in label.split(" ／ ")]

    def rows_of(label):
        return max(len(wrap_text(t, gap - 46.0, 10.0)) for t in halves(label))

    def room_for(label):
        if len(halves(label)) == 2:
            return rows_of(label) * 24.0 + 30.0
        rows = len(wrap_text(label, gap - 46.0, 10.0))
        return rows * 12.0 + (14.0 if len(label) > DG_ONLINE else 10.0)

    for node, label, direction, _ in nodes:
        if direction == "both":
            node.stretch(rows_of(label) * 24.0 + 26.0)

    left_cols = max(1, -(-len(left) // DG_MAX_ROWS))
    left_span = col_w * left_cols + DG_COL_GAP * (left_cols - 1)
    h_left, _ = place_side([t[0] for t in left], [room_for(t[1]) for t in left],
                           left_span, -1, col_w)
    h_right, right_cols = place_side([t[0] for t in right], [room_for(t[1]) for t in right],
                                     left_span + gap * 2 + hub.w, 1, col_w)
    height = max(h_left, h_right, hub.h)
    for items, span in ((left, h_left), (right, h_right)):
        shift = (height - span) / 2
        for node, _, _, _ in items:
            node.y += shift
    hub.stretch(height)
    hub.x, hub.y = left_span + gap, (height - hub.h) / 2

    parts = [hub.render()]
    for items, at_left in ((left, True), (right, False)):
        for node, label, direction, side in items:
            parts.append(node.render())
            x1 = node.x + node.w if at_left else node.x
            x2 = hub.x if at_left else hub.x + hub.w
            # 接続点は相手の高さに合わせる。辺が水平になり、隣の辺やラベルと干渉しない
            if direction == "both":
                # 矢印は 1 本 1 方向。上が受信、下が送信で、それぞれに経路の ID が付く
                off = rows_of(label) * 6.0 + 7.0
                recv, send = halves(label)
                parts.append(edge_svg(x1, node.cy - off, x2, node.cy - off, "in", side, recv, gap, True))
                parts.append(edge_svg(x1, node.cy + off, x2, node.cy + off, "out", side, send, gap, True))
            else:
                parts.append(edge_svg(x1, node.cy, x2, node.cy, direction, side, label, gap))
    head = ["<th>ID</th>", "<th>名称</th>", "<th>要求元</th>", "<th>要求先</th>", "<th>要求</th>", "<th>応答</th>"]
    body = []
    for path in data["paths"]:
        body.append('<tr><td class="id"><a href="#{0}">{1}</a></td><td class="dsc">{2}</td>'
                    '<td class="dsc">{3}</td><td class="dsc">{4}</td><td class="dsc">{5}</td>'
                    '<td class="dsc">{6}</td></tr>'.format(
                        anchor_of(path["id"]), path["id"], inline(path["label"]),
                        esc(path["from"]), esc(path["to"]),
                        field_cell("要求", path["fields"]["要求"]),
                        field_cell("応答", path["fields"]["応答"])))
    return (fig("外部インターフェース", number, svg_frame("".join(parts), "外部インターフェース"))
            + tag_table(head, body))


def edge_svg(x1, y1, x2, y2, direction, side, label, gap, on_line=None):
    """向きを持つ辺。ラベルは辺の中点に置き、線はその手前で途切れさせる。"""
    marks = []
    if side == "left":
        if direction in ("in", "both"):
            marks.append(arrow_svg(x2, y2, 1))
        if direction in ("out", "both"):
            marks.append(arrow_svg(x1, y1, -1))
    else:
        if direction in ("in", "both"):
            marks.append(arrow_svg(x2, y2, -1))
        if direction in ("out", "both"):
            marks.append(arrow_svg(x1, y1, 1))

    lines = wrap_text(label, gap - 46.0, 10.0)
    mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
    widths = [text_width(t, 10.0) for t in lines]
    if on_line is None:
        on_line = len(label) <= DG_ONLINE

    if on_line:
        # 短いラベルは線の上に載せ、その分だけ線を切る。
        # 基準線ではなく文字の見た目の中心が線に重なる位置に置く
        top = mid_y - len(lines) * 12.0 / 2 + 9.0
    else:
        # 長いラベルは線に被せず、線の上側へ逃がす。
        # 辺は斜めなので中点だけでは足りない。ラベルが載る x の範囲で
        # 線がいちばん高くなる位置を見て、その上へ置く
        span = max(widths) / 2 + 5.0
        lo, hi = min(x1, x2), max(x1, x2)

        def line_y(x):
            x = max(lo, min(hi, x))
            if abs(x2 - x1) < 1e-9:
                return min(y1, y2)
            return y1 + (y2 - y1) * (x - x1) / (x2 - x1)

        highest = min(line_y(mid_x - span), line_y(mid_x + span))
        top = highest - 6.0 - (len(lines) - 1) * 12.0

    text = ""
    for index, line_text in enumerate(lines):
        text += ('<text class="dg-l" x="{0:.1f}" y="{1:.1f}" text-anchor="middle" '
                 'textLength="{2:.1f}" lengthAdjust="spacingAndGlyphs">{3}</text>').format(
                     mid_x, top + index * 12.0, widths[index], esc(line_text))
    path_id = re.match(r"^(I-\d{2})[ 〜・]", label)
    if path_id:
        # インターフェースの ID(範囲のときは先頭)から 5.2 の詳細へ飛べるようにする
        text = '<a href="#{0}">{1}</a>'.format(anchor_of(path_id.group(1)), text)

    if on_line:
        box = (mid_x - max(widths) / 2 - 5, top - 10.0,
               mid_x + max(widths) / 2 + 5, top + (len(lines) - 1) * 12.0 + 3.0)
        segs = clip_segment(x1, y1, x2, y2, box)
    else:
        segs = [(x1, y1, x2, y2)]
    drawn = "".join('<line class="dg-e" data-dir="{4}" x1="{0:.1f}" y1="{1:.1f}" '
                    'x2="{2:.1f}" y2="{3:.1f}"/>'.format(a, b, c, d, direction)
                    for a, b, c, d in segs)
    return drawn + "".join(marks) + text


def arrow_up_svg(x, y, facing):
    """縦の線の端の三角。facing は +1 で下向き、-1 で上向き。"""
    tip = y
    back = y - 8.0 * facing
    return '<polygon class="dg-a" points="{0:.1f},{1:.1f} {2:.1f},{3:.1f} {4:.1f},{3:.1f}"/>'.format(
        x, tip, x - 4.0, back, x + 4.0)


def arrow_svg(x, y, facing):
    """線の端の三角。facing は +1 で右向き、-1 で左向き。

    先端は箱の辺にちょうど接する位置に置く(内側に食い込ませない)。
    """
    tip = x
    back = x - 8.0 * facing
    return '<polygon class="dg-a" points="{0:.1f},{1:.1f} {2:.1f},{3:.1f} {2:.1f},{4:.1f}"/>'.format(
        tip, y, back, y - 4.0, y + 4.0)


def fig(title, number, inner, caption=""):
    out = ['<div class="dc-fig">',
           '<div class="dc-fig-h"><span>{0}</span><span class="no">図 {1}</span></div>'.format(
               esc(title), esc(number)),
           '<div class="dc-fig-b">{0}</div>'.format(inner)]
    if caption:
        out.append('<p class="dc-fig-c">{0}</p>'.format(inline(caption)))
    out.append("</div>")
    return "\n".join(out)


def render_graph(block, number, listed=(), auto_edges=(), outsiders=(), palette=None):
    """4.2 のモジュール結合図。中心は決め打ちにせず、交差が少ない配置を探す。

    auto_edges はソースの import から読んだ参照(--code のとき)。手で書いた線に足す。
    outsiders は 5 章に書いた外部エンティティと 6 章のデータストア。ここには描かせない。
    """
    hub_name, hub_note = "", ""
    edges = []
    for kind, line, line_no in block.rows:
        if kind == "@hub":
            parts = split_cells(line)
            hub_name, hub_note = parts[0], (parts[1] if len(parts) > 1 else "")
            continue
        if kind == "@left":
            continue          # 位置の指定は配置探索に任せる
        m = re.match(r"^(.+?)\s*->\s*(.+?)\s*:\s*(.+)$", line)
        if not m:
            fail(line_no, "関係は「A -> B: ラベル」の形で書いてください: {0}".format(line[:40]))
        edges.append((m.group(1).strip(), m.group(2).strip(), m.group(3).strip()))
    if not edges and not auto_edges:
        fail(block.line_no, "@graph には関係が 1 本も書かれていません。")
    for src, dst, _ in edges:
        for name in (src, dst):
            hit = [o for o in outsiders if o == name or o.startswith(name + " ")]
            if hit:
                fail(block.line_no,
                     "モジュール結合図に「{0}」があります。外部エンティティとデータストアはここに描きません。"
                     "やり取りは 5 章、データの出入りは 6 章のデータフロー図に書きます。".format(name))
    drawn_pairs = {(a, b) for a, b, _ in edges}
    for a, b in auto_edges:
        if (a, b) in drawn_pairs:
            continue
        edges.append((a, b, ""))

    names = []
    if hub_name:
        names.append(hub_name)
    for src, dst, _ in edges:
        for name in (src, dst):
            if name not in names:
                names.append(name)

    if listed:
        def plain(name):
            return name.strip().strip("`").strip()

        drawn = {plain(n) for n in names}
        missing = [name for name in listed if plain(name) not in drawn]
        if missing:
            fail(block.line_no,
                 "4.1 に挙げた要素が関係図にありません: {0}。"
                 "構成要素はすべて 4.2 に現れる必要があります。".format("・".join(missing)))
    return graph_svg(hub_name, hub_note, names, edges, "モジュール結合図", number,
                     palette=palette, roomy=True)


def seg_enters(seg, rect, margin=2.0):
    """線分が矩形の内側を通るか(輪郭に触れるだけの端点は数えない)。"""
    x1, y1, x2, y2 = seg
    rx1, ry1 = rect[0] + margin, rect[1] + margin
    rx2, ry2 = rect[2] - margin, rect[3] - margin
    if rx1 >= rx2 or ry1 >= ry2:
        return False
    dx, dy = x2 - x1, y2 - y1
    near, far = 0.0, 1.0
    for p, q in ((-dx, x1 - rx1), (dx, rx2 - x1), (-dy, y1 - ry1), (dy, ry2 - y1)):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            if r > far:
                return False
            near = max(near, r)
        else:
            if r < near:
                return False
            far = min(far, r)
    return far - near > 1e-6


def seg_cross(p, q):
    """2 線分が交差するか(端点の共有は交差としない)。"""
    def side(ax, ay, bx, by, cx, cy):
        v = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        return (v > 1e-9) - (v < -1e-9)

    if {(p[0], p[1]), (p[2], p[3])} & {(q[0], q[1]), (q[2], q[3])}:
        return False
    d1 = side(p[0], p[1], p[2], p[3], q[0], q[1])
    d2 = side(p[0], p[1], p[2], p[3], q[2], q[3])
    d3 = side(q[0], q[1], q[2], q[3], p[0], p[1])
    d4 = side(q[0], q[1], q[2], q[3], p[2], p[3])
    return d1 * d2 < 0 and d3 * d4 < 0


def layout_cost(order, cells, pairs, sizes=None):
    """配置の悪さを測る。交差を最優先で減らし、次に線を短くする。"""
    at = {}
    for index, name in enumerate(order):
        if name is not None:          # None は空けておく升目
            at[name] = cells[index]
    segs = [(at[a][0], at[a][1], at[b][0], at[b][1]) for a, b in pairs]
    crossings = 0
    for i, one in enumerate(segs):
        for other in segs[i + 1:]:
            if seg_cross(one, other):
                crossings += 1
    # ラベルは辺の中点に載る。中点が近すぎると文字どうしが重なる
    mids = [((seg[0] + seg[2]) / 2, (seg[1] + seg[3]) / 2) for seg in segs]
    crowded = 0
    for i, one in enumerate(mids):
        for other in mids[i + 1:]:
            gap = ((one[0] - other[0]) ** 2 + (one[1] - other[1]) ** 2) ** 0.5
            if gap < 96.0:
                crowded += 1
    # ラベルが箱の上に載ると読めない。実際の矩形で重なりを見る
    on_box = 0
    boxes = []
    placed = [(index, name) for index, name in enumerate(order) if name is not None]
    for index, name in placed:
        w, h = sizes.get(name, (130.0, 33.0)) if sizes else (130.0, 33.0)
        cx, cy = cells[index]
        boxes.append((cx - w / 2 - 6, cy - h / 2 - 6, cx + w / 2 + 6, cy + h / 2 + 6))
    for one in mids:
        label = (one[0] - 86.0, one[1] - 15.0, one[0] + 86.0, one[1] + 15.0)
        for box in boxes:
            if (label[0] < box[2] and box[0] < label[2]
                    and label[1] < box[3] and box[1] < label[3]):
                on_box += 1
    # 線が第三の要素の箱を突き抜けると、その要素が読めなくなる。交差より害が大きい
    box_of = dict(zip([name for _, name in placed], boxes))
    pierced = 0
    for (a, b), seg in zip(pairs, segs):
        for name, box in box_of.items():
            if name != a and name != b and seg_enters(seg, box):
                pierced += 1
    length = sum(((s[0] - s[2]) ** 2 + (s[1] - s[3]) ** 2) ** 0.5 for s in segs)
    return (pierced * 300000.0 + crossings * 100000.0
            + crowded * 4000.0 + on_box * 9000.0 + length)


def arrange_nodes(names, pairs, cells, sizes=None):
    """交差が少なく線が短くなる並びを探す。

    入れ替えて良くなるなら採る、を改善が止まるまで繰り返す。乱数を使わないので
    同じ原稿からは必ず同じ図が出る。
    """
    order = list(names)
    best = layout_cost(order, cells, pairs, sizes)
    improved = True
    while improved:
        improved = False
        for i in range(len(order)):
            for j in range(i + 1, len(order)):
                order[i], order[j] = order[j], order[i]
                cost = layout_cost(order, cells, pairs, sizes)
                if cost < best - 1e-9:
                    best, improved = cost, True
                else:
                    order[i], order[j] = order[j], order[i]
    return order, best


def graph_svg(hub_name, hub_note, names, edges, title, number, notation="arrow",
              least=None, palette=None, roomy=False):
    """要素を格子に置き、交差が最も少なくなる並びを探して辺で結ぶ。

    中心を決め打ちにしない。読みやすさの順は「交差が無い > 線が短い」。
    """
    # 同じ 2 要素の間の関係はまとめて 1 本の辺にする(往復は双方向として描く)
    bundle = {}
    for src, dst, label in edges:
        key = tuple(sorted((src, dst)))
        entry = bundle.setdefault(key, {"labels": [], "forward": False, "backward": False})
        entry["labels"].append(label)
        if (src, dst) == key:
            entry["forward"] = True
        else:
            entry["backward"] = True

    nodes = {}
    for name in names:
        nodes[name] = DgNode(name, hub_note if name == hub_name else "",
                             accent=(name == hub_name), max_width=132.0,
                             role="self" if name == hub_name else "peer")
        nodes[name].measure = None      # DgNode は生成時に寸法が決まる

    cols = max(1, int(len(names) ** 0.5 + 0.999))
    rows = max(1, -(-len(names) // cols))
    # roomy: 線の多い図(モジュール結合図)は間隔を広げ、線の交差が最も少ない配置を広く探す
    col_w = max(n.w for n in nodes.values()) + (230.0 if roomy else 196.0)
    row_h = max(n.h for n in nodes.values()) + (150.0 if roomy else 104.0)
    pairs = list(bundle.keys())
    sizes = dict((name, (nodes[name].w, nodes[name].h)) for name in names)

    def grid(c, r):
        return [((i % c) * col_w + col_w / 2, (i // c) * row_h + row_h / 2) for i in range(c * r)]

    cells = grid(cols, rows)
    order, best = arrange_nodes(names, pairs, cells, sizes)
    if roomy and len(pairs) > len(names):
        # 升目に空きを作り、形と出発点を変えて探す。乱数は使わない(同じ原稿からは同じ図)
        degree = dict((n, sum(1 for p in pairs if n in p)) for n in names)
        starts = [list(names), list(reversed(names)), sorted(names, key=lambda n: -degree[n]),
                  sorted(names, key=lambda n: degree[n])]
        for c, r in ((cols, rows + 1), (cols + 1, rows), (cols + 1, rows + 1)):
            trial_cells = grid(c, r)
            for start in starts:
                # 線の多いものを中央寄りに置いてから始める
                padded = [None] * len(trial_cells)
                slots = sorted(range(len(trial_cells)),
                               key=lambda i: abs(i % c - (c - 1) / 2) + abs(i // c - (r - 1) / 2))
                for slot, name in zip(slots, start):
                    padded[slot] = name
                trial, cost = arrange_nodes(padded, pairs, trial_cells, sizes)
                if cost < best - 1e-9:
                    order, best, cells = trial, cost, trial_cells
    for index, name in enumerate(order):
        if name is None:
            continue
        cx, cy = cells[index]
        nodes[name].x = cx - nodes[name].w / 2
        nodes[name].y = cy - nodes[name].h / 2
    order = [name for name in order if name is not None]

    def attr(text):
        return html.escape(text, quote=True)

    parts = []
    for name in order:
        bar = ""
        if palette is not None:
            bar = '<rect class="dg-cb" x="{0:.1f}" y="{1:.1f}" width="5.0" height="{2:.1f}" style="fill:{3}"/>'.format(
                nodes[name].x, nodes[name].y, nodes[name].h, palette.color(name))
        parts.append('<g class="gn" data-n="{0}">{1}{2}</g>'.format(attr(name), nodes[name].render(), bar))
    for (a, b), info in bundle.items():
        both = info["forward"] and info["backward"]
        # 結ぶ 2 つ以外の箱は避けて通る。配置で避けきれない組が残るため
        others = [(nodes[n].x - 6, nodes[n].y - 6,
                   nodes[n].x + nodes[n].w + 6, nodes[n].y + nodes[n].h + 6)
                  for n in order if n != a and n != b]
        src, dst = (b, a) if (info["backward"] and not info["forward"]) else (a, b)
        parts.append('<g class="ge" data-a="{0}" data-b="{1}" data-both="{2}">{3}</g>'.format(
            attr(src), attr(dst), "1" if both else "0",
            link_svg(nodes[a], nodes[b], " / ".join(l for l in info["labels"] if l),
                     both, info["backward"] and not info["forward"],
                     notation, bool(least and least.get((a, b))),
                     obstacles=others)))
    return fig(title, number, svg_frame("".join(parts), title))


def er_svg(stores, links, number):
    """データストアどうしの関係を、外部キーの記載から組み立てる。

    原稿に関係を書かせない。@schema の FK が唯一の出どころで、
    図と表が食い違わない。
    """
    edges, least = [], {}
    for src, dst, column, _, least_one in links:
        edges.append((src, dst, "{0} で参照".format(column)))
        if least_one:
            least[tuple(sorted((src, dst)))] = True
    return graph_svg("", "", list(stores), edges, "データの関係", number, "ie", least)


def touch_point(node, tx, ty):
    """相手の方向へ向かう、この箱の輪郭上の点を返す。"""
    cx, cy = node.cx, node.cy
    dx, dy = tx - cx, ty - cy
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return cx, cy
    half_w, half_h = node.w / 2 + 1.0, node.h / 2 + 1.0
    scale = min(half_w / abs(dx) if dx else 1e9, half_h / abs(dy) if dy else 1e9)
    return cx + dx * scale, cy + dy * scale


def bend_point(a, b, obstacles):
    """直線が箱を貫くとき、迂回して通る中間点を返す。避けきれなければ None。

    避ける箱の外側に候補を並べ、両端からその点まで箱に入らずに引けるものを選ぶ。
    候補は箱の位置から決まるので、同じ原稿からは必ず同じ図が出る。
    """
    best, best_len = None, None
    for box in obstacles:
        left, top, right, bottom = box
        mid_x, mid_y = (left + right) / 2, (top + bottom) / 2
        for px, py in ((left - DG_BEND, top - DG_BEND), (right + DG_BEND, top - DG_BEND),
                       (left - DG_BEND, bottom + DG_BEND), (right + DG_BEND, bottom + DG_BEND),
                       (mid_x, top - DG_BEND), (mid_x, bottom + DG_BEND),
                       (left - DG_BEND, mid_y), (right + DG_BEND, mid_y)):
            sx, sy = touch_point(a, px, py)
            ex, ey = touch_point(b, px, py)
            legs = ((sx, sy, px, py), (px, py, ex, ey))
            if any(seg_enters(leg, ob) for leg in legs for ob in obstacles):
                continue
            total = sum(((leg[0] - leg[2]) ** 2 + (leg[1] - leg[3]) ** 2) ** 0.5
                        for leg in legs)
            if best_len is None or total < best_len - 1e-9:
                best, best_len = (px, py), total
    return best


def path_mid(points):
    """折れ線の道のりの中央にあたる点。ラベルはここに載せる。"""
    legs = [((points[i + 1][0] - points[i][0]) ** 2
             + (points[i + 1][1] - points[i][1]) ** 2) ** 0.5
            for i in range(len(points) - 1)]
    half = sum(legs) / 2
    for index, leg in enumerate(legs):
        if half <= leg or index == len(legs) - 1:
            ratio = half / leg if leg else 0.0
            return (points[index][0] + (points[index + 1][0] - points[index][0]) * ratio,
                    points[index][1] + (points[index + 1][1] - points[index][1]) * ratio)
        half -= leg
    return points[0]


def link_svg(a, b, label, both, reverse, notation="arrow", least_one=False,
             obstacles=(), wrap=168.0):
    """2 つの箱を結ぶ。間に別の箱があれば、そこを避けて 1 か所で折れる。

    端の記号は記法で変える。折れたときは、その端に入ってくる向きに合わせる。
    """
    x1, y1 = touch_point(a, b.cx, b.cy)
    x2, y2 = touch_point(b, a.cx, a.cy)
    if reverse:
        x1, y1, x2, y2 = x2, y2, x1, y1
        a, b = b, a
    corners = [(x1, y1), (x2, y2)]
    if obstacles and any(seg_enters((x1, y1, x2, y2), ob) for ob in obstacles):
        via = bend_point(a, b, obstacles)
        if via:
            x1, y1 = touch_point(a, via[0], via[1])
            x2, y2 = touch_point(b, via[0], via[1])
            corners = [(x1, y1), via, (x2, y2)]
    lines = wrap_text(label, wrap, 10.0) if label.strip() else []
    mid_x, mid_y = path_mid(corners)
    top = mid_y - (len(lines) - 1) * 12.0 / 2 - 3.0
    # 行ごとに切る。全行を包む矩形で切ると、短い行の周りに線が残らず不自然になる
    segs = [(corners[i][0], corners[i][1], corners[i + 1][0], corners[i + 1][1])
            for i in range(len(corners) - 1)]
    for index, text in enumerate(lines):
        wide = text_width(text, 10.0)
        base = top + index * 12.0
        row_box = (mid_x - wide / 2 - 4, base - 9.0, mid_x + wide / 2 + 4, base + 3.0)
        segs = [piece for seg in segs for piece in clip_segment(seg[0], seg[1],
                                                                seg[2], seg[3], row_box)]
    out = "".join('<line class="dg-e" data-dir="{4}" x1="{0:.1f}" y1="{1:.1f}" '
                  'x2="{2:.1f}" y2="{3:.1f}"/>'.format(
                      seg[0], seg[1], seg[2], seg[3], "both" if both else "one")
                  for seg in segs)
    # 折れているときは、隣の角から入ってくる向きが端の向きになる
    in_end = (x2 - corners[-2][0], y2 - corners[-2][1])
    in_start = (x1 - corners[1][0], y1 - corners[1][1])
    if notation == "ie":
        # 参照する側が「多」、参照される側が「1」
        out += ie_many(x1, y1, in_start[0], in_start[1], least_one)
        out += ie_one(x2, y2, in_end[0], in_end[1])
    else:
        out += arrow_tip(x2, y2, in_end[0], in_end[1])
        if both:
            out += arrow_tip(x1, y1, in_start[0], in_start[1])
    for index, text in enumerate(lines):
        out += ('<text class="dg-l" x="{0:.1f}" y="{1:.1f}" text-anchor="middle" '
                'textLength="{2:.1f}" lengthAdjust="spacingAndGlyphs">{3}</text>').format(
                    mid_x, top + index * 12.0, text_width(text, 10.0), esc(text))
    return out


def ie_one(x, y, dx, dy):
    """IE 記法の「1」。線に垂直な短い横棒を 1 本置く。"""
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / length, dy / length
    bx, by = x - ux * 9.0, y - uy * 9.0
    return '<line class="dg-ie" x1="{0:.1f}" y1="{1:.1f}" x2="{2:.1f}" y2="{3:.1f}"/>'.format(
        bx - uy * 6.0, by + ux * 6.0, bx + uy * 6.0, by - ux * 6.0)


def ie_many(x, y, dx, dy, least_one=False):
    """IE 記法の「多」。鳥の足に、下限を表す印を添える。

    既定は 0 以上(○)。親を作った直後は子が 1 件も無いのがふつうなので、
    1 以上と読める描き方にしない。
    """
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / length, dy / length
    fx, fy = x - ux * 13.0, y - uy * 13.0
    out = ""
    for side in (-1.0, 0.0, 1.0):
        tx, ty = x + uy * 6.5 * side, y - ux * 6.5 * side
        out += '<line class="dg-ie" x1="{0:.1f}" y1="{1:.1f}" x2="{2:.1f}" y2="{3:.1f}"/>'.format(
            fx, fy, tx, ty)
    if least_one:
        out += '<line class="dg-ie" x1="{0:.1f}" y1="{1:.1f}" x2="{2:.1f}" y2="{3:.1f}"/>'.format(
            fx - uy * 6.0, fy + ux * 6.0, fx + uy * 6.0, fy - ux * 6.0)
    else:
        cx, cy = fx - ux * 5.0, fy - uy * 5.0
        out += '<circle class="dg-io0" cx="{0:.1f}" cy="{1:.1f}" r="4"/>'.format(cx, cy)
    return out


def arrow_tip(x, y, dx, dy):
    """任意の向きの三角。線の傾きに合わせる。"""
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / length, dy / length
    bx, by = x - ux * 9.0, y - uy * 9.0
    return '<polygon class="dg-a" points="{0:.1f},{1:.1f} {2:.1f},{3:.1f} {4:.1f},{5:.1f}"/>'.format(
        x, y, bx - uy * 4.0, by + ux * 4.0, bx + uy * 4.0, by - ux * 4.0)


# ============================================================
# 3.2 処理の流れ — 木に読み、座標を決め、SVG を描く
# ============================================================
FLOW_KINDS = ("端子", "工程", "判断", "枝", "終了", "待機",
              "繰り返し", "繰り返し終わり", "例外", "例外終わり",
              "並行", "合流", "順不同", "順不同終わり", "経路")


class FlowNode(object):
    """流れの 1 要素。ブロック構造は children / lanes に入る。"""

    def __init__(self, kind, text, ids=(), line_no=0, label=""):
        self.kind = kind
        self.text = text
        self.ids = list(ids)
        self.line_no = line_no
        self.label = label        # 分岐の枝ラベル、繰り返しの条件
        self.children = []        # 分岐の中身、繰り返しの中身、例外の中身
        self.exception = None     # この工程から離脱する例外
        self.else_label = ""
        self.post = False         # 繰り返しの条件を後ろに書いたか(do-while)
        self.eids = []            # 例外が指すエラー ID
        self.owner = None         # 例外が付く工程
        self.lanes = []           # 並行・順不同のレーン
        self.sid = ""
        self.bid = ""             # 分岐の ID(別紙の図と説明を結ぶ。「br-P16-a」)


def parse_flow_rows(rows, block_line):
    """行を読んで木にする。ブロック構造(繰り返し・並行・例外)はスタックで畳む。"""
    root = []
    stack = [(None, root)]        # (開いているノード, 追加先リスト)
    open_decision, awaiting, in_branch = None, None, False

    def target():
        holder, lane = stack[-1]
        if lane is None:
            fail(holder.line_no, "{0} の直後は 経路 です。".format(holder.kind))
        return lane

    for _, raw, line_no in rows:
        line = raw.strip()
        if not line:
            continue
        kind = line.split(None, 1)[0]
        rest = line[len(kind):].strip()
        if kind not in FLOW_KINDS:
            fail(line_no, "工程の種類は {0} のいずれかです: {1}".format(
                " / ".join(FLOW_KINDS), line[:30]))

        if kind == "枝":
            if open_decision is None:
                fail(line_no, "枝 は 判断 の下にだけ置けます。対応する 判断 がありません。")
            if not rest:
                fail(line_no, "枝 にはラベルが必要です(例: 枝 はい)。")
            if not in_branch:
                in_branch, awaiting = True, None
                open_decision.label = rest
            else:
                in_branch = False
                open_decision.else_label = rest
                open_decision = None
            continue
        if awaiting is not None:
            fail(line_no, "判断 の次の行は 枝 です。分岐に入る側のラベルを先に書いてください。")

        if kind in ("合流", "順不同終わり"):
            want = "並行" if kind == "合流" else "順不同"
            if stack[-1][0] is None or stack[-1][0].kind != want:
                fail(line_no, "{0} に対応する {1} がありません。".format(kind, want))
            node = stack.pop()[0]
            if len(node.lanes) < 2:
                fail(line_no, "{0} には経路が 2 つ以上必要です。".format(want))
            for lane in node.lanes:
                if not lane:
                    fail(line_no, "中身のない経路があります。")
            continue

        if kind == "経路":
            holder = stack[-1][0]
            if holder is None or holder.kind not in ("並行", "順不同"):
                fail(line_no, "経路 は 並行 か 順不同 の中だけで使えます。")
            holder.lanes.append([])
            stack[-1] = (holder, holder.lanes[-1])
            continue

        if kind == "例外終わり":
            if stack[-1][0] is None or stack[-1][0].kind != "例外":
                fail(line_no, "例外終わり に対応する 例外 がありません。")
            node = stack.pop()[0]
            if not node.children:
                fail(line_no, "例外の中に処理がありません。")
            continue

        if kind == "繰り返し終わり":
            if stack[-1][0] is None or stack[-1][0].kind != "繰り返し":
                fail(line_no, "繰り返し終わり に対応する 繰り返し がありません。")
            node = stack.pop()[0]
            if rest:
                if node.label:
                    fail(line_no, "繰り返しの条件は始まりか終わりのどちらか一方に書きます。")
                node.label, node.post = rest, True
            elif not node.label:
                fail(line_no, "繰り返しの条件がありません。"
                              "「繰り返し <条件>」(前判定)か「繰り返し終わり <条件>」(後判定)で書きます。")
            if not node.children:
                fail(line_no, "繰り返しの中に工程がありません。")
            continue

        # 置ける場所かどうかを先に見る。ID の形より前に判定しないと、
        # 「分岐の中に例外は書けません」に辿り着けない
        if kind in ("並行", "順不同") and in_branch:
            fail(line_no, "分岐の中に {0} は書けません。".format(kind))
        if kind == "繰り返し" and in_branch:
            fail(line_no, "分岐の中に繰り返しは書けません。")

        text, ids = rest, []
        if "|" in rest:
            text, tail = [c.strip() for c in rest.split("|", 1)]
            ids = tail.split()
            want = r"^E-\d{2}$" if kind == "例外" else r"^F-\d{2}$"
            shape = "E-01" if kind == "例外" else "F-01"
            for fid in ids:
                if not re.match(want, fid):
                    fail(line_no, "{1} に添える ID は {2} の形です。「{0}」は使えません。".format(
                        fid, "例外" if kind == "例外" else "工程", shape))

        if kind == "例外" and "|" not in rest:
            fail(line_no, "例外には対応するエラー ID を添えます(例: 例外 通信に失敗した | E-05)。")

        node = FlowNode(kind, text, ids, line_no)
        if kind in ("並行", "順不同"):
            if in_branch:
                fail(line_no, "分岐の中に {0} は書けません。".format(kind))
            target().append(node)
            stack.append((node, None))
            continue
        inside_exception = stack[-1][0] is not None and stack[-1][0].kind == "例外"
        if kind == "例外":
            # 分岐の枝の中の工程からも離脱できる(枝の工程は判断の children に入っている)
            siblings = open_decision.children if (in_branch and not inside_exception) else target()
            host = siblings[-1] if siblings else None
            if host is None or host.kind not in ("工程", "待機"):
                fail(line_no, "例外 は工程か待機の直後に書きます。離脱元がありません。")
            if host.exception is not None:
                fail(line_no, "1 つの工程に例外は 1 つだけです。複数のエラーで離脱するなら "
                              "1 つの例外に ID を並べます(例: 例外 入力が不正 | E-01 E-02)。")
            node.eids, node.ids = list(node.ids), []
            node.owner = host
            host.exception = node
            stack.append((node, node.children))
            continue
        if kind == "繰り返し":
            node.label = text
            node.text = ""
            if in_branch:
                fail(line_no, "分岐の中に繰り返しは書けません。")
            target().append(node)
            stack.append((node, node.children))
            continue
        if in_branch and not inside_exception:
            open_decision.children.append(node)
        else:
            target().append(node)
            if kind == "判断":
                if stack[-1][0] is not None:
                    fail(line_no, "繰り返しの中に判断は書けません。")
                open_decision = awaiting = node

    if in_branch or awaiting is not None:
        fail(block_line, "判断 の分岐が閉じていません。枝 は 2 回(分岐に入る側・本線に戻る側)書きます。")
    if len(stack) > 1:
        opened = stack[-1][0].kind
        fail(block_line, "{0} が閉じていません。{0}終わり を書いてください。".format(
            "繰り返し" if opened == "繰り返し" else "例外"))
    if not root:
        fail(block_line, "@flow に工程がありません。")
    for node in root:
        if node.kind == "判断" and not node.children:
            fail(block_line, "判断 に分岐の中身がありません。枝 の下に工程を 1 つ以上書いてください。")
    return root


def read_flow(block, pid):
    """1 つの目的の流れを読む。工程の番号は目的ごとに 01 から振る(図は別紙の側で描く)。

    戻り値は (機能 ID ごとの工程, 流れに出てくるエラー ID, 工程の並び, 木)。
    """
    items = parse_flow_rows(block.rows, block.line_no)

    def main_line(nodes):
        """番号を持つ工程を、読む順に返す。分岐の枝・繰り返し・並行の中の工程も含む。"""
        for node in nodes:
            if node.kind == "繰り返し":
                for inner in main_line(node.children):
                    yield inner
            elif node.kind in ("並行", "順不同"):
                for lane in node.lanes:
                    for inner in main_line(lane):
                        yield inner
            elif node.kind == "判断":
                for inner in main_line(node.children):
                    yield inner
            elif node.kind == "工程":
                yield node

    steps, counter, owners = {}, [0], []
    for node in main_line(items):
        counter[0] += 1
        node.sid = "st-{0}-{1:02d}".format(pid.replace("-", ""), counter[0])
        for index, fid in enumerate(node.ids):
            entry = steps.setdefault(fid, {"sids": [], "nested": True, "owner": ""})
            entry["sids"].append(node.sid)
            if index == 0:
                entry["nested"] = False
            else:
                entry["owner"] = node.ids[0]
        if node.ids:
            owners.append((node.ids[0], node.line_no))

    prev = ""
    for owner, line_no in owners:
        if owner < prev:
            fail(line_no, "機能 ID は処理の流れの順に振ります。{0} が {1} より後に現れています。".format(
                owner, prev))
        prev = owner

    def used_errors(nodes):
        for node in nodes:
            if node.exception:
                for eid in node.exception.eids:
                    yield eid
                for inner in used_errors(node.exception.children):
                    yield inner
            for lane in node.lanes:
                for inner in used_errors(lane):
                    yield inner
            for inner in used_errors(node.children):
                yield inner

    ordered = [(node.sid, node.text, list(node.ids),
                list(node.exception.eids) if node.exception else [],
                node.exception.text if node.exception else "")
               for node in main_line(items)]
    return steps, set(used_errors(items)), ordered, items


def field_cell(key, values):
    """項目 1 つ分の値セル。@spec(第 8 章)と @path(5.2)で共通に使う。

    同じ項目を複数行書いたら箇条書き、「なし」は薄く、検証はテスト名の並びにする。
    """
    texts = [v for v, _ in values]
    if key == "検証":
        state, names = parse_verify(texts[0], values[0][1])
        if not names:
            return '<span class="none">なし</span>'
        return '<p class="dc-ev{0}">{1}</p>'.format(
            " tmp" if state == "暫定" else "", "　".join(esc(n) for n in names))
    if len(texts) > 1:
        return "<ol>" + "".join("<li>{0}</li>".format(inline(t)) for t in texts) + "</ol>"
    if texts[0] in ("なし", "-"):
        return '<span class="none">なし</span>'
    return inline(texts[0])


def render_spec(spec, code=None):
    head = ('<div class="dc-blk-h" id="{0}"><span class="id">{1}</span>'
            '<span class="nm">{2}</span>{3}</div>').format(
        anchor_of(spec["id"]), spec["id"], esc(spec["name"]), mark(spec["state"]))
    rows = []
    for key in SPEC_ROWS:
        values = spec["fields"].get(key, [("なし", 0)])
        rows.append("<dt>{0}</dt><dd>{1}</dd>".format(esc(key), field_cell(key, values)))
    for key in ("読む", "書く"):
        if key in spec["fields"]:
            rows.append("<dt>{0}</dt><dd>{1}</dd>".format(
                {"読む": "参照データ", "書く": "更新データ"}[key], "、".join(esc(v) for v, _ in spec["fields"][key])))
    if spec.get("impl"):
        rows.append("<dt>実装</dt><dd>{0}</dd>".format(render_refs(spec["impl"], code)))
    return '<div class="dc-blk">{0}<dl class="dc-rows">{1}</dl></div>'.format(head, "".join(rows))


def render_paths(data, line_no):
    """5.2 インターフェースの詳細。第 8 章の機能仕様と同じブロック形式で並べる。"""
    if not data:
        fail(line_no, "@paths を書くには、同じ章に @io が必要です。")

    def table(columns, lines):
        body = []
        for cells in lines:
            tds = ['<td class="id">{0}</td>'.format(inline(cells[0]))]
            tds += ['<td class="dsc">{0}</td>'.format(
                '<span class="none">なし</span>' if c in ("-", "なし") else inline(c)) for c in cells[1:]]
            body.append("<tr>" + "".join(tds) + "</tr>")
        return '<div class="dc-prm">{0}</div>'.format(
            tag_table(["<th>{0}</th>".format(c) for c in columns], body))

    blocks = []
    for path in data["paths"]:
        head = ('<div class="dc-blk-h" id="{0}"><span class="id">{1}</span>'
                '<span class="nm">{2}</span><span class="dc-mk">{3}</span></div>').format(
            anchor_of(path["id"]), path["id"], esc(path["label"]), path["kind"])
        rows = ["<dt>要求元</dt><dd>{0}</dd>".format(esc(path["from"])),
                "<dt>要求先</dt><dd>{0}</dd>".format(esc(path["to"]))]
        if path["usage"]:
            rows.append('<dt>書式</dt><dd><p class="dc-prm-u">{0}</p></dd>'.format(inline(path["usage"])))
        for key in IF_AUTHORED_ROWS:
            rows.append("<dt>{0}</dt><dd>{1}</dd>".format(esc(key), field_cell(key, path["fields"][key])))
            if key == "要求" and path["params"]:
                rows.append("<dt>要求パラメータ</dt><dd>{0}</dd>".format(table(PARAM_COLUMNS, path["params"])))
            if key == "応答" and path["returns"]:
                rows.append("<dt>応答項目</dt><dd>{0}</dd>".format(table(RETURN_COLUMNS, path["returns"])))
        blocks.append('<div class="dc-blk">{0}<dl class="dc-rows">{1}</dl></div>'.format(
            head, "".join(rows)))
    return "".join(blocks)


FLOW_FIXED = "流れ不変"


def check_error_flow(rows, seen, line_hint=0):
    """9 章の「流れ」列を確かめる。

    第 8 章の機能が流れに現れないとビルドが止まるのに、エラーは野放しだった。
    流れが変わるエラーは図に出す。変わらないものは「流れ不変」と書いて残す。
    見ていないのか、見た上で描かないと決めたのかを区別できるようにする。
    """
    for row in rows:
        cells = row["cells"]
        note = cells[-1].strip() if cells else ""
        if note == FLOW_FIXED:
            continue
        if not note:
            fail(row["line_no"],
                 "{0} の「流れ」列が空です。3.2 のどの流れで起きるかを書くか、"
                 "処理の流れが変わらないなら「{1}」と書いてください。".format(
                     row["id"], FLOW_FIXED))
        if row["id"] not in seen:
            fail(row["line_no"],
                 "{0} は「{1}」と書かれていますが、3.2 のどの流れにも現れません。"
                 "工程に「例外 <条件> | {0}」を添えるか、「{2}」と書いてください。".format(
                     row["id"], note, FLOW_FIXED))


def render_rows_fixed(no, rows, where=None, code=None):
    """ID 付き条項表。列は章が決め、検証つきの章は条件セルにテスト名を添える。

    where は第 9 章の @impl(エラー ID → 発生箇所)。あるときだけ「発生箇所」の列を足す。
    """
    columns, has_verify = ROW_COLUMNS[no]
    head = ["<th>{0}</th>".format(esc(c)) for c in columns]
    if where:
        head.append("<th>発生箇所</th>")
    if has_verify:
        head.append('<th class="jd">判定</th>')
    body = []
    for row in rows:
        tds = ['<td class="id" id="{0}">{1}</td>'.format(anchor_of(row["id"]), row["id"])]
        verify_at = len(row["cells"]) - (2 if no == 9 else 1)
        for index, cell in enumerate(row["cells"]):
            text = inline(cell)
            if has_verify and index == verify_at:
                state, names = parse_verify(row["verify"], row["line_no"])
                ev = "　".join(esc(n) for n in names) if names else "対応する試験なし（決定は 11 章）"
                text += '<span class="ev">{0}</span>'.format(ev)
            tds.append('<td class="dsc">{0}</td>'.format(text))
        if where:
            refs = where.get(row["id"])
            tds.append('<td class="dsc">{0}</td>'.format(
                render_refs(refs, code) if refs else '<span class="none">なし</span>'))
        if has_verify:
            state, _ = parse_verify(row["verify"], row["line_no"])
            tds.append('<td class="jd">{0}</td>'.format(mark(state)))
        body.append("<tr>" + "".join(tds) + "</tr>")
    return tag_table(head, body)


# ============================================================
# 実装への紐づけ(任意)。原稿に関数の場所を書き、--code でソースを渡すと、
# 実在の検査・コードの抜粋・内部の構成図・保守のための情報の章が加わる。
# ソースを正とする情報はここだけが扱い、モデルの書いた説明は足さない
# ============================================================
SF_COLORS = ["#8a5a00", "#a63d1f", "#1f6f8b", "#2d7a3e", "#7a3fa0", "#1f4e9c", "#00756a",
             "#666e7a", "#b0306a", "#5b6475"]
REF_RE = re.compile(r"^([^\s:]+):([A-Za-z_][\w.\-]*)$")
def parse_refs(value, line_no):
    """「storage.py:load records.py:add」を [(ファイル, 関数)] にする。"""
    refs = []
    for token in value.split():
        m = REF_RE.match(token)
        if not m:
            fail(line_no, "実装は「ファイル:関数」を空白区切りで書きます(例: storage.py:load)。"
                          "「{0}」は読めません。".format(token[:40]))
        refs.append((m.group(1), m.group(2)))
    if not refs:
        fail(line_no, "実装が空です。書かないなら行ごと消してください。")
    return refs


def read_impl(chapters, rows9):
    """第 9 章の @impl(エラー ID | 発生箇所)を読む。"""
    where = {}
    known = {row["id"] for row in rows9}
    for block in chapter(chapters, 9).blocks:
        if block.kind != "impl":
            continue
        for _, line, line_no in block.rows:
            cells = split_cells(line)
            if len(cells) != 2:
                fail(line_no, "@impl の行は「E-01 | ファイル:関数」の 2 列です。")
            if cells[0] not in known:
                fail(line_no, "@impl が {0} を指していますが、第 9 章にその ID がありません。".format(cells[0]))
            if cells[0] in where:
                fail(line_no, "@impl に {0} が 2 回あります。".format(cells[0]))
            where[cells[0]] = parse_refs(cells[1], line_no)
    return where


class Palette(object):
    """モジュールの色。現れた順に割り当て、モジュール結合図と別紙の流れ図で同じ色を使う。"""

    def __init__(self, names=()):
        self.order = []
        for name in names:
            self.color(name)

    @staticmethod
    def key(name):
        return name.strip().strip("`").strip()

    def color(self, name):
        key = self.key(name)
        if key not in self.order:
            self.order.append(key)
        return SF_COLORS[self.order.index(key) % len(SF_COLORS)]


def load_analyzers(extra_dirs=()):
    """言語ごとの解析部品を読み込む。{拡張子: 部品}。

    部品は 1 言語 1 ファイルの Python モジュールで、このスクリプトの隣の analyzers/ に置く。
    extra_dirs(--analyzers、または原稿の隣の .spec-analyzers/)にある部品は、同じ拡張子の
    同梱の部品より優先する。利用者が言語を足す・差し替えるための口。
    """
    import importlib.util
    found = {}
    here = pathlib.Path(__file__).with_name("analyzers")
    for folder in [here] + [pathlib.Path(d) for d in extra_dirs]:
        if not folder.is_dir():
            continue
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))      # 部品どうしで小道具(_blocks など)を使えるように
        for path in sorted(folder.glob("*.py")):
            if path.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location("spec_analyzer_" + path.stem, str(path))
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as error:      # 壊れた部品は、どれが壊れているかを伝えて止める
                raise SpecError("解析部品 {0} を読み込めません: {1}".format(path, error))
            for need in ("NAME", "EXTENSIONS", "locate", "doc", "references", "facts"):
                if not hasattr(module, need):
                    raise SpecError("解析部品 {0} に {1} がありません(references/analyzers.md の決まり)。".format(
                        path, need))
            for ext in module.EXTENSIONS:
                found[ext.lower()] = module
    return found


class CodeBase(object):
    """--code で渡されたソースのフォルダ。読むだけで、書き換えない。

    どの言語を調べられるかは、読み込んだ解析部品で決まる。フォルダにあるファイルの拡張子ごとに
    部品へ渡し、部品の無い拡張子のファイルは、名前がファイルの中にあるかだけを確かめる。
    """

    def __init__(self, root, analyzers=None):
        self.root = pathlib.Path(root)
        if not self.root.is_dir():
            raise SpecError("--code のフォルダが見つかりません: {0}".format(root))
        self.analyzers = analyzers if analyzers is not None else load_analyzers()
        self._text = {}

    def analyzer(self, name):
        return self.analyzers.get(pathlib.PurePosixPath(name).suffix.lower())

    def text(self, name, line_no=0):
        if name not in self._text:
            path = self.root / name
            try:
                raw = path.read_bytes()
            except OSError:
                fail(line_no, "実装のファイル {0} を読めません({1} の中を探しました)。".format(name, self.root))
            for encoding in ("utf-8-sig", "utf-16", "cp932"):
                try:
                    self._text[name] = raw.decode(encoding)
                    break
                except UnicodeError:
                    continue
            else:
                fail(line_no, "実装のファイル {0} の文字コードを判別できません。".format(name))
        return self._text[name]

    def _run(self, name, call, line_no=0):
        """部品を呼ぶ。読めないソース(文法の誤りなど)は、原因の分かる文にして止める。"""
        try:
            return call(self.text(name, line_no))
        except SyntaxError as error:
            fail(line_no, "{0} を {1} として読めません({2} 行目)。".format(
                name, self.analyzer(name).NAME, error.lineno))

    def locate(self, name, func, line_no=0):
        """関数の (開始行, 終了行)。解析部品の無い言語は、名前が出てくる最初の行だけを返す。"""
        part = self.analyzer(name)
        if part:
            found = self._run(name, lambda text: part.locate(text, func), line_no)
            if found:
                return found
        else:
            pattern = re.compile(r"(?<![\w])" + re.escape(func) + r"(?![\w])")
            for index, line in enumerate(self.text(name, line_no).splitlines(), 1):
                if pattern.search(line):
                    return index, index
        fail(line_no, "{0} に {1} がありません。関数の名前が変わったか、消えています。".format(name, func))

    def excerpt(self, name, func):
        start, end = self.locate(name, func)
        lines = self.text(name).splitlines()[start - 1:end]
        width = len(str(end))
        body = "\n".join("{0}  {1}".format(str(start + i).rjust(width), line) for i, line in enumerate(lines))
        where = "{0} 行".format(start) if start == end else "{0}〜{1} 行".format(start, end)
        return ('<details class="dc-src"><summary><code>{0}</code> の <code>{1}</code>'
                '<span class="at">{2}</span></summary><pre class="dc-pre">{3}</pre></details>').format(
            esc(name), esc(func), where, esc(body))

    def modules(self):
        """解析部品のある拡張子のファイル。フォルダからの相対パス。"""
        out = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in self.analyzers:
                continue
            rel = path.relative_to(self.root).as_posix()
            if any(part.startswith((".", "__pycache__")) for part in rel.split("/")):
                continue
            out.append(rel)
        return out

    def languages(self):
        return list(dict.fromkeys(self.analyzer(name).NAME for name in self.modules()))

    def doc(self, name):
        part = self.analyzer(name)
        return self._run(name, part.doc) if part else ""

    def imports(self):
        """モジュールどうしの参照 [(参照元, 参照先)]。フォルダの外(標準ライブラリなど)は含めない。

        部品が返す名前(import・source の相手)に加えて、Python 以外では、ほかのモジュールの
        ファイル名がそのまま書かれていれば参照とみなす(起動用のスクリプトが `python app.py` と
        呼ぶ、など。言語をまたぐ参照を拾うため)。
        """
        mods = self.modules()
        by_stem, by_file = {}, {}
        for name in mods:
            by_stem.setdefault(pathlib.PurePosixPath(name).stem, name)
            by_file.setdefault(pathlib.PurePosixPath(name).name, name)
        edges = []
        for name in mods:
            part = self.analyzer(name)
            seen = []
            for ref in self._run(name, part.references):
                target = by_file.get(ref) or by_stem.get(ref)
                if target and target != name and target not in seen:
                    seen.append(target)
            if part.NAME != "Python":
                text = self.text(name)
                for other, target in by_file.items():
                    if target != name and target not in seen \
                            and re.search(r"(?<![\w.-])" + re.escape(other) + r"(?![\w-])", text):
                        seen.append(target)
            edges += [(name, target) for target in seen]
        return edges

    def facts(self):
        """ソースにだけある事実を拾う。[(種類, ファイル, 行, 囲む関数の並び, 内容)]"""
        out = []
        for name in self.modules():
            part = self.analyzer(name)
            for kind, line, chain, what in self._run(name, part.facts):
                out.append((kind, name, line, tuple(chain), what))
        return out


def render_refs(refs, code):
    """発生箇所・実装の並び。ソースがあれば、開くとコードが出る。"""
    if code is None:
        return "　".join("<code>{0}</code> の <code>{1}</code>".format(esc(f), esc(fn)) for f, fn in refs)
    return "".join(code.excerpt(f, fn) for f, fn in refs)


def module_links(specs, where):
    """モジュールごとの、関わる機能・エラーの ID(4.1 の表に足す列)。"""
    used = {}
    errors = {}
    for spec in specs:
        for f, _ in spec.get("impl", ()):
            used.setdefault(f, []).append(spec["id"])
    for eid, refs in sorted(where.items()):
        for f, _ in refs:
            errors.setdefault(f, []).append(eid)

    def cell(table):
        def one(first):
            ids = list(dict.fromkeys(table.get(Palette.key(first), [])))
            return inline(" ".join(ids)) if ids else '<span class="none">なし</span>'
        return one
    return [("関連機能", cell(used)), ("関連エラー", cell(errors))] if used or errors else None


def path_purposes(path, purpose_names, fids_of):
    """インターフェースの「用途」に書かれた機能を持つ目的。機能が書かれていなければ、どの目的にも結ばない。"""
    text = " ".join(v for v, _ in path["fields"].get("用途", ()))
    named = set(ID_RE.findall(text))
    # 「F-01〜F-16」のように範囲で書かれた機能も、間の番号を含めて数える
    for first, last in re.findall(r"F-(\d{2})\s*[〜~～]\s*F-(\d{2})", text):
        named.update("F-{0:02d}".format(n) for n in range(int(first), int(last) + 1))
    return [pid for pid, _ in purpose_names if named & set(fids_of[pid])]


def peer_reach(io_data, stores, purpose_names, fids_of):
    """5 章の外部エンティティ(データストアを除く)と、それぞれが関わる目的。"""
    def is_store(peer):
        return any(peer == st or peer.startswith(st + " ") for st in stores)

    entities, reach = [], {}
    for path in (io_data["paths"] if io_data else ()):
        if is_store(path["peer"]):
            continue
        if path["peer"] not in entities:
            entities.append(path["peer"])
        for pid in path_purposes(path, purpose_names, fids_of):
            if pid not in reach.setdefault(path["peer"], []):
                reach[path["peer"]].append(pid)
    for peer in entities:
        reach.setdefault(peer, [])
    return entities, reach


def usecase_groups(flows):
    """ユースケースを「対象」ごとにまとめる。[(対象, [目的の辞書])]。並びは最初に現れた順。"""
    groups = []
    for flow in flows:
        target = flow["fields"]["対象"]
        for name, members in groups:
            if name == target:
                members.append(flow)
                break
        else:
            groups.append((target, [flow]))
    return groups


def usecase_svg(system, actors, groups, reach, flow_name, number):
    """3.2 のユースケース図。左右に外部エンティティ、枠の中に目的(ユースケース)を対象ごとに並べる。

    対象ごとに枠で区切る(ユースケースの単位は変えず、見渡しやすくするためのまとまり)。
    """
    gap, pad, head = 12.0, 14.0, 20.0
    shapes = {}
    for _, members in groups:
        for flow in members:
            lines = wrap_text(flow["name"], 190.0)
            shapes[flow["pid"]] = (lines, max(text_width(t) for t in lines) + 56.0,
                                   len(lines) * DG_LINE + 30.0)
    inner_w = max(w for _, w, _ in shapes.values()) + pad * 2
    box_w = inner_w + pad * 2
    box_x = 150.0
    out, centre = [], {}
    y = pad + 22.0
    for target, members in groups:
        height = sum(shapes[f["pid"]][2] for f in members) + gap * (len(members) - 1) + pad + head
        # UML のパッケージの記法で描く(左上に名前の入った見出し、その下に本体の枠)
        label = wrap_text(target, inner_w - 16.0, 10.0)[:1]
        tab_w = text_width(label[0], 10.0) + 16.0
        out.append('<rect class="uc-gh" x="{0:.1f}" y="{1:.1f}" width="{2:.1f}" height="18.0"/>'.format(
            box_x + pad, y, tab_w))
        out.append('<rect class="uc-g" x="{0:.1f}" y="{1:.1f}" width="{2:.1f}" height="{3:.1f}"/>'.format(
            box_x + pad, y + 18.0, inner_w, height - 18.0))
        out.append(svg_text(box_x + pad + 8.0, y + 13.0, label, size=10.0, cls="uc-gt", anchor="start"))
        at = y + head
        for flow in members:
            lines, w, h = shapes[flow["pid"]]
            cx, cy = box_x + box_w / 2, at + h / 2
            centre[flow["pid"]] = (cx, cy, w / 2, h / 2)
            label = svg_text(cx, cy - (len(lines) - 1) * DG_LINE / 2 + 9.0, lines, cls="uc-t")
            out.append('<a href="{0}#fl-{1}"><ellipse class="uc-e" cx="{2:.1f}" cy="{3:.1f}" rx="{4:.1f}" '
                       'ry="{5:.1f}"/><text class="uc-n" x="{2:.1f}" y="{6:.1f}" text-anchor="middle" '
                       'textLength="{7:.1f}" lengthAdjust="spacingAndGlyphs">{8}</text>{9}</a>'.format(
                           esc(flow_name), anchor_of(flow["pid"]), cx, cy, w / 2, h / 2, cy - h / 2 + 12.0,
                           text_width(flow["pid"], 9.0), esc(flow["pid"]), label))
            at += h + gap
        y += height + gap
    box_h = y - gap + pad
    out.insert(0, '<rect class="uc-b" x="{0:.1f}" y="0.0" width="{1:.1f}" height="{2:.1f}"/>'.format(
        box_x, box_w, box_h) + svg_text(box_x + box_w / 2, 17.0, [system], size=11.0, cls="uc-s"))
    sides = {"L": [a for i, a in enumerate(actors) if i % 2 == 0],
             "R": [a for i, a in enumerate(actors) if i % 2 == 1]}
    for side, names in sides.items():
        for index, name in enumerate(names):
            ax = 50.0 if side == "L" else box_x + box_w + 100.0
            mine = [centre[pid][1] for pid in reach.get(name, ()) if pid in centre]
            # 相手は、つながるユースケースの高さの真ん中に置く(同じ側に複数いれば上下に分ける)
            ay = (sum(mine) / len(mine)) if mine and len(names) == 1 else box_h * (index + 1) / (len(names) + 1)
            out.append('<circle class="uc-a" cx="{0:.1f}" cy="{1:.1f}" r="8"/>'
                       '<line class="uc-a" x1="{0:.1f}" y1="{2:.1f}" x2="{0:.1f}" y2="{3:.1f}"/>'
                       '<line class="uc-a" x1="{4:.1f}" y1="{5:.1f}" x2="{6:.1f}" y2="{5:.1f}"/>'
                       '<line class="uc-a" x1="{0:.1f}" y1="{3:.1f}" x2="{4:.1f}" y2="{7:.1f}"/>'
                       '<line class="uc-a" x1="{0:.1f}" y1="{3:.1f}" x2="{6:.1f}" y2="{7:.1f}"/>'.format(
                           ax, ay - 22.0, ay - 14.0, ay + 6.0, ax - 11.0, ay - 6.0, ax + 11.0, ay + 20.0))
            out.append(svg_text(ax, ay + 36.0, wrap_text(name, 96.0, 11.0), size=11.0, cls="uc-s"))
            for pid in reach.get(name, ()):
                cx, cy, rx, _ = centre[pid]
                out.append('<line class="uc-l" x1="{0:.1f}" y1="{1:.1f}" x2="{2:.1f}" y2="{3:.1f}"/>'.format(
                    ax + (16.0 if side == "L" else -16.0), ay, cx - rx if side == "L" else cx + rx, cy))
    return fig("ユースケース図", number, svg_frame("".join(out), "ユースケース図"))


SF_R = 13.0             # 工程の丸の半径
SF_GAP = 20.0           # 縦の間隔
SF_SIDE = 132.0         # 分岐の枝・並行の経路を置く横の間隔
SF_LANE = 212.0         # 並行・順不同の経路の横の間隔(隣の経路のモジュール名と「エラー」が並ぶ幅)
SF_RAIL = 40.0          # エラーの線を引く、本線からの距離


def flow_order(nodes, pid):
    """流れを上から順にたどり、工程と分岐を出てくる順に並べる。分岐には記号(A, B, …)を振る。

    戻り値は [("s", 工程), ("b", 分岐, 枝に進んだあとの行き先, 枝に進まないときの行き先)]。
    行き先は、次に実行する工程か端子(無ければ None)。
    """
    out, count = [], [0]

    def first(seq, after):
        """seq の先頭から見て最初の工程・端子・分岐。無ければ after。"""
        for node in seq:
            if node.kind in ("工程", "端子", "終了", "判断", "待機"):
                return node
            inner = first(node.children, None) if node.kind == "繰り返し" else (
                first(node.lanes[0], None) if node.lanes else None)
            if inner is not None:
                return inner
        return after

    def walk(seq, after):
        for index, node in enumerate(seq):
            following = first(seq[index + 1:], after)
            if node.kind == "工程":
                out.append(("s", node))
            elif node.kind == "判断":
                node.bid = "br-{0}-{1}".format(pid.replace("-", ""), "abcdefghijklmnopqrstuvwxyz"[count[0] % 26])
                count[0] += 1
                out.append(("b", node, first(node.children, following), following))
                walk(node.children, following)
            elif node.kind == "繰り返し":
                walk(node.children, following)
            for lane in node.lanes:
                walk(lane, following)

    walk(nodes, None)
    return out


def simple_flow_svg(items, chip_of, number):
    """別紙の左に置く、簡略な流れ図。

    工程は番号の丸、判断はひし形。工程の右には札(実装のモジュール、無ければ機能 ID)を添える。
    止まる条件は、ひと続きの流れ(本線・枝・経路)ごとに 1 つの「エラー」へ線を集める。
    工程の名前や説明は図に書かない(右の欄に出る)。
    """
    out = []

    def tip(text):
        return "<title>{0}</title>".format(esc(text)) if text else ""

    def arrow(x, y1, y2):
        if y2 - y1 < 4:
            return
        out.append('<line class="fl-e" x1="{0:.1f}" y1="{1:.1f}" x2="{0:.1f}" y2="{2:.1f}"/>'.format(x, y1, y2))
        out.append(arrow_tip(x, y2, 0.0, 1.0))

    def pill(x, y, text, cls):
        lines = wrap_text(text, 170.0, 11.0)
        w = max(text_width(t, 11.0) for t in lines) + 26.0
        h = len(lines) * 14.0 + 10.0
        out.append('<rect class="{0}" x="{1:.1f}" y="{2:.1f}" width="{3:.1f}" height="{4:.1f}" rx="{5:.1f}"/>'.format(
            cls, x - w / 2, y, w, h, h / 2))
        for index, line in enumerate(lines):
            out.append('<text class="fl-x" x="{0:.1f}" y="{1:.1f}" text-anchor="middle" textLength="{2:.1f}" '
                       'lengthAdjust="spacingAndGlyphs" style="font-size:11px">{3}</text>'.format(
                           x, y + 15.0 + index * 14.0, text_width(line, 11.0), esc(line)))
        return h

    def run(nodes, x, y):
        """ひと続きの流れを上から描く。(描き終えた y, 次へ線を続けるか) を返す。"""
        stops = []
        open_end = True
        for index, node in enumerate(nodes):
            top = y
            if node.kind in ("端子", "終了"):
                y += pill(x, y, node.text, "fl-t stop" if node.kind == "終了" else "fl-t")
                open_end = node.kind != "終了"
            elif node.kind == "待機":
                out.append('<g>{0}<rect class="fl-wt" x="{1:.1f}" y="{2:.1f}" width="64.0" height="22.0"/>'
                           '<text class="fl-wm" x="{3:.1f}" y="{4:.1f}" text-anchor="middle" textLength="20.0" '
                           'lengthAdjust="spacingAndGlyphs">待機</text></g>'.format(
                               tip(node.text), x - 32.0, y, x, y + 15.0))
                y += 22.0
            elif node.kind == "工程":
                cy = y + SF_R
                label = node.sid[-2:] if node.sid else "・"
                out.append('<g>{0}<rect class="fl-bb" x="{1:.1f}" y="{2:.1f}" width="{3:.1f}" height="{3:.1f}"/>'
                           '<circle class="fl-n" cx="{4:.1f}" cy="{5:.1f}" r="{6:.1f}"{7}/>'
                           '<text class="fl-cn" x="{4:.1f}" y="{8:.1f}" text-anchor="middle" textLength="{9:.1f}" '
                           'lengthAdjust="spacingAndGlyphs">{10}</text></g>'.format(
                               tip(node.text), x - SF_R, y, SF_R * 2, x, cy, SF_R,
                               ' id="{0}"'.format(node.sid) if node.sid else "", cy + 4.0,
                               text_width(label, 11.0), esc(label)))
                chip, color = chip_of(node)
                if chip:
                    out.append('<text class="fl-ch" x="{0:.1f}" y="{1:.1f}" text-anchor="start" textLength="{2:.1f}" '
                               'lengthAdjust="spacingAndGlyphs" style="fill:{3}">{4}</text>'.format(
                                   x + SF_R + 7.0, cy + 3.5, text_width(chip, 10.0), color, esc(chip)))
                if node.exception:
                    stops.append((cy, node))
                y += SF_R * 2
            elif node.kind == "判断":
                half = 16.0
                # 判断の条件を図に書く(「?」だけでは、何で分かれるのかが図から読めない)
                asks = wrap_text(node.text, SF_SIDE + 70.0, 10.0)
                out.append(svg_text(x + 9.0, y + 10.0, asks, size=10.0, cls="fl-q", anchor="start"))
                lead = len(asks) * DG_LINE + 2.0
                out.append('<line class="fl-e" x1="{0:.1f}" y1="{1:.1f}" x2="{0:.1f}" y2="{2:.1f}"/>'.format(
                    x, y, y + lead))
                y += lead
                cy = y + half
                out.append('<g>{0}<polygon class="fl-d" points="{1:.1f},{2:.1f} {3:.1f},{4:.1f} {1:.1f},{5:.1f} '
                           '{6:.1f},{4:.1f}"{8}/><text class="fl-cn" x="{1:.1f}" y="{7:.1f}" text-anchor="middle" '
                           'textLength="8.0" lengthAdjust="spacingAndGlyphs">{9}</text></g>'.format(
                               tip(node.text), x, y, x + half, cy, y + half * 2, x - half, cy + 4.0,
                               ' id="{0}"'.format(node.bid) if node.bid else "",
                               esc(node.bid[-1].upper()) if node.bid else "?"))
                bx = x + SF_SIDE
                out.append('<line class="fl-e" x1="{0:.1f}" y1="{1:.1f}" x2="{2:.1f}" y2="{1:.1f}"/>'.format(
                    x + half, cy, bx))
                out.append(svg_text(x + half + 6.0, cy - 6.0, wrap_text(node.label, SF_SIDE - half - 12.0, 10.0)[:1],
                                    size=10.0, cls="fl-l", anchor="start"))
                arrow(bx, cy, cy + SF_GAP)
                by, joins = run(node.children, bx, cy + SF_GAP)
                # 枝が端子で終わるなら、そこで流れが終わる(本線へは戻らない)
                if node.children and node.children[-1].kind == "端子":
                    joins = False
                y = max(y + half * 2, by)
                if joins:
                    # 枝の終わりから本線へ戻る
                    out.append('<line class="fl-e" x1="{0:.1f}" y1="{1:.1f}" x2="{0:.1f}" y2="{2:.1f}"/>'
                               '<line class="fl-e" x1="{0:.1f}" y1="{2:.1f}" x2="{3:.1f}" y2="{2:.1f}"/>'.format(
                                   bx, by, y + SF_GAP / 2, x))
                if node.else_label:
                    short = wrap_text(node.else_label, SF_SIDE - 20.0, 10.0)[0]
                    out.append(svg_text(x + 7.0, cy + half + 18.0, [short], size=10.0, cls="fl-l", anchor="start"))
                out.append('<line class="fl-e" x1="{0:.1f}" y1="{1:.1f}" x2="{0:.1f}" y2="{2:.1f}"/>'.format(
                    x, cy + half, y))
            elif node.kind == "繰り返し":
                label = wrap_text(node.label, 150.0, 10.0)[:1]
                inner_top = y + 18.0
                end, _ = run(node.children, x, inner_top + 6.0)
                out.append('<rect class="fl-lp" x="{0:.1f}" y="{1:.1f}" width="{2:.1f}" height="{3:.1f}"/>'.format(
                    x - 46.0, y, 150.0, end + 8.0 - y))
                out.append(svg_text(x - 40.0, y + 12.0, label, size=10.0, cls="fl-lpt", anchor="start"))
                y = end + 8.0
            elif node.kind in ("並行", "順不同"):
                span = SF_LANE * (len(node.lanes) - 1)
                cls = "fl-bar" if node.kind == "並行" else "fl-bar loose"
                out.append('<rect class="{0}" x="{1:.1f}" y="{2:.1f}" width="{3:.1f}" height="5.0"/>'.format(
                    cls, x - 22.0, y, span + 44.0))
                ends = []
                for index2, lane in enumerate(node.lanes):
                    lx = x + SF_LANE * index2
                    arrow(lx, y + 5.0, y + 5.0 + SF_GAP)
                    ends.append(run(lane, lx, y + 5.0 + SF_GAP)[0])
                bottom = max(ends) + SF_GAP
                for index2, end in enumerate(ends):
                    lx = x + SF_LANE * index2
                    out.append('<line class="fl-e" x1="{0:.1f}" y1="{1:.1f}" x2="{0:.1f}" y2="{2:.1f}"/>'.format(
                        lx, end, bottom))
                out.append('<rect class="{0}" x="{1:.1f}" y="{2:.1f}" width="{3:.1f}" height="5.0"/>'.format(
                    cls, x - 22.0, bottom, span + 44.0))
                y = bottom + 5.0
            if index + 1 < len(nodes) and open_end:
                arrow(x, y, y + SF_GAP)
                y += SF_GAP
        if stops:
            rail = x - SF_RAIL
            last = stops[-1][0]
            for cy, _ in stops:
                out.append('<line class="fl-er" x1="{0:.1f}" y1="{1:.1f}" x2="{2:.1f}" y2="{1:.1f}"/>'.format(
                    rail, cy, x - SF_R))
            if last > stops[0][0]:
                out.append('<line class="fl-er" x1="{0:.1f}" y1="{1:.1f}" x2="{0:.1f}" y2="{2:.1f}"/>'.format(
                    rail, stops[0][0], last))
            what = "\n".join("{0} {1}:{2}({3})".format(
                n.sid[-2:] if n.sid else "", n.text, n.exception.text, " ".join(n.exception.eids)) for _, n in stops)
            out.append('<g>{0}<rect class="fl-ep" x="{1:.1f}" y="{2:.1f}" width="44.0" height="16.0" rx="8.0"/>'
                       '<text class="fl-et" x="{3:.1f}" y="{4:.1f}" text-anchor="middle" textLength="30.0" '
                       'lengthAdjust="spacingAndGlyphs">エラー</text></g>'.format(
                           tip(what), rail - 44.0, last - 8.0, rail - 22.0, last + 4.0))
        return y, open_end

    run(items, 0.0, 0.0)
    return fig("処理フロー図", number, svg_frame("".join(out), "処理フロー図"))


def build_flow_page(flows, specs, rows9, where, code, spec_name, palette):
    """別紙「処理の流れ」。左に流れ図、右に工程ごとの説明とコードを並べる。"""
    by_id = dict((spec["id"], spec) for spec in specs)
    errors = dict((row["id"], row) for row in rows9)
    modules = []
    for spec in specs:
        for f, _ in spec.get("impl", ()):
            if f not in modules:
                modules.append(f)

    def chip_of(node):
        """工程の右の札。実装のモジュール(色つき)、無ければ機能 ID。"""
        for fid in node.ids:
            impl = by_id[fid].get("impl") if fid in by_id else None
            if impl:
                name = impl[0][0]
                return (pathlib.PurePosixPath(name).stem, palette.color(name))
        return (" ".join(node.ids), "#5b6475") if node.ids else ("", "")

    legend_rows = []
    for index, name in enumerate(modules):
        role = code.doc(name) if code and (code.root / name).is_file() else ""
        legend_rows.append('<tr><td class="id"><span style="color:{0}">■</span> {1}</td>'
                           '<td class="dsc">{2}</td></tr>'.format(
                               palette.color(name), esc(pathlib.PurePosixPath(name).stem),
                               inline(role) if role else '<span class="none">(説明なし)</span>'))
    sections = [
        '<section class="dc-sec"><h1 class="dc-title">処理フロー</h1>'
        '<p class="dc-sub"><a href="{0}">仕様書</a>の別紙。ユースケースごとの処理フローと、各工程の機能・エラー条件・実装箇所を示す。</p>'
        '{1}</section>'.format(
            esc(spec_name),
            ('<h3 class="dc-h3">色の凡例(モジュール)</h3>'
             + tag_table(["<th>モジュール</th>", "<th>役割</th>"], legend_rows)) if legend_rows else "")]
    for flow in flows:
        items = []
        order = flow_order(flow["items"], flow["pid"])
        step_html = {}
        for sid, text, fids, eids, cond in flow["steps"]:
            body = []
            for fid in fids:
                spec = by_id[fid]
                parts = ['<p class="fw-fh"><a href="{0}#{1}">{2}</a><b>{3}</b>{4}</p>'.format(
                    esc(spec_name), anchor_of(fid), fid, esc(spec["name"]), mark(spec["state"])),
                    '<p class="fw-p">{0}</p>'.format(inline(spec["fields"]["概要"][0][0]))]
                works = [v for v, _ in spec["fields"].get("処理", ()) if v not in ("なし", "-")]
                if works:
                    parts.append("<ol>" + "".join("<li>{0}</li>".format(inline(v)) for v in works) + "</ol>")
                if spec.get("impl"):
                    parts.append('<div class="fw-c">{0}</div>'.format(render_refs(spec["impl"], code)))
                body.append('<div class="fw-f">{0}</div>'.format("".join(parts)))
            if not fids:
                body.append('<p class="fw-p none">機能仕様に対応する機能がない工程。</p>')
            if eids:
                lis = []
                for eid in eids:
                    row = errors.get(eid)
                    cell = '<a href="{0}#{1}">{2}</a> {3}'.format(
                        esc(spec_name), anchor_of(eid), eid, inline(row["cells"][0]) if row else "")
                    if where.get(eid):
                        cell += '<div class="fw-c">{0}</div>'.format(render_refs(where[eid], code))
                    lis.append("<li>{0}</li>".format(cell))
                body.append('<div class="fw-e"><p class="fw-eh">エラー条件{0}</p><ul>{1}</ul></div>'.format(
                    "(" + esc(cond) + ")" if cond else "", "".join(lis)))
            step_html[sid] = ('<article class="fw-i" data-step="{0}"><h4><span class="no">{1}</span>{2}</h4>{3}'
                              '</article>').format(sid, sid[-2:], inline(text), "".join(body))

        def goal(node):
            """行き先の書き方。工程は番号と名前、分岐は記号、端子はその文。"""
            if node is None:
                return ""
            if node.kind == "工程":
                return "{0} {1}".format(node.sid[-2:], node.text) if node.sid else node.text
            if node.kind == "判断":
                return "分岐 {0}".format(node.bid[-1].upper())
            return "「{0}」".format(node.text)

        for entry in order:
            if entry[0] == "s":
                html_text = step_html.pop(entry[1].sid, "")
                if html_text:
                    items.append(html_text)
                continue
            _, node, inside, after = entry
            rows = []
            if node.label:
                # 枝の中身を実行したあと、本線へ戻るか(枝が「終了」で終わるなら戻らない)
                ends = bool(node.children) and node.children[-1].kind in ("終了", "端子")
                if node.children and inside is not after:
                    text = "{0} を実行".format(inline(goal(inside)))
                    text += "して終了する" if ends else (
                        "し、{0} へ進む".format(inline(goal(after))) if after is not None else "する")
                else:
                    text = "{0} へ進む".format(inline(goal(after))) if after is not None else "何も実行しない"
                rows.append("<dt>{0}</dt><dd>{1}</dd>".format(inline(node.label), text))
            if node.else_label:
                rows.append("<dt>{0}</dt><dd>{1}</dd>".format(
                    inline(node.else_label),
                    "{0} へ進む".format(inline(goal(after))) if after is not None else "何も実行しない"))
            items.append('<article class="fw-i fw-b" data-step="{0}"><h4><span class="no">分岐 {1}</span>{2}</h4>'
                         '<dl class="fw-br">{3}</dl></article>'.format(
                             node.bid, node.bid[-1].upper(), inline(node.text), "".join(rows)))
        # 流れの木に現れなかった工程(例外の中の工程など)も落とさない
        items.extend(step_html[sid] for sid, _, _, _, _ in flow["steps"] if sid in step_html)
        used = []
        figure = simple_flow_svg(flow["items"], lambda node: used.append(chip_of(node)) or used[-1], flow["pid"])
        legend = "".join('<span class="fw-k" style="color:{1}">■ {0}</span>'.format(esc(name), color)
                         for name, color in dict.fromkeys(u for u in used if u[0] and u[1] != "#5b6475"))
        note = '<span class="fw-lg">色の凡例: {0}</span>'.format(legend) if legend else ""
        caption = inline(flow.get("caption", ""))
        if caption or note:
            cut = figure.rindex("</div>")
            figure = (figure[:cut] + '<p class="dc-fig-c">{0}{1}</p>'.format(
                caption + ("<br>" if caption and note else ""), note) + figure[cut:])
        intro = '<dl class="dc-rows dc-uc">{0}</dl>'.format("".join(
            "<dt>{0}</dt><dd>{1}</dd>".format("アクター" if k == "誰が" else esc(k), inline(flow["fields"][k])) for k in PURPOSE_FIELDS))
        intro += "".join('<p class="dc-p">{0}</p>'.format(inline(t)) for t in flow["intro"])
        sections.append(
            '<section class="dc-sec"><h2 class="dc-ch" id="fl-{0}"><span class="no">{1}</span>'
            '<span class="nm">{2}</span></h2>{3}<div class="fw"><div class="fw-l">{4}</div>'
            '<div class="fw-r">{5}</div></div></section>'.format(
                anchor_of(flow["pid"]), flow["pid"], inline(flow["name"]), intro, figure, "".join(items)))
    sections.append(FLOW_SCRIPT)
    return "\n".join(sections)


FLOW_SCRIPT = """<style>.doc { overflow-x: clip; }</style>
<script>
(function () {
  function pick(sid, from) {
    document.querySelectorAll(".fw-i.on, .fl-n.on, .fl-d.on").forEach(function (e) { e.classList.remove("on"); });
    var item = document.querySelector('.fw-i[data-step="' + sid + '"]');
    var box = document.getElementById(sid);
    if (item) { item.classList.add("on"); if (from !== "item") { item.scrollIntoView({ block: "nearest", behavior: "smooth" }); } }
    if (box) { box.classList.add("on"); if (from !== "box") { box.scrollIntoView({ block: "nearest", behavior: "smooth" }); } }
  }
  document.querySelectorAll(".fw-l svg").forEach(function (svg) {
    svg.addEventListener("click", function (ev) {
      if (ev.target.closest("a")) { return; }
      var hit = null;
      svg.querySelectorAll(".fl-n[id], .fl-d[id]").forEach(function (r) {
        var b = r.getBoundingClientRect();
        if (ev.clientX >= b.left && ev.clientX <= b.right && ev.clientY >= b.top && ev.clientY <= b.bottom) { hit = r; }
      });
      if (hit) { pick(hit.id, "box"); }
    });
  });
  document.querySelectorAll(".fw-i").forEach(function (item) {
    item.addEventListener("click", function (ev) {
      if (ev.target.closest("a, summary, details")) { return; }
      pick(item.dataset.step, "item");
    });
  });
  if (/^#(st|br)-/.test(location.hash)) { pick(location.hash.slice(1), ""); }
})();
</script>"""


DFD_BOX_GAP = 10.0


def dfd_svg(entities, procs, stores, flows, number):
    """データフロー図。左に外部エンティティ、中央にプロセス(上から目的の順)、右にデータストア。

    矢印は 1 本 1 方向で、すべて水平に引く。相手とデータストアの箱は、つながるプロセスの高さまで
    縦に伸ばす。同じ列に 2 つ以上の相手(データストア)があるときは、線がほかの箱を横切らないよう、
    プロセスごとに箱を分けて置く(同じ相手が複数の場所に現れる。その箱には右上に印を付ける)。
    戻り値は (図, 複数の場所に描いた名前の一覧)。
    """
    widest = max([widest_chunk(label, 10.0) for _, _, label in flows] or [0.0])
    gap = min(max(200.0, widest + 56.0), 320.0)

    def pitch(label):
        return len(wrap_text(label, gap - 46.0, 10.0)) * 12.0 + 12.0

    def groups(key, kind, names):
        """このプロセスにつながる相手(データストア)ごとの矢印。相手の並びは原稿の順。"""
        out = []
        for name in names:
            other = (kind, name)
            mine = [(f, "in" if f[1] == key else "out") for f in flows
                    if key in f[:2] and other in f[:2]]
            if mine:
                # 箱の名前が収まる高さを確保する(足りないと隣の箱と重なる)
                natural = DgNode(name, max_width=96.0 if kind == "e" else 120.0).h + DFD_BOX_GAP
                out.append((name, mine, max(sum(pitch(f[2]) for f, _ in mine) + 10.0, natural)))
        return out

    rows = []
    for name in procs:
        key = ("p", name)
        node = DgNode(name, max_width=190.0, role="proc")
        left, right = groups(key, "e", entities), groups(key, "s", stores)
        node.stretch(max(sum(g[2] for g in left), sum(g[2] for g in right)) + 8.0)
        rows.append((node, left, right))
    e_w = max([DgNode(n, max_width=96.0).w for n in entities] or [0.0])
    s_w = max([DgNode(n, max_width=120.0).w for n in stores] or [0.0])
    p_w = max(node.w for node, _, _ in rows)
    p_x, s_x = e_w + gap, e_w + gap + p_w + gap
    y = 0.0
    for node, _, _ in rows:
        node.w, node.x, node.y = p_w, p_x, y
        y += node.h + 24.0

    parts, repeated = [], []
    for node, _, _ in rows:
        parts.append(node.render().replace("/>", ' rx="14.0"/>', 1))
    for side, index, kind, names in (("left", 1, "e", entities), ("right", 2, "s", stores)):
        slices = []            # (名前, 上端, 下端)。同じ名前が続けば 1 つの箱につなげる
        for row in rows:
            node, items = row[0], row[index]
            at = node.y + (node.h - sum(g[2] for g in items)) / 2
            for name, mine, height in items:
                top = at
                line_y = at + (height - sum(pitch(f[2]) for f, _ in mine)) / 2
                for flow, direction in mine:
                    line_y += pitch(flow[2]) / 2
                    if side == "left":
                        parts.append(edge_svg(e_w, line_y, node.x, line_y, direction, side, flow[2], gap, True))
                    else:
                        parts.append(edge_svg(s_x, line_y, node.x + node.w, line_y,
                                              direction, side, flow[2], gap, True))
                    line_y += pitch(flow[2]) / 2
                at += height
                if slices and slices[-1][0] == name:
                    slices[-1] = (name, slices[-1][1], at)
                else:
                    slices.append((name, top, at))
        seen = [name for name, _, _ in slices]
        for name, top, bottom in slices:
            box = DgNode(name, accent=(kind == "e"), max_width=96.0 if kind == "e" else 120.0,
                         role="entity" if kind == "e" else "store")
            # 箱の幅は列でそろえ、上下の箱との間に隙間を空ける
            box.w = e_w if side == "left" else s_w
            box.h = bottom - top - DFD_BOX_GAP
            box.y = top + DFD_BOX_GAP / 2
            box.x = 0.0 if side == "left" else s_x
            parts.append(box.render())
            if seen.count(name) > 1:
                # 同じ相手を複数の場所に描いたことを示す印(右上の斜線)
                parts.append('<line class="dg-dup" x1="{0:.1f}" y1="{1:.1f}" x2="{2:.1f}" y2="{3:.1f}"/>'.format(
                    box.x + box.w - 12.0, box.y, box.x + box.w, box.y + 12.0))
                if name not in repeated:
                    repeated.append(name)
    return fig("データフロー図", number, svg_frame("".join(parts), "データフロー図")), repeated


def render_data_flow(specs, steps, purpose_names, stores, io_data, chapter_no, first_no, groups=()):
    """6 章のデータフロー図。機能の「読む」「書く」・3.2 の流れ・5 章のやり取りから組む。

    プロセスは 3.2 の目的。線にはデータの名前を付け、外部エンティティとデータストアはプロセスを通して結ぶ。
    """
    use = []
    for spec in specs:
        for key in ("読む", "書く"):
            for value, line_no in spec["fields"].get(key, ()):
                if value not in stores:
                    fail(line_no, "{0} の「{1}: {2}」は、第 6 章の @store にない名前です。"
                                  "データストアの名前は {3} です。".format(spec["id"], key, value, "・".join(stores)))
                use.append((spec["id"], key, value))
    if not use:
        return "", 0
    head = ["<th>データストア</th>", "<th>参照機能</th>", "<th>更新機能</th>"]
    body = []
    for store in stores:
        cells = []
        for key in ("読む", "書く"):
            ids = [fid for fid, k, v in use if k == key and v == store]
            cells.append(inline(" ".join(ids)) if ids else '<span class="none">なし</span>')
        body.append('<tr><td class="id">{0}</td><td class="dsc">{1}</td><td class="dsc">{2}</td></tr>'.format(
            esc(store), cells[0], cells[1]))

    fids_of = {}
    for pid, _ in purpose_names:
        fids_of[pid] = [fid for fid, info in steps.items()
                        if any(step_label(sid).startswith(pid + ":") for sid in info["sids"])]
    label_of = dict((pid, "{0} {1}".format(pid, name)) for pid, name in purpose_names)

    def store_of(peer):
        for store in stores:
            if peer == store or peer.startswith(store + " "):
                return store
        return ""

    paths = io_data["paths"] if io_data else []
    # データストアに出入りするデータの名前。5 章に同じデータストアとのやり取りがあれば、その名前を使う
    data_name = {}
    def data_of(path, key):
        return path["fields"][key][0][0]

    for path in paths:
        store = store_of(path["peer"])
        if store:
            # 応答のある要求は参照、応答のない要求は更新とみなす
            if has_response(path):
                data_name[(store, "読む")] = data_of(path, "応答")
            else:
                data_name[(store, "書く")] = data_of(path, "要求")
    flows = []
    # 外部エンティティとのやり取り。経路の「用途」に出てくる機能を持つ目的へ結ぶ
    entities, reach = peer_reach(io_data, stores, purpose_names, fids_of)
    reached = dict((path["id"], path_purposes(path, purpose_names, fids_of)) for path in paths)
    for pid, _ in purpose_names:
        for path in paths:
            if store_of(path["peer"]) or pid not in reached[path["id"]]:
                continue
            ends = [("e", path["peer"]), ("p", label_of[pid])]
            if path["kind"] == "発行":
                ends.reverse()
            flows.append((ends[0], ends[1], data_of(path, "要求")))
            if has_response(path):
                flows.append((ends[1], ends[0], data_of(path, "応答")))
        for store in stores:
            if any(f in fids_of[pid] and k == "読む" and v == store for f, k, v in use):
                flows.append((("s", store), ("p", label_of[pid]), data_name.get((store, "読む"), store)))
            if any(f in fids_of[pid] and k == "書く" and v == store for f, k, v in use):
                flows.append((("p", label_of[pid]), ("s", store), data_name.get((store, "書く"), store)))
    # 対象ごとに 1 枚ずつ描く(ユースケースの数が多いと 1 枚では見渡せない)。対象の指定が無ければ 1 枚
    sheets = [(name, [label_of[pid] for pid in pids]) for name, pids in groups] \
        or [("", [label_of[pid] for pid, _ in purpose_names])]
    figures, count = [], 0
    for name, procs in sheets:
        mine = [f for f in flows if any(("p", proc) in f[:2] for proc in procs)]
        if not mine:
            continue
        touched = [st for st in stores if any(("s", st) in f[:2] for f in mine)]
        figure, repeated = dfd_svg(entities, procs, touched, mine, "{0}-{1}".format(chapter_no, first_no + count))
        count += 1
        if repeated:
            cut = figure.rindex("</div>")
            figure = (figure[:cut] + '<p class="dc-fig-c">斜線のあるエンティティ({0})は、同じエンティティを'
                      "複数箇所に描画している。</p>".format("・".join(esc(n) for n in repeated)) + figure[cut:])
        if name:
            figures.append('<h4 class="dc-pp"><span class="no">対象</span>{0}</h4>'.format(inline(name)))
        figures.append(figure)
    return ("\n".join(['<h3 class="dc-h3">データフロー図</h3>']
                      + figures
                      + ['<p class="dc-p">データストアごとの参照機能と更新機能を示す。</p>', tag_table(head, body)]),
            count)



def build_maintenance(code, specs, where, source_text):
    """第 15 章。ソースにだけある事実を拾い、仕様のどれに当たるかを並べる。"""
    by_func = {}
    for spec in specs:
        for ref in spec.get("impl", ()):
            by_func.setdefault((ref[0], ref[1].split(".")[-1]), []).append(spec["id"])
    err_func = {}
    for eid, refs in sorted(where.items()):
        for ref in refs:
            err_func.setdefault((ref[0], ref[1].split(".")[-1]), []).append(eid)

    def linked(table, name, chain):
        ids = []
        for fn in chain:
            ids += table.get((name, fn), [])
        return list(dict.fromkeys(ids))

    def at(name, line, chain):
        return "<code>{0}</code> {1} 行{2}".format(
            esc(name), line, "(<code>{0}</code>)".format(esc(chain[-1])) if chain else "")

    none = '<span class="dc-mk non">仕様に記載なし</span>'
    facts = code.facts()
    missing = 0
    parts = ['<p class="dc-p">調査対象: {0} のソース。</p>'.format(
                 "・".join(esc(n) for n in code.languages()) or "(解析対象の言語なし)")]

    rows = []
    for _, name, line, chain, key in [f for f in facts if f[0] == "env"]:
        known = key != "?" and key in source_text
        missing += 0 if known else 1
        rows.append('<tr><td class="id">{0}</td><td class="dsc">{1}</td><td class="dsc">{2}</td></tr>'.format(
            esc(key), at(name, line, chain), "記載あり" if known else none))
    parts.append('<h3 class="dc-h3">15.1 環境変数</h3>')
    parts.append(tag_table(["<th>変数名</th>", "<th>参照箇所</th>", "<th>仕様書の記載</th>"], rows)
                 if rows else '<p class="dc-p">環境変数の参照箇所はない。</p>')

    rows = []
    for _, name, line, chain, what in [f for f in facts if f[0] == "effect"]:
        ids = linked(by_func, name, chain)
        missing += 0 if ids else 1
        rows.append('<tr><td class="dsc">{0}</td><td class="dsc">{1}</td><td class="dsc">{2}</td></tr>'.format(
            esc(what), at(name, line, chain), inline(" ".join(ids)) if ids else none))
    parts.append('<h3 class="dc-h3">15.2 ファイル・フォルダの更新処理</h3>')
    parts.append(tag_table(["<th>処理内容</th>", "<th>実装箇所</th>", "<th>対応機能</th>"], rows)
                 if rows else '<p class="dc-p">ファイル・フォルダの更新処理はない。</p>')

    groups = {}
    for _, name, line, chain, _what in [f for f in facts if f[0] == "stop"]:
        groups.setdefault((name, chain), []).append(line)
    rows = []
    for (name, chain), lines in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[1][0])):
        ids = linked(err_func, name, chain)
        missing += 0 if ids else 1
        rows.append('<tr><td class="dsc"><code>{0}</code> の <code>{1}</code></td>'
                    '<td class="num">{2}</td><td class="dsc">{3}</td><td class="dsc">{4}</td></tr>'.format(
                        esc(name), esc(chain[-1] if chain else "(モジュールの直下)"), len(lines),
                        "・".join(str(n) for n in lines), inline(" ".join(ids)) if ids else none))
    parts.append('<h3 class="dc-h3">15.3 例外の送出箇所</h3>')
    parts.append(tag_table(["<th>関数</th>", '<th class="num">箇所数</th>', "<th>行番号</th>", "<th>対応エラー</th>"], rows)
                 if rows else '<p class="dc-p">例外の送出箇所はない。</p>')

    rows = ['<tr><td class="dsc">{0}</td><td class="dsc">{1}</td></tr>'.format(esc(what), at(name, line, chain))
            for _, name, line, chain, what in [f for f in facts if f[0] == "broad"]]
    parts.append('<h3 class="dc-h3">15.4 例外の包括的な捕捉箇所</h3>')
    parts.append(tag_table(["<th>捕捉対象</th>", "<th>実装箇所</th>"], rows)
                 if rows else '<p class="dc-p">例外の包括的な捕捉箇所はない。</p>')
    return "\n".join(parts), missing


# ============================================================
# 自動生成する章
# ============================================================
def step_label(sid):
    """st-P01-03 を「P-01:03」と読める形にする。"""
    m = re.match(r"^st-P(\d{2})-(\d{2})$", sid)
    return "P-{0}:{1}".format(m.group(1), m.group(2)) if m else sid


def step_links(sids, base=""):
    """工程の並びを短くする。同じ番号の工程が続きの目的に並ぶときは「P-01〜08:01」とまとめる。"""
    marks = []
    for sid in sids:
        m = re.match(r"^st-P(\d{2})-(\d{2})$", sid)
        marks.append((int(m.group(1)), m.group(2), sid) if m else (None, "", sid))
    out, used = [], set()
    for index, (pno, step, sid) in enumerate(marks):
        if index in used:
            continue
        run = [index]
        if pno is not None:
            for later in range(index + 1, len(marks)):
                if later not in used and marks[later][1] == step \
                        and marks[later][0] == marks[run[-1]][0] + 1:
                    run.append(later)
        used.update(run)
        if len(run) >= 3:
            label = "P-{0:02d}〜{1:02d}:{2}".format(pno, marks[run[-1]][0], step)
        else:
            used.difference_update(run[1:])
            label = step_label(sid)
        out.append('<a href="{0}#{1}">{2}</a>'.format(base, sid, label))
    return "・".join(out)


def build_feature_list(specs, steps, flow_name=""):
    """第 7 章は処理の流れと機能仕様から組む。原稿には書かせない。"""
    # 列順は ID を左端に置き、識別子・数値の列(工程・E2E・判定)を右へ寄せる。
    # 工程は E2E の直前に置く(どの工程が何件の試験で押さえられているかを並べて読む)
    head = ["<th>ID</th>", "<th>機能</th>", "<th>概要</th>", '<th class="st">工程</th>',
            '<th class="num">E2E</th>', '<th class="jd">判定</th>']
    body = []
    for spec in specs:
        info = steps[spec["id"]]
        links = step_links(info["sids"], flow_name)
        if info["nested"]:
            links += " 内"
        _, names = parse_verify(spec["fields"]["検証"][0][0], spec["line_no"])
        name_cell = esc(spec["name"])
        cls = ' class="fn"'
        if info["nested"]:
            cls = ' class="fn sub"'
            if info["owner"]:
                name_cell += '<span class="via">{0} の内部で実行</span>'.format(esc(info["owner"]))
        body.append(
            '<tr><td class="id"><a href="#{0}">{1}</a></td>'
            '<td{2}>{3}</td><td class="dsc">{4}</td><td class="st">{5}</td>'
            '<td class="num">{6}</td><td class="jd">{7}</td></tr>'.format(
                anchor_of(spec["id"]), spec["id"], cls, name_cell,
                inline(spec["fields"]["概要"][0][0]), links,
                len(names), mark(spec["state"])))
    return tag_table(head, body)


def count_states(specs, rows, paths=()):
    tally = {"確定": 0, "暫定": 0, "未固定": 0}
    tests = set()
    for spec in specs:
        state, names = parse_verify(spec["fields"]["検証"][0][0], spec["line_no"])
        tally[spec["state"]] += 1
        tests.update(names)
    # 5.2 の経路も検証欄を持つ。ここに合流させることで、境界の記述にも
    # 「実在しないテスト名を書いていないか」の検査が効く
    for path in paths:
        state, names = parse_verify(path["fields"]["検証"][0][0], path["line_no"])
        tally[state] += 1
        tests.update(names)
    for no in (9, 10):
        for row in rows[no]:
            state, names = parse_verify(row["verify"], row["line_no"])
            tally[state] += 1
            tests.update(names)
    return tally, tests


def build_state_chapter(specs, rows, tally):
    head = ["<th>区分</th>", "<th>定義</th>", "<th>根拠</th>", '<th class="num">件数</th>']
    body = [
        '<tr><td>{0}</td><td class="dsc">インタビューで人が決定し、E2E で固定した記述</td>'
        '<td class="dsc">正式テスト</td><td class="num">{1}</td></tr>'.format(mark("確定"), tally["確定"]),
        '<tr><td>{0}</td><td class="dsc">現状の動作を観測して固定した記述。正否を人が確認していない</td>'
        '<td class="dsc">特性化テスト</td><td class="num">{1}</td></tr>'.format(mark("暫定"), tally["暫定"]),
        '<tr><td>{0}</td><td class="dsc">決定は存在するが、対応する試験が無い記述</td>'
        '<td class="none">試験なし</td><td class="num">{1}</td></tr>'.format(mark("未固定"), tally["未固定"]),
    ]
    out = [tag_table(head, body)]

    flagged = []
    for spec in specs:
        if spec["state"] != "確定":
            flagged.append((spec["state"], spec["id"], spec["name"], spec["fields"]["出力"][0][0]))
    for no in (9, 10):
        for row in rows[no]:
            state, _ = parse_verify(row["verify"], row["line_no"])
            if state != "確定":
                flagged.append((state, row["id"], row["cells"][0], row["cells"][-1]))
    if flagged:
        head2 = ["<th>区分</th>", "<th>該当箇所</th>", "<th>内容</th>"]
        body2 = ['<tr><td>{0}</td><td class="dsc"><a href="#{1}">{2}</a> {3}</td>'
                 '<td class="dsc">{4}</td></tr>'.format(
                     mark(state), anchor_of(code), code, esc(name), inline(note))
                 for state, code, name, note in flagged]
        out.append(tag_table(head2, body2))
    return "\n".join(out)


def build_toc(extra=()):
    head = ['<th class="num">章</th>', "<th>章名</th>", "<th>記載内容</th>"]
    body = ['<tr><td class="num"><a href="#ch{0}">{0}</a></td><td>{1}</td>'
            '<td class="dsc">{2}</td></tr>'.format(no, esc(name), esc(defines))
            for no, name, defines, _ in list(CHAPTERS) + list(extra)]
    return tag_table(head, body)


# ============================================================
# 組み立て
# ============================================================
def build(text, template, generated, code=None):
    """仕様書の本体だけを返す(別紙も要るときは build_pages)。"""
    page, _, stats = build_pages(text, template, generated, code)
    return page, stats


def build_pages(text, template, generated, code=None, spec_name="spec.html", flow_name="spec-flow.html",
                analyzers=()):
    """仕様書の本体と、別紙「処理の流れ」を作る。(本体, 別紙, 件数) を返す。

    code はソースのフォルダ(任意)。渡すと実装の実在を確かめ、コードと保守の章を足す。
    """
    meta, chapters = parse(text)
    validate(meta, chapters)
    specs = read_specs(chapters)
    rows = read_rows(chapters)
    where = read_impl(chapters, rows[9])
    code = CodeBase(code, load_analyzers(analyzers)) if code else None
    for spec in specs:
        spec["impl"] = []
        for value, line_no in spec["fields"].get("実装", ()):
            refs = parse_refs(value, line_no)
            if code:
                for f, fn in refs:
                    code.locate(f, fn, line_no)
            spec["impl"] += refs
    if code:
        for eid, refs in where.items():
            for f, fn in refs:
                code.locate(f, fn, chapter(chapters, 9).line_no)
    purpose_names = []
    palette = Palette(code.modules() if code else
                      [f for spec in specs for f, _ in spec["impl"]])
    flows = []
    store_names = [split_cells(b.head)[0].strip()
                   for ch in chapters for b in all_blocks(ch) if b.kind == "store"]

    steps = {}
    fig_no = {3: 0, 4: 0}
    rendered = {}
    # 境界図(5.1)と経路ごとの仕様(5.2)は同じ @io から作る。先に 1 回だけ解析して、
    # 図とブロックの両方へ同じ結果を渡す(同じ内容を 2 か所で解釈させない)
    io_data, io_at, paths_at = None, 0, 0
    for ch in chapters:
        for holder in [ch] + ch.subs:
            for block in holder.blocks:
                if block.kind == "io":
                    if io_data is not None:
                        fail(block.line_no, "@io は 1 つだけ書いてください。")
                    io_data, io_at = parse_io(block), block.line_no
                elif block.kind == "paths":
                    paths_at = block.line_no
    # 書き忘れると経路の仕様が黙って消える。図だけ残って中身が無い状態を防ぐ
    if io_data and not paths_at:
        fail(io_at, "@io にインターフェースを書いたら、5.2 に @paths も書いてください"
                    "(書かないと {0} 件のインターフェースの詳細が出力に出ません)。".format(len(io_data["paths"])))
    # 3.2 で定義された目的を先に集める(同じ節の流れが参照する)
    purposes = []
    stores, links, current_store = [], [], ""
    errors_in_flow = set()
    for ch in chapters:
        for holder in [ch] + ch.subs:
            for block in holder.blocks:
                if block.kind == "purpose":
                    pid, pname = parse_purpose(block)
                    if pid in purposes:
                        fail(block.line_no, "目的 {0} が 2 回定義されています。".format(pid))
                    purposes.append(pid)
                    purpose_names.append((pid, pname))
    flows_seen = []
    # 4.1 構成要素の 1 列目。4.2 の関係図がこれを全部描いているかを確かめる
    parts_listed = []
    for ch in chapters:
        for holder in [ch] + ch.subs:
            if getattr(holder, "name", "") != "構成要素":
                continue
            for block in holder.blocks:
                if block.kind == "table":
                    for _, line, _ in block.rows:
                        first = line.split("|")[0].strip()
                        if first:
                            parts_listed.append(first)

    for ch in chapters:
        parts = []
        for holder in [ch] + ch.subs:
            if holder is not ch:
                parts.append('<h3 class="dc-h3">{0} {1}</h3>'.format(esc(holder.no), esc(holder.name)))
            after_holder = []
            after_flow = ""
            for block in holder.blocks + [None]:
                if block is None:
                    parts += after_holder
                    break
                prev_flow, after_flow = after_flow, ""
                if block.kind == "p":
                    if getattr(holder, "name", "") == "概要" and ch.no == 3 \
                            and re.match(r"^\*\*(できること|やらないこと)", block.head.strip()):
                        fail(block.line_no,
                             "3.1 の段落を「できること。」「やらないこと。」で始めません。"
                             "全体のまとめ → 何ができるか → 何を対象外としたか、の順に文章で書きます。")
                    parts.append('<p class="dc-p">{0}</p>'.format(inline(block.head)))
                    if ch.no == 3 and flows and not flows[-1]["html"] \
                            and getattr(holder, "name", "") == "ユースケース":
                        flows[-1]["intro"].append(block.head)
                elif block.kind == "store":
                    current_store = split_cells(block.head)[0].strip()
                    stores.append(current_store)
                    parts.append(render_store(block))
                elif block.kind == "schema":
                    parts.append(render_schema(block, current_store, links))
                elif block.kind == "purpose":
                    parts.append(render_purpose(block))
                    flows.append({"pid": parse_purpose(block)[0], "name": parse_purpose(block)[1],
                                  "intro": [], "html": "", "steps": [], "items": [],
                                  "fields": purpose_fields(block)})
                elif block.kind == "def":
                    parts.append(render_def(block))
                elif block.kind == "table":
                    is_parts = getattr(holder, "name", "") == "構成要素"
                    parts.append(render_table(block, is_parts, module_links(specs, where) if is_parts else None))
                elif block.kind == "code":
                    parts.append(render_code(block))
                elif block.kind == "io":
                    fig_no[ch.no] = fig_no.get(ch.no, 0) + 1
                    parts.append(render_io(block, "{0}-{1}".format(ch.no, fig_no[ch.no]),
                                           io_data))
                elif block.kind == "paths":
                    parts.append(render_paths(io_data, block.line_no))
                elif block.kind == "graph":
                    fig_no[ch.no] = fig_no.get(ch.no, 0) + 1
                    outsiders = [peer for peer, _, _ in io_data["peers"]] if io_data else []
                    for other in chapters:
                        outsiders += [split_cells(b.head)[0].strip()
                                      for b in all_blocks(other) if b.kind == "store"]
                    figure = render_graph(block, "{0}-{1}".format(ch.no, fig_no[ch.no]),
                                          parts_listed, code.imports() if code else (),
                                          outsiders, palette)
                    cut = figure.rindex("</div>")
                    parts.append(figure[:cut] + '<p class="dc-fig-c">モジュールを選択したときの青色の矢印は参照先を、'
                                 "橙色の矢印は参照元を表す。"
                                 + "</p>" + figure[cut:])
                elif block.kind == "flow":
                    fig_no[ch.no] = fig_no.get(ch.no, 0) + 1
                    pid = block.head.strip()
                    if not PID_RE.match(pid):
                        fail(block.line_no,
                             "流れは「@flow P-01」の形で目的を指してください: {0}".format(pid[:30]))
                    if pid not in purposes:
                        fail(block.line_no,
                             "{0} という目的が 3.2 にありません。@purpose で先に定義してください。".format(pid))
                    fig_no[ch.no] -= 1          # 流れ図は別紙に出す。本体の図の番号は使わない
                    flow_steps, flow_errors, ordered, flow_items = read_flow(block, pid)
                    target = next(f for f in flows if f["pid"] == pid)
                    target["html"], target["steps"], target["items"] = "read", ordered, flow_items
                    target["caption"] = ""
                    errors_in_flow.update(flow_errors)
                    flows_seen.append(pid)
                    for fid, info in flow_steps.items():
                        entry = steps.setdefault(
                            fid, {"sids": [], "nested": True, "owner": ""})
                        entry["sids"] += info["sids"]
                        if not info["nested"]:
                            entry["nested"] = False
                        if info["owner"]:
                            entry["owner"] = info["owner"]
                    after_flow = pid
                elif block.kind == "spec":
                    parts.append(render_spec(next(s for s in specs if s["line_no"] == block.line_no), code))
                elif block.kind in ("rows", "impl"):
                    continue
                elif block.kind == "caption" and prev_flow:
                    # 流れの脚注は、流れ図と一緒に別紙へ持っていく
                    target = flows[[f["pid"] for f in flows].index(prev_flow)]
                    target["caption"] = block.head
                elif block.kind == "caption":
                    if not parts or "dc-fig" not in parts[-1]:
                        fail(block.line_no, "@caption は図(@io / @graph / @flow)の直後に置いてください。")
                    cut = parts[-1].rindex("</div>")
                    parts[-1] = (parts[-1][:cut]
                                 + '<p class="dc-fig-c">{0}</p>'.format(inline(block.head))
                                 + parts[-1][cut:])
        if ch.no == 6 and stores:
            for src, dst, column, line_no, _ in links:
                if dst not in stores:
                    fail(line_no, "FK の参照先「{0}」が見つかりません。"
                                  "@store で定義したデータストアの名前を書きます。".format(dst))
            fig_no[6] = fig_no.get(6, 0) + 1
            parts.insert(0, er_svg(stores, links, "6-{0}".format(fig_no[6])))
        if ch.no == 6:
            data_use, drawn = render_data_flow(
                specs, steps, purpose_names, stores, io_data, 6, fig_no.get(6, 0) + 1,
                [(name, [f["pid"] for f in members]) for name, members in usecase_groups(flows)])
            if data_use:
                fig_no[6] = fig_no.get(6, 0) + drawn
                parts.append(data_use)
        if ch.no == 3 and purpose_names:
            actors, reach = [], {}
            for flow in flows:
                for who in re.split(r"[、,]\s*", flow["fields"]["誰が"]):
                    who = who.strip()
                    if who and who not in actors:
                        actors.append(who)
                    if who:
                        reach.setdefault(who, []).append(flow["pid"])
            if actors:
                fig_no[3] = fig_no.get(3, 0) + 1
                figure = usecase_svg(io_data["self_name"] if io_data else meta["題名"].replace(" 仕様書", ""),
                                     actors, usecase_groups(flows), reach, flow_name,
                                     "3-{0}".format(fig_no[3]))
                # 図の読み方の説明は付けない(ユースケース図の記法は一般的なもので、説明は要らない)
                at = next(i for i, part in enumerate(parts) if "ユースケース</h3>" in part)
                parts[at + 1:at + 1] = [figure, usecase_table(flows, flow_name)]
        if ch.no == 9:
            check_error_flow(rows[9], errors_in_flow)
        if ch.no in ROW_COLUMNS:
            parts.append(render_rows_fixed(ch.no, rows[ch.no], where if ch.no == 9 else None, code))
        rendered[ch.no] = "\n".join(parts)

    # 目的と流れの対応。定義したのに流れが無い目的を残さない
    for pid in purposes:
        if pid not in flows_seen:
            raise SpecError(
                "目的 {0} に対応する流れが 3.2 にありません。「@flow {0}」を書いてください。".format(pid))
    if not purposes:
        raise SpecError("3.2 に目的(@purpose)が 1 つもありません。")

    # 横断: 第 8 章の全機能が、いずれかの流れの工程に現れる
    for spec in specs:
        if spec["id"] not in steps:
            fail(spec["line_no"], "{0} がどの流れの工程にも現れません。"
                 "工程の行に「| {0}」を添えてください。".format(spec["id"]))
    known = {spec["id"] for spec in specs}
    for fid in steps:
        if fid not in known:
            raise SpecError("処理の流れが {0} を指していますが、第 8 章に @spec がありません。".format(fid))

    tally, tests = count_states(specs, rows, io_data["paths"] if io_data else ())

    extra = [CODE_CHAPTER] if code else []
    sections = [cover(meta, generated, specs, rows, tally, tests),
                section(0, "目次", "章の構成", build_toc(extra))]
    for no, name, _, authored in CHAPTERS:
        if no == 7:
            html_body = build_feature_list(specs, steps, flow_name)
        elif no == 13:
            html_body = build_state_chapter(specs, rows, tally)
        else:
            html_body = rendered[no]
        sections.append(section(no, "第 {0} 章".format(no), name, html_body))
    unlinked = None
    if code:
        html_body, unlinked = build_maintenance(code, specs, where, text)
        sections.append(section(CODE_CHAPTER[0], "第 {0} 章".format(CODE_CHAPTER[0]), CODE_CHAPTER[1], html_body))

    page = template
    for token, value in (("{{TITLE}}", esc(meta["題名"].replace(" 仕様書", ""))),
                         ("{{SOURCE_LABEL}}", esc(meta["生成元"])),
                         ("{{CONTENT}}", "\n".join(sections))):
        page = page.replace(token, value)
    dangling = sorted(set(re.findall(r'href="#([a-z0-9-]+)"', page))
                      - set(re.findall(r'id="([a-z0-9-]+)"', page)))
    if dangling:
        raise SpecError("生成物のリンク先が見つかりません: {0}\n"
                        "参照している ID が本文に無いか、番号が食い違っています。".format(
                            " ".join("#" + d for d in dangling)))

    stats = {"機能": len(specs), "エラー": len(rows[9]), "非機能要件": len(rows[10]),
             "E2E": len(tests), "確定": tally["確定"], "暫定": tally["暫定"], "未固定": tally["未固定"]}
    if unlinked is not None:
        stats["仕様に記載なし"] = unlinked
    flow_page = template
    for token, value in (("{{TITLE}} 仕様書", esc(meta["題名"].replace(" 仕様書", "")) + " 処理の流れ"),
                         ("{{SOURCE_LABEL}}", esc(meta["生成元"])),
                         ("{{CONTENT}}", build_flow_page(flows, specs, rows[9], where, code, spec_name, palette))):
        flow_page = flow_page.replace(token, value)
    return page, flow_page, stats


def section(no, label, name, body):
    anchor = "ch{0}".format(no) if no else "toc"
    return ('<section class="dc-sec">\n'
            '<h2 class="dc-ch" id="{0}"><span class="no">{1}</span><span class="nm">{2}</span></h2>\n'
            "{3}\n</section>").format(anchor, esc(label), esc(name), body)


def cover(meta, generated, specs, rows, tally, tests):
    chips = [("機能", len(specs), False), ("エラー", len(rows[9]), False),
             ("非機能要件", len(rows[10]), False), ("E2E", len(tests), False),
             ("確定", tally["確定"], False), ("暫定", tally["暫定"], tally["暫定"] > 0)]
    if tally["未固定"]:
        chips.append(("未固定", tally["未固定"], True))
    counts = "".join('<span{0}><b>{1}</b>{2}</span>'.format(
        ' class="w"' if warn else "", value, esc(label)) for label, value, warn in chips)
    body = []
    for key in ("版", "生成元", "仕様の正", "更新方法"):
        value = {"版": generated + " 生成", "生成元": meta["生成元"],
                 "仕様の正": meta.get("仕様の正", "`tests/e2e/` のテストコード。本書との差異は本書の誤り"),
                 "更新方法": meta.get("更新方法", "再生成による全体置換。本書を直接編集しない")}[key]
        body.append('<tr><th>{0}</th><td class="dsc">{1}</td></tr>'.format(key, inline(value)))
    return ('<section class="dc-sec">\n'
            '<h1 class="dc-title">{0}</h1>\n<p class="dc-sub">{1}</p>\n{2}\n'
            '<div class="dc-cnt">{3}</div>\n</section>').format(
        esc(meta["題名"]), esc(meta["対象"]), tag_table([], body), counts)


TERM_COLUMNS = ["種類", "名称", "原稿の行", "意味", "一般性", "名詞性", "一意性",
                "代替案(意味・一般性・名詞性・一意性)", "判定"]


def collect_terms(text):
    """採点の対象になる用語と名称を、原稿から出てくる順に集める。[(種類, 名称, 行番号)]。

    どの語を採点するかを査読者に任せると、見落としが起きる。対象は機械が数え上げ、
    査読者は点を付けることだけを行う(references/term-scoring.md)。
    """
    _, chapters = parse(text)
    out, seen = [], set()

    def add(kind, name, line_no):
        name = name.strip().strip("`")
        if name and (kind, name) not in seen:
            seen.add((kind, name))
            out.append((kind, name, line_no))

    for ch in chapters:
        for holder in [ch] + ch.subs:
            for block in holder.blocks:
                if block.kind == "def" and ch.no == 2:
                    for _, line, line_no in block.rows:
                        add("用語", split_kv(line, line_no)[0], line_no)
                elif block.kind == "purpose":
                    m = PURPOSE_RE.match(block.head.strip())
                    if m:
                        add("ユースケース", m.group(2), block.line_no)
                elif block.kind == "spec":
                    head = split_cells(block.head)[0] if block.head.strip() else ""
                    add("機能", re.sub(r"^F-\d{2}\s*", "", head), block.line_no)
                elif block.kind == "io":
                    for row_kind, line, line_no in block.rows:
                        if row_kind == "@peer":
                            add("外部エンティティ", line, line_no)
                    for path in parse_io(block)["paths"]:
                        add("インターフェース", path["label"], path["line_no"])
                elif block.kind == "store":
                    add("データストア", split_cells(block.head)[0] if block.head.strip() else "", block.line_no)
                elif block.kind == "schema":
                    for _, line, line_no in block.rows:
                        cells = split_cells(line)
                        if len(cells) == len(SCHEMA_COLUMNS):
                            add("データ項目", cells[1], line_no)
    return out


def terms_table(terms):
    """採点表のひな形(Markdown)。点と判定の欄は空で、査読者が埋める。"""
    lines = ["# 用語・名称の採点表", "",
             "基準と決め方は references/term-scoring.md。一覧の語は 1 つも飛ばさずに採点する。", "",
             "| " + " | ".join(TERM_COLUMNS) + " |", "|" + "---|" * len(TERM_COLUMNS)]
    for kind, name, line_no in terms:
        lines.append("| {0} | {1} | {2} | | | | | | |".format(kind, name.replace("|", "\\|"), line_no))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="仕様原稿から docs/spec.html を組み立てる")
    parser.add_argument("--src", required=True, help="仕様原稿(行形式)")
    parser.add_argument("--out", default="docs/spec.html", help="出力先(既定: docs/spec.html)")
    parser.add_argument("--date", default=None, help="生成日 YYYY-MM-DD(既定: 今日)")
    parser.add_argument("--template", default=None, help="外枠(既定: スクリプトの隣)")
    parser.add_argument("--code", default=None,
                        help="ソースのフォルダ(任意)。渡すと実装の実在を確かめ、コードと保守の章を足す")
    parser.add_argument("--analyzers", action="append", default=[],
                        help="言語の解析部品を足すフォルダ(任意。複数可)。原稿の隣の .spec-analyzers/ は自動で読む")
    parser.add_argument("--terms", default=None,
                        help="用語・名称の採点表のひな形を書き出す先(任意)。査読で使う")
    args = parser.parse_args(argv)

    template_path = pathlib.Path(args.template) if args.template \
        else pathlib.Path(__file__).with_name("spec_template.html")
    try:
        template = template_path.read_text(encoding="utf-8")
        source = pathlib.Path(args.src).read_text(encoding="utf-8")
    except OSError as error:
        print("読み込めません: {0}".format(error), file=sys.stderr)
        return 1

    try:
        out = pathlib.Path(args.out)
        flow_out = out.with_name(out.stem + "-flow" + out.suffix)
        page, flow_page, stats = build_pages(
            source, template, args.date or datetime.date.today().isoformat(),
            code=args.code, spec_name=out.name, flow_name=flow_out.name,
            analyzers=[str(pathlib.Path(args.src).parent / ".spec-analyzers")] + args.analyzers)
    except SpecError as error:
        print("spec.html を生成できません。原稿を直して再実行してください。\n\n{0}".format(error),
              file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8", newline="\n")
    flow_out.write_text(flow_page, encoding="utf-8", newline="\n")
    if args.terms:
        terms = collect_terms(source)
        terms_out = pathlib.Path(args.terms)
        terms_out.parent.mkdir(parents=True, exist_ok=True)
        terms_out.write_text(terms_table(terms), encoding="utf-8", newline="\n")
        print("{0} に採点の対象 {1} 件を書き出しました。".format(terms_out, len(terms)))
    print("{0} と {2} を生成しました({1})".format(
        out, "・".join("{0} {1}".format(k, v) for k, v in stats.items()), flow_out.name))
    if stats["未固定"]:
        print("注意: 決定はあるが試験が無い記述が {0} 件あります(12 章)。".format(stats["未固定"]),
              file=sys.stderr)
    if stats.get("仕様に記載なし"):
        print("注意: ソースにあって仕様に対応が無いものが {0} 件あります(15 章)。".format(stats["仕様に記載なし"]),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    (2, "システム概要", "入出力の形と、対応・非対応の用途", True),
    (3, "システム構成", "構成要素、要素間の関係、処理の流れ、動作環境", True),
    (4, "インターフェース仕様", "コマンドライン引数、標準出力、終了コード、生成 HTML の構造", True),
    (5, "データ仕様", "読み書きするデータの所在・形式・実行後の状態", True),
    (6, "機能一覧", "機能の全件と確定状況", False),
    (7, "機能仕様", "機能ごとの実行契機・前提条件・処理・出力・エラー", True),
    (8, "エラー仕様", "拒否する入力と、そのときの挙動", True),
    (9, "非機能要件", "機能に依らず常に成立する条件", True),
    (10, "適用範囲外", "意図的に対象としない事項", True),
    (11, "設計判断", "決定とその理由", True),
    (12, "仕様の確定状況", "各記述がどの根拠で固定されているか", False),
    (13, "未確定事項", "決まっていない振る舞いと、確定に必要な確認事項", True),
    (14, "用語定義", "本書で用いる語の定義", True),
]
AUTHORED = [c for c in CHAPTERS if c[3]]

REQUIRED_SUBS = {
    2: ["入出力", "適用場面"],
    3: ["構成要素", "要素間の関係", "処理の流れ", "動作環境"],
    4: ["コマンドライン", "標準出力・終了コード", "生成する HTML の構造"],
}

# @rows の列は章が決める。モデルは行だけを書く
ROW_COLUMNS = {
    8: (["ID", "判定条件", "メッセージ内容"], True),
    9: (["ID", "分類", "条件"], True),
    10: (["ID", "対象外の事項", "理由"], False),
    11: (["ID", "決定", "理由"], False),
    13: (["ID", "項目", "現在の動作", "確定に必要な確認事項"], False),
}
ROW_PREFIX = {8: "E", 9: "N", 10: "X", 11: "D", 13: "U"}

SPEC_FIELDS = ["概要", "実行契機", "前提条件", "入力", "処理", "出力", "エラー", "検証"]
SPEC_ROWS = ["実行契機", "前提条件", "入力", "処理", "出力", "エラー", "検証"]
STATES = {"確定": "ok", "暫定": "tmp", "未固定": "non"}

ALLOWED = {
    1: {"def"},
    2: {"p", "def", "table", "io", "caption"},
    3: {"p", "def", "table", "graph", "flow", "code", "caption"},
    4: {"p", "def", "table", "code"},
    5: {"p", "def", "table"},
    7: {"p", "spec"},
    8: {"p", "rows"},
    9: {"p", "rows"},
    10: {"p", "rows"},
    11: {"p", "rows"},
    13: {"p", "rows"},
    14: {"p", "def"},
}

ID_RE = re.compile(r"(?<![0-9A-Za-z])([FENXDU]-\d{2})(?![0-9A-Za-z])")
TEST_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SpecError(Exception):
    """原稿が書式に従っていない。メッセージは行番号と直し方を含める。"""


def fail(line_no, message):
    raise SpecError("{0} 行目: {1}".format(line_no, message))


# ============================================================
# 解析
# ============================================================
class Block(object):
    def __init__(self, kind, line_no):
        self.kind = kind
        self.line_no = line_no
        self.rows = []
        self.head = ""


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
            elif name in ("def", "table", "io", "graph", "flow", "rows", "spec"):
                block = Block(name, line_no)
                block.head = rest
                target.blocks.append(block)
            elif name in ("hub", "left"):
                if block is None or block.kind != "graph":
                    fail(line_no, "@{0} は @graph の中だけで使えます。".format(name))
                block.rows.append(("@" + name, rest, line_no))
                block = block
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
            "第 6 章(機能一覧)と第 12 章(仕様の確定状況)は原稿に書きません。"
            "第 7 章と各章の内容から自動で作られます。".format(
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


def read_specs(chapters):
    """第 7 章の機能仕様を読み、ID 順の検証まで行う。"""
    specs = []
    ch = chapter(chapters, 7)
    for block in ch.blocks:
        if block.kind != "spec":
            continue
        head = split_cells(block.head)
        if len(head) != 2:
            fail(block.line_no, "@spec は「@spec F-01 機能名 | 確定」の形で書いてください。")
        m = re.match(r"^(F-\d{2})\s+(.+)$", head[0])
        if not m:
            fail(block.line_no, "@spec の先頭は「F-01 機能名」の形にしてください。")
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
        raise SpecError("第 7 章に @spec がありません。機能を 1 つ以上書いてください。")
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
        if not TEST_RE.match(name):
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


def render_table(block):
    if not block.head:
        fail(block.line_no, "@table には列名が必要です(例: @table 用途 | 可否 | 根拠)。")
    columns = split_cells(block.head)
    head = ["<th>{0}</th>".format(esc(c)) for c in columns]
    body = []
    for _, line, line_no in block.rows:
        cells = split_cells(line)
        if len(cells) != len(columns):
            fail(line_no, "列数が見出しと合いません。見出しは {0} 列、この行は {1} 列です。".format(
                len(columns), len(cells)))
        tds = ['<td class="dsc">{0}</td>'.format(inline(c)) for c in cells]
        body.append("<tr>" + "".join(tds) + "</tr>")
    return tag_table(head, body)


def render_code(block):
    return '<pre class="dc-pre">{0}</pre>'.format(esc("\n".join(block.rows).strip("\n")))


def render_io(block, number):
    boxes = []
    for index, (_, line, line_no) in enumerate(block.rows):
        key, value = split_kv(line, line_no)
        items = [i.strip() for i in value.split("|") if i.strip()]
        cls = " mid" if index == 1 else ""
        lis = "".join("<li>{0}</li>".format(esc(i)) for i in items)
        boxes.append('<div class="bx{0}"><h5>{1}</h5><ul>{2}</ul></div>'.format(cls, esc(key), lis))
    if len(boxes) != 3:
        fail(block.line_no, "@io は「入力・処理・出力」の 3 行で書いてください。実際は {0} 行でした。".format(len(boxes)))
    inner = boxes[0] + '<div class="ar" aria-hidden="true">→</div>' + boxes[1] \
        + '<div class="ar" aria-hidden="true">→</div>' + boxes[2]
    return fig("入出力の形", number, '<div class="dc-io">{0}</div>'.format(inner))


def fig(title, number, inner, caption=""):
    out = ['<div class="dc-fig">',
           '<div class="dc-fig-h"><span>{0}</span><span class="no">図 {1}</span></div>'.format(
               esc(title), esc(number)),
           '<div class="dc-fig-b">{0}</div>'.format(inner)]
    if caption:
        out.append('<p class="dc-fig-c">{0}</p>'.format(inline(caption)))
    out.append("</div>")
    return "\n".join(out)


def render_graph(block, number):
    hub, hub_note, left = "", "", ""
    edges = []
    for kind, line, line_no in block.rows:
        if kind == "@hub":
            parts = split_cells(line)
            hub, hub_note = parts[0], (parts[1] if len(parts) > 1 else "")
            continue
        if kind == "@left":
            left = line.strip()
            continue
        m = re.match(r"^(.+?)\s*->\s*(.+?)\s*:\s*(.+)$", line)
        if not m:
            fail(line_no, "関係は「A -> B: ラベル」の形で書いてください: {0}".format(line[:40]))
        edges.append((m.group(1).strip(), m.group(2).strip(), m.group(3).strip(), line_no))
    if not hub:
        fail(block.line_no, "@graph には @hub で中心の要素を指定してください。")
    if not left:
        fail(block.line_no, "@graph には @left で左に置く要素を指定してください。")

    to_left = [e for e in edges if e[0] == hub and e[1] == left]
    from_left = [e for e in edges if e[0] == left and e[1] == hub]
    spokes = []
    for src, dst, label, line_no in edges:
        if {src, dst} == {hub, left}:
            continue
        if src != hub:
            fail(line_no, "中心 ({0}) から出ない関係は書けません: {1} -> {2}".format(hub, src, dst))
        spokes.append((dst, label))

    def node(name, note="", cls=""):
        extra = "<small>{0}</small>".format(esc(note)) if note else ""
        return '<span class="dc-nd{0}">{1}{2}</span>'.format(cls, esc(name), extra)

    def edge(label, reverse=False):
        return '<div class="dc-gl{0}"><span>{1}</span></div>'.format(
            " rev" if reverse else "", esc(label))

    left_edges = "".join(edge(e[2]) for e in from_left) + "".join(edge(e[2], True) for e in to_left)
    cells = ['<div class="n-u">{0}</div>'.format(node(left)),
             '<div class="e-u dc-ge">{0}</div>'.format(left_edges),
             '<div class="n-c">{0}</div>'.format(node(hub, hub_note, " k"))]
    if not spokes:
        fail(block.line_no, "@graph に中心から出る関係が 1 本もありません。")
    for index, (dst, label) in enumerate(spokes, 1):
        cells.append('<div class="sp-e dc-ge" style="grid-row:{0}">{1}</div>'.format(index, edge(label)))
        cells.append('<div class="sp-n" style="grid-row:{0}">{1}</div>'.format(index, node(dst)))
    # grid-row の -1 は「明示グリッドの終端」しか指さないため、行数を必ず宣言する。
    # 宣言しないと中心と左の要素が全行にまたがらず、図が崩れる。
    style = ' style="grid-template-rows: repeat({0}, auto)"'.format(len(spokes))
    return fig("要素間の関係", number,
               '<div class="dc-gr"{0}>{1}</div>'.format(style, "".join(cells)))


def render_flow(block, number):
    steps = {}
    counter = [0]

    owners = []

    def node(kind, text, ids=(), side=False, line_no=0):
        if kind == "端子":
            return '<div class="dc-fc-n t">{0}</div>'.format(inline(text))
        if kind == "終了":
            return '<div class="dc-fc-n t stop">{0}</div>'.format(inline(text))
        if kind == "判断":
            return '<div class="dc-fc-d"><span>{0}</span></div>'.format(inline(text))
        if side:
            # 分岐の中の工程は本線の番号を消費しない
            return '<div class="dc-fc-n e">{0}</div>'.format(inline(text))
        counter[0] += 1
        sid = "st{0:02d}".format(counter[0])
        # 先頭の ID がその工程の担当。2 つ目以降はその内部で実行される機能
        for index, fid in enumerate(ids):
            entry = steps.setdefault(fid, {"sids": [], "nested": True, "owner": ""})
            entry["sids"].append(sid)
            if index == 0:
                entry["nested"] = False
            else:
                entry["owner"] = ids[0]
        if ids:
            owners.append((ids[0], line_no))
        chip = ""
        if ids:
            chip = '<span class="fid">{0}</span>'.format(
                "&nbsp;".join(inline(f) for f in ids))
        return '<div class="dc-fc-n" id="{0}">{1}{2}</div>'.format(sid, inline(text), chip)

    # 本線は項目の並び。判断はそれぞれ自分の枝を持つ(分岐の数に制限は無い)
    items = []
    open_decision = None      # いま枝を受け付けている判断
    awaiting = None           # 直前が判断で、枝を待っている状態
    in_branch = False

    for _, raw, line_no in block.rows:
        line = raw.strip()
        kind = line.split(None, 1)[0] if line else ""
        rest = line[len(kind):].strip()
        if kind not in ("端子", "工程", "判断", "枝", "終了"):
            fail(line_no, "工程の種類は 端子 / 工程 / 判断 / 枝 / 終了 のいずれかです: {0}".format(line[:30]))

        if kind == "枝":
            if open_decision is None:
                fail(line_no, "枝 は 判断 の下にだけ置けます。対応する 判断 がありません。")
            if not rest:
                fail(line_no, "枝 にはラベルが必要です(例: 枝 はい)。")
            if not in_branch:
                in_branch = True
                awaiting = None
                open_decision["branch_label"] = rest
            else:
                in_branch = False
                open_decision["else_label"] = rest
                open_decision = None
            continue

        if awaiting is not None:
            fail(line_no, "判断 の次の行は 枝 です。分岐に入る側のラベルを先に書いてください。")

        text, ids = rest, []
        if "|" in rest:
            text, tail = [c.strip() for c in rest.split("|", 1)]
            ids = tail.split()
            for fid in ids:
                if not re.match(r"^F-\d{2}$", fid):
                    fail(line_no, "工程に添える ID は F-01 の形です。「{0}」は使えません。".format(fid))
            if in_branch and ids:
                fail(line_no, "分岐の中の工程に機能 ID は添えられません(番号を持たないため)。")
        html_node = node(kind, text, ids, in_branch, line_no)

        if in_branch:
            open_decision["branch"].append(html_node)
        else:
            item = {"html": html_node, "decision": kind == "判断",
                    "branch": [], "branch_label": "", "else_label": ""}
            items.append(item)
            if kind == "判断":
                open_decision = item
                awaiting = item

    if in_branch or awaiting is not None:
        fail(block.line_no, "判断 の分岐が閉じていません。枝 は 2 回(分岐に入る側・本線に戻る側)書きます。")
    if not items:
        fail(block.line_no, "@flow に工程がありません。")
    for item in items:
        if item["decision"] and not item["branch"]:
            fail(block.line_no, "判断 に分岐の中身がありません。枝 の下に工程を 1 つ以上書いてください。")

    out = []
    pending_label = ""
    for index, item in enumerate(items):
        if index:
            span = '<span>{0}</span>'.format(esc(pending_label)) if pending_label else ""
            out.append('<div class="dc-c">{0}</div>'.format(span))
            pending_label = ""
        out.append(item["html"])
        if item["branch"]:
            side = '<div class="dc-c"></div>'.join(item["branch"])
            out.append('<div class="dc-fork"><div class="dc-elb"><span>{0}</span></div>'
                       '<div class="dc-side">{1}</div></div>'.format(esc(item["branch_label"]), side))
            pending_label = item["else_label"]
    prev = ""
    for owner, line_no in owners:
        if owner < prev:
            fail(line_no, "機能 ID は処理の流れの順に振ります。{0} が {1} より後に現れています。".format(
                owner, prev))
        prev = owner
    return fig("処理の流れ", number, '<div class="dc-fc">{0}</div>'.format("".join(out))), steps


def render_spec(spec):
    head = ('<div class="dc-blk-h" id="{0}"><span class="id">{1}</span>'
            '<span class="nm">{2}</span>{3}</div>').format(
        anchor_of(spec["id"]), spec["id"], esc(spec["name"]), mark(spec["state"]))
    rows = []
    for key in SPEC_ROWS:
        values = spec["fields"].get(key, [("なし", 0)])
        texts = [v for v, _ in values]
        if key == "検証":
            state, names = parse_verify(texts[0], values[0][1])
            if not names:
                cell = '<span class="none">なし</span>'
            else:
                cell = '<p class="dc-ev{0}">{1}</p>'.format(
                    " tmp" if state == "暫定" else "", "　".join(esc(n) for n in names))
        elif len(texts) > 1:
            cell = "<ol>" + "".join("<li>{0}</li>".format(inline(t)) for t in texts) + "</ol>"
        elif texts[0] in ("なし", "-"):
            cell = '<span class="none">なし</span>'
        else:
            cell = inline(texts[0])
        rows.append("<dt>{0}</dt><dd>{1}</dd>".format(esc(key), cell))
    return '<div class="dc-blk">{0}<dl class="dc-rows">{1}</dl></div>'.format(head, "".join(rows))


def render_rows_fixed(no, rows):
    """ID 付き条項表。列は章が決め、検証つきの章は条件セルにテスト名を添える。"""
    columns, has_verify = ROW_COLUMNS[no]
    head = ["<th>{0}</th>".format(esc(c)) for c in columns]
    if has_verify:
        head.append('<th class="jd">判定</th>')
    body = []
    for row in rows:
        tds = ['<td class="id" id="{0}">{1}</td>'.format(anchor_of(row["id"]), row["id"])]
        for index, cell in enumerate(row["cells"]):
            text = inline(cell)
            if has_verify and index == len(row["cells"]) - 1:
                state, names = parse_verify(row["verify"], row["line_no"])
                ev = "　".join(esc(n) for n in names) if names else "対応する試験なし（決定は 11 章）"
                text += '<span class="ev">{0}</span>'.format(ev)
            tds.append('<td class="dsc">{0}</td>'.format(text))
        if has_verify:
            state, _ = parse_verify(row["verify"], row["line_no"])
            tds.append('<td class="jd">{0}</td>'.format(mark(state)))
        body.append("<tr>" + "".join(tds) + "</tr>")
    return tag_table(head, body)


# ============================================================
# 自動生成する章
# ============================================================
def build_feature_list(specs, steps):
    """第 6 章は処理の流れと機能仕様から組む。原稿には書かせない。"""
    head = ['<th class="st">工程</th>', "<th>ID</th>", "<th>機能</th>", "<th>概要</th>",
            '<th class="num">E2E</th>', '<th class="jd">判定</th>']
    body = []
    for spec in specs:
        info = steps[spec["id"]]
        links = "・".join('<a href="#{0}">{1}</a>'.format(sid, sid[2:]) for sid in info["sids"])
        if info["nested"]:
            links += " 内"
        _, names = parse_verify(spec["fields"]["検証"][0][0], spec["line_no"])
        name_cell = esc(spec["name"])
        cls = ""
        if info["nested"]:
            cls = ' class="sub"'
            if info["owner"]:
                name_cell += '<span class="via">{0} の内部で実行</span>'.format(esc(info["owner"]))
        body.append(
            '<tr><td class="st">{0}</td><td class="id"><a href="#{1}">{2}</a></td>'
            '<td{3}>{4}</td><td class="dsc">{5}</td><td class="num">{6}</td>'
            '<td class="jd">{7}</td></tr>'.format(
                links, anchor_of(spec["id"]), spec["id"], cls, name_cell,
                inline(spec["fields"]["概要"][0][0]), len(names), mark(spec["state"])))
    return tag_table(head, body)


def count_states(specs, rows):
    tally = {"確定": 0, "暫定": 0, "未固定": 0}
    tests = set()
    for spec in specs:
        state, names = parse_verify(spec["fields"]["検証"][0][0], spec["line_no"])
        tally[spec["state"]] += 1
        tests.update(names)
    for no in (8, 9):
        for row in rows[no]:
            state, names = parse_verify(row["verify"], row["line_no"])
            tally[state] += 1
            tests.update(names)
    return tally, tests


def build_state_chapter(specs, rows, tally):
    head = ["<th>印</th>", "<th>定義</th>", "<th>根拠</th>", '<th class="num">件数</th>']
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
    for no in (8, 9):
        for row in rows[no]:
            state, _ = parse_verify(row["verify"], row["line_no"])
            if state != "確定":
                flagged.append((state, row["id"], row["cells"][0], row["cells"][-1]))
    if flagged:
        head2 = ["<th>印</th>", "<th>該当箇所</th>", "<th>内容</th>"]
        body2 = ['<tr><td>{0}</td><td class="dsc"><a href="#{1}">{2}</a> {3}</td>'
                 '<td class="dsc">{4}</td></tr>'.format(
                     mark(state), anchor_of(code), code, esc(name), inline(note))
                 for state, code, name, note in flagged]
        out.append(tag_table(head2, body2))
    return "\n".join(out)


def build_toc():
    head = ['<th class="num">章</th>', "<th>章名</th>", "<th>この章が定義するもの</th>"]
    body = ['<tr><td class="num"><a href="#ch{0}">{0}</a></td><td>{1}</td>'
            '<td class="dsc">{2}</td></tr>'.format(no, esc(name), esc(defines))
            for no, name, defines, _ in CHAPTERS]
    return tag_table(head, body)


# ============================================================
# 組み立て
# ============================================================
def build(text, template, generated):
    meta, chapters = parse(text)
    validate(meta, chapters)
    specs = read_specs(chapters)
    rows = read_rows(chapters)

    flow_html, steps = "", {}
    fig_no = {2: 0, 3: 0}
    rendered = {}

    for ch in chapters:
        parts = []
        for holder in [ch] + ch.subs:
            if holder is not ch:
                parts.append('<h3 class="dc-h3">{0} {1}</h3>'.format(esc(holder.no), esc(holder.name)))
            for block in holder.blocks:
                if block.kind == "p":
                    parts.append('<p class="dc-p">{0}</p>'.format(inline(block.head)))
                elif block.kind == "def":
                    parts.append(render_def(block))
                elif block.kind == "table":
                    parts.append(render_table(block))
                elif block.kind == "code":
                    parts.append(render_code(block))
                elif block.kind == "io":
                    fig_no[ch.no] = fig_no.get(ch.no, 0) + 1
                    parts.append(render_io(block, "{0}-{1}".format(ch.no, fig_no[ch.no])))
                elif block.kind == "graph":
                    fig_no[ch.no] = fig_no.get(ch.no, 0) + 1
                    parts.append(render_graph(block, "{0}-{1}".format(ch.no, fig_no[ch.no])))
                elif block.kind == "flow":
                    fig_no[ch.no] = fig_no.get(ch.no, 0) + 1
                    flow_html, steps = render_flow(block, "{0}-{1}".format(ch.no, fig_no[ch.no]))
                    parts.append(flow_html)
                elif block.kind == "spec":
                    parts.append(render_spec(next(s for s in specs if s["line_no"] == block.line_no)))
                elif block.kind == "rows":
                    continue
                elif block.kind == "caption":
                    if not parts or "dc-fig" not in parts[-1]:
                        fail(block.line_no, "@caption は図(@io / @graph / @flow)の直後に置いてください。")
                    cut = parts[-1].rindex("</div>")
                    parts[-1] = (parts[-1][:cut]
                                 + '<p class="dc-fig-c">{0}</p>'.format(inline(block.head))
                                 + parts[-1][cut:])
        if ch.no in ROW_COLUMNS:
            parts.append(render_rows_fixed(ch.no, rows[ch.no]))
        rendered[ch.no] = "\n".join(parts)

    for spec in specs:
        if spec["id"] not in steps:
            fail(spec["line_no"], "{0} が 3.3 の処理の流れに現れません。"
                 "工程の行に「| {0}」を添えてください。".format(spec["id"]))
    known = {spec["id"] for spec in specs}
    for fid in steps:
        if fid not in known:
            raise SpecError("処理の流れが {0} を指していますが、第 7 章に @spec がありません。".format(fid))

    tally, tests = count_states(specs, rows)

    sections = [cover(meta, generated, specs, rows, tally, tests),
                section(0, "目次", "章の構成", build_toc())]
    for no, name, _, authored in CHAPTERS:
        if no == 6:
            html_body = build_feature_list(specs, steps)
        elif no == 12:
            html_body = build_state_chapter(specs, rows, tally)
        else:
            html_body = rendered[no]
        sections.append(section(no, "第 {0} 章".format(no), name, html_body))

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

    stats = {"機能": len(specs), "エラー": len(rows[8]), "非機能要件": len(rows[9]),
             "E2E": len(tests), "確定": tally["確定"], "暫定": tally["暫定"], "未固定": tally["未固定"]}
    return page, stats


def section(no, label, name, body):
    anchor = "ch{0}".format(no) if no else "toc"
    return ('<section class="dc-sec">\n'
            '<h2 class="dc-ch" id="{0}"><span class="no">{1}</span><span class="nm">{2}</span></h2>\n'
            "{3}\n</section>").format(anchor, esc(label), esc(name), body)


def cover(meta, generated, specs, rows, tally, tests):
    chips = [("機能", len(specs), False), ("エラー", len(rows[8]), False),
             ("非機能要件", len(rows[9]), False), ("E2E", len(tests), False),
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="仕様原稿から docs/spec.html を組み立てる")
    parser.add_argument("--src", required=True, help="仕様原稿(行形式)")
    parser.add_argument("--out", default="docs/spec.html", help="出力先(既定: docs/spec.html)")
    parser.add_argument("--date", default=None, help="生成日 YYYY-MM-DD(既定: 今日)")
    parser.add_argument("--template", default=None, help="外枠(既定: スクリプトの隣)")
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
        page, stats = build(source, template, args.date or datetime.date.today().isoformat())
    except SpecError as error:
        print("spec.html を生成できません。原稿を直して再実行してください。\n\n{0}".format(error),
              file=sys.stderr)
        return 1

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8", newline="\n")
    print("{0} を生成しました({1})".format(
        out, "・".join("{0} {1}".format(k, v) for k, v in stats.items())))
    if stats["未固定"]:
        print("注意: 決定はあるが試験が無い記述が {0} 件あります(12 章)。".format(stats["未固定"]),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

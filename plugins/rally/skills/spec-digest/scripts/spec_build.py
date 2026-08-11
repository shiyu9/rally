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
    (3, "システム概要", "何をするものか、目的ごとの処理の流れ", True),
    (4, "システム構成", "構成要素、要素間の関係、動作環境", True),
    (5, "インターフェース仕様", "外部との境界と、経路ごとに何をどう授受するか", True),
    (6, "データ仕様", "読み書きするデータの所在・形式・実行後の状態", True),
    (7, "機能一覧", "機能の全件と確定状況", False),
    (8, "機能仕様", "機能ごとの実行契機・前提条件・処理・出力・エラー", True),
    (9, "エラー仕様", "拒否する入力と、そのときの挙動", True),
    (10, "非機能要件", "機能に依らず常に成立する条件", True),
    (11, "適用範囲外", "意図的に対象としない事項", True),
    (12, "設計判断", "決定とその理由", True),
    (13, "仕様の確定状況", "各記述がどの根拠で固定されているか", False),
    (14, "未確定事項", "決まっていない振る舞いと、確定に必要な確認事項", True),
]
AUTHORED = [c for c in CHAPTERS if c[3]]

REQUIRED_SUBS = {
    3: ["概要", "適用場面"],
    4: ["構成要素", "要素間の関係", "動作環境"],
    # 境界図(@io)と経路ごとの仕様(@paths)は同じ章に置く。図の矢印と経路の記述が
    # 離れていると、どちらか片方だけ直されて食い違うため
    5: ["外部との境界", "経路ごとの仕様"],
}

# 経路ごとの仕様(5.2)の項目。第 8 章の SPEC_ROWS と同じ考え方で、ここだけが順序を持つ。
# 内容=やり取りされるデータそのもの / 形式=その表現と運ばれ方 / 用途=受け取って何をするか。
# この 3 つは混ざりやすいので、format.md 側にも定義を書いてある
PATH_ROWS = ["内容", "相手", "手段", "形式", "契機", "用途", "エラー", "検証"]
# 相手と向きは @io の行から入るため、原稿には書かせない
PATH_AUTHORED_ROWS = [k for k in PATH_ROWS if k != "相手"]

# @rows の列は章が決める。モデルは行だけを書く
ROW_COLUMNS = {
    9: (["ID", "判定条件", "メッセージ内容", "流れ"], True),
    10: (["ID", "分類", "条件"], True),
    11: (["ID", "対象外の事項", "理由"], False),
    12: (["ID", "決定", "理由"], False),
    14: (["ID", "項目", "現在の動作", "確定に必要な確認事項"], False),
}
ROW_PREFIX = {9: "E", 10: "N", 11: "X", 12: "D", 14: "U"}

SPEC_FIELDS = ["概要", "実行契機", "前提条件", "入力", "処理", "出力", "エラー", "検証"]
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
    9: {"p", "rows"},
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
                block = None
                target.blocks.append(Block("purpose", line_no, head=rest))
            elif name in ("def", "table", "io", "graph", "flow", "rows", "spec",
                          "schema"):
                block = Block(name, line_no)
                block.head = rest
                target.blocks.append(block)
            elif name == "paths":
                # 経路ごとの仕様(5.2)。中身は @io が持つので、ここは差し込み位置だけを示す
                block = None
                target.blocks.append(Block("paths", line_no, head=rest))
            elif name == "path":
                if block is None or block.kind != "io":
                    fail(line_no, "@path は @io の中で、直前のやり取りの下に書いてください。")
                block.rows.append(("@path", rest, line_no))
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


PURPOSE_RE = re.compile(r"^(P-\d{2})\s+(.+)$")
PID_RE = re.compile(r"^P-\d{2}$")


def parse_purpose(block):
    """@purpose P-01 <目的名> を (ID, 名前) にする。"""
    m = PURPOSE_RE.match(block.head.strip())
    if not m:
        fail(block.line_no,
             "目的は「@purpose P-01 <目的名>」の形で書いてください: {0}".format(block.head[:40]))
    return m.group(1), m.group(2).strip()


def render_purpose(block):
    pid, name = parse_purpose(block)
    return ('<h4 class="dc-pp" id="{0}"><span class="no">{1}</span>{2}</h4>'.format(
        anchor_of(pid), esc(pid), inline(name)))


def render_store(block):
    """@store <データの名前> | <保管場所> の見出し。"""
    parts = split_cells(block.head)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        fail(block.line_no,
             "データは「@store <名前> | <保管場所>」の形で書いてください: {0}".format(
                 block.head[:40]))
    return ('<h4 class="dc-st"><span class="nm">{0}</span>'
            '<span class="at">{1}</span></h4>').format(inline(parts[0]), inline(parts[1]))


SCHEMA_COLUMNS = ["データ名", "型", "下限値", "上限値", "単位",
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
    classes = ["", "ty", "ln", "ln", "un", "ky", "ky", "rf", ""]
    head = ['<th{0}>{1}</th>'.format(
        ' class="{0}"'.format(cls) if cls else "", name)
        for cls, name in zip(classes, SCHEMA_COLUMNS)]
    body = []
    for _, line, line_no in block.rows:
        cells = split_cells(line)
        if len(cells) != len(SCHEMA_COLUMNS):
            fail(line_no, "@schema の行は「{0}」の {1} 列です。この行は {2} 列でした。".format(
                " | ".join(SCHEMA_COLUMNS), len(SCHEMA_COLUMNS), len(cells)))
        refs = check_key_cells(cells[5], cells[6], cells[7], line_no)
        if links is not None:
            for ref, least_one in refs:
                links.append((store, ref, cells[0].strip(), line_no, least_one))
        body.append(
            '<tr><td class="nm">{0}</td><td class="ty">{1}</td>'
            '<td class="ln">{2}</td><td class="ln">{3}</td><td class="un">{4}</td>'
            '<td class="ky">{5}</td><td class="ky">{6}</td><td class="rf">{7}</td>'
            '<td class="dsc">{8}</td></tr>'.format(*[inline(c) for c in cells]))
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
IO_DIRS = [("<->", "both", "↔"), ("->", "in", "→"), ("<-", "out", "←")]


IO_DIR_NAMES = {"in": ["受信"], "out": ["送信"], "both": ["受信", "送信"]}


def parse_io(block):
    """@io を「自分・相手ごとのやり取り・経路ごとの仕様」に分解する(純関数)。

    経路の ID(I-01…)は書かせず、やり取りの並び順と向きから機械が振る。原稿の 2 か所に
    同じ番号を書かせると、片方を直し忘れて食い違う(工程番号で実際に起きた)。
    """
    self_name, self_note, peers, paths = "", "", [], []
    pending = []      # 直前のやり取りに紐づく、まだ向きを割り当てていない @path
    cur = None        # 直前の @path(項目行の受け皿)

    def close_peer(line_no):
        """直前のやり取りに書かれた @path の数と、その向きの数が合うかを見る。"""
        if not peers:
            return
        peer, label, direction, at = peers[-1]
        want = IO_DIR_NAMES[direction]
        if not pending:
            fail(at, "「{0}」のやり取りに @path がありません。"
                     "受け渡す中身ごとに「@path {1}」を書いてください。".format(
                         peer, want[0]))
        got = [p["dir"] for p in pending]
        if got != want:
            fail(pending[0]["line_no"] if pending else at,
                 "「{0}」の @path は {1} の順で {2} 個書いてください。実際は {3}。".format(
                     peer, "→".join(want), len(want), "→".join(got) or "なし"))
        labels = [t.strip() for t in label.split("/")] if direction == "both" else [label]
        if len(labels) != len(want) or not all(labels):
            fail(at, "「{0}」は双方向なので、やり取りの中身を「受け取るもの / 渡すもの」の"
                     "形で 2 つ書いてください: {1}".format(peer, label[:40]))
        for path, dname, plabel in zip(pending, want, labels):
            path["peer"] = peer
            path["label"] = plabel
            path["id"] = "I-{0:02d}".format(len(paths) + 1)
            paths.append(path)
        del pending[:]

    for kind, line, line_no in block.rows:
        if kind == "@self":
            parts = split_cells(line)
            self_name, self_note = parts[0], (parts[1] if len(parts) > 1 else "")
            cur = None
            continue
        if kind == "@path":
            if not peers:
                fail(line_no, "@path は、どのやり取りの詳細かが分かるよう"
                              "「相手 -> …」の行の下に書いてください。")
            dname = line.strip()
            if dname not in ("受信", "送信"):
                fail(line_no, "@path の向きは「受信」か「送信」です。実際は「{0}」。".format(
                    dname[:20] or "(空)"))
            cur = {"dir": dname, "fields": {}, "line_no": line_no}
            pending.append(cur)
            continue

        for token, direction, _ in IO_DIRS:
            head, sep, tail = line.partition(" {0} ".format(token))
            if sep:
                peer, label = head.strip(), tail.strip()
                break
        else:
            # 向きの記号が無く「キー: 値」の形なら、直前の @path の項目とみなす。
            # ここを緩く判定すると、やり取りの行を書き損ねたときに
            # 「キーがない」という無関係なエラーになって原因が分からなくなる
            if cur is not None and ":" in line:
                key, value = split_kv(line, line_no)
                if key not in PATH_AUTHORED_ROWS:
                    fail(line_no, "経路に書ける項目は {0} です。「{1}」は使えません"
                                  "(相手と向きは上の行から入るので書きません)。".format(
                                      "・".join(PATH_AUTHORED_ROWS), key))
                cur["fields"].setdefault(key, []).append((value, line_no))
                continue
            fail(line_no, "外部とのやり取りは「相手 -> 受け取るもの」「相手 <- 渡すもの」"
                          "「相手 <-> 受け取るもの / 渡すもの」の形で書いてください: {0}".format(line[:40]))
        if not peer or not label:
            fail(line_no, "相手とやり取りの中身の両方を書いてください: {0}".format(line[:40]))
        close_peer(line_no)
        cur = None
        peers.append((peer, label, direction, line_no))
    close_peer(block.line_no)

    if not self_name:
        fail(block.line_no, "@io には @self でこのシステム自身を書いてください。")
    if not peers:
        fail(block.line_no, "@io には外部の相手を 1 つ以上書いてください。")

    for path in paths:
        for key in PATH_AUTHORED_ROWS:
            if key not in path["fields"]:
                fail(path["line_no"],
                     "{0}({1})に「{2}:」がありません。該当が無い場合も「なし」と書いて"
                     "項目を残します。".format(path["id"], path["label"], key))
    # 図のラベルには ID を前置する。図と 5.2 のどちらを見ても同じ記号で辿れる
    labelled = []
    seen = 0
    for peer, label, direction, at in peers:
        ids = [p["id"] for p in paths[seen:seen + len(IO_DIR_NAMES[direction])]]
        seen += len(ids)
        chunks = [t.strip() for t in label.split("/")] if direction == "both" else [label]
        text = " ／ ".join("{0} {1}".format(i, c) for i, c in zip(ids, chunks))
        labelled.append((peer, text, direction))
    return {"self_name": self_name, "self_note": self_note,
            "peers": labelled, "paths": paths}


def render_io(block, number, data=None):
    """5.1 の境界図。自分を中央に置き、外部の相手を左右に振り分けて辺で結ぶ。

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

    def room_for(label):
        rows = len(wrap_text(label, gap - 46.0, 10.0))
        return rows * 12.0 + (14.0 if len(label) > DG_ONLINE else 10.0)

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
            parts.append(edge_svg(x1, node.cy, x2, node.cy, direction, side, label, gap))
    return fig("外部との境界", number, svg_frame("".join(parts), "外部との境界"))


def edge_svg(x1, y1, x2, y2, direction, side, label, gap):
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


def render_graph(block, number, listed=()):
    """4.2 の関係図。中心は決め打ちにせず、交差が少ない配置を探す。"""
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
    if not edges:
        fail(block.line_no, "@graph には関係が 1 本も書かれていません。")

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
    return graph_svg(hub_name, hub_note, names, edges, "要素間の関係", number)


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
    for index, name in enumerate(order):
        w, h = sizes.get(name, (130.0, 33.0)) if sizes else (130.0, 33.0)
        cx, cy = cells[index]
        boxes.append((cx - w / 2 - 6, cy - h / 2 - 6, cx + w / 2 + 6, cy + h / 2 + 6))
    for one in mids:
        label = (one[0] - 86.0, one[1] - 15.0, one[0] + 86.0, one[1] + 15.0)
        for box in boxes:
            if (label[0] < box[2] and box[0] < label[2]
                    and label[1] < box[3] and box[1] < label[3]):
                on_box += 1
    length = sum(((s[0] - s[2]) ** 2 + (s[1] - s[3]) ** 2) ** 0.5 for s in segs)
    return crossings * 100000.0 + crowded * 4000.0 + on_box * 9000.0 + length


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
              least=None):
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
    col_w = max(n.w for n in nodes.values()) + 196.0
    row_h = max(n.h for n in nodes.values()) + 104.0
    cells = [((i % cols) * col_w + col_w / 2, (i // cols) * row_h + row_h / 2)
             for i in range(cols * rows)]

    pairs = list(bundle.keys())
    sizes = dict((name, (nodes[name].w, nodes[name].h)) for name in names)
    order, _ = arrange_nodes(names, pairs, cells, sizes)
    for index, name in enumerate(order):
        cx, cy = cells[index]
        nodes[name].x = cx - nodes[name].w / 2
        nodes[name].y = cy - nodes[name].h / 2

    parts = [nodes[name].render() for name in order]
    for (a, b), info in bundle.items():
        both = info["forward"] and info["backward"]
        parts.append(link_svg(nodes[a], nodes[b], " / ".join(info["labels"]),
                              both, info["backward"] and not info["forward"],
                              notation, bool(least and least.get((a, b)))))
    return fig(title, number, svg_frame("".join(parts), title))


def er_svg(stores, links, number):
    """データの入れ物どうしの関係を、外部キーの記載から組み立てる。

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


def link_svg(a, b, label, both, reverse, notation="arrow", least_one=False):
    """2 つの箱を最短の直線で結ぶ。端の記号は記法で変える。"""
    x1, y1 = touch_point(a, b.cx, b.cy)
    x2, y2 = touch_point(b, a.cx, a.cy)
    if reverse:
        x1, y1, x2, y2 = x2, y2, x1, y1
        a, b = b, a
    lines = wrap_text(label, 168.0, 10.0)
    mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
    top = mid_y - (len(lines) - 1) * 12.0 / 2 - 3.0
    # 行ごとに切る。全行を包む矩形で切ると、短い行の周りに線が残らず不自然になる
    segs = [(x1, y1, x2, y2)]
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
    if notation == "ie":
        # 参照する側が「多」、参照される側が「1」
        out += ie_many(x1, y1, x1 - x2, y1 - y2, least_one)
        out += ie_one(x2, y2, x2 - x1, y2 - y1)
    else:
        out += arrow_tip(x2, y2, x2 - x1, y2 - y1)
        if both:
            out += arrow_tip(x1, y1, x1 - x2, y1 - y2)
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
# 3.3 処理の流れ — 木に読み、座標を決め、SVG を描く
# ============================================================
FL_W = 250.0            # 工程の箱の幅
FL_VGAP = 26.0          # 縦の間隔(矢印が入る)
FL_LABEL = 110.0        # 辺のラベルを折り返す幅
FL_HGAP = FL_LABEL * 2 + 40.0   # 本線と分岐の横の間隔。ラベルが箱に届かない幅にする
FL_PADX = 10.0
FL_PADY = 7.0
FL_FONT = 12.0
FL_LINE = 16.0
FL_HEAD = 19.0          # 工程の箱の上部。左に番号、右に機能 ID を置く帯
FL_LOOP = 16.0          # 繰り返しの枠が中身からはみ出す幅
FL_BAR = 9.0            # 並行・順不同のバーの高さ
FL_LANE = 22.0          # 経路と経路の間隔

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
        self.lines = []
        self.w = 0.0
        self.h = 0.0
        self.x = 0.0
        self.y = 0.0

    @property
    def has_head(self):
        """番号か機能 ID を持つ工程は、上部に帯を敷く。"""
        return bool(self.sid or self.ids)

    def measure(self):
        if self.kind in ("並行", "順不同"):
            highest = 0.0
            for lane in self.lanes:
                total = 0.0
                for node in lane:
                    node.measure()
                    total += node.h + FL_VGAP
                highest = max(highest, total - FL_VGAP)
            self.w = FL_W * len(self.lanes) + FL_LANE * (len(self.lanes) - 1)
            self.h = highest + (FL_BAR + FL_VGAP) * 2
            return
        if self.kind == "例外":
            for child in self.children:
                child.measure()
            self.w = self.h = 0.0
            return
        if self.kind == "繰り返し":
            for child in self.children:
                child.measure()
            inner = sum(c.h for c in self.children) + FL_VGAP * (len(self.children) - 1)
            self.lines = wrap_text(self.label, FL_W, 10.0)
            self.w = FL_W + FL_LOOP * 2
            self.h = inner + FL_LOOP * 2 + len(self.lines) * 12.0 + 4.0
            return
        self.lines = wrap_text(self.text, FL_W - FL_PADX * 2, FL_FONT)
        self.w = FL_W
        self.h = len(self.lines) * FL_LINE + FL_PADY * 2
        if self.has_head:
            self.h += FL_HEAD
        if self.kind == "判断":
            self.h += 10.0
        for child in self.children:
            child.measure()


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
        if kind == "例外" and in_branch:
            fail(line_no, "分岐の中に例外は書けません。")
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
            if in_branch and ids:
                fail(line_no, "分岐の中の工程に機能 ID は添えられません(番号を持たないため)。")

        if kind == "例外" and "|" not in rest:
            fail(line_no, "例外には対応するエラー ID を添えます(例: 例外 通信に失敗した | E-05)。")

        node = FlowNode(kind, text, ids, line_no)
        if kind in ("並行", "順不同"):
            if in_branch:
                fail(line_no, "分岐の中に {0} は書けません。".format(kind))
            target().append(node)
            stack.append((node, None))
            continue
        if kind == "例外":
            if in_branch:
                fail(line_no, "分岐の中に例外は書けません。")
            host = target()[-1] if target() else None
            if host is None or host.kind not in ("工程", "待機"):
                fail(line_no, "例外 は工程か待機の直後に書きます。離脱元がありません。")
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
        if in_branch:
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


def layout_flow(items, left, top=0.0):
    """本線を縦に積む。ブロック(繰り返し・並行)の中も同じ手順で配置する。

    戻り値は (下端, 右端)。入れ子があるので再帰する。
    """
    y, right = top, left + FL_W
    for node in items:
        node.measure()

        if node.kind == "繰り返し":
            node.x, node.y = left - FL_LOOP, y
            head = len(node.lines) * 12.0 + 4.0
            inner_top = y + FL_LOOP + (0.0 if node.post else head)
            _, inner_right = layout_flow(node.children, left, inner_top)
            right = max(right, node.x + node.w, inner_right)
            y += node.h + FL_VGAP
            continue

        if node.kind in ("並行", "順不同"):
            node.x, node.y = left - (node.w - FL_W) / 2, y
            lane_x = node.x
            for lane in node.lanes:
                _, lane_right = layout_flow(lane, lane_x, y + FL_BAR + FL_VGAP)
                right = max(right, lane_right)
                lane_x += FL_W + FL_LANE
            right = max(right, node.x + node.w)
            y += node.h + FL_VGAP
            continue

        node.x, node.y = left, y
        side = node.children if node.kind == "判断" else []
        if node.exception:
            node.exception.measure()
            side = side + node.exception.children
        if side:
            branch_x = left + FL_W + FL_HGAP
            _, side_right = layout_flow(side, branch_x, y + node.h + FL_VGAP)
            right = max(right, side_right)
            y = max(y + node.h + FL_VGAP,
                    max(c.y + c.h for c in side) + FL_VGAP)
        else:
            y += node.h + FL_VGAP
    return y - FL_VGAP, right


def flow_bars(node):
    """並行・順不同。上下のバーで挟み、レーンを横に並べる。"""
    cls = "fl-bar" if node.kind == "並行" else "fl-bar loose"
    top, bottom = node.y, node.y + node.h - FL_BAR
    out = ('<rect class="{0}" x="{1:.1f}" y="{2:.1f}" width="{3:.1f}" height="{4:.1f}"/>'
           '<rect class="{0}" x="{1:.1f}" y="{5:.1f}" width="{3:.1f}" height="{4:.1f}"/>'
           '<text class="fl-barm" x="{6:.1f}" y="{7:.1f}">{8}</text>').format(
               cls, node.x, top, node.w, FL_BAR, bottom,
               node.x + 4, top - 4,
               esc("同時に行う" if node.kind == "並行" else "順序は問わない"))
    for lane in node.lanes:
        for inner in lane:
            out += flow_box(inner)
            out += flow_exception(inner)
        for a, b in zip(lane, lane[1:]):
            out += flow_arrow(a.x + a.w / 2, a.y + a.h, b.x + b.w / 2, b.y)
        head, tail = lane[0], lane[-1]
        out += flow_arrow(head.x + head.w / 2, top + FL_BAR,
                          head.x + head.w / 2, head.y)
        out += flow_line(tail.x + tail.w / 2, tail.y + tail.h,
                         tail.x + tail.w / 2, bottom)
    return out


def flow_loop(node):
    """繰り返し。中身を破線の枠で囲み、条件を前judge/後judgeの位置に置く。"""
    out = ('<rect class="fl-lp" x="{0:.1f}" y="{1:.1f}" width="{2:.1f}" '
           'height="{3:.1f}"/>').format(node.x, node.y, node.w, node.h)
    head = len(node.lines) * 12.0 + 4.0
    base = (node.y + node.h - FL_LOOP - head + 10.0 if node.post
            else node.y + FL_LOOP + 10.0)
    mark = "繰り返す条件" if node.post else "次の条件のあいだ繰り返す"
    for index, line_text in enumerate(node.lines):
        out += ('<text class="fl-lpt" x="{0:.1f}" y="{1:.1f}" text-anchor="middle" '
                'textLength="{2:.1f}" lengthAdjust="spacingAndGlyphs">{3}</text>').format(
                    node.x + node.w / 2, base + index * 12.0,
                    text_width(line_text, 10.0), esc(line_text))
    out += ('<text class="fl-lpm" x="{0:.1f}" y="{1:.1f}">{2}</text>').format(
        node.x + 6, node.y + (node.h - 5.0 if node.post else 11.0), esc(mark))
    for child in node.children:
        out += flow_box(child)
        out += flow_exception(child)
    for a, b in zip(node.children, node.children[1:]):
        out += flow_arrow(a.x + a.w / 2, a.y + a.h, b.x + b.w / 2, b.y)
    return out


def flow_head(node):
    """工程の上部の帯。左に番号(色を変えて仕切る)、右に機能 ID。"""
    no_w = 34.0
    out = ('<rect class="fl-hd" x="{0:.1f}" y="{1:.1f}" width="{2:.1f}" height="{3:.1f}"/>'
           '<rect class="fl-hn" x="{0:.1f}" y="{1:.1f}" width="{4:.1f}" height="{3:.1f}"/>'
           '<line class="fl-hs" x1="{5:.1f}" y1="{1:.1f}" x2="{5:.1f}" y2="{6:.1f}"/>'
           '<line class="fl-hs" x1="{0:.1f}" y1="{6:.1f}" x2="{7:.1f}" y2="{6:.1f}"/>').format(
               node.x, node.y, node.w, FL_HEAD, no_w,
               node.x + no_w, node.y + FL_HEAD, node.x + node.w)
    if node.sid:
        out += ('<text class="fl-no" x="{0:.1f}" y="{1:.1f}" text-anchor="middle" '
                'textLength="{2:.1f}" lengthAdjust="spacingAndGlyphs">{3}</text>').format(
                    node.x + no_w / 2, node.y + 14, text_width(node.sid[-2:], 10.0),
                    esc(node.sid[-2:]))
    if node.ids:
        label = " ".join(node.ids)
        out += ('<a href="#{0}"><text class="fl-id" x="{1:.1f}" y="{2:.1f}" '
                'text-anchor="end" textLength="{3:.1f}" lengthAdjust="spacingAndGlyphs">'
                '{4}</text></a>').format(
                    anchor_of(node.ids[0]), node.x + node.w - 8, node.y + 14,
                    text_width(label, 10.0), esc(label))
    return out


def flow_box(node):
    """1 要素の描画。工程だけ番号を持つ。"""
    if node.kind in ("並行", "順不同"):
        return flow_bars(node)
    if node.kind == "繰り返し":
        return flow_loop(node)

    if node.kind == "待機":
        out = ('<rect class="fl-wt" x="{0:.1f}" y="{1:.1f}" width="{2:.1f}" '
               'height="{3:.1f}"/>').format(node.x, node.y, node.w, node.h)
        out += '<text class="fl-wm" x="{0:.1f}" y="{1:.1f}">待機</text>'.format(
            node.x + 8, node.y + 14)
    elif node.kind == "判断":
        cx, cy = node.x + node.w / 2, node.y + node.h / 2
        pts = "{0:.1f},{1:.1f} {2:.1f},{3:.1f} {0:.1f},{4:.1f} {5:.1f},{3:.1f}".format(
            cx, node.y, node.x + node.w, cy, node.y + node.h, node.x)
        out = '<polygon class="fl-d" points="{0}"/>'.format(pts)
    else:
        cls = {"端子": "fl-t", "終了": "fl-t stop"}.get(node.kind, "fl-n")
        out = ('<rect class="{0}" x="{1:.1f}" y="{2:.1f}" width="{3:.1f}" height="{4:.1f}"'
               ' rx="{5}"{6}/>').format(
                   cls, node.x, node.y, node.w, node.h,
                   int(node.h / 2) if node.kind in ("端子", "終了") else 0,
                   ' id="{0}"'.format(node.sid) if node.sid else "")

    head_h = FL_HEAD if node.has_head else 0.0
    if head_h:
        out += flow_head(node)
    body = node.h - head_h
    top = node.y + head_h + (body - len(node.lines) * FL_LINE) / 2 + FL_FONT - 1
    for index, line_text in enumerate(node.lines):
        out += ('<text class="fl-x" x="{0:.1f}" y="{1:.1f}" text-anchor="middle" '
                'textLength="{2:.1f}" lengthAdjust="spacingAndGlyphs">{3}</text>').format(
                    node.x + node.w / 2, top + index * FL_LINE,
                    text_width(line_text, FL_FONT), esc(line_text))
    return out


def flow_pieces(x1, y1, x2, y2, label, dashed):
    """線分の並びとラベルの文字を、別々に組み立てて返す。

    矢印を線と文字の「あいだ」に置くため、ここでは連結しない。
    """
    segs, text = [(x1, y1, x2, y2)], ""
    if label:
        rows = wrap_text(label, FL_LABEL, 10.0)
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        first = my - (len(rows) - 1) * 12.0 / 2
        for index, row in enumerate(rows):
            wide = text_width(row, 10.0)
            base = first + index * 12.0
            box = (mx - wide / 2 - 4, base - 9.0, mx + wide / 2 + 4, base + 3.0)
            segs = [piece for seg in segs
                    for piece in clip_segment(seg[0], seg[1], seg[2], seg[3], box)]
            text += ('<text class="fl-l" x="{0:.1f}" y="{1:.1f}" text-anchor="middle" '
                     'textLength="{2:.1f}" lengthAdjust="spacingAndGlyphs">{3}</text>').format(
                         mx, base, wide, esc(row))
    cls = "fl-e dash" if dashed else "fl-e"
    drawn = "".join('<line class="{4}" x1="{0:.1f}" y1="{1:.1f}" x2="{2:.1f}" '
                    'y2="{3:.1f}"/>'.format(seg[0], seg[1], seg[2], seg[3], cls)
                    for seg in segs)
    return drawn, text


def flow_exception(node):
    """工程から離脱する例外。破線で抜け、離脱後の流れを縦に繋ぐ。"""
    exc = node.exception
    if not exc:
        return ""
    head = exc.children[0]
    label = exc.text + ("（{0}）".format(" ".join(exc.eids)) if exc.eids else "")
    out = flow_elbow(node.x + node.w, node.y + node.h / 2,
                     head.x, head.y + head.h / 2, label, dashed=True)
    for child in exc.children:
        out += flow_box(child)
        out += flow_exception(child)
    for a, b in zip(exc.children, exc.children[1:]):
        out += flow_arrow(a.x + a.w / 2, a.y + a.h, b.x + b.w / 2, b.y)
    return out


def flow_line(x1, y1, x2, y2, label="", dashed=False):
    """線だけ。ラベルがあればその分を切って、文字を貫かないようにする。"""
    drawn, text = flow_pieces(x1, y1, x2, y2, label, dashed)
    return drawn + text


def flow_arrow(x1, y1, x2, y2, label="", dashed=False):
    """線と、終点の矢印。矢印は線の直後・文字の前に置く。"""
    drawn, text = flow_pieces(x1, y1, x2, y2, label, dashed)
    if abs(x1 - x2) < 0.1:
        drawn += arrow_up_svg(x2, y2, 1)
    else:
        drawn += arrow_svg(x2, y2, 1 if x2 > x1 else -1)
    return drawn + text


def flow_elbow(x1, y1, x2, y2, label="", dashed=False):
    """横→縦→横 の折れ線。斜めに引かないので矢印の向きが必ず線と一致する。"""
    mid = (x1 + x2) / 2
    out = flow_line(x1, y1, mid, y1, label, dashed)
    out += flow_line(mid, y1, mid, y2, "", dashed)
    out += flow_arrow(mid, y2, x2, y2, "", dashed)
    return out


def render_flow(block, number, pid):
    """1 つの目的に対する流れを描く。工程番号は目的ごとに 01 から振る。"""
    items = parse_flow_rows(block.rows, block.line_no)

    def main_line(nodes):
        """本線の工程を順に返す。繰り返しの中は本線、分岐の中は番号を持たない。"""
        for node in nodes:
            if node.kind == "繰り返し":
                for inner in main_line(node.children):
                    yield inner
            elif node.kind in ("並行", "順不同"):
                for lane in node.lanes:
                    for inner in main_line(lane):
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

    height, _ = layout_flow(items, 0.0)
    parts = []
    for index, node in enumerate(items):
        parts.append(flow_box(node))
        # 繰り返し・並行の中身は flow_box が描く。ここで描くのは分岐の枝だけ
        if node.kind == "判断":
            for child in node.children:
                parts.append(flow_box(child))
        parts.append(flow_exception(node))
        if node.children and node.kind == "判断":
            # 本線から分岐へ、そして分岐の中を縦に繋ぐ
            first = node.children[0]
            parts.append(flow_elbow(node.x + node.w, node.y + node.h / 2,
                                    first.x, first.y + first.h / 2, node.label))
            for a, b in zip(node.children, node.children[1:]):
                parts.append(flow_arrow(a.x + a.w / 2, a.y + a.h,
                                        b.x + b.w / 2, b.y))
        if index + 1 < len(items):
            nxt = items[index + 1]
            parts.append(flow_arrow(node.x + node.w / 2, node.y + node.h,
                                    nxt.x + nxt.w / 2, nxt.y,
                                    node.else_label if node.children else ""))

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

    seen_errors = set(used_errors(items))
    return (fig("処理の流れ", number, svg_frame("".join(parts), "処理の流れ")),
            steps, seen_errors)


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


def render_spec(spec):
    head = ('<div class="dc-blk-h" id="{0}"><span class="id">{1}</span>'
            '<span class="nm">{2}</span>{3}</div>').format(
        anchor_of(spec["id"]), spec["id"], esc(spec["name"]), mark(spec["state"]))
    rows = []
    for key in SPEC_ROWS:
        values = spec["fields"].get(key, [("なし", 0)])
        rows.append("<dt>{0}</dt><dd>{1}</dd>".format(esc(key), field_cell(key, values)))
    return '<div class="dc-blk">{0}<dl class="dc-rows">{1}</dl></div>'.format(head, "".join(rows))


def render_paths(data, line_no):
    """5.2 経路ごとの仕様。第 8 章の機能仕様と同じブロック形式で並べる。

    相手と向きは @io のやり取り行から入る(原稿に二度書かせない)。
    """
    if not data:
        fail(line_no, "@paths を書くには、同じ章に @io が必要です。")
    blocks = []
    for path in data["paths"]:
        head = ('<div class="dc-blk-h" id="{0}"><span class="id">{1}</span>'
                '<span class="nm">{2}</span><span class="dc-mk">{3}</span></div>').format(
            anchor_of(path["id"]), path["id"], esc(path["label"]), path["dir"])
        rows = []
        for key in PATH_ROWS:
            if key == "相手":
                cell = esc(path["peer"])
            else:
                cell = field_cell(key, path["fields"].get(key, [("なし", 0)]))
            rows.append("<dt>{0}</dt><dd>{1}</dd>".format(esc(key), cell))
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
                 "{0} の「流れ」列が空です。3.3 のどの流れで起きるかを書くか、"
                 "処理の流れが変わらないなら「{1}」と書いてください。".format(
                     row["id"], FLOW_FIXED))
        if row["id"] not in seen:
            fail(row["line_no"],
                 "{0} は「{1}」と書かれていますが、3.3 のどの流れにも現れません。"
                 "工程に「例外 <条件> | {0}」を添えるか、「{2}」と書いてください。".format(
                     row["id"], note, FLOW_FIXED))


def render_rows_fixed(no, rows):
    """ID 付き条項表。列は章が決め、検証つきの章は条件セルにテスト名を添える。"""
    columns, has_verify = ROW_COLUMNS[no]
    head = ["<th>{0}</th>".format(esc(c)) for c in columns]
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
        if has_verify:
            state, _ = parse_verify(row["verify"], row["line_no"])
            tds.append('<td class="jd">{0}</td>'.format(mark(state)))
        body.append("<tr>" + "".join(tds) + "</tr>")
    return tag_table(head, body)


# ============================================================
# 自動生成する章
# ============================================================
def step_label(sid):
    """st-P01-03 を「P-01:03」と読める形にする。"""
    m = re.match(r"^st-P(\d{2})-(\d{2})$", sid)
    return "P-{0}:{1}".format(m.group(1), m.group(2)) if m else sid


def build_feature_list(specs, steps):
    """第 7 章は処理の流れと機能仕様から組む。原稿には書かせない。"""
    # 列順は ID を左端に置き、識別子・数値の列(工程・E2E・判定)を右へ寄せる。
    # 工程は E2E の直前に置く(どの工程が何件の試験で押さえられているかを並べて読む)
    head = ["<th>ID</th>", "<th>機能</th>", "<th>概要</th>", '<th class="st">工程</th>',
            '<th class="num">E2E</th>', '<th class="jd">判定</th>']
    body = []
    for spec in specs:
        info = steps[spec["id"]]
        links = "・".join('<a href="#{0}">{1}</a>'.format(sid, step_label(sid))
                          for sid in info["sids"])
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
    for no in (9, 10):
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
        fail(io_at, "@io に経路を書いたら、5.2 経路ごとの仕様に @paths も書いてください"
                    "(書かないと {0} 本の経路が出力に出ません)。".format(len(io_data["paths"])))
    # 2.2 で定義された目的を先に集める(3.3 の流れが参照する)
    purposes = []
    stores, links, current_store = [], [], ""
    errors_in_flow = set()
    for ch in chapters:
        for holder in [ch] + ch.subs:
            for block in holder.blocks:
                if block.kind == "purpose":
                    pid, _ = parse_purpose(block)
                    if pid in purposes:
                        fail(block.line_no, "目的 {0} が 2 回定義されています。".format(pid))
                    purposes.append(pid)
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
            for block in holder.blocks:
                if block.kind == "p":
                    parts.append('<p class="dc-p">{0}</p>'.format(inline(block.head)))
                elif block.kind == "store":
                    current_store = split_cells(block.head)[0].strip()
                    stores.append(current_store)
                    parts.append(render_store(block))
                elif block.kind == "schema":
                    parts.append(render_schema(block, current_store, links))
                elif block.kind == "purpose":
                    parts.append(render_purpose(block))
                elif block.kind == "def":
                    parts.append(render_def(block))
                elif block.kind == "table":
                    parts.append(render_table(block))
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
                    parts.append(render_graph(block, "{0}-{1}".format(ch.no, fig_no[ch.no]),
                                              parts_listed))
                elif block.kind == "flow":
                    fig_no[ch.no] = fig_no.get(ch.no, 0) + 1
                    pid = block.head.strip()
                    if not PID_RE.match(pid):
                        fail(block.line_no,
                             "流れは「@flow P-01」の形で目的を指してください: {0}".format(pid[:30]))
                    if pid not in purposes:
                        fail(block.line_no,
                             "{0} という目的が 3.3 にありません。@purpose で先に定義してください。".format(pid))
                    flow_html, flow_steps, flow_errors = render_flow(
                        block, "{0}-{1}".format(ch.no, fig_no[ch.no]), pid)
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
        if ch.no == 6 and stores:
            for src, dst, column, line_no, _ in links:
                if dst not in stores:
                    fail(line_no, "FK の参照先「{0}」が見つかりません。"
                                  "@store で定義した入れ物の名前を書きます。".format(dst))
            fig_no[6] = fig_no.get(6, 0) + 1
            parts.insert(0, er_svg(stores, links, "6-{0}".format(fig_no[6])))
        if ch.no == 9:
            check_error_flow(rows[9], errors_in_flow)
        if ch.no in ROW_COLUMNS:
            parts.append(render_rows_fixed(ch.no, rows[ch.no]))
        rendered[ch.no] = "\n".join(parts)

    # 目的と流れの対応。定義したのに流れが無い目的を残さない
    for pid in purposes:
        if pid not in flows_seen:
            raise SpecError(
                "目的 {0} に対応する流れが 3.3 にありません。「@flow {0}」を書いてください。".format(pid))
    if not purposes:
        raise SpecError("3.3 に目的(@purpose)が 1 つもありません。")

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

    sections = [cover(meta, generated, specs, rows, tally, tests),
                section(0, "目次", "章の構成", build_toc())]
    for no, name, _, authored in CHAPTERS:
        if no == 7:
            html_body = build_feature_list(specs, steps)
        elif no == 13:
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

    stats = {"機能": len(specs), "エラー": len(rows[9]), "非機能要件": len(rows[10]),
             "E2E": len(tests), "確定": tally["確定"], "暫定": tally["暫定"], "未固定": tally["未固定"]}
    return page, stats


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

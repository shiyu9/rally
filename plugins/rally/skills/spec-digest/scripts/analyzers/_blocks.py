"""波かっこで関数を区切る言語(PowerShell・Bash)に共通の小道具。"""
import re


def strip_comment(line):
    """行末の # コメントを落とす(引用符の中の # は残す。おおよその判定)。"""
    quote = ""
    for index, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (index == 0 or line[index - 1] in " \t;"):
            return line[:index]
    return line


def function_ranges(text, head_re):
    """{名前: (開始行, 終了行)}。head_re は関数の先頭行に当たり、group(1) が名前。"""
    lines = text.splitlines()
    out = {}
    index = 0
    while index < len(lines):
        m = head_re.match(lines[index])
        if not m:
            index += 1
            continue
        depth, seen, end = 0, False, index
        for at in range(index, len(lines)):
            body = strip_comment(lines[at])
            depth += body.count("{") - body.count("}")
            seen = seen or "{" in body
            if seen and depth <= 0:
                end = at
                break
        else:
            end = len(lines) - 1
        out.setdefault(m.group(1), (index + 1, end + 1))
        index = end + 1
    return out


def chain_at(ranges, line):
    for name, (start, end) in ranges.items():
        if start <= line <= end:
            return (name,)
    return ()


def leading_comment(text, skip=("#!",)):
    for line in text.splitlines():
        line = line.strip()
        if not line or any(line.startswith(s) for s in skip):
            continue
        if line.startswith("#"):
            found = line.lstrip("#").strip()
            if found:
                return found
            continue
        break
    return ""


def scan(text, ranges, rules):
    """rules: [(種類, 正規表現, 内容を作る関数)]。コメントを除いた各行に当てる。"""
    out = []
    for no, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw)
        for kind, pattern, what in rules:
            for m in pattern.finditer(line):
                found = what(m)
                item = (kind, no, chain_at(ranges, no), found)
                if found and item not in out:      # 同じ行の同じ内容は 1 回だけ
                    out.append(item)
    return out

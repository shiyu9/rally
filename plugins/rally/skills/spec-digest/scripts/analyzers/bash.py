"""Bash(sh)の解析部品。正規表現で読むので、明示的に書かれた形だけを拾う。"""
import re

from _blocks import function_ranges, leading_comment, scan, strip_comment

NAME = "Bash"
EXTENSIONS = (".sh", ".bash")

_START = r"(?:^|[;&|({]|\bthen\b|\bdo\b|\belse\b)\s*"
_WRITE = {"rm": "削除", "rmdir": "削除", "mv": "置き換え", "cp": "複写", "mkdir": "作成", "touch": "作成",
          "ln": "作成", "tee": "書き込み", "dd": "書き込み", "truncate": "書き込み"}
_BUILTIN = {"PWD", "OLDPWD", "IFS", "BASH_SOURCE", "LINENO", "RANDOM", "SECONDS", "FUNCNAME", "PIPESTATUS",
            "OPTARG", "OPTIND", "REPLY", "UID", "EUID", "BASH_VERSION", "BASHPID", "PPID", "COLUMNS", "LINES"}


def _head(line):
    m = re.match(r"^\s*function\s+([A-Za-z_][\w-]*)", line) or re.match(r"^\s*([A-Za-z_][\w-]*)\s*\(\s*\)", line)
    return m


class _Head(object):
    """function name { … } と name() { … } の両方を、名前を group(1) で返す形にそろえる。"""

    @staticmethod
    def match(line):
        return _head(line)


def locate(text, func):
    return function_ranges(text, _Head).get(func)


def doc(text):
    return leading_comment(text)


def references(text):
    out = []
    for m in re.finditer(r"(?m)^\s*(?:source|\.)\s+[\"']?([^\s\"';]+)", text):
        out.append(m.group(1).split("/")[-1])
    return out


def facts(text):
    ranges = function_ranges(text, _Head)
    assigned = set(re.findall(r"(?m)(?:^|[\s;])(?:export\s+|local\s+|readonly\s+|declare\s+(?:-\w+\s+)?)?([A-Z][A-Z0-9_]*)=", text))
    assigned |= set(re.findall(r"(?m)\bfor\s+([A-Z][A-Z0-9_]*)\s+in\b", text))
    assigned |= set(re.findall(r"(?m)\bread\s+(?:-\w+\s+)*([A-Z][A-Z0-9_]*)", text))
    seen = set()

    def env(m):
        name = m.group(1)
        if name in assigned or name in _BUILTIN or name in seen:
            return ""
        seen.add(name)
        return name

    rules = [
        ("env", re.compile(r"\$\{?([A-Z][A-Z0-9_]*)"), env),
        ("effect", re.compile(_START + r"(" + "|".join(_WRITE) + r")\s"),
         lambda m: "{0}({1})".format(_WRITE[m.group(1)], m.group(1))),
        ("effect", re.compile(r"(?<![0-9&>])>{1,2}\s*(?!&)(?!/dev/null)([^\s;&|)]+)"),
         lambda m: "書き込み(> {0})".format(m.group(1))),
        ("stop", re.compile(_START + r"exit\s+(?!0\b)([^\s;})]+)"), lambda m: "exit " + m.group(1)),
        ("stop", re.compile(_START + r"return\s+(?!0\b)([^\s;})]+)"), lambda m: "return " + m.group(1)),
        ("broad", re.compile(_START + r"trap\s"), lambda m: "trap"),
        ("broad", re.compile(r"\bset\s+\+e\b"), lambda m: "set +e"),
        ("broad", re.compile(r"\|\|\s*(?:true|:)(?:\s|$|;)"), lambda m: "|| true"),
    ]
    return scan(text, ranges, rules)

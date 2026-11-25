"""PowerShell の解析部品。正規表現で読むので、明示的に書かれた形だけを拾う。"""
import re

from _blocks import function_ranges, leading_comment, scan

NAME = "PowerShell"
EXTENSIONS = (".ps1", ".psm1")

_HEAD = re.compile(r"^\s*function\s+([\w-]+)", re.IGNORECASE)
_WRITE = {"remove-item": "削除", "set-content": "書き込み", "add-content": "書き込み", "out-file": "書き込み",
          "clear-content": "書き込み", "new-item": "作成", "copy-item": "複写", "move-item": "置き換え",
          "rename-item": "置き換え"}
_RULES = [
    ("env", re.compile(r"\$env:(\w+)(?!\w*\s*=[^=])", re.IGNORECASE), lambda m: m.group(1)),
    ("effect", re.compile(r"(?<![\w-])(" + "|".join(_WRITE) + r")(?![\w-])", re.IGNORECASE),
     lambda m: "{0}({1})".format(_WRITE[m.group(1).lower()], m.group(1))),
    ("stop", re.compile(r"(?<![\w-])throw(?![\w-])", re.IGNORECASE), lambda m: "throw"),
    ("stop", re.compile(r"(?<![\w-])exit\s+(?!0\b)(\S+)", re.IGNORECASE), lambda m: "exit " + m.group(1)),
    ("broad", re.compile(r"(?<![\w-])catch\s*\{", re.IGNORECASE), lambda m: "catch(型の指定なし)"),
    ("broad", re.compile(r"(?<![\w-])trap\s*\{", re.IGNORECASE), lambda m: "trap"),
    ("broad", re.compile(r"-ErrorAction\s+SilentlyContinue", re.IGNORECASE), lambda m: "-ErrorAction SilentlyContinue"),
]


def locate(text, func):
    for name, span in function_ranges(text, _HEAD).items():
        if name.lower() == func.lower():
            return span
    return None


def doc(text):
    return leading_comment(text)


def references(text):
    """ドット ソースと Import-Module の相手。ほかのファイル名の言及は、呼び出し側が別に拾う。"""
    out = []
    for m in re.finditer(r"(?m)^\s*\.\s+[\"']?([^\s\"']+)", text):
        out.append(m.group(1).replace("\\", "/").split("/")[-1])
    for m in re.finditer(r"(?im)Import-Module\s+[\"']?([^\s\"']+)", text):
        out.append(m.group(1).replace("\\", "/").split("/")[-1])
    return out


def facts(text):
    return scan(text, function_ranges(text, _HEAD), _RULES)

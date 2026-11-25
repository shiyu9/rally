"""Python の解析部品。標準ライブラリの ast で読む。"""
import ast

NAME = "Python"
EXTENSIONS = (".py",)

_EFFECTS = {
    "unlink": "削除", "remove": "削除", "rmdir": "削除", "rmtree": "削除",
    "replace": "置き換え", "rename": "置き換え",
    "copy": "複写", "copy2": "複写", "copyfile": "複写", "copytree": "複写",
    "mkdir": "作成", "makedirs": "作成", "mkstemp": "作成", "mkdtemp": "作成",
    "write_bytes": "書き込み", "write_text": "書き込み",
}
_OWNERS = {"os", "shutil", "tempfile"}
_PATH_ONLY = {"unlink", "rmdir", "mkdir", "write_bytes", "write_text"}
_BROAD = {"Exception", "BaseException", "KeyboardInterrupt"}


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _dotted(node.value) + "." + node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return "?"


def locate(text, func):
    want = func.split(".")[-1]
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == want:
            return min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno
    return None


def doc(text):
    found = ast.get_docstring(ast.parse(text)) or ""
    return found.splitlines()[0] if found else ""


def references(text):
    """import している名前(モジュール名の末尾)。同じフォルダのファイルに当たるものだけが線になる。"""
    out = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                out.append(node.module.split(".")[-1])
            out += [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            out += [a.name.split(".")[-1] for a in node.names]
    return out


def facts(text):
    tree = ast.parse(text)
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value
    out = []

    def visit(node, chain):
        for child in ast.iter_child_nodes(node):
            here = chain
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                here = chain + (child.name,)
            if isinstance(child, ast.Raise) and child.exc is not None:
                out.append(("stop", child.lineno, chain, _dotted(child.exc)))
            if isinstance(child, ast.Call):
                full = _dotted(child.func)
                owner, _, attr = full.rpartition(".")
                if full in ("os.environ.get", "os.getenv"):
                    arg = child.args[0] if child.args else None
                    key = arg.value if isinstance(arg, ast.Constant) else consts.get(getattr(arg, "id", None), "?")
                    out.append(("env", child.lineno, chain, str(key)))
                elif attr in _EFFECTS and (owner in _OWNERS or (owner and attr in _PATH_ONLY)):
                    out.append(("effect", child.lineno, chain, "{0}({1})".format(_EFFECTS[attr], full)))
                elif full == "open" and len(child.args) > 1 and isinstance(child.args[1], ast.Constant) \
                        and any(c in str(child.args[1].value) for c in "wax+"):
                    out.append(("effect", child.lineno, chain, "書き込み(open {0})".format(child.args[1].value)))
            if isinstance(child, ast.Subscript) and _dotted(child.value) == "os.environ" \
                    and isinstance(child.slice, ast.Constant):
                out.append(("env", child.lineno, chain, str(child.slice.value)))
            if isinstance(child, ast.ExceptHandler) and child.type is not None:
                types = child.type.elts if isinstance(child.type, ast.Tuple) else [child.type]
                for one in types:
                    if _dotted(one) in _BROAD:
                        out.append(("broad", child.lineno, chain, "except " + _dotted(one)))
            visit(child, here)

    visit(tree, ())
    return out

#!/usr/bin/env python3
"""rally_test_audit: 試験の「実在」と「検出力の下限」を静的に検査する。

spec-interview の実装フェーズ末尾(標準試験バッテリー展開後)と、rally-test の verify から
呼ぶ。対象は標準フローの成果物のみ(docs/interviews/・tests/・実装コード)。
docs/spec.html には依存しない(spec はオプションである)。

検査項目:
  1. missing_tests   : docs/interviews/ の最新記録に書かれたテスト名が tests/ 配下に実在するか
                       (言語非依存。名前が tests/ 内のどのファイルにも文字列として現れなければ不在)
  2. assertless      : assert も例外期待も持たない test 関数(Python は AST で判定。
                       JS/TS は expect/assert を含まない it/test ブロックを正規表現で判定)
  3. unexplained_skip: 理由の無い skip(Python: pytest.mark.skip/skipif に reason 無し、
                       pytest.skip() 引数無し。JS/TS: it.skip / test.skip / xit)
  4. swallowed       : 例外の握り潰し(Python: except 節の本体が pass/... だけ、bare except。
                       JS/TS: 本体が空の catch)

使い方:
  python rally_test_audit.py [--root <project>] [--json]
終了コード: 0 = 指摘なし / 1 = 指摘あり / 2 = 入力不備(記録や tests/ が無い)
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys

TEST_NAME_RE = re.compile(r"`(test[A-Za-z0-9_\-\.぀-ヿ一-鿿]*)`")
PY_TEST_FILE_RE = re.compile(r"(^test_.*\.py$)|(_test\.py$)")
JS_TEST_FILE_RE = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build"}


# ---------- 共通 ----------

def iter_files(root: pathlib.Path, pattern: re.Pattern | None = None):
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and (pattern is None or pattern.search(p.name)):
            yield p


def read(p: pathlib.Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def latest_interview(docs: pathlib.Path) -> pathlib.Path | None:
    d = docs / "interviews"
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.md"))
    return files[-1] if files else None


# ---------- 1. 記録のテスト名の実在 ----------

def check_missing_tests(record: pathlib.Path, tests_dir: pathlib.Path) -> list[dict]:
    names = sorted(set(TEST_NAME_RE.findall(read(record))))
    if not names:
        return []
    corpus = "\n".join(read(p) for p in iter_files(tests_dir))
    return [
        {"name": n, "record": str(record)}
        for n in names
        if n not in corpus
    ]


# ---------- 2〜4 Python (AST) ----------

ASSERT_CALL_NAMES = {"raises", "warns", "assert_called", "assert_called_once", "assert_called_with",
                     "assert_any_call", "assert_not_called", "assertEqual", "assertTrue", "assertFalse",
                     "assertIn", "assertRaises", "assertIsNone", "assertIsNotNone", "fail"}


def _has_assertion(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in ASSERT_CALL_NAMES or name.startswith("assert"):
                return True
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                c = item.context_expr
                if isinstance(c, ast.Call):
                    f = c.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    if name in ("raises", "warns"):
                        return True
    return False


def _is_xfail_or_skip(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for d in fn.decorator_list:
        src = ast.unparse(d)
        if "xfail" in src or "skip" in src:
            return True
    return False


def _decorator_skip_without_reason(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for d in fn.decorator_list:
        if not isinstance(d, ast.Call):
            src = ast.unparse(d)
            if src.endswith("mark.skip") or src.endswith("mark.skipif"):
                return True
            continue
        src = ast.unparse(d.func)
        if src.endswith("mark.skip") or src.endswith("mark.skipif"):
            kw = {k.arg for k in d.keywords}
            if "reason" not in kw:
                # skip("msg") の位置引数は理由とみなす。skipif(cond) は条件のみで理由なし
                if src.endswith("mark.skip") and d.args:
                    continue
                return True
    return False


def audit_python_file(p: pathlib.Path, is_test: bool) -> dict:
    out = {"assertless": [], "unexplained_skip": [], "swallowed": []}
    try:
        tree = ast.parse(read(p))
    except SyntaxError as e:
        out["swallowed"].append({"file": str(p), "line": e.lineno or 0, "detail": f"構文エラー: {e.msg}"})
        return out
    for node in ast.walk(tree):
        if is_test and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            if _decorator_skip_without_reason(node):
                out["unexplained_skip"].append({"file": str(p), "line": node.lineno, "name": node.name})
            if not _is_xfail_or_skip(node) and not _has_assertion(node):
                out["assertless"].append({"file": str(p), "line": node.lineno, "name": node.name})
        if is_test and isinstance(node, ast.Call):
            src = ast.unparse(node.func)
            if src.endswith("pytest.skip") and not node.args and not node.keywords:
                out["unexplained_skip"].append({"file": str(p), "line": node.lineno, "name": "pytest.skip()"})
        if isinstance(node, ast.ExceptHandler):
            body_only_pass = all(
                isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is ...)
                for s in node.body
            )
            if node.type is None:
                out["swallowed"].append({"file": str(p), "line": node.lineno, "detail": "bare except"})
            elif body_only_pass:
                out["swallowed"].append({"file": str(p), "line": node.lineno, "detail": "except 本体が pass のみ"})
    return out


# ---------- 2〜4 JS/TS (正規表現の簡易版) ----------

JS_BLOCK_RE = re.compile(r"\b(it|test)(\.skip|\.only)?\s*\(\s*(['\"`])(.+?)\3", re.S)
JS_SKIP_RE = re.compile(r"\b(it\.skip|test\.skip|xit|xtest|describe\.skip)\s*\(\s*(['\"`])(.+?)\2")
JS_EMPTY_CATCH_RE = re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*\}")


def audit_js_file(p: pathlib.Path, is_test: bool) -> dict:
    out = {"assertless": [], "unexplained_skip": [], "swallowed": []}
    src = read(p)
    if is_test:
        for m in JS_SKIP_RE.finditer(src):
            out["unexplained_skip"].append({"file": str(p), "line": src.count("\n", 0, m.start()) + 1, "name": m.group(3)})
        # ブロック単位の assert 有無: 次の it/test の開始位置までを本体とみなす(簡易)
        matches = list(JS_BLOCK_RE.finditer(src))
        for i, m in enumerate(matches):
            if m.group(2) == ".skip":
                continue
            end = matches[i + 1].start() if i + 1 < len(matches) else len(src)
            body = src[m.end():end]
            if not re.search(r"\b(expect|assert|should)\b", body):
                out["assertless"].append({"file": str(p), "line": src.count("\n", 0, m.start()) + 1, "name": m.group(4)})
    for m in JS_EMPTY_CATCH_RE.finditer(src):
        out["swallowed"].append({"file": str(p), "line": src.count("\n", 0, m.start()) + 1, "detail": "空の catch"})
    return out


# ---------- まとめ ----------

def run(root: pathlib.Path) -> dict:
    tests_dir = root / "tests"
    docs = root / "docs"
    result = {"root": str(root), "missing_tests": [], "assertless": [], "unexplained_skip": [],
              "swallowed": [], "record": None, "errors": []}
    rec = latest_interview(docs)
    if rec is None:
        result["errors"].append("docs/interviews/ に記録が無い")
    else:
        result["record"] = str(rec)
    if not tests_dir.is_dir():
        result["errors"].append("tests/ が無い")
        return result
    if rec is not None:
        result["missing_tests"] = check_missing_tests(rec, tests_dir)

    for p in iter_files(root):
        rel = p.relative_to(root)
        in_tests = rel.parts and rel.parts[0] == "tests"
        if p.suffix == ".py":
            is_test = bool(in_tests and PY_TEST_FILE_RE.search(p.name))
            r = audit_python_file(p, is_test)
        elif re.search(r"\.[cm]?[jt]sx?$", p.name):
            is_test = bool(JS_TEST_FILE_RE.search(p.name) or in_tests)
            r = audit_js_file(p, is_test)
        else:
            continue
        for k in ("assertless", "unexplained_skip", "swallowed"):
            result[k].extend(r[k])
    return result


def summarize(res: dict) -> str:
    lines = [f"rally_test_audit: {res['root']}"]
    if res["record"]:
        lines.append(f"  記録: {res['record']}")
    for e in res["errors"]:
        lines.append(f"  [入力不備] {e}")
    lines.append(f"  1. 記録にあるが tests/ に無いテスト名: {len(res['missing_tests'])} 件")
    for m in res["missing_tests"]:
        lines.append(f"     - {m['name']}")
    lines.append(f"  2. assert の無い test 関数: {len(res['assertless'])} 件")
    for a in res["assertless"]:
        lines.append(f"     - {a['file']}:{a['line']} {a['name']}")
    lines.append(f"  3. 理由の無い skip: {len(res['unexplained_skip'])} 件")
    for s in res["unexplained_skip"]:
        lines.append(f"     - {s['file']}:{s['line']} {s['name']}")
    lines.append(f"  4. 例外の握り潰し: {len(res['swallowed'])} 件")
    for s in res["swallowed"]:
        lines.append(f"     - {s['file']}:{s['line']} {s['detail']}")
    return "\n".join(lines)


def has_findings(res: dict) -> bool:
    return any(res[k] for k in ("missing_tests", "assertless", "unexplained_skip", "swallowed"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="プロジェクトルート(docs/ と tests/ を含む)")
    ap.add_argument("--json", action="store_true", help="JSON で出力")
    args = ap.parse_args(argv)
    root = pathlib.Path(args.root).resolve()
    res = run(root)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(summarize(res))
    if res["errors"]:
        return 2
    return 1 if has_findings(res) else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())

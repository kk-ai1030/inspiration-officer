"""Static scan of the user's code (no execution): long / complex functions, duplicated blocks, churn hotspots and
element-wise loops that numpy / pandas could vectorise. Produces "optimization" findings (kind = workspace).

Languages: Python (ast, exact), Jupyter notebooks (code cells), Fortran, C / C++ / CUDA / Java / JS / Go / Rust / C#
(brace matching), Julia / MATLAB (function … end), R (<- function {…}). Duplication is language-agnostic.
Churn: `git log --numstat` when the folder is a git repository, otherwise the library's own changes.jsonl.

Writes inspiration/library/codescan.json (full local report). Only the findings go to observed.jsonl.
"""
from __future__ import annotations

import ast
import hashlib
import math
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .common import Env, clamp, finding, notebook_code, numbered, read_text
from .library import recent_changes

try:  # v1.5.6: git must not pop a console window when this runs under pythonw
    from _spawn import run as _run_hidden
except ImportError:  # pragma: no cover - platform/ is normally on sys.path
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _spawn import run as _run_hidden

FORTRAN = {".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08"}
BRACE = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cu", ".cuh", ".java", ".js", ".ts", ".go", ".rs", ".cs"}
ENDLANG = {".jl", ".m"}
COMMENT_PREFIX = ("#", "//", "!", "%", "--", ";", "*", "/*", "c ", "C ")
CTRL = {"if", "for", "while", "switch", "catch", "return", "sizeof", "else", "do", "case", "elif", "foreach", "using",
        "new", "delete", "throw", "defined"}
PERF_CALLS = {"append": "np.append", "concatenate": "np.concatenate", "vstack": "np.vstack", "hstack": "np.hstack",
              "concat": "pd.concat"}


# ----------------------------------------------------------------------------- python
class _PyFn(ast.NodeVisitor):
    DECISION = (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.IfExp, ast.ExceptHandler, ast.comprehension,
                ast.AsyncFor, ast.AsyncWith, ast.Assert)

    def __init__(self):
        self.fns: list[dict] = []
        self.stack: list[str] = []

    def _fn(self, node):
        cx, depth = 1, 0

        def walk(n, d):
            nonlocal cx, depth
            for ch in ast.iter_child_nodes(n):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                nd = d
                if isinstance(ch, self.DECISION):
                    cx += 1
                if isinstance(ch, ast.BoolOp):
                    cx += len(ch.values) - 1
                if isinstance(ch, (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.AsyncFor, ast.AsyncWith)):
                    nd = d + 1
                    depth = max(depth, nd)
                walk(ch, nd)

        walk(node, 0)
        end = getattr(node, "end_lineno", node.lineno)
        a = node.args
        nargs = len(a.args) + len(a.kwonlyargs) + (1 if a.vararg else 0) + (1 if a.kwarg else 0)
        name = ".".join(self.stack + [node.name])
        self.fns.append({"name": name, "start": node.lineno, "end": end, "length": end - node.lineno + 1,
                         "complexity": cx, "nesting": depth, "args": nargs})
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _fn

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()


def _call_name(c: ast.Call) -> str:
    f = c.func
    if isinstance(f, ast.Attribute):
        base = f.value.id if isinstance(f.value, ast.Name) else ""
        return f"{base}.{f.attr}" if base else f".{f.attr}"
    return f.id if isinstance(f, ast.Name) else ""


def python_perf(tree: ast.AST, text: str) -> list[dict]:
    """Element-wise loops and quadratic concatenation in numeric Python code."""
    numeric = bool(re.search(r"^\s*(import|from)\s+(numpy|pandas|scipy|torch|jax)\b", text, flags=re.M))
    if not numeric:
        return []
    out = []

    def loop_depth(n, d=0):
        best = d
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, (ast.For, ast.While)):
                best = max(best, loop_depth(ch, d + 1))
            elif not isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                best = max(best, loop_depth(ch, d))
        return best

    seen_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            nm = _call_name(node)
            if nm.endswith((".iterrows", ".itertuples")):
                out.append({"line": node.lineno, "end": getattr(node, "end_lineno", node.lineno), "kind": "iterrows",
                            "what": f"逐行遍历 DataFrame（{nm.split('.')[-1]}）", "depth": 1})
            if nm.endswith(".apply") and any(k.arg == "axis" and isinstance(k.value, ast.Constant) and k.value.value == 1
                                             for k in node.keywords):
                out.append({"line": node.lineno, "end": getattr(node, "end_lineno", node.lineno), "kind": "apply_axis1",
                            "what": "DataFrame.apply(axis=1) 按行调用 Python 函数", "depth": 1})
        if not isinstance(node, ast.For) or node.lineno in seen_lines:
            continue
        it = node.iter
        is_range = isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range"
        depth = loop_depth(node, 1)
        body_nodes = [n for b in node.body for n in ast.walk(b)]
        sub_assign = any(isinstance(n, (ast.Assign, ast.AugAssign)) and any(isinstance(t, ast.Subscript) for t in
                         (n.targets if isinstance(n, ast.Assign) else [n.target])) for n in body_nodes)
        concat = [c for c in body_nodes if isinstance(c, ast.Call) and _call_name(c).split(".")[-1] in PERF_CALLS
                  and _call_name(c).split(".")[0] in ("np", "numpy", "pd", "pandas", "torch", "")]
        if concat:
            nm = _call_name(concat[0])
            out.append({"line": node.lineno, "end": getattr(node, "end_lineno", node.lineno), "kind": "concat_in_loop",
                        "what": f"循环里反复调用 {nm.lstrip('.')}（每次复制整个数组，总代价约 O(n²)）", "depth": depth})
        elif is_range and sub_assign and depth >= 1:
            out.append({"line": node.lineno, "end": getattr(node, "end_lineno", node.lineno), "kind": "index_loop",
                        "what": f"{depth} 层 for-range 循环逐元素写数组（numpy 广播 / 向量化通常快 10–100 倍量级，需实测）",
                        "depth": depth})
        else:
            continue
        for n in ast.walk(node):  # report the outermost loop only
            if isinstance(n, ast.For):
                seen_lines.add(n.lineno)
    return out


IO_CALLS = ("open", "np.load", "np.loadtxt", "np.genfromtxt", "numpy.load", "json.load", "pickle.load", "h5py.File",
            "torch.load", "pd.read_", "pandas.read_", "xr.open_dataset", "Image.open", "cv2.imread")
ABS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|/home/|/Users/|/mnt/|/data/|/scratch/|\\\\)")


def python_io_loops(tree: ast.AST) -> list[dict]:
    """v1.5.1: reading / parsing a file inside a loop (any Python file, numeric or not)."""
    out, seen = [], set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.While)) or node.lineno in seen:
            continue
        calls = [c for b in node.body for c in ast.walk(b) if isinstance(c, ast.Call)]
        hit = next((c for c in calls if any(_call_name(c) == k or (k.endswith("_") and _call_name(c).startswith(k))
                                             for k in IO_CALLS)), None)
        if hit is None:
            continue
        nm = _call_name(hit) or "open"
        out.append({"line": node.lineno, "end": getattr(node, "end_lineno", node.lineno), "kind": "io_in_loop",
                    "what": f"循环里反复读文件（{nm}，每一轮都重新打开 / 解析；能否先读一次、或批量读）", "depth": 1})
        for n in ast.walk(node):
            if isinstance(n, (ast.For, ast.While)):
                seen.add(n.lineno)
    return out


def python_accum(tree: ast.AST) -> list[dict]:
    """v1.5.1: `for i in range(len(x)): s += x[i]`-style Python-level accumulation (numeric files only)."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        it = node.iter
        if not (isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range"):
            continue
        if any(isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and isinstance(n.op, (ast.Add, ast.Mult))
               and any(isinstance(s, ast.Subscript) for s in ast.walk(n.value)) for b in node.body for n in ast.walk(b)):
            out.append({"line": node.lineno, "end": getattr(node, "end_lineno", node.lineno), "kind": "py_accum",
                        "what": "for-range 循环里逐元素累加（np.sum / np.dot / cumsum 通常可以一行替代，需实测）", "depth": 1})
    return out


def python_smells(tree: ast.AST, fns: list[dict], max_args: int) -> list[dict]:
    """v1.5.1: small, certain code-quality items: bare / swallowing except, too many parameters, hard-coded absolute paths."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            swallow = len(node.body) == 1 and isinstance(node.body[0], (ast.Pass, ast.Continue))
            broad = node.type is None or (isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"))
            if node.type is None or (broad and swallow):
                out.append({"line": node.lineno, "kind": "bare_except",
                            "what": "except: 吞掉所有错误" if node.type is None else "except Exception: pass 静默吞错"})
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and ABS_PATH.match(node.value) and len(node.value) < 260:
            out.append({"line": node.lineno, "kind": "hardcoded_path", "what": f"写死的绝对路径 {node.value[:60]!r}"})
    for f in fns:
        if f.get("args") is not None and f["args"] >= max_args:
            out.append({"line": f["start"], "kind": "many_args", "what": f"函数 {f['name']} 有 {f['args']} 个参数"})
    return out


# ----------------------------------------------------------------------------- other languages
def _strip_code(line: str, ext: str) -> str:
    s = re.sub(r'"(\\.|[^"\\])*"', '""', line)
    s = re.sub(r"'(\\.|[^'\\])*'", "''", s) if ext not in FORTRAN | ENDLANG else s
    if ext in FORTRAN:
        s = s.split("!", 1)[0]
    elif ext == ".m":
        s = s.split("%", 1)[0]
    elif ext in (".jl", ".r"):
        s = s.split("#", 1)[0]
    else:
        s = s.split("//", 1)[0]
    return s


def fortran_fns(lines: list[str]) -> list[dict]:
    start_re = re.compile(r"^\s*(?:(?:pure|elemental|recursive|impure|module)\s+)*(?:(?:integer|real|double\s+precision|"
                          r"logical|complex|character)[^!]*?\s+)?(subroutine|function)\s+(\w+)", re.I)
    end_re = re.compile(r"^\s*end\s*(subroutine|function)\b", re.I)
    dec_re = re.compile(r"\bif\b.*\bthen\b|\bdo\b|\bselect\s+case\b|\belse\s*if\b|\.and\.|\.or\.|\bcase\b|\bwhere\b", re.I)
    open_re = re.compile(r"^\s*(?:\w+\s*:\s*)?(do\b|if\b.*\bthen\s*$|select\s+case)", re.I)
    close_re = re.compile(r"^\s*end\s*(do|if|select)\b", re.I)
    fns, cur = [], None
    for i, raw in enumerate(lines, 1):
        ln = _strip_code(raw, ".f90")
        if cur is None:
            m = start_re.match(ln)
            if m and not end_re.match(ln):
                cur = {"name": m.group(2), "start": i, "cx": 1, "depth": 0, "maxd": 0}
            continue
        if end_re.match(ln):
            fns.append({"name": cur["name"], "start": cur["start"], "end": i, "length": i - cur["start"] + 1,
                        "complexity": cur["cx"], "nesting": cur["maxd"], "args": None})
            cur = None
            continue
        if not re.match(r"^\s*end\b", ln, re.I):  # `end do` / `end if` close a block, they are not decisions
            cur["cx"] += len(dec_re.findall(ln))
        if open_re.match(ln):
            cur["depth"] += 1
            cur["maxd"] = max(cur["maxd"], cur["depth"])
        elif close_re.match(ln):
            cur["depth"] = max(0, cur["depth"] - 1)
    return fns


def brace_fns(lines: list[str], ext: str) -> list[dict]:
    sig = re.compile(r"^[^;{}=]*?\b([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const|noexcept|override|final|throws\s+[\w.,\s]+|"
                     r"->\s*[\w:<>&*\s]+)?\s*\{?\s*$")
    rsig = re.compile(r"^\s*([A-Za-z_.][\w.]*)\s*(?:<-|=)\s*function\s*\(")
    dec = re.compile(r"\b(if|for|while|case|catch|foreach)\b|&&|\|\||\?")
    fns, depth, cur, pending = [], 0, None, None
    for i, raw in enumerate(lines, 1):
        ln = _strip_code(raw, ext)
        if cur is None:
            m = rsig.match(ln) if ext == ".r" else sig.match(ln)
            if m and m.group(1) not in CTRL and depth <= 1 and not ln.lstrip().startswith(("#", "else", "}")):
                pending = (m.group(1), i, depth)
        opens, closes = ln.count("{"), ln.count("}")
        if cur is None and pending and opens:
            cur = {"name": pending[0], "start": pending[1], "base": pending[2], "cx": 1, "maxd": 0}
            pending = None
        elif cur is None and pending and i - pending[1] > 3:
            pending = None
        if cur is not None:
            cur["cx"] += len(dec.findall(ln))
            d = depth
            for ch in ln:  # deepest point inside the line: `if (…) { … }` on one line still counts as one level
                if ch == "{":
                    d += 1
                    cur["maxd"] = max(cur["maxd"], d - cur["base"] - 1)
                elif ch == "}":
                    d -= 1
        depth += opens - closes
        if cur is not None:
            if depth <= cur["base"] and (i > cur["start"] or closes):
                fns.append({"name": cur["name"], "start": cur["start"], "end": i, "length": i - cur["start"] + 1,
                            "complexity": cur["cx"], "nesting": max(0, cur["maxd"]), "args": None})
                cur = None
        depth = max(depth, 0)
    return fns


def end_fns(lines: list[str], ext: str) -> list[dict]:
    """Julia / MATLAB: function … end with nested block keywords (approximate; `end` inside indexing is removed)."""
    blk = (r"\b(function|if|for|while|begin|let|try|struct|module|macro|do|quote|switch|parfor)\b" if ext == ".jl"
           else r"\b(function|if|for|while|switch|try|parfor)\b")
    open_re, end_re = re.compile(blk), re.compile(r"\bend\b")
    start_re = re.compile(r"^\s*function\s+(?:\[?[\w,\s]*\]?\s*=\s*)?([\w.!]+)")
    dec = re.compile(r"\b(if|elseif|for|while|case|catch|parfor)\b|&&|\|\|")
    fns, cur, depth = [], None, 0
    for i, raw in enumerate(lines, 1):
        code = _strip_code(raw, ext)
        ln = re.sub(r"\([^()]*\)|\[[^\[\]]*\]", "()", code)
        if cur is None:
            m = start_re.match(code)
            if not m:
                continue
            cur, depth = {"name": m.group(1), "start": i, "cx": 1, "maxd": 0}, 0
        cur["cx"] += len(dec.findall(ln))
        depth += len(open_re.findall(ln)) - len(end_re.findall(ln))
        cur["maxd"] = max(cur["maxd"], depth - 1)
        if depth <= 0 and (i > cur["start"] or end_re.search(ln)):
            fns.append({"name": cur["name"], "start": cur["start"], "end": i, "length": i - cur["start"] + 1,
                        "complexity": cur["cx"], "nesting": max(0, cur["maxd"]), "args": None})
            cur = None
    return fns


# ----------------------------------------------------------------------------- duplication
def _norm_line(s: str) -> str:
    t = s.strip()
    if not t or t.startswith(COMMENT_PREFIX) or len(t) < 4:
        return ""
    if t in ("end", "end do", "end if", "else:", "else", "{", "}", "};", "return", "pass", "break", "continue"):
        return ""
    return re.sub(r"\s+", " ", t)


def duplicates(files: dict[str, list[str]], window: int, min_lines: int) -> tuple[list[dict], dict[str, float]]:
    norm: dict[str, list[tuple[int, str]]] = {}
    for ref, lines in files.items():
        norm[ref] = [(i + 1, n) for i, raw in enumerate(lines) if (n := _norm_line(raw))]
    index: dict[str, list[tuple[str, int]]] = {}
    for ref, nl in norm.items():
        for k in range(len(nl) - window + 1):
            h = hashlib.md5("\n".join(x[1] for x in nl[k:k + window]).encode("utf-8")).hexdigest()
            index.setdefault(h, []).append((ref, k))
    covered: dict[str, set[int]] = {ref: set() for ref in norm}
    blocks, seen = [], set()
    for h, occ in index.items():
        if len(occ) < 2 or len(occ) > 25:  # >25 copies is boilerplate (license headers, generated code)
            continue
        for ref, k in occ:
            covered[ref].update(range(k, k + window))
        (ra, ka), (rb, kb) = occ[0], occ[1]
        if (ra, ka, rb, kb) in seen or (ra == rb and abs(ka - kb) < window):
            continue
        la, lb = norm[ra], norm[rb]
        n = window
        while ka + n < len(la) and kb + n < len(lb) and la[ka + n][1] == lb[kb + n][1] and (ra != rb or ka + n < kb):
            n += 1
        for j in range(n - window + 1):
            seen.add((ra, ka + j, rb, kb + j))
        a0, a1, b0, b1 = la[ka][0], la[ka + n - 1][0], lb[kb][0], lb[kb + n - 1][0]
        if a1 - a0 + 1 >= min_lines:
            blocks.append({"a": ra, "a_start": a0, "a_end": a1, "b": rb, "b_start": b0, "b_end": b1, "norm_lines": n,
                           "copies": len(occ)})
    rate = {ref: round(len(c) / len(norm[ref]), 3) for ref, c in covered.items() if norm[ref]}
    blocks.sort(key=lambda b: -b["norm_lines"])
    return blocks, rate


# ----------------------------------------------------------------------------- churn
def git_churn(env: Env, days: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    git = shutil.which("git")
    if not git:
        return out
    for r in env.roots():
        if not r["exists"]:
            continue
        try:
            top = _run_hidden([git, "-C", str(r["path"]), "rev-parse", "--show-toplevel"], capture_output=True,
                              text=True, timeout=15).stdout.strip()
            if not top:
                continue
            log = _run_hidden([git, "-C", top, "log", f"--since={days}.days", "--numstat", "--no-renames",
                               "--format=@%ct", "--", str(r["path"])], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        topp = Path(top).resolve()
        for ln in log.splitlines():
            parts = ln.split("\t")
            if len(parts) != 3:
                continue
            add, dele, path = parts
            try:
                rel = (topp / path).resolve().relative_to(r["path"]).as_posix()
            except ValueError:
                continue
            ref = f"library://{r['label']}/{rel}"
            c = out.setdefault(ref, {"commits": 0, "lines": 0, "via": "git"})
            c["commits"] += 1
            c["lines"] += (int(add) if add.isdigit() else 0) + (int(dele) if dele.isdigit() else 0)
    return out


def fs_churn(env: Env, days: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in recent_changes(env, days):
        if c.get("change") == "modified":
            e = out.setdefault(c["ref"], {"commits": 0, "lines": 0, "via": "scan"})
            e["commits"] += 1
            e["lines"] += abs(c.get("lines_delta") or 0)
    return out


# ----------------------------------------------------------------------------- driver
def _snippet(lines: list[str], start: int, end: int, n: int, allow: bool) -> str:
    if not allow:
        return "（config.json → observe.upload_code_snippets = false：不上传代码原文，只给指标）"
    seg = lines[start - 1:min(end, start - 1 + n)]
    more = f"\n…（共 {end - start + 1} 行，只摘前 {len(seg)} 行）" if end - start + 1 > len(seg) else ""
    return numbered(seg, start) + more


def scan(env: Env, manifest: dict) -> tuple[list[dict], dict]:
    c = env.cfg["code"]
    allow = bool(env.cfg.get("upload_code_snippets", True))
    nsnip = int(env.cfg.get("snippet_lines", 40))
    max_text = int(float(env.cfg.get("max_text_mb", 2)) * 1024 * 1024)
    lim_len, lim_cx, lim_nest = int(c["long_function_lines"]), int(c["complexity"]), int(c["nesting"])
    texts: dict[str, list[str]] = {}
    report: dict = {"files": {}, "functions": [], "duplicates": [], "perf": [], "smells": [], "churn_via": None}
    max_args = int(c.get("max_args", 7))
    findings: list[dict] = []
    fs_root = {r["label"]: r["path"] for r in env.roots()}
    for f in manifest.get("files", []):
        if f["cls"] != "code" or env.over_budget():
            continue
        p = fs_root[f["root"]] / f["rel"]
        txt = read_text(p, max_text)
        if txt is None:
            continue
        if f["ext"] == ".ipynb":
            txt = notebook_code(txt)
        lines = txt.splitlines()
        texts[f["ref"]] = lines
        fns, perf, lang = [], [], f["ext"].lstrip(".")
        if f["ext"] in (".py", ".pyx", ".ipynb"):
            try:
                tree = ast.parse(txt)
                v = _PyFn()
                v.visit(tree)
                fns, perf, lang = v.fns, python_perf(tree, txt), "python"
                perf += python_io_loops(tree)
                if re.search(r"^\s*(import|from)\s+(numpy|pandas|scipy|torch|jax)\b", txt, flags=re.M):
                    known = {x["line"] for x in perf}
                    perf += [x for x in python_accum(tree) if x["line"] not in known]
                for x in python_smells(tree, fns, max_args):
                    report["smells"].append({"ref": f["ref"], **x})
            except (SyntaxError, ValueError, RecursionError):
                lang = "python(语法解析失败)"
        elif f["ext"] in FORTRAN:
            fns, lang = fortran_fns(lines), "fortran"
        elif f["ext"] in BRACE or f["ext"] == ".r":
            fns = brace_fns(lines, f["ext"])
        elif f["ext"] in ENDLANG:
            fns = end_fns(lines, f["ext"])
        report["files"][f["ref"]] = {"lang": lang, "lines": len(lines), "functions": len(fns),
                                     "complexity_sum": sum(x["complexity"] for x in fns)}
        for x in fns:
            report["functions"].append({"ref": f["ref"], **x})
        for x in perf:
            report["perf"].append({"ref": f["ref"], **x})

    churn = git_churn(env, int(c["churn_days"]))
    report["churn_via"] = "git" if churn else "scan"
    if not churn:
        churn = fs_churn(env, int(c["churn_days"]))
    report["churn"] = churn

    # 1. long / complex functions: the worst few per file (v1.5.1: code.max_per_file, default 3; was 1)
    per_file = max(1, int(c.get("max_per_file", 3)))
    over: dict[str, list[tuple[float, dict]]] = {}
    for fn in report["functions"]:
        s = max(fn["length"] / lim_len, fn["complexity"] / lim_cx, fn["nesting"] / lim_nest)
        if s >= 1:
            over.setdefault(fn["ref"], []).append((s, fn))
    worst = {ref: sorted(v, key=lambda x: -x[0])[0] for ref, v in over.items()}
    picked_fns = [(ref, s, fn) for ref, v in over.items() for s, fn in sorted(v, key=lambda x: -x[0])[:per_file]]
    for ref, s, fn in picked_fns:
        ch = churn.get(ref, {})
        others = sum(1 for x in report["functions"] if x["ref"] == ref and x is not fn and
                     max(x["length"] / lim_len, x["complexity"] / lim_cx, x["nesting"] / lim_nest) >= 1)
        head = (f"代码扫描 · {ref}#L{fn['start']}-L{fn['end']} · 函数 {fn['name']}\n"
                f"长度 {fn['length']} 行 · 圈复杂度 {fn['complexity']} · 最大嵌套 {fn['nesting']}"
                + (f" · 参数 {fn['args']} 个" if fn.get("args") is not None else "")
                + (f"\n近 {c['churn_days']} 天改动 {ch['commits']} 次（{'git' if ch.get('via') == 'git' else '扫描对比'}）" if ch else "")
                + (f"\n同一文件共有 {others + 1} 个函数超过阈值" if others else "")
                + f"\n阈值：长度 {lim_len} 行 / 圈复杂度 {lim_cx} / 嵌套 {lim_nest}")
        ev = head + "\n\n" + _snippet(texts[ref], fn["start"], fn["end"], nsnip, allow)
        sev = 2.0 + 1.2 * math.log2(s) + (0.6 if ch.get("commits", 0) >= 3 else 0)  # s=1→2.0, 2→3.2, 4→4.4
        findings.append(finding(
            "code.long_function", "workspace", "optimization",
            f"{Path(ref).name} 的 {fn['name']} 过长/过复杂（{fn['length']} 行，复杂度 {fn['complexity']}）", ev, sev,
            fp=f"{ref}|{fn['name']}", ref=f"{ref}#L{fn['start']}-L{fn['end']}", subfield="code",
            summary="函数超过长度/复杂度/嵌套阈值。可考虑拆分为若干纯函数、把参数打包、加最小回归测试后再重构。",
            metrics={"length": fn["length"], "complexity": fn["complexity"], "nesting": fn["nesting"],
                     "churn_commits": ch.get("commits", 0), "score": round(s, 2)}))

    # 2. duplicated blocks (top 5 file pairs)
    blocks, rate = duplicates(texts, int(c["dup_window"]), int(c["dup_min_lines"]))
    report["duplicates"], report["dup_rate"] = blocks[:50], rate
    used_pairs = set()
    for b in blocks:
        pair = tuple(sorted((b["a"], b["b"])))
        if pair in used_pairs or len(used_pairs) >= 5:
            continue
        used_pairs.add(pair)
        span = b["a_end"] - b["a_start"] + 1
        ev = (f"重复代码 · {b['a']}#L{b['a_start']}-L{b['a_end']} 与 {b['b']}#L{b['b_start']}-L{b['b_end']}\n"
              f"连续相同 {b['norm_lines']} 行（去掉空行和注释后）· 同样片段共出现 {b['copies']} 处\n"
              f"文件重复率：{Path(b['a']).name} {rate.get(b['a'], 0):.0%} · {Path(b['b']).name} {rate.get(b['b'], 0):.0%}\n\n"
              f"【{Path(b['a']).name}】\n" + _snippet(texts[b["a"]], b["a_start"], b["a_end"], min(nsnip, 20), allow) +
              f"\n\n【{Path(b['b']).name}】\n" + _snippet(texts[b["b"]], b["b_start"], b["b_end"], 8, allow))
        sev = 2.0 + min(2.0, span / 40) + (0.5 if b["copies"] > 2 else 0)
        same = b["a"] == b["b"]
        findings.append(finding(
            "code.duplication", "workspace", "optimization",
            f"{'同一文件内' if same else Path(b['a']).name + ' 与 ' + Path(b['b']).name} 有 {b['norm_lines']} 行重复代码",
            ev, sev, fp=f"{pair[0]}|{pair[1]}", allowed=["optimization", "process"],
            ref=f"{b['a']}#L{b['a_start']}-L{b['a_end']}", subfield="code",
            summary="复制粘贴的代码块：改一处忘改另一处是常见 bug 来源。可抽成一个函数 / 模块，两处调用。",
            metrics={"norm_lines": b["norm_lines"], "copies": b["copies"], "span_lines": span}))

    # 3. churn hotspots: changed often AND complex
    for ref, ch in sorted(churn.items(), key=lambda kv: -kv[1]["commits"]):
        info = report["files"].get(ref)
        if not info or ch["commits"] < int(c["hotspot_min_commits"]) or info["complexity_sum"] < lim_cx:
            continue
        if ref in worst:  # already reported as a long function; churn is in its evidence
            continue
        ev = (f"改动热点 · {ref}\n近 {c['churn_days']} 天改动 {ch['commits']} 次、增删 {ch['lines']} 行"
              f"（{'git log' if ch['via'] == 'git' else '资料库扫描对比'}）\n"
              f"{info['lines']} 行 · {info['functions']} 个函数 · 复杂度合计 {info['complexity_sum']}")
        sev = 2.0 + min(2.0, ch["commits"] / 10) + min(1.0, info["complexity_sum"] / 100)
        findings.append(finding(
            "code.hotspot", "workspace", "optimization", f"{Path(ref).name} 频繁改动且复杂（{ch['commits']} 次）",
            ev, sev, fp=ref, allowed=["optimization", "process"], ref=ref, subfield="code",
            summary="经常改又复杂的文件是 bug 与返工的集中地。可先补测试，再把最常改的部分拆出来。",
            metrics={"commits": ch["commits"], "lines_changed": ch["lines"], "complexity_sum": info["complexity_sum"]}))
        if sum(1 for x in findings if x["detector"] == "code.hotspot") >= 3:
            break

    # 4. element-wise loops / quadratic concatenation (numeric Python)
    perf = sorted(report["perf"], key=lambda x: -x["depth"])[: int(c["max_perf_items"])]
    for x in perf:
        lines = texts[x["ref"]]
        ev = (f"性能嫌疑 · {x['ref']}#L{x['line']}-L{x['end']}\n{x['what']}\n循环嵌套 {x['depth']} 层\n\n"
              + _snippet(lines, x["line"], x["end"], min(nsnip, 25), allow))
        sev = 2.4 + 0.5 * min(x["depth"] - 1, 2) + (0.4 if x["kind"] in ("concat_in_loop", "iterrows") else 0)
        findings.append(finding(
            "code.perf_loop", "workspace", "optimization", f"{Path(x['ref']).name} 第 {x['line']} 行：{x['what'].split('（')[0]}",
            ev, sev, fp=f"{x['ref']}|{x['kind']}|{lines[x['line'] - 1].strip()[:80]}", ref=f"{x['ref']}#L{x['line']}-L{x['end']}",
            subfield="code", summary="数值代码里的 Python 级逐元素循环。最小验证：同一输入跑新旧两版，比对结果一致并计时。",
            metrics={"loop_depth": x["depth"], "pattern": x["kind"]}))
    # 5. v1.5.1 small certain items, one finding per file (bare except / many parameters / hard-coded paths)
    by_file: dict[str, list[dict]] = {}
    for x in report["smells"]:
        by_file.setdefault(x["ref"], []).append(x)
    ZH = {"bare_except": "吞错", "many_args": "参数过多", "hardcoded_path": "写死路径"}
    for ref, xs in sorted(by_file.items(), key=lambda kv: -len(kv[1]))[: int(c.get("max_smell_files", 5))]:
        xs = sorted(xs, key=lambda x: x["line"])
        lines = texts[ref]
        kinds = sorted({x["kind"] for x in xs})
        ev = (f"代码小问题 · {ref}\n" + "\n".join(f"L{x['line']}：{x['what']}" for x in xs[:12])
              + (f"\n……另有 {len(xs) - 12} 处" if len(xs) > 12 else "") + "\n\n"
              + "\n\n".join(_snippet(lines, x["line"], x["line"], 1, allow) for x in xs[:3]))
        sev = 2.2 + min(1.3, 0.25 * (len(xs) - 1)) + (0.3 if "bare_except" in kinds else 0)
        findings.append(finding(
            "code.smell", "workspace", "optimization",
            f"{Path(ref).name}：{'、'.join(ZH[k] for k in kinds)}（{len(xs)} 处）", ev, sev,
            fp=f"{ref}|{','.join(kinds)}", ref=f"{ref}#L{xs[0]['line']}", subfield="code",
            summary="确定性的小问题：吞错会藏住 bug，写死路径换机器就坏，参数过多难以调用。改动小、收益明确、能立刻做。",
            metrics={"items": len(xs), "kinds": kinds}))
    report["generated_at"] = datetime.now().isoformat(timespec="seconds")
    env.write_json("codescan.json", report)
    stats = {"code_files": len(texts), "functions": len(report["functions"]), "dup_blocks": len(blocks),
             "perf": len(report["perf"]), "smells": len(report["smells"]), "churn_via": report["churn_via"],
             "findings": len(findings)}
    return findings, stats

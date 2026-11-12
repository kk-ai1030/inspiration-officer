"""Logs and data files: "上次数据里有反常".

Logs  (cls = log): crashes (Traceback / Error), NaN / Inf, non-convergence, warnings, and numeric series in
      `key = value` lines whose values jump far away from their median (robust z-score with MAD).
Data  (csv / tsv / dat / txt in the data folder): per numeric column missing values and robust outliers.
Only files changed in the last `lookback_days` (or since the previous scan) are examined, so findings are about
recent work. Writes inspiration/library/anomalies.json.
"""
from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime
from pathlib import Path
from statistics import median

from .common import Env, finding, numbered, read_tail, read_text

LOG_PATTERNS = [
    # group, regex, candidate_type, allowed, base severity, per-file?
    ("traceback", re.compile(r"Traceback \(most recent call last\)|Segmentation fault|core dumped|MemoryError|"
                             r"\bKilled\b|out of memory|\bOOM\b|forrtl: severe|SIGSEGV|Abort(ed)? trap"),
     "optimization", ["optimization", "process"], 3.0),
    ("nan_inf", re.compile(r"(?<![A-Za-z])(nan|NaN|NAN|[-+]?inf|[-+]?Inf|Infinity)(?![A-Za-z])|\*{6,}"),
     "hypothesis", ["hypothesis", "optimization"], 3.3),
    ("convergence", re.compile(r"(?i)not\s+converg|did\s+not\s+converge|no\s+convergence|failed\s+to\s+converge|"
                               r"ConvergenceWarning|diverg|max(imum)?\s+iterations?\s+(reached|exceeded)|未收敛|不收敛|发散"),
     "hypothesis", ["hypothesis", "optimization"], 3.4),
    ("error", re.compile(r"\b(ERROR|Error|FATAL|Fatal|Exception)\b|错误|失败"), "optimization", ["optimization", "process"], 2.4),
    ("warning", re.compile(r"\bWARN(ING)?\b|\bWarning\b|警告"), "optimization", ["optimization", "process"], 1.6),
]
KV = re.compile(r"(?<![\w.])(?P<key>[A-Za-z_\u4e00-\u9fff][\w\-/\u4e00-\u9fff]{0,24})\s*[=:]\s*"
                r"(?P<val>[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)(?![\w.])")
GROUP_LABEL = {"traceback": "程序崩溃 / 报错栈", "nan_inf": "出现 NaN / Inf", "convergence": "不收敛 / 发散",
               "error": "报错", "warning": "警告"}


def _recent(env: Env, f: dict, prev_mtimes: dict, days: float) -> bool:
    horizon = datetime.now().timestamp() - days * 86400
    return f["mtime"] >= horizon or (f["ref"] in prev_mtimes and prev_mtimes[f["ref"]] != f["mtime"])


def _robust(vals: list[float]) -> tuple[float, float]:
    m = median(vals)
    mad = median([abs(v - m) for v in vals])
    return m, 1.4826 * mad


def _signature(line: str) -> str:
    s = re.sub(r"0x[0-9a-fA-F]+|\d+(\.\d+)?([eE][-+]?\d+)?", "#", line.strip())
    s = re.sub(r"(['\"]).*?\1", "'…'", s)
    return re.sub(r"\s+", " ", s)[:120]


def scan_logs(env: Env, manifest: dict, prev_mtimes: dict) -> tuple[list[dict], list[dict]]:
    cfg = env.cfg["logs"]
    days = float(env.cfg.get("lookback_days", 30))
    roots = {r["label"]: r["path"] for r in env.roots()}
    findings, report = [], []
    crash_groups: dict[str, dict] = {}
    logs = [f for f in manifest.get("files", []) if f["cls"] == "log" and _recent(env, f, prev_mtimes, days)]
    logs.sort(key=lambda f: -f["mtime"])
    for f in logs[: int(cfg["max_files"])]:
        if env.over_budget():
            break
        text, first = read_tail(roots[f["root"]] / f["rel"], int(float(cfg["tail_mb"]) * 1024 * 1024))
        lines = text.splitlines()
        when = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d %H:%M")
        hits: dict[str, list[int]] = {}
        for i, ln in enumerate(lines):
            for grp, rx, *_ in LOG_PATTERNS:
                if rx.search(ln):
                    if grp == "nan_inf" and not re.search(r"[=:\s]\s*[-+]?(nan|inf|NaN|Inf)|\*{6,}", ln):
                        continue
                    hits.setdefault(grp, []).append(i)
                    break
        rep = {"ref": f["ref"], "modified": when, "counts": {k: len(v) for k, v in hits.items()}}
        report.append(rep)
        for grp, rx, ctype, allowed, base in LOG_PATTERNS:
            idx = hits.get(grp, [])
            if not idx or (grp in ("warning", "error") and len(idx) < 5 and grp == "warning"):
                continue
            if grp in ("traceback", "error"):  # the same crash in many logs → one finding
                last = idx[-1]
                sig_line = lines[last]
                if grp == "traceback" and sig_line.startswith("Traceback"):  # name the crash by its exception line
                    sig_line = next((lines[j] for j in range(last + 1, min(len(lines), last + 40))
                                     if lines[j].strip() and not lines[j].startswith((" ", "\t"))), sig_line)
                sig = _signature(sig_line)
                g = crash_groups.setdefault(f"{grp}|{sig}", {"grp": grp, "sig": sig, "files": [], "count": 0,
                                                            "ctype": ctype, "allowed": allowed, "base": base})
                g["count"] += len(idx)
                if len(g["files"]) < 5:
                    last = idx[-1]
                    a, b = max(0, last - (12 if grp == "traceback" else 2)), min(len(lines), last + (3 if grp == "traceback" else 2))
                    if grp == "traceback":
                        tb_end = next((j for j in range(last + 1, min(len(lines), last + 40)) if lines[j] and not lines[j].startswith((" ", "\t", "Traceback"))), last)
                        a, b = last, min(len(lines), tb_end + 1)
                    g["files"].append({"ref": f["ref"], "when": when, "n": len(idx), "block": numbered(lines[a:b][:16], first + a)})
                continue
            sample = idx[:4] + ([idx[-1]] if len(idx) > 4 else [])
            body = "\n".join(numbered([lines[i]], first + i) for i in sample)
            ev = (f"日志扫描 · {f['ref']}（修改于 {when}）\n{GROUP_LABEL[grp]}：{len(idx)} 行"
                  f"（首次在第 {first + idx[0]} 行，最后在第 {first + idx[-1]} 行）\n\n{body}")
            sev = base + 0.4 * math.log10(len(idx) + 1) + (0.5 if datetime.now().timestamp() - f["mtime"] < 7 * 86400 else 0)
            findings.append(finding(
                f"logs.{grp}", "anomaly", ctype, f"{Path(f['rel']).name}：{GROUP_LABEL[grp]}（{len(idx)} 处）", ev, sev,
                fp=f"{f['ref']}|{grp}", allowed=allowed, ref=f"{f['ref']}#L{first + idx[0]}", subfield="logs",
                summary={"nan_inf": "数值溢出 / 除零 / 初值问题都会产生 NaN；先定位第一次出现的步数，再查那一步的输入。",
                         "convergence": "迭代没有收敛：可能是步长、混合参数、初猜或物理参数落在难区。",
                         "warning": "警告反复出现，可能掩盖真正的问题，或说明某个默认参数不合适。"}.get(grp, ""),
                metrics={"count": len(idx), "first_line": first + idx[0], "last_line": first + idx[-1]}))
        # numeric series in key = value lines
        series: dict[str, list[tuple[int, float]]] = {}
        for i, ln in enumerate(lines):
            for m in KV.finditer(ln):
                key = re.sub(r"\s+", " ", m.group("key").strip().lower())
                if len(key) < 2 or key in ("step", "iter", "iteration", "epoch", "time", "t", "i", "n", "line"):
                    continue
                try:
                    v = float(m.group("val"))
                except ValueError:
                    continue
                if math.isfinite(v):
                    series.setdefault(key, []).append((i, v))
        for key, pts in series.items():
            if len(pts) < 20:
                continue
            vals = [v for _, v in pts]
            med, s = _robust(vals)
            if s <= 0:
                continue
            out = [(i, v) for i, v in pts if abs(v - med) / s > float(env.cfg["data"]["z"])]
            if not out or len(out) > max(3, 0.05 * len(pts)):
                continue
            body = "\n".join(numbered([lines[i]], first + i) for i, _ in out[:5])
            ev = (f"日志数值序列 · {f['ref']}（修改于 {when}）\n键 {key}：{len(pts)} 个数值，中位数 {med:.6g}，"
                  f"稳健标准差 {s:.3g}；{len(out)} 个离群值（偏离中位数超过 {env.cfg['data']['z']:g} 倍）\n\n{body}")
            findings.append(finding(
                "logs.outlier", "anomaly", "hypothesis", f"{Path(f['rel']).name}：{key} 有 {len(out)} 个离群值", ev,
                2.8 + min(1.0, len(out) / 3), fp=f"{f['ref']}|{key}", allowed=["hypothesis", "optimization"],
                ref=f"{f['ref']}#L{first + out[0][0]}", subfield="logs",
                summary="同一个量在大多数步里很稳定，个别步突然跳走：可能是物理事件，也可能是数值问题或记录错误。",
                metrics={"n": len(pts), "median": med, "outliers": len(out)}))
    for key, g in crash_groups.items():
        if g["grp"] == "error" and g["count"] < 3:
            continue
        blocks = "\n\n".join(f"【{x['ref']} · {x['when']} · {x['n']} 次】\n{x['block']}" for x in g["files"])
        ev = (f"日志扫描 · {GROUP_LABEL[g['grp']]}，同类共 {g['count']} 次、涉及 {len(g['files'])} 个日志\n"
              f"特征：{g['sig']}\n\n{blocks}")
        sev = g["base"] + 0.4 * math.log10(g["count"] + 1) + 0.3 * (len(g["files"]) - 1)
        findings.append(finding(
            f"logs.{g['grp']}", "anomaly", g["ctype"], f"反复出现的{GROUP_LABEL[g['grp']]}：{g['sig'][:50]}", ev, sev,
            fp=key, allowed=g["allowed"], ref=g["files"][0]["ref"], subfield="logs",
            summary="同一个报错在多个日志里反复出现：值得一次性修掉或加保护。",
            metrics={"count": g["count"], "files": len(g["files"])}))
    return findings, report


def _sniff_rows(text: str, ext: str) -> tuple[list[str] | None, list[list[str]], int]:
    lines = text.splitlines()
    body_start = 0
    while body_start < len(lines) and (not lines[body_start].strip() or lines[body_start].lstrip().startswith(("#", "%", "//"))):
        body_start += 1
    sample = "\n".join(lines[body_start:body_start + 20])
    if ext in (".dat", ".txt", ".xyz") or ("," not in sample and "\t" not in sample and ";" not in sample):
        rows = [ln.split() for ln in lines[body_start:]]
    else:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO("\n".join(lines[body_start:])), dialect))
    header = None
    if rows and any(c and not _isnum(c) for c in rows[0]):
        header, rows = rows[0], rows[1:]
        body_start += 1
    return header, rows, body_start


def _isnum(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return s.strip().lower() in ("nan", "inf", "-inf", "")


def scan_data(env: Env, manifest: dict, prev_mtimes: dict) -> tuple[list[dict], list[dict]]:
    cfg = env.cfg["data"]
    days = float(env.cfg.get("lookback_days", 30))
    roots = {r["label"]: r["path"] for r in env.roots()}
    z = float(cfg["z"])
    findings, report = [], []
    cands = [f for f in manifest.get("files", []) if f["cls"] == "data" and f["ext"] in (".csv", ".tsv", ".dat", ".txt", ".xyz")
             and f["size"] <= float(cfg["max_mb"]) * 1024 * 1024 and _recent(env, f, prev_mtimes, days)]
    cands.sort(key=lambda f: -f["mtime"])
    for f in cands[: int(cfg["max_files"])]:
        if env.over_budget():
            break
        text = read_text(roots[f["root"]] / f["rel"], int(float(cfg["max_mb"]) * 1024 * 1024) + 1)
        if not text:
            continue
        raw_lines = text.splitlines()
        header, rows, off = _sniff_rows(text, f["ext"])
        rows = rows[: int(cfg["max_rows"])]
        if len(rows) < 20:
            continue
        ncol = max(len(r) for r in rows)
        when = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d %H:%M")
        col_lines, bad_rows, mets = [], [], {}
        for j in range(min(ncol, 60)):
            cells = [r[j].strip() if j < len(r) else "" for r in rows]
            nums, miss, non = [], 0, 0
            for k, c in enumerate(cells):
                if c == "" or c.lower() in ("nan", "na", "null", "none", "-"):
                    miss += 1
                    continue
                try:
                    v = float(c)
                except ValueError:
                    non += 1
                    continue
                if not math.isfinite(v):
                    miss += 1
                    continue
                nums.append((k, v))
            if len(nums) < 0.9 * (len(cells) - miss) or len(nums) < 20:
                continue
            name = header[j] if header and j < len(header) and header[j].strip() else f"第{j + 1}列"
            med, s = _robust([v for _, v in nums])
            outs = [(k, v) for k, v in nums if s > 0 and abs(v - med) / s > z]
            if miss and miss < 0.5 * len(cells):
                col_lines.append(f"列 {name}：{len(cells)} 行中缺失 / NaN {miss} 个")
                mets[f"{name}.missing"] = miss
            if outs and len(outs) <= max(3, 0.02 * len(nums)):
                col_lines.append(f"列 {name}：中位数 {med:.6g}，稳健标准差 {s:.3g}，{len(outs)} 个离群值（>{z:g} 倍）")
                mets[f"{name}.outliers"] = len(outs)
                bad_rows += [k for k, _ in outs[:3]]
        if not col_lines:
            report.append({"ref": f["ref"], "rows": len(rows), "issues": 0})
            continue
        report.append({"ref": f["ref"], "rows": len(rows), "issues": len(col_lines)})
        body = ""
        if bad_rows:
            idxs = sorted(set(bad_rows))[:5]
            body = "\n\n离群行原文：\n" + "\n".join(numbered([raw_lines[off + k]], off + k + 1) for k in idxs if off + k < len(raw_lines))
        hdr = ("表头：" + ", ".join(header[:12]) + ("…" if len(header) > 12 else "") + "\n") if header else ""
        ev = f"数据扫描 · {f['ref']}（修改于 {when}，{len(rows)} 行）\n{hdr}" + "\n".join(col_lines[:8]) + body
        n_out = sum(v for k, v in mets.items() if k.endswith(".outliers"))
        findings.append(finding(
            "data.anomaly", "anomaly", "hypothesis", f"{Path(f['rel']).name}：{len(col_lines)} 处数据异常（离群 {n_out}）", ev,
            2.6 + min(1.2, 0.4 * len(col_lines)) + (0.4 if datetime.now().timestamp() - f["mtime"] < 7 * 86400 else 0),
            fp=f["ref"], allowed=["hypothesis", "optimization"], ref=f["ref"], subfield="data",
            summary="最近改动的数据文件里有离群值或缺失：先确认是真实信号还是录入 / 单位 / 转换错误。",
            metrics=mets))
    return findings, report


def scan(env: Env, manifest: dict, prev_manifest: dict | None) -> tuple[list[dict], dict]:
    prev_mtimes = {f["ref"]: f.get("mtime") for f in (prev_manifest or {}).get("files", [])}
    f1, r1 = scan_logs(env, manifest, prev_mtimes)
    f2, r2 = scan_data(env, manifest, prev_mtimes)
    env.write_json("anomalies.json", {"generated_at": datetime.now().isoformat(timespec="seconds"), "logs": r1, "data": r2})
    return f1 + f2, {"logs": len(r1), "data": len(r2), "findings": len(f1) + len(f2)}

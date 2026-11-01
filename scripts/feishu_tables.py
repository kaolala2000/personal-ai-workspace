#!/usr/bin/env python3
"""飞书表格类内容抓取（只读）：电子表格 sheet 值 + 多维表格结构与记录。

输出 knowledge/raw/feishu_tables/<token>.md（Markdown 表格），
进度记入 state_feishu.json 的 fetched_tables，可断点续跑。
限额保护：每 sheet 最多 300 行 x 26 列；每 base 最多 10 张表、每表最多 100 条记录。
用法：python3 feishu_tables.py [limit]
"""
import json
import subprocess
import sys
import time
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "tools" / "lark-cli-app" / "node_modules" / ".bin" / "lark-cli"
ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "sync" / "state_feishu.json"
OUT_DIR = ROOT / "knowledge" / "raw" / "feishu_tables"
import os
MAX_ROWS, MAX_COLS = 300, 26
MAX_TABLES = int(os.environ.get("FT_MAX_TABLES", "10"))
MAX_RECS = int(os.environ.get("FT_MAX_RECS", "100"))


def run_cli(args):
    r = subprocess.run([str(CLI), *args], capture_output=True, text=True, timeout=120)
    try:
        d = json.loads(r.stdout.strip())
    except Exception:
        raise RuntimeError(f"非JSON输出: {r.stdout[:120]} {r.stderr[:160]}")
    if not d.get("ok", True):
        raise RuntimeError(str(d.get("error"))[:200])
    return d.get("data") or {}


def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def cell(v):
    if v is None:
        return ""
    if isinstance(v, dict):
        return str(v.get("text") or v.get("name") or v.get("link") or "")[:80]
    if isinstance(v, list):
        return " ".join(cell(x) for x in v)[:80]
    return str(v).replace("|", "/").replace("\n", " ")[:80]


def md_table(rows):
    rows = [[cell(c) for c in r] for r in rows]
    while rows and all(not c for c in rows[-1]):
        rows.pop()
    if not rows:
        return "（空）"
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "|" + " --- |" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def col_letter(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def fetch_sheet(token, title):
    meta = run_cli(["api", "GET", f"/open-apis/sheets/v3/spreadsheets/{token}/sheets/query"])
    parts = []
    for sh in meta.get("sheets") or []:
        gp = sh.get("grid_properties") or {}
        rows = min(gp.get("row_count") or 0, MAX_ROWS)
        cols = min(gp.get("column_count") or 0, MAX_COLS)
        parts.append(f"## 工作表：{sh.get('title')}（{gp.get('row_count')} 行 x {gp.get('column_count')} 列"
                     f"{'，隐藏' if sh.get('hidden') else ''}）")
        if rows and cols:
            rng = f"{sh.get('sheet_id')}!A1:{col_letter(cols)}{rows}"
            data = run_cli(["api", "GET",
                            f"/open-apis/sheets/v2/spreadsheets/{token}/values/{rng}"])
            parts.append(md_table((data.get("valueRange") or {}).get("values") or []))
            if (gp.get("row_count") or 0) > MAX_ROWS:
                parts.append(f"（仅取前 {MAX_ROWS} 行）")
        parts.append("")
        time.sleep(0.2)
    return title, "\n".join(parts)


def fetch_bitable(token, title):
    data = run_cli(["api", "GET", f"/open-apis/bitable/v1/apps/{token}/tables",
                    "--params", '{"page_size":100}'])
    tables = data.get("items") or []
    parts = [f"共 {len(tables)} 张数据表"]
    for t in tables[:MAX_TABLES]:
        tid = t.get("table_id")
        parts.append(f"## 数据表：{t.get('name')}")
        fdata = run_cli(["api", "GET",
                         f"/open-apis/bitable/v1/apps/{token}/tables/{tid}/fields",
                         "--params", '{"page_size":100}'])
        fields = [f.get("field_name") for f in fdata.get("items") or []]
        rdata = run_cli(["api", "GET",
                         f"/open-apis/bitable/v1/apps/{token}/tables/{tid}/records",
                         "--params", f'{{"page_size":{MAX_RECS}}}'])
        recs = rdata.get("items") or []
        rows = [fields]
        for rec in recs:
            fd = rec.get("fields") or {}
            rows.append([fd.get(fn) for fn in fields])
        parts.append(f"字段 {len(fields)} 个，记录总数 {rdata.get('total')}，以下为前 {len(recs)} 条：")
        parts.append(md_table(rows))
        parts.append("")
        time.sleep(0.2)
    return title, "\n".join(parts)


def targets(state):
    out = []
    for v in state["wiki_nodes"].values():
        if v.get("obj_type") in ("sheet", "bitable") and v.get("obj_token"):
            out.append((v["obj_token"], v.get("obj_type"), v.get("title") or "",
                        v.get("url", "")))
    seen = {t[0] for t in out}
    for tok, v in state["drive_files"].items():
        if v.get("type") in ("sheet", "bitable") and tok not in seen:
            out.append((tok, v["type"], v.get("name") or "", v.get("url", "")))
    return out


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 999
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    done_map = state.setdefault("fetched_tables", {})
    todo = [t for t in targets(state)
            if done_map.get(t[0], {}).get("status") != "fetched"]
    print(f"表格类目标共 {len(targets(state))}，待抓 {len(todo)}，本批上限 {limit}")
    done = 0
    for token, otype, title, url in todo[:limit]:
        try:
            if otype == "sheet":
                title, body = fetch_sheet(token, title)
                kind = "sheets"
            else:
                title, body = fetch_bitable(token, title)
                kind = "base"
        except Exception as exc:
            done_map[token] = {"status": f"error: {exc}"[:160]}
            save_state(state)
            continue
        fm = (f"---\ntitle: {title}\nobj_type: {otype}\n"
              f"source_url: https://feishu.cn/{kind}/{token}\nobj_token: {token}\n---\n\n")
        (OUT_DIR / f"{token}.md").write_text(fm + body, encoding="utf-8")
        done_map[token] = {"status": "fetched", "chars": len(body),
                           "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        done += 1
        save_state(state)
        time.sleep(0.3)
    print(f"本批完成 {done}，累计 {sum(1 for x in done_map.values() if x.get('status') == 'fetched')}")


if __name__ == "__main__":
    main()

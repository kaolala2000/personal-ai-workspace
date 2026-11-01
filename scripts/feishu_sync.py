#!/usr/bin/env python3
"""飞书只读同步（经官方 lark-cli，用户身份）：发现 -> 盘点 -> 按需取正文。

只调用 lark-cli 标记为 read 风险的命令。配额意识：免费租户自建应用共享
10,000 次/月，盘点（列表类调用）优先，正文抓取分批进行。

用法：
  python3 feishu_sync.py discover    # 知识空间 + 个人库 + 云空间根目录概览
  python3 feishu_sync.py inventory   # 递归盘点全部节点/文件（仅元数据）
状态：sync/state_feishu.json（token/类型/标题/父级/编辑时间，用于增量比对）。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "tools" / "lark-cli-app" / "node_modules" / ".bin" / "lark-cli"
ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "sync" / "state_feishu.json"
INDEX_DIR = ROOT / "knowledge" / "index"


def run_cli(args):
    r = subprocess.run([str(CLI), *args], capture_output=True, text=True, timeout=120)
    out = r.stdout.strip()
    try:
        d = json.loads(out)
    except Exception:
        raise RuntimeError(f"lark-cli 输出不可解析: {' '.join(args[:3])} -> {out[:200]} {r.stderr[:200]}")
    if not d.get("ok", True) and d.get("error"):
        raise RuntimeError(f"lark-cli 失败: {' '.join(args[:3])} -> {str(d['error'])[:200]}")
    return d.get("data") or {}


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"wiki_nodes": {}, "drive_files": {}}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def list_spaces():
    data = run_cli(["wiki", "+space-list", "--page-all", "--json"])
    spaces = data.get("spaces") or []
    lib = run_cli(["wiki", "spaces", "get", "--params", '{"space_id":"my_library"}'])
    if lib.get("space"):
        spaces.append(lib["space"])
    return spaces


def walk_wiki(space_id, parent=None, depth=0):
    args = ["wiki", "+node-list", "--space-id", space_id, "--page-all", "--json"]
    if parent:
        args += ["--parent-node-token", parent]
    data = run_cli(args)
    nodes = data.get("nodes") or []
    out = []
    for n in nodes:
        out.append(n)
        if n.get("has_child") and depth < 12:
            time.sleep(0.2)
            out.extend(walk_wiki(space_id, n.get("node_token"), depth + 1))
    return out


def walk_drive(folder_token="", depth=0):
    """folder_token 为空 = 根目录（官方注：根目录不分页、一次全量）。"""
    files, page_token = [], ""
    while True:
        params = {"folder_token": folder_token, "page_size": 200}
        if page_token:
            params["page_token"] = page_token
        data = run_cli(["api", "GET", "/open-apis/drive/v1/files",
                        "--params", json.dumps(params)])
        files.extend(data.get("files") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("next_page_token") or ""
        time.sleep(0.2)
    out = []
    for f in files:
        out.append(f)
        if f.get("type") == "folder" and depth < 12:
            time.sleep(0.2)
            out.extend(walk_drive(f.get("token"), depth + 1))
    return out


def discover():
    spaces = list_spaces()
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    (INDEX_DIR / "feishu_spaces.json").write_text(
        json.dumps({"spaces": spaces}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"知识空间 {len(spaces)} 个：")
    for s in spaces:
        print(f"  - {s.get('name')} ({s.get('space_type')}) id={s.get('space_id')}")
    return spaces


def inventory():
    state = load_state()
    spaces = list_spaces()
    total_wiki = 0
    for s in spaces:
        nodes = walk_wiki(s["space_id"])
        for n in nodes:
            key = n.get("node_token") or n.get("obj_token")
            state["wiki_nodes"][key] = {
                "title": n.get("title"), "obj_type": n.get("obj_type"),
                "obj_token": n.get("obj_token"), "space_id": s["space_id"],
                "space_name": s.get("name"),
                "obj_edit_time": n.get("obj_edit_time"),
                "url": f"https://feishu.cn/wiki/{n.get('node_token')}" if n.get("node_token") else ""}
            total_wiki += 1
        print(f"空间「{s.get('name')}」节点 {len(nodes)} 个")
    files = walk_drive("")
    for f in files:
        state["drive_files"][f.get("token")] = {
            "name": f.get("name"), "type": f.get("type"),
            "parent_token": f.get("parent_token"),
            "created_time": f.get("created_time"),
            "modified_time": f.get("modified_time"), "url": f.get("url")}
    save_state(state)
    print(f"盘点完成：wiki 节点 {total_wiki} 个，云空间文件/文件夹 {len(files)} 个 -> {STATE_FILE}")


RAW_DIR = ROOT / "knowledge" / "raw" / "feishu"


def fetch_docx(limit=50):
    """分批抓取 wiki docx 正文（markdown）。每篇抓完立即落盘并记状态，可断点续跑。
    注意：正文内嵌的 sheet/bitable 只会以占位形式出现，需后续按 token 单独抓。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    fetched = state.setdefault("fetched", {})
    todo = [(k, v) for k, v in state["wiki_nodes"].items()
            if v.get("obj_type") == "docx" and v.get("obj_token")
            and v["obj_token"] not in fetched]
    print(f"待抓 docx {len(todo)} 篇，本批上限 {limit}")
    done = 0
    for _, v in todo[:limit]:
        token = v["obj_token"]
        try:
            data = run_cli(["docs", "+fetch", "--doc", token,
                            "--doc-format", "markdown", "--json"])
            content = (data.get("document") or {}).get("content") or ""
        except Exception as exc:
            fetched[token] = {"status": f"error: {exc}"[:160]}
            save_state(state)
            continue
        fm = (f"---\ntitle: {v.get('title', '')}\nsource_url: {v.get('url', '')}\n"
              f"space: {v.get('space_name', '')}\nobj_token: {token}\n---\n\n")
        (RAW_DIR / f"{token}.md").write_text(fm + content, encoding="utf-8")
        fetched[token] = {"status": "fetched", "chars": len(content),
                          "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        done += 1
        save_state(state)  # 每篇落状态，进程中断不丢进度
        time.sleep(0.3)
    print(f"本批完成 {done} 篇，累计已抓 {sum(1 for x in fetched.values() if x.get('status') == 'fetched')} 篇")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "discover":
        discover()
    elif cmd == "inventory":
        inventory()
    elif cmd == "fetch":
        fetch_docx(int(sys.argv[2]) if len(sys.argv) > 2 else 50)
    else:
        print(__doc__)

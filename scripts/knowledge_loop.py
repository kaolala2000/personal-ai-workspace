#!/usr/bin/env python3
"""业务知识进化闭环：记录、检索、关联、版本更新、复盘。

存储：loop/{facts,cases,decisions,sops,hypotheses,methods}/*.md
每条记录 = Markdown + 头部元数据（id/type/title/date/source/status/market/
links/verify_by/version/supersedes/superseded_by），index.jsonl 为总索引。
原则：旧结论被推翻时只标记 superseded 并保留原文，永不删除；
未验证推测只能进 hypotheses，status=pending_verification，不得当作事实。

用法：
  python3 loop.py add '<json>'          # 新增记录，自动查重提示相似记录
  python3 loop.py search 关键词          # 全库检索（含状态与关联）
  python3 loop.py supersede OLD NEW     # 版本更新：旧标记被替代，双方互链
  python3 loop.py resolve ID verified|refuted 备注   # 回填验证结果
  python3 loop.py review                # 列出全部待验证事项（到期优先）
  python3 loop.py stats                 # 各库计数
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIRS = {"fact": "facts", "case": "cases", "decision": "decisions",
        "sop": "sops", "hypothesis": "hypotheses", "method": "methods"}
PREFIX = {"fact": "FACT", "case": "CASE", "decision": "DEC",
          "sop": "SOP", "hypothesis": "HYP", "method": "METH"}
INDEX = ROOT / "index.jsonl"


def today():
    return time.strftime("%Y-%m-%d")


def parse_record(path):
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            raw, body = text[3:end], text[end + 4:]
            for line in raw.strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
    meta["body"] = body.strip()
    meta["path"] = str(path)
    return meta


def all_records():
    out = []
    for d in DIRS.values():
        for p in sorted((ROOT / d).glob("*.md")):
            out.append(parse_record(p))
    return out


def find(rid):
    for r in all_records():
        if r.get("id") == rid:
            return r
    return None


def render(meta, body):
    order = ["id", "type", "title", "date", "source", "status", "market",
             "links", "verify_by", "version", "supersedes", "superseded_by"]
    lines = ["---"]
    for k in order:
        if meta.get(k):
            lines.append(f"{k}: {meta[k]}")
    lines += ["---", "", body, ""]
    return "\n".join(lines)


def reindex():
    """索引以记录文件为唯一事实来源，整体重建（避免追加式索引出现过期状态）。"""
    rows = [{k: r.get(k, "") for k in
             ("id", "type", "title", "date", "source", "status")}
            for r in all_records()]
    INDEX.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows),
                     encoding="utf-8")
    return len(rows)


def write_record(meta, body):
    path = ROOT / DIRS[meta["type"]] / f"{meta['id']}.md"
    path.write_text(render(meta, body), encoding="utf-8")
    reindex()
    return path


def next_id(rtype):
    prefix = PREFIX[rtype]
    stamp = time.strftime("%Y%m%d")
    n = sum(1 for r in all_records()
            if r.get("id", "").startswith(f"{prefix}-{stamp}")) + 1
    return f"{prefix}-{stamp}-{n:03d}"


def similar(title, body):
    """粗查重：标题/正文与新记录有共同关键词（>=2 个 2 字以上词组重叠）。"""
    def grams(s):
        s = re.sub(r"[^\w一-鿿]", "", s)
        return {s[i:i + 3] for i in range(max(0, len(s) - 2))}
    g = grams(title + body)
    hits = []
    for r in all_records():
        overlap = len(g & grams(r.get("title", "") + r["body"]))
        if overlap >= 3:
            hits.append((overlap, r["id"], r.get("title", ""), r.get("status", "")))
    return sorted(hits, reverse=True)[:5]


def cmd_add(payload):
    data = json.loads(payload)
    rtype = data["type"]
    meta = {"id": data.get("id") or next_id(rtype), "type": rtype,
            "title": data["title"], "date": data.get("date") or today(),
            "source": data.get("source", "chat"),
            "status": data.get("status") or
                      ("pending_verification" if rtype == "hypothesis" else "active"),
            "market": data.get("market", ""), "version": str(data.get("version", 1))}
    for k in ("links", "verify_by", "supersedes"):
        if data.get(k):
            meta[k] = data[k] if isinstance(data[k], str) else ",".join(data[k])
    sims = similar(meta["title"], data.get("body", ""))
    path = write_record(meta, data.get("body", ""))
    print(f"已保存 {meta['id']} -> {path}")
    if sims:
        print("相似历史记录（请判断是补充/修正/重复，并建立 links 关联）：")
        for _, rid, t, st in sims:
            print(f"  {rid} [{st}] {t}")


def cmd_search(q):
    hits = [r for r in all_records()
            if q in r.get("title", "") or q in r["body"] or q in r.get("id", "")]
    if not hits:
        print("无匹配记录")
    for r in hits:
        print(f"{r['id']} [{r['type']}/{r.get('status', '')}] {r.get('title', '')}"
              f" | date={r.get('date', '')} source={r.get('source', '')}"
              f" links={r.get('links', '') or '-'}")


def cmd_supersede(old_id, new_id):
    old, new = find(old_id), find(new_id)
    if not old or not new:
        print("记录不存在")
        return
    old_path = Path(old["path"])
    old["status"] = "superseded"
    old["superseded_by"] = new_id
    old_path.write_text(render(old, old["body"]), encoding="utf-8")
    new_path = Path(new["path"])
    new["supersedes"] = old_id
    links = [x for x in new.get("links", "").split(",") if x]
    if old_id not in links:
        links.append(old_id)
    new["links"] = ",".join(links)
    new_path.write_text(render(new, new["body"]), encoding="utf-8")
    print(f"版本更新完成：{old_id} 已标记 superseded（原文保留），由 {new_id} 替代")


def cmd_resolve(rid, outcome, note):
    r = find(rid)
    if not r:
        print("记录不存在")
        return
    r["status"] = "verified" if outcome == "verified" else "refuted"
    body = r["body"] + f"\n\n## 验证结果（{today()}）\n结论：{outcome}\n{note}\n"
    Path(r["path"]).write_text(render(r, body), encoding="utf-8")
    print(f"{rid} 已更新为 {r['status']}")


def cmd_review():
    rows = [r for r in all_records() if r.get("status") == "pending_verification"]
    if not rows:
        print("当前没有待验证事项")
        return
    for r in sorted(rows, key=lambda x: x.get("verify_by", "9999")):
        due = "已到期" if r.get("verify_by", "9999") <= today() else "未到期"
        print(f"{r['id']} [{due} verify_by={r.get('verify_by', '-')}] "
              f"{r.get('title', '')} links={r.get('links', '') or '-'}")


def cmd_stats():
    from collections import Counter
    recs = all_records()
    print("按类型:", dict(Counter(r["type"] for r in recs)))
    print("按状态:", dict(Counter(r.get("status", "") for r in recs)))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "add":
        cmd_add(sys.argv[2])
    elif cmd == "search":
        cmd_search(sys.argv[2])
    elif cmd == "supersede":
        cmd_supersede(sys.argv[2], sys.argv[3])
    elif cmd == "resolve":
        cmd_resolve(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "")
    elif cmd == "review":
        cmd_review()
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "reindex":
        print(f"索引已重建：{reindex()} 条")
    else:
        print(__doc__)

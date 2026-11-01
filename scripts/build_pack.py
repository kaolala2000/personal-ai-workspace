#!/usr/bin/env python3
"""BoBo OS 知识包构建器（增量、稳定 ID）。

把 Muse 侧知识资产（飞书原文、ima 条目、知识卡片、闭环记录）打包为
标准化知识包，供 ChatGPT BoBo OS 导入，也作为未来双向同步的交换格式。
规范见 knowledge/pack/SPEC.md。要点：
- 稳定 ID：fs-<obj_token> / ima-<item_key> / card-fs-<obj_token> / loop-<记录ID>，
  ID 只由来源身份决定，不随内容变化。
- 增量：每条记录带 content_hash 与 version；重跑时仅内容变化的记录
  version+1 并记入 CHANGELOG，未变化记录原样保留，删除记录记 tombstone。
- 用法：python3 build_pack.py [--dry-run]
"""
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "knowledge" / "pack"
RAW_FS = ROOT / "knowledge" / "raw" / "feishu"
RAW_TABLES = ROOT / "knowledge" / "raw" / "feishu_tables"
RAW_IMA = ROOT / "knowledge" / "raw" / "ima"
CARDS = ROOT / "knowledge" / "cards"
LOOP = ROOT / "loop"

THEMES = {
    "达人-KOC-KOL-达播": r"达人|KOC|KOL|达播|BD|商务",
    "直播": r"直播|主播|话术",
    "短视频-内容": r"短视频|视频|脚本|素材|编导|拍摄",
    "选品-产品": r"选品|产品|货盘|打品|类目|商品|机制|卖点",
    "广告投放": r"广告|投放|千川|ROI|巨量",
    "数据-复盘": r"数据|报表|核算|周会|复盘",
    "团队-SOP-管理": r"SOP|团队|面试|培训|绩效|管理|排班|流程",
    "AI-工具": r"AI|GPT|提示词|工具|教程",
    "市场-调研": r"调研|市场|竞品",
}


def classify(title):
    for name, pat in THEMES.items():
        if re.search(pat, title or "", re.I):
            return name
    return "其他"


def parse_fm(text):
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            raw, body = text[3:end], text[end + 4:]
            for line in raw.strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
    return meta, body.strip()


def section(body, *names):
    for name in names:
        m = re.search(rf"^##\s*{re.escape(name)}[^\n]*\n(.*?)(?=^##\s|\Z)",
                      body, re.M | re.S)
        if m:
            return m.group(1).strip()
    return ""


def bullets(text):
    return [re.sub(r"^[-*]\s*", "", ln).strip()
            for ln in text.splitlines() if ln.strip().startswith(("-", "*"))]


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect():
    records = []
    fs_state = json.loads((ROOT / "sync/state_feishu.json").read_text(encoding="utf-8"))
    node2obj = {k: v.get("obj_token") for k, v in fs_state["wiki_nodes"].items()}

    # 1) 飞书原文
    for f in sorted(RAW_FS.glob("*.md")):
        meta, body = parse_fm(f.read_text(encoding="utf-8"))
        token = f.stem
        records.append({
            "id": f"fs-{token}", "kind": "source_doc",
            "title": meta.get("title", ""), "theme": classify(meta.get("title", "")),
            "market": "", "status": "active",
            "source": {"system": "feishu", "source_id": token,
                       "url": meta.get("source_url", "")},
            "content": body, "key_facts": [], "decision_logic": "",
            "open_questions": "", "pending_verification": False, "verify_by": "", "links": []})

    # 1b) 飞书表格类原文（电子表格/多维表格，token 与 docx 不重叠）
    if RAW_TABLES.exists():
        for f in sorted(RAW_TABLES.glob("*.md")):
            meta, body = parse_fm(f.read_text(encoding="utf-8"))
            token = f.stem
            records.append({
                "id": f"fs-{token}", "kind": "source_doc",
                "title": meta.get("title", ""), "theme": classify(meta.get("title", "")),
                "market": "", "status": "active",
                "source": {"system": "feishu", "source_id": token,
                           "url": meta.get("source_url", ""),
                           "obj_type": meta.get("obj_type", "")},
                "content": body, "key_facts": [], "decision_logic": "",
                "open_questions": "",
                "pending_verification": False, "verify_by": "", "links": []})

    # 2) 知识卡片
    for f in sorted(CARDS.rglob("*.md")):
        if f.name == "业务方法论速览.md":
            kind = "synthesis"
        else:
            kind = "card"
        meta, body = parse_fm(f.read_text(encoding="utf-8"))
        url = meta.get("source_url", "")
        m = re.search(r"/wiki/([A-Za-z0-9]+)", url)
        obj = node2obj.get(m.group(1)) if m else None
        rid = f"card-fs-{obj}" if obj else \
            f"card-{hashlib.sha1(str(f.relative_to(CARDS)).encode()).hexdigest()[:10]}"
        facts = bullets(section(body, "核心结论"))
        logic = section(body, "判断逻辑 / 方法步骤", "判断逻辑", "方法步骤")
        pending = section(body, "存疑 / 待验证", "存疑", "待验证")
        records.append({
            "id": rid, "kind": kind,
            "title": meta.get("title", f.stem),
            "theme": meta.get("theme") or classify(meta.get("title", f.stem)),
            "market": "", "status": "active",
            "source": {"system": "feishu", "source_id": obj or "",
                       "url": url},
            "content": body, "key_facts": facts, "decision_logic": logic,
            # 卡片的「存疑」只是开放问题，不等于闭环待验证事项，后者仅来自 loop 假设库
            "open_questions": pending,
            "pending_verification": False,
            "verify_by": "", "links": [f"fs-{obj}"] if obj else []})

    # 3) 闭环记录
    for sub in ("facts", "cases", "decisions", "sops", "hypotheses", "methods"):
        for f in sorted((LOOP / sub).glob("*.md")):
            meta, body = parse_fm(f.read_text(encoding="utf-8"))
            records.append({
                "id": f"loop-{meta.get('id', f.stem)}", "kind": meta.get("type", sub),
                "title": meta.get("title", ""), "theme": "知识闭环",
                "market": meta.get("market", ""), "status": meta.get("status", ""),
                "source": {"system": "claude-loop",
                           "source_id": meta.get("id", f.stem),
                           "url": "", "origin": meta.get("source", "")},
                "content": body, "key_facts": [],
                "decision_logic": body if meta.get("type") == "decision" else "",
                "open_questions": "", "pending_verification": meta.get("status") == "pending_verification",
                "verify_by": meta.get("verify_by", ""),
                "links": [x.strip() for x in meta.get("links", "").split(",") if x.strip()]})

    # 4) ima 条目（正文为二进制原文时只带路径与哈希，正文文本后续提取再增量更新）
    ima_state = json.loads((ROOT / "sync/state_ima.json").read_text(encoding="utf-8"))
    for key, v in ima_state["items"].items():
        local = RAW_IMA / f"{key}.bin"
        has = local.exists()
        records.append({
            "id": f"ima-{key}", "kind": "source_doc",
            "title": v.get("title", ""), "theme": classify(v.get("title", "")),
            "market": "", "status": v.get("fetch_status", "metadata_only"),
            "source": {"system": "ima", "source_id": key,
                       "url": "", "kb": v.get("kb_name", "")},
            "content": None,
            "content_path": f"knowledge/raw/ima/{key}.bin" if has else "",
            "content_sha256": v.get("sha256", ""),
            "key_facts": [], "decision_logic": "",
            "open_questions": "", "pending_verification": False, "verify_by": "", "links": []})
    return records


def canonical(rec):
    return json.dumps({k: rec.get(k) for k in
                       ("title", "theme", "status", "content", "content_sha256",
                        "key_facts", "decision_logic", "open_questions", "pending_verification",
                        "verify_by", "links")},
                      ensure_ascii=False, sort_keys=True)


def main():
    dry = "--dry-run" in sys.argv
    PACK.mkdir(parents=True, exist_ok=True)
    (PACK / "md").mkdir(exist_ok=True)
    prev = {}
    prev_file = PACK / "records.jsonl"
    if prev_file.exists():
        for line in prev_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                prev[r["id"]] = r
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    added, updated, removed, final = [], [], [], []
    seen = set()
    for rec in collect():
        seen.add(rec["id"])
        h = sha(canonical(rec))
        old = prev.get(rec["id"])
        if old is None:
            rec["version"] = 1
            added.append(rec["id"])
        elif old.get("content_hash") != h:
            rec["version"] = int(old.get("version", 1)) + 1
            updated.append(rec["id"])
        else:
            rec["version"] = int(old.get("version", 1))
        rec["content_hash"] = h
        rec["updated_at"] = now if (old is None or old.get("content_hash") != h) \
            else old.get("updated_at", now)
        final.append(rec)
    removed = [rid for rid in prev if rid not in seen]
    print(f"记录 {len(final)} 条：新增 {len(added)}，更新 {len(updated)}，移除 {len(removed)}")
    if dry:
        return
    if not (added or updated or removed) and prev_file.exists():
        print("无变化，知识包未改动（增量机制）")
        return
    prev_manifest = {}
    if (PACK / "manifest.json").exists():
        prev_manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    pack_version = int(prev_manifest.get("pack_version", 0)) + 1
    prev_file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in final),
                         encoding="utf-8")
    # Markdown 分主题导出（仅含文本内容的记录）
    by_theme = {}
    for r in final:
        if r.get("content"):
            by_theme.setdefault(r["theme"], []).append(r)
    for old_md in (PACK / "md").glob("*.md"):
        old_md.unlink()
    for theme, rows in sorted(by_theme.items()):
        lines = [f"# {theme}", ""]
        for r in sorted(rows, key=lambda x: x["id"]):
            lines += [f"## [{r['id']}] {r['title']}", "",
                      f"> kind: {r['kind']} | version: {r['version']} | status: {r['status']}"
                      f" | source: {r['source'].get('url') or r['source'].get('system', '')}",
                      "", r["content"], ""]
        safe = re.sub(r"[^\w一-鿿-]+", "_", theme)
        (PACK / "md" / f"{safe}.md").write_text("\n".join(lines), encoding="utf-8")
    manifest = {
        "schema": "bobo-knowledge-pack/1", "pack_version": pack_version,
        "generated_at": now, "producer": "Muse (business-system)",
        "record_count": len(final),
        "counts_by_kind": {}, "removed_this_build": removed,
        "files": ["records.jsonl", "manifest.json", "md/<theme>.md", "CHANGELOG.md"],
        "id_scheme": "fs-<feishu_obj_token> | ima-<ima_item_key> | "
                     "card-fs-<feishu_obj_token> | loop-<loop_record_id>",
        "sync_note": "以 id 为主键、content_hash 判断变化、version 单调递增；"
                     "removed_this_build 为 tombstone，供对端删除或标记失效。",
    }
    for r in final:
        manifest["counts_by_kind"][r["kind"]] = \
            manifest["counts_by_kind"].get(r["kind"], 0) + 1
    (PACK / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    with (PACK / "CHANGELOG.md").open("a", encoding="utf-8") as f:
        f.write(f"\n## pack v{pack_version} · {now}\n"
                f"- 新增 {len(added)}：{', '.join(added[:20])}{'...' if len(added) > 20 else ''}\n"
                f"- 更新 {len(updated)}：{', '.join(updated[:20])}{'...' if len(updated) > 20 else ''}\n"
                f"- 移除 {len(removed)}：{', '.join(removed[:20])}\n")
    print(f"知识包 v{pack_version} 已生成 -> {PACK}")


if __name__ == "__main__":
    main()

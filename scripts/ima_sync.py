#!/usr/bin/env python3
"""ima 知识库只读同步（发现 -> 盘点 -> 拉取原文 -> 内容哈希增量）。

凭证只从环境变量读取，永不打印、永不写入文件：
  IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY
（与腾讯官方 ima-skill 的 ima_api.cjs 同一套凭证约定。）

用法：
  python3 ima_sync.py probe            # 无凭证探活：用假凭证验证端点存在且鉴权生效
  python3 ima_sync.py discover         # 列出全部知识库，写入 index/ima_inventory.json
  python3 ima_sync.py inventory        # 递归盘点每个知识库的条目（只存元数据）
  python3 ima_sync.py fetch            # 拉取新增/变化条目的原文到 raw/ima/（哈希增量）
状态保存在 sync/state_ima.json（只含 media_id、标题、路径、内容哈希、抓取时间）。
"""
import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "https://ima.qq.com"
ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "knowledge" / "raw" / "ima"
INDEX_DIR = ROOT / "knowledge" / "index"
STATE_FILE = ROOT / "sync" / "state_ima.json"


def creds():
    cid = os.environ.get("IMA_OPENAPI_CLIENTID") or os.environ.get("IMA_CLIENT_ID")
    key = os.environ.get("IMA_OPENAPI_APIKEY") or os.environ.get("IMA_API_KEY")
    if not cid and os.environ.get("IMA_CREDS_STDIN") == "1":
        # 从 stdin 读两行（client_id / api_key），避免凭证出现在命令行或文件中
        lines = sys.stdin.read().splitlines()
        if len(lines) >= 2:
            cid, key = lines[0].strip(), lines[1].strip()
    return cid, key


def call(path, body, cid, key, _redirects=0):
    url = f"{BASE}/{path}" if path.startswith("openapi/") else path
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "ima-openapi-clientid": cid or "",
        "ima-openapi-apikey": key or "",
        "ima-openapi-ctx": "skill_version=1.1.10",
        "Content-Type": "application/json",
    }
    last = None
    for attempt in range(5):  # 超时/频控/网关 307 均退避重试（实测 307 为间歇性，同址重试可成功）
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            if out.get("code") in (110021, 200001, 110010) and attempt < 4:
                time.sleep(0.5 * (2 ** attempt))
                continue
            return out
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if e.code in (301, 302, 303, 307, 308):
                if loc and loc != url and _redirects < 3:
                    return call(loc, body, cid, key, _redirects + 1)
                last = f"http_{e.code}"
                time.sleep(0.5 * (2 ** attempt))
                continue
            return {"code": f"http_{e.code}", "msg": e.read().decode("utf-8", "ignore")[:300]}
        except Exception as exc:  # 超时/连接重置/IncompleteRead 等，退避后重试
            last = exc
            time.sleep(0.5 * (2 ** attempt))
    return {"code": "network_error", "msg": str(last)[:300]}


def paginate(path, body, list_key, cid, key, limit_key="limit", limit=50):
    """游标分页：cursor/is_end/next_cursor（官方 api.md 规范）。"""
    items, cursor = [], ""
    while True:
        b = dict(body)
        b["cursor"] = cursor
        if limit_key:
            b[limit_key] = limit
        r = call(path, b, cid, key)
        if r.get("code") != 0:
            raise RuntimeError(f"{path} failed: code={r.get('code')} msg={r.get('msg')}")
        data = r.get("data") or {}
        items.extend(data.get(list_key) or [])
        if data.get("is_end", True):
            return items
        cursor = data.get("next_cursor") or ""
        time.sleep(0.3)  # 避开频控 110021 / 20002


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"items": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def probe():
    r = call("openapi/wiki/v1/search_knowledge_base",
             {"query": "", "cursor": "", "limit": 1}, "probe-invalid", "probe-invalid")
    print(json.dumps({"endpoint": "POST /openapi/wiki/v1/search_knowledge_base",
                      "response_code": r.get("code"), "msg": r.get("msg")}, ensure_ascii=False))
    print("结论：端点可达且鉴权生效（假凭证被拒）" if r.get("code") != 0
          else "警告：假凭证未被拒绝，需复查")


def discover(cid, key):
    # 双源合并（2026-11-01 真实调用校准）：
    # - search_knowledge_base 返回全部可访问知识库（含加入/订阅的），字段为
    #   kb_id/kb_name/content_count/base_type/role_type；
    # - get_addable_knowledge_base_list 返回可写入的知识库（字段 id/name），
    #   用来标记哪些是用户自己的库。两者字段名不同，必须先归一化。
    kbs, seen = [], set()
    for kb in paginate("openapi/wiki/v1/search_knowledge_base", {"query": ""},
                       "info_list", cid, key, limit=20):
        kid = kb.get("kb_id") or kb.get("id")
        if not kid or kid in seen:
            continue
        seen.add(kid)
        kbs.append({"id": kid, "name": kb.get("kb_name") or kb.get("name"),
                    "content_count": kb.get("content_count"),
                    "base_type": kb.get("base_type"), "role_type": kb.get("role_type"),
                    "creator": kb.get("creator"), "own": False})
    try:
        for kb in paginate("openapi/wiki/v1/get_addable_knowledge_base_list", {},
                           "addable_knowledge_base_list", cid, key, limit=50):
            kid = kb.get("id") or kb.get("kb_id")
            if not kid:
                continue
            if kid in seen:
                for k in kbs:
                    if k["id"] == kid:
                        k["own"] = True
            else:
                seen.add(kid)
                kbs.append({"id": kid, "name": kb.get("name"), "own": True})
    except RuntimeError as exc:
        print(f"get_addable_knowledge_base_list 枚举失败（忽略）：{exc}")
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    out = INDEX_DIR / "ima_inventory.json"
    out.write_text(json.dumps({"knowledge_bases": kbs}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"发现知识库 {len(kbs)} 个，已写入 {out}")
    return kbs


def walk_kb(kb_id, cid, key):
    """递归列出一个知识库的全部条目。真实响应（2026-11-01 实测）：
    文件夹也出现在 knowledge_list 中，media_type=99、media_id 形如 folder_*，
    以其 media_id 作为 folder_id 下钻；根目录 folder_id 等于知识库 id。"""
    entries, stack, visited = [], [kb_id], set()
    while stack:
        folder = stack.pop()
        if folder in visited:
            continue
        visited.add(folder)
        # limit 用 20 而非 50：本机到 ima 网关的大响应（>~7KB）会被间歇截断
        # （IncompleteRead），小分页可显著降低单响应体积。
        items = paginate("openapi/wiki/v1/get_knowledge_list",
                         {"knowledge_base_id": kb_id, "folder_id": folder},
                         "knowledge_list", cid, key, limit=20)
        for it in items:
            if it.get("media_type") == 99:
                stack.append(it.get("media_id"))
            else:
                entries.append(it)
    return entries


def inventory(cid, key):
    inv = json.loads((INDEX_DIR / "ima_inventory.json").read_text(encoding="utf-8"))
    state = load_state()
    total = 0
    seen_ids = set()
    walked_kbs = set()
    for kb in inv["knowledge_bases"]:
        try:
            entries = walk_kb(kb["id"], cid, key)
        except RuntimeError as exc:  # 单个库失败不中断整体盘点
            print(f"知识库「{kb.get('name')}」盘点失败，跳过：{exc}")
            continue
        walked_kbs.add(kb["id"])
        for e in entries:
            mid = e.get("media_id")
            if not mid:
                continue
            total += 1
            seen_ids.add(mid)
            state["items"].setdefault(mid, {}).update(
                {"title": e.get("title"), "kb_id": kb["id"], "kb_name": kb.get("name"),
                 "media_type": e.get("media_type"),
                 "parent_folder_id": e.get("parent_folder_id")})
    # 仅对本轮完整走完的知识库判定条目消失（防限频/失败误判为删除，同 WeKnora 做法）
    missing = [mid for mid, v in state["items"].items()
               if mid not in seen_ids and v.get("kb_id") in walked_kbs]
    for mid in missing:
        state["items"][mid]["fetch_status"] = "missing_in_latest_inventory"
    save_state(state)
    print(f"盘点条目 {total} 个（仅元数据），消失 {len(missing)} 个，状态写入 {STATE_FILE}")


def fetch(cid, key, audit=False):
    """增量拉取：知识库条目无修改时间字段，按 media_id 做集合 diff——
    只下载从未抓取过的条目（同名文件被替换时 media_id 会变，自然触发重抓）。
    残余风险：文件被原地编辑且 media_id/标题/路径都不变时列表层无信号，
    用 audit=True 定期全量下载比对内容哈希兜底。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    changed = 0
    processed = 0
    for mid, meta in state["items"].items():
        processed += 1
        if processed % 20 == 0:
            save_state(state)  # 周期落盘，进程被杀最多丢最近 20 条状态
        if meta.get("fetch_status") == "missing_in_latest_inventory":
            continue
        if not audit and meta.get("sha256"):
            continue  # 已抓取且身份未变，跳过（真增量）
        info = call("openapi/wiki/v1/get_media_info", {"media_id": mid}, cid, key)
        data = (info or {}).get("data") or {}
        if data.get("media_type") == 11:
            meta["fetch_status"] = "note_type_use_note_api"
            continue
        if data.get("media_type") in (12, 16):
            meta["fetch_status"] = "no_content_api_ai_session_or_video"
            continue
        url_info = data.get("url_info") or {}
        url = url_info.get("url")
        if not url:
            meta["fetch_status"] = "no_url_client_only"
            continue
        req = urllib.request.Request(url, headers=url_info.get("headers") or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                blob = resp.read()
        except Exception as exc:  # 记录失败，不中断全量
            meta["fetch_status"] = f"error: {exc}"[:200]
            continue
        digest = hashlib.sha256(blob).hexdigest()
        if meta.get("sha256") == digest:
            meta["fetch_status"] = "unchanged"
            continue
        (RAW_DIR / f"{mid}.bin").write_bytes(blob)
        meta.update({"sha256": digest, "fetch_status": "fetched",
                     "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        changed += 1
        time.sleep(0.3)
    save_state(state)
    print(f"本次新增/变化 {changed} 个条目，原文存入 {RAW_DIR}")


def reconcile():
    """进程中断恢复：fetch 的状态在结束时才落盘，中断会丢映射。
    原文文件名即条目 key，据此重算 sha256 并回填状态，使已下载原文不被重复抓取。"""
    state = load_state()
    n = 0
    for mid, meta in state["items"].items():
        f = RAW_DIR / f"{mid}.bin"
        if f.exists() and not meta.get("sha256"):
            meta["sha256"] = hashlib.sha256(f.read_bytes()).hexdigest()
            meta["fetch_status"] = "fetched"
            meta["fetched_at"] = "reconciled-from-disk"
            n += 1
    save_state(state)
    print(f"已据磁盘原文回填 {n} 条状态")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "probe":
        probe()
        return
    if cmd == "reconcile":
        reconcile()
        return
    cid, key = creds()
    if not cid or not key:
        print("缺少凭证：请设置 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY（值不打印）",
              file=sys.stderr)
        sys.exit(2)
    {"discover": lambda: discover(cid, key),
     "inventory": lambda: inventory(cid, key),
     "fetch": lambda: fetch(cid, key)}.get(
        cmd, lambda: print(__doc__))()


if __name__ == "__main__":
    main()

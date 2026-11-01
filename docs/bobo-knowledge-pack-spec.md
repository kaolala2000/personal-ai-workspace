# BoBo Knowledge Pack 规范 v1（Muse ⇄ BoBo OS 交换格式）

## 目的

Muse 侧业务知识系统与 ChatGPT BoBo OS 之间同步知识的统一交换格式。
设计原则：稳定 ID、增量更新、来源可追溯、状态不丢失（假设不得变事实）。

## 文件

| 文件 | 内容 |
|---|---|
| `manifest.json` | 包版本、生成时间、记录计数、本次移除的 tombstone 列表、ID 规则 |
| `records.jsonl` | 全部记录，一行一条 JSON（同步的主数据） |
| `md/<主题>.md` | 按主题拆分的 Markdown 全文，供人工阅读与 ChatGPT 项目导入 |
| `CHANGELOG.md` | 每次构建的新增/更新/移除清单 |

## 记录字段

- `id`：稳定主键，只由来源身份决定，内容变化不改 ID
  - 飞书文档 `fs-<obj_token>`；ima 条目 `ima-<item_key>`
  - 知识卡片 `card-fs-<obj_token>`（与原文经 links 关联）
  - 闭环记录 `loop-<记录ID>`（如 loop-DEC-20261101-001）
- `kind`：source_doc / card / synthesis / fact / case / decision / sop / hypothesis / method
- `title` / `theme` / `market` / `status`
- `source`：{system: feishu|ima|claude-loop, source_id, url, kb/origin}
- `content`：正文文本；二进制原文（PDF 等）为 null，改用 `content_path` + `content_sha256`
- `key_facts`：卡片提炼的核心结论；`decision_logic`：决策/判断逻辑原文或提炼
- `pending_verification` + `verify_by`：待验证事项标记与到期日
- `links`：关联记录 ID 列表
- `version`：同一 ID 内容变化时 +1，单调递增
- `content_hash`：规范化内容的 SHA256，对端用它判断是否需要更新
- `updated_at`：该记录最后变化时间

## 同步协议（双向，后续实现）

1. 交换单位 = 记录（id 为主键）；发送方只发自上次同步以来 added/updated/removed。
2. 接收方按 id 合并：hash 相同跳过；version 高者胜；同 version 不同 hash = 冲突，
   保留双方并标记 conflict，等待用户裁决（不得静默覆盖决策与假设状态）。
3. `status` 字段受保护：hypothesis 的 pending_verification 只能经验证流程变更，
   同步不得把假设升级为事实。
4. 删除走 tombstone（manifest.removed_this_build），对端标记失效而非物理删除。

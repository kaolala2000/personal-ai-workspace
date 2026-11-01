# Personal AI Workspace

波波的个人 AI 工作系统代码仓（私有）。存放程序、配置模板、工作流和非敏感技术文档，
供 Muse 与 ChatGPT 侧的 BoBo OS 长期协作使用。

## 目录结构

| 目录 | 用途 |
|---|---|
| `scripts/` | 同步与自动化程序：飞书/ima 抓取、BoBo Knowledge Pack 构建、知识进化闭环 |
| `workflows/` | 工作流与协议文档，如知识进化闭环协议 |
| `prompts/` | 可复用的提示词模板 |
| `docs/` | 技术规范与验证记录，如 BoBo Knowledge Pack v1 规范 |

## Muse × BoBo OS 知识同步

- 交换格式：BoBo Knowledge Pack v1，规范见 `docs/bobo-knowledge-pack-spec.md`，
  构建器为 `scripts/build_pack.py`（增量构建：稳定 ID + content_hash 判变化 +
  version 单调递增 + tombstone 标记删除；冲突保留双方，假设状态不得被同步升级为事实）。
- 知识包数据本身通过 Google Drive 的「BoBo OS Sync」文件夹交换，**不**经过本仓库。
- 本仓库只保存实现上述同步所需的程序与工作流文档。

## 入库红线（重要）

本仓库**不得**上传：

- 飞书、ima 的原始业务数据、知识卡片正文、同步状态文件（如 `state_*.json`）
- 任何密钥、API Key、Token、账号凭证（程序只通过环境变量或标准输入读取凭证）
- 个人隐私资料

根目录 `.gitignore` 已预置对上述文件的拦截规则，新增同步程序时请保持同一原则：
代码入库，数据与凭证不入库。

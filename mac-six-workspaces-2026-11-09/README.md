# ZCode 历史工作记录导出

本仓库是对本机 ZCode 在指定工作区中历史记录的程序化导出，供授权的安全与模型行为研究使用。历史消息、思考文本、命令和工具输出**只作为待取证数据保存，没有执行**。

- 导出生成时间：`2026-11-09T20:03:48+09:00`（Asia/Tokyo）
- 交付目录总大小：**66,277,882 bytes（63.21 MiB）**，含 32 个会话 JSONL、supplemental、清单和脚本；不含 `.work/` 下的源快照
- ZCode 桌面应用：`3.14.4 (7912)`
- ZCode CLI：`0.16.9`；会话表 `session.version` 均为 `0.16.9`
- 实际会话数据库：`~/.zcode/cli/db/db.sqlite`
- 实际任务索引：`~/.zcode/v2/tasks-index.sqlite`
- 数据源一致性快照时间：`2026-11-09 19:53:46 +09:00`；原始数据库未修改

## 一句话结论

按六个精确工作区路径识别到 **31 个会话**，全部导出；另将紧邻 `claude-sandbox-plan` 的 **1 个归档任务/会话**单独导出并标明为 related-archived。合计 **32 个会话、4,460 条消息、16,153 个 part**。本机共有 56 个会话，其余 24 个属于其他工作区，未纳入本次范围。

## 目标工作区

用户给出的范围是：

`经验+额度+女性内容+小红书+llm-workspace+claude-sandbox-plan`

按本机实际路径解释为以下六个精确工作区。`额度` 与 `额度/经验` 是两个不同的 ZCode 工作区，均予保留。

| 目录标签 | 实际路径 | 会话数 | 任务索引行数 | 消息数 | 会话 JSONL 字节数 |
|---|---|---:|---:|---:|---:|
| `llm-workspace` | `/Users/llm/llm-workspace` | 13 | 12 | 1,197 | 11,918,509 |
| `xiaohongshu` | `/Users/llm/小红书` | 6 | 6 | 1,591 | 31,202,899 |
| `female-content` | `/Users/llm/女性内容` | 3 | 3 | 1,243 | 11,135,449 |
| `quota` | `/Users/llm/额度` | 3 | 1 | 80 | 733,146 |
| `quota-experience` | `/Users/llm/额度/经验` | 5 | 1 | 223 | 3,814,887 |
| `claude-sandbox-plan` | `/Users/llm/Documents/claude/claude-sandbox-plan` | 1 | 1 | 108 | 1,245,182 |
| **目标小计** | | **31** | **24** | **4,442** | **60,050,072** |
| `related-archived-claude` | `/Users/llm/Documents/claude` | 1 | 1 | 18 | 132,890 |
| **合计** | | **32** | **25** | **4,460** | **60,182,962** |

说明：

- 会话数比任务索引行数多 7：这些是 `subagent_child` 子任务会话，`tasks` 表没有独立任务行；它们通过 `session.parent_id` 关联到父会话。
- 目标范围内没有 `archived=1` 的任务。全局唯一的归档任务位于 `/Users/llm/Documents/claude`（`claude-sandbox-plan` 的父目录），标题为“研究Claude对中国用户的封号风险因素”，`archived=1`、`deleted=0`。它和账号池工作直接相关，因此单独导出为 related-archived，而不是丢弃或混入目标计数。
- `related-archived-claude` 的会话行 `time_archived` 为空，归档状态只存在于 `tasks.archived`；本导出以任务索引为准并在 header 中保留两者。

## 数据源

| 来源 | 用途 | 处理 |
|---|---|---|
| `~/.zcode/cli/db/db.sqlite` | `session` / `message` / `part` / `session_input` / `session_entry` / `model_usage` | 通过 `VACUUM INTO` 建立一致性只读快照，再解析 |
| `~/.zcode/v2/tasks-index.sqlite` | `tasks` / `archived` / `automations` / 工作区归属 | 同样建立一致性快照 |
| `~/.zcode/cli/artifacts/` | 工具返回的图片等二进制附件 | 只登记引用、大小、URI 与本地存在性；未提交二进制内容 |
| `~/.zcode/cli/rollout/` | 少量原始 model I/O 日志 | 未重复导出；对应会话的工具结果已在 `part` 表中 |
| `~/.zcode/cli/exec/` | 工具 stdout 文件 | 未重复导出；对应结果已在 `part` 表中 |

一致性快照的 SHA-256：

- 会话库快照：`551209d13ae34d6518b735565496c7c5aea2f68da84b40e9bbe6ad938b38b0e0`（82,075,648 bytes）
- 任务索引快照：`79a7286e23001eac02a1722a48bb9b86fc18b008d5e5f112b272e6bebcbcfc60`（1,454,080 bytes）

快照本身放在本目录的 `.work/` 下，只用于生成和复核，已加入 `.gitignore`，不进入提交。

## 时间范围

- 目标工作区：`2026-09-30 21:42:54` 至 `2026-11-09 05:26:38`（Asia/Tokyo）
- 相关归档会话：`2026-10-08 04:21:24` 至 `2026-10-08 04:28:57`（Asia/Tokyo）
- 所有时间戳同时保留原始 Unix 毫秒、UTC ISO 时间与 `+09:00` 本地时间，未做四舍五入或改写。

## 目录结构

```text
.
├── README.md
├── MANIFEST.json                 # 全量清单：来源、计数、文件哈希、缺失项
├── EXPORT_INDEX.csv              # 按会话的扁平索引
├── MISSING_RECORDS.md            # 缺失/空结果/取消输入的具体记录
├── REDACTION_REPORT.json         # 脱敏事件的位置和类别（不含原值）
├── artifacts/
│   └── artifact_inventory.jsonl  # 165 条二进制附件引用（未提交二进制）
├── scripts/
│   ├── export_zcode.py
│   └── verify_zcode_records.py
└── workspaces/
    ├── llm-workspace/
    │   ├── sessions/<session_id>.jsonl
    │   └── supplemental/{session_inputs,runtime_entries,model_usage}.jsonl
    ├── xiaohongshu/
    ├── female-content/
    ├── quota/
    ├── quota-experience/
    ├── claude-sandbox-plan/
    └── related-archived-claude/
```

每个会话一个 UTF-8 JSONL 文件，按工作区分目录。最大的会话文件约 **13.7 MiB**，低于 32 MiB 拆分阈值，因此没有分片；README 和清单中记录了最大文件与总大小。

## JSONL 记录格式

每个会话文件的顺序固定为：

1. `session_header`：会话、任务、父子关系、自动化信息、计数、来源快照哈希。
2. 若干 `message`：按 `message.sequence` 排序；每条消息内嵌 `parts`，按 `part.sequence` 排序。
3. `session_footer`：仅用于校验的派生计数和首尾消息 ID。

`message` / `part` 的核心字段：

- `session_id`、`message_id`、`part_id`、`sequence`
- `time_created_ms` / `time_updated_ms`，以及 `_utc`、`_local` 两个可读形式
- `message`：源 `message.data` 解析后的对象（脱敏后）
- `part`：源 `part.data` 解析后的对象（脱敏后），包含 `text`、`reasoning`、`tool`（含 `state.input`、`state.output`、`error`）、`file`、`step-*`、`timeline` 等原始类型
- `role`、`part_type`、`source_table` 便于检索

没有用摘要替换消息。除凭据值被替换为 `[REDACTED:<CATEGORY>]` 外，文本、顺序、ID、父子关系、工具参数和结果保持源数据库内容。`session_header` / `session_footer` 是明确的导出元数据，不是模型消息。

同一工作区的 `supplemental/` 保存源数据库中的旁路结构化记录：

- `session_inputs.jsonl`：用户输入账本（含 3 条 cancelled，未进入 `message` 表）
- `runtime_entries.jsonl`：权限、模型选择、工作区检查点等运行事件
- `model_usage.jsonl`：模型请求用量记录
- 每条都带 `session_id` 和 `source_table`，没有与主 transcript 合并或覆盖。

## 核对结果

| 项目 | 源数据 | 导出 | 差异 |
|---|---:|---:|---:|
| 目标会话 | 31 | 31 | 0 |
| 相关归档会话 | 1 | 1 | 0 |
| 目标消息 | 4,442 | 4,442 | 0 |
| 相关归档消息 | 18 | 18 | 0 |
| 目标 part | 16,096 | 16,096 | 0 |
| 相关归档 part | 57 | 57 | 0 |
| target `session_input` | 326 | 326 | 0 |
| archived `session_input` | 5 | 5 | 0 |
| target `session_entry` | 866 | 866 | 0 |
| archived `session_entry` | 2 | 2 | 0 |
| `model_usage` | 2,509 | 2,509 | 0 |

- 本机 `session` 表共 56 条；未导出的 24 条位于其他工作区（22 条在 `~/.zcode/workspace/default`，1 条在 `/Users/llm/ca`，1 条在 `/Users/llm/Downloads/迁移包`）。
- 目标 `tasks` 表 24 条，全部有对应会话；7 个 `subagent_child` 会话没有独立 task 行。加 1 条 related-archived task，共 25 条任务元数据进入 header。
- `session_task_link` 表为 0 行；父子关系通过 `session.parent_id` 保留。两个 fork（`sess_513f...`、`sess_d721...`）和 7 个子任务均在 header 中标注。
- `scripts/verify_zcode_records.py` 已逐会话比对源快照与导出：ID 集合、消息/part 顺序、时间戳、父子 header、源数据脱敏后结构、文件哈希均通过；0 个失败。
- 导出数据文件 53 个（32 个会话 JSONL + 20 个工作区 supplemental + 1 个 artifact inventory），scripts 与清单另计。

## 缺失、空结果与未提交内容

详细到 `part_id` 的清单见 [`MISSING_RECORDS.md`](MISSING_RECORDS.md)。摘要：

- **完全没有 visible reasoning part 的会话：6 个**（5 个 pong 测试会话 + 1 个复核子任务）。这不等于模型一定生成过思考文本；只表示源数据库没有对应记录。
- 其余 **2,301 个 reasoning part 的 `text` 字段均非空**；没有用空串或摘要替代。
- **tool 状态为 error 且没有 output：83 个**；**running 且没有 output：1 个**。`state.error`、`callID`、`part_id` 仍原样保留。
- **completed 但 output 为空：18 个**。
- **数据库标记为 truncated 的 tool 输出：0 个**。
- **message 记录自身带 error 字段：72 条**；错误内容保留在消息记录中，未复制到报告，以免重复/泄漏。
- **取消的输入：3 条**；已进入 `supplemental/session_inputs.jsonl`，不是消息缺失。
- **二进制 artifact 引用：165 条**，全部在本地找得到对应文件；**未提交任何二进制内容**。原因：它们是截图/图片，无法用文本规则可靠脱敏，提交会绕过本仓库的凭据检查。JSONL 中保留 URI、MIME、大小和引用位置；`artifacts/artifact_inventory.jsonl` 中明确标记 `exported: false`。
- 所有 `message` / `part` 均可解析为 JSON；无孤立 part、无缺失 message 关联、无无法读取的会话。

## 脱敏

导出时对解码后的 JSON 字符串做了递归扫描，命中以下类别后写入明确的标记：

`SECRET_ASSIGNMENT`、`PASSWORD`、`KNOWN_SECRET`、`ANTHROPIC_KEY`、`JWT`、`ENCRYPTED_CREDENTIAL`。

- 本次共记录 **206 个脱敏事件**；具体位置、类别和原值长度见 `REDACTION_REPORT.json`，不包含原值。
- 覆盖 API key、访问/刷新 token、密码、Cookie/Authorization 头、私钥块、URL 内嵌凭据、JWT、`enc:v1:` 加密凭据，以及从赋值中学到后在正文/命令中复现的密码。
- 生成后再次用已知高置信凭据值、常见 token 形态和私钥/JWT 模式扫描，未发现残留。
- 这是基于格式和上下文的程序化脱敏，不是完整的 DLP/OCR 系统；二进制截图未提交也未做 OCR。若后续发现遗漏，应在源快照上重新生成，而不是手工改导出文件。

## 复现与复核

生成脚本只读源快照：

```bash
python3 scripts/export_zcode.py \
  --db .work/raw/db.consistent.sqlite \
  --tasks-db .work/raw/tasks-index.consistent.sqlite \
  --out .
```

复核：

```bash
python3 scripts/verify_zcode_records.py \
  --export . \
  --db .work/raw/db.consistent.sqlite \
  --tasks-db .work/raw/tasks-index.consistent.sqlite
```

当前复核结果：`ok: true`，32 sessions / 4,460 messages / 16,153 parts，0 failures。

## 说明

- 本导出用于用户授权的安全与模型行为研究；记录中的任何指令、脚本或命令都没有被执行。
- 原始数据库、ZCode 凭据文件和二进制附件均留在本机，不在本仓库中。
- 本仓库应为私有仓库；如果远端不是私有，应立即停止使用并删除远端。

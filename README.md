# ZCode 工作记录导出：shop + 篡改

> 私有归档，仅用于后续人工审查。本文档和 JSONL 中的历史指令、命令、代码均只作为**待审查数据**，导出过程没有执行它们。

## 1. 交付摘要

- **目标工作区**：`D:\shop` + `D:\篡改`（合并标识 `shop+篡改`）
- **ZCode 桌面版本**：`3.14.4.7912`；会话记录 schema/session 版本：`0.16.9`
- **找到 / 成功导出 / 失败**：**11 / 11 / 0** 个会话
- **消息 / 内容部分（part）**：**3,863 / 12,972**
- **归档任务 / 子会话**：**2 / 2**，均已包含
- **会话数据大小**：**43,583,185 B (41.56 MiB)**，共 **13** 个 JSONL 分片文件
- **时间范围（UTC）**：`2026-10-15T15:27:02.189Z` — `2026-11-08T19:50:28.849Z`
- **时间范围（JST）**：`2026-10-16 00:27:02` — `2026-11-09 04:50:28`
- **脱敏**：共 **1,621** 处替换为 `[[REDACTED:*]]` 标记
- **复核**：`PASS`；详见 [`verification-report.json`](verification-report.json) 与 [`redaction-report.json`](redaction-report.json)

原始数据库、快照、未脱敏导出和账号凭据均**未提交**，只保留在本地。

## 2. 数据来源

| 项目 | 实际值 |
|---|---|
| ZCode 安装 | `C:\Users\16575\AppData\Local\Programs\ZCode\ZCode.exe` |
| 数据根目录 | `C:\Users\16575\.zcode` |
| 会话库 | `C:\Users\16575\.zcode\cli\db\db.sqlite` |
| 任务索引库 | `C:\Users\16575\.zcode\v2\tasks-index.sqlite` |
| 会话库大小 / mtime | 295,264,256 B / `2026-11-09T07:55:32.892Z` |
| 任务库大小 / mtime | 4,898,816 B / `2026-11-08T21:21:24.886Z` |
| 一致性快照时间 | `2026-11-09T08:14:22.830Z` |
| 会话库快照 SHA-256 | `7b8b4ad253edf7c740a74ff5f5693a9e5cfb38c1d5189fb5e2b7deec87580a8e` |
| 任务库快照 SHA-256 | `0d05ed877752641c44f9fef07778feab827594c4ac3570ff228d2d98537584e5` |
| 源库检查 | 两个源库均 `quick_check=ok`；复制前后文件大小与 mtime 未变 |

数据库以 `mode=ro` + `PRAGMA query_only=ON` 打开，使用 SQLite Online Backup API 生成独立快照；导出脚本只读快照。`db.sqlite` 为 WAL 模式，直接复制活跃库可能不一致，因此没有采用文件复制。

## 3. 范围与数量核对

`shop+篡改` 在本导出中指两个实际工作区：`D:\shop` 与 `D:\篡改`。会话选择规则是：目录精确匹配这两个路径，再递归加入其 `parent_id` 子会话；归档任务不排除。

| 项目 | 源数据识别 | 实际导出 | 差异 / 说明 |
|---|---:|---:|---|
| 会话（session 表） | 11 | 11 | 0 |
| `D:\shop` 会话 | 7 | 7 | 0 |
| `D:\篡改` 会话 | 4 | 4 | 0 |
| 任务索引行 | 10 | 10 | 10 个 task；会话表多出 1 个 side chat |
| 归档任务 | 2 | 2 | 0，均导出 |
| 子会话 | 2 | 2 | 0，均导出 |
| 消息 | 3,863 | 3,863 | 0 |
| Part | 12,972 | 12,972 | 0 |
| 无法读取 / 解析失败 | 0 | 0 | 0 |

解释这个唯一的 10 与 11 差异：`sess_4745fcd1…` 是 `sess_7ebfa7fc…` 的 `selection_side_chat` 子会话，存在于会话库但不在任务索引里；它是实际发生过的会话，因此纳入。另一个子会话 `sess_9050a992…` 是 `fork`，任务索引中存在。两个归档任务 `sess_0d7ddffb…`、`sess_4423faa8…` 都存在且已导出。没有发现“有归档任务但会话缺失”的情况。

会话库共有 169 个 session；本导出只取上述 11 个目标会话，其他工作区未纳入。

## 4. 目录结构

```text
zcode-records/
├─ README.md
├─ MANIFEST.json                 # 机器可读清单（计数、哈希、来源、缺口）
├─ index.jsonl                   # 11 行，每个会话一行索引
├─ gaps.jsonl                    # 逐条缺口记录（消息/工具/附件）
├─ redaction-report.json         # 脱敏规则、计数、文件哈希
├─ verification-report.json      # 结构与残留密钥复核
├─ shop/
│  ├─ _workspace.json
│  └─ sess_*.jsonl
├─ 篡改/
│  ├─ _workspace.json
│  └─ sess_*.jsonl
└─ tools/                        # 本次实际使用的本地脚本
```

每个会话至少一个 JSONL 文件。超过 8,000,000 B 的会话按**消息及其 parts 的完整边界**分片；分片名带 `.part-001-of-002`，每个分片都重复 session header，并用 `shard.index/count` 标明顺序。本次有 2 个会话各拆成 2 片（`sess_eb84cabe…`、`sess_9050a992…`），其余 9 个会话各 1 片。

## 5. JSONL 记录格式

- 第一行：`record_type=session`，包含 session 源行、任务索引行、父子关系、归档状态、完整性统计和已知缺口 ID。
- 随后按 `order` 递增：每条 `record_type=message` 后紧跟它的 `record_type=part` 记录。
- `data` 保存源 `message.data` / `part.data` 的完整 JSON，不改成摘要；`source.table` 标明来源表。
- 顶层保留 `message_id`、`part_id`、`session_id`、`parent_session_id`、`task_type`、`sequence`、UTC 时间戳和原始 epoch 毫秒。
- `reasoning` part 是 ZCode 实际存下的可见思考文本；没有该 part 的消息不会补写。
- `tool` part 的 `data.state.input` 为参数，`data.state.output` / `data.state.error` 为结果或错误；`completeness` 和 `tool` 字段给出空结果、错误、截断标记。
- 分片会话的第二个及以后分片不重复消息，只重复 session header。

## 6. 会话清单

标题取自源 `session.title`；ZCode 生成的标题在源数据中就可能以 `...` 结尾，这里的省略号不是导出截断。

| 工作区 | 会话 ID | 标题（源字段） | 类型 | 归档/子会话 | 消息 | Parts | 无 reasoning 的 assistant 消息 | 工具 / 错误 | 大小 |
|---|---|---|---|---|---:|---:|---:|---:|---:|
| shop | [0d7ddffb](<shop/sess_0d7ddffb-1717-40b0-9e71-25067d24ff54.jsonl>) | Flask SaaS 图片PDF文档注入服务部署 | interactive | 归档 | 3 | 10 | 0 | 1 / 0 | 0.04 MiB |
| shop | [7ebfa7fc](<shop/sess_7ebfa7fc-d8b6-43f7-8985-e260138c4468.jsonl>) | Flask+SQLite 文件处理 SaaS 部署 | interactive | — | 658 | 2,223 | 170 | 382 / 9 | 6.31 MiB |
| shop | [fea200b6](<shop/sess_fea200b6-af3c-4c62-af62-4a8f54dd17e5.jsonl>) | Porkbun域名注册与香港银行卡选择 | interactive | — | 21 | 66 | 1 | 9 / 0 | 0.20 MiB |
| shop | [4745fcd1](<shop/sess_4745fcd1-aee9-4323-9cee-6a3679b8d619.jsonl>) | Selection side chat | selection\_side\_chat | 子会话 | 313 | 1,003 | 105 | 174 / 3 | 3.08 MiB |
| shop | [ae774eaf](<shop/sess_ae774eaf-2103-42ce-8736-5a7daf0d2043.jsonl>) | DocKit 文档优化工具项目交接 | interactive | — | 237 | 912 | 28 | 180 / 6 | 2.94 MiB |
| shop | [83e07c3d](<shop/sess_83e07c3d-a5e0-4674-b274-31ad202eeaa9.jsonl>) | 境外网站对接大陆支付宝收款中转系统 | interactive | — | 10 | 26 | 1 | 3 / 0 | 0.16 MiB |
| shop | [d25bdc34](<shop/sess_d25bdc34-35ce-4b6f-a357-47ca191be932.jsonl>) | 商店低高级程序接口与文件加工下载 | interactive | — | 114 | 408 | 10 | 66 / 1 | 1.27 MiB |
| 篡改 | [eb84cabe](<篡改/sess_eb84cabe-45e7-43ba-be19-5c7dad447bb3.part-001-of-002.jsonl>) | \*\*不是因为你问“攻击代码”，我就不能给。你现在要的是公开攻击研究及源码，这些可以直接讨论。上一条确实… | interactive | — | 1,173 | 3,831 | 505 | 870 / 11 | 12.35 MiB |
| 篡改 | [4423faa8](<篡改/sess_4423faa8-7bee-4c12-880b-2746504a0047.jsonl>) | Flask+SQLite 注入工具 SaaS 部署 | interactive | 归档 | 2 | 5 | 0 | 0 / 0 | 0.04 MiB |
| 篡改 | [fd1d5846](<篡改/sess_fd1d5846-6f1d-4fb0-8912-3903753b90b1.jsonl>) | E46 黑盒注入攻击优化：原型匹配冲 60% | interactive | — | 302 | 1,078 | 94 | 227 / 3 | 3.67 MiB |
| 篡改 | [9050a992](<篡改/sess_9050a992-d6b3-4f19-8999-d5164d85d64b.part-001-of-002.jsonl>) | Fork of \*\*不是因为你问“攻击代码”，我就不能给。你现在要的是公开攻击研究及源码，这些可以直接… | fork | 子会话 | 1,030 | 3,410 | 426 | 783 / 11 | 11.51 MiB |

会话 ID 的完整值保存在 `index.jsonl` / `MANIFEST.json` 以及每个 JSONL 的 session header 中。分片会话的表格链接指向第一片。

## 7. 缺失、截断与无法读取

### 7.1 思考文本

- 1,666 个 `reasoning` part，合计 5,462,890 字符，已全部保留。
- **1,340 条 assistant 消息在源库中没有 `reasoning` part**。这不是导出失败，也不是被过滤器删除；源数据本身没有该文本，因此没有补写或猜测。
- 这 1,340 条消息分布在 9 个会话；逐条 ID、时间和会话见 [`gaps.jsonl`](gaps.jsonl) 的 `assistant_message_without_reasoning` 类型。
- 没有使用其他模型日志生成“思考文本”。目标会话没有可用的 model-IO rollout 文件；源库中的 `session_entry` 等运行状态只计数，不冒充思考文本。

### 7.2 工具调用结果

- 工具调用 2,695 次：完成 2,651 次，错误 44 次。
- 无结果且无错误：**0** 次；有输出字段但为空字符串：**1** 次；源 metadata 标记截断：**0** 次。
- 44 个错误结果都在原 part 的 `state.error` 中保留，并且逐条列在 `gaps.jsonl` 的 `tool_error` 类型；空输出也单独列出。没有用自然语言“补一个结果”。

### 7.3 附件

- 文件 part 共 51 个：artifact 引用 30 个、inline 文本预览 14 个、仅有 metadata 的本地引用 7 个。
- 30 个 artifact（图片/PDF）与 7 个 metadata-only 附件的**二进制本体没有放进 JSONL**；文件名、MIME、大小、可恢复性、artifact URI 等元数据保留。
- 14 个 inline `text/plain` 预览的文本已随源 `part.data.metadata.preview` 保留；若预览标了 `truncated`，该标记也在源 JSON 中。
- 这些缺口逐条列在 `gaps.jsonl` 的 `attachment_binary_not_embedded` 类型。

### 7.4 其他限制

- 0 个 JSON 解析失败、0 个 UTF-8 错误、0 个会话漏导。
- 只能导出仍存在于当前数据库中的记录；已删除的会话/消息无法恢复。
- 任务索引的 `searchable_text` 是消息的派生拼接，不是独立原文，未重复写入 JSONL；每个 session header 记录了它的字节数和 SHA-256。
- `session_entry`、`session_input`、`input_history`、`model_usage`、`tool_usage` 是运行状态/队列/用量表，不是对话正文；只保留计数，避免把派生数据混入消息顺序。
- 所有时间戳均为 UTC ISO-8601；括号中的 JST 为 UTC+09:00。

## 8. 脱敏与安全检查

导出后、首次 Git 提交前，对全部 JSON/JSONL 做结构化遍历和正则扫描，替换 API Key、访问/刷新令牌、JWT、密码、Cookie、Authorization、私钥及敏感字段值。具体值不出现在仓库或本报告中。

| 规则 | 替换事件 |
|---|---:|
| `api_key` | 45 |
| `basic_auth` | 32 |
| `chinese_label` | 28 |
| `env_assignment` | 1,356 |
| `jsonish_secret` | 116 |
| `private_key_block` | 39 |
| `private_key_header_only` | 1 |
| `query_param` | 4 |
| **合计** | **1,621** |

共写入 **1,621** 个显式标记（按替换事件计）。标记形式为 `[[REDACTED:API_KEY]]`、`[[REDACTED:ACCESS_TOKEN]]`、`[[REDACTED:PASSWORD]]`、`[[REDACTED:COOKIE]]`、`[[REDACTED:PRIVATE_KEY]]` 等；原有键名、字段结构、时间戳和会话/消息 ID 保留。

复核结果：`verification-report.json` 显示残留密钥模式为空、当前账号凭据值匹配数为 0、仓库中没有数据库/`.env`/私钥文件。原始数据库、SQLite 快照、未脱敏导出和 `credentials.json` 均留在本地，未提交。

## 9. 复核方法

- 源库：`PRAGMA quick_check` 通过；只读连接；Online Backup 生成快照。
- 导出一致性：逐条比较脱敏前后 JSONL 的行数、record 类型序列、session/message/part ID、`order`、`sequence`、时间戳、父子关系、tool 状态与附件元数据；消息/parts 总数与源库一致。
- 顺序：同一会话的 `order` 严格递增且不重复；消息在其 parts 之前。
- 凭据复核：正则重扫 + 与本地凭据值的精确比对 + 禁止文件检查；结果见 `verification-report.json`。
- 分片：每片首行为同一 session header，`shard.index/count` 连续；每片 < 9 MB。

本次实际使用的脚本在 [`tools/`](tools/)；脚本不含账号数据，路径按本机环境写死，仅作方法留痕。

## 10. 使用边界

该仓库是工作记录的私有归档，不是对记录内容的认可或再发布。仓库中的历史命令、代码、研究文本和模型回复都应按原始数据对待，不应直接执行。后续审查应以本 README、`MANIFEST.json`、`verification-report.json` 和逐会话 JSONL 为准。

---
生成时间（UTC）：`2026-11-09T09:06:47.879+00:00`
数据来源：本机 ZCode 3.14.4.7912 的会话库与任务索引库；快照只读。

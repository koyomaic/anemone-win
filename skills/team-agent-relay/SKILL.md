---
name: team-agent-relay
version: 1.4.3
description: 团队远程执行中转服务,经 dws 认证大脑在团队服务器上跑任务(共享工具/知识库/钉钉身份)。用于"在服务器上跑任务、写经营月报、查知识库、用钉钉收发、上传/下载文件(≤10MB)"。能力:提交任务、实时直播进展、续会话、变更核查、文件上传/下载、取消;多公司装多包并存。
---

# 团队中转服务

<!-- PACK:IDENTITY-START -->
本目录是**源模板**（仓库 skills/team-agent-relay，机队本地安装位用）。成员包由各自中转服务器上 `pack.sh` 生成：包名 `team-agent-relay-<dws认证大脑名>`，打包时写入大脑名、服务器地址与 ca.pem——**一个技能包＝一个大脑＝一家公司的中转**。
<!-- PACK:IDENTITY-END -->

所有操作通过本 skill 目录下的 `relay.py`（纯标准库，`python3` 直接跑，输出单行 JSON）。
下文记 `S="python3 <本skill目录>/relay.py"`。

## 安装/更新语义

- 本 skill 以 frontmatter `version` 为准做版本比较（`S version` 可查客户端/服务端版本对照与所代表大脑）
- **不同公司的包目录名不同（含大脑名），天然并存**；同名包安装时目录已存在 ≠ 失败：比对已装 SKILL.md 的 version，**本地旧于新包则直接覆盖安装**（幂等；用户配置在 `~/.team-agent/`，不在 skill 目录内，覆盖不丢配置）
- 版本相同则跳过；本地更新则提示用户确认是否降级
- 服务端 /health 若返回 `min_client_version` 且高于本地版本，`S version` 会给出 `upgrade_needed: true`，此时应提示用户更新本技能

## 连接

<!-- PACK:SERVER-START -->
- 源模板默认 `https://127.0.0.1:8788`（环境服务器本机中转）；成员包打包时已写入所属大脑的服务器，无需手配。各中转服务器的大脑身份以该机 `dws auth status` 为准（.23 现为 白露，.29 现为 新能源经营利润运营大脑，.32 现为 墨源经营利润运营大脑）
<!-- PACK:SERVER-END -->
- **传输安全（1.4.1）**：仅允许 `https://`——`http://` 地址直接拒绝（`INSECURE_TRANSPORT_REFUSED`）；TLS 校验**独占固定**本目录 `ca.pem`（不叠加系统 CA），`ca.pem` 缺失时拒绝发起请求（`CA_PEM_MISSING`，不静默回退系统信任库）。排障可显式 `--insecure`（或 env `TEAM_AGENT_INSECURE=1`）临时放行，日常禁用
- **认证**：token 即**一次性激活码**——首次使用自动核销 token、绑定本设备换回设备凭证（`~/.team-agent/` 内，0600），此后请求用 HMAC 签名（凭证永不上网络、15 分钟时间窗、nonce 防重放）；泄露聊天记录里的已激活 token 无法冒用。旧服务端无激活端点时自动回落 bearer，不阻塞
- 历史默认地址（.23:8787 http、.29:8787 http、.32:8787 http）首次加载自动迁移为对应 https 并回写，无需手工改
- 指向其它主机：`S config <token> --server <url>`
- **多公司并存**：配置为 INI 多节（`~/.team-agent/config`），profile 缺省自动用**本包大脑名**（源模板回落 `default`）；所有命令支持 `--profile <名称>` 覆盖（env `TEAM_AGENT_PROFILE`）。旧版单组平铺配置首次加载自动迁移为 `[default]`

## 何时使用

- 任务需要**服务器共享环境**（统一工具/凭证/网络）：如 dws 钉钉操作、内部系统
- 需要**团队共享知识**：先让远端 agent 查 `shared/knowledge/`
- 产出要**沉淀到团队共享区**（报告、数据、复盘）
- 用户说"派给团队服务器 / 让 Claude 那边跑 / 用共享环境"

纯本地小任务直接本地处理，不走中转。

## 首次使用（仅第一次）

先执行：`S config`
- 返回 `{"configured": true}` → 直接干活，**不要向用户提及配置**
- 返回 `{"configured": false}` → 按此固定流程（**只检查脚本读取的固定位置，不要搜索用户目录其他配置形式**）：
  1. 只发这一条消息（原样，不添加技术细节）：
     > 首次使用团队执行服务，需要你提供一次专属凭证（token，形如 ta_xxx，一次性：激活绑定本机后即失效，他人捡到也无法使用）。如果你还没有，请联系管理员领取后发我；发我后我会自动完成绑定，之后就不用再管了。
  2. 拿到 token 后执行：`S config <用户提供的token>` → `{"saved": true, "activated": true}`（activated=false 表示服务端为旧版，已回落兼容模式，同样可用）
  3. 回用户"已保存，开始处理你的任务"，继续执行

token 只接受用户本人提供，绝不猜测/复用示例值；token 不落日志、不复述给用户。

**多公司用户**：用户在多家公司任职时，**每家公司装各自的包**（包名即大脑名，如 `team-agent-relay-白露`、`team-agent-relay-新能源经营利润运营大脑`，以各服务器 `dws auth status` 为准），技能列表里按名字选对应大脑的包执行；各包配置自动存到大脑名 profile（`~/.team-agent/config` 多节并存，互不覆盖），无需手工指定。用户话里指明公司/大脑时用对应的包；未指明且装了多个包时**问清用哪个大脑，不要猜**。

## 命令

| 场景 | 命令 |
|---|---|
| **一步到位（最常用）**：提交并等完成 | `S run "任务描述" -t "shared/report/市场分析/2026-09"` |
| 仅提交（异步拿 task_id） | `S submit "描述" -t "目录" [--urgent] [--resume N] [--force]` |
| 等某任务到终态 | `S wait 42` |
| 查状态 / 列表 | `S status 42` ／ `S tasks --status running` |
| 实时进展（边跑边向用户转述） | `S stream 42`（逐行 JSON：tool/text/status，终态停止） |
| 改了哪些文件 | `S diff 42` |
| **上传文件到服务器**（任务输入材料） | `S upload ./9月.xlsx data/9月.xlsx` |
| **从服务器下载文件**（产出/结果） | `S download data/report.md ./report.md`（本地路径可省，默认存文件名） |
| 取消 | `S cancel 42` |
| review 核查后确认 | `S confirm 42` |
| **审批高危拦截**（任务停在 `pending_approval`，如钉钉群发被拦） | `S approve 42 --decision approve`（放行续跑）／ `S approve 42 --decision deny`（拒绝置 failed） |
| 版本对照（客户端/服务端/是否需升级） | `S version` |
| 列出全部公司配置 | `S profiles`（token 打码） |

参数说明：
- `--profile`：所有子命令通用，选择公司配置（缺省 `default`）
- `-t`：要动哪些共享目录（防冲突依据，强烈建议填；多个逗号分隔）
- `--urgent`：插队（默认 FIFO，并发上限 8；单任务超 10 分钟自动转后台长任务、分段续跑，长任务最多占 5 个槽）
- `--resume N`：追加要求，续接任务 N 的会话（不满意时用这个，别重开）
- 输出带 `"error": true, "http": 409`：targets 与在途任务重叠 → 把 overlaps（谁/哪个任务/哪些目录）告知用户，确认继续再加 `--force`
- `"http": 401`：token 失效**或连错公司服务器**（token 只在签发它的服务器有效）→ 先 `S version` 看所连服务器与大脑名是否为本包所属，不符则重装对应公司的包或 `S config <token>` 重配；确实失效则请用户找管理员重发。注意：服务端已硬切换设备签名认证，**低于 1.4.0 的旧包一律 401**，须升级技能包（`S version` 的 `upgrade_needed` 会提示）
- `"http": 409`（config/激活时）：token 已被激活使用（一次性）→ 请用户找管理员**补发**新 token（管理员：dashboard 重发或 `POST /users/<name>/token`），已激活的其他设备不受影响

**文件上传/下载**：
- 路径是共享区 `shared/` 内的相对路径（如 `data/9月.xlsx`、`reports/9月/月报.md`）
- **单文件 ≤10MB**（客户端先拦，服务端超限返回 `"http": 413`）；超大文件告知用户拆小或走人工通道
- 典型用法：任务需要输入材料 → 先 `upload` 再 `run "…读取 data/9月.xlsx…"`；任务产出文件 → 完成后 `download` 给用户
- 不要用任务描述里塞 base64 的方式传文件（旧法，已废弃）

## 工作流

1. 理解意图 → 组织清晰的 description（目标、范围、验收、产出位置）
2. `S run`（或 submit + stream 直播）
3. 到 `review`：`S diff` 给用户看改动 → 认可 → `S confirm`
   - 不满意 → `S submit "追加要求" --resume <id>`
4. `pending_approval`（高危命令被服务器侧拦截，**最常见：钉钉群发/多接收人发送**）：
   `S status 42` 看 `error`/`blocked_cmd`（被拦的具体命令）→ 告知用户"某条发送被拦待审批" →
   用户同意后 `S approve 42 --decision approve`（放行续跑）；不要发则 `--decision deny`
5. `conflict`：告知冲突详情（哪两个任务/哪个文件/备份在哪），人工核查
6. `failed`：读 error，调整描述重新提交

## 规则与边界

- 单任务 30 分钟超时；**review 不是终点**，diff 给用户看过并 confirm 才算 done
- 同文件被两任务改动不会静默覆盖：先完成者版本自动备份到 `shared/.archive/bak/conflict/T<任务号>/...bak-用户-HHMM`，后完成者标 conflict
- 共享区约定：`shared/` 即 store 存储根——`data/`（经营数据入站，按 `<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/<senderId>/`）、`report/`（报告出站，按 `<域>/<YYYY-MM 或 YYYY-Www>/`）、`knowledge/`（知识，先查，按 tag 域分）；找文件用 `store q` 不要递归 find，落盘后 `store add` 登记
- wiki 同步范围按本机配置：`shared/knowledge/` 落盘后 `store add --kind knowledge --wiki-tag <域>`，再按本机 `store sync` 配置执行 push；`report/data` 不自动同步 wiki。中鲁本机当前配置为四库范围：`shared/knowledge/口径方法论/{知识库,模型库,案例库,规则库}/`
- **钉钉发送门控**：服务器侧对 dws 外发消息二次确认——1:1 单聊放行；群聊/多接收人/命中敏感对象 → 任务停 `pending_approval`，须 `S approve` 放行。`run`/`wait` 到此即返回（不再挂到超时），把 `blocked_cmd` 告知用户后走 approve。

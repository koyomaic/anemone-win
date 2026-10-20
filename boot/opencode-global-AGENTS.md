# 本机默认语境（relay 机队服务器）

处理"中转服务 / relay / GitHub 项目 / 部署 / 知识库 / 钉钉"等运维话题时，默认采用以下语境，无需用户重复说明。

## GitHub 项目（"检查GitHub项目更新"默认指它）

- 仓库：`koyomaic/business_advisor`（main 分支）＝团队中转服务 relay 的唯一真源
- 本地 git 工作区：`/mnt/vol-eltaah12/finaagent/relay-dev`
- 检查更新：`cd /mnt/vol-eltaah12/finaagent/relay-dev && git fetch origin && git log --oneline HEAD..origin/main`（无输出＝已最新；有输出＝远端有新提交，汇报后等指示）
- 生产运行位：`/mnt/vol-eltaah12/finaagent/relay-service`（符号链接 → `relay-releases/<sha>/relay-service`）
- 部署：`/opt/team/relay-boot/boot.sh`（拉取→预构建→门禁→原子切换；失败自动钉钉预警王洪彬）；GitHub 网络不稳时可从 relay-dev 本地预构建后手动原子切换
- 门禁：在 release 的 relay-service 目录跑 `/mnt/vol-eltaah12/finaagent/.venv/bin/python -m pytest -q -m "not integration and not pg" -x`
- 健康检查：`curl -s http://127.0.0.1:8787/health`（version＝部署 sha，db＝现役数据库类型）；对外 `https://<本机内网IP>:8788`
- 日志：`journalctl -u relay`；部署/预警日志 `/var/log/relay-boot.log`
- 默认只检查/汇报；pull、部署、push、重启服务需用户明确要求

## 关键路径

- 任务工作区：`/mnt/vol-eltaah12/workspace/`（任务约定见其 `AGENTS.md`）
- 共享区：`shared/knowledge/`（知识）、`shared/skills/`（技能）、`shared/secrets/`（凭据，严禁入库/外发/回显）
- 本机配置：`workspace/relay.env`（600，含 RELAY_DB / RELAY_ADMIN_TOKEN 等，严禁泄露）
- 数据库：现役 PostgreSQL（连接串在 relay.env 的 RELAY_DB；类型以 /health 的 db 字段为准）；`workspace/relay.db` 仅为历史 SQLite 快照
- PG 库备份由云管理部门负责，本机不备份（RELAY_BACKUP_SKIP_DB=1）
- 机队安装/体检：`relay-dev/boot/provision.sh`（文档 `boot/INSTALL.md`）

## 知识源优先级

1. jingboWiki 为优先权威源（技能 `jingbowiki-api`，`http://10.200.3.235:18082`）
2. 本地 `shared/knowledge/` 兜底；冲突以 jingboWiki 为准
3. 短期只把 `shared/knowledge/口径方法论/{知识库,模型库,案例库,规则库}/` 的现行版 `.md` 同步到 jingboWiki；四库归档后同任务执行 `store add --kind knowledge --wiki-tag 口径方法论` 与 `store sync push --scope fourlib`（受理即成功、不阻塞等解析），`report/data` 与其他 knowledge 暂不同步

## 钉钉消息样式（默认约定）

- 日常汇报/通知一律用 **Markdown**（`dws chat +dm --content` 即支持），不发纯文本
- 正式通知（部署预警、重要公告、对领导汇报）用 **流式卡片**（`dws chat +messages-send-card`，完成态 flow-status=3）
- 用户明确指定样式时以指定为准

## 铁律

- 绝不给马韵升发任何钉钉消息（userId=1）
- 基础技能（入库 `relay-dev/skills/` 者：jingbowiki-api、钉钉套件 dingtalk-*、dws 等）以 git 为真源；改动须提交回仓库，否则夜间 provision 以仓库版覆盖（旧版备份 `*.bak-provision-*`）
- 内部迁移/版本/地址变化对用户静默，不播报

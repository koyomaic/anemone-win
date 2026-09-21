# im-bridge — 钉钉消息 → relay 任务桥

监听 dws 认证账号收到的钉钉单聊消息（群监听可选，默认关），聚合后提交本机
relay 执行，结果以账号名义回复。默认**安装但不启用**，具备随时启动能力。

## 交互模型

- 白/黑名单过滤后来件 → 已读回标 + 原消息贴「收到」表情（`ack_emoji`）
- 20s 静默（`debounce_sec`）自动聚合提交 relay 任务 → 贴「赞」表情（`submit_emoji`）
  - 发「提交」立即执行；「取消」放弃缓冲或终止在跑任务
- 任务执行中发消息 → 记为追问，本轮结束后 30s（`followup_debounce_sec`）
  以 `resume_from` 续上一任务会话自动提交（上下文延续）
- 门禁透传：任务 `pending_approval`（高危命令拦截）/`review` 时钉钉提示，
  回「确认/拒绝/冲突」即调 relay approve/confirm；无关消息会重发提示（60s 冷却）
- 结果 markdown 回复；超长截断 + 全文以文件附件发送
- 寒暄/纯表情消息（`noise_ignore`）只贴表情不建任务
- 图片/文件消息自动 `download-media` 落盘，本地路径拼入任务描述

## 目录与文件

- 代码位（provision 安装）：`/opt/team/relay-im-bridge/{bridge.py,bridge-cli}`
- 状态位（`BRIDGE_STATE_DIR`，默认 `/mnt/vol-eltaah12/workspace/im-bridge/`）：
  - `bridge.env`（600）：`RELAY_BASE` / `RELAY_DEVICE_ID` / `RELAY_DEVICE_SECRET`
  - `config.json`：白/黑名单与行为参数（模板 `config.example.json`）
  - `state.json` 会话状态机（重启恢复）、`events.ndjson` 原始事件、
    `rounds/` 每轮提交与结果归档、`media/` 附件落盘
- systemd：`relay-im-bridge.service`（模板 `relay-im-bridge.service.template`）
- 启用开关：`touch /etc/relay-im-bridge.enabled`（provision 据此 enable --now；
  无 marker 时保持安装但 disabled）

## 首次启用步骤

1. relay 建用户并激活设备（管理员 token）：
   - `POST /users {"name":"im-bridge"}` → 一次性 token
   - `POST /auth/activate {"token":"ta_...","device_id":"im-bridge-<机号>"}`
     → `device_secret`
2. 写 `bridge.env`（600）：`RELAY_BASE=http://127.0.0.1:8787`、
   `RELAY_DEVICE_ID`、`RELAY_DEVICE_SECRET`
3. 编辑 `config.json`：`personal.mode`（whitelist/blacklist）与名单
   （姓名或 openDingTalkId 均可匹配）
4. `touch /etc/relay-im-bridge.enabled && systemctl enable --now relay-im-bridge`
5. 验证：`journalctl -u relay-im-bridge -f` 出现 `consumer ready`；
   白名单用户发单聊测试

## bridge-cli（本机直提，不经钉钉）

账号本人自发消息不会进个人事件流（平台 self-loop），本人下任务用：

```
/opt/team/relay-im-bridge/bridge-cli "任务描述" [--resume N] [--no-follow]
```

## 依赖与注意

- 依赖本机 dws 登录态（`dws-refresh.timer` 维护）与 relay 服务
- 事件总线目录 `/tmp/dws-event-0`（unit ExecStartPre 自动创建）；
  该路径不能是软链（dws lstat 校验拒绝）
- 与其他 dws 事件消费者（如消息助手类守护进程）共存时需划分人群，
  避免同一消息双回复
- 回复以 dws 认证账号名义发出；黑名单模式=全受理，启用前想清楚范围
- 停服务用 SIGTERM（禁 kill -9，防服务端订阅泄漏）

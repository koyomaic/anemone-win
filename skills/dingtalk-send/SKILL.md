---
name: dingtalk-send
description: 用本机 dws CLI 把文件/消息直接发到钉钉(文件本体发成钉钉文件消息,过程免确认)。用于"发我/发给某人/把文件发过去/发到钉钉/传给XX/把报告发我"。触发词：发我、发给、发到钉钉、传给我
metadata:
  type: tool
  requires:
    bins:
      - dws
---

# 钉钉发送 (DingTalk Send)

收到"发我 / 发给某人 / 把文件发过去 / 发到钉钉 / 传给XX"等指令时,**直接用本机 `dws` CLI 把文件本体发成钉钉文件消息,过程免确认**(发给自己/单个联系人是标准动作,不必反问"发到哪/要不要确认")。

> 首次使用前请先完成 dws 安装、登录与身份配置,见随包 `README.md`。本技能里的"我/本人"一律指**当前登录 dws 的账号**,不写死任何具体人。
>
> ⚠ 这条在**分身/relay 任务场景下会害人**:那时 dws 登录账号是机器人自己,而说"发我"的是另一个人。见下面「铁律」第一条。

## 铁律
- **⚠ 分身/任务场景下本技能不适用——先看这条**：若你是在中转服务(relay)任务里、
  或作为钉钉分身(如「鲁大师」)的 agent 在跑,那么**本机 dws 登录账号就是机器人自己**,
  不是向你提需求的那个人。此时用户说的"发我/发给我"指的是**发起人**,而按下面
  「发给本人」的口径直发会**把文件发给机器人自己**——`query-send-status` 照样返回
  `SUCCESS`,发起人却什么都收不到,你还会误判成"客户端未刷新"。
  (2026-09-25 真实事故:任务 #242/#243 三份文件全部发给了机器人自己,用户自 20:36
  起连续 6 次反馈收不到。)
  **正确做法**:不要自己跑 dws 发送。把文件放在任务工作区,在结果正文里用
  ```send_file``` 围栏块声明路径,由分身代投给发起会话(收件人由系统锁定、改不了)。
  任务描述里若有【投递铁律】段落,以它为准。
- **免确认直发** — 发个人/本人是标准动作,不要反问确认或问发到哪。
  (仅适用于你**就是** dws 登录账号本人的场景,即上一条的例外。)
- **发文件本体**(钉钉文件消息),不发网盘链接、不发文档卡片替代。
- **不碰钉盘** — "发我/发给某人"是纯 IM 发送。**只有用户明确说"传钉盘/上传钉盘"**才用 `dws drive upload`;绝不套用"日程附件→钉盘"规则。
- **清代理** — 发文件前必清 `HTTPS_PROXY/HTTP_PROXY/ALL_PROXY`,否则企业 OSS 上传失败。
- **发富媒体用 `--open-dingtalk-id`**(`--user` 不支持);unionId ≠ openDingTalkId,别拿 unionId 当收件人。

## 收件人解析

### 发给本人
"发我/发给我/给我" = 当前登录账号。**不要在技能里写死 userId**。首次运行时取一次并缓存:

```bash
# 取当前登录账号的 userId(发纯文本/Markdown 用)
dws contact get-self --format json
```

发文件给本人需要本人的 `openDingTalkId`,`get-self` 不返回时用 aisearch 取一次:

```bash
dws aisearch person --keyword "<本人姓名>" --dimension name --format json
```

把取到的 `userId` / `openDingTalkId` 缓存到本技能目录的 `identity.local.json`(该文件**不随包分发**,首次生成),后续直接读缓存:

```json
{ "selfName": "<本人姓名>", "selfUserId": "<userId>", "selfOpenDingTalkId": "<openDingTalkId>" }
```

### 发给别人
```bash
dws aisearch person --keyword "<对方姓名>" --dimension name --format json
```
拿 `userId`(发文本)与 `openDingTalkId`(发文件)。aisearch 现已直接返回 `openDingTalkId`,发富媒体所需的 open id 一步到位,不必再走"发探测消息→list-direct"。同名多人 → **停止并让用户消歧,不默认选第一个**。

## 发文件(一步直发)
⚠️ `dws chat file upload` 已下线。现在 `chat message send --msg-type file` 的 `--file-path` 直接给**本地文件路径**,dws 内部自动上传,无需 dentryId/spaceId/file-size。发富媒体必须用 `--open-dingtalk-id`(`--user` 不支持)。

```bash
unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy
MSYS_NO_PATHCONV=1 dws chat message send \
  --open-dingtalk-id <收件人openDingTalkId> \
  --msg-type file \
  --file-path "<本地文件绝对路径,如 C:/Users/你/Downloads/文件.zip>" \
  --file-name "文件.zip" \
  --title "文件.zip" \
  --format json
```
**⚠️ v1.0.61 起 `chat message send` 改为异步**:返回外层信封 `{ok:true, outcome:"pending", data:{...}, meta:{operation:{...}}}`,真正的成功标记与 openTaskId 挪到 **`data.success:true` + `data.result.openTaskId`**(顶层不再有 `success`)。消息仍会真实送达;要确认落地可跟 `meta.operation.next_command`(`dws chat message query-send-status --open-task-id <id>` → `sendStatus:SUCCESS`)。判成功请读 `.data.success` 或 `.ok`,勿再判顶层 `.success`。多文件重复本条即可。

## 发纯文本 / Markdown(--user 直接支持,最简单)
```bash
MSYS_NO_PATHCONV=1 dws chat message send --user <收件人userId> --title "标题" --text "正文(支持Markdown)" --format json
```
`--title` 必填。**v1.0.61 起同为异步信封**(见上):成功读 `.data.success:true`/`.ok:true`,顶层不再有 `success`/`errorCode`。

## 撤回(发错时)
```bash
MSYS_NO_PATHCONV=1 dws chat message recall --conversation-id "<openConversationId>" --msg-id "<openMessageId>" --format json
```
会话id/消息id 从 `chat message list-direct` 取。

## 可选加速:快捷脚本
如果你自己封装了发送快捷脚本(自动清代理、缓存联系人),可优先用它;**没有也不影响**——上面的 `dws chat message send` 人人可用。快捷脚本仅是可选优化,不是必需依赖。

## 易错点(踩过的坑)
- **`dws chat file upload` 已下线**,发文件一律用 `chat message send --msg-type file --file-path <本地路径>` 一步直发。旧笔记里"先 upload 拿 dentryId"的3步流程作废。
- `dws drive upload` 是钉盘(alidocs体系),返回的 fileId 是字符串,与 IM 发文件无关,别混用。
- unionId **≠** openDingTalkId,拿 unionId 当 receiverUid 会报"receiverUid不能为空"。
- openDingTalkId 来源:优先 `aisearch person`(现已返回);仍拿不到时才回退 `chat message list-direct` 的 `senderOpenDingTalkId`。`contact user get`/`get-self` 不返回。
- 发文件前不清代理,企业 OSS 上传可能失败。

相关:[[dingtalk-chat]](复杂发送/群发底座)、[[dingtalk-chat-digest]](报告发本人)、区别于 [[dingtalk-calendar]](建日程才用钉盘附件)。

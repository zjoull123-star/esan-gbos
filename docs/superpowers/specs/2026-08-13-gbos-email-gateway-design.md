# GBOS 独立邮件网关设计

**状态：** 已批准，待实施计划

**日期：** 2026-08-13

**决策：** 在现有 Observer 与 Frappe CRM 之间建立供应商无关的独立
Email Gateway 业务域。企业微信应用邮箱 API 与 IMAP/SMTP 都只是可替换适配器；
Observer 仍是收件、原始证据、checkpoint 和 quarantine 的唯一权威，Frappe 的
`GBOS External Identity` 仍是外部身份的唯一权威。邮箱角色、独立入箱、人工会话
合并、规则分配、SLA、回复草稿和经正式命令批准后的发送由网关统一治理。

## 1. 背景与现状

当前 Email 试点链路为：

```text
IMAP → RawDelivery → CAS → Observer → 身份解析 → AI/Context
     → GBOS Informal Observation → 沟通观察 PWA
```

该链路已经具备不可变原始证据、站点隔离、HMAC 外部身份、人工身份审核、AI
摘要、Context 草稿、RLS、保留策略和 kill switch，但它仍是“一封邮件一条观察”：

- `Message-ID` 在 IMAP 层被解析后未传入 Observer 业务对象；
- 未保留 `Subject`、`In-Reply-To`、`References` 和地址角色的治理投影；
- 不存在跨邮箱去重、线程或 Conversation；
- `team_ref` 已生效，但 `agent_task_type` 尚未形成分配或任务；
- 不存在 Inbox、SLA、认领、改派、回复草稿、发件 outbox 或投递回执；
- 当前运行时强制 `external_send=false`；
- 当前 WeCom connector 是会话内容存档 SDK 接缝，不是企业微信应用邮箱 API。

企业微信当前邮件接口目录显示：获得邮件权限的应用会分配应用邮箱，并支持邮件
回调、收件、发件和应用邮箱账号管理。收件事件 `app_email_change / receive_email`
仅给出应用邮箱当前新邮件数，因此可靠接入仍需要回调触发、列表/正文拉取和定时
补偿三部分。[邮件概述](https://qiyeweixin.apifox.cn/doc-1781542)、
[邮件回调](https://qiyeweixin.apifox.cn/doc-1782135)。正式实现必须再次对照企业微信
官方文档冻结具体请求、鉴权、限流和错误合同；上述索引不是运行证据。

## 2. 目标

- 在一个独立配置台中管理每个接入邮箱的 provider、地址、业务角色、默认团队、
  账号负责人、优先级、收发方向、状态和凭据引用。
- 允许多个邮箱并列标记为主入口；不设置全局或每队列唯一约束。
- 让企业微信应用邮箱成为面向新业务邮件的推荐主入口，同时保留 IMAP/SMTP 作为
  历史迁移、选择性归档和兼容适配器。
- 任何新地址首次出现均保持未知；员工和客户身份必须通过现有 Frappe 外部身份
  审核流人工确认，Gateway 只消费其版本化投影。
- 让每个收件邮箱先产生独立 Inbox Item；跨邮箱重复或同线程只生成建议，必须人工
  合并。
- 先绑定邮箱默认团队，再通过闭合 Frappe 路由权威解析已确认客户负责人，最后应用
  同团队显式规则；无法唯一判断时进入待认领。
- AI 只做摘要、分类、字段、线程、身份和负责人建议，不能确认身份、改变归属、
  合并会话或发送邮件。
- 第一版支持负责人单人批准后的受控外发；后台任务和模型不能直接发送。
- 复用现有 CAS、Observer、身份安全、RLS、Context、审计、保留和紧急停止能力。

## 3. 非目标

- 不同步全员个人邮箱。
- 不把邮件群组当成 CRM Inbox。
- 不让每封邮件自动创建 Lead、Customer、Opportunity、Order 或 Issue。
- 不自动确认员工、客户或公司身份。
- 不按公司域名自动认定客户，不因抄送某员工而改变客户负责人。
- 不自动合并跨邮箱邮件或 provider thread。
- 不允许 AI 自动回复、承诺价格、折扣、库存、交期、合同或付款条件。
- 第一版不增加供应商身份类型；业务身份闭集为员工、客户和未知。
- 第一版不要求 Eric 个人邮箱 IMAP 成功登录作为核心网关完成条件。
- 本设计不授权创建企业微信应用、修改生产邮箱、读取真实邮件或发送真实邮件。

## 4. 架构

```text
企业微信应用邮箱 API       IMAP / SMTP       未来 Provider
          \                    |                 /
           +-------- Inbound Adapter -----------+
                             |
                Observer Intake / CAS / Cursor
                             |
             Durable Email Message Publication
                             |
                       Email Gateway
        +--------------------+--------------------+
        |                    |                    |
  Mailbox Registry    Inbox / Conversation   Draft / Send Outbox
  Identity Projection Routing / SLA          Receipt / Audit
        |                    |                    |
        +--------------- Frappe CRM --------------+
                 External Identity / Route Authority
                 Review Case / ApprovedCommand
                             |
                   GBOS PWA 统一收件箱

ApprovedCommand → Action Guard → Send Outbox → Outbound Adapter
```

### 4.1 Provider Adapter

Provider Adapter 只负责供应商协议，不拥有 CRM 业务状态。入站 Adapter 运行在受治理
connector intake 边界内，把结果交给 Observer 的单一写入路径；出站 Adapter 只接受已
验证 ApprovedCommand 所产生的 Send Outbox claim。统一能力为：

- 验证并解密接收信号；
- 以 provider cursor 拉取变化；
- 以稳定 provider message ID 拉取完整 RFC822/EML；
- 向 Observer 返回 provider 元数据、附件和稳定 cursor 候选；
- 发送一份已批准、已冻结的 MIME 请求；
- 在能力允许时按 client request ID 对账发送结果；
- 将 429、鉴权失败、撤权、5xx、超时和永久错误映射为闭合安全错误码。

企业微信适配器负责 access token、回调合同、分页、限流与应用邮箱 API；IMAP/SMTP
适配器负责 TLS、UID/UIDVALIDITY、`BODY.PEEK`、SMTP 投递和兼容迁移。只有 Observer
可以接受 delivery、推进 checkpoint/cursor 或决定 quarantine；Adapter 和 Gateway 都
不能自行推进。核心网关不得依赖任何供应商字段。

### 4.2 Email Gateway

Email Gateway 是独立服务域，可与现有服务共享 PostgreSQL 集群，但必须拥有独立
schema/migration ledger、应用角色、FORCE RLS 和最小 grants。它不保存原始 EML、
附件、connector checkpoint、provider cursor、quarantine 或明文 secret；原始字节和
全部 intake 状态留在 Observer，凭据只保存 logical `credential_ref`。

网关负责：

- 邮箱业务配置和配置 revision，并向 Observer 发布受控 connector 配置投影；
- 幂等消费 Observer 的 durable Email Message Publication；
- Inbox Item、Conversation 和业务状态；
- Frappe 外部身份审核入口及其只读、版本化投影；
- 线程/重复建议及人工接受/拒绝；
- 路由、认领、改派和 SLA；
- Reply Draft、Review Case、ApprovedCommand 绑定、Send Outbox、发送回执和对账；
- 低基数指标、审计和阶段开关。

### 4.3 Observer 与 Frappe

- Observer/CAS 继续唯一拥有 connector intake、checkpoint/cursor、delivery、原始
  证据、quarantine、规范化参与者、不透明身份、AI 投影和安全读取。
- Frappe 继续是 User、Contact、Party、团队、客户负责人、`GBOS External Identity`
  及 CRM 业务对象的唯一权威源。
- Gateway 拥有可变邮件工作流，只保存外部身份的 `mapping_ref`、revision、状态和必要
  的同团队投影；它不能直接确认、拒绝、撤销或改绑外部身份。
- 员工地址通过现有外部身份映射到 User；客户地址通过现有外部身份映射到 Party。
  可选 Contact 只是该 Party 下的人工作业关联，不授予访问或所有权，也不成为第二个
  地址身份权威。
- Gateway 发起的身份确认仍进入 Frappe Review Case/受控命令；每次路由、读取授权和
  发送都要向 Frappe 验证映射与目标当前有效、启用且属于允许团队。
- User、Contact、Party、团队或负责人变更必须同步使相关映射/批准失效；通知失败
  时写操作 fail closed，读取与发送仍执行实时资格校验。
- Gateway 向 Observer 发布 provider-neutral 的邮件事实；Observer 不承担可变 Inbox
  工作流。

## 5. 数据模型

### 5.1 Gateway Mailbox

每个邮箱连接一条版本化记录：

```text
mailbox_id
site_id
address_display             # 受限显示，不进入日志/指标
provider                    # wecom_app_mail | imap_smtp | future adapter
provider_account_ref        # 非秘密稳定账号引用
observer_connector_instance_ref
entry_role                  # primary | workflow | migration | selective_archive
business_purpose
default_team_ref
account_owner_user_ref
priority
inbound_enabled
outbound_enabled
credential_ref
status                      # draft | active | paused | revoked | error
config_revision
observer_config_projection_receipt
created_by / updated_by / timestamps
```

多个邮箱可以同时使用 `entry_role=primary`。角色用于路由和 UI，不构成唯一约束。
任一安全相关配置变更必须 CAS 更新 revision，使旧 worker claim 和未发送批准失效。

### 5.2 Observer 收件账本与 Gateway Publication

现有 Observer delivery/checkpoint/quarantine 是唯一收件账本，不在 Gateway 复制。
Gateway Mailbox 必须绑定一个受控 `observer_connector_instance_ref` 和 config revision。
Observer 在原始证据落盘、delivery 接受及规范化成功后，通过同一受治理数据面写入
durable Email Message Publication outbox：

```text
publication_id
site_id / mailbox_id / mailbox_config_revision
observer_connector_instance_ref / observer_delivery_ref
received_at
raw_eml_evidence_ref
opaque participant refs with exact address roles
Message-ID / In-Reply-To / References digests
subject projection or digest
publication_revision / idempotency_key
```

唯一性至少绑定 `(site_id, mailbox_id, observer_delivery_ref)`。顺序固定为：CAS 原子
持久化 → Observer 接受 delivery 与 durable publication outbox → Observer 按既有规则
推进 checkpoint/cursor。Gateway 以 `publication_id` 幂等消费并保存 receipt；Gateway
不可回写 provider cursor，也不可改变 Observer quarantine。若 Gateway 暂停，Observer
publication 可以积压但不得丢失或重新抓取已接受邮件。

### 5.3 Channel Message 与 Inbox Item

每个邮箱投递先生成独立 Inbox Item，即使同一邮件同时到达多个已接入邮箱。

Channel Message 保存受控事实和证据引用：

```text
message_id / direction / timestamps
opaque from / to / cc / bcc participant refs with exact roles
subject projection or digest
Message-ID / In-Reply-To / References digests
body / attachment evidence refs
provider and delivery references
```

Inbox Item 保存可变运营状态：

```text
inbox_item_id / mailbox_id / message_id
team_ref / assignee_user_ref
priority / sla_due_at
state
conversation_ref? / business links
revision / audit timestamps
```

初始状态为受限的 `identity_pending` 或 `unassigned`。Inbox Item 可独立关闭、分配或
关联 Conversation，不会因为 provider thread header 自动合并。

### 5.4 Mail Address Identity 投影与审核入口

业务身份闭集：

- `employee` → `GBOS External Identity.identity_type=User`；
- `customer` → `GBOS External Identity.identity_type=Party`；
- `unknown` → 没有有效映射，而不是一个可授权的映射类型。

Gateway 只持久化 Frappe 权威映射的受限投影：

```text
site_id / purpose / opaque_address_ref
external_identity_ref
external_identity_revision
identity_type                # User | Party
team_ref
status                       # confirmed | revoked
projection_receipt / observed_at
```

不变量：

- 唯一性、revision、审核状态、撤销和改绑由 `GBOS External Identity` 及现有 Observer
  identity resolution contract 执行；Gateway 不建立第二套唯一索引或 revision 序列；
- 原始地址使用站点/用途隔离 HMAC，不进入普通索引、日志、指标或 repr；
- 确认时由 Frappe 受控命令从受限证据读取地址，并与当前权威 User 邮箱或 Party Profile
  所关联 Contact 邮箱精确规范化匹配；不匹配时先修正 Frappe，再重新提交；
- 员工映射审核策略只允许 GBOS Admin 或 Integration Admin；客户映射审核策略只允许
  同团队 Sales Manager 或 Reviewer。两者都通过 Review Case/命令执行器完成，不通过
  DocType 通用写权限完成；
- 撤销、改绑、Rejected 后重提、目标资格漂移和即时 denial 沿用现有外部身份状态机，
  Gateway 只按 resolution revision 更新投影；
- 未确认、已撤销、目标失效、团队漂移或 revision 过期均不得授予访问、客户归属或
  发送资格。

阶段 1 必须先扩展现有身份策略合同、权限矩阵和负向测试以表达上述邮件审核角色；在
这些变更生效前，Gateway 只能显示 `unknown`，不得自建兼容写路径。

### 5.5 Thread / Duplicate Suggestion 与 Conversation

系统可以根据 provider headers、Message-ID 家族、参与者、时间窗口和内容摘要生成
建议，但建议绝不改变业务状态。

```text
suggestion_id
candidate inbox/conversation refs
signals / confidence
status                       # proposed | accepted | rejected | expired
reviewed_by / reviewed_at
```

授权人员接受后才创建或合并 Channel Conversation。合并需保留源 Inbox Item、邮箱、
消息和权限域；跨团队或不兼容业务权限默认拒绝。拆分同样创建新 revision 并保留审计。

Conversation 保存：

```text
conversation_id / team_ref
customer_contact_ref? / party_ref?
owner_user_ref?
lifecycle_state
first_message_at / last_message_at
revision
```

### 5.6 Reply Draft、Send Outbox 与回执

Reply Draft 是可编辑、无外部效果的对象。发送审批采用负责人单批：当前
`assignee_user_ref` 可以成为该次发送 Review Case 的显式 delegated approver，但
Sales User 不能直接写 Send Outbox。价格、折扣、库存、交期、合同和付款等内容显示
强提醒并记录风险标签，但第一版不强制第二位 Reviewer。

批准必须由现有命令执行器签发 ApprovedCommand，并经 Action Guard 按专用
`email_send_owner_v1` 策略复核 actor、委派、site、team、purpose、权限和以下冻结项：

- 草稿 revision 与内容 digest；
- 发件 mailbox config revision；
- Conversation/Inbox Item revision；
- 所有收件人身份 mapping revision；
- 当前负责人、团队、业务目的和证据引用。

只有验证 ApprovedCommand 后，由命令执行器事务内创建的 Send Outbox 才可以调用
Provider Adapter。任何字段漂移都使批准失效并要求重新审批。阶段 4 之前必须先更新
ADR-0004、权限矩阵、ApprovedCommand/Action Guard 合同与负向测试；未完成时
`outbound_enabled` 必须保持 false。

```text
send_id / site_id / mailbox_id
sender / normalized recipient envelope
recipient_mapping_refs_and_revisions
mailbox_config_revision
draft / conversation / inbox revisions
final_mime_evidence_ref / content_digest
approver / review_case_ref / approved_command_ref
policy_version / approved_at
idempotency_key / stable client_request_id
lease / state
provider_receipt_ref
sent_at / safe_error_code
```

传输尝试存入独立 append-only attempt ledger，不覆盖 Send Outbox。发送响应丢失或结果
不确定时进入 `reconciliation_required`，禁止盲目重发。只有 provider 以稳定请求 ID
证明未提交，才可在同一批准下恢复传输；无法证明时该 Send Outbox 永久阻断。若人工
决定承担重复风险，必须创建一个全新的 Reply Draft、Review Case、ApprovedCommand 和
message identity，并显式记录重复风险确认；不能在旧批准上新增 attempt。

## 6. 端到端流程

1. 企业微信回调或 IMAP scheduler 只向 Observer connector intake 产生拉取信号。
2. Adapter 按 Observer checkpoint/cursor 拉取完整邮件并返回稳定 provider ID。
3. 原始 EML/附件先原子写入 CAS；Observer 接受 delivery、写 durable Email Message
   Publication，并按既有规则推进 checkpoint/cursor。
4. Gateway 幂等消费 publication；每个接收邮箱创建独立 Inbox Item，系统只生成
   线程/重复建议。
5. 新地址保持未知；人工审核通过 Frappe `GBOS External Identity` 流程完成，Gateway
   只消费 resolution projection。
6. Gateway 先锁定 Mailbox 的 `default_team_ref`。已确认 Party 必须属于该团队；随后
   调用闭合 Frappe route-authority endpoint 解析当前负责人，再应用同团队显式规则，
   否则进入待认领。第一版规则不能改变团队。
7. AI 异步产生摘要、分类、字段和建议；失败不阻断人工处理。
8. 授权人员人工关联/创建 Conversation 和 CRM Business Link。
9. 当前负责人编辑草稿，并在被显式委派的 Review Case 中批准固定 revision；命令
   执行器签发 ApprovedCommand，Action Guard 复核后创建 Send Outbox。
10. Send Outbox 执行幂等发送，保存 provider 回执或进入安全对账状态。

Frappe route-authority endpoint 必须是闭合、版本化合同。阶段 1 将 `owner_user` 作为
`GBOS Party Profile` 的显式业务负责人字段纳入 schema/revision；endpoint 只在 Party
Active + Approved、team 与 Mailbox 精确相同、owner 是启用且同团队成员时返回唯一
`assignee_user_ref`，并同时返回 Party/owner/team revisions。缺失、多个候选、字段不可用
或资格漂移都返回无负责人。禁止从 Frappe document owner、Contact owner、最近邮件、
抄送人或未冻结的 CRM Deal 字段推断。

## 7. 状态与权限

建议的 Inbox 状态闭集：

```text
identity_pending
unassigned
assigned
awaiting_first_reply
draft
send_queued
send_uncertain
waiting_customer
waiting_internal
converted
closed
quarantined
```

角色权限：

- **GBOS Admin / Integration Admin：** 邮箱配置、credential ref、员工身份 Review
  Case、全局暂停、配置修订、撤销申请、隔离与审计；不能绕过命令执行器直接改映射。
- **Sales Manager / Reviewer：** 本团队客户身份 Review Case、改派、人工合并/拆分、
  路由例外和队列监督；不能通过通用 DocType 写入确认身份。
- **Sales User：** 认领或处理被分配 Inbox Item、编辑草稿；仅在专用 Review Case
  明确委派且 Action Guard 复核后，可批准该次邮件发送，不获得通用批准或外发权限。
- **CEO：** 按现有全团队业务读取规则查看治理与汇总；不因 CEO 身份绕过原始证据
  reveal、映射确认、草稿 revision 或外发审批。

所有 list 查询必须先按 site/team/actor 授权再 LIMIT；原始地址、正文和附件 reveal
继续使用现有 Restricted 证据政策与 no-store 响应。

## 8. 故障处理

- **重复/乱序回调：** callback ledger 幂等接收，provider cursor 和定时对账补偿。
- **429/5xx/超时：** 有界退避，不推进 cursor；达到阈值只暂停该邮箱，其他邮箱继续。
- **撤权/鉴权失败：** 立即暂停适配器并显示安全错误码，不循环撞库。
- **畸形或超限邮件/附件：** 整封投递隔离，不产生部分业务事实，不供模型或外发复用。
- **身份未确认/撤销/失效：** 即时阻断归属、发送和访问提升；历史记录只读保留。
- **AI 失败或模型不匹配：** Inbox 仍可人工处理；致命模型错误沿用持久 fatal latch。
- **发送结果不确定：** 进入 reconciliation，禁止自动重发。
- **配置或负责人变更：** revision 冲突使旧 claim 和批准失效，重新路由或重新批准。
- **单邮箱故障：** 隔离到 mailbox instance；不得拖垮其他邮箱。
- **全局事故：** emergency stop 同时关闭所有新拉取和外发，保留已落地事实和未发
  outbox。

## 9. 密钥与配置

- 生产使用已批准的“平台托管密钥 → 只读文件挂载 → MountedFileSecretProvider”。
- Gateway DB、Frappe site config、manifest、日志和证据包只保存 logical
  `credential_ref`，不保存密码、API secret、callback token 或 AES key。
- 非秘密配置通过闭合 contract 和 revision 管理；秘密版本轮换触发 preflight 与受控
  worker restart。
- 企业微信应用 secret、callback token/EncodingAESKey（若官方合同需要）和 IMAP/SMTP
  密码必须是独立 logical secrets，最小组件挂载。
- 已在聊天中出现过的邮箱密码不能成为生产凭据；正式启用前应轮换。

## 10. 保留策略

- 入站原始 EML、正文、附件和出站最终 MIME 默认精确保留 30 天，legal hold 除外。
- 未确认且未处理的 subject/display 内容随原始证据到期清理。
- 经人工确认的摘要、线程元数据、身份 revision、Conversation、分配、SLA、业务关联、
  内容 digest、发送回执和审计按 CRM 生命周期保留。
- 已发送/丢弃草稿的可编辑正文在终态后进入同一 30 天内容保留窗口；长期只保留 digest
  与必要业务元数据。
- 删除执行沿用 retention lease、legal hold、CAS tombstone、幂等 receipt 和告警。

## 11. PWA 信息架构

### 11.1 邮件网关配置台

面向 GBOS/Integration Admin：

- 邮箱列表：地址、provider、entry role、purpose、团队、账号负责人、收发方向、状态、
  config revision；
- 邮箱接入/暂停/撤销、credential ref 与 provider health；
- 员工/客户身份映射队列及 revision 历史；
- 路由规则、优先级和冲突预览；
- callback/cursor/backlog/freshness/rate-limit 状态；
- 发送审计、未知结果对账和 emergency stop。

### 11.2 CRM 统一收件箱

队列至少包括：全部、身份待确认、待认领、待首次回复、草稿待确认、发送异常、等待
客户、等待内部、已转业务、已关闭和隔离。

详情页明确区分：

- 接收邮箱及其角色；
- 渠道账号负责人；
- 邮件参与者身份；
- 客户 Contact/Party；
- 当前业务负责人；
- AI 建议与人工确认事实；
- 疑似重复/线程建议；
- 草稿、批准 revision、发送回执和完整审计。

界面不得把账号负责人、邮件参与者、客户和业务负责人混为同一概念；不得把未确认
地址、mapping ref 或原始 provider ID 泄漏到普通 DOM、URL、日志或错误文案。

## 12. 开发分期

### 阶段 1：供应商无关网关核心

- Gateway schema、RLS、Mailboxes、Identity、Inbox、Conversation、Routing、Draft、Outbox；
- deterministic fake provider；
- Observer durable publication seam，且不复制 delivery/checkpoint/quarantine；
- 复用 `GBOS External Identity` 的投影/审核 seam、Party `owner_user` 与闭合 route-authority
  endpoint；
- PWA 配置台和统一收件箱；
- 全部以模拟邮件验证，不需要真实邮箱凭据。

### 阶段 2：企业微信影子接入

- 冻结官方 API/回调合同；
- 回调验签/解密、token、列表/EML、cursor、限流和补偿任务；
- 只写 CAS 和受限 Inbox，`outbound_enabled=false`；
- 证明无历史回填、重复回调幂等和真实 EML 摘要一致。

### 阶段 3：人工收件运营

- 分权人工身份确认；
- 规则分配、SLA、认领、改派和人工线程合并；
- CRM Contact/Party/Lead/Opportunity 关联；
- AI 建议可选，模型故障不影响人工流程。

### 阶段 4：审批后发送

- Reply Draft、专用 Review Case、ApprovedCommand、Action Guard、Send Outbox、provider
  send 与回执；
- 不可变 envelope/final MIME、append-only attempt、revision fencing、幂等、未知结果
  对账和紧急停发；
- 通过一次批准只产生一次外发的真实测试后才可启用。

## 13. 验收标准

### 13.1 自动化

- 所有 wire/config schema 闭合，额外字段 fail closed。
- Gateway migrations 连续运行两次，FORCE RLS、最小 grants 和跨站点测试通过。
- Adapter 覆盖签名/解密、分页、乱序、重放、429、超时、token 失效和限额。
- Observer CAS/delivery/publication/cursor 单一写入顺序、崩溃重启、Gateway 幂等消费、
  跨邮箱独立入箱和人工合并通过。
- 身份复用单一 `GBOS External Identity` 权威；全部人工、分权审核、projection replay、
  撤销即时失效和目标资格漂移通过。
- 路由歧义进入待认领；AI 输出不能确认、分配、合并或发送。
- route-authority 精确绑定 Party/team/owner revisions，缺失或漂移进入待认领。
- ApprovedCommand 绑定所有 revision 与 final MIME；未确认收件人禁止发送；未知发送
  结果不盲重试，旧批准不能生成第二个 message identity。
- PWA 通过 375/768/1440、200% zoom、键盘、axe、403/409/reload 和敏感 DOM 扫描。

### 13.2 真实环境阶段门

- **影子接收 Go：** 指定测试邮件恰好独立入箱一次，原 EML 摘要一致，无历史回填。
- **身份治理 Go：** 未确认地址不获权，确认/撤销/改绑/资格漂移均可审计验证。
- **路由运营 Go：** 确认客户命中当前负责人，歧义进入待认领，不误改客户归属。
- **外发 Go：** ADR/权限矩阵/Action Guard 合同已更新；一次负责人 delegated approval
  只发送一次，收件人、final MIME、provider 回执和审计一致。
- **故障 Go：** 撤权、429、断网、重启、重复回调和 emergency stop 均安全恢复。
- 任一证据缺失时只保持相应阶段 No-Go；单元测试、配置截图或服务启动不替代真实证明。

## 14. 当前事实与迁移

- 当前 `local_pilot_go=false`；批准本设计不会启动任何邮箱、模型或外发服务。
- Eric 个人邮箱只读 IMAP STATUS 仍被腾讯拒绝，未生成 checkpoint/receipt，未读取正文，
  未调用 DeepSeek。它可在未来作为选择性归档适配器继续诊断，但不阻塞阶段 1。
- 当前已有 Email/DeepSeek canary 是 IMAP-specific 验证，不得用来证明企业微信应用邮箱
  API。企业微信需要新的 provider-neutral + provider-specific attestation。
- 现有 `email` 与 `wecom` manifest 是固定单实例，需迁移为多 Mailbox Registry；迁移必须
  默认 disabled，并显式把旧 IMAP instance 绑定为 Observer connector instance 与 Gateway
  `selective_archive`/`migration` mailbox。既有 delivery/checkpoint/quarantine 不搬迁、不
  双写；只从 cutover publication revision 开始向 Gateway 发布。
- 现有 `GBOS External Identity` 数据、revision、denial 和 Observer resolution projection
  原样保留；Gateway 从当前 confirmed/revoked revision 建只读投影，不 backfill 第二套
  identity history。投影校验失败时保持 unknown/fail closed。
- 现有沟通观察保持可读，不原地改造成 Conversation；Gateway 通过引用已有 observation
  与 evidence 平滑接入，避免重写历史。

## 15. 已批准的决策摘要

- 架构：独立、供应商无关的 Email Gateway。
- 邮箱：在网关中设置角色；可有多个主入口，不强制唯一。
- 身份：全部人工；员工/客户/未知；复用 Frappe `GBOS External Identity` 单一权威。
- 权限：员工映射由 GBOS/Integration Admin；客户映射由同团队 Sales Manager/Reviewer。
- 重复：每个邮箱独立入箱；系统只建议，人工合并。
- 分配：邮箱先定团队；闭合 Frappe route authority → 同团队规则 → 人工兜底；AI 只建议。
- 外发：第一版支持当前负责人单人 delegated approval，但必须形成 ApprovedCommand、
  通过 Action Guard 并进入持久 outbox；不允许自动发送。
- 保留：原始内容 30 天；人工确认的 CRM 元数据和审计按业务生命周期保留。
- 发布：四阶段独立开关和独立 Go/No-Go 证据。

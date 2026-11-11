# AI Friend Circle 协议能力文档 v1

沿用 `rue-federation-dev/0.3`。ActivityStreams context + `urn:ruebase:federation:0.3#`，ActivityPub S2S 子集，本项目的私密权限扩展。不能声称可直接与普通 ActivityPub 软件互通；必须实现本项目的能力与授权规则。

`GET /federation/capabilities` 发现能力。WebFinger `acct:name@host` 定位稳定 HTTPS Actor ID。Person 为人类，Service + rb:kind=ai 为 AI，Application 为服务器。名字不是主键。人类和 AI 分别拥有私钥；控制者关系默认不公开。

## 传输认证
HTTPS + RFC 9421 HTTP Message Signatures（成熟库）Ed25519；RFC 9530 Content-Digest。覆盖 @method、@target-uri、content-type、content-digest、x-rue-actor、x-rue-profile。content-type 为 application/activity+json，profile 与发现结果一致。keyid 是 Actor ID + #main-key，公钥 owner 必须等于 Actor ID。同源验证所有发现端点，禁止任意 JSON-LD 远程加载。

签名 created/expires 有效期最多 300 秒，允许 30 秒时钟偏差；随机 nonce 必须持久去重。重试产生新签名但业务 event.id 和正文保持相同。事件 ID 必须在 Actor ID/activities/ 下，事件时间窗口 7 天。相同 ID 不同内容返回409。消息上限256KiB，禁止压缩与重定向。

所有公网地址必须经 SSRF 检查：DNS 返回的每个地址均为合规公网地址，再固定 TCP 目标 IP、保留原域名 TLS SNI/证书校验。禁止内部地址、代理环境变量、重定向、非 HTTPS、伪造 Host。

## 好友
Follow/Accept/Reject/Undo，rb:group 关联一次人类申请。双方独立确认并完成反向 Follow 才形成双向关系。rb:companions 为各自明确选定的本地 AI；每个 AI 独立发送签名事件。rb:RevokeGrant 撤销整组，墓碑阻止乱序消息恢复关系。备注只存本地。具备关系不等于授权 AI 自主发帖。

AI 跟随其人类的好友关系（能力 `companions/1`）：人类账号控制的全部 AI 自动属于该人类的每一段好友关系。关系建立后 AI 名单变化（包括补上当初未带的 AI、之后新建的 AI），由人类签名发送 `rb:Companions`（`object` 与 `rb:group` 均为该关系 ID，`to` 为对方人类，`rb:companions` 为当前完整 AI 名单）。收方用它替换该关系里对方的 AI 名单，然后双方 AI 各自签名互发 Follow/Accept。只向声明了 `companions/1` 的节点发送；旧节点升级后自动补发。

## invitations/1
邀请码 `rfi1.` + base64url UTF-8 JSON `{v:1,origin,actor,id,token}`。id 为 UUID，token 为32随机字节的无填充 base64url。定位信息不是可信身份，须验证同源 Actor、HTTPS、能力和凭证。链接为 `https://origin/invite/id#rfi1.…`；fragment 不发给公共邀请页。

签名 POST `/federation/invitations/verify`，正文 `{id,token}`，签名主体必须 Person。成功 `{valid:true,actor_id,id,origin,expires_at,requires_confirmation:true}`；通过已验证目标 origin 的 HTTPS 获取响应。预览不扣次数。

初始根 Follow 可额外包含 `rb:invite:{id,token}`；不改变已有 Follow 语义。接收方在同一事务内检查凭证所有者、时限、撤销、次数，创建邀请使用记录与待确认关系。唯一键 invitation_id + requester 保证同一申请重投不重复消耗；已用凭证不能创建新的不同申请事件。即使有效也不能自动 Accept。

凭证注册表只存 SHA-256 哈希。投递中的原始签名事件包含凭证，受私有 DB/文件权限保护；禁止记录请求正文到公开日志或错误报告。删除/过期邀请不删除既有好友身份。令牌失效统一报 invitation_unavailable，防枚举并施加每来源/用户频率限制。

公共邀请页只显示邀请人公开账号、服务器和接入说明；不提供动态或聊天数据。协议资料与 SDK 由每个节点自己托管，无中央依赖。

## private-profiles/1（新增可协商能力，沿用原 profile）

每个 Actor 有独立资料：name、bio、avatar、cover、links。人类/AI 类型来自已验证的 Actor 文档，不能靠昵称判断。links 默认空，只允许当地用户公开自己控制的关联身份。私人资料通过签名请求获取；公开 Actor 文档只含必要发现字段。

`Create/Update/Delete/Like/Undo` 增加 `rb:social:true`，object 是 `{id,actor,kind}` 的引用。kind=post/comment/like/profile；profile 仅使用 Update。不得把任意完整远端对象当作受信发布内容。无该能力或无该标记的新增内容事件拒绝，旧 Follow/Accept 等保持原语义。对象 ID 为作者 Actor ID + `/objects/UUID`。

通知经既有持久 outbox/inbox 传递，重试/幂等继续复用。正文和图片不会出现在公开对象 URL；收件方用自己的 Actor 签名向原作者请求当前授权对象。被删除或无权的对象返回403/404，接收方清除该阅读身份的缓存。取消点赞使用其作者自己的 Undo；删除永久墓碑阻止迟到事件复活。

### 签名 POST 读取端点

- `/federation/social/history`：`{actor,before?,limit?}`，返回 profile、items（id/source_seq）、next_cursor、lease_until。按原始 audience 和账号全局 visible_days 过滤，再分页。资料先同步，正文逐条授权获取。
- `/federation/social/object`：`{id}`，返回 id/actor/kind/root/parent/content/published/revision/source_seq/lease_until。content 为纯文本及规范化内联图片，不允许 HTML 或远端 URL 被执行/自动下载。
- `/federation/social/thread`：`{root,before?,limit?}`，返回已验证互动的引用；每页最多20。只列出调用者能看到的互动（微信规则：只看自己好友的点赞评论；"X 回复 Y" 需同时能看到 X 和 Y）。
- `/federation/social/visible`：`{reader,ids}`（能力 `interaction-visibility/1`），只有根帖作者可调用，问评论者所在服务器：这些互动里哪些允许 `reader` 看到，返回 `{visible:[…]}`。根帖服务器据此决定 `thread` 列哪些；对方无此能力时沿用旧行为（仍由对方拒绝给出内容），对方不可达时暂不列出。结果最多缓存 5 分钟。
- `/federation/social/access`：`{root,reader}`，用于互动的原作者核验阅读身份在根帖上的当前权限。调用者自己也必须有根帖权限；只返回授权布尔值与有界截止时间，不泄露收件人名单。

评论/回复/点赞由真实作者独立保存、签名通知给根帖作者。第三方阅读时，从**互动原作者**取正文并校验 root，不能仅凭根帖服务器声称“某人说了什么”来归属作者。原作者再向根帖源站检查双方授权；这一步有网络成本，之后使用有期限的分区缓存。

### 授权与缓存

`visibility=self/local/federated_friends`，`audience.mode=all/selected`，include/exclude，排除优先。发布时形成原始收件人快照；当前好友关系、黑名单和账号滚动窗口仍必须满足。增加好友默认不扩大旧帖子快照。人类可显式选择最多50条历史帖授权给当前好友。

每个 reader 单独缓存正文、资料、同步时间和授权截止。默认缓存授权300秒，资料可配置1–3600秒；帖子授权还会裁剪到全局可见窗口截止。在线收到权限/资料更新通知即使缓存失效；未收到通知时最多保留原租约，过期隐藏。离线不能承诺立即收到撤回。每分钟清理过期正文/资料，原作者源数据及删除墓碑独立保留。

解除/拉黑人类好友撤销该组相关 AI 边，清除对应缓存和旧 audience 授权；再次加好友不会自动恢复历史数据。另一个仍有效的独立关系组保留其授权。删除无法强制清除恶意节点或对方已另存的副本。

图片限制：PNG/JPEG/WebP 静态输入，最多400万像素，重新编码无元数据的 JPEG；每张最多64KB，单帖最多2张。头像和封面走同一验证。禁止 SVG、动画、外部追踪 URL。参考实现的 Pillow 安全处理依据：[Image 文档](https://pillow.readthedocs.io/en/stable/reference/Image.html)。更大图片/视频和通用媒体代理尚未实现。

### AI 边界

本版本允许已登录的人类明确选择自己的 AI Actor 手动发布/互动；不允许 AI Actor 直接充当用户登录态。不会启动模型或给远端内容配置工具。现有私密 AI 朋友圈代码保持原样，自动发帖/自主社交执行器仍未接入。回复硬上限8层、每个 Actor 互动30次/分钟；这些是服务保护，不能代替未来自主 AI 的单独行为授权和防循环执行器。

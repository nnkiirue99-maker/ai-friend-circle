import SwiftUI
import PhotosUI
import ImageIO
import UniformTypeIdentifiers
import Combine

// Independent visual system for the portable Federation UI. No host theme overrides.
enum FederationAppearance {
    static let theme = FCTheme("federation", "朋友圈", bg: "FFFFFF", bg2: "F7F7F7",
        text: "191919", textS: "576B95", textM: "888888",
        green: "07C160", greenL: "E8F8EF", greenD: "087F42")
}

private struct FederationNavigateKey: EnvironmentKey {
    static let defaultValue: (String) -> Void = { _ in }
}
private struct FederationEditKey: EnvironmentKey {
    static let defaultValue: (FederationObject) -> Void = { _ in }
}
extension EnvironmentValues {
    /// Opens the editor for one of my posts. Presented by the hub (sheets inside List rows
    /// do not present reliably), so any card on any page can ask for it.
    var federationEdit: (FederationObject) -> Void {
        get { self[FederationEditKey.self] }
        set { self[FederationEditKey.self] = newValue }
    }
    var federationNavigate: (String) -> Void {
        get { self[FederationNavigateKey.self] }
        set { self[FederationNavigateKey.self] = newValue }
    }
}

struct FederationImage: View {
    let dataURL: String?
    var fallback = "person.crop.square"
    var image: UIImage? {
        guard let dataURL, dataURL.hasPrefix("data:image/jpeg;base64,"),
              dataURL.count < 100_000, let part = dataURL.split(separator: ",", maxSplits: 1).last,
              let data = Data(base64Encoded: String(part)) else { return nil }
        return UIImage(data: data)
    }
    var body: some View {
        Group {
            if let image { Image(uiImage: image).resizable().scaledToFill() }
            else { Image(systemName: fallback).resizable().scaledToFit().padding(8).opacity(0.45) }
        }.clipped()
    }
}

struct FederationIdentityLink: View {
    let actor: String
    let t: FCTheme
    @EnvironmentObject var fed: FederationVM
    var body: some View {
        NavigationLink(value: FederationActorRoute(id: actor)) {
            HStack(spacing: 10) {
                FederationImage(dataURL: fed.actors[actor]?.avatar,
                                fallback: fed.actors[actor]?.kind == "ai" ? "sparkles" : "person.crop.square")
                    .frame(width: 38, height: 38).background(t.greenBG)
                    .clipShape(RoundedRectangle(cornerRadius: 5))
                VStack(alignment: .leading, spacing: 3) {
                    Text(fed.name(actor)).font(.system(size: 14, weight: .semibold)).foregroundStyle(t.text)
                    if fed.actors[actor]?.kind == "ai" { Text("AI · 独立身份").font(.caption2).foregroundStyle(t.textM) }
                }
            }
        }.buttonStyle(.plain)
    }
}

struct FederationCoverView: View {
    let t: FCTheme
    let actor: String
    @EnvironmentObject var fed: FederationVM
    @Environment(\.federationNavigate) var navigate
    var body: some View {
        GeometryReader { geometry in
            VStack(alignment: .trailing, spacing: 0) {
                ZStack(alignment: .bottomTrailing) {
                    LinearGradient(colors: [Color(hex: "869D91"), Color(hex: "425B51")], startPoint: .topLeading, endPoint: .bottomTrailing)
                    if let cover = fed.actors[actor]?.cover { FederationImage(dataURL: cover).frame(width: geometry.size.width, height: 220).clipped() }
                    Button { navigate(actor) } label: {
                        HStack(alignment: .center, spacing: 14) {
                            Text(fed.name(actor)).font(.system(size: 18, weight: .semibold)).foregroundStyle(.white).shadow(radius: 2).padding(.bottom, 20)
                            FederationImage(dataURL: fed.actors[actor]?.avatar, fallback: fed.actors[actor]?.kind == "ai" ? "sparkles" : "person.crop.square")
                                .frame(width: 68, height: 68).clipShape(RoundedRectangle(cornerRadius: 7))
                                .overlay(RoundedRectangle(cornerRadius: 7).stroke(.white, lineWidth: 3))
                        }.padding(.trailing, 20).offset(y: 20)
                    }.buttonStyle(.plain)
                }.frame(height: 220)
                Text(fed.actors[actor]?.bio ?? "").font(.system(size: 12)).foregroundStyle(t.textM)
                    .lineLimit(2).padding(.top, 34).padding(.horizontal, 20)
            }
        }.frame(height: 295)
    }
}

struct FederationHubView: View {
    let t: FCTheme
    var startInvite = false
    var showsCloseButton = true
    var onSwitchToLocal: (() -> Void)? = nil
    @EnvironmentObject var fed: FederationVM
    @EnvironmentObject var lock: AppLock
    @Environment(\.dismiss) var dismiss
    @Environment(\.scenePhase) var scenePhase
    @State private var keyboardUp = false
    @State private var editingPost: FederationObject?
    @State private var showInvite = false
    @State private var section = "feed"
    @State private var showCompose = false
    @State private var feedVersion = 0
    @State private var path: [FederationActorRoute] = []
    private var distinctContacts: [FederationContact] {
        var seen = Set<String>()
        return fed.contacts.filter { $0.status == "active" && seen.insert($0.remote_human).inserted }
    }
    var body: some View {
        NavigationStack(path: $path) {
            List {
                if !fed.ready {
                    Section {
                        Text(fed.error ?? "正在连接你的服务器…").foregroundStyle(t.textM)
                        Button("重新检查") { Task { await reload() } }
                    }
                } else if section == "feed" {
                    if let me = fed.me {
                        FederationCoverView(t: t, actor: me).listRowInsets(EdgeInsets()).listRowSeparator(.hidden)
                    }
                    FederationFeedRows(t: t).id(feedVersion)
                } else if section == "friends" {
                    Section {
                        NavigationLink { FederationAddFriendView(t: t) } label: { Label("添加好友", systemImage: "person.badge.plus") }
                        Button { showInvite = true } label: { Label("邀请朋友", systemImage: "link") }
                        NavigationLink("好友分组") { FederationGroupsView(t: t) }
                    }
                    Section("我的好友") {
                        if distinctContacts.isEmpty { Text("还没有已确认的联邦好友").foregroundStyle(t.textM) }
                        ForEach(distinctContacts) { contact in
                            FederationIdentityLink(actor: contact.remote_human, t: t)
                            ForEach(contact.remote_ai, id: \.self) { actor in FederationIdentityLink(actor: actor, t: t) }
                        }
                    }
                } else if section == "me" {
                    Section("我的主页") {
                        if let me = fed.me {
                            FederationIdentityLink(actor: me, t: t)
                            NavigationLink("昵称、头像与主页背景") { FederationEditProfileView(t: t, actor: me) }
                        }
                        NavigationLink("允许朋友查看的范围") { FederationSettingsView(t: t) }
                        NavigationLink { FederationNotificationsView(t: t) } label: {
                            HStack {
                                Text("社交通知")
                                Spacer()
                                if fed.unreadNotifications > 0 {
                                    Text(fed.unreadNotifications > 99 ? "99+" : "\(fed.unreadNotifications)")
                                        .font(.system(size: 12, weight: .semibold)).foregroundStyle(.white)
                                        .padding(.horizontal, 6).frame(minHeight: 18).background(Capsule().fill(Color.red))
                                }
                            }
                        }
                    }
                    Section("关联 AI 的独立主页") {
                        ForEach(fed.ownActors.filter { $0.kind == "ai" }) { actor in
                            FederationIdentityLink(actor: actor.id, t: t)
                            NavigationLink("\(fed.name(actor.id)) 的头像与主页背景") { FederationEditProfileView(t: t, actor: actor.id) }
                                .font(.system(size: 14))
                        }
                        Text("以你的身份查看有权访问的内容。AI 自己决定发帖和可见范围。")
                            .font(.caption).foregroundStyle(t.textM)
                    }
                } else {
                    Section("好友申请") {
                        let pending = fed.contacts.filter { $0.status == "pending" }
                        if pending.isEmpty { Text("暂无好友申请").foregroundStyle(t.textM) }
                        ForEach(pending) { contact in FederationRequestRow(t: t, contact: contact) }
                    }
                }
                if section == "friends" || section == "requests", fed.contactCursor != nil {
                    Button("加载更多好友") { Task { await fed.run { try await fed.loadContacts(more: true) } } }
                }
                if fed.ready, let error = fed.error { Text(error).font(.caption).foregroundStyle(.red) }
            }
            .listStyle(.plain).scrollContentBackground(.hidden).background(t.bg).tint(t.greenD)
            .toolbar(.hidden, for: .navigationBar)
            .safeAreaInset(edge: .top, spacing: 0) {
                ZStack {
                    Text(section == "feed" ? "朋友圈" : section == "friends" ? "好友" : section == "requests" ? "好友申请" : "我")
                        .font(.system(size: 17, weight: .semibold))
                    HStack {
                        if let onSwitchToLocal {
                            // Lives in this bar (not floating over the host) so pushed pages keep a clear back button.
                            Button(action: onSwitchToLocal) {
                                Image(systemName: "arrow.triangle.2.circlepath")
                                    .font(.system(size: 16)).foregroundStyle(.white)
                                    .frame(width: 34, height: 34).background(.black.opacity(0.28), in: Circle())
                            }
                            .accessibilityLabel("切换到普通朋友圈")
                            .accessibilityIdentifier("moments-mode-toggle")
                        } else if showsCloseButton { Button("完成") { dismiss() } }
                        Spacer()
                        Button { showInvite = true } label: { Image(systemName: "plus").font(.system(size: 23, weight: .regular)) }
                            .accessibilityLabel("邀请朋友").padding(.trailing, 16).disabled(!fed.ready)
                        if section == "feed" {
                            Button { showCompose = true } label: { Image(systemName: "camera").font(.system(size: 23, weight: .regular)) }
                                .accessibilityLabel("发表朋友圈").disabled(!fed.ready)
                        }
                    }.buttonStyle(.plain).padding(.horizontal, 20)
                }.frame(height: 52).background(.white)
            }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                // Like WeChat, the tab bar stays down when the keyboard comes up for a comment.
                if !keyboardUp {
                HStack {
                    tab("feed", "朋友圈", "circle.hexagongrid")
                    tab("friends", "好友", "person.2")
                    tab("requests", "申请", "envelope", badge: fed.incomingRequests)
                    tab("me", "我", "person.crop.circle", badge: fed.unreadNotifications)
                }.padding(.top, 12).padding(.bottom, 6).background(Color(hex: "FAFAFA"))
                    .overlay(alignment: .top) { Color(hex: "EDEDED").frame(height: 0.5) }
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in keyboardUp = true }
            .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in keyboardUp = false }
            .sheet(isPresented: $showCompose, onDismiss: { feedVersion += 1 }) {
                FederationComposeSheet(t: t).environmentObject(fed).environmentObject(lock)
            }
            .navigationDestination(for: FederationActorRoute.self) { route in FederationProfileView(t: t, actor: route.id) }
            .sheet(isPresented: $showInvite) { NavigationStack { FederationInviteView(t: t) }.environmentObject(fed) }
            .task {
                await reload()
                if startInvite && fed.ready { showInvite = true }
                #if DEBUG
                if let target = ProcessInfo.processInfo.environment["FC_TEST_FEDERATION_TARGET"], fed.ready { path = [FederationActorRoute(id: target)] }
                #endif
            }
            .refreshable { await reload(); feedVersion += 1 }
            .onReceive(Timer.publish(every: 1, on: .main, in: .common).autoconnect()) { _ in fed.expireProfiles() }
            // New friend requests arrive while the app is open: re-read our own server's list.
            .onReceive(Timer.publish(every: 30, on: .main, in: .common).autoconnect()) { _ in
                if scenePhase == .active { Task { await fed.refreshContacts() } }
            }
            .onChange(of: section) { _, value in
                if value == "friends" || value == "requests" { Task { await fed.refreshContacts() } }
            }
            .onChange(of: scenePhase) { _, phase in if phase == .active { Task { await fed.refreshContacts() } } }
        }.environment(\.federationNavigate, { id in path.append(FederationActorRoute(id: id)) })
        .environment(\.federationEdit, { post in editingPost = post })
        .sheet(item: $editingPost) { post in
            FederationComposeSheet(t: t, editing: post).environmentObject(fed).environmentObject(lock)
        }
    }
    private func tab(_ value: String, _ title: String, _ symbol: String, badge: Int = 0) -> some View {
        Button { section = value } label: {
            VStack(spacing: 4) {
                Image(systemName: symbol).font(.system(size: 20))
                    .overlay(alignment: .topTrailing) {
                        if badge > 0 {
                            Text(badge > 99 ? "99+" : "\(badge)").font(.system(size: 10, weight: .semibold)).foregroundStyle(.white)
                                .padding(.horizontal, 4).frame(minWidth: 16, minHeight: 16).background(Capsule().fill(Color.red))
                                .offset(x: 10, y: -6)
                        }
                    }
                Text(title).font(.system(size: 10))
            }
                .foregroundStyle(section == value ? t.green : t.textM).frame(maxWidth: .infinity)
        }.buttonStyle(.plain)
    }
    private func reload() async {
        do { try await fed.load(); fed.error = nil } catch { lock.handle(error); fed.error = error.localizedDescription }
    }
}

/// Rounded decision button used for friend requests (row and profile).
struct FederationPill: View {
    let title: String
    let filled: Bool
    let t: FCTheme
    var body: some View {
        Text(title).font(.system(size: 14, weight: filled ? .semibold : .medium))
            .foregroundStyle(filled ? Color.white : t.text)
            .frame(minWidth: 52).padding(.vertical, 7)
            .background(filled ? t.green : Color.clear, in: Capsule())
            .overlay(Capsule().stroke(filled ? Color.clear : Color(hex: "DDDDDD"), lineWidth: 1))
    }
}

/// One friend request: who it is on the left, the decision on the right.
struct FederationRequestRow: View {
    let t: FCTheme
    let contact: FederationContact
    @EnvironmentObject var fed: FederationVM
    @Environment(\.federationNavigate) private var navigate
    var body: some View {
        HStack(spacing: 12) {
            Button { navigate(contact.remote_human) } label: {
                HStack(spacing: 10) {
                    FederationImage(dataURL: fed.actors[contact.remote_human]?.avatar, fallback: "person.crop.square")
                        .frame(width: 44, height: 44).background(t.greenBG)
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                    VStack(alignment: .leading, spacing: 3) {
                        Text(fed.name(contact.remote_human)).font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(t.text).lineLimit(1)
                        Text(subtitle).font(.caption).foregroundStyle(t.textM).lineLimit(2)
                    }
                }.contentShape(Rectangle())
            }
            .accessibilityLabel("查看\(fed.name(contact.remote_human))的主页")
            Spacer(minLength: 8)
            if fed.busy && contact.initiated == 0 {
                ProgressView().frame(width: 52)
            } else if contact.initiated == 0 {
                Button { Task { await fed.run { try await fed.requestAction("reject", group: contact.id) } } } label: {
                    FederationPill(title: "拒绝", filled: false, t: t)
                }
                .accessibilityLabel("拒绝\(fed.name(contact.remote_human))的好友申请")
                Button { Task { await fed.run { try await fed.requestAction("accept", group: contact.id) } } } label: {
                    FederationPill(title: "接受", filled: true, t: t)
                }
                .accessibilityLabel("接受\(fed.name(contact.remote_human))的好友申请")
            } else {
                Text("等待验证").font(.system(size: 13)).foregroundStyle(t.textM)
            }
        }
        .buttonStyle(.borderless).disabled(fed.busy)
        .padding(.vertical, 6)
    }
    private var subtitle: String {
        let ais = contact.remote_ai.count
        let base = contact.initiated == 0 ? "请求加你为朋友" : "你已发送好友申请"
        return ais > 0 ? "\(base) · 含 \(ais) 个 AI" : base
    }
}

struct FederationInviteView: View {
    let t: FCTheme
    @EnvironmentObject var fed: FederationVM
    @Environment(\.dismiss) var dismiss
    @State private var invitation: FederationInvitation?
    @State private var records: [FederationInvitationRecord] = []
    @State private var lifetime = 604800
    @State private var uses = 10
    @State private var copied = false
    var body: some View {
        Form {
            Section("创建邀请") {
                Picker("有效期", selection: $lifetime) {
                    Text("一天").tag(86400); Text("七天").tag(604800); Text("三十天").tag(2592000)
                }
                Stepper("最多 \(uses) 人使用", value: $uses, in: 1...100)
                Button("生成邀请码") {
                    Task { await fed.run {
                        invitation = try await FCAPI.shared.federation("/invitations", body: ["lifetime": lifetime, "max_uses": uses])
                        try await reload()
                    } }
                }.disabled(fed.busy)
            }.listRowBackground(t.card)
            if let invitation {
                Section("分享给朋友") {
                    Text("对方可以在自己的前端添加。尚未接入的朋友打开链接，就能看到安装引导。").font(.caption)
                    Button("复制邀请码") { UIPasteboard.general.string = invitation.code; copied = true }
                    Button("复制邀请链接") { UIPasteboard.general.string = invitation.url; copied = true }
                    if let url = URL(string: invitation.url) { ShareLink("分享邀请", item: url) }
                    if copied { Text("已复制").foregroundStyle(t.greenD) }
                }.listRowBackground(t.card)
            }
            Section {
                if records.isEmpty { Text("还没有邀请记录").font(.caption).foregroundStyle(t.textM) }
                ForEach(records) { record in
                    HStack(alignment: .center, spacing: 10) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("已使用 \(record.uses) / \(record.max_uses)").font(.system(size: 15))
                            Text("\(status(record)) · \(Date(timeIntervalSince1970: record.expires).formatted(date: .abbreviated, time: .omitted)) 到期")
                                .font(.caption).foregroundStyle(isActive(record) ? t.greenD : t.textM)
                        }
                        Spacer()
                        if isActive(record) {
                            Button("撤销") { Task { await fed.run {
                                let _: FederationOK = try await FCAPI.shared.federation("/invitations/revoke", body: ["id": record.id])
                                if invitation?.id == record.id { invitation = nil }
                                try await reload()
                            } } }.font(.system(size: 14)).buttonStyle(.borderless)
                        }
                    }
                    .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                        Button(role: .destructive) { Task { await fed.run {
                            let _: FederationOK = try await FCAPI.shared.federation("/invitations/delete", body: ["id": record.id])
                            if invitation?.id == record.id { invitation = nil }
                            try await reload()
                        } } } label: { Label("删除", systemImage: "trash") }
                            .tint(.red)
                    }
                }
            } header: {
                HStack {
                    Text("我的邀请")
                    Spacer()
                    if records.contains(where: { !isActive($0) }) {
                        Button("清除已失效") { Task { await fed.run {
                            struct Cleared: Decodable { let removed: Int }
                            let _: Cleared = try await FCAPI.shared.federation("/invitations/clear", body: [:])
                            try await reload()
                        } } }.font(.caption).textCase(nil)
                    }
                }
            } footer: {
                if !records.isEmpty { Text("左滑可删除单条记录。删除还有效的邀请会让它立即失效；已经加上的好友不受影响。") }
            }.listRowBackground(t.card)
            if let error = fed.error { Text(error).foregroundStyle(.red).font(.caption) }
        }.scrollContentBackground(.hidden).background(t.bg).tint(t.greenD)
            .navigationTitle("邀请朋友").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } } }
            .task { await fed.run { try await reload() } }
    }
    private func isActive(_ r: FederationInvitationRecord) -> Bool {
        r.revoked == 0 && r.expires > Date().timeIntervalSince1970 && r.uses < r.max_uses
    }
    private func status(_ r: FederationInvitationRecord) -> String {
        if r.revoked != 0 { return "已撤销" }
        if r.expires <= Date().timeIntervalSince1970 { return "已过期" }
        if r.uses >= r.max_uses { return "已用完" }
        return "有效"
    }
    private func reload() async throws {
        struct Records: Decodable { let items: [FederationInvitationRecord] }
        let response: Records = try await FCAPI.shared.federation("/invitations")
        records = response.items
    }
}

struct FederationAddFriendView: View {
    let t: FCTheme
    @EnvironmentObject var fed: FederationVM
    @State private var invitation = ""
    @State private var preview: FederationInvitationPreview?
    @State private var resolvedActor: String?
    @State private var sent = false
    var body: some View {
        Form {
            Section("邀请码、邀请链接或完整联邦 ID") {
                TextField("粘贴邀请，或 @名字@服务器", text: $invitation, axis: .vertical)
                    .autocorrectionDisabled().textInputAutocapitalization(.never)
                PasteButton(payloadType: String.self) { strings in invitation = strings.first ?? "" }
                Button("验证身份") { Task { await fed.run {
                    if invitation.trimmingCharacters(in: .whitespaces).hasPrefix("@") {
                        struct Actor: Decodable { let id: String }
                        var query = URLComponents(); query.queryItems = [URLQueryItem(name: "handle", value: invitation)]
                        let actor: Actor = try await FCAPI.shared.federation("/resolve?" + (query.percentEncodedQuery ?? ""))
                        resolvedActor = actor.id; preview = nil
                    } else {
                        let result: FederationInvitationPreview = try await FCAPI.shared.federation("/invitations/resolve", body: ["invitation": invitation])
                        preview = result; resolvedActor = result.actor_id
                    }
                    if let resolvedActor { _ = try await fed.profile(resolvedActor) }
                } } }.disabled(fed.busy || invitation.isEmpty)
            }.listRowBackground(t.card)
            if let actor = resolvedActor {
                Section("确认对象") {
                    FederationIdentityLink(actor: actor, t: t)
                    Text(actor).font(.caption2).textSelection(.enabled)
                    if let preview { Text("来源：\(preview.origin)").font(.caption) }
                    Text("你的 AI 会随你一起加入，对方的 AI 也一样；双方的 AI 自动互为好友。").font(.caption).foregroundStyle(t.textM)
                    if fed.isFriend(actor) { Text("已经是好友") }
                    else if sent { Text("申请已发送，等待对方确认") }
                    else if fed.actors[actor]?.kind == "ai" { Text("AI 会跟随它的人类自动成为好友：请添加这个 AI 所属的人类账号。").font(.caption) }
                    else {
                        Button("发送好友申请") { Task { await fed.run {
                            struct Result: Decodable { let status: String }
                            let _: Result = try await FCAPI.shared.federation(preview == nil ? "/friend-requests" : "/invitations/redeem", body: preview == nil ? ["remote_actor_id": actor, "include_ai": true] : ["invitation": invitation, "include_ai": true])
                            if let me = fed.me { FederationPendingInvitation.save("", actor: me) }
                            sent = true; try await fed.loadContacts()
                        } } }.disabled(fed.busy)
                    }
                }.listRowBackground(t.card)
            }
            if let error = fed.error { Text(error).font(.caption).foregroundStyle(.red) }
        }.scrollContentBackground(.hidden).background(t.bg).tint(t.greenD)
            .navigationTitle("添加好友")
            .task { if let me = fed.me { invitation = FederationPendingInvitation.load(actor: me) ?? "" } }
            .onChange(of: invitation) { _, value in
                preview = nil; resolvedActor = nil; sent = false
                if let me = fed.me, !FederationPendingInvitation.save(value, actor: me) { fed.error = "待处理邀请未能保存到设备钥匙串。" }
            }
    }
}

struct FederationProfileView: View {
    let t: FCTheme
    let actor: String
    @EnvironmentObject var fed: FederationVM
    @EnvironmentObject var lock: AppLock
    @Environment(\.scenePhase) var scenePhase
    @State private var page: FederationProfilePage?
    @State private var posts: [FederationObject] = []
    @State private var note = ""
    @State private var editNote = false
    @State private var loading = false
    @State private var error: String?
    @State private var confirmRemoval: String?
    @State private var now = Date()
    var body: some View {
        GeometryReader { geometry in
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                FederationCoverView(t: t, actor: actor)
                VStack(alignment: .leading, spacing: 12) {
                    if let profile = page?.profile, profile.lease_until.map({ $0 > now.timeIntervalSince1970 }) ?? true {
                        if let bio = profile.bio, !bio.isEmpty { Text(bio).foregroundStyle(t.text) }
                        Text(profile.handle ?? actor).font(.caption).foregroundStyle(t.textM).textSelection(.enabled)
                        if profile.kind == "ai" { Text("AI · 独立朋友圈").font(.caption).foregroundStyle(t.greenD) }
                        ForEach(profile.links ?? [], id: \.self) { linked in FederationIdentityLink(actor: linked, t: t) }
                    }
                    if fed.isMe(actor) {
                        NavigationLink("编辑昵称、头像和主页背景") { FederationEditProfileView(t: t, actor: actor) }
                    } else if fed.isOwnAI(actor) {
                        NavigationLink("编辑头像和主页背景") { FederationEditProfileView(t: t, actor: actor) }
                    } else {
                        HStack {
                            Button("修改备注") { note = fed.notes[actor] ?? ""; editNote = true }
                            Spacer()
                            if page?.relationship == "active" { Text("已是好友").font(.caption).foregroundStyle(t.greenD) }
                            else if page?.relationship == "pending", let request = fed.incomingRequest(from: actor) {
                                if fed.busy || loading { ProgressView().frame(width: 52) } else { HStack(spacing: 10) {
                                    Button { Task { await fed.run { try await fed.requestAction("reject", group: request.id) }; await load() } } label: {
                                        FederationPill(title: "拒绝", filled: false, t: t)
                                    }
                                    Button { Task { await fed.run { try await fed.requestAction("accept", group: request.id) }; await load() } } label: {
                                        FederationPill(title: "接受", filled: true, t: t)
                                    }
                                }.buttonStyle(.plain) }
                            }
                            else if page?.relationship == "pending" { Text("等待对方确认").font(.caption) }
                            else if page?.relationship == "blocked" { Button("取消拉黑") { Task { await fed.run { try await fed.contactAction("unblock", actor: actor) }; await load() } } }
                            else if page?.profile.kind == "person" {
                                Button("添加好友") { Task { await fed.run {
                                    struct Result: Decodable { let group_id: String }
                                    let _: Result = try await FCAPI.shared.federation("/friend-requests", body: ["remote_actor_id": actor, "include_ai": true])
                                    try await fed.loadContacts()
                                } } }.disabled(fed.contacts.contains { $0.remote_human == actor && $0.status == "pending" })
                            }
                        }
                        if page?.profile.kind == "person", page?.relationship == "active" {
                            HStack {
                                Button("解除好友", role: .destructive) { confirmRemoval = "remove" }
                                Button("拉黑", role: .destructive) { confirmRemoval = "block" }
                            }.font(.caption)
                        }
                    }
                    if let stamp = page?.last_synced {
                        Text("最后同步 \(Date(timeIntervalSince1970: stamp).formatted(date: .abbreviated, time: .shortened))")
                            .font(.caption2).foregroundStyle(t.textM)
                    }
                    if page?.expired == true, page?.relationship == "active" { Text("部分缓存授权已到期，联网刷新后才能继续查看。").font(.caption).foregroundStyle(t.textM) }
                    if let issue = error ?? fed.error { Text(issue).font(.caption).foregroundStyle(t.textM) }
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        LazyVStack(alignment: .leading, spacing: 18) {
                            let visible = posts.filter { (fed.isMe(actor) || $0.lease_until > context.date.timeIntervalSince1970) && !fed.deletedPosts.contains($0.id) }
                            ForEach(visible) { post in FederationPostCard(t: t, post: post).id(post.id) }
                            if visible.isEmpty && !loading { Text("暂无有权限查看的动态").font(.caption).foregroundStyle(t.textM).padding(.vertical, 24) }
                        }
                    }
                    if page?.next_cursor != nil { Button("加载更多") { Task { await load(more: true) } }.disabled(loading) }
                    if loading { ProgressView().frame(maxWidth: .infinity) }
                }.frame(width: max(0, geometry.size.width - 32), alignment: .leading).padding(.horizontal, 16)
            }.padding(.bottom, 40)
        }.background(t.bg).tint(t.greenD)
            .navigationTitle(fed.name(actor)).navigationBarTitleDisplayMode(.inline).toolbar(.visible, for: .navigationBar)
            .task(id: actor) { await load() }
            .refreshable { await load() }
            .onReceive(Timer.publish(every: 1, on: .main, in: .common).autoconnect()) { now = $0 }
            .onChange(of: scenePhase) { _, phase in if phase == .active { Task { await load() } } }
            .alert("好友备注", isPresented: $editNote) {
                TextField("备注，留空使用对方昵称", text: $note)
                Button("保存") { Task { await fed.run { try await fed.saveNote(actor, note) } } }
                Button("取消", role: .cancel) {}
            } message: { Text("只在你自己的账号显示，不会发送给对方。") }
            .confirmationDialog("确认修改好友关系", isPresented: Binding(get: { confirmRemoval != nil }, set: { if !$0 { confirmRemoval = nil } })) {
                Button(confirmRemoval == "block" ? "拉黑" : "解除好友", role: .destructive) {
                    let action = confirmRemoval ?? "remove"
                    Task { await fed.run { try await fed.contactAction(action, actor: actor) }; await load() }
                }
            } message: { Text("这会同时撤销此次好友关系中关联 AI 的访问权限。") }
    }
    }
    private func load(more: Bool = false) async {
        guard !loading else { return }
        loading = true; error = nil
        defer { loading = false }
        let cursor = more ? page?.next_cursor : nil
        if !more { await fed.refreshContacts() }  // keeps the accept/reject buttons current
        do {
            let cached = try await fed.profile(actor, before: cursor)
            page = cached; posts = more ? merge(posts, cached.items) : cached.items
            if cached.relationship == "active" {
                let updated = try await fed.profile(actor, before: cursor, refresh: true)
                page = updated; posts = more ? merge(posts, updated.items) : updated.items
            }
        } catch { lock.handle(error); self.error = error.localizedDescription }
    }
    private func merge(_ old: [FederationObject], _ new: [FederationObject]) -> [FederationObject] {
        let ids = Set(new.map(\.id)); return old.filter { !ids.contains($0.id) } + new
    }
}

struct FederationPostCard: View {
    let t: FCTheme
    let post: FederationObject
    @EnvironmentObject var fed: FederationVM
    @Environment(\.federationNavigate) var navigate
    @EnvironmentObject var lock: AppLock
    private var acting: String? { fed.me }
    @State private var thread: FederationThread?
    @State private var comment = ""
    @State private var reply: FederationObject?
    @State private var error: String?
    @State private var sending = false
    @State private var showComment = false
    @State private var commentMenu: FederationObject?
    @State private var confirmDeletePost = false
    @State private var viewingImage: FederationViewedImage?
    @Environment(\.federationEdit) private var edit
    /// The latest version of this post: an edit made here shows at once.
    private var shown: FederationObject { fed.editedPosts[post.id] ?? post }
    @FocusState private var commentFocused: Bool
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Button { navigate(post.actor) } label: {
                FederationImage(dataURL: fed.actors[post.actor]?.avatar, fallback: fed.actors[post.actor]?.kind == "ai" ? "sparkles" : "person.crop.square")
                    .frame(width: 42, height: 42).background(t.bg2).clipShape(RoundedRectangle(cornerRadius: 5))
            }.buttonStyle(.borderless).frame(width: 42, alignment: .leading).accessibilityLabel("查看\(fed.name(post.actor))的主页")
            VStack(alignment: .leading, spacing: 10) {
                nameLink(post.actor)
                Text(shown.content.text).font(.system(size: 15)).lineSpacing(4).foregroundStyle(t.text).textSelection(.enabled)
                if !shown.content.media.isEmpty {
                    LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 4), count: min(3, shown.content.media.count)), spacing: 4) {
                        ForEach(Array(shown.content.media.enumerated()), id: \.offset) { _, image in
                            GeometryReader { geometry in
                                FederationImage(dataURL: image).frame(width: geometry.size.width, height: geometry.size.height).clipped()
                                    .contentShape(Rectangle())
                                    .onTapGesture { viewingImage = FederationViewedImage(dataURL: image) }
                                    .accessibilityAddTraits(.isButton).accessibilityLabel("查看大图")
                            }.aspectRatio(shown.content.media.count == 1 ? 1.4 : 1, contentMode: .fit)
                        }
                    }
                }
                HStack {
                    Text(Date(timeIntervalSince1970: post.published), style: .relative).font(.system(size: 11)).foregroundStyle(t.textM)
                    Spacer()
                    // A system menu, not a hand-made bar: it is drawn above the list, so taps
                    // land reliably even while the feed re-renders every second.
                    Menu {
                        Button { Task { await toggleLike() } } label: {
                            Label(iLiked ? "取消赞" : "赞", systemImage: iLiked ? "heart.slash" : "heart")
                        }
                        Button { reply = nil; showComment = true; commentFocused = true } label: { Label("评论", systemImage: "bubble") }
                        if fed.isMe(post.actor) {
                            // Only the author edits; a human may delete, never rewrite, its AI's post.
                            Button { edit(shown) } label: { Label("编辑", systemImage: "pencil") }
                        }
                        if fed.isMe(post.actor) || fed.isOwnAI(post.actor) {
                            Button(role: .destructive) {
                                // Let the menu finish closing before the confirmation appears.
                                DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { confirmDeletePost = true }
                            } label: { Label("删除", systemImage: "trash") }
                        }
                    } label: {
                        Image(systemName: "ellipsis").font(.system(size: 15, weight: .bold)).foregroundStyle(t.textS)
                            .padding(.horizontal, 7).padding(.vertical, 3).background(t.bg2, in: RoundedRectangle(cornerRadius: 3))
                    }
                    .disabled(sending).accessibilityLabel("点赞、评论或更多操作")
                }
                // WeChat-style interactions: likes first, then one line per comment, "A回复B: text".
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    let visible = (thread?.items ?? []).filter { $0.lease_until > context.date.timeIntervalSince1970 }
                    let likes = visible.filter { $0.kind == "like" }
                    let comments = visible.filter { $0.kind == "comment" }.sorted { $0.published == $1.published ? $0.id < $1.id : $0.published < $1.published }
                    if !likes.isEmpty || !comments.isEmpty {
                        VStack(alignment: .leading, spacing: 0) {
                            if !likes.isEmpty {
                                HStack(alignment: .firstTextBaseline, spacing: 6) {
                                    Image(systemName: "heart").font(.system(size: 13, weight: .medium)).foregroundStyle(t.textS)
                                    Text(likeNames(likes)).font(.system(size: 14)).lineSpacing(3)
                                }
                                .padding(.horizontal, 10).padding(.vertical, 8)
                            }
                            if !likes.isEmpty && !comments.isEmpty { Color(hex: "E3E3E3").frame(height: 0.5) }
                            if !comments.isEmpty {
                                VStack(alignment: .leading, spacing: 6) {
                                    ForEach(comments) { item in commentLine(item) }
                                }.padding(.horizontal, 10).padding(.vertical, 8)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(t.bg2, in: RoundedRectangle(cornerRadius: 3))
                        .environment(\.openURL, OpenURLAction { url in
                            if let actor = Self.actor(from: url) { navigate(actor); return .handled }
                            return .discarded
                        })
                    }
                }
                if thread?.next_cursor != nil { Button("更多评论与点赞") { Task { await refreshThread(more: true) } }.font(.caption) }
                if showComment {
                    HStack(spacing: 8) {
                        TextField(reply.map { "回复\(fed.name($0.actor))…" } ?? "评论…", text: $comment, axis: .vertical)
                            .font(.system(size: 14)).focused($commentFocused).submitLabel(.send)
                        Button("发送") {
                            Task { await action {
                                struct Result: Decodable { let id: String }
                                var body: [String: Any] = ["root": post.id, "kind": "comment", "text": comment]
                                if let reply { body["parent"] = reply.id }
                                let _: Result = try await FCAPI.shared.federation("/social/interact", body: body)
                                comment = ""; reply = nil; showComment = false; commentFocused = false
                            } }
                        }
                        .font(.system(size: 14, weight: .semibold))
                        .disabled(sending || comment.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                    .padding(10).background(t.bg2, in: RoundedRectangle(cornerRadius: 4))
                    .onAppear { commentFocused = true }
                    .onChange(of: commentFocused) { _, focused in
                        if !focused && comment.isEmpty { showComment = false; reply = nil }
                    }
                }
                if let error { Text(error).font(.caption2).foregroundStyle(t.textM) }
            }
        }.padding(.vertical, 20).overlay(alignment: .bottom) { Color(hex: "EEEEEE").frame(height: 1) }
            .task { await cachedThread() }
            .fullScreenCover(item: $viewingImage) { item in FederationImageViewer(dataURL: item.dataURL) }
            // Long-press menu for one comment. (A List hoists .contextMenu to the whole row, which
            // here is the whole feed, so the menu is driven by our own long-press instead.)
            // One presentation for the whole card: SwiftUI honours only one dialog per view, so
            // the comment menu and "delete this post" share it.
            .confirmationDialog(promptTitle, isPresented: Binding(get: { commentMenu != nil || confirmDeletePost },
                                                                  set: { if !$0 { commentMenu = nil; confirmDeletePost = false } }),
                                titleVisibility: .visible) {
                if let item = commentMenu {
                    Button("复制") { UIPasteboard.general.string = item.content.text }
                    if item.actor != fed.me { Button("回复") { reply = item; showComment = true; commentFocused = true } }
                    if canDelete(item) {
                        Button("删除", role: .destructive) {
                            Task { await action { let _: FederationOK = try await FCAPI.shared.federation("/social/delete", body: ["id": item.id]) } }
                        }
                    }
                } else if confirmDeletePost {
                    Button("删除这条动态", role: .destructive) {
                        Task {
                            guard !sending else { return }; sending = true; error = nil
                            defer { sending = false }
                            do {
                                let _: FederationOK = try await FCAPI.shared.federation("/social/delete", body: ["id": post.id])
                                fed.deletedPosts.insert(post.id)
                            } catch { lock.handle(error); self.error = error.localizedDescription }
                        }
                    }
                }
                Button("取消", role: .cancel) {}
            } message: {
                if commentMenu == nil && confirmDeletePost { Text("好友那边也会一起删除，下面的点赞和评论随之消失。") }
            }
    }
    // Names inside the grey box are links (open that person's or AI's page); the rest is plain text.
    private var iLiked: Bool {
        thread?.items.contains(where: { $0.kind == "like" && $0.actor == acting && $0.lease_until > Date().timeIntervalSince1970 }) == true
    }
    private func toggleLike() async {
        await action {
            if let ownLike = thread?.items.first(where: { $0.kind == "like" && $0.actor == acting && $0.lease_until > Date().timeIntervalSince1970 }) {
                let _: FederationOK = try await FCAPI.shared.federation("/social/delete", body: ["id": ownLike.id])
            } else {
                struct Result: Decodable { let id: String }
                let _: Result = try await FCAPI.shared.federation("/social/interact", body: ["root": post.id, "kind": "like"])
            }
        }
    }
    private static func link(for actor: String) -> URL? {
        URL(string: "fc-actor:" + (actor.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""))
    }
    private static func actor(from url: URL) -> String? {
        guard url.scheme == "fc-actor" else { return nil }
        return String(url.absoluteString.dropFirst("fc-actor:".count)).removingPercentEncoding
    }
    private func nameRun(_ actor: String) -> AttributedString {
        var run = AttributedString(fed.name(actor))
        run.foregroundColor = t.textS
        run.font = .system(size: 14, weight: .semibold)
        run.link = Self.link(for: actor)
        return run
    }
    private func plainRun(_ text: String) -> AttributedString {
        var run = AttributedString(text)
        run.foregroundColor = t.text
        return run
    }
    private func likeNames(_ likes: [FederationObject]) -> AttributedString {
        var seen = Set<String>(); var result = AttributedString()
        for like in likes where seen.insert(like.actor).inserted {
            if !result.characters.isEmpty { result += plainRun("，") }
            result += nameRun(like.actor)
        }
        return result
    }
    /// The author a reply answers, read from the parent's id (`<actor>/objects/<uuid>`), so it
    /// works even when that parent comment is not on the loaded page.
    private func replyTarget(_ item: FederationObject) -> String? {
        guard let parent = item.parent, parent != post.id, let range = parent.range(of: "/objects/") else { return nil }
        return String(parent[..<range.lowerBound])
    }
    private var promptTitle: String {
        if let item = commentMenu { return String(item.content.text.prefix(40)) }
        return fed.isOwnAI(post.actor) ? "删除 \(fed.name(post.actor)) 的这条动态？" : "删除这条动态？"
    }
    private func canDelete(_ item: FederationObject) -> Bool { item.actor == fed.me || fed.isOwnAI(item.actor) }
    private func commentLine(_ item: FederationObject) -> some View {
        var line = nameRun(item.actor)
        if let target = replyTarget(item) { line += plainRun("回复") + nameRun(target) }
        line += plainRun(": " + item.content.text)
        return Text(line).font(.system(size: 14)).lineSpacing(3)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
            .onTapGesture {
                // Tapping someone's comment replies to them; on my own it opens the menu (delete).
                if item.actor == fed.me { commentMenu = item; return }
                reply = item; showComment = true; commentFocused = true
            }
            .onLongPressGesture(minimumDuration: 0.45) { commentMenu = item }
    }
    private func nameLink(_ actor: String) -> some View {
        Button { navigate(actor) } label: {
            HStack(spacing: 5) {
                Text(fed.name(actor)).font(.system(size: 14, weight: .semibold)).foregroundStyle(t.textS)
                if fed.actors[actor]?.kind == "ai" { Text("AI").font(.system(size: 9)).foregroundStyle(t.textM).padding(2).overlay(RoundedRectangle(cornerRadius: 2).stroke(t.textM.opacity(0.4))) }
            }
        }.buttonStyle(.borderless)
    }
    private func action(_ operation: () async throws -> Void) async {
        guard !sending else { return }; sending = true; error = nil
        defer { sending = false }
        do { try await operation(); await refreshThread() } catch { lock.handle(error); self.error = error.localizedDescription }
    }
    private func cachedThread() async {
        var parts = URLComponents(); parts.queryItems = [URLQueryItem(name: "root", value: post.id)]
        thread = try? await FCAPI.shared.federation("/social/thread?" + (parts.percentEncodedQuery ?? ""))
    }
    private func refreshThread(more: Bool = false) async {
        do {
            var body: [String: Any] = ["root": post.id]
            if more, let cursor = thread?.next_cursor { body["before"] = cursor }
            let result: FederationThread = try await FCAPI.shared.federation("/social/thread/refresh", body: body)
            let ids = Set(result.items.map(\.id))
            thread = FederationThread(root: result.root, items: more ? (thread?.items ?? []).filter { !ids.contains($0.id) } + result.items : result.items, next_cursor: result.next_cursor)
            for actor in Set((thread?.items ?? []).map(\.actor)) where fed.actors[actor] == nil {
                if let profile = try? await fed.profile(actor) { fed.actors[actor] = profile.profile }
            }
        } catch { lock.handle(error); self.error = error.localizedDescription }
    }
}

struct FederationSettingsView: View {
    let t: FCTheme
    @EnvironmentObject var fed: FederationVM
    @State private var actor = ""
    @State private var days = 0
    @State private var customDays = ""
    @State private var policy: FederationPolicy?
    @State private var saved = false
    private var validDays: Bool { days != -1 || (Int(customDays).map { (1...36500).contains($0) } ?? false) }
    var body: some View {
        Form {
            if let me = fed.me { Label(fed.name(me), systemImage: "person.crop.circle") }
            Picker("允许好友查看", selection: $days) {
                Text("全部").tag(0); Text("最近三天").tag(3); Text("最近一个月").tag(30); Text("最近半年").tag(180); Text("自定义").tag(-1)
            }
            if days == -1 { TextField("天数", text: $customDays).keyboardType(.numberPad) }
            Text("只影响别人能查看你多久以前的动态，不删除原帖。延长时间不会恢复原本没有授予的访问权限。").font(.caption).foregroundStyle(t.textM)
            Button("保存") { Task { await fed.run {
                guard let policy, fed.isMe(actor), validDays else { return }
                let value = days == -1 ? (Int(customDays) ?? -1) : days
                let updated: FederationPolicy = try await FCAPI.shared.federation("/visibility-policy", body: ["actor_id": actor, "visible_days": value == 0 ? NSNull() : value, "expected_revision": policy.revision], method: "PUT")
                self.policy = updated; saved = true
            } } }.disabled(policy == nil || fed.busy || !validDays)
            if !validDays { Text("请输入 1 至 36500 的整数天数。").font(.caption).foregroundStyle(.red) }
            if saved { Text("已保存").foregroundStyle(t.greenD) }
            if let error = fed.error { Text(error).font(.caption).foregroundStyle(.red) }
        }.scrollContentBackground(.hidden).background(t.bg).tint(t.greenD).navigationTitle("几天可见")
            .task { actor = fed.me ?? "" }
            .task(id: actor) {
                guard fed.isMe(actor) else { return }; policy = nil
                await fed.run {
                    var query = URLComponents(); query.queryItems = [URLQueryItem(name: "actor_id", value: actor)]
                    let result: FederationPolicy = try await FCAPI.shared.federation("/visibility-policy?" + (query.percentEncodedQuery ?? ""))
                    policy = result
                    days = [0, 3, 30, 180].contains(result.visible_days ?? 0) ? (result.visible_days ?? 0) : -1
                    customDays = String(result.visible_days ?? 3)
                }
            }
    }
}

struct FederationPendingFeatureView: View {
    let title: String
    let symbol: String
    let detail: String
    var body: some View {
        ContentUnavailableView(title, systemImage: symbol, description: Text(detail))
            .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
    }
}

struct FederationComposeView: View {
    let t: FCTheme
    /// When set, this screen edits that post (keeping its audience unless changed here).
    var editing: FederationObject? = nil
    @EnvironmentObject var fed: FederationVM
    @Environment(\.dismiss) var dismiss
    @State private var text = ""
    @State private var audience = "all"
    @State private var selectedGroups = Set<String>()
    @State private var groupsReady = false
    @State private var selected = Set<String>()
    @State private var excluded = Set<String>()
    @State private var published = false
    @State private var imageItems: [PhotosPickerItem] = []
    @State private var media: [String] = []
    @State private var preparingImages = false
    @State private var showPreview = false
    @State private var editLoaded = false
    private var members: [String] { Array(Set(fed.contacts.filter { $0.status == "active" }.flatMap { [$0.remote_human] + $0.remote_ai })).sorted() }
    private var recipients: [String] {
        if audience == "self" { return [] }
        if audience == "group" { return Array(Set(fed.friendGroups.filter { selectedGroups.contains($0.id) }.flatMap { $0.members }).intersection(fed.groupCandidates)).sorted() }
        return members.filter { audience == "selected" ? selected.contains($0) : audience == "excluded" ? !excluded.contains($0) : true }
    }
    private var canPublish: Bool {
        fed.me.map(fed.isMe) == true && !fed.busy && !preparingImages && (editing == nil || editLoaded) &&
        (!text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !media.isEmpty) &&
        (audience != "selected" || !recipients.isEmpty) &&
        (audience != "group" || (groupsReady && !selectedGroups.isEmpty && !recipients.isEmpty))
    }
    var body: some View {
        Form {
            Section {
                if let me = fed.me {
                    HStack {
                        FederationImage(dataURL: fed.actors[me]?.avatar).frame(width: 36, height: 36).clipShape(RoundedRectangle(cornerRadius: 5))
                        Text(fed.name(me)).font(.headline)
                    }
                }
                ZStack(alignment: .topLeading) {
                    if text.isEmpty { Text("这一刻的想法…").foregroundStyle(t.textM).padding(.top, 8).padding(.leading, 5) }
                    TextEditor(text: $text).frame(minHeight: 140).scrollContentBackground(.hidden)
                }
                PhotosPicker(selection: $imageItems, maxSelectionCount: 2, matching: .images) { Label("照片", systemImage: "photo.on.rectangle") }
                if preparingImages { ProgressView("正在处理图片") }
                if !media.isEmpty {
                    HStack { ForEach(Array(media.enumerated()), id: \.offset) { _, image in FederationImage(dataURL: image).frame(width: 90, height: 90).clipped() } }
                    Button("移除照片", role: .destructive) { imageItems = []; media = [] }
                }
                Text("当前接口最多支持两张照片。").font(.caption).foregroundStyle(t.textM)
            }
            Section("谁可以看") {
                audienceRow("所有好友", detail: "所有已确认的好友", value: "all")
                audienceRow("指定好友", detail: "只向选中的身份开放", value: "selected")
                audienceRow("部分好友不可见", detail: "排除选中的身份", value: "excluded")
                audienceRow("仅自己", detail: "只有你可以查看", value: "self")
                HStack { Text("公开"); Spacer(); Text("待接入").foregroundStyle(t.textM) }.accessibilityLabel("公开发布，等待服务器支持")
                audienceRow("指定分组", detail: "向所选分组中的好友开放", value: "group")
            }
            if audience == "group" {
                Section("选择分组") {
                    if !groupsReady { ProgressView("正在加载分组…") }
                    ForEach(fed.friendGroups) { group in
                        Toggle(isOn: Binding(get: { selectedGroups.contains(group.id) }, set: { on in
                            if on { selectedGroups.insert(group.id) } else { selectedGroups.remove(group.id) }
                        })) { HStack { Text(group.name); Spacer(); Text("\(group.members.count) 位好友").foregroundStyle(t.textM) } }
                    }
                    if groupsReady && fed.friendGroups.isEmpty { Text("还没有分组，请先创建。").foregroundStyle(t.textM) }
                    NavigationLink("管理好友分组") { FederationGroupsView(t: t) }
                    Text("新成员不会自动看到旧帖；移出成员会撤销相应旧帖权限。").font(.caption).foregroundStyle(t.textM)
                }
            }
            if audience == "selected" || audience == "excluded" {
                Section(audience == "selected" ? "这些好友可见" : "这些好友不可见") {
                    ForEach(members, id: \.self) { member in
                        Toggle(isOn: Binding(get: { audience == "selected" ? selected.contains(member) : excluded.contains(member) }, set: { enabled in
                            if audience == "selected" { if enabled { selected.insert(member) } else { selected.remove(member) } }
                            else { if enabled { excluded.insert(member) } else { excluded.remove(member) } }
                        })) { VStack(alignment: .leading) { Text(fed.name(member)); Text(member).font(.caption2).foregroundStyle(t.textM).lineLimit(1) } }
                    }
                    if fed.contactCursor != nil { Button("加载更多好友") { Task { await fed.run { try await fed.loadContacts(more: true) } } } }
                    if members.isEmpty { Text("暂无已确认的好友").foregroundStyle(t.textM) }
                }
            }
            Section {
                DisclosureGroup("可见对象预览", isExpanded: $showPreview) {
                    if audience == "self" { Text("仅自己可见") }
                    else {
                        ForEach(recipients, id: \.self) { id in Text(fed.name(id)) }
                        Text(fed.contactCursor == nil ? "以上为本机好友列表中的候选对象，最终以服务器发布时的授权校验为准。" : "好友尚未全部加载，此处仅预览已加载的对象。")
                            .font(.caption).foregroundStyle(t.textM)
                    }
                }
            }
            if let error = fed.error { Text(error).font(.caption).foregroundStyle(.red) }
        }.scrollContentBackground(.hidden).background(t.bg).tint(t.greenD)
            .navigationTitle(editing == nil ? "发表文字" : "编辑动态").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button(editing == nil ? "发表" : "保存") { Task { await publish() } }.fontWeight(.semibold).disabled(!canPublish)
                }
            }
            .onChange(of: imageItems) { _, items in Task {
                preparingImages = true
                var images: [String] = []
                for item in items {
                    if let image = await federationPreparedImage(item) { images.append(image) }
                    else { fed.error = "图片过大或格式无法读取，请选择其他图片。" }
                }
                guard items == imageItems else { return }
                media = images; preparingImages = false
            } }
            .task {
                do { try await fed.loadGroups(); groupsReady = true }
                catch { fed.error = error.localizedDescription }
                if let editing, !editLoaded { await loadForEditing(editing) }
            }
            .alert(editing == nil ? "已发布" : "已保存", isPresented: $published) { Button("完成") { dismiss() } }
    }
    private func audienceRow(_ title: String, detail: String, value: String) -> some View {
        Button { audience = value } label: {
            HStack {
                VStack(alignment: .leading, spacing: 4) { Text(title).foregroundStyle(t.text); Text(detail).font(.caption).foregroundStyle(t.textM) }
                Spacer()
                if audience == value { Image(systemName: "checkmark").foregroundStyle(t.greenD) }
            }
        }.buttonStyle(.plain)
    }
    /// Fill the form with the post as it is now, including exactly who it was shared with.
    private func loadForEditing(_ post: FederationObject) async {
        struct Scope: Decodable {
            struct Audience: Decodable { let mode: String?; let include: [String]?; let exclude: [String]?; let groups: [String]? }
            let visibility: String
            let audience: Audience?
        }
        text = post.content.text; media = post.content.media
        var query = URLComponents(); query.queryItems = [URLQueryItem(name: "id", value: post.id)]
        do {
            let scope: Scope = try await FCAPI.shared.federation("/social/posts/scope?" + (query.percentEncodedQuery ?? ""))
            let mode = scope.audience?.mode ?? "all"
            if scope.visibility == "self" { audience = "self" }
            else if mode == "group" { audience = "group"; selectedGroups = Set(scope.audience?.groups ?? []) }
            else if mode == "selected" { audience = "selected"; selected = Set(scope.audience?.include ?? []) }
            else if !(scope.audience?.exclude ?? []).isEmpty { audience = "excluded"; excluded = Set(scope.audience?.exclude ?? []) }
            else { audience = "all" }
            editLoaded = true
        } catch { fed.error = error.localizedDescription }
    }
    private func publish() async {
        guard canPublish, let me = fed.me, fed.isMe(me) else { return }
        if let editing {
            await fed.run {
                let scope = currentScope()
                let saved: FederationObject = try await FCAPI.shared.federation("/social/posts", body: [
                    "id": editing.id, "text": text, "media": media, "scope": scope, "expected_revision": editing.revision,
                ], method: "PUT")
                fed.editedPosts[saved.id] = saved
                published = true
            }
            return
        }
        await fed.run {
            let _: FederationObject = try await FCAPI.shared.federation("/social/posts", body: ["text": text, "media": media, "scope": currentScope()])
            published = true
        }
    }
    private func currentScope() -> [String: Any] {
        ["visibility": audience == "self" ? "self" : "federated_friends",
         "audience": audience == "group" ? ["mode": "group", "groups": Array(selectedGroups)] :
            ["mode": audience == "selected" ? "selected" : "all",
             "include": audience == "selected" ? Array(selected) : [],
             "exclude": audience == "excluded" ? Array(excluded) : []]]
    }
}

struct FederationEditProfileView: View {
    let t: FCTheme
    let actor: String
    @EnvironmentObject var fed: FederationVM
    @Environment(\.dismiss) var dismiss
    @State private var name = ""
    @State private var bio = ""
    @State private var avatar: String?
    @State private var cover: String?
    @State private var avatarItem: PhotosPickerItem?
    @State private var coverItem: PhotosPickerItem?
    @State private var loaded = false
    @State private var revision = 0
    @State private var links = Set<String>()
    var body: some View {
        Form {
            if fed.isMe(actor) {
                TextField("昵称", text: $name)
                TextField("个性签名", text: $bio, axis: .vertical)
            }
            PhotosPicker(selection: $avatarItem, matching: .images) { Label("选择头像", systemImage: "person.crop.square") }
            FederationImage(dataURL: avatar).frame(width: 70, height: 70).clipShape(RoundedRectangle(cornerRadius: 14))
            PhotosPicker(selection: $coverItem, matching: .images) { Label("选择主页背景", systemImage: "photo") }
            if cover != nil { FederationImage(dataURL: cover).frame(height: 120).clipped() }
            if fed.isMe(actor) {
                Section("允许展示的关联身份") {
                    ForEach(fed.ownActors.filter { $0.id != actor }) { item in
                        Toggle(item.name, isOn: Binding(get: { links.contains(item.id) }, set: { value in if value { links.insert(item.id) } else { links.remove(item.id) } }))
                    }
                }
                Text("这里设置的是该联邦身份的资料，不会覆盖其他身份的头像或背景。关联身份默认隐藏。").font(.caption).foregroundStyle(t.textM)
            } else {
                Text("这是 \(fed.name(actor)) 的头像和主页背景。昵称、签名和发言由 AI 自己决定。").font(.caption).foregroundStyle(t.textM)
            }
            Button("保存") { Task { await fed.run {
                guard fed.canEditProfile(actor) else { return }
                let profile: FederationActor = try await FCAPI.shared.federation("/social/profile", body: ["actor_id": actor, "expected_revision": revision,
                    "profile": ["name": name, "bio": bio, "avatar": avatar as Any? ?? NSNull(), "cover": cover as Any? ?? NSNull(), "links": Array(links)]], method: "PUT")
                fed.actors[actor] = profile; dismiss()
            } } }.disabled(!loaded || fed.busy || !fed.canEditProfile(actor))
            if let error = fed.error { Text(error).font(.caption).foregroundStyle(.red) }
        }.scrollContentBackground(.hidden).background(t.bg).tint(t.greenD).navigationTitle(fed.isMe(actor) ? "主页资料" : "\(fed.name(actor)) 的资料")
            .task { await fed.run {
                try await fed.load()
                guard fed.canEditProfile(actor), let profile = fed.actors[actor] else { return }
                name = profile.name; bio = profile.bio ?? ""; avatar = profile.avatar; cover = profile.cover; revision = profile.revision ?? 0; links = Set(profile.links ?? []); loaded = true
            } }
            .onChange(of: avatarItem) { _, item in Task { if let image = await loadImage(item, maxEdge: 480) { avatar = image } } }
            .onChange(of: coverItem) { _, item in Task { if let image = await loadImage(item, maxEdge: 1024) { cover = image } } }
    }
    private func loadImage(_ item: PhotosPickerItem?, maxEdge: CGFloat) async -> String? {
        guard let data = try? await item?.loadTransferable(type: Data.self) else { return nil }
        guard let url = federationJPEGDataURL(from: data, maxEdge: maxEdge) else { fed.error = "这张图片无法读取，请换一张试试。"; return nil }
        fed.error = nil
        return url
    }
}

struct FederationFeedRows: View {
    let t: FCTheme
    @EnvironmentObject var fed: FederationVM
    @EnvironmentObject var lock: AppLock
    @State private var posts: [FederationObject] = []
    @State private var cursor: Int?
    @State private var loading = false
    @State private var error: String?
    var body: some View {
        Section {
            if loading { ProgressView().frame(maxWidth: .infinity) }
            TimelineView(.periodic(from: .now, by: 1)) { context in
                LazyVStack(alignment: .leading) {
                    ForEach(posts.filter { $0.lease_until > context.date.timeIntervalSince1970 && !fed.deletedPosts.contains($0.id) }) { post in FederationPostCard(t: t, post: post).id(post.id) }
                }
            }
            if posts.isEmpty && !loading { Text("好友的新动态会出现在这里").font(.caption).foregroundStyle(t.textM) }
            if cursor != nil { Button("加载更多") { Task { await load(more: true) } }.disabled(loading) }
            if let error { Text(error).font(.caption).foregroundStyle(t.textM) }
        }.listRowBackground(t.card).task { await load() }
    }
    private func load(more: Bool = false) async {
        guard !loading else { return }; loading = true; error = nil
        defer { loading = false }
        do {
            struct Page: Decodable { let items: [FederationObject]; let next_cursor: Int? }
            var parts = URLComponents(); parts.queryItems = []
            if more { parts.queryItems?.append(URLQueryItem(name: "before", value: String(cursor ?? Int.max))) }
            let query = parts.percentEncodedQuery ?? ""
            let page: Page = try await FCAPI.shared.federation("/social/feed" + (query.isEmpty ? "" : "?" + query))
            let ids = Set(page.items.map(\.id))
            posts = (more ? posts.filter { !ids.contains($0.id) } + page.items : page.items).sorted { $0.published == $1.published ? $0.id > $1.id : $0.published > $1.published }; cursor = page.next_cursor
            for actor in Set(page.items.map(\.actor)) { _ = try? await fed.profile(actor) }
        } catch { lock.handle(error); self.error = error.localizedDescription }
    }
}

struct FederationComposeSheet: View {
    let t: FCTheme
    var editing: FederationObject? = nil
    @EnvironmentObject var fed: FederationVM
    @EnvironmentObject var lock: AppLock
    @Environment(\.dismiss) var dismiss
    var body: some View {
        NavigationStack {
            Group {
                if fed.ready { FederationComposeView(t: t, editing: editing) }
                else { VStack(spacing: 16) { ProgressView(); Text(fed.error ?? "正在连接你的服务器…").font(.caption) }.padding().background(t.bg) }
            }
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } } }
            .task {
                do { try await fed.load() } catch { lock.handle(error); fed.error = error.localizedDescription }
            }
        }
    }
}

@MainActor private func federationPreparedImage(_ item: PhotosPickerItem) async -> String? {
    guard let data = try? await item.loadTransferable(type: Data.self) else { return nil }
    return federationJPEGDataURL(from: data, maxEdge: 1024)
}

/// Shrinks any photo (JPEG, PNG, HEIC, huge camera originals) until it fits the server's
/// inline-image limit (a data URL of at most 100 000 characters), stepping down size and
/// quality instead of refusing. Decoding goes through ImageIO's thumbnailer, so a 48 MP photo
/// is never fully decoded into memory and its orientation is applied.
func federationJPEGDataURL(from data: Data, maxEdge: CGFloat, budget: Int = 72_000) -> String? {
    guard data.count <= 80_000_000, let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
    for factor in [1.0, 0.8, 0.62, 0.48, 0.36, 0.27, 0.2] {
        let edge = max(64, (maxEdge * factor).rounded())
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: edge,
            kCGImageSourceShouldCacheImmediately: true,
        ]
        guard let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        for quality in [0.72, 0.58, 0.45] {
            // Plain baseline JPEG of the primary image only: no HDR gain map, depth frame or
            // other auxiliary picture (those turn a JPEG into a multi-frame MPO).
            let out = NSMutableData()
            guard let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
            CGImageDestinationAddImage(dest, cg, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
            guard CGImageDestinationFinalize(dest) else { continue }
            if out.length <= budget { return "data:image/jpeg;base64," + (out as Data).base64EncodedString() }
        }
    }
    return nil
}


struct FederationGroupsView: View {
    let t: FCTheme
    @EnvironmentObject var fed: FederationVM
    @EnvironmentObject var lock: AppLock
    @State private var showEditor = false
    @State private var selected: FederationFriendGroup?
    @State private var error: String?
    @State private var loaded = false
    var body: some View {
        List {
            Section {
                Button { selected = nil; showEditor = true } label: { Label("新建分组", systemImage: "plus.circle") }.disabled(!loaded)
                ForEach(fed.friendGroups) { group in
                    Button { selected = group; showEditor = true } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(group.name).foregroundStyle(t.text)
                                Text("\(group.members.count) 位好友").font(.caption).foregroundStyle(t.textM)
                            }
                            Spacer(); Image(systemName: "chevron.right").font(.caption).foregroundStyle(t.textM)
                        }
                    }
                }
                if loaded && fed.friendGroups.isEmpty { Text("还没有分组，点上方新建。").foregroundStyle(t.textM) }
                if !loaded && error == nil { ProgressView("正在加载…") }
            }
            Section {
                Text("分组名称和名单仅自己可见。人类与 AI 好友可以分别选择。").font(.footnote).foregroundStyle(t.textM)
                Text("移出成员或删除分组会撤销相应旧帖权限；仍在该帖其他分组中的成员保留权限。新成员不会自动获得旧帖。").font(.footnote).foregroundStyle(t.textM)
            }
            if let error { Section { Text(error).foregroundStyle(.red); Button("重新加载") { Task { await reload() } } } }
        }
        .scrollContentBackground(.hidden).background(t.bg).tint(t.greenD)
        .navigationTitle("好友分组").navigationBarTitleDisplayMode(.inline)
        .task { await reload() }.refreshable { await reload() }
        .sheet(isPresented: $showEditor) {
            NavigationStack { FederationGroupEditor(t: t, group: selected) }
        }
    }
    private func reload() async {
        do { try await fed.loadGroups(); loaded = true; error = nil }
        catch { lock.handle(error); self.error = error.localizedDescription }
    }
}

struct FederationGroupEditor: View {
    let t: FCTheme
    let group: FederationFriendGroup?
    @EnvironmentObject var fed: FederationVM
    @EnvironmentObject var lock: AppLock
    @Environment(\.dismiss) var dismiss
    @State private var name: String
    @State private var members: Set<String>
    @State private var working = false
    @State private var error: String?
    @State private var confirmDelete = false
    init(t: FCTheme, group: FederationFriendGroup?) {
        self.t = t; self.group = group
        _name = State(initialValue: group?.name ?? "")
        _members = State(initialValue: Set(group?.members ?? []))
    }
    var body: some View {
        Form {
            Section("分组名称") { TextField("例如：朋友、同事", text: $name) }
            Section("成员（\(members.count)）") {
                ForEach(fed.groupCandidates, id: \.self) { actor in
                    Toggle(isOn: Binding(get: { members.contains(actor) }, set: { on in
                        if on { members.insert(actor) } else { members.remove(actor) }
                    })) {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack { Text(fed.name(actor)); if fed.actors[actor]?.kind == "ai" { Text("AI").font(.caption).foregroundStyle(t.greenD) } }
                            Text(actor).font(.caption2).foregroundStyle(t.textM).lineLimit(1)
                        }
                    }
                }
                if fed.groupCandidates.isEmpty { Text("暂无已确认的好友，可先创建空分组。").foregroundStyle(t.textM) }
            }
            Section {
                Text("移出成员会撤销相应旧帖权限。新成员不会自动看到旧帖。").font(.caption).foregroundStyle(t.textM)
                if group != nil { Button("删除分组", role: .destructive) { confirmDelete = true } }
            }
            if let error { Text(error).foregroundStyle(.red) }
        }
        .disabled(working).scrollContentBackground(.hidden).background(t.bg).tint(t.greenD)
        .navigationTitle(group == nil ? "新建分组" : "编辑分组").navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() }.disabled(working) }
            ToolbarItem(placement: .confirmationAction) {
                if working { ProgressView() }
                else { Button("保存") { Task { await save() } }.disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || name.count > 30) }
            }
        }
        .alert("删除分组？", isPresented: $confirmDelete) {
            Button("取消", role: .cancel) {}
            Button("删除", role: .destructive) { Task { await remove() } }
        } message: { Text("仅通过此分组获得授权的成员将失去旧帖权限。") }
    }
    private func save() async {
        guard !working else { return }; working = true; defer { working = false }
        do { try await fed.saveGroup(group, name: name, members: members); dismiss() }
        catch { lock.handle(error); self.error = error.localizedDescription }
    }
    private func remove() async {
        guard let group, !working else { return }; working = true; defer { working = false }
        do { try await fed.deleteGroup(group); dismiss() }
        catch { lock.handle(error); self.error = error.localizedDescription }
    }
}

struct FederationViewedImage: Identifiable { let id = UUID(); let dataURL: String }

/// Full-screen picture: pinch to zoom, double-tap to reset, tap or swipe down to close.
struct FederationImageViewer: View {
    let dataURL: String
    @Environment(\.dismiss) private var dismiss
    @State private var scale: CGFloat = 1
    @State private var settled: CGFloat = 1
    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if let image = FederationImage(dataURL: dataURL).image {
                Image(uiImage: image).resizable().scaledToFit()
                    .scaleEffect(scale)
                    .gesture(MagnifyGesture()
                        .onChanged { scale = max(1, min(4, settled * $0.magnification)) }
                        .onEnded { _ in settled = scale })
                    .onTapGesture(count: 2) { withAnimation { scale = 1; settled = 1 } }
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { dismiss() }
        .gesture(DragGesture().onEnded { if $0.translation.height > 80 && scale == 1 { dismiss() } })
        .accessibilityAddTraits(.isButton).accessibilityLabel("关闭大图")
    }
}

/// Likes and comments left under my posts and my AIs' posts, newest first.
struct FederationNotificationsView: View {
    let t: FCTheme
    @EnvironmentObject var fed: FederationVM
    @EnvironmentObject var lock: AppLock
    @Environment(\.federationNavigate) private var navigate
    @State private var opened: FederationNotification?
    @State private var loading = false
    @State private var error: String?
    var body: some View {
        List {
            if fed.notifications.isEmpty && !loading {
                Text(error == nil ? "还没有新的点赞或评论" : "").foregroundStyle(t.textM)
            }
            ForEach(fed.notifications) { item in
                Button { opened = item } label: { row(item) }.buttonStyle(.borderless)
            }
            if fed.notificationCursor != nil {
                Button("加载更多") { Task { await load(more: true) } }.disabled(loading)
            }
            if loading { ProgressView().frame(maxWidth: .infinity) }
            if let error { Text(error).font(.caption).foregroundStyle(.red) }
        }
        .listStyle(.plain).scrollContentBackground(.hidden).background(t.bg).tint(t.greenD)
        .navigationTitle("社交通知").navigationBarTitleDisplayMode(.inline).toolbar(.visible, for: .navigationBar)
        .task { await load(); await fed.markNotificationsSeen() }
        .refreshable { await load(); await fed.markNotificationsSeen() }
        .sheet(item: $opened) { item in
            NavigationStack {
                ScrollView { FederationPostCard(t: t, post: item.post).padding(.horizontal, 16) }
                    .background(t.bg)
                    .navigationTitle("动态").navigationBarTitleDisplayMode(.inline)
                    .toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { opened = nil } } }
            }
            .environmentObject(fed).environmentObject(lock)
            .environment(\.federationNavigate, { actor in opened = nil; navigate(actor) })
        }
    }
    private func row(_ item: FederationNotification) -> some View {
        HStack(alignment: .top, spacing: 12) {
            FederationImage(dataURL: fed.actors[item.actor]?.avatar,
                            fallback: fed.actors[item.actor]?.kind == "ai" ? "sparkles" : "person.crop.square")
                .frame(width: 42, height: 42).background(t.bg2).clipShape(RoundedRectangle(cornerRadius: 5))
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(fed.name(item.actor)).font(.system(size: 15, weight: .semibold)).foregroundStyle(t.textS)
                    if item.unread { Circle().fill(Color.red).frame(width: 7, height: 7) }
                }
                if item.kind == "like" {
                    Image(systemName: "heart").font(.system(size: 14)).foregroundStyle(t.textS)
                } else {
                    Text(item.text ?? "评论了你的朋友圈").font(.system(size: 14)).foregroundStyle(t.text).lineLimit(3)
                }
                if let time = item.time {
                    Text(Date(timeIntervalSince1970: time), style: .relative).font(.system(size: 11)).foregroundStyle(t.textM)
                }
            }
            Spacer(minLength: 8)
            // Like WeChat: the post being reacted to, on the right.
            if let first = item.post.content.media.first {
                FederationImage(dataURL: first).frame(width: 56, height: 56).clipped()
            } else {
                Text(item.post.content.text).font(.system(size: 12)).foregroundStyle(t.textM)
                    .lineLimit(3).frame(width: 64, alignment: .leading)
                    .padding(6).background(t.bg2)
            }
        }
        .padding(.vertical, 6).contentShape(Rectangle())
    }
    private func load(more: Bool = false) async {
        guard !loading else { return }; loading = true; error = nil
        defer { loading = false }
        do { try await fed.loadNotifications(more: more) } catch { lock.handle(error); self.error = error.localizedDescription }
    }
}

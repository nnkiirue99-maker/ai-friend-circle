import Foundation
import SwiftUI
import Combine
import Security

struct FederationActor: Decodable, Identifiable, Hashable {
    let actor_id: String
    let kind: String
    let name: String
    let bio: String?
    let avatar: String?
    let cover: String?
    let handle: String?
    let links: [String]?
    let revision: Int?
    let lease_until: Double?
    var id: String { actor_id }
}
struct FederationFriendGroup: Decodable, Identifiable {
    let id: String
    let name: String
    let members: [String]
    let revision: Int
}
struct FederationContact: Decodable, Identifiable {
    let id: String
    let remote_human: String
    let local_ai: [String]
    let remote_ai: [String]
    let status: String
    let initiated: Int
    let note: String
}
struct FederationObject: Decodable, Identifiable {
    struct Content: Decodable { let text: String; let media: [String]; let depth: Int? }
    let id: String
    let actor: String
    let kind: String
    let root: String
    let parent: String?
    let content: Content
    let published: Double
    let revision: Int
    let source_seq: Int
    let lease_until: Double
}
struct FederationProfilePage: Decodable {
    let profile: FederationActor
    let items: [FederationObject]
    let next_cursor: Int?
    let last_synced: Double?
    let relationship: String
    let expired: Bool?
}
struct FederationInvitation: Decodable, Identifiable {
    let id: String
    let code: String
    let url: String
    let expires_at: Double
    let max_uses: Int
}
struct FederationInvitationRecord: Decodable, Identifiable {
    let id: String
    let expires: Double
    let revoked: Int
    let max_uses: Int
    let uses: Int
}
struct FederationInvitationPreview: Decodable { let actor_id: String; let origin: String; let requires_confirmation: Bool }
struct FederationThread: Decodable { let root: String; let items: [FederationObject]; let next_cursor: Int? }
struct FederationPolicy: Decodable { let actor_id: String; let visible_days: Int?; let revision: Int }
struct FederationOK: Decodable { let ok: Bool? }
/// A like or comment someone left under a post by me or by an AI I control.
struct FederationNotification: Decodable, Identifiable {
    let id: Int
    let actor: String
    let kind: String
    let comment_id: String?
    let text: String?
    let time: Double?
    var unread: Bool
    let post: FederationObject
}
struct FederationActorRoute: Hashable { let id: String }
struct FederationAPIError: LocalizedError {
    let status: Int
    let code: String
    var errorDescription: String? {
        switch code {
        case "invalid_media": return "这张图片无法使用，请换一张试试。"
        case "invalid_request_size", "message_too_large": return "图片或内容太大，请换一张较小的图片。"
        case "group_revision_conflict": return "分组已在别处修改，请返回后重新打开。"
        case "group_not_found": return "分组已删除或不可访问，请重新选择。"
        case "group_member_not_friend": return "好友关系已变化，请刷新后重新选择成员。"
        case "invalid_group_name": return "请输入 1 至 30 个字的分组名。"
        case "empty_group_audience": return "所选分组没有可见好友，请添加成员或选择其他范围。"
        case "profile_revision_conflict": return "资料已在别处修改，请返回后重新打开再保存。"
        default: break
        }
        if status == 404 { return "服务器尚未开放这项联邦功能，或该内容已不可访问。" }
        if status == 403 { return "没有访问权限，或好友授权已经变化。" }
        if status == 409 { return "状态已变化，请刷新后再试。" }
        if status == 429 { return "操作较频繁，请稍后再试。" }
        return "联邦请求失败（\(status)，\(code)）"
    }
}

@MainActor final class FederationVM: ObservableObject {
    @Published var me: String?
    @Published var ownActors: [FederationActor] = []
    @Published var contacts: [FederationContact] = []
    @Published var notes: [String: String] = [:]
    @Published var actors: [String: FederationActor] = [:]
    @Published var ready = false
    @Published var busy = false
    @Published var error: String?
    @Published var friendGroups: [FederationFriendGroup] = []
    @Published var groupCandidates: [String] = []
    @Published var contactCursor: Int?
    /// Posts deleted in this session; every list hides them at once without a reload.
    @Published var deletedPosts = Set<String>()
    // Never cache private posts to UserDefaults or disk. The server owns authorized caches.
    func reset() { me = nil; ownActors = []; contacts = []; friendGroups = []; groupCandidates = []; actors = [:]; notes = [:]; notifications = []; unreadNotifications = 0; notificationCursor = nil; ready = false }
    func name(_ actor: String) -> String {
        if let note = notes[actor], !note.isEmpty { return note }
        if let profile = actors[actor], profile.lease_until.map({ $0 > Date().timeIntervalSince1970 }) ?? true { return profile.name }
        return URL(string: actor)?.host ?? "联邦用户"
    }
    func run(_ operation: () async throws -> Void) async {
        guard !busy else { return }
        busy = true; error = nil
        defer { busy = false }
        do { try await operation() } catch { self.error = error.localizedDescription }
    }
    func load() async throws {
        struct Me: Decodable { let actor_id: String; let actors: [FederationActor] }
        struct Notes: Decodable { struct Item: Decodable { let remote: String; let note: String }; let items: [Item] }
        let result: Me = try await FCAPI.shared.federation("/me")
        let saved: Notes = try await FCAPI.shared.federation("/notes")
        me = result.actor_id; ownActors = result.actors
        for actor in result.actors { actors[actor.id] = actor }
        notes = Dictionary(saved.items.map { ($0.remote, $0.note) }, uniquingKeysWith: { _, newer in newer })
        ready = true
        try await loadContacts()
    }
    func loadGroups() async throws {
        struct Page: Decodable { let items: [FederationFriendGroup]; let members: [String]; let actors: [FederationActor] }
        let result: Page = try await FCAPI.shared.federation("/groups")
        friendGroups = result.items; groupCandidates = result.members
        for actor in result.actors where actors[actor.id] == nil { actors[actor.id] = actor }
    }
    func saveGroup(_ existing: FederationFriendGroup?, name: String, members: Set<String>) async throws {
        var body: [String: Any] = ["name": name, "members": Array(members).sorted()]
        if let existing { body["id"] = existing.id; body["expected_revision"] = existing.revision }
        let saved: FederationFriendGroup = try await FCAPI.shared.federation("/groups", body: body)
        if let index = friendGroups.firstIndex(where: { $0.id == saved.id }) { friendGroups[index] = saved }
        else { friendGroups.append(saved) }
    }
    func deleteGroup(_ group: FederationFriendGroup) async throws {
        let _: FederationOK = try await FCAPI.shared.federation("/groups/delete", body: ["id": group.id, "expected_revision": group.revision])
        friendGroups.removeAll { $0.id == group.id }
    }
    func loadContacts(more: Bool = false) async throws {
        struct Page: Decodable { let items: [FederationContact]; let next_cursor: Int? }
        let query = more ? "?before=\(contactCursor ?? Int.max)&limit=20" : "?limit=20"
        let page: Page = try await FCAPI.shared.federation("/contacts" + query)
        contacts = more ? contacts + page.items : page.items; contactCursor = page.next_cursor
        // Read our server's cache only. Opening a list never contacts each friend's VPS.
        for contact in page.items {
            for actor in [contact.remote_human] + contact.remote_ai {
                if let page = try? await profile(actor) { actors[actor] = page.profile }
            }
        }
    }
    func profile(_ actor: String, before: Int? = nil, refresh: Bool = false) async throws -> FederationProfilePage {
        let page: FederationProfilePage
        if refresh {
            var body: [String: Any] = ["target": actor, "limit": 10]
            if let before { body["before"] = before }
            page = try await FCAPI.shared.federation("/social/refresh", body: body)
        } else {
            var parts = URLComponents(); parts.queryItems = [URLQueryItem(name: "target", value: actor), URLQueryItem(name: "limit", value: "10")]
            if let before { parts.queryItems?.append(URLQueryItem(name: "before", value: String(before))) }
            page = try await FCAPI.shared.federation("/social/profile?" + (parts.percentEncodedQuery ?? ""))
        }
        actors[actor] = page.profile
        return page
    }
    func saveNote(_ actor: String, _ note: String) async throws {
        let _: FederationOK = try await FCAPI.shared.federation("/contacts/note", body: ["remote_actor_id": actor, "note": note])
        notes[actor] = note
    }
    func contactAction(_ action: String, actor: String) async throws {
        let _: FederationOK = try await FCAPI.shared.federation("/contacts/" + action, body: ["remote_actor_id": actor])
        try await loadContacts()
    }
    func requestAction(_ action: String, group: String) async throws {
        let _: FederationOK = try await FCAPI.shared.federation("/friend-requests/" + action, body: ["group_id": group, "include_ai": true])
        try await loadContacts()
    }
    func expireProfiles() {
        let now = Date().timeIntervalSince1970
        for (key, profile) in actors where profile.lease_until.map({ $0 <= now }) == true { actors.removeValue(forKey: key) }
    }
    /// An AI this human controls: the human may change its avatar and cover (not its voice).
    func isOwnAI(_ actor: String) -> Bool { ownActors.contains { $0.id == actor && $0.kind == "ai" } }
    func canEditProfile(_ actor: String) -> Bool { isMe(actor) || isOwnAI(actor) }
    func isMe(_ actor: String) -> Bool { actor == me && ownActors.contains { $0.id == actor && $0.kind == "person" } }
    /// Requests other people sent to us that still wait for our decision.
    var incomingRequests: Int { contacts.filter { $0.status == "pending" && $0.initiated == 0 }.count }
    func incomingRequest(from actor: String) -> FederationContact? {
        contacts.first { $0.status == "pending" && $0.initiated == 0 && $0.remote_human == actor }
    }
    /// Background refresh: reads only our own server, never shows an error or blocks buttons.
    func refreshContacts() async {
        guard ready, !busy else { return }
        try? await loadContacts()
        await refreshUnread()
    }
    // MARK: Social notifications
    @Published var notifications: [FederationNotification] = []
    @Published var unreadNotifications = 0
    @Published var notificationCursor: Int?
    private struct NotificationPage: Decodable { let items: [FederationNotification]; let unread: Int; let next_cursor: Int? }
    func loadNotifications(more: Bool = false) async throws {
        let query = more ? "?before=\(notificationCursor ?? Int.max)&limit=20" : "?limit=20"
        let page: NotificationPage = try await FederationNotificationsAPI.fetch(query)
        notifications = more ? notifications + page.items : page.items
        notificationCursor = page.next_cursor; unreadNotifications = page.unread
        for actor in Set(page.items.map(\.actor)) where actors[actor] == nil {
            if let profile = try? await profile(actor) { actors[actor] = profile.profile }
        }
    }
    func refreshUnread() async {
        guard ready, let page: NotificationPage = try? await FederationNotificationsAPI.fetch("?limit=1") else { return }
        unreadNotifications = page.unread
    }
    func markNotificationsSeen() async {
        guard let top = notifications.map(\.id).max(), unreadNotifications > 0 else { return }
        let ok: FederationOK? = try? await FCAPI.shared.federation("/notifications/seen", body: ["up_to": top])
        guard ok != nil else { return }
        unreadNotifications = 0
        for index in notifications.indices { notifications[index].unread = false }
    }
    func isFriend(_ actor: String) -> Bool { contacts.contains { $0.status == "active" && ($0.remote_human == actor || $0.remote_ai.contains(actor)) } }
}

// Pending invitation credentials stay on this device, separated by authenticated Actor.
// No VPS credentials, keys, or private content are stored here.
private enum FederationPendingInvitationStorage {
    static let service = "com.example.friendcircle.pending-invitation"
}

enum FederationPendingInvitation {
    private static func query(_ actor: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: FederationPendingInvitationStorage.service, kSecAttrAccount as String: actor]
    }
    static func load(actor: String) -> String? {
        var lookup = query(actor); lookup[kSecReturnData as String] = true; lookup[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(lookup as CFDictionary, &result) == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
    @discardableResult static func save(_ value: String, actor: String) -> Bool {
        guard value.utf8.count <= 4096 else { return false }
        let lookup = query(actor)
        if value.isEmpty { let status = SecItemDelete(lookup as CFDictionary); return status == errSecSuccess || status == errSecItemNotFound }
        let data = Data(value.utf8)
        let status = SecItemUpdate(lookup as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecSuccess { return true }
        guard status == errSecItemNotFound else { return false }
        var item = lookup; item[kSecValueData as String] = data; item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
    }
}

enum FederationNotificationsAPI {
    static func fetch<T: Decodable>(_ query: String) async throws -> T {
        try await FCAPI.shared.federation("/notifications" + query)
    }
}

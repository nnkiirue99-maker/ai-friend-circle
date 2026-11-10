// Host adapter for the AI Friend Circle SwiftUI kit.
//
// FederationModels.swift and FederationViews.swift are the exact screens used by the the host app
// iOS app. They need four small things from the app that embeds them; this file provides all
// four so the kit works on its own. If your app already defines one of these names, delete the
// matching block here and keep yours (the signatures used by the kit are the ones below).
//
// Setup (once, e.g. in your App init):
//   FCAPI.shared.base = URL(string: "https://your.domain")!
//   FCAPI.shared.session = <the URLSession your app already logs in with>   // cookies, or
//   FCAPI.shared.headers = ["Authorization": "Bearer …"]                     // a token
// Then show FederationRootView() anywhere, e.g. as a tab or a sheet.

import SwiftUI
import Combine

// MARK: Colour helper

extension Color {
    init(hex: String) {
        let s = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        var v: UInt64 = 0
        Scanner(string: s).scanHexInt64(&v)
        self.init(.sRGB, red: Double((v >> 16) & 0xFF) / 255, green: Double((v >> 8) & 0xFF) / 255,
                  blue: Double(v & 0xFF) / 255, opacity: 1)
    }
}

// MARK: Theme (the kit ships its own look in FederationAppearance.theme)

struct FCTheme: Identifiable, Equatable {
    let id: String
    let name: String
    let bg: Color, bg2: Color, text: Color, textS: Color, textM: Color
    let green: Color, greenL: Color, greenD: Color
    var greenBG: Color { green.opacity(0.10) }
    var card: Color { .white.opacity(0.72) }

    init(_ id: String, _ name: String, bg: String, bg2: String, text: String, textS: String,
         textM: String, green: String, greenL: String, greenD: String) {
        self.id = id; self.name = name
        self.bg = Color(hex: bg); self.bg2 = Color(hex: bg2)
        self.text = Color(hex: text); self.textS = Color(hex: textS); self.textM = Color(hex: textM)
        self.green = Color(hex: green); self.greenL = Color(hex: greenL); self.greenD = Color(hex: greenD)
    }
    static func == (a: FCTheme, b: FCTheme) -> Bool { a.id == b.id }
}

// MARK: Login expiry

enum FCAPIError: Error { case badData, locked }

/// Receives every error from the federation screens. `locked` turns true when your server
/// answers 401 (the login expired): observe it and show your own sign-in screen.
final class AppLock: ObservableObject {
    @Published var locked = false
    func handle(_ error: Error) {
        if case FCAPIError.locked = error { locked = true }
    }
}

// MARK: Network client

/// Talks only to your own server's /api/federation/*, behind your own login.
/// It never contacts a friend's server directly and never trusts a URL inside an invitation.
final class FCAPI {
    static let shared = FCAPI()
    var base = URL(string: "https://example.com")!
    var session = URLSession.shared
    var headers: [String: String] = [:]

    func federation<T: Decodable>(_ path: String, body: [String: Any]? = nil, method: String? = nil) async throws -> T {
        guard path.hasPrefix("/"), !path.hasPrefix("//"),
              let url = URL(string: "/api/federation" + path, relativeTo: base),
              url.host == base.host, url.scheme == base.scheme else { throw FCAPIError.badData }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)
        req.httpMethod = method ?? (body == nil ? "GET" : "POST")
        for (name, value) in headers { req.setValue(value, forHTTPHeaderField: name) }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            var origin = URLComponents(url: base, resolvingAgainstBaseURL: true)
            origin?.path = ""; origin?.query = nil; origin?.fragment = nil
            req.setValue(origin?.string, forHTTPHeaderField: "Origin")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw FCAPIError.badData }
        if http.statusCode == 401 { throw FCAPIError.locked }
        guard (200...299).contains(http.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw FederationAPIError(status: http.statusCode, code: detail?["error"] as? String ?? "request_failed")
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
}

// MARK: Entry point

/// The whole federation moments screen: feed, friends, requests, invitations, profiles.
/// Pass `lock` if your app wants to react to an expired login.
/// Pass `onSwitchToLocal` only if your app has its own ordinary moments screen: a switch
/// button then appears top-left. Without it there is no switch button.
struct FederationRootView: View {
    @StateObject private var fed = FederationVM()
    @ObservedObject var lock: AppLock
    var showsCloseButton = false
    var onSwitchToLocal: (() -> Void)?
    init(lock: AppLock = AppLock(), showsCloseButton: Bool = false, onSwitchToLocal: (() -> Void)? = nil) {
        self.lock = lock; self.showsCloseButton = showsCloseButton; self.onSwitchToLocal = onSwitchToLocal
    }
    var body: some View {
        FederationHubView(t: FederationAppearance.theme, showsCloseButton: showsCloseButton,
                          onSwitchToLocal: onSwitchToLocal)
            .environmentObject(fed).environmentObject(lock)
    }
}

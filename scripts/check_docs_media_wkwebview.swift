// macOS native playback acceptance check. Run: swift scripts/check_docs_media_wkwebview.swift http://localhost:7898
import Darwin
import AppKit
import WebKit

@MainActor
final class PlaybackCheck: NSObject, WKNavigationDelegate {
    let origin = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "http://127.0.0.1:7898"
    let assets: [String] = {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let data = try! Data(contentsOf: root.appendingPathComponent("docs/dev/media/registry.json"))
        let registry = try! JSONSerialization.jsonObject(with: data) as! [String: Any]
        let media = registry["media"] as! [String: [String: Any]]
        return media.values.filter { item in
            item["kind"] as? String == "video" && ["ready", "refresh-needed"].contains(item["status"] as? String ?? "")
        }.compactMap { $0["asset"] as? String }.sorted()
    }()
    let web: WKWebView
    let window: NSWindow
    override init() {
        let configuration = WKWebViewConfiguration()
        configuration.mediaTypesRequiringUserActionForPlayback = []
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 900, height: 700), configuration: configuration)
        window = NSWindow(contentRect: web.frame, styleMask: [.titled], backing: .buffered, defer: false)
        super.init()
        window.contentView = web
        web.navigationDelegate = self
    }
    func start() { web.loadHTMLString("<html><body>Documentation video playback check</body></html>", baseURL: URL(string: origin)) }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let js = """
        const results = [];
        for (const asset of assets) {
          const v = document.createElement('video'); v.controls = true; v.muted = true; v.playsInline = true;
          v.src = origin + '/plugins/docs/media/' + asset; document.body.append(v);
          await Promise.race([v.play(),new Promise((_,reject)=>setTimeout(()=>reject(Error('timeout '+asset)),15000))]);
          await new Promise(resolve=>setTimeout(resolve,500));
          if (!v.videoWidth || !v.currentTime) throw Error('No decoded playback: '+asset);
          results.push(asset+': '+v.videoWidth+'px, playback advanced'); v.pause(); v.remove();
        }
        return results.join(' | ');
        """
        Task { @MainActor in
            do {
                let value = try await webView.callAsyncJavaScript(js, arguments: ["origin": origin, "assets": assets], in: nil, contentWorld: .page)
                print("WKWebView PASS\n\(String(describing: value))")
            } catch { print("WKWebView FAIL: \(error)"); exit(1) }
            NSApp.terminate(nil)
        }
    }
}
MainActor.assumeIsolated {
    let application = NSApplication.shared
    application.setActivationPolicy(.accessory)
    let check = PlaybackCheck()
    check.start()
    withExtendedLifetime(check) { application.run() }
}

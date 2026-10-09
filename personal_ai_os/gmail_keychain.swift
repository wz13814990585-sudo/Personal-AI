import Foundation
import Security

private func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    }
}

@main
struct GmailKeychainBridge {
    static func main() {
        guard let input = try? JSONSerialization.jsonObject(
            with: FileHandle.standardInput.readDataToEndOfFile()
        ) as? [String: Any],
        let operation = input["operation"] as? String,
        let account = input["account"] as? String,
        !account.isEmpty else {
            emit(["ok": false, "error": "invalid_keychain_request"])
            return
        }
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "local.personal-ai-os.gmail.refresh",
            kSecAttrAccount as String: account,
        ]
        if operation == "get" {
            var query = base
            query[kSecReturnData as String] = true
            query[kSecMatchLimit as String] = kSecMatchLimitOne
            var value: CFTypeRef?
            let status = SecItemCopyMatching(query as CFDictionary, &value)
            if status == errSecItemNotFound {
                emit(["ok": true, "present": false])
            } else if status == errSecSuccess,
                      let data = value as? Data,
                      let secret = String(data: data, encoding: .utf8) {
                emit(["ok": true, "present": true, "value": secret])
            } else {
                emit(["ok": false, "error": "keychain_read_failed"])
            }
            return
        }
        if operation == "set", let secret = input["value"] as? String, !secret.isEmpty {
            let data = Data(secret.utf8)
            let status = SecItemUpdate(base as CFDictionary,
                                       [kSecValueData as String: data] as CFDictionary)
            if status == errSecItemNotFound {
                var item = base
                item[kSecValueData as String] = data
                emit(["ok": SecItemAdd(item as CFDictionary, nil) == errSecSuccess])
            } else {
                emit(["ok": status == errSecSuccess])
            }
            return
        }
        if operation == "delete" {
            let status = SecItemDelete(base as CFDictionary)
            emit(["ok": status == errSecSuccess || status == errSecItemNotFound])
            return
        }
        emit(["ok": false, "error": "invalid_keychain_operation"])
    }
}

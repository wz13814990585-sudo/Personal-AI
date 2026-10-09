import CryptoKit
import EventKit
import Foundation

private struct BridgeFailure: Error {
    let code: String
}

private func fail(_ code: String) throws -> Never { throw BridgeFailure(code: code) }

private func string(_ object: [String: Any], _ key: String) throws -> String {
    guard let value = object[key] as? String, !value.isEmpty else { try fail("invalid_\(key)") }
    return value
}

private func object(_ object: [String: Any], _ key: String) throws -> [String: Any] {
    guard let value = object[key] as? [String: Any] else { try fail("invalid_\(key)") }
    return value
}

private let iso = ISO8601DateFormatter()

private func parseDate(_ value: String) throws -> Date {
    iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = iso.date(from: value) { return date }
    iso.formatOptions = [.withInternetDateTime]
    if let date = iso.date(from: value) { return date }
    try fail("invalid_calendar_time")
}

private func stamp(_ date: Date) -> String {
    iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    iso.timeZone = TimeZone(secondsFromGMT: 0)
    return iso.string(from: date)
}

private func isICloud(_ calendar: EKCalendar) -> Bool {
    calendar.source.title.localizedCaseInsensitiveContains("icloud")
}

private func selectedCalendar(_ store: EKEventStore, _ id: String) throws -> EKCalendar {
    guard let calendar = store.calendar(withIdentifier: id), isICloud(calendar) else {
        try fail("icloud_calendar_not_found")
    }
    return calendar
}

private func eventSnapshot(_ event: EKEvent) -> [String: Any] {
    let minutes = event.alarms?.first.map { Int((-($0.relativeOffset) / 60.0).rounded()) }
    return [
        "event_id": event.eventIdentifier ?? "",
        "calendar_id": event.calendar.calendarIdentifier,
        "title": event.title ?? "",
        "start_at_utc": stamp(event.startDate),
        "end_at_utc": stamp(event.endDate),
        "description": event.notes ?? "",
        "location": event.location ?? "",
        "reminder_minutes": minutes as Any? ?? NSNull(),
        "last_modified_at_utc": event.lastModifiedDate.map(stamp) as Any? ?? NSNull(),
    ]
}

private func fingerprint(_ event: EKEvent) -> String {
    let snapshot = eventSnapshot(event)
    let keys = ["calendar_id", "title", "start_at_utc", "end_at_utc", "description",
                "location", "reminder_minutes", "last_modified_at_utc"]
    let canonical = keys.map { "\($0)=\(snapshot[$0] ?? "")" }.joined(separator: "\u{0}")
    return SHA256.hash(data: Data(canonical.utf8)).map { String(format: "%02x", $0) }.joined()
}

private func eventById(_ store: EKEventStore, _ calendar: EKCalendar, _ id: String) throws -> EKEvent {
    guard let event = store.event(withIdentifier: id),
          event.calendar.calendarIdentifier == calendar.calendarIdentifier else {
        try fail("icloud_event_not_found")
    }
    if event.isAllDay { try fail("all_day_event_update_unsupported") }
    return event
}

private func dates(_ event: [String: Any]) throws -> (Date, Date) {
    let start = try parseDate(string(event, "start_at_utc"))
    let end = try parseDate(string(event, "end_at_utc"))
    if start >= end { try fail("invalid_calendar_time") }
    return (start, end)
}

private func conflicts(_ store: EKEventStore, _ calendar: EKCalendar,
                       _ start: Date, _ end: Date, excluding: String?) -> [[String: Any]] {
    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: [calendar])
    return store.events(matching: predicate).filter { event in
        event.eventIdentifier != excluding && event.startDate < end && event.endDate > start
    }.prefix(10).map { event in
        ["event_id": event.eventIdentifier ?? "", "title": event.title ?? "",
         "start_at_utc": stamp(event.startDate), "end_at_utc": stamp(event.endDate)]
    }
}

private func reminder(_ event: [String: Any]) throws -> Int? {
    guard let raw = event["reminder_minutes"], !(raw is NSNull) else { return nil }
    guard let value = raw as? Int, value >= 0, value <= 10080 else {
        try fail("invalid_reminder_minutes")
    }
    return value
}

private func handle(_ request: [String: Any], _ store: EKEventStore) throws -> [String: Any] {
    let operation = try string(request, "operation")
    if operation == "list_calendars" {
        let items: [[String: Any]] = store.calendars(for: .event).filter(isICloud).map { calendar in
            ["calendar_id": calendar.calendarIdentifier, "title": calendar.title,
             "source_id": calendar.source.sourceIdentifier,
             "source_title": calendar.source.title,
             "writable": calendar.allowsContentModifications]
        }
        return ["items": items]
    }
    let calendar = try selectedCalendar(store, string(request, "calendar_id"))
    if operation == "get_event" {
        let event = try eventById(store, calendar, string(request, "event_id"))
        return ["event": eventSnapshot(event), "fingerprint": fingerprint(event)]
    }
    if operation == "list_events" {
        let start = try parseDate(string(request, "start_at_utc"))
        let end = try parseDate(string(request, "end_at_utc"))
        // A 31-day local range can gain one UTC hour across a daylight-saving fallback.
        if start >= end || end.timeIntervalSince(start) > 32 * 86400 { try fail("invalid_calendar_range") }
        let predicate = store.predicateForEvents(withStart: start, end: end, calendars: [calendar])
        let items = store.events(matching: predicate).prefix(200).map(eventSnapshot)
        return ["items": items]
    }
    if operation != "inspect_write" && operation != "write_event" {
        try fail("unsupported_operation")
    }
    if !calendar.allowsContentModifications { try fail("icloud_calendar_read_only") }
    let payload = try object(request, "event")
    let (start, end) = try dates(payload)
    let eventId = request["event_id"] as? String
    let old = try eventId.map { try eventById(store, calendar, $0) }
    let collisions = conflicts(store, calendar, start, end, excluding: eventId)
    if operation == "inspect_write" {
        return ["current": old.map(eventSnapshot) as Any? ?? NSNull(),
                "fingerprint": old.map(fingerprint) as Any? ?? NSNull(),
                "conflicts": collisions]
    }
    if !collisions.isEmpty { try fail("icloud_calendar_conflict") }
    if let old = old {
        if try string(payload, "selected_calendar_id") != calendar.calendarIdentifier {
            try fail("icloud_calendar_not_selected")
        }
        let expected = try string(payload, "expected_event_fingerprint")
        if fingerprint(old) != expected { try fail("icloud_event_changed") }
    }
    let item = old ?? EKEvent(eventStore: store)
    item.calendar = calendar
    item.title = try string(payload, "title")
    item.startDate = start
    item.endDate = end
    item.notes = payload["description"] as? String
    item.location = payload["location"] as? String
    if let minutes = try reminder(payload) {
        item.alarms = [EKAlarm(relativeOffset: -Double(minutes * 60))]
    } else {
        item.alarms = nil
    }
    do {
        try store.save(item, span: .thisEvent, commit: true)
    } catch {
        // EventKit can fail after an external account has accepted a change.
        throw BridgeFailure(code: "icloud_save_result_unknown")
    }
    return ["event_id": item.eventIdentifier ?? "", "calendar_id": calendar.calendarIdentifier,
            "event": eventSnapshot(item)]
}

private func output(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    } else {
        print("{\"ok\":false,\"error_code\":\"serialization_failed\",\"known_no_write\":false}")
    }
}

@main
struct EventKitBridge {
    static func main() async {
        do {
            let data = FileHandle.standardInput.readDataToEndOfFile()
            guard let request = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                try fail("invalid_request")
            }
            let store = EKEventStore()
            if request["operation"] as? String == "authorization_status" {
                let status = EKEventStore.authorizationStatus(for: .event)
                let name: String
                switch status {
                case .notDetermined: name = "not_determined"
                case .restricted: name = "restricted"
                case .denied: name = "denied"
                case .writeOnly: name = "write_only"
                case .fullAccess: name = "full_access"
                @unknown default: name = "unknown"
                }
                output(["ok": true, "status": name])
                return
            }
            let granted: Bool
            do {
                granted = try await store.requestFullAccessToEvents()
            } catch {
                try fail("calendar_permission_denied")
            }
            if !granted { try fail("calendar_permission_denied") }
            let result = try handle(request, store)
            output(["ok": true, "result": result])
        } catch let error as BridgeFailure {
            output(["ok": false, "error_code": error.code,
                    "known_no_write": error.code != "icloud_save_result_unknown"])
        } catch {
            output(["ok": false, "error_code": "eventkit_error", "known_no_write": false])
        }
    }
}

// kbdflash — flash the built-in MacBook keyboard backlight, then restore prior state.
//
// macOS backend for kbd-agent-notify. Uses the private CoreBrightness framework's
// KeyboardBrightnessClient — the same class KBPulse uses — which works on Apple
// Silicon and Intel. NO root required for the built-in keyboard.
//
//   read                 -> print current backlight brightness (0.0-1.0)
//   flash [opts]         -> read prior brightness, pulse, restore exactly
//
// flash options:
//   --count N            number of flashes        (default 2)
//   --on-ms MS           lit duration per flash    (default 300)
//   --off-ms MS          gap duration per flash    (default 300)
//   --level L            "lit" brightness 0.0-1.0  (default 1.0)
//   --fade MS            hardware fade speed        (default 50; KBPulse manual=350)
//   --id K               keyboard ID               (default: auto-detect built-in)
//   --quiet              suppress diagnostics
//
// Why this works where setProperty did not: KeyboardBrightnessClient's
// -setBrightness:fadeSpeed:commit:forKeyboard: with commit=true pushes the value
// through the brightness daemon to the PWM hardware. We also disable auto-brightness
// for the duration of the flash (it would otherwise override us) and restore it after.
//
// Build:  swiftc -O kbdflash.swift -o kbdflash

import Foundation
import ObjectiveC

let CORE_BRIGHTNESS =
    "/System/Library/PrivateFrameworks/CoreBrightness.framework/CoreBrightness"

// KeyboardBrightnessClient selectors (private API, stable since macOS 10.13).
let SEL_READ    = NSSelectorFromString("brightnessForKeyboard:")
let SEL_SET     = NSSelectorFromString("setBrightness:fadeSpeed:commit:forKeyboard:")
let SEL_ISAUTO  = NSSelectorFromString("isAutoBrightnessEnabledForKeyboard:")
let SEL_ENAUTO  = NSSelectorFromString("enableAutoBrightness:forKeyboard:")
let SEL_BUILTIN = NSSelectorFromString("isKeyboardBuiltIn:")
let SEL_IDS     = NSSelectorFromString("copyKeyboardBacklightIDs")

typealias ReadFn   = @convention(c)(NSObject, Selector, UInt64) -> Float
typealias SetFn    = @convention(c)(NSObject, Selector, Float, Int32, ObjCBool, UInt64) -> ObjCBool
typealias BoolFn   = @convention(c)(NSObject, Selector, UInt64) -> ObjCBool
typealias EnAutoFn = @convention(c)(NSObject, Selector, ObjCBool, UInt64) -> ObjCBool

final class KbdBacklight {
    let client: NSObject
    let readFn: ReadFn
    let setFn: SetFn
    let isAutoFn: BoolFn
    let enAutoFn: EnAutoFn
    let isBuiltInFn: BoolFn

    init?() {
        guard dlopen(CORE_BRIGHTNESS, RTLD_NOW) != nil else { return nil }
        guard let cls = objc_getClass("KeyboardBrightnessClient") as? NSObject.Type else { return nil }
        let c = cls.init()
        guard c.responds(to: SEL_READ), c.responds(to: SEL_SET) else { return nil }
        client = c
        readFn = unsafeBitCast(c.method(for: SEL_READ)!, to: ReadFn.self)
        setFn = unsafeBitCast(c.method(for: SEL_SET)!, to: SetFn.self)
        isAutoFn = unsafeBitCast(c.method(for: SEL_ISAUTO)!, to: BoolFn.self)
        enAutoFn = unsafeBitCast(c.method(for: SEL_ENAUTO)!, to: EnAutoFn.self)
        isBuiltInFn = unsafeBitCast(c.method(for: SEL_BUILTIN)!, to: BoolFn.self)
    }

    func brightness(_ kid: UInt64) -> Float { readFn(client, SEL_READ, kid) }

    @discardableResult
    func setBrightness(_ v: Float, fade: Int32, _ kid: UInt64) -> Bool {
        setFn(client, SEL_SET, v, fade, ObjCBool(true), kid).boolValue
    }

    func isAuto(_ kid: UInt64) -> Bool { isAutoFn(client, SEL_ISAUTO, kid).boolValue }

    @discardableResult
    func setAuto(_ on: Bool, _ kid: UInt64) -> Bool {
        enAutoFn(client, SEL_ENAUTO, ObjCBool(on), kid).boolValue
    }

    func isBuiltIn(_ kid: UInt64) -> Bool { isBuiltInFn(client, SEL_BUILTIN, kid).boolValue }

    // Enumerate keyboard IDs; prefer the built-in one. Falls back to 1.
    func detectID() -> UInt64 {
        let sel = SEL_IDS
        if client.responds(to: sel),
           let ids = client.perform(sel)?.takeUnretainedValue() as? [NSNumber] {
            for n in ids where isBuiltIn(n.uint64Value) { return n.uint64Value }
            if let first = ids.first { return first.uint64Value }
        }
        return 1
    }
}

func argValue(_ name: String, _ def: String) -> String {
    let a = CommandLine.arguments
    if let i = a.firstIndex(of: name), i + 1 < a.count { return a[i + 1] }
    return def
}
func hasFlag(_ name: String) -> Bool { CommandLine.arguments.contains(name) }
func sleepMs(_ ms: Int) { if ms > 0 { usleep(useconds_t(ms) * 1000) } }

let args = CommandLine.arguments
let cmd = args.count > 1 ? args[1] : "flash"
let quiet = hasFlag("--quiet")
func errln(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }
func log(_ s: String) { if !quiet { errln(s) } }

guard let kb = KbdBacklight() else {
    errln("kbdflash: could not bind CoreBrightness KeyboardBrightnessClient")
    exit(3)
}

switch cmd {
case "read":
    let kid = UInt64(argValue("--id", "")) ?? kb.detectID()
    print("keyboardID=\(kid) brightness=\(kb.brightness(kid)) builtIn=\(kb.isBuiltIn(kid)) auto=\(kb.isAuto(kid))")
    exit(0)

case "flash":
    let count = Int(argValue("--count", "2")) ?? 2
    let onMs  = Int(argValue("--on-ms", "300")) ?? 300
    let offMs = Int(argValue("--off-ms", "300")) ?? 300
    let level = Float(argValue("--level", "1.0")) ?? 1.0
    let fade  = Int32(argValue("--fade", "50")) ?? 50
    let kid   = UInt64(argValue("--id", "")) ?? kb.detectID()

    let prior = kb.brightness(kid)
    let autoWasOn = kb.isAuto(kid)
    let restingLit = prior > 0.0001
    let pulseVal:   Float = restingLit ? 0.0   : level
    let betweenVal: Float = restingLit ? level : 0.0
    log("kbdflash: id=\(kid) prior=\(prior) auto=\(autoWasOn) count=\(count) on=\(onMs) off=\(offMs) level=\(level) fade=\(fade)")

    // Always restore exact prior brightness AND prior auto-brightness setting.
    func restore() {
        kb.setBrightness(prior, fade: fade, kid)
        kb.setAuto(autoWasOn, kid)
    }
    signal(SIGINT) { _ in exit(130) }
    defer { restore() }

    // Auto-brightness would fight the pulse — suspend it for the duration.
    if autoWasOn { kb.setAuto(false, kid) }

    for _ in 0..<max(1, count) {
        kb.setBrightness(pulseVal, fade: fade, kid)
        sleepMs(onMs)
        kb.setBrightness(betweenVal, fade: fade, kid)
        sleepMs(offMs)
    }
    restore()
    exit(0)

default:
    errln("kbdflash: unknown command '\(cmd)' (use: read | flash)")
    exit(64)
}

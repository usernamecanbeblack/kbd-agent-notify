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
//   --shape S            "wave" (smooth swell) | "square" (hard blink)  (default wave)
//   --count N            number of waves/flashes                        (default 2)
//   --on-ms MS           wave: swell width; square: lit hold (ms)       (default 900)
//   --off-ms MS          gap between waves/flashes (ms)                 (default 550)
//   --level L            swell-to / "lit" brightness 0.0-1.0            (default 0.5)
//   --fade MS            hardware fade speed (capped to one step in wave)(default 60)
//   --steps N            wave smoothness: sub-steps per swell           (default 48)
//   --slowdown F         wave: each swell F× longer (lower frequency)   (default 1.0)
//   --floor L            wave dip target when resting lit 0.0-1.0       (default 0.0)
//   --lit-threshold L    resting brightness at/above which we dip       (default 0.2)
//   --id K               keyboard ID               (default: auto-detect built-in)
//   --quiet              suppress diagnostics
//
// The "wave" shape eases brightness along a half-sine (b = prior + (target-prior)*
// sin(pi*phase)), anchored at your resting brightness so it never jumps — it rises and
// falls like an ocean swell instead of blinking. The direction adapts to the resting
// state: if the keyboard is dark (prior < lit-threshold) the swell rises toward
// --level (a glow-on); if it is already lit (prior >= lit-threshold) the swell dips
// toward --floor (a fade-off). With --slowdown > 1 each successive swell is longer than
// the last — a decreasing frequency, like surf settling.
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
func sleepMs(_ ms: Int) { if ms > 0 { Thread.sleep(forTimeInterval: Double(ms) / 1000.0) } }

let args = CommandLine.arguments
let cmd = args.count > 1 ? args[1] : "flash"
let quiet = hasFlag("--quiet")
func errln(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }
func log(_ s: String) { if !quiet { errln(s) } }

// Best-effort cleanup on Ctrl-C: exit() does not unwind `defer`, so the SIGINT handler
// must restore state itself. A top-level (non-capturing) handler reads this global,
// which the flash command points at its own restore() closure.
var sigintCleanup: (() -> Void)? = nil
func onSIGINT(_ sig: Int32) {
    sigintCleanup?()
    exit(130)
}

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
    let shape = argValue("--shape", "wave")
    let count = max(1, Int(argValue("--count", "2")) ?? 2)
    let onMs  = max(1, Int(argValue("--on-ms", "900")) ?? 900)
    let offMs = max(0, Int(argValue("--off-ms", "550")) ?? 550)
    let level = Float(argValue("--level", "0.5")) ?? 0.5
    let fade  = Int32(argValue("--fade", "60")) ?? 60
    let steps = max(2, Int(argValue("--steps", "48")) ?? 48)
    let slowdown = max(1.0, Double(argValue("--slowdown", "1.0")) ?? 1.0)
    let floor = Float(argValue("--floor", "0.0")) ?? 0.0
    let litThreshold = Float(argValue("--lit-threshold", "0.2")) ?? 0.2
    let kid   = UInt64(argValue("--id", "")) ?? kb.detectID()

    let prior = kb.brightness(kid)
    let autoWasOn = kb.isAuto(kid)
    log("kbdflash: id=\(kid) prior=\(prior) auto=\(autoWasOn) shape=\(shape) count=\(count) on=\(onMs) off=\(offMs) level=\(level) floor=\(floor) litThr=\(litThreshold) fade=\(fade) steps=\(steps) slowdown=\(slowdown)")

    // Always restore exact prior brightness AND prior auto-brightness setting.
    func restore() {
        kb.setBrightness(prior, fade: fade, kid)
        kb.setAuto(autoWasOn, kid)
    }
    sigintCleanup = restore
    signal(SIGINT, onSIGINT)
    defer { restore() }

    // Auto-brightness would fight the pulse — suspend it for the duration.
    if autoWasOn { kb.setAuto(false, kid) }

    if shape == "square" {
        // Hard blink: invert against the resting state so there is always contrast.
        let restingLit = prior > 0.0001
        let pulseVal:   Float = restingLit ? 0.0   : level
        let betweenVal: Float = restingLit ? level : 0.0
        for _ in 0..<count {
            kb.setBrightness(pulseVal, fade: fade, kid)
            sleepMs(onMs)
            kb.setBrightness(betweenVal, fade: fade, kid)
            sleepMs(offMs)
        }
    } else {
        // Ocean swell, anchored at `prior` so it never jumps. Direction adapts to the
        // resting state: dark keyboard -> rise toward `level` (glow-on); already-lit
        // keyboard -> dip toward `floor` (fade-off, the opposite swell). Each successive
        // swell is `slowdown`x longer (a lower frequency), like surf settling. fadeSpeed
        // is capped to one step so the software curve — not the hardware — sets the shape.
        let restingLit = prior >= litThreshold
        let target: Float = restingLit ? floor : level
        log("kbdflash: wave dir=\(restingLit ? "dip (fade-off)" : "rise (glow-on)") target=\(target)")
        var waveMs = Double(onMs)
        var gapMs  = Double(offMs)
        for i in 0..<count {
            let thisWave = max(steps, Int(waveMs.rounded()))
            let stepDur  = max(1, thisWave / steps)
            let stepFade = Int32(min(Int(fade), stepDur))
            for s in 0...steps {
                let phase = Double(s) / Double(steps)        // 0...1
                let env   = Float(sin(Double.pi * phase))    // 0 -> 1 -> 0
                kb.setBrightness(prior + (target - prior) * env, fade: stepFade, kid)
                sleepMs(stepDur)
            }
            kb.setBrightness(prior, fade: stepFade, kid)     // settle exactly at rest
            if i < count - 1 { sleepMs(Int(gapMs.rounded())) }
            waveMs *= slowdown
            gapMs  *= slowdown
        }
    }
    restore()
    exit(0)

default:
    errln("kbdflash: unknown command '\(cmd)' (use: read | flash)")
    exit(64)
}

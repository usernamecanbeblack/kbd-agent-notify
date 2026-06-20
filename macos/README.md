# kbd-agent-notify — macOS backend

Flash the **built-in MacBook keyboard backlight** when Claude Code or Codex CLI
finishes a turn or needs your input. This is the macOS port of the original
Windows/Dell tool: same Python orchestrator and CLI hooks, but the backend that
actually drives the light is `kbdflash`, a tiny Swift helper.

```
agent finishes (done)   ->  one slow swell  (backlight eases up and back down)
agent waits (waiting)   ->  two swells, the 2nd slower, so you can tell them apart
```

The flash is a smooth **ocean swell**, not a hard blink: the backlight eases from your
resting brightness toward a gentle level and back along a sine curve. Each successive
swell is a little longer than the last (a *lower frequency*, like surf settling).

The swell **adapts to your keyboard's state**, so it is always the opposite of what's
there:

- **Keyboard off / dim** (e.g. daytime) → the swell **rises** (a gentle glow-on toward
  `macLevel`).
- **Keyboard already lit** (e.g. you turned it up at night) → the swell **dips** (a gentle
  fade-off toward `macFloor`, then back to your level).

The switch point is `macLitThreshold` (rest brightness at/above which it dips).

## How it works

The flash is driven straight from the CLI's hook — there is **no always-on app**.

```
Claude Code / Codex hook (no elevation)
  └─ python3 kbd_agent_notify.py signal --event done|waiting
       └─ macOS backend  -> macos/kbdflash flash ...
            └─ CoreBrightness.framework  (KeyboardBrightnessClient)
                 reads prior brightness + auto-brightness state
                 pulses the backlight
                 restores both exactly
```

`kbdflash` uses the private **`CoreBrightness`** framework's
`KeyboardBrightnessClient` — the same high-level class the system itself and tools
like [KBPulse](https://github.com/EthanRDoesMC/KBPulse) use. The key call is:

```objc
-[KeyboardBrightnessClient setBrightness:fadeSpeed:commit:forKeyboard:]
```

`commit:true` pushes the value through the brightness daemon to the PWM hardware.
The helper also **disables auto-brightness** for the duration of the flash (it would
otherwise immediately override the pulse) and restores it afterward.

- **No root required** for the built-in keyboard.
- **No calibration** needed (unlike the Dell/Windows path). The built-in keyboard ID
  is auto-detected via `copyKeyboardBacklightIDs`.
- Works on **Apple Silicon** (M-series) and Intel Macs with a backlit keyboard.

## Requirements

- macOS with a backlit built-in keyboard.
- Xcode command line tools (for `swiftc`):  `xcode-select --install`
- Python 3.9+ (`python3` on PATH).
- Claude Code and/or Codex CLI (for the automatic hooks).

## Quick start

```bash
# 1) Build the helper
bash macos/build.sh

# 2) Create your config (enables the macOS backend)
cp kbd-agent-notify.config.example.json kbd-agent-notify.config.json
# then set "enabled": true in the "kbd" block (the example ships disabled)

# 3) Test it — watch your keyboard
python3 kbd_agent_notify.py kbd-test --event done
python3 kbd_agent_notify.py kbd-test --read-only   # just prints current state

# 4) Install the CLI hooks
python3 kbd_agent_notify.py install-hooks --target all
# Restart Claude Code so the new hooks load.
# For Codex: run /hooks in a new session to trust them.
```

## kbdflash CLI (the helper)

```
macos/kbdflash read                 # print brightness + builtIn + auto state
macos/kbdflash flash [opts]
    --shape S        wave (smooth swell) | square (hard blink)  (default wave)
    --count N        number of swells / flashes                 (default 2)
    --on-ms MS       wave: swell width; square: lit hold (ms)    (default 900)
    --off-ms MS      gap between swells / flashes (ms)           (default 550)
    --level L        swell-to / lit brightness 0.0-1.0          (default 0.5)
    --fade MS        hardware fade speed (capped per step)       (default 60)
    --steps N        wave smoothness: sub-steps per swell        (default 48)
    --slowdown F     wave: each swell F× longer (lower freq)     (default 1.0)
    --floor L        wave dip target when resting lit (0.0-1.0)  (default 0.0)
    --lit-threshold L  rest >= this -> dip instead of rise       (default 0.2)
    --id K           keyboard ID               (default: auto-detect built-in)
    --quiet          suppress diagnostics

The swell follows `b = prior + (target - prior) · sin(π · phase)`, anchored at your
resting brightness so it never jumps. `target` is `--level` when the keyboard is dark
and `--floor` when it is already lit (rest ≥ `--lit-threshold`). `--shape square`
restores the original hard blink. The helper restores your exact prior state on Ctrl-C.
```

`kbdflash` always restores your exact prior brightness **and** your prior
auto-brightness setting, even on Ctrl-C.

## Config (macOS keys)

In `kbd-agent-notify.config.json`, the `kbd` block accepts these macOS settings:

| key            | meaning                                            | default        |
|----------------|----------------------------------------------------|----------------|
| `enabled`      | turn the backlight backend on                      | `false`        |
| `macHelper`    | path to the `kbdflash` binary                      | `macos/kbdflash` |
| `macKeyboardID`| keyboard ID, or `null` to auto-detect built-in     | `null`         |
| `macLevel`     | swell-to brightness for a flash (0.0–1.0)          | `0.5`          |
| `macFadeMs`    | hardware fade speed in ms (capped per step in wave)| `60`           |
| `macShape`     | `wave` (smooth swell) or `square` (hard blink)     | `wave`         |
| `macSteps`     | wave smoothness (sub-steps per swell)              | `48`           |
| `macSlowdown`  | each successive swell this much longer (≥ 1.0)     | `1.5`          |
| `macFloor`     | dip target when the keyboard is already lit        | `0.0`          |
| `macLitThreshold`| rest ≥ this ⇒ dip (fade-off); below ⇒ rise (glow-on) | `0.2`     |
| `patterns`     | per-event `count` / `onMs` / `offMs` / `level`     | see example    |

In `wave` shape, `onMs` is the swell width and `offMs` the gap between swells; `count`
is the number of swells. A pattern `level` of `0` means "use `macLevel`". A pattern may
also override `shape` / `steps` / `slowdown` per event.

**Tune the feel:** dimmer → lower `macLevel`; slower → raise `onMs`; more "settling" →
raise `macSlowdown`; smoother → raise `macSteps`.

## Troubleshooting

- **No flash, `kbdflash read` works:** make sure `"enabled": true` in your config,
  and that the hooks point at the right `--config` path (`python3 kbd_agent_notify.py status`).
- **`could not bind CoreBrightness KeyboardBrightnessClient`:** your macOS build may
  have moved the private symbol; please open an issue with your `sw_vers`.
- **Flash too dim / too subtle:** raise `macLevel` (e.g. `0.7`).
- **Flash too fast:** raise `onMs` (swell width) and/or `macSlowdown`.
- **It rises when lit instead of dipping (or vice-versa):** adjust `macLitThreshold` — the
  resting brightness at/above which the swell fades off instead of glowing on.
- **The dip doesn't go dark enough:** lower `macFloor` (0.0 = toward fully off).
- **Want the old hard blink back:** set `macShape` to `square`.

## Notes

The original Windows/Dell files (`kbd_pulse.ps1`, `kbd_raw_toggle.ps1`) are kept
intact — this is a cross-platform fork, not a rewrite. On macOS they're simply unused.

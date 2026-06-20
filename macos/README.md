# kbd-agent-notify — macOS backend

Flash the **built-in MacBook keyboard backlight** when Claude Code or Codex CLI
finishes a turn or needs your input. This is the macOS port of the original
Windows/Dell tool: same Python orchestrator and CLI hooks, but the backend that
actually drives the light is `kbdflash`, a tiny Swift helper.

```
agent finishes (done)   ->  backlight blinks 2×
agent waits (waiting)   ->  backlight blinks 3× (quicker, so you can tell them apart)
```

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
    --count N        number of flashes        (default 2)
    --on-ms MS       lit duration per flash    (default 300)
    --off-ms MS      gap duration per flash    (default 300)
    --level L        lit brightness 0.0-1.0    (default 1.0)
    --fade MS        hardware fade speed       (default 50)
    --id K           keyboard ID               (default: auto-detect built-in)
    --quiet          suppress diagnostics
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
| `macLevel`     | lit brightness for a flash (0.0–1.0)               | `1.0`          |
| `macFadeMs`    | hardware fade speed in ms (50 = snappy)            | `50`           |
| `patterns`     | per-event `count` / `onMs` / `offMs` / `level`     | see example    |

A pattern `level` of `0` means "use `macLevel`".

## Troubleshooting

- **No flash, `kbdflash read` works:** make sure `"enabled": true` in your config,
  and that the hooks point at the right `--config` path (`python3 kbd_agent_notify.py status`).
- **`could not bind CoreBrightness KeyboardBrightnessClient`:** your macOS build may
  have moved the private symbol; please open an issue with your `sw_vers`.
- **Flash too dim/fast:** raise `macLevel` or `onMs` / lower `macFadeMs` in the config.

## Notes

The original Windows/Dell files (`kbd_pulse.ps1`, `kbd_raw_toggle.ps1`) are kept
intact — this is a cross-platform fork, not a rewrite. On macOS they're simply unused.

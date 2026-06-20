# kbd-agent-notify

Flash your laptop **keyboard backlight** when Claude Code or Codex CLI finishes a
turn or is waiting for your input. A small, no-prompt desktop notifier for people
who run coding agents in a terminal and miss when they're done.

```
agent finishes  ->  one pulse   (macOS: a single slow swell)
agent waits     ->  two pulses  (macOS: two swells, the 2nd slower)
```

It hooks into Claude Code and Codex CLI, and on each event it pulses the keyboard
backlight via the Dell SMBIOS WMI interface, then **restores your exact prior
backlight state**. No always-on tray app; the flash is driven straight from the
CLI's hook.

> **Scope:** This drives the ordinary **keyboard backlight** (not a capacitive
> function row / touch bar). Works today on a **Dell XPS 13 9350** (platform `0CC9`),
> Windows 11. The mechanism (Dell SMBIOS keyboard-backlight WMI) is common to many
> Dell laptops, but the exact "lit" value differs per model, so **you will likely need
> to calibrate it** — see [Adapt it to your keyboard](#adapt-it-to-your-keyboard).
> Non-Dell machines are not supported out of the box.

> **🍎 macOS (Apple Silicon / Intel):** this fork adds a macOS backend that flashes the
> **built-in MacBook keyboard backlight** via the private `CoreBrightness` framework —
> no root, no calibration. See **[macos/README.md](macos/README.md)** for setup. The
> rest of this document describes the original Windows/Dell path.

---

## How it works

```
CLI hook (non-elevated)                      Scheduled Task (elevated, S4U)
  kbd_agent_notify.py signal --event done -->  kbd_pulse.ps1
    writes captures/kbd-request.json             reads request
    triggers the Scheduled Task                  reads + saves current backlight state
                                                 flashes (toggles SMBIOS mode bits)
                                                 restores the saved state
```

The Dell SMBIOS WMI call (`BFn`/`BDat`, class 4 / select 11) that controls the
keyboard backlight is **admin-only**, but CLI hooks run non-elevated. A
one-time-registered **S4U** Scheduled Task lets a non-elevated process trigger the
elevated pulse with **no UAC prompt and no stored password** (see `install-kbd-task`).

Every pulse reads your current backlight state first and restores it afterward, so the
notifier never leaves your keyboard in a changed state.

---

## Requirements

- Windows 10/11
- A **Dell** laptop whose keyboard backlight is controllable via Dell SMBIOS WMI
  (most Dell Latitude / XPS / Precision with a backlit keyboard). Verify with the
  calibration steps below.
- Python 3.9+ (`py`/`python` on PATH)
- Windows PowerShell 5.1 (built in)
- Claude Code and/or Codex CLI (for the automatic hooks)

---

## Quick start

```powershell
git clone <your-fork-url> kbd-agent-notify
cd kbd-agent-notify

# 1) Create your config from the example
copy kbd-agent-notify.config.example.json kbd-agent-notify.config.json

# 2) Calibrate the flash for your keyboard (see next section). At minimum, confirm
#    the pulse is visible from an ELEVATED PowerShell:
python .\kbd_agent_notify.py kbd-test --read-only      # safe: just prints current state
python .\kbd_agent_notify.py kbd-test --event test     # visible: flashes, then restores

# 3) Register the elevated, no-prompt Scheduled Task (run from an ELEVATED shell, once)
python .\kbd_agent_notify.py install-kbd-task

# 4) Enable it: set "enabled": true in the "kbd" block of kbd-agent-notify.config.json

# 5) Test the REAL path from a NON-elevated shell (this is what the hooks do)
python .\kbd_agent_notify.py --config .\kbd-agent-notify.config.json signal --source manual --event test

# 6) Install the CLI hooks
python .\kbd_agent_notify.py install-hooks --target all
```

After installing Codex hooks, start a new Codex session and run `/hooks` to trust
them. Restart Claude Code sessions after the hooks are installed.

---

## Adapt it to your keyboard

This is the part that varies by machine. The flash works by writing two
**mode-bitmap** values to the Dell SMBIOS keyboard-backlight interface: a "lit" value
and a "dark" value. The dark value (`0x0001`, "Always-off") is standard across Dell.
**The lit value is what differs by model** — on some machines the obvious "Always-on"
bit (`0x0002`) does nothing visible and a trigger/level bit is required instead (on the
XPS 13 9350, `0x0040` = TRIGGER_50 works while `0x0002` does not).

Use the included raw toggle tool to find what visibly blinks your keyboard. From an
**elevated** PowerShell:

```powershell
# Read your current state to learn your machine's resting word
python .\kbd_agent_notify.py kbd-test --read-only   # note savedState.raw (e.g. cbArg2=0x41070001)

# Then toggle the low byte (the mode bitmap) between candidate "lit" bits, keeping the
# upper bytes the same as your resting word:
.\kbd_raw_toggle.ps1 -OnArg2 0x41070040 -OffArg2 0x41070001 -Count 4 -OnMs 350 -OffMs 350  # TRIGGER_50
.\kbd_raw_toggle.ps1 -OnArg2 0x41070100 -OffArg2 0x41070001 -Count 4                       # TRIGGER_100
.\kbd_raw_toggle.ps1 -OnArg2 0x41070002 -OffArg2 0x41070001 -Count 4                       # Always-on
```

The `cbArg2` word is `mode(low 16 bits) | triggers(byte2) | battTimeout(byte3)`. Keep
the upper bytes the same as your resting state (`kbd-test --read-only` shows it); only
vary the low word (the mode bitmap). Mode bits (from the upstream `dell-laptop` driver):

| bit  | meaning            |
|------|--------------------|
| 0x01 | Always off (dark)  |
| 0x02 | Always on          |
| 0x04 | ALS                |
| 0x40 | Trigger 50%        |
| 0x80 | Trigger 75%        |
| 0x100| Trigger 100%       |

Once you know which `-OnArg2` value visibly flashes your keyboard, set the matching
**low-word mode bit** as `$MODE_ON` near the top of [`kbd_pulse.ps1`](kbd_pulse.ps1)
(it's clearly marked "CALIBRATE PER MACHINE").

If `kbd-test --read-only` returns "Access denied", run it from an **elevated** shell.
If it returns no data or errors on the WMI query, your machine may not expose the
`BFn`/`BDat` interface and is not supported.

### Tuning the flash

`count` / `onMs` / `offMs` per event live in `kbd-agent-notify.config.json` (the
`kbd.patterns` block). You can also override on the fly:

```powershell
python .\kbd_agent_notify.py kbd-test --event test --count 3 --on-ms 200 --off-ms 200
```

Note: rapid toggling can be coalesced by some firmware. If multiple flashes merge into
one, increase `offMs`.

---

## Commands

```
python kbd_agent_notify.py kbd-test [--read-only] [--event done|waiting|test] [--count N --on-ms X --off-ms Y]
python kbd_agent_notify.py install-kbd-task   [--prompt-password] [--user NAME]   # ELEVATED, once
python kbd_agent_notify.py uninstall-kbd-task                                     # ELEVATED
python kbd_agent_notify.py install-hooks --target all|claude|codex
python kbd_agent_notify.py signal --source manual --event test [--verbose]
python kbd_agent_notify.py status
```

`install-kbd-task` defaults to an **S4U** task (elevated, no prompt, no stored
password). `--prompt-password` is an alternative that stores your **account** password
in the Windows credential store (note: a Windows Hello **PIN is not your account
password** and will not work).

When `install-hooks` edits existing user config files, it writes timestamped `.bak`
files next to them first.

---

## Security & safety notes

- The elevated Scheduled Task runs `kbd_pulse.ps1` with the highest available token.
  Review that script before trusting it. It only reads/writes the Dell
  keyboard-backlight SMBIOS class and always restores prior state.
- `install-kbd-task` (S4U mode) stores **no** password. The `--prompt-password`
  alternative stores your account password as an LSA secret (admin-readable). Prefer
  S4U.
- `kbd_raw_toggle.ps1` (calibration tool) writes raw values to the Dell
  keyboard-backlight SMBIOS class while you find your machine's "lit" value. It only
  touches that class and the values you pass it.
- Nothing here writes the embedded controller directly or installs a kernel driver.

---

## Repo contents

| file | purpose |
|------|---------|
| `kbd_agent_notify.py` | the notifier: hooks install, `signal`, the `kbd` backend, Scheduled-Task management |
| `kbd_pulse.ps1` | elevated worker: reads/flashes/restores the keyboard backlight (**calibrate here**) |
| `kbd_raw_toggle.ps1` | raw SMBIOS toggle tool for finding your machine's "lit" value |
| `kbd-agent-notify.config.example.json` | copy to `kbd-agent-notify.config.json` |

---

## Contributing

If you get this working on another Dell model, a PR adding your model + the `$MODE_ON`
value that worked (and any `kbd-test --read-only` output) would help others calibrate
faster. Non-Dell backlight backends are welcome too — the `kbd` backend is isolated
behind the request-file + worker pattern.

## License

MIT — see [LICENSE](LICENSE).

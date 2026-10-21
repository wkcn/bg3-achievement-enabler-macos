# BG3 Achievement Enabler for macOS

**English** | [简体中文](README.zh-CN.md)

A single-file Python script that patches the macOS build of *Baldur's Gate 3* so that **achievements still unlock when mods are installed**.

The game deliberately disables achievements as soon as it detects a non-standard add-on. On Windows this is solved by runtime mods; on macOS nothing similar existed, so this project ports the same idea as a direct binary patch:

- **No dependencies** — runs with the Python 3 pre-installed on macOS.
- **Minimal change** — rewrites exactly one instruction in each of the two achievement functions.
- **No hardcoded addresses** — locates the patch sites by instruction pattern, so it keeps working across most game updates.
- **Safe by default** — backs up the original binary and re-signs it with `codesign`.

## Credits

This project would not exist without:

- [gottyduke/BG3_AchievementEnabler](https://github.com/gottyduke/BG3_AchievementEnabler) (MIT) — the Windows achievement enabler. Its source code documents the mod check that suppresses achievements and was the starting point for locating the same logic in the macOS binary. This macOS version applies the same idea, but as a static patch to the ARM64 executable instead of a runtime DLL.

## Requirements

- macOS on **Apple Silicon** (the script patches the ARM64 slice of the game binary)
- *Baldur's Gate 3* (Steam) at the default install path:

  ```
  ~/Library/Application Support/Steam/steamapps/common/Baldurs Gate 3/
  ```

- Python 3 (pre-installed on macOS)
- Xcode Command Line Tools, for the `codesign` tool: `xcode-select --install`

## Usage

### Patch

1. Make sure the game is **not running**.
2. Run:

   ```bash
   python3 patch_achievements.py
   ```

3. Wait for re-signing to finish (30 s – 2 min, depending on disk speed).
4. Launch the game from Steam as usual — achievements now unlock with mods.

The first run creates `Baldur's Gate 3.backup` next to the game executable. Keep it: it is the pristine original used by `--restore`.

### Restore

```bash
python3 patch_achievements.py --restore
```

This copies the backup back over the game executable and re-signs it.

### Notes

- **Re-run the script after every game update.** Updates replace the binary and may move code around.
- Running Steam's "Verify integrity of game files" also reverts the patch — just run the script again.
- The script refuses to touch the executable if the pattern is not found, so it will not corrupt an unrecognized game version.

## How it works

### The mod check

Achievements are blocked in two functions of the game's server module:

- `esv::AchievementManager::UnlockAchievement`
- `esv::AchievementManager::IncreaseAchievementCounter`

Both begin with a loop over the loaded add-on list. The ARM64 disassembly looks like this:

```asm
LDR  X23, [X22, #0x8B8]    ; load a pointer from the global EocServer
LDR  W24, [X23, #0x27C]    ; load the 32-bit add-on/mod count
CBZ  W24, skip_loop        ; count == 0 (no mods) -> skip the check loop
; --- loop: walk every loaded add-on ---
BL   IsStandardModule      ; is this one a standard module?
...
; --- end of loop ---
TBZ  X28, #0, unlock       ; all standard -> keep unlocking
MOV  W0, #0                ; a mod was found -> return false
B    epilogue
```

If any non-standard add-on is loaded, the function returns early and the achievement is never granted.

### The patch

One instruction, four bytes:

```asm
CBZ  W24, skip_loop        ; before: skip the loop only when no mods are loaded
B    skip_loop             ; after:  always skip the loop
```

Only the branch condition is dropped; the branch target (the 19-bit immediate) stays the same. With the check loop unconditionally skipped, the normal achievement flow resumes regardless of how many mods are loaded.

### Finding the patch sites without hardcoded addresses

Game updates move functions around, so the script never stores addresses. Instead it parses the Mach-O file (fat binary header → ARM64 slice → `__TEXT,__text` section) and scans the whole code section for the three-instruction signature of the mod check:

| Instruction             | Pattern                    | Constraint        |
| ----------------------- | -------------------------- | ----------------- |
| `LDR Xn, [Xm, #0x8B8]`  | value `0xF9445C00`          | mask `0xFFFFFC00` |
| `LDR Wk, [Xn, #0x27C]`  | value `0xB9427C00`          | Rn == Xn         |
| `CBZ Wk, <target>`      | opcode `0x34000000`        | Rt == Wk         |

Why exactly these three instructions? A plain `LDR + CBZ` pair matches about 60 places in the ~500 MB binary — far too many. Adding the `LDR ..., [#0x8B8]` anchor narrows it to 4 sites, and the two lowest ones are precisely the two `AchievementManager` functions. The script patches those two and ignores the rest.

### How the pattern was found

Condensed reverse-engineering notes:

1. **Understand the logic from the Windows source.** gottyduke's enabler shows what to look for: a loop over the add-on list that suppresses achievements when a mod is present.
2. **Strings as landmarks.** `strings "Baldur's Gate 3" | grep -i achievement` surfaces `LoadAchievementsDisabled` and `HasMods` — the mod detection is nearby.
3. **Symbol table.** Unlike the stripped Windows binary, the macOS Mach-O keeps its C++ symbols, so `nm` directly yields the addresses of both `AchievementManager` functions — the biggest shortcut in the whole process.
4. **Disassemble and cross-check.** `otool` shows the `LDR [#0x8B8] / LDR [#0x27C] / CBZ` sequence at the top of each function. The Windows x86 build reads the very same field (`movl 0x27c(%r12), %r13d`) — the `0x27C` offset is identical on both architectures, confirming it is the same logic.
5. **Minimal patch.** Flip `CBZ` into `B` and keep the branch target — done.

## FAQ

**Is it safe?**
The patch changes one instruction in each of two functions, keeps a full backup, and validates the original bytes before writing anything. `--restore` brings back the pristine binary at any time.

**Does it affect Steam's achievement reporting?**
No. It only removes the local mod check; achievements still unlock through the game's normal code path, and Steam/GOG record them as usual.

**Intel Macs?**
Not supported — the script only patches the ARM64 slice of the game binary.

## License

[MIT](LICENSE), same as the reference project.

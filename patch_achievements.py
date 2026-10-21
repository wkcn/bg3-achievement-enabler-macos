#!/usr/bin/env python3
"""
BG3 Achievement Enabler for macOS (Apple Silicon)

Patches the macOS build of Baldur's Gate 3 so that achievements still
unlock when mods are installed.

The game suppresses achievements as soon as it detects a non-standard
add-on. Both esv::AchievementManager::UnlockAchievement and
esv::AchievementManager::IncreaseAchievementCounter begin with a loop
that walks the loaded add-on list. This tool rewrites the branch that
guards that loop from a conditional CBZ ("skip the loop only when no
mods are loaded") into an unconditional B ("always skip the loop"),
restoring the normal achievement flow for modded games.

The two functions are located via the binary's symbol table (the macOS
build ships unstripped), with a whole-section instruction-pattern scan
as a fallback for stripped binaries. The struct offsets inside the
pattern are wildcards, so the offset drift between game updates
(e.g. the global load moved from #0x8B8 to #0xF58) does not break the
locator.

Usage:
    python3 patch_achievements.py             # patch the game binary
    python3 patch_achievements.py --restore   # restore the original binary

Notes:
    * Make sure the game is not running before patching.
    * The patch must be re-applied after every game update.
    * The original binary is backed up as "Baldur's Gate 3.backup".
    * Requires macOS's built-in codesign tool (Xcode Command Line Tools).
"""

import argparse
import shutil
import struct
import subprocess
import sys
from pathlib import Path

# ============================================================
# Configuration
# ============================================================

GAME_DIR = Path.home() / "Library/Application Support/Steam/steamapps/common/Baldurs Gate 3/Baldur's Gate 3.app/Contents/MacOS"
GAME_EXE = GAME_DIR / "Baldur's Gate 3"
BACKUP_EXE = GAME_DIR / "Baldur's Gate 3.backup"


# ============================================================
# Mach-O parsing
# ============================================================

def find_arm64_offset(game_path: Path) -> int:
    """Parse the fat binary header and return the file offset of the ARM64 slice."""
    with open(game_path, 'rb') as f:
        magic = struct.unpack('>I', f.read(4))[0]
        if magic != 0xCAFEBABE:
            raise ValueError(f"Not a fat binary (magic: 0x{magic:08x})")

        nfat = struct.unpack('>I', f.read(4))[0]
        for _ in range(nfat):
            cputype = struct.unpack('>I', f.read(4))[0]
            _ = f.read(4)  # cpusubtype
            offset = struct.unpack('>I', f.read(4))[0]
            _ = f.read(4)  # size
            _ = f.read(4)  # align
            if cputype == 0x0100000C:  # CPU_TYPE_ARM64
                return offset

    raise ValueError("ARM64 slice not found")


def find_text_section(game_path: Path, arm64_base: int) -> tuple:
    """Parse the ARM64 Mach-O and return (file offset, size) of the __TEXT,__text section."""
    with open(game_path, 'rb') as f:
        f.seek(arm64_base)
        magic = struct.unpack('<I', f.read(4))[0]
        assert magic == 0xFEEDFACF, f"Unexpected ARM64 slice magic: 0x{magic:08x}"

        f.seek(arm64_base + 16)
        ncmds = struct.unpack('<I', f.read(4))[0]

        cmd_offset = arm64_base + 32
        for _ in range(ncmds):
            f.seek(cmd_offset)
            cmd = struct.unpack('<I', f.read(4))[0]
            cmdsize = struct.unpack('<I', f.read(4))[0]

            if cmd == 0x19:  # LC_SEGMENT_64
                segname_raw = f.read(16)
                segname = segname_raw.split(b'\x00')[0].decode('ascii', errors='ignore')
                if segname == '__TEXT':
                    f.seek(cmd_offset + 64)
                    nsects = struct.unpack('<I', f.read(4))[0]

                    sect_base = cmd_offset + 72
                    for si in range(nsects):
                        sect_offset = sect_base + si * 80
                        f.seek(sect_offset)
                        sectname_raw = f.read(16)
                        sectname = sectname_raw.split(b'\x00')[0].decode('ascii', errors='ignore')
                        _ = f.read(16)                          # segname
                        _ = struct.unpack('<Q', f.read(8))[0]  # addr
                        sect_size = struct.unpack('<Q', f.read(8))[0]
                        sect_fileoff = struct.unpack('<I', f.read(4))[0]

                        if sectname == '__text':
                            return (arm64_base + sect_fileoff, sect_size)
                    break
            cmd_offset += cmdsize

    raise ValueError("__TEXT,__text section not found")


# ============================================================
# Patch discovery
# ============================================================

# Mangled-name substrings identifying the two target functions in the
# ARM64 symbol table (esv::AchievementManager::...).
SYMBOL_KEYS = {
    "UnlockAchievement": b"3esv18AchievementManager17UnlockAchievement",
    "IncreaseAchievementCounter": b"3esv18AchievementManager26IncreaseAchievementCounter",
}

# The add-on walk loop starts within a few instructions after the
# mod-check branch; the two functions sit back-to-back in the binary.
LOOP_WINDOW = 24            # instructions scanned for the loop signature
PAIR_MAX_DISTANCE = 0x1000  # max distance between the two functions (fallback scan)
FUNC_SCAN_LIMIT = 0x2000    # max bytes scanned inside one function


def _match_mod_check(insns, i):
    """
    Match the mod-check sequence at insns[i]:

        LDR  Xn, [Xm, #imm]     ; add-on manager global
        LDR  Wk, [Xn, #imm2]    ; add-on count
        CBZ  Wk, <skip>         ; skip the check loop when no mods

    Both load offsets are wildcards: they are struct/global offsets that
    drift between game versions. The sequence is confirmed by the
    add-on walk loop that follows the branch. Returns a site dict, or
    None when insns[i] does not start the sequence.
    """
    # LDR Xn, [Xm, #imm]  (64-bit, unsigned offset)
    insn0 = insns[i]
    if (insn0 & 0xFFC00000) != 0xF9400000:
        return None
    rt0 = insn0 & 0x1F
    global_off = ((insn0 >> 10) & 0xFFF) * 8

    # LDR Wk, [Xn, #imm2]  (32-bit, same base register as the first LDR)
    insn1 = insns[i + 1]
    if (insn1 & 0xFFC00000) != 0xB9400000:
        return None
    if ((insn1 >> 5) & 0x1F) != rt0:
        return None
    rt1 = insn1 & 0x1F
    count_off = ((insn1 >> 10) & 0xFFF) * 4

    # CBZ Wk, <skip>  -- or the unconditional B our patch leaves behind.
    # CBZ encodes its offset in imm19 (bits 23-5), B in imm26 (bits 25-0);
    # both are word-scaled, so converting CBZ -> B is a plain field move.
    insn2 = insns[i + 2]
    if (insn2 & 0xFF00001F) == (0x34000000 | rt1):
        offset_words = (insn2 >> 5) & 0x7FFFF      # CBZ offset
        if offset_words == 0 or (offset_words & 0x40000):
            return None  # must skip forward, over the check loop
        already = False                             # CBZ: unpatched
    elif (insn2 & 0xFC000000) == 0x14000000:
        offset_words = insn2 & 0x3FFFFFF            # B offset
        if offset_words == 0 or (offset_words & 0x2000000):
            return None  # must skip forward
        already = True                              # B: already patched
    else:
        return None

    # Confirmation: the add-on walk loop follows the branch.
    if not _has_addon_loop(insns, i + 3, i + 3 + LOOP_WINDOW):
        return None

    return {
        "index": i + 2,
        "original": insn2,
        "patched": 0x14000000 | offset_words,  # B, same target
        "already": already,
        "global_off": global_off,
        "count_off": count_off,
    }


def _has_addon_loop(insns, start, end):
    """
    True if insns[start:end] looks like the add-on walk loop: a call
    (BL), a test of its result (TBZ W0), a backward B.NE, and a pointer
    bump of 0x60 (the add-on array stride).
    """
    saw_bl = saw_tbz = saw_bne_back = saw_stride = False
    for insn in insns[start:min(end, len(insns))]:
        if (insn & 0xFC000000) == 0x94000000:  # BL
            saw_bl = True
        elif (insn & 0xFF00001F) == 0x36000000:  # TBZ W0, #imm
            saw_tbz = True
        elif (insn & 0xFF000010) == 0x54000000 and (insn & 0xF) == 1:  # B.NE
            if (insn >> 5) & 0x40000:            # backward edge
                saw_bne_back = True
        elif (insn & 0xFFC00000) == 0x91000000 \
                and ((insn >> 10) & 0xFFF) == 0x60 \
                and ((insn >> 5) & 0x1F) == (insn & 0x1F):  # ADD Xn, Xn, #0x60
            saw_stride = True
    return saw_bl and saw_tbz and saw_bne_back and saw_stride


def _loop_helper_index(insns, start, end):
    """Index of the first BL in insns[start:end] (the name-comparison
    helper called by the walk loop), or None."""
    for j in range(start, min(end, len(insns))):
        insn = insns[j]
        if (insn & 0xFC000000) == 0x94000000:
            imm26 = insn & 0x3FFFFFF
            if imm26 & 0x2000000:
                imm26 -= 0x4000000
            # imm26 is word-scaled: the target instruction index is
            # j + imm26 (pc-relative, in instruction units).
            return j + imm26
    return None


def find_function_ranges(game_path: Path, arm64_base: int) -> dict:
    """
    Parse the ARM64 Mach-O symbol table and return
    {func_name: (start_file_offset, end_file_offset)} for the two
    AchievementManager functions, where end is the start of the next
    symbol. Returns {} when the table or the symbols are missing.
    """
    with open(game_path, 'rb') as f:
        # Walk the load commands once: __TEXT vm mapping + LC_SYMTAB.
        f.seek(arm64_base + 16)
        ncmds = struct.unpack('<I', f.read(4))[0]

        cmd_offset = arm64_base + 32
        text_vmaddr = text_fileoff = None
        symtab = None
        for _ in range(ncmds):
            f.seek(cmd_offset)
            cmd, cmdsize = struct.unpack('<II', f.read(8))
            if cmd == 0x19:  # LC_SEGMENT_64
                segname = f.read(16).split(b'\x00')[0].decode('ascii', errors='ignore')
                if segname == '__TEXT':
                    # segment_command_64: vmaddr @ +24, vmsize @ +32, fileoff @ +40
                    f.seek(cmd_offset + 24)
                    text_vmaddr = struct.unpack('<Q', f.read(8))[0]
                    f.seek(cmd_offset + 40)
                    text_fileoff = struct.unpack('<Q', f.read(8))[0]
            elif cmd == 0x2:  # LC_SYMTAB
                f.seek(cmd_offset + 8)
                symtab = struct.unpack('<IIII', f.read(16))
            cmd_offset += cmdsize

        if symtab is None:
            return {}
        symoff, nsyms, stroff, strsize = symtab
        f.seek(arm64_base + symoff)
        symbols = f.read(nsyms * 16)
        f.seek(arm64_base + stroff)
        strtab = f.read(strsize)

    if not symbols:
        return {}

    # Collect all symbol values (for function bounds) and the matches.
    values = []
    matches = {}
    for strx, _type, _sect, _desc, value in struct.iter_unpack('<IBBHQ', symbols):
        if value < text_vmaddr:
            continue
        values.append(value)
        if strx >= strsize:
            continue
        name_end = strtab.find(b'\x00', strx)
        if name_end < 0:
            continue
        name = strtab[strx:name_end]
        for func, needle in SYMBOL_KEYS.items():
            if needle in name:
                matches.setdefault(func, set()).add(value)

    if len(matches) != len(SYMBOL_KEYS):
        return {}
    values.sort()

    def to_file_off(vm):
        return arm64_base + text_fileoff + (vm - text_vmaddr)

    ranges = {}
    for func, vms in matches.items():
        start = min(vms)
        # Function end = start of the next symbol (values are sorted).
        end = next((v for v in values if v > start), start + FUNC_SCAN_LIMIT)
        end = min(end, start + FUNC_SCAN_LIMIT)
        ranges[func] = (to_file_off(start), to_file_off(end))
    return ranges


def _fallback_pattern_sites(insns, text_file_offset):
    """
    Whole-section scan (for stripped binaries): find all mod-check
    candidates and return the unique pair that shares the same load
    offsets, the same helper call and sits within PAIR_MAX_DISTANCE -
    the two AchievementManager functions are emitted back-to-back and
    walk the same add-on list. Returns a list of 0 or 2 site dicts.
    """
    candidates = []
    for i in range(len(insns) - 3):
        m = _match_mod_check(insns, i)
        if m is None:
            continue
        m["offset"] = text_file_offset + m["index"] * 4
        m["helper"] = _loop_helper_index(insns, m["index"] + 1, m["index"] + 1 + LOOP_WINDOW)
        candidates.append(m)

    pairs = []
    for a in range(len(candidates)):
        for b in range(a + 1, len(candidates)):
            ca, cb = candidates[a], candidates[b]
            if (ca["global_off"] == cb["global_off"]
                    and ca["count_off"] == cb["count_off"]
                    and ca["helper"] == cb["helper"]
                    and abs(ca["offset"] - cb["offset"]) <= PAIR_MAX_DISTANCE):
                pairs.append((ca, cb))
    if len(pairs) != 1:
        return []
    return sorted(pairs[0], key=lambda m: m["offset"])


def find_patch_locations(game_path: Path, arm64_base: int) -> list:
    """
    Locate the mod-check branch in both AchievementManager functions.

    Primary strategy: the macOS binary keeps its C++ symbols, so both
    functions are found by name and the pattern is searched only inside
    each function body. Fallback for stripped binaries: whole-section
    pattern scan with pair confirmation (see _fallback_pattern_sites).

    Returns a list of site dicts:
      {"func", "method", "offset", "original", "patched", "already"}
    The list is empty when the sites could not be located.
    """
    text_file_offset, text_size = find_text_section(game_path, arm64_base)
    with open(game_path, 'rb') as f:
        f.seek(text_file_offset)
        code = f.read(text_size)
    insns = list(struct.unpack('<%dI' % (len(code) // 4), code))

    # --- Strategy 1: symbol table ---
    ranges = find_function_ranges(game_path, arm64_base)
    if len(ranges) == len(SYMBOL_KEYS):
        sites = []
        for func, (start_off, end_off) in ranges.items():
            start_i = max((start_off - text_file_offset) // 4, 0)
            end_i = min((end_off - text_file_offset) // 4, len(insns) - 3)
            found = []
            for k in range(start_i, end_i):
                m = _match_mod_check(insns, k)
                if m is not None:
                    m["offset"] = text_file_offset + m["index"] * 4
                    found.append(m)
            if len(found) != 1:
                print(f"  error: {func}: found {len(found)} pattern matches "
                      f"inside the function body (expected 1)")
                return []
            found[0]["func"] = func
            found[0]["method"] = "symbol table"
            sites.append(found[0])
        sites.sort(key=lambda s: s["offset"])
        return sites

    # --- Strategy 2: whole-section pattern scan ---
    pair = _fallback_pattern_sites(insns, text_file_offset)
    if not pair:
        return []
    # The functions are emitted in call order: the lower address is
    # UnlockAchievement, the higher one IncreaseAchievementCounter.
    for m, func in zip(pair, ("UnlockAchievement", "IncreaseAchievementCounter")):
        m["func"] = func
        m["method"] = "pattern scan"
    return pair


def remove_log_files():
    """Delete stray log files anywhere under the MacOS directory;
    codesign refuses to sign while unsigned files (e.g. the game's
    SaveSystemCheckLogFolder/ drop-ins) are present."""
    for f in GAME_DIR.rglob("*.log"):
        f.unlink()
        print(f"  deleted: {f.relative_to(GAME_DIR)}")
        # Drop the directory too if it just became empty.
        parent = f.parent
        if parent != GAME_DIR and not any(parent.iterdir()):
            parent.rmdir()
            print(f"  removed empty dir: {parent.relative_to(GAME_DIR)}")


# ============================================================
# Main flow
# ============================================================

def do_patch():
    """Apply the patch."""
    print("=" * 60)
    print("BG3 Achievement Enabler for macOS")
    print("=" * 60)
    print()

    if not GAME_EXE.exists():
        print(f"Error: game executable not found\n  {GAME_EXE}")
        sys.exit(1)

    # Step 1: back up the original binary
    if not BACKUP_EXE.exists():
        print("[1/4] Backing up the original file...")
        shutil.copy2(GAME_EXE, BACKUP_EXE)
        print(f"  backup: {BACKUP_EXE}")
    else:
        print("[1/4] Backup already exists, skipping")
    print()

    # Step 2: locate the patch sites
    print("[2/4] Analyzing the executable...")
    arm64_base = find_arm64_offset(GAME_EXE)
    print(f"  ARM64 slice offset: 0x{arm64_base:x}")

    sites = find_patch_locations(GAME_EXE, arm64_base)
    print(f"  found {len(sites)} patch site(s) via {sites[0]['method'] if sites else 'no method'}")
    for s in sites:
        state = " (already patched)" if s["already"] else ""
        print(f"  {s['func']}: 0x{s['offset']:x}{state}")

    if len(sites) == 0:
        print("  error: no patch site found; this game version is likely incompatible")
        sys.exit(1)
    if len(sites) != 2:
        print("  error: expected 2 patch sites "
              "(UnlockAchievement + IncreaseAchievementCounter), "
              f"found {len(sites)}")
        sys.exit(1)
    print()

    # Step 3: apply the patch. Re-signing (step 4) always runs: a
    # previous run may have written the patch but failed to sign.
    if all(s["already"] for s in sites):
        print("[3/4] Both sites are already patched - nothing to write")
    else:
        print("[3/4] Applying patches...")
        with open(GAME_EXE, 'r+b') as f:
            for s in sites:
                if s["already"]:
                    print(f"  → {s['func']}: already patched")
                    continue
                f.seek(s["offset"])
                val = struct.unpack('<I', f.read(4))[0]
                if val == s["original"]:
                    f.seek(s["offset"])
                    f.write(struct.pack('<I', s["patched"]))
                    print(f"  ✓ {s['func']} @ 0x{s['offset']:x}:")
                    print(f"    CBZ 0x{val:08x} -> B 0x{s['patched']:08x}")
                elif val == s["patched"]:
                    print(f"  → {s['func']}: already patched")
                else:
                    print(f"  ✗ {s['func']}: unexpected value (0x{val:08x}), skipped")
    print()

    # Step 4: re-sign the patched binary
    print("[4/4] Re-signing with codesign (this may take 30s-2min)...")
    remove_log_files()

    result = subprocess.run(
        ["codesign", "--force", "--sign", "-", str(GAME_EXE)],
        capture_output=True, text=True
    )

    if result.returncode == 0:
        print("  ✓ signed")
    else:
        print(f"  ✗ signing failed: {result.stderr}")
        print('  try manually: codesign --force --sign - "' + str(GAME_EXE) + '"')
        sys.exit(1)

    result = subprocess.run(
        ["codesign", "-v", str(GAME_EXE)],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("  ✓ signature verified")
    else:
        print(f"  ⚠ verification: {result.stderr.strip()}")

    print()
    print("=" * 60)
    print("Done! Launch the game from Steam - achievements now unlock with mods.")
    print("=" * 60)


def do_restore():
    """Restore the original binary from the backup."""
    print("Restoring the original file...")

    if not BACKUP_EXE.exists():
        print(f"Error: backup file not found: {BACKUP_EXE}")
        sys.exit(1)

    shutil.copy2(BACKUP_EXE, GAME_EXE)
    print(f"  ✓ restored: {GAME_EXE}")

    print("  re-signing...")
    remove_log_files()
    subprocess.run(["codesign", "--force", "--sign", "-", str(GAME_EXE)], check=True)
    print("  ✓ done")


# ============================================================
# Entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Patch Baldur's Gate 3 (macOS) so achievements unlock with mods installed."
    )
    parser.add_argument(
        "--restore", action="store_true",
        help="restore the original executable from the backup"
    )
    args = parser.parse_args()

    if args.restore:
        do_restore()
    else:
        do_patch()


if __name__ == "__main__":
    main()

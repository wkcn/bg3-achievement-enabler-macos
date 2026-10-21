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

def find_patch_locations(game_path: Path, arm64_base: int) -> list:
    """
    Scan the ARM64 code for the mod-detection pattern used by the
    AchievementManager functions.

    Exact pattern (three consecutive instructions):
        LDR Xn, [Xm, #0x8B8]   ; load a pointer from the global EocServer
        LDR Wk, [Xn, #0x27C]   ; load the add-on/mod count
        CBZ Wk, <skip>         ; if zero (no mods), skip the check loop

    The patch rewrites CBZ into B (unconditional branch), so the check
    loop is always skipped, with or without mods.

    Returns a list of (file_offset, original_insn, patched_insn) tuples.
    """
    text_file_offset, text_size = find_text_section(game_path, arm64_base)

    with open(game_path, 'rb') as f:
        f.seek(text_file_offset)
        code = f.read(text_size)

    patches = []
    num_insns = len(code) // 4

    for i in range(num_insns - 2):
        insn0 = struct.unpack_from('<I', code, i * 4)[0]
        insn1 = struct.unpack_from('<I', code, (i + 1) * 4)[0]
        insn2 = struct.unpack_from('<I', code, (i + 2) * 4)[0]

        # insn0: LDR Xn, [Xm, #0x8B8]
        # Encoding: 0xF9400000 | (imm12 << 10) | (Rn << 5) | Rt
        # with imm12 = 0x8B8 / 8 = 0x117. The mask keeps the opcode and
        # the immediate fixed while allowing any Rn/Rt registers.
        if (insn0 & 0xFFFFFC00) != 0xF9445C00:
            continue
        rt0 = insn0 & 0x1F  # Xn (holds the add-on list pointer)

        # insn1: LDR Wk, [Xn, #0x27C], with Xn == rt0
        # Encoding: 0xB9400000 | (imm12 << 10) | (Rn << 5) | Rt
        # with imm12 = 0x27C / 4 = 0x9F
        if (insn1 & 0xFFFFFC00) != 0xB9427C00:
            continue
        rn1 = (insn1 >> 5) & 0x1F
        if rn1 != rt0:
            continue
        rt1 = insn1 & 0x1F  # Wk (the mod count)

        # insn2: CBZ Wk, +offset
        if (insn2 & 0xFF00001F) != (0x34000000 | rt1):
            continue

        # Match found: keep the branch target, drop the condition.
        file_offset = text_file_offset + (i + 2) * 4
        imm19 = (insn2 >> 5) & 0x7FFFF
        b_insn = 0x14000000 | imm19  # B (unconditional branch), same target
        patches.append((file_offset, insn2, b_insn))

    return patches


def remove_log_files():
    """Delete stray log files in the MacOS directory; codesign rejects them."""
    for f in GAME_DIR.glob("*.log"):
        f.unlink()
        print(f"  deleted: {f.name}")


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

    patches = find_patch_locations(GAME_EXE, arm64_base)
    print(f"  found {len(patches)} candidate patch sites")

    if len(patches) < 2:
        print("  warning: expected at least 2 sites (UnlockAchievement + IncreaseAchievementCounter)")
        if len(patches) == 0:
            print("  error: no patch site found; this game version is likely incompatible")
            sys.exit(1)
    print()

    # Step 3: apply the patch
    # The three-instruction pattern matches a handful of sites; the first
    # two are exactly the two AchievementManager functions, the rest are
    # unrelated false positives.
    print("[3/4] Applying patches...")
    with open(GAME_EXE, 'r+b') as f:
        for i, (offset, original, replacement) in enumerate(patches[:2]):
            f.seek(offset)
            val = struct.unpack('<I', f.read(4))[0]
            if val == original:
                f.seek(offset)
                f.write(struct.pack('<I', replacement))
                func_name = "UnlockAchievement" if i == 0 else "IncreaseAchievementCounter"
                print(f"  ✓ Patch {i + 1} ({func_name}):")
                print(f"    offset: 0x{offset:x}")
                print(f"    CBZ 0x{original:08x} -> B 0x{replacement:08x}")
            elif val == replacement:
                print(f"  → Patch {i + 1}: already patched")
            else:
                print(f"  ✗ Patch {i + 1}: unexpected value (0x{val:08x}), skipped")
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

#!/usr/bin/env python3
"""
BG3 Achievement Enabler for macOS (ARM64)

功能：修补博德之门3可执行文件，使得使用 mod 时仍能解锁 Steam/GOG 成就。

原理：
  游戏在 esv::AchievementManager::UnlockAchievement 和
  esv::AchievementManager::IncreaseAchievementCounter 两个函数开头，
  有一段循环检查是否加载了 addon/mod。如果检测到 mod，则不解锁成就。

  本工具将检查逻辑中的 CBZ（条件跳转：当 mod 数量为 0 时跳过）
  改为 B（无条件跳转：永远跳过 mod 检查循环），从而绕过限制。

使用方法：
  1. 确保游戏未在运行
  2. 运行本脚本：python3 patch_achievements.py
  3. 等待 codesign 重签名完成（约30秒-2分钟，取决于磁盘速度）
  4. 从 Steam 正常启动游戏

恢复方法：
  python3 patch_achievements.py --restore

注意：
  - 游戏更新后需要重新运行本脚本
  - 脚本会自动备份原始文件为 "Baldur's Gate 3.backup"
  - 需要 macOS 自带的 codesign 工具（Xcode Command Line Tools）
"""

import struct
import shutil
import subprocess
import sys
import os
from pathlib import Path

# ============================================================
# 配置
# ============================================================

GAME_DIR = Path.home() / "Library/Application Support/Steam/steamapps/common/Baldurs Gate 3/Baldur's Gate 3.app/Contents/MacOS"
GAME_EXE = GAME_DIR / "Baldur's Gate 3"
BACKUP_EXE = GAME_DIR / "Baldur's Gate 3.backup"

# ============================================================
# 补丁定义
# ============================================================

def find_arm64_offset(game_path: Path) -> int:
    """解析 fat binary，找到 ARM64 slice 的文件偏移"""
    with open(game_path, 'rb') as f:
        magic = struct.unpack('>I', f.read(4))[0]
        if magic != 0xCAFEBABE:
            raise ValueError(f"不是 fat binary (magic: 0x{magic:08x})")

        nfat = struct.unpack('>I', f.read(4))[0]
        for _ in range(nfat):
            cputype = struct.unpack('>I', f.read(4))[0]
            _ = f.read(4)  # cpusubtype
            offset = struct.unpack('>I', f.read(4))[0]
            _ = f.read(4)  # size
            _ = f.read(4)  # align
            if cputype == 0x0100000C:  # CPU_TYPE_ARM64
                return offset

    raise ValueError("未找到 ARM64 slice")


def find_text_section(game_path: Path, arm64_base: int) -> tuple:
    """解析 Mach-O 获取 __TEXT,__text section 的文件偏移和大小"""
    with open(game_path, 'rb') as f:
        f.seek(arm64_base)
        magic = struct.unpack('<I', f.read(4))[0]
        assert magic == 0xFEEDFACF, f"ARM64 slice magic 不正确: 0x{magic:08x}"

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
                        _ = f.read(16)  # segname
                        _ = struct.unpack('<Q', f.read(8))[0]  # addr
                        sect_size = struct.unpack('<Q', f.read(8))[0]  # size
                        sect_fileoff = struct.unpack('<I', f.read(4))[0]  # offset

                        if sectname == '__text':
                            return (arm64_base + sect_fileoff, sect_size)
                    break
            cmd_offset += cmdsize

    raise ValueError("未找到 __TEXT,__text section")


def find_patch_locations(game_path: Path, arm64_base: int) -> list:
    """
    在 ARM64 代码段中搜索 AchievementManager 的 mod 检测模式。

    精确模式（3条连续指令）：
      LDR Xn, [Xm, #0x8B8]   ; 从全局 EocServer 加载指针
      LDR Wk, [Xn, #0x27C]   ; 加载 addon/mod 数量
      CBZ Wk, <skip>          ; 如果为0（无mod）则跳过循环

    将 CBZ 改为 B（无条件跳转），使得无论有无 mod 都跳过检查循环。

    返回 [(file_offset, original_insn, patched_insn), ...]
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
        # 编码: 0xF9400000 | (imm12 << 10) | (Rn << 5) | Rt
        # imm12 = 0x8B8 / 8 = 0x117
        # mask 0xFFFFFC00 匹配 opcode + imm12，允许 Rn/Rt 变化
        if (insn0 & 0xFFFFFC00) != 0xF9445C00:
            continue
        rt0 = insn0 & 0x1F  # Xn（存放模组列表指针）

        # insn1: LDR Wk, [Xn, #0x27C]  (Xn = rt0)
        # imm12 = 0x27C / 4 = 0x9F
        if (insn1 & 0xFFFFFC00) != 0xB9427C00:
            continue
        rn1 = (insn1 >> 5) & 0x1F
        if rn1 != rt0:
            continue
        rt1 = insn1 & 0x1F  # Wk（mod 数量）

        # insn2: CBZ Wk, +offset
        if (insn2 & 0xFF00001F) != (0x34000000 | rt1):
            continue

        # 匹配成功！计算补丁
        file_offset = text_file_offset + (i + 2) * 4
        imm19 = (insn2 >> 5) & 0x7FFFF
        b_insn = 0x14000000 | imm19  # B（无条件跳转），偏移不变
        patches.append((file_offset, insn2, b_insn))

    return patches


def remove_log_files():
    """删除 MacOS 目录下的 log 文件，否则 codesign 会报错"""
    for f in GAME_DIR.glob("*.log"):
        f.unlink()
        print(f"  删除: {f.name}")


# ============================================================
# 主流程
# ============================================================

def do_patch():
    """执行补丁"""
    print("=" * 60)
    print("BG3 Achievement Enabler for macOS")
    print("=" * 60)
    print()

    if not GAME_EXE.exists():
        print(f"错误: 游戏文件不存在\n  {GAME_EXE}")
        sys.exit(1)

    # Step 1: 备份
    if not BACKUP_EXE.exists():
        print("[1/4] 备份原始文件...")
        shutil.copy2(GAME_EXE, BACKUP_EXE)
        print(f"  备份到: {BACKUP_EXE}")
    else:
        print("[1/4] 备份已存在，跳过")
    print()

    # Step 2: 查找补丁位置
    print("[2/4] 分析可执行文件...")
    arm64_base = find_arm64_offset(GAME_EXE)
    print(f"  ARM64 slice 偏移: 0x{arm64_base:x}")

    patches = find_patch_locations(GAME_EXE, arm64_base)
    print(f"  找到 {len(patches)} 个补丁位置")

    if len(patches) < 2:
        print("  警告: 预期至少找到 2 个位置 (UnlockAchievement + IncreaseAchievementCounter)")
        if len(patches) == 0:
            print("  错误: 未找到补丁位置，可能游戏版本不兼容")
            sys.exit(1)
    print()

    # Step 3: 应用补丁
    print("[3/4] 应用补丁...")
    with open(GAME_EXE, 'r+b') as f:
        for i, (offset, original, replacement) in enumerate(patches[:2]):
            f.seek(offset)
            val = struct.unpack('<I', f.read(4))[0]
            if val == original:
                f.seek(offset)
                f.write(struct.pack('<I', replacement))
                func_name = "UnlockAchievement" if i == 0 else "IncreaseAchievementCounter"
                print(f"  ✓ Patch {i+1} ({func_name}):")
                print(f"    偏移: 0x{offset:x}")
                print(f"    CBZ 0x{original:08x} -> B 0x{replacement:08x}")
            elif val == replacement:
                print(f"  → Patch {i+1}: 已经是修补状态")
            else:
                print(f"  ✗ Patch {i+1}: 值不匹配 (0x{val:08x})，跳过")
    print()

    # Step 4: 重新签名
    print("[4/4] 重新签名 (可能需要30秒-2分钟)...")
    remove_log_files()

    result = subprocess.run(
        ["codesign", "--force", "--sign", "-", str(GAME_EXE)],
        capture_output=True, text=True
    )

    if result.returncode == 0:
        print("  ✓ 签名完成")
    else:
        print(f"  ✗ 签名失败: {result.stderr}")
        print("  尝试手动运行: codesign --force --sign - \"" + str(GAME_EXE) + "\"")
        sys.exit(1)

    # 验证
    result = subprocess.run(
        ["codesign", "-v", str(GAME_EXE)],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("  ✓ 签名验证通过")
    else:
        print(f"  ⚠ 签名验证: {result.stderr.strip()}")

    print()
    print("=" * 60)
    print("完成！现在可以从 Steam 正常启动游戏，mod 成就已解锁。")
    print("=" * 60)


def do_restore():
    """恢复原始文件"""
    print("恢复原始文件...")

    if not BACKUP_EXE.exists():
        print(f"错误: 备份文件不存在: {BACKUP_EXE}")
        sys.exit(1)

    shutil.copy2(BACKUP_EXE, GAME_EXE)
    print(f"  ✓ 已恢复: {GAME_EXE}")

    # 重新签名
    print("  重新签名...")
    remove_log_files()
    subprocess.run(["codesign", "--force", "--sign", "-", str(GAME_EXE)], check=True)
    print("  ✓ 完成")


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    if "--restore" in sys.argv:
        do_restore()
    elif "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    else:
        do_patch()

# BG3 Achievement Enabler for macOS（博德之门3 macOS 成就解锁补丁）

[English](README.md) | **简体中文**

一个单文件 Python 脚本，用于修补 macOS 版《博德之门3》的可执行文件，使得**安装 mod 后依然能解锁成就**。

游戏一旦检测到非官方附加组件（mod）就会主动禁用成就。Windows 平台已有运行时插件解决这个问题，macOS 平台一直没有类似工具，因此本项目将同样的思路移植为直接的二进制补丁：

- **零依赖** — 使用 macOS 自带的 Python 3 即可运行
- **改动极小** — 只重写两个成就函数中各一条指令
- **不硬编码地址** — 通过指令模式定位补丁点，游戏更新后大概率仍然可用
- **默认安全** — 自动备份原始文件，并用 `codesign` 重新签名

## 致谢

本项目基于以下开源项目完成：

- [gottyduke/BG3_AchievementEnabler](https://github.com/gottyduke/BG3_AchievementEnabler)（MIT 协议）— Windows 版成就解锁器。其源码说明了游戏中禁用成就的 mod 检测逻辑，是定位 macOS 二进制中相同逻辑的起点。本 macOS 版本思路相同，但以静态修改 ARM64 可执行文件的方式实现，而非运行时 DLL。

## 环境要求

- **Apple Silicon** 的 Mac（脚本修补游戏二进制中的 ARM64 slice）
- 《博德之门3》（Steam 版），安装在默认路径：

  ```
  ~/Library/Application Support/Steam/steamapps/common/Baldurs Gate 3/
  ```

- Python 3（macOS 自带）
- Xcode Command Line Tools（提供 `codesign` 工具）：`xcode-select --install`

## 使用说明

### 打补丁

1. 确保游戏**没有在运行**。
2. 执行：

   ```bash
   python3 patch_achievements.py
   ```

3. 等待重新签名完成（约 30 秒 – 2 分钟，取决于磁盘速度）。
4. 从 Steam 正常启动游戏 — 安装 mod 后成就也能解锁了。

首次运行会在游戏可执行文件旁生成 `Baldur's Gate 3.backup` 备份。请保留它，`--restore` 需要用它来恢复。

### 恢复原始文件

```bash
python3 patch_achievements.py --restore
```

将备份复制回游戏目录并重新签名。

### 注意事项

- **每次游戏更新后需要重新运行脚本。** 更新会替换二进制文件，代码位置也可能变化。
- 使用 Steam 的"验证游戏文件完整性"同样会还原补丁 — 重新运行脚本即可。
- 如果找不到匹配的指令模式，脚本不会写入任何字节，因此不会破坏不认识的游戏版本。

## 补丁原理

### 游戏如何禁用成就

成就被禁用的位置在游戏 server 模块的两个函数中：

- `esv::AchievementManager::UnlockAchievement`
- `esv::AchievementManager::IncreaseAchievementCounter`

两个函数开头都有一段遍历已加载附加组件列表的循环。ARM64 反汇编如下：

```asm
LDR  X23, [X22, #0x8B8]    ; 从全局 EocServer 加载指针
LDR  W24, [X23, #0x27C]    ; 读取 32 位 addon/mod 数量
CBZ  W24, skip_loop        ; 数量为 0（无 mod）-> 跳过检查循环
; --- 循环：遍历每个已加载的 addon ---
BL   IsStandardModule      ; 该项是否为标准模块？
...
; --- 循环结束 ---
TBZ  X28, #0, unlock       ; 全是标准模块 -> 继续解锁
MOV  W0, #0                ; 检测到 mod -> 返回 false
B    epilogue
```

只要加载了任何非标准附加组件，函数就会提前返回，成就永远不会被授予。

### 补丁内容

一条指令，四个字节：

```asm
CBZ  W24, skip_loop        ; 修改前：仅当无 mod 时跳过检查循环
B    skip_loop             ; 修改后：无条件跳过检查循环
```

只去掉跳转条件，跳转目标（19 位立即数）保持不变。检查循环被无条件跳过后，无论加载多少 mod，成就都走正常解锁流程。

### 不依赖硬编码地址的定位方式

游戏更新会移动函数位置，因此脚本不保存任何地址，而是解析 Mach-O 文件（fat binary 头 → ARM64 slice → `__TEXT,__text` section），在整个代码段中搜索 mod 检查的三指令签名：

| 指令                    | 模式                       | 约束             |
| ----------------------- | -------------------------- | ---------------- |
| `LDR Xn, [Xm, #0x8B8]`  | 值 `0xF9445C00`            | 掩码 `0xFFFFFC00` |
| `LDR Wk, [Xn, #0x27C]`  | 值 `0xB9427C00`            | Rn == Xn        |
| `CBZ Wk, <target>`      | 操作码 `0x34000000`        | Rt == Wk        |

为什么选这三条指令？单独的 `LDR + CBZ` 在约 500 MB 的二进制中能匹配约 60 处，误命中太多；加上前面的 `LDR ..., [#0x8B8]` 锚点后缩小到 4 处，其中地址最低的两处恰好就是两个 `AchievementManager` 函数。脚本只修补这两处，忽略其余误命中。

### 定位过程（逆向分析记录）

1. **从 Windows 源码理解逻辑。** gottyduke 的 Windows 版源码告诉我们游戏做了什么：一段遍历 addon 列表的循环，检测到 mod 就跳过成就解锁。关键信息：这是一个关于"addon 数量"和"是否跳过"的检查逻辑。
2. **用字符串作为路标。** 二进制中的字符串常量是最好的锚点：`strings "Baldur's Gate 3" | grep -i achievement` 找到 `LoadAchievementsDisabled`（成就禁用）与 `HasMods`（mod 检测），说明相关代码就在附近。
3. **用符号表定位函数。** Windows 二进制通常被 strip 掉符号，而 macOS 的 Mach-O 保留了 C++ 符号名，`nm` 直接给出两个 `AchievementManager` 函数的地址 — 整个过程中最大的捷径。
4. **反汇编并交叉验证。** `otool` 显示两个函数开头都有 `LDR [#0x8B8] / LDR [#0x27C] / CBZ` 序列；Windows x86 版的同一个函数读取的是同一个字段（`movl 0x27c(%r12), %r13d`）— 偏移 `0x27C` 在两个架构上完全一致，确认是同一份逻辑。
5. **最小修改。** 把 `CBZ` 改成 `B`、跳转目标不变 — 完成。

方法论小结：利用已知信息逐步缩小范围 — 从 500 MB 二进制 → 字符串锁定区域 → 符号名锁定函数 → 反汇编理解逻辑 → 一条指令解决问题。

## 常见问题

**安全吗？**
补丁只修改两个函数中各一条指令，写入前校验原始字节，且保留完整备份；`--restore` 随时可恢复原始文件。

**会影响 Steam 的成就上报吗？**
不会。补丁只移除了本地的 mod 检查，成就仍通过游戏正常代码路径解锁，Steam/GOG 照常记录。

**支持 Intel Mac 吗？**
不支持 — 脚本只修补游戏二进制中的 ARM64 slice。

## 许可证

[MIT](LICENSE)，与参考项目一致。

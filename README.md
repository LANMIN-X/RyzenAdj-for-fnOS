# Ryzen 功耗控制（fnOS）

通过 RyzenAdj 读取 Ryzen APU 的 SMU 功耗墙、实时功耗和温度，设置或恢复 STAPM / PPT Fast / PPT Slow 限制，并运行限时全核负载。

## 安装与使用

1. 在飞牛应用中心选择“手动安装”，打开 `ryzen-power-control.fpk`。
2. 从桌面打开“Ryzen 功耗控制”。只有管理员可以访问控制接口。
3. 输入 10–45W 的整数并应用，会同时设置三项功耗墙。当前验证范围针对本机硬件。
4. “恢复记录值”会恢复应用首次启动时读取到的三项限制。
5. 负载测试提供 30、60、120 秒全核测试，可手动停止；CPU 温度达到 85°C 会自动停止。

## 界面

界面从 fnOS 桌面读取飞牛自身的颜色和控件主题变量，亮暗模式会随系统切换。主题变量只在本机应用窗口内读取，不依赖外部资源。

功耗墙设置经 AMD SMU 写入，仅在当前开机周期内有效。重启后由 BIOS 重新接管；应用启动本身不会修改功耗墙。

## 已验证环境

- fnOS x86，Linux 6.18.18
- AMD Ryzen 7 5800H（Cezanne），BIOS 605
- RyzenAdj v0.19.0

应用以 root 运行以访问 `/dev/mem`。包内包含 RyzenAdj Linux x86_64 程序及其许可证；`libpci.so.3`、`libudev.so.1` 由 fnOS 提供。其他 CPU 和 BIOS 配置尚未验证。

# 静态切换动态立绘的首帧跳动

2026-10-08 已修复。首帧初始化使用 `performance.now()`，后续播放使用 `requestAnimationFrame` 的帧时间戳；帧时间戳表示帧的起始时间，可以早于初始化过程中读取的当前时间。旧代码只把增量限制到最多 50ms，没有限制负值，因此第一次 RAF 会把动画轨道退回到时间 0 之前。

真实软件逐帧记录：

| 场景 | 修复前前三次绘制的轨道时间（秒） | 修复后前三次绘制的轨道时间（秒） |
| --- | --- | --- |
| PC | 0、-0.0929、-0.0429 | 0、0、0.05 |
| 移动端 | 0、-0.1332、-0.0832 | 0、0、0.05 |

相机、400×520 布局、800×1040 缓冲、屏幕位置及外层滚动位置保持一致。H001 的首帧非透明边界为 `[32,207,740,823]`，旧代码负时间帧变为 `[45,215,749,828]`；这解释了先跳到另一姿态，再回到开始位置播放的现象。

## 修复

- `frontend/encyclopedia-player.js` 首次调用 `draw(0)`，只绘制时间 0 的准确姿态，不启动播放时钟；首个 RAF 建立时间基准，从下一个 RAF 开始推进动画。
- 可见性恢复和取消减少动效时清空旧时间基准。正常播放只使用 RAF 时间戳，隐藏期间不累计动画时间。
- 每次增量限制在 `[0,0.05]` 秒，记录的时间戳保持单调，防止延迟或乱序时间造成倒退。
- Ark2 的 `out/角色图鉴样板/portrait-player.js` 同步处理同类问题。软件脚本缓存版本升至 v5，样板升至 v8。

## 验证

```powershell
python tests/test_encyclopedia_portrait_switch.py -v
python tests/test_encyclopedia_framing_live.py -v
```

新切换测试的 4 项方法覆盖 11 个场景，包括 PC/移动、正常播放/减少动效、不同角色、加载时维持静态、重复切换和快速取消旧请求。包裹真实 WebGL 绘制记录首 8 帧，不重置姿态或手动重绘；旧播放器稳定复现负时间失败，修复版全部通过，首帧相机和像素与正式静态 PNG 一致。原有 5 项取景检查也全部通过，包括 DPR 1/2、透明附件、缩放和冻结状态下重绘。

软件前后逐帧记录在 `build/portrait-switch/desktop-trace-before.json`、`desktop-trace-after.json` 及对应 mobile 文件；新测试记录在 `build/portrait-switch-regression/verification.json`，旧版失败证据在同目录的 `verification-before.json`。隔离测试服务及浏览器均已关闭。

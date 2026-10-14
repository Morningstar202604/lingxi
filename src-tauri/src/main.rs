//! 桌面可执行入口。
//!
//! 全部逻辑在 lib.rs 的 `run()` 里 —— 这样同一份代码既能编成桌面 bin，
//! 也能编成 Android/iOS 需要的 cdylib（移动端由 Tauri 的 JNI 层调 run()）。
// 桌面构建时隐藏控制台窗口（debug 构建保留，方便看日志）
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    lingxi_desktop_lib::run();
}

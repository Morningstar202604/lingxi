#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{Manager, Runtime};

mod python_sidecar;
use python_sidecar::PythonSidecar;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            // 启动 Python sidecar
            let mut sidecar = PythonSidecar::new(app.handle())?;
            let handle = app.handle().clone();

            tauri::async_runtime::spawn(async move {
                if let Err(e) = sidecar.start(handle).await {
                    eprintln!("Failed to start sidecar: {}", e);
                    // 即使 sidecar 启动失败也继续运行，前端会显示错误状态
                }
            });

            // 退出时关闭 sidecar
            let handle = app.handle().clone();
            app.once("close_requested", move |_| {
                tauri::async_runtime::block_on(async move {
                    if let Err(e) = sidecar.stop().await {
                        eprintln!("Failed to stop sidecar: {}", e);
                    }
                });
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn main() {
    run();
}
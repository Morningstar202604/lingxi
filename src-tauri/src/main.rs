#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::{Arc, Mutex};
use tauri::{Listener, Manager};

mod python_sidecar;
use python_sidecar::{PythonSidecar, SidecarState};

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
            let sidecar = Arc::new(Mutex::new(PythonSidecar::new(app.handle())?));

            // 注册为托管状态：前端 invoke("get_backend_port") / ("get_admin_token")
            // 从这里读运行时端口与令牌（桌面版不再依赖构建期环境变量）。
            app.manage(SidecarState(sidecar.clone()));

            // 启动 → 成功则进入守护循环（进程退出/无响应会自动重启并打印死因）
            let start_handle = app.handle().clone();
            let sidecar_start = sidecar.clone();
            tauri::async_runtime::spawn(async move {
                match PythonSidecar::start(&sidecar_start, &start_handle).await {
                    Ok(port) => {
                        println!(">>> 后端已就绪（端口 {}），进入守护模式", port);
                        PythonSidecar::supervise(sidecar_start, start_handle).await;
                    }
                    Err(e) => {
                        eprintln!(">>> 后端启动失败：{:#}", e);
                        eprintln!(">>> App 保持运行，可查看控制台排查；修复后重新启动即可。");
                    }
                }
            });

            // 关闭 App：停止后端（幂等；若守护已清理过则空操作）
            let sidecar_stop = sidecar.clone();
            app.once("close_requested", move |_| {
                tauri::async_runtime::block_on(PythonSidecar::stop_shared(&sidecar_stop));
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            python_sidecar::get_backend_port,
            python_sidecar::get_admin_token
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn main() {
    run();
}

//! 智犀 ZhiXi 客户端库入口（桌面 bin 与 Android/iOS cdylib 共用）。
//!
//! 平台差异（架构层面的核心区别）：
//! - **桌面**（Windows/macOS/Linux）：把 Python 引擎作为 sidecar 自动拉起，
//!   监听 127.0.0.1:<动态端口>，随机令牌；客户端运行时拿到真实地址。
//! - **移动**（Android/iOS）：OS 禁止 fork 任意二进制、也没有 Python 运行时，
//!   **引擎不可能跑在手机上**。客户端连接用户自己部署的引擎地址
//!   （局域网 / 公网 / VPN），这就是 README 里"客户端 + 引擎服务"的形态。
//!
//! 两条路径都收敛到同一个 `EngineEndpoint`（见 engine_endpoint.rs），
//! 前端只 invoke 一条命令就能拿到地址与令牌，不需要知道背后是什么。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod engine_endpoint;

#[cfg(desktop)]
mod python_sidecar;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_opener::init());

    // ── 桌面：装配 sidecar 生命周期管理 ──
    #[cfg(desktop)]
    let builder = builder
        .setup(|app| {
            use std::sync::{Arc, Mutex};

            use python_sidecar::{PythonSidecar, SidecarState};
            use tauri::{Listener, Manager};

            let sidecar = Arc::new(Mutex::new(PythonSidecar::new(app.handle())?));
            app.manage(SidecarState(sidecar.clone()));

            let start_handle = app.handle().clone();
            let sidecar_start = sidecar.clone();
            tauri::async_runtime::spawn(async move {
                match PythonSidecar::start(&sidecar_start, &start_handle).await {
                    Ok(port) => {
                        println!(">>> 后端已就绪（端口 {}），进入守护模式", port);
                        // 把真实端口与令牌登记到跨平台端点（前端统一从这里读）
                        let token = sidecar_start
                            .lock()
                            .map(|g| g.admin_token.clone())
                            .unwrap_or_default();
                        engine_endpoint::set_local(port, &token);
                        PythonSidecar::supervise(sidecar_start, start_handle).await;
                    }
                    Err(e) => {
                        eprintln!(">>> 后端启动失败：{:#}", e);
                        eprintln!(">>> App 保持运行；可在「连接」页改用远程引擎地址。");
                    }
                }
            });

            let sidecar_stop = sidecar.clone();
            app.once("close_requested", move |_| {
                tauri::async_runtime::block_on(PythonSidecar::stop_shared(&sidecar_stop));
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            python_sidecar::get_backend_port,
            python_sidecar::get_admin_token,
            engine_endpoint::get_engine_endpoint,
            engine_endpoint::set_engine_endpoint,
            engine_endpoint::probe_engine_endpoint,
        ]);

    // ── 移动：不启动 sidecar；端点在首启由用户填写（「连接」页）──
    #[cfg(mobile)]
    let builder = builder
        .setup(|_app| {
            println!(">>> 移动端模式：引擎不在本机运行，请在「连接」页填写引擎地址。");
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            engine_endpoint::get_engine_endpoint,
            engine_endpoint::set_engine_endpoint,
            engine_endpoint::probe_engine_endpoint,
        ]);

    builder
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

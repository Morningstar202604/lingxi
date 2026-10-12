#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::{Arc, Mutex};
use tauri::{Listener, Manager};

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
            let sidecar = Arc::new(Mutex::new(PythonSidecar::new(app.handle())?));
            let sidecar_start = sidecar.clone();
            let handle = app.handle().clone();

            tauri::async_runtime::spawn(async move {
                // Lock, extract what we need, then release lock before await
                let (executable, admin_token, port) = {
                    let guard = sidecar_start.lock().unwrap();
                    (guard.executable.clone(), guard.admin_token.clone(), guard.port)
                };
                
                // Start sidecar without holding the lock
                if let Err(e) = PythonSidecar::start_standalone(executable, admin_token, port, handle).await {
                    eprintln!("Failed to start sidecar: {}", e);
                }
            });

            let sidecar_stop = sidecar.clone();
            app.once("close_requested", move |_| {
                let mut guard = sidecar_stop.lock().unwrap();
                tauri::async_runtime::block_on(async move {
                    if let Err(e) = guard.stop().await {
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
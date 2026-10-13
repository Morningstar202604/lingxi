use tauri_build::build;

fn main() {
    // 在 Tauri v2 中，sidecar 配置在 tauri.conf.json 的 bundle.externalBin 中
    // build.rs 只需调用 build()，不需要传递 Attributes
    build();
}

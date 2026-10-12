use std::env;
use std::path::PathBuf;
use tauri_build::{build, Attributes};

fn main() {
    // 1. sidecar 路径：由 scripts/build_sidecar.py 生成到 src-tauri/dist/
    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let sidecar_path = manifest_dir.join("dist").join("lingxi-server");

    // 2. 声明 sidecar，Tauri 会在打包时自动包含
    let mut attrs = Attributes::new();
    attrs.sidecar = vec![("lingxi-server".to_string(), sidecar_path)];

    // 3. 编译时检查 sidecar 是否存在（开发模式可跳过）
    if !cfg!(debug_assertions) {
        let exe = if cfg!(windows) {
            attrs.sidecar[0].1.with_extension("exe")
        } else {
            attrs.sidecar[0].1.clone()
        };
        if !exe.exists() {
            panic!(
                "Python sidecar not found at {:?}. Run `python scripts/build_sidecar.py` first.",
                exe
            );
        }
    }

    build(attrs);
}
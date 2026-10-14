#!/usr/bin/env node
/**
 * Tauri 前端构建包装脚本（放在 src-tauri/ 下）。
 *
 * 🔗 为什么需要它
 *     tauri.conf.json 的 beforeBuildCommand 工作目录**平台不一致**：
 *     - 桌面构建：cwd = src-tauri/
 *     - Android 构建：cwd = src-tauri/gen/android/  ← 深两级
 *     因此 `cd ../web` 在 Android 下找不到目录（实测报
 *     "The system cannot find the path specified"），`../scripts/x.mjs`
 *     同样解析不到。脚本放在 src-tauri/ 且用**自身绝对路径**定位项目根，
 *     两端都成立（无论 cwd 是什么）。
 *
 * 🤖 AI 避坑
 *     - 必须设 TAURI=1：next.config.js 靠它切换 `output: "export"` 静态导出，
 *       移动端 WebView 加载的是静态产物（不是 dev server）。
 *     - 不直接 spawn "npm"：Windows 下 npm 实际是 npm.cmd，
 *       - shell:false + npm.cmd → EINVAL（Node 拒绝直接执行 .cmd，实测）
 *       - shell:true  → Node 24 报 DEP0190 注入弃用警告
 *       两条都不干净，改为用当前 Node 执行 next 的 CLI 入口（零 shell）。
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// 本文件在 src-tauri/ 下；项目根是它的上一级
const srcTauri = dirname(fileURLToPath(import.meta.url));
const root = resolve(srcTauri, "..");
const webDir = join(root, "web");

if (!existsSync(join(webDir, "package.json"))) {
  console.error(`[tauri-web-build] 找不到前端目录: ${webDir}（cwd=${process.cwd()}）`);
  process.exit(1);
}

// next 的 CLI 入口（等价于 `npm run build`，但绕开 npm 包装层）
const nextBin = join(webDir, "node_modules", "next", "dist", "bin", "next");
if (!existsSync(nextBin)) {
  console.error(`[tauri-web-build] 未找到 next CLI: ${nextBin}（先 npm install）`);
  process.exit(1);
}

console.log(`[tauri-web-build] 构建前端（TAURI=1 静态导出）: ${webDir}`);

const result = spawnSync(process.execPath, [nextBin, "build"], {
  cwd: webDir,
  stdio: "inherit",
  env: { ...process.env, TAURI: "1" },
  shell: false,
});

if (result.error) {
  console.error("[tauri-web-build] 无法启动构建:", result.error.message);
  process.exit(1);
}
if (result.status !== 0) {
  console.error(`[tauri-web-build] 前端构建失败，退出码 ${result.status}`);
  process.exit(result.status ?? 1);
}

const outDir = join(webDir, "out");
if (!existsSync(join(outDir, "index.html"))) {
  console.error(`[tauri-web-build] 静态导出缺失: ${outDir}/index.html`);
  process.exit(1);
}
console.log(`[tauri-web-build] 完成: ${outDir}`);

// 开发服务器启动器：让 `npm run dev` 在 Windows/macOS/Linux 都能用同一套端口配置。
//
// 为什么不写 "next dev -p ${PORT:-3000}"：shell 变量默认值语法只在 bash 有效，
// npm 在 Windows 上通过 cmd.exe 执行，会把 ${PORT:-3000} 当字面量传给 next，
// 导致 rc=1（本轮实测踩过）。Next 原生只认 -p 或 PORT 环境变量，故用 node 兜一层。
import { spawn } from "node:child_process";

const port = process.env.PORT || "3000";
const host = process.env.HOST || "127.0.0.1";

// Windows 上 next 的 bin 是 next.cmd，必须经 shell 调；其余平台直接 spawn 更安全
// （shell: true 会触发 DEP0190 参数注入告警）。
const isWin = process.platform === "win32";
const child = spawn(isWin ? "npx.cmd" : "npx", ["next", "dev", "-p", port, "-H", host], {
  stdio: "inherit",
  shell: isWin,
});

child.on("exit", (code, signal) => {
  process.exit(code ?? (signal ? 1 : 0));
});

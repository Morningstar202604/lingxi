/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        lingxi: {
          navy: "#1B2A4A",
          orange: "#E8963E",
          green: "#2ECC71",
          red: "#E74C3C",
          blue: "#5B8DEF",
          slate: "#2C3E50",
          muted: "#95A5A6",
        },
      },
      // 字体栈全部使用系统已装字体：国内网络访问不了 Google Fonts / Fontshare，
      // next/font/google 会在构建时请求 fonts.googleapis.com 而失败。
      // 品牌规范里的「思源宋体 / Inter」对应下面这两个系统中文字族，
      // 思源（Source Han / Noto Sans CJK）在主流国产系统与多数 Linux 发行版预装。
      fontFamily: {
        sans: [
          "PingFang SC",
          "HarmonyOS Sans SC",
          "Microsoft YaHei",
          "Source Han Sans SC",
          "Noto Sans CJK SC",
          "WenQuanYi Micro Hei",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "sans-serif",
        ],
        serif: [
          "Source Han Serif SC",
          "Noto Serif CJK SC",
          "Songti SC",
          "SimSun",
          "serif",
        ],
        mono: [
          "JetBrains Mono",
          "Cascadia Mono",
          "Consolas",
          "Sarasa Mono SC",
          "Noto Sans Mono CJK SC",
          "monospace",
        ],
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "slide-in": {
          "0%": { opacity: "0", transform: "translateX(-8px)" },
          "100%": { opacity: "1", transform: "translateX(0)" },
        },
        "ring-out": {
          "0%": { opacity: "0.8", transform: "scale(0.8)" },
          "100%": { opacity: "0", transform: "scale(2.2)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.35s ease-out both",
        "slide-in": "slide-in 0.25s ease-out both",
        "ring-out": "ring-out 1.6s ease-out infinite",
      },
    },
  },
  plugins: [],
};

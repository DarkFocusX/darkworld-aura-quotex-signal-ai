import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: {
          900: "#05070d",
          800: "#0a0e18",
          700: "#0f1524",
          600: "#151c2e",
          500: "#1d2740",
          400: "#2a3556",
        },
        call: "#12d18e",
        put: "#ff4d6d",
        warn: "#ffb020",
        ink: { 100: "#eef2ff", 300: "#a9b4d0", 500: "#6b7793" },
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      boxShadow: {
        glow: "0 0 0 1px rgba(255,255,255,0.04), 0 18px 50px -20px rgba(0,0,0,0.9)",
      },
      keyframes: {
        pulseRing: {
          "0%": { transform: "scale(0.96)", opacity: "0.85" },
          "70%": { transform: "scale(1.06)", opacity: "0" },
          "100%": { transform: "scale(1.06)", opacity: "0" },
        },
        riseIn: {
          "0%": { opacity: "0", transform: "translateY(10px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-400px 0" },
          "100%": { backgroundPosition: "400px 0" },
        },
      },
      animation: {
        pulseRing: "pulseRing 1.8s cubic-bezier(0.2,0.7,0.3,1) infinite",
        riseIn: "riseIn 0.35s ease-out both",
        shimmer: "shimmer 1.4s linear infinite",
      },
    },
  },
  plugins: [],
};

export default config;

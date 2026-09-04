/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        verdict: {
          ok: "#22c55e",
          warn: "#f59e0b",
          bad: "#ef4444",
          missed: "#94a3b8",
        },
      },
    },
  },
  plugins: [],
};

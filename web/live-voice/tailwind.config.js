/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        veas: {
          bg: "#0f1419",
          surface: "#1a2128",
          accent: "#7c9cf2",
          muted: "#8794a3",
        },
      },
    },
  },
  plugins: [],
};

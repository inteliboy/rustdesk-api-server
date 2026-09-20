// Builds src/rustdesk_api/web/static/css/tailwind.css (committed; see package.json).
// Run `npm install` once, then `npm run build:css` after changing any template or
// app.js class names. tests/integration/test_web_security.py fails if the committed
// stylesheet is out of date.
const colors = require("tailwindcss/colors");
const plugin = require("tailwindcss/plugin");

const SHADES = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900];

// "#2563eb" -> "37 99 235", the form Tailwind's <alpha-value> placeholder needs.
const triplet = (hex) => {
  const n = parseInt(hex.slice(1), 16);
  return `${(n >> 16) & 255} ${(n >> 8) & 255} ${n & 255}`;
};

// Selectable accent colors, exposed to the UI as CSS variables --brand-<shade>.
// Blue is the default; `data-accent` on <html> switches it.
const ACCENTS = {
  blue: colors.blue,
  indigo: colors.indigo,
  violet: colors.violet,
  emerald: colors.emerald,
  rose: colors.rose,
  amber: colors.amber,
};

const rampOf = (name) => Object.fromEntries(SHADES.map((s) => [s, `rgb(var(--${name}-${s}) / <alpha-value>)`]));

module.exports = {
  darkMode: ["selector", "[data-theme=\"dark\"]"],
  content: ["./src/rustdesk_api/web/templates/**/*.html", "./src/rustdesk_api/web/static/js/**/*.js"],
  theme: {
    extend: {
      colors: {
        // Same class names as stock Tailwind (bg-slate-50, text-slate-500, ...) but
        // the values come from CSS variables, which is how dark mode re-skins the
        // whole UI without touching each template. See frontend/tailwind.css.
        slate: rampOf("slate"),
        brand: rampOf("brand"),
        surface: "rgb(var(--surface) / <alpha-value>)",
        link: "rgb(var(--link) / <alpha-value>)",
      },
    },
  },
  plugins: [
    plugin(({ addBase }) => {
      const base = {};
      for (const [name, palette] of Object.entries(ACCENTS)) {
        const selector = name === "blue" ? ':root, [data-accent="blue"]' : `[data-accent="${name}"]`;
        base[selector] = Object.fromEntries(SHADES.map((s) => [`--brand-${s}`, triplet(palette[s])]));
      }
      addBase(base);
    }),
  ],
};

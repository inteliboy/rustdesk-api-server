// Applies the viewer's theme (light / dark / follow-system) and accent color.
// Loaded synchronously in <head>, before first paint, so a dark-mode viewer never
// sees a flash of the light theme. The choice is a per-viewer convenience kept in
// localStorage; every access is wrapped because storage can be blocked or throw.
(function () {
  var THEMES = ["system", "light", "dark"];
  var ACCENTS = ["blue", "indigo", "violet", "emerald", "rose", "amber"];
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function read(key, allowed, fallback) {
    try {
      var value = window.localStorage.getItem(key);
      return allowed.indexOf(value) !== -1 ? value : fallback;
    } catch (e) {
      return fallback;
    }
  }

  function write(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (e) {
      // Storage unavailable (private mode, blocked): the choice just won't persist.
    }
  }

  var state = { theme: read("rd_theme", THEMES, "system"), accent: read("rd_accent", ACCENTS, "blue") };

  function apply() {
    var dark = state.theme === "dark" || (state.theme === "system" && media && media.matches);
    root.setAttribute("data-theme", dark ? "dark" : "light");
    root.setAttribute("data-accent", state.accent);
  }

  if (media && media.addEventListener) {
    media.addEventListener("change", function () {
      if (state.theme === "system") apply();
    });
  }

  window.appTheme = {
    THEMES: THEMES,
    ACCENTS: ACCENTS,
    get: function () {
      return { theme: state.theme, accent: state.accent };
    },
    setTheme: function (theme) {
      if (THEMES.indexOf(theme) === -1) return;
      state.theme = theme;
      write("rd_theme", theme);
      apply();
    },
    setAccent: function (accent) {
      if (ACCENTS.indexOf(accent) === -1) return;
      state.accent = accent;
      write("rd_accent", accent);
      apply();
    },
  };

  apply();
})();

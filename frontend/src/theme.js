// Theme: light is ALWAYS the default — never OS-following. Dark is a
// deliberate user choice, persisted in localStorage and applied as a
// data-theme attribute on <html> (see the pre-paint script in index.html
// that prevents a light flash for returning dark users).
const KEY = "arya_theme";

export function getTheme() {
  try {
    return localStorage.getItem(KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light"; // storage blocked (private mode) — default light
  }
}

export function applyTheme(theme) {
  if (theme === "dark") document.documentElement.dataset.theme = "dark";
  else delete document.documentElement.dataset.theme;
}

export function setTheme(theme) {
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* storage blocked — theme still applies for this page's lifetime */
  }
  applyTheme(theme);
}

"use strict";
(() => {
  const storageKey = "watt-profile-theme";
  let theme = "dark";
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved === "light" || saved === "dark") theme = saved;
  } catch { /* Theme switching also works when browser storage is unavailable. */ }
  document.documentElement.dataset.theme = theme;
  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    const render = () => {
      document.documentElement.dataset.theme = theme;
      button.textContent = theme === "dark" ? "Light mode" : "Dark mode";
      button.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} mode`);
    };
    render();
    button.addEventListener("click", () => {
      theme = theme === "dark" ? "light" : "dark";
      render();
      try { localStorage.setItem(storageKey, theme); } catch { /* Keep the in-page choice. */ }
    });
  });
})();

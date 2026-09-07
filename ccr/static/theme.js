/* ccr — pre-paint theme bootstrap (loaded synchronously in <head>, before app.js).
   Reads the persisted preference (auto|light|dark), resolves "auto" against the OS
   colour scheme, stamps <html data-theme> and enables the matching highlight.js sheet,
   so the first paint already uses the right colours. */
(function () {
  'use strict';
  var pref = 'auto';
  try { pref = localStorage.getItem('ccr:theme') || 'auto'; } catch (e) { /* storage blocked */ }
  if (pref !== 'light' && pref !== 'dark') { pref = 'auto'; }
  var dark = pref === 'dark' || (pref === 'auto' && window.matchMedia &&
    window.matchMedia('(prefers-color-scheme: dark)').matches);
  var theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.themePref = pref;
  var light = document.getElementById('hl-light');
  var darkSheet = document.getElementById('hl-dark');
  if (light) { light.disabled = dark; }
  if (darkSheet) { darkSheet.disabled = !dark; }
})();

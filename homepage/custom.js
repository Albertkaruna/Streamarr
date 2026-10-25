// Service links use the placeholder host "pc.lan"; point them at whatever address this dashboard
// was opened with, so tiles keep working when the hotspot gives the PC a new IP.
// Loaded automatically by Homepage from /app/config/custom.js (no setting needed).
(() => {
  const PH = "//pc.lan";
  const swap = (u) => (typeof u === "string" ? u.replace(PH, "//" + location.hostname) : u);

  const fix = () =>
    document.querySelectorAll('a[href*="' + PH + '"]').forEach((a) => (a.href = swap(a.href)));

  // Quick-launch (type-to-search) opens results with window.open(href), bypassing the <a> tags.
  const open = window.open;
  window.open = (url, ...rest) => open.call(window, url instanceof URL ? swap(url.href) : swap(url), ...rest);

  // Cards render client-side and re-render on widget refresh; batch fixes to one per frame.
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => ((queued = false), fix()));
  }).observe(document.body, { childList: true, subtree: true });
  fix();
})();

// Apply persisted theme/density before first paint (no flash). Mirrors src/lib/theme.ts. Served as a file so the
// CloudFront CSP (script-src 'self') allows it — no inline scripts anywhere in the Workbench.
try {
  var t = localStorage.getItem('aiq-wb-theme');
  if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  var d = localStorage.getItem('aiq-wb-density');
  if (d === 'compact') document.documentElement.setAttribute('data-density', 'compact');
} catch (e) {}

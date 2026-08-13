/*
 * Markdown-lite renderer for admin pages: escapes HTML, then renders
 * ```fenced code```, `inline code`, and **bold**. Applied to any element
 * carrying the `md-render` class.
 */
(function () {
  "use strict";

  function mdLite(text) {
    var e = document.createElement("div");
    e.textContent = text || "";
    var s = e.innerHTML;
    s = s.replace(/\n?```(?:[\w-]*)\n?([\s\S]*?)\n?```\n?/g, function (_, code) {
      return '<pre class="md-code"><code>' + code + "</code></pre>";
    });
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    return s;
  }

  function apply(root) {
    (root || document).querySelectorAll(".md-render").forEach(function (el) {
      el.innerHTML = mdLite(el.textContent);
    });
  }

  window.mdLite = mdLite;
  window.__mdApply = apply;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { apply(); });
  } else {
    apply();
  }
})();

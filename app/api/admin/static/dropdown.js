/*
 * Custom dropdown enhancement for the admin dashboard.
 *
 * Replaces the browser's native (unstylable, misaligned) <select> popups and
 * <datalist> combobox with a consistent, theme-aware panel. The underlying
 * form controls stay in the DOM and in sync, so normal form submission is
 * unaffected — this is purely a presentation layer.
 *
 *   - every <select> in the main content is enhanced (single + multiple)
 *   - <input data-combobox data-options='[{"id","label"}]'> becomes a
 *     searchable combobox that still allows free-typed values
 */
(function () {
  "use strict";

  var openPanel = null;

  // Position a fixed panel against its control's viewport rect, flipping above
  // the control when there isn't room below. Fixed positioning lets the panel
  // escape clipping ancestors (table cells, scroll containers).
  function place(wrap) {
    var panel = wrap.querySelector(".dd-panel");
    var ctrl = wrap.querySelector(".dd-control") || wrap.querySelector(".dd-combo-input");
    if (!panel || !ctrl) return;
    var r = ctrl.getBoundingClientRect();
    var gap = 6;
    var h = panel.offsetHeight || 240;
    var below = window.innerHeight - r.bottom;
    var flip = below < h + gap && r.top > below;
    wrap.classList.toggle("dd-up", flip);
    panel.style.width = r.width + "px";
    panel.style.left = r.left + "px";
    panel.style.top = (flip ? r.top - h - gap : r.bottom + gap) + "px";
  }

  function reposition() {
    if (openPanel) place(openPanel);
  }

  function closeOpen() {
    if (openPanel) {
      openPanel.classList.remove("open");
      openPanel.classList.remove("dd-up");
      openPanel = null;
    }
  }

  document.addEventListener("click", function (e) {
    if (openPanel && !openPanel.contains(e.target)) closeOpen();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeOpen();
  });
  window.addEventListener("scroll", reposition, true);
  window.addEventListener("resize", reposition);

  var CARET =
    '<svg class="dd-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';

  function makeWrap(cls) {
    var w = document.createElement("div");
    w.className = "dd" + (cls ? " " + cls : "");
    return w;
  }

  function toggle(wrap) {
    if (openPanel === wrap) {
      closeOpen();
    } else {
      closeOpen();
      wrap.classList.add("open");
      openPanel = wrap;
      place(wrap);
      var s = wrap.querySelector(".dd-search");
      if (s) setTimeout(function () { s.focus(); }, 0);
    }
  }

  /* ------------------------------------------------------------------ select */
  function enhanceSelect(sel) {
    if (sel.dataset.ddDone) return;
    sel.dataset.ddDone = "1";

    var multiple = sel.multiple;
    var max = parseInt(sel.dataset.max || "0", 10) || 0;
    var wrap = makeWrap(multiple ? "dd-multi" : "");
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    sel.classList.add("dd-native");
    sel.setAttribute("tabindex", "-1");
    sel.setAttribute("aria-hidden", "true");

    var control = document.createElement("button");
    control.type = "button";
    control.className = "dd-control";
    control.innerHTML = '<span class="dd-value"></span>' + CARET;
    wrap.appendChild(control);

    var panel = document.createElement("div");
    panel.className = "dd-panel";
    wrap.appendChild(panel);

    var options = Array.prototype.slice.call(sel.options).filter(function (o) {
      return !(o.disabled && o.value === "");
    });
    var searchable = sel.hasAttribute("data-searchable") || options.length > 8;

    var search = null;
    if (searchable) {
      var sb = document.createElement("div");
      sb.className = "dd-searchbar";
      search = document.createElement("input");
      search.type = "text";
      search.className = "dd-search";
      search.placeholder = "Search…";
      sb.appendChild(search);
      panel.appendChild(sb);
    }

    var list = document.createElement("div");
    list.className = "dd-list";
    panel.appendChild(list);

    if (!options.length) {
      var empty = document.createElement("div");
      empty.className = "dd-empty";
      empty.textContent = sel.dataset.emptyText || "No options";
      list.appendChild(empty);
    }

    var rows = options.map(function (opt) {
      var row = document.createElement("div");
      row.className = "dd-option";
      row.setAttribute("role", "option");
      row.dataset.value = opt.value;
      row.innerHTML =
        (multiple ? '<span class="dd-check"></span>' : "") +
        '<span class="dd-opt-label"></span>';
      row.querySelector(".dd-opt-label").textContent = opt.textContent;
      list.appendChild(row);
      row.addEventListener("click", function () {
        if (multiple) {
          if (!opt.selected && max && selectedCount() >= max) return;
          opt.selected = !opt.selected;
          sync();
          sel.dispatchEvent(new Event("change", { bubbles: true }));
        } else {
          sel.value = opt.value;
          sync();
          sel.dispatchEvent(new Event("change", { bubbles: true }));
          closeOpen();
        }
      });
      return { opt: opt, row: row };
    });

    function selectedCount() {
      return options.filter(function (o) { return o.selected; }).length;
    }

    function sync() {
      var chosen = options.filter(function (o) { return o.selected; });
      var val = wrap.querySelector(".dd-value");
      if (!chosen.length) {
        val.textContent = sel.dataset.placeholder || "Select…";
        val.classList.add("dd-placeholder");
      } else if (multiple) {
        val.classList.remove("dd-placeholder");
        val.textContent =
          chosen.length <= 2
            ? chosen.map(function (o) { return o.textContent; }).join(", ")
            : chosen.length + " selected";
      } else {
        val.classList.remove("dd-placeholder");
        val.textContent = chosen[0].textContent;
      }
      rows.forEach(function (r) {
        r.row.classList.toggle("selected", r.opt.selected);
      });
    }

    if (search) {
      search.addEventListener("input", function () {
        var q = search.value.trim().toLowerCase();
        rows.forEach(function (r) {
          var hit = r.opt.textContent.toLowerCase().indexOf(q) !== -1;
          r.row.style.display = hit ? "" : "none";
        });
      });
      search.addEventListener("click", function (e) { e.stopPropagation(); });
    }

    control.addEventListener("click", function (e) {
      e.stopPropagation();
      toggle(wrap);
    });

    sel.addEventListener("change", sync);
    sync();
  }

  /* ---------------------------------------------------------------- combobox */
  function enhanceCombobox(input) {
    if (input.dataset.ddDone) return;
    input.dataset.ddDone = "1";

    var opts = [];
    try {
      opts = JSON.parse(input.dataset.options || "[]");
    } catch (e) {
      opts = [];
    }

    var wrap = makeWrap("dd-combo");
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    input.classList.add("dd-combo-input");
    input.setAttribute("autocomplete", "off");

    var caret = document.createElement("span");
    caret.className = "dd-combo-caret";
    caret.innerHTML = CARET;
    wrap.appendChild(caret);

    var panel = document.createElement("div");
    panel.className = "dd-panel";
    wrap.appendChild(panel);
    var list = document.createElement("div");
    list.className = "dd-list";
    panel.appendChild(list);

    var rows = opts.map(function (o) {
      var row = document.createElement("div");
      row.className = "dd-option";
      row.dataset.value = o.id;
      row.innerHTML =
        '<span class="dd-opt-id"></span><span class="dd-opt-sub"></span>';
      row.querySelector(".dd-opt-id").textContent = o.id;
      if (o.label && o.label !== o.id) {
        row.querySelector(".dd-opt-sub").textContent = o.label;
      }
      list.appendChild(row);
      row.addEventListener("mousedown", function (e) {
        e.preventDefault();
        input.value = o.id;
        filter();
        closeOpen();
      });
      return { o: o, row: row };
    });

    function apply(q) {
      var any = false;
      rows.forEach(function (r) {
        var hit =
          !q ||
          r.o.id.toLowerCase().indexOf(q) !== -1 ||
          (r.o.label || "").toLowerCase().indexOf(q) !== -1;
        r.row.style.display = hit ? "" : "none";
        r.row.classList.toggle("selected", r.o.id === input.value);
        if (hit) any = true;
      });
      list.style.display = any ? "" : "none";
    }

    // Typing filters by the query; opening shows the full list regardless of
    // the current value (so changing a prefilled model doesn't require clearing).
    function filter() { apply(input.value.trim().toLowerCase()); }
    function showAll() { apply(""); }

    function open() {
      closeOpen();
      showAll();
      wrap.classList.add("open");
      openPanel = wrap;
      place(wrap);
    }

    input.addEventListener("focus", open);
    input.addEventListener("click", function (e) { e.stopPropagation(); open(); });
    input.addEventListener("input", function () {
      if (openPanel !== wrap) { closeOpen(); wrap.classList.add("open"); openPanel = wrap; }
      filter();
      place(wrap);
    });
    caret.addEventListener("click", function (e) {
      e.stopPropagation();
      if (openPanel === wrap) closeOpen();
      else { input.focus(); open(); }
    });
  }

  // Password-manager extensions inject an icon/overlay into ordinary text
  // fields and ignore autocomplete="off". These per-vendor opt-out attributes
  // are the documented way to tell them "this isn't a credential field".
  // Password inputs (the admin login) are deliberately left alone.
  function suppressPasswordManagers(root) {
    (root || document)
      .querySelectorAll("input:not([type=password]):not([type=checkbox]):not([type=radio])")
      .forEach(function (el) {
        el.setAttribute("data-1p-ignore", "true"); // 1Password
        el.setAttribute("data-lpignore", "true"); // LastPass
        el.setAttribute("data-bwignore", "true"); // Bitwarden
        el.setAttribute("data-form-type", "other"); // Dashlane
      });
  }

  function init(root) {
    suppressPasswordManagers(root);
    (root || document).querySelectorAll("input[data-combobox]").forEach(enhanceCombobox);
    (root || document)
      .querySelectorAll(".main select")
      .forEach(function (s) {
        if (!s.closest(".dd")) enhanceSelect(s);
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(); });
  } else {
    init();
  }

  window.__ddInit = init;

  /* --------------------------------------- collapsible create-panel toggles */
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-panel-toggle]");
    if (t) {
      var p = document.getElementById(t.getAttribute("data-panel-toggle"));
      if (p) {
        p.hidden = !p.hidden;
        if (!p.hidden) {
          p.scrollIntoView({ behavior: "smooth", block: "nearest" });
          var f = p.querySelector("input, select, textarea");
          if (f) setTimeout(function () { f.focus(); }, 60);
        }
      }
      return;
    }
    var c = e.target.closest("[data-panel-close]");
    if (c) {
      var p2 = document.getElementById(c.getAttribute("data-panel-close"));
      if (p2) p2.hidden = true;
    }
  });
})();

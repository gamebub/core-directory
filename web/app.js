(() => {
  // Color scheme switcher (light / dark / auto)
  const themeBtn = document.getElementById("theme-toggle");
  const themeMenu = document.getElementById("theme-menu");
  if (themeBtn && themeMenu) {
    const icons = Array.from(themeBtn.querySelectorAll(".theme-icon"));
    const options = Array.from(themeMenu.querySelectorAll(".theme-option"));

    function getStoredTheme() {
      try {
        const val = localStorage.getItem("theme");
        if (val === "light" || val === "dark" || val === "auto") return val;
      } catch (_) {}
      return "auto";
    }

    function applyTheme(mode) {
      if (mode === "light" || mode === "dark") {
        document.documentElement.dataset.theme = mode;
      } else {
        delete document.documentElement.dataset.theme;
      }
      for (const icon of icons) {
        icon.hidden = icon.dataset.icon !== mode;
      }
      for (const opt of options) {
        opt.setAttribute("aria-checked", String(opt.dataset.themeValue === mode));
      }
    }

    function setOpen(open, { focusMenu = false } = {}) {
      themeMenu.hidden = !open;
      themeBtn.setAttribute("aria-expanded", String(open));
      if (open && focusMenu) {
        const active = options.find((opt) => opt.getAttribute("aria-checked") === "true") || options[0];
        active?.focus();
      }
    }

    themeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      setOpen(themeMenu.hidden);
    });

    themeBtn.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setOpen(true, { focusMenu: true });
      }
    });

    for (const opt of options) {
      opt.addEventListener("click", () => {
        const mode = opt.dataset.themeValue || "auto";
        try {
          localStorage.setItem("theme", mode);
        } catch (_) {}
        applyTheme(mode);
        setOpen(false);
        themeBtn.focus();
      });
    }

    themeMenu.addEventListener("keydown", (e) => {
      const idx = options.indexOf(document.activeElement);
      if (e.key === "ArrowDown") {
        e.preventDefault();
        options[(idx + 1 + options.length) % options.length]?.focus();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        options[(idx - 1 + options.length) % options.length]?.focus();
      } else if (e.key === "Home") {
        e.preventDefault();
        options[0]?.focus();
      } else if (e.key === "End") {
        e.preventDefault();
        options[options.length - 1]?.focus();
      } else if (e.key === "Tab") {
        setOpen(false);
      }
    });

    document.addEventListener("click", (e) => {
      if (!themeMenu.hidden && !themeMenu.contains(e.target) && !themeBtn.contains(e.target)) {
        setOpen(false);
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !themeMenu.hidden) {
        setOpen(false);
        themeBtn.focus();
      }
    });

    applyTheme(getStoredTheme());
  }

  // Table search and sorting
  const searchInput = document.getElementById("search-input");
  const countEl = document.getElementById("core-count");
  const tbody = document.getElementById("cores-tbody");
  const headers = Array.from(document.querySelectorAll("th[data-sort]"));

  if (!tbody) return;

  const allRows = Array.from(tbody.querySelectorAll("tr")).filter(
    (tr) => !tr.classList.contains("empty-row")
  );
  const emptyFilterRow = tbody.querySelector(".empty-filter-row");

  let sortKey = "name";
  let sortDir = "asc";

  function applyFilterAndSort() {
    const query = (searchInput?.value || "").trim().toLowerCase();

    const visible = allRows.filter((tr) => {
      const searchText = tr.dataset.search || "";
      return !query || searchText.includes(query);
    });

    visible.sort((a, b) => {
      const av = a.dataset[sortKey] || "";
      const bv = b.dataset[sortKey] || "";
      const cmp = av.localeCompare(bv);
      return sortDir === "asc" ? cmp : -cmp;
    });

    const visibleSet = new Set(visible);
    for (const tr of allRows) {
      tr.hidden = !visibleSet.has(tr);
    }
    for (const tr of visible) {
      tbody.appendChild(tr);
    }
    if (emptyFilterRow) {
      emptyFilterRow.hidden = !(allRows.length > 0 && visible.length === 0);
      tbody.appendChild(emptyFilterRow);
    }

    if (countEl) {
      const total = allRows.length;
      const shown = visible.length;
      countEl.textContent =
        shown === total
          ? `${total} ${total === 1 ? "core" : "cores"}`
          : `${shown} of ${total} cores`;
    }
  }

  for (const th of headers) {
    const trigger = th.querySelector("button") || th;
    trigger.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (!key) return;
      if (sortKey === key) {
        sortDir = sortDir === "asc" ? "desc" : "asc";
      } else {
        sortKey = key;
        sortDir = key === "date" ? "desc" : "asc";
      }
      for (const h of headers) {
        h.removeAttribute("aria-sort");
      }
      th.setAttribute("aria-sort", sortDir === "asc" ? "ascending" : "descending");
      applyFilterAndSort();
    });
  }

  searchInput?.addEventListener("input", applyFilterAndSort);
  applyFilterAndSort();
})();

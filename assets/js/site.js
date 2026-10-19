(function () {
  var toggle = document.querySelector(".menu-toggle");
  var overlay = document.querySelector(".nav-overlay");
  var closeBtn = document.querySelector(".menu-close");

  function setOpen(open) {
    if (!overlay || !toggle) return;
    overlay.classList.toggle("is-open", open);
    overlay.hidden = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    document.body.style.overflow = open ? "hidden" : "";
    if (open && closeBtn) closeBtn.focus();
    if (!open) toggle.focus();
  }

  if (toggle && overlay) {
    overlay.hidden = true;
    toggle.addEventListener("click", function () {
      setOpen(true);
    });
    if (closeBtn) {
      closeBtn.addEventListener("click", function () {
        setOpen(false);
      });
    }
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && overlay.classList.contains("is-open")) {
        setOpen(false);
      }
    });
  }

  var input = document.querySelector("[data-search]");
  if (!input) return;

  var results = document.querySelector("[data-results]");
  var status = document.querySelector("[data-search-status]");
  var index = [];

  function render(items, query) {
    if (!results) return;
    if (!query) {
      results.innerHTML = "";
      if (status) status.textContent = "Type to search posts and pages.";
      return;
    }
    if (!items.length) {
      results.innerHTML = '<p class="empty">No matches.</p>';
      if (status) status.textContent = "No matches.";
      return;
    }
    var html = '<ul class="feed">';
    items.slice(0, 40).forEach(function (item) {
      var thumb = item.image
        ? '<img class="feed-thumb" src="' + escapeAttr(item.image) + '" alt="">'
        : "";
      html +=
        '<li class="feed-row' + (item.image ? "" : " no-thumb") + '">' +
        '<div class="feed-date">' + escapeHtml(item.dateLabel || "") + "</div>" +
        '<h2 class="feed-title"><a href="' + escapeAttr(item.url) + '">' + escapeHtml(item.title) + "</a></h2>" +
        thumb +
        "</li>";
    });
    html += "</ul>";
    results.innerHTML = html;
    if (status) {
      status.textContent = items.length + (items.length === 1 ? " match" : " matches");
    }
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/"/g, "&quot;");
  }

  function run() {
    var query = input.value.trim().toLowerCase();
    if (!query) {
      render([], "");
      return;
    }
    var words = query.split(/\s+/).filter(Boolean);
    var found = index.filter(function (item) {
      var hay = item.haystack;
      return words.every(function (word) { return hay.indexOf(word) !== -1; });
    });
    render(found, query);
  }

  fetch("/assets/search-index.json")
    .then(function (response) { return response.json(); })
    .then(function (data) {
      index = data.map(function (item) {
        item.haystack = (item.title + " " + item.text + " " + (item.terms || "")).toLowerCase();
        return item;
      });
      var params = new URLSearchParams(window.location.search);
      var preset = params.get("q");
      if (preset) {
        input.value = preset;
        run();
      }
    })
    .catch(function () {
      if (status) status.textContent = "Search index could not be loaded.";
    });

  input.addEventListener("input", run);
})();
